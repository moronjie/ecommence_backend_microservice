from datetime import UTC, datetime, timedelta

from conftest import as_user, uniform


# ---- viewing with live prices and stock -----------------------------------------------
async def test_view_shows_price_changes_but_remembers_the_added_price(env):
    pid = await env.product(price=1000)
    await env.add(pid, 2)
    await env.change_product(pid, price_cents=1200)

    line = (await env.view())["items"][0]
    assert line["unit_price_cents"] == 1200 and line["added_price_cents"] == 1000
    assert line["price_changed"] is True and line["line_total_cents"] == 2400
    assert (await env.view())["has_issues"] is True


async def test_view_flags_products_that_were_hidden(env):
    keep, hidden = await env.product(name="Keep"), await env.product(name="Gone", price=500)
    await env.add(keep)
    await env.add(hidden)
    await env.change_product(hidden, is_active=False)

    data = await env.view()
    flags = {i["name"]: i["purchasable"] for i in data["items"]}
    assert flags == {"Keep": True, "Gone": False}
    assert data["subtotal_cents"] == 1000  # the hidden product is not counted


async def test_view_flags_insufficient_stock(env):
    pid = await env.product(stock=10)
    await env.add(pid, 5)
    env.backend.stock[pid] = 3
    line = (await env.view())["items"][0]
    assert line["stock_available"] == 3 and line["purchasable"] is False


async def test_view_still_works_when_inventory_is_down(env):
    pid = await env.product()
    await env.add(pid, 2)
    env.backend.inventory_down = True
    data = uniform(await env.client.get("/api/v1/cart", headers=as_user()), 200, True)["data"]
    line = data["items"][0]
    assert line["stock_available"] is None and line["purchasable"] is True  # unknown, not failed


async def test_view_tells_inventory_who_is_asking(env):
    pid = await env.product()
    await env.add(pid)
    await env.view()
    assert "POST /api/v1/inventory/availability" in env.backend.calls


# ---- product events -------------------------------------------------------------------
async def test_events_keep_the_local_copy_current(env):
    pid = await env.product(price=1000)
    await env.add(pid)
    await env.change_product(pid, name="Renamed", price_cents=900)
    line = (await env.view())["items"][0]
    assert (line["name"], line["unit_price_cents"]) == ("Renamed", 900)


async def test_a_late_older_event_does_not_overwrite_newer_data(env):
    pid = await env.product(price=1000)
    await env.add(pid)
    now = datetime.now(UTC)
    await env.change_product(pid, price_cents=1300)  # newest
    old = {
        "product_id": pid,
        "sku": "S",
        "name": "Mug",
        "price_cents": 700,
        "currency": "USD",
        "is_active": True,
    }
    await env.send("product.updated", old, occurred_at=(now - timedelta(hours=1)).isoformat())
    assert (await env.view())["items"][0]["unit_price_cents"] == 1300


async def test_replayed_event_is_harmless(env):
    pid = await env.product(price=1000)
    data = {
        "product_id": pid,
        "sku": "S",
        "name": "Mug",
        "price_cents": 1000,
        "currency": "USD",
        "is_active": True,
    }
    stamp = datetime.now(UTC).isoformat()
    await env.send("product.updated", data, occurred_at=stamp)
    await env.send("product.updated", data, occurred_at=stamp)
    assert env.dlq.events == []


async def test_bad_product_events_go_to_the_dlq(env):
    for payload in (
        {"product_id": "nope", "name": "x", "price_cents": 1, "currency": "USD", "is_active": True},
        {
            "product_id": "5f3c1c0e-8c1a-4a53-9a79-5b8b7f5f4a11",
            "name": "x",
            "price_cents": -5,
            "currency": "USD",
            "is_active": True,
        },
        {"product_id": "5f3c1c0e-8c1a-4a53-9a79-5b8b7f5f4a11"},
    ):
        await env.send("product.created", payload)
    assert len(env.dlq.events) == 3
