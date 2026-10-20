import asyncio
import uuid

from conftest import as_user, uniform


async def test_empty_cart_view(env):
    data = uniform(await env.client.get("/api/v1/cart", headers=as_user()), 200, True)["data"]
    assert data["items"] == [] and data["subtotal_cents"] == 0 and data["item_count"] == 0


async def test_identity_is_required(env):
    uniform(await env.client.get("/api/v1/cart"), 401, False)
    resp = await env.client.post("/api/v1/cart/items", json={"product_id": str(uuid.uuid4())})
    uniform(resp, 401, False)


# ---- adding ---------------------------------------------------------------------------
async def test_add_item_uses_the_local_copy_without_calling_other_services(env):
    pid = await env.product(price=1500, name="Blue Mug")
    data = uniform(await env.add(pid, 2), 200, True)["data"]
    line = data["items"][0]
    assert (line["name"], line["quantity"], line["unit_price_cents"]) == ("Blue Mug", 2, 1500)
    assert data["subtotal_cents"] == 3000 and data["item_count"] == 2
    assert not any("/products/" in c for c in env.backend.calls)  # no product lookup needed


async def test_add_same_product_again_increases_quantity(env):
    pid = await env.product()
    await env.add(pid, 2)
    data = uniform(await env.add(pid, 3), 200, True)["data"]
    assert len(data["items"]) == 1 and data["items"][0]["quantity"] == 5


async def test_unknown_to_the_local_copy_falls_back_to_the_product_service(env):
    pid = await env.product(via_event=False)  # e.g. Redis was wiped
    uniform(await env.add(pid), 200, True)
    assert sum("/products/" in c for c in env.backend.calls) == 1
    await env.add(pid)  # now remembered
    assert sum("/products/" in c for c in env.backend.calls) == 1


async def test_unavailable_products_cannot_be_added(env):
    hidden = await env.product(active=False)
    uniform(await env.add(hidden), 404, False)
    uniform(await env.add(str(uuid.uuid4())), 404, False)  # exists nowhere


async def test_product_service_down_and_product_unknown_is_503(env):
    pid = await env.product(via_event=False)
    env.backend.product_service_down = True
    uniform(await env.add(pid), 503, False)


async def test_limits(env):
    pid = await env.product()
    uniform(await env.add(pid, 99), 200, True)
    uniform(await env.add(pid, 1), 409, False)  # 100 of one product
    for _ in range(env.settings.max_lines - 1):
        uniform(await env.add(await env.product()), 200, True)
    uniform(await env.add(await env.product()), 409, False)  # one product too many


async def test_cart_has_a_single_currency(env):
    usd, eur = await env.product(currency="USD"), await env.product(currency="EUR")
    uniform(await env.add(usd), 200, True)
    uniform(await env.add(eur), 409, False)


async def test_validation(env):
    pid = await env.product()
    for body in (
        {"product_id": pid, "quantity": 0},
        {"product_id": pid, "quantity": 100},
        {"product_id": pid, "quantity": 1.5},
        {"product_id": "not-a-uuid"},
        {},
    ):
        resp = await env.client.post("/api/v1/cart/items", json=body, headers=as_user())
        uniform(resp, 422, False)


# ---- changing and removing ------------------------------------------------------------
async def test_set_quantity_remove_and_clear(env):
    a, b = await env.product(name="A"), await env.product(name="B")
    await env.add(a, 2)
    await env.add(b)

    resp = await env.client.put(f"/api/v1/cart/items/{a}", json={"quantity": 7}, headers=as_user())
    items = {i["name"]: i["quantity"] for i in uniform(resp, 200, True)["data"]["items"]}
    assert items == {"A": 7, "B": 1}

    resp = await env.client.delete(f"/api/v1/cart/items/{a}", headers=as_user())
    assert [i["name"] for i in uniform(resp, 200, True)["data"]["items"]] == ["B"]

    uniform(await env.client.delete("/api/v1/cart", headers=as_user()), 200, True)
    assert (await env.view())["items"] == []


async def test_removing_the_last_item_empties_the_cart(env):
    pid = await env.product()
    await env.add(pid)
    await env.client.delete(f"/api/v1/cart/items/{pid}", headers=as_user())
    assert await env.redis.exists("cart:user-1") == 0


async def test_changing_something_not_in_the_cart_is_404(env):
    pid = await env.product()
    url = f"/api/v1/cart/items/{pid}"
    uniform(await env.client.put(url, json={"quantity": 2}, headers=as_user()), 404, False)
    uniform(await env.client.delete(url, headers=as_user()), 404, False)
    await env.add(await env.product())
    uniform(await env.client.put(url, json={"quantity": 2}, headers=as_user()), 404, False)


async def test_quantity_validation(env):
    pid = await env.product()
    await env.add(pid)
    for q in (0, 100, -1):
        resp = await env.client.put(
            f"/api/v1/cart/items/{pid}", json={"quantity": q}, headers=as_user()
        )
        uniform(resp, 422, False)


# ---- isolation, expiry, concurrency ---------------------------------------------------
async def test_carts_are_per_user(env):
    pid = await env.product()
    await env.add(pid, 2, user="ann")
    assert (await env.view("bob"))["items"] == []
    assert (await env.view("ann"))["items"][0]["quantity"] == 2


async def test_cart_expires_after_inactivity_and_ttl_is_renewed(env):
    pid = await env.product()
    await env.add(pid)
    thirty_days = env.settings.cart_ttl_days * 86400
    assert thirty_days - 5 <= await env.redis.ttl("cart:user-1") <= thirty_days
    await env.redis.expire("cart:user-1", 100)
    await env.add(pid)  # activity renews it
    assert await env.redis.ttl("cart:user-1") > thirty_days - 5


async def test_concurrent_adds_are_not_lost(env):
    pid = await env.product()
    results = await asyncio.gather(*(env.add(pid) for _ in range(8)))
    assert all(r.status_code == 200 for r in results)
    assert (await env.view())["items"][0]["quantity"] == 8
