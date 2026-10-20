import uuid

from app.models.inventory import StockMovement
from conftest import ADMIN, CUSTOMER, uniform
from sqlalchemy import func, select


# ---- product events -------------------------------------------------------------------
async def test_product_created_makes_an_empty_stock_row_once(env):
    pid = str(uuid.uuid4())
    await env.send("product.created", {"product_id": pid, "sku": "A1"}, event_id="e1")
    await env.send("product.created", {"product_id": pid, "sku": "A1"}, event_id="e1")  # replay
    await env.send("product.created", {"product_id": pid, "sku": "A1"})  # re-issued
    assert await env.stock(pid) == (0, 0) and env.dlq.events == []


async def test_product_updated_syncs_sku_and_creates_if_missing(env):
    pid = await env.product(5, "OLD")
    await env.send("product.updated", {"product_id": pid, "sku": "NEW"})
    body = uniform(await env.client.get(f"/api/v1/inventory/{pid}", headers=ADMIN), 200, True)
    assert body["data"]["sku"] == "NEW" and body["data"]["on_hand"] == 5  # stock untouched

    late = str(uuid.uuid4())  # product.updated overtook product.created
    await env.send("product.updated", {"product_id": late, "sku": "L1"})
    assert await env.stock(late) == (0, 0)


# ---- who can see and change what ------------------------------------------------------
async def test_customers_see_availability_but_not_warehouse_numbers(env):
    pid = await env.product(10)
    await env.order([(pid, 4)])
    customer = uniform(
        await env.client.get(f"/api/v1/inventory/{pid}", headers=CUSTOMER), 200, True
    )
    assert customer["data"] == {"product_id": pid, "sku": "SKU-1", "available": 6, "in_stock": True}
    admin = uniform(await env.client.get(f"/api/v1/inventory/{pid}", headers=ADMIN), 200, True)
    assert (admin["data"]["on_hand"], admin["data"]["reserved"], admin["data"]["available"]) == (
        10,
        4,
        6,
    )


async def test_identity_is_required_and_writes_are_admin_only(env):
    pid = await env.product(1)
    uniform(await env.client.get(f"/api/v1/inventory/{pid}"), 401, False)
    for method, url, body in [
        ("put", f"/api/v1/inventory/{pid}", {"on_hand": 5}),
        ("post", f"/api/v1/inventory/{pid}/adjust", {"delta": 5}),
    ]:
        uniform(await getattr(env.client, method)(url, json=body, headers=CUSTOMER), 403, False)
    uniform(await env.client.get("/api/v1/inventory", headers=CUSTOMER), 403, False)


async def test_unknown_product_is_404(env):
    ghost = uuid.uuid4()
    uniform(await env.client.get(f"/api/v1/inventory/{ghost}", headers=ADMIN), 404, False)
    uniform(
        await env.client.put(f"/api/v1/inventory/{ghost}", json={"on_hand": 1}, headers=ADMIN),
        404,
        False,
    )
    uniform(
        await env.client.post(
            f"/api/v1/inventory/{ghost}/adjust", json={"delta": 1}, headers=ADMIN
        ),
        404,
        False,
    )


# ---- setting and adjusting stock ------------------------------------------------------
async def test_set_and_adjust_stock(env):
    pid = await env.product()
    url = f"/api/v1/inventory/{pid}"
    uniform(await env.client.put(url, json={"on_hand": 20}, headers=ADMIN), 200, True)
    resp = await env.client.post(
        f"{url}/adjust", json={"delta": 5, "reason": "delivery"}, headers=ADMIN
    )
    assert uniform(resp, 200, True)["data"]["on_hand"] == 25
    resp = await env.client.post(
        f"{url}/adjust", json={"delta": -3, "reason": "damaged"}, headers=ADMIN
    )
    assert uniform(resp, 200, True)["data"]["on_hand"] == 22
    async with env.session_factory() as s:
        count = await s.scalar(select(func.count()).select_from(StockMovement))
    assert count == 3  # one ledger row per change


async def test_cannot_take_stock_below_what_is_promised(env):
    pid = await env.product(10)
    await env.order([(pid, 6)])
    url = f"/api/v1/inventory/{pid}"
    uniform(await env.client.put(url, json={"on_hand": 5}, headers=ADMIN), 409, False)
    uniform(await env.client.post(f"{url}/adjust", json={"delta": -5}, headers=ADMIN), 409, False)
    uniform(
        await env.client.post(f"{url}/adjust", json={"delta": -4}, headers=ADMIN), 200, True
    )  # to 6
    assert await env.stock(pid) == (6, 6)


async def test_cannot_go_negative_or_send_bad_values(env):
    pid = await env.product(2)
    url = f"/api/v1/inventory/{pid}"
    uniform(await env.client.post(f"{url}/adjust", json={"delta": -3}, headers=ADMIN), 409, False)
    uniform(await env.client.put(url, json={"on_hand": -1}, headers=ADMIN), 422, False)
    uniform(await env.client.put(url, json={"on_hand": 1.5}, headers=ADMIN), 422, False)
    uniform(
        await env.client.post(f"{url}/adjust", json={"delta": "lots"}, headers=ADMIN), 422, False
    )


# ---- availability and listing ---------------------------------------------------------
async def test_batch_availability_includes_unknown_products_as_zero(env):
    have, ghost = await env.product(4), str(uuid.uuid4())
    resp = await env.client.post(
        "/api/v1/inventory/availability",
        json={"product_ids": [have, ghost, have]},  # a duplicate is ignored
        headers=CUSTOMER,
    )
    items = uniform(resp, 200, True)["data"]["items"]
    assert [(i["product_id"], i["available"], i["in_stock"]) for i in items] == [
        (have, 4, True),
        (ghost, 0, False),
    ]


async def test_batch_availability_validation(env):
    for ids in ([], [str(uuid.uuid4())] * 101, ["nope"]):
        resp = await env.client.post(
            "/api/v1/inventory/availability", json={"product_ids": ids}, headers=CUSTOMER
        )
        uniform(resp, 422, False)


async def test_admin_list_with_low_stock_filter_and_paging(env):
    await env.product(0, "A-EMPTY")
    await env.product(2, "B-LOW")
    await env.product(50, "C-PLENTY")
    everything = uniform(
        await env.client.get("/api/v1/inventory?size=2", headers=ADMIN), 200, True
    )["data"]
    assert everything["total"] == 3 and len(everything["items"]) == 2
    low = uniform(
        await env.client.get("/api/v1/inventory?max_available=5", headers=ADMIN), 200, True
    )["data"]
    assert [i["sku"] for i in low["items"]] == ["A-EMPTY", "B-LOW"]


async def test_health_and_ready(env):
    uniform(await env.client.get("/health"), 200, True)
    uniform(await env.client.get("/ready"), 200, True)
