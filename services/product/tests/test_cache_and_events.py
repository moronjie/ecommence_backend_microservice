import json

from conftest import ADMIN, uniform


def cache_state(resp) -> str:
    return resp.headers["x-cache"]


# ---- product detail cache -------------------------------------------------------------
async def test_detail_miss_then_hit_and_redis_holds_it_with_ttl(env):
    p = await env.product()
    url = f"/api/v1/products/{p['id']}"
    assert cache_state(await env.client.get(url)) == "MISS"
    assert cache_state(await env.client.get(url)) == "HIT"
    key = f"product:product:{p['id']}"
    assert json.loads(await env.redis.get(key))["sku"] == "SKU-1"
    assert 0 < await env.redis.ttl(key) <= 300


async def test_update_invalidates_detail_cache(env):
    p = await env.product(price_cents=1000)
    url = f"/api/v1/products/{p['id']}"
    await env.client.get(url)
    assert cache_state(await env.client.get(url)) == "HIT"

    await env.client.patch(url, json={"price_cents": 2000}, headers=ADMIN)
    after = await env.client.get(url)
    assert cache_state(after) == "MISS" and after.json()["data"]["price_cents"] == 2000


async def test_delete_invalidates_so_public_cannot_see_stale_copy(env):
    p = await env.product()
    url = f"/api/v1/products/{p['id']}"
    await env.client.get(url)  # now cached as active
    await env.client.delete(url, headers=ADMIN)
    uniform(await env.client.get(url), 404, False)


async def test_hidden_cached_product_is_still_hidden_from_public(env):
    p = await env.product(is_active=False)
    url = f"/api/v1/products/{p['id']}"
    assert cache_state(await env.client.get(url, headers=ADMIN)) == "MISS"  # caches it
    uniform(await env.client.get(url), 404, False)  # cache hit must not leak it


async def test_not_found_is_not_cached(env):
    resp = await env.client.get("/api/v1/products/00000000-0000-0000-0000-000000000000")
    uniform(resp, 404, False)
    assert await env.redis.keys("product:product:*") == []


# ---- list cache -----------------------------------------------------------------------
async def test_list_miss_then_hit_per_distinct_query(env):
    await env.product()
    assert cache_state(await env.client.get("/api/v1/products?q=mug")) == "MISS"
    assert cache_state(await env.client.get("/api/v1/products?q=MUG ")) == "HIT"  # same search
    assert cache_state(await env.client.get("/api/v1/products?q=mug&page=2")) == "MISS"


async def test_any_product_change_invalidates_all_cached_lists(env):
    await env.product("A", "Alpha")
    first = await env.client.get("/api/v1/products")
    assert cache_state(await env.client.get("/api/v1/products")) == "HIT"
    assert first.json()["data"]["total"] == 1

    await env.product("B", "Beta")  # create invalidates
    after = await env.client.get("/api/v1/products")
    assert cache_state(after) == "MISS" and after.json()["data"]["total"] == 2

    pid = after.json()["data"]["items"][0]["id"]
    await env.client.patch(f"/api/v1/products/{pid}", json={"name": "Renamed"}, headers=ADMIN)
    assert cache_state(await env.client.get("/api/v1/products")) == "MISS"  # update invalidates

    assert cache_state(await env.client.get("/api/v1/products")) == "HIT"
    await env.client.delete(f"/api/v1/products/{pid}", headers=ADMIN)
    gone = await env.client.get("/api/v1/products")
    assert cache_state(gone) == "MISS" and gone.json()["data"]["total"] == 1  # delete invalidates


async def test_admin_views_are_never_cached(env):
    await env.product()
    for _ in range(2):
        resp = await env.client.get("/api/v1/products?include_inactive=true", headers=ADMIN)
        assert cache_state(resp) == "MISS"


async def test_categories_cached_and_invalidated(env):
    await env.category("Books")
    assert cache_state(await env.client.get("/api/v1/categories")) == "MISS"
    assert cache_state(await env.client.get("/api/v1/categories")) == "HIT"
    await env.category("Toys")
    fresh = await env.client.get("/api/v1/categories")
    assert cache_state(fresh) == "MISS" and len(fresh.json()["data"]["items"]) == 2


async def test_requests_still_work_when_redis_is_down(env):
    p = await env.product()

    async def down(*args, **kwargs):
        raise ConnectionError("redis down")

    for name in ("get", "set", "delete", "incr"):
        setattr(env.redis, name, down)
    resp = await env.client.get(f"/api/v1/products/{p['id']}")
    assert uniform(resp, 200, True)["data"]["sku"] == "SKU-1" and cache_state(resp) == "MISS"
    uniform(await env.client.get("/api/v1/products"), 200, True)
    uniform(
        await env.client.patch(f"/api/v1/products/{p['id']}", json={"name": "Z"}, headers=ADMIN),
        200,
        True,
    )


# ---- events ---------------------------------------------------------------------------
async def test_create_and_update_publish_events_with_snapshot(env):
    cat = await env.category()
    p = await env.product(category_id=cat["id"], price_cents=1000)
    await env.client.patch(f"/api/v1/products/{p['id']}", json={"price_cents": 1200}, headers=ADMIN)

    assert await env.relay.publish_pending() == 2
    (t1, k1, created), (t2, k2, updated) = env.publisher.events
    assert (t1, t2) == ("product.created", "product.updated") and k1 == k2 == p["id"]
    assert created["payload"] == {
        "product_id": p["id"],
        "sku": "SKU-1",
        "name": "Blue Mug",
        "price_cents": 1000,
        "currency": "USD",
        "category_id": cat["id"],
        "is_active": True,
    }
    assert updated["payload"]["price_cents"] == 1200


async def test_delete_publishes_update_with_is_active_false(env):
    p = await env.product()
    await env.client.delete(f"/api/v1/products/{p['id']}", headers=ADMIN)
    await env.relay.publish_pending()
    topic, _, event = env.publisher.events[-1]
    assert topic == "product.updated" and event["payload"]["is_active"] is False


async def test_noop_update_publishes_nothing_and_keeps_cache(env):
    p = await env.product(name="Mug")
    url = f"/api/v1/products/{p['id']}"
    await env.client.get(url)
    await env.client.patch(url, json={"name": "Mug"}, headers=ADMIN)  # same value
    assert cache_state(await env.client.get(url)) == "HIT"
    assert await env.relay.publish_pending() == 1  # only product.created


async def test_failed_write_publishes_nothing(env):
    await env.product("DUP")
    body = {"sku": "dup", "name": "Other", "price_cents": 1}
    uniform(await env.client.post("/api/v1/products", json=body, headers=ADMIN), 409, False)
    assert await env.relay.publish_pending() == 1  # just the first create
