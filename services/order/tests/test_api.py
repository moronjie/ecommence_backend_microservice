import uuid

from conftest import ADA, ADMIN, BOB, as_user, uniform


async def get(env, order_id, headers):
    return await env.client.get(f"/api/v1/orders/{order_id}", headers=headers)


# ---- reading --------------------------------------------------------------------------
async def test_order_detail_has_items_address_and_history(env):
    order_id = await env.checkout(user=ADA)
    data = uniform(await get(env, order_id, as_user(ADA)), 200, True)["data"]
    assert data["status"] == "pending" and data["total_cents"] == 6500
    assert {i["name"] for i in data["items"]} == {"Mug", "Pan"}
    assert data["shipping_address"]["city"] == "London"
    assert [h["to_status"] for h in data["history"]] == ["pending"]


async def test_customers_only_see_their_own_orders(env):
    mine, theirs = await env.checkout(user=ADA), await env.checkout(user=BOB)
    listing = uniform(await env.client.get("/api/v1/orders", headers=as_user(ADA)), 200, True)[
        "data"
    ]
    assert [o["id"] for o in listing["items"]] == [mine] and listing["total"] == 1

    # someone else's order looks exactly like one that does not exist
    other = uniform(await get(env, theirs, as_user(ADA)), 404, False)
    missing = uniform(await get(env, uuid.uuid4(), as_user(ADA)), 404, False)
    assert other["error_message"] == missing["error_message"]


async def test_admins_can_see_everything_and_filter(env):
    a, b = await env.checkout(user=ADA), await env.checkout(user=BOB)
    await env.send("inventory.failed", {"order_id": b, "reason": "x"})

    everything = uniform(await env.client.get("/api/v1/orders", headers=ADMIN), 200, True)["data"]
    assert everything["total"] == 2
    one = await env.client.get(f"/api/v1/orders?owner={BOB}", headers=ADMIN)
    assert [o["id"] for o in uniform(one, 200, True)["data"]["items"]] == [b]
    cancelled = await env.client.get("/api/v1/orders?status=cancelled", headers=ADMIN)
    assert [o["id"] for o in uniform(cancelled, 200, True)["data"]["items"]] == [b]
    assert (await get(env, a, ADMIN)).status_code == 200


async def test_customers_cannot_browse_other_customers(env):
    uniform(await env.client.get(f"/api/v1/orders?owner={BOB}", headers=as_user(ADA)), 403, False)
    own = await env.client.get(f"/api/v1/orders?owner={ADA}", headers=as_user(ADA))
    uniform(own, 200, True)  # asking for yourself is fine


async def test_pagination_and_bad_parameters(env):
    for _ in range(3):
        await env.checkout(user=ADA)
    page = uniform(await env.client.get("/api/v1/orders?size=2", headers=as_user(ADA)), 200, True)
    assert page["data"]["total"] == 3 and len(page["data"]["items"]) == 2
    for query in ("size=1000", "page=0", "status=teleported", "owner=nope"):
        uniform(await env.client.get(f"/api/v1/orders?{query}", headers=ADMIN), 422, False)


async def test_identity_is_required(env):
    uniform(await env.client.get("/api/v1/orders"), 401, False)
    uniform(await env.client.get("/api/v1/orders", headers={"X-User-Id": "nope"}), 401, False)


# ---- cancelling -----------------------------------------------------------------------
async def test_customer_can_cancel_before_payment(env):
    for prepare in (env.checkout, lambda **kw: _awaiting(env, **kw)):
        order_id = await prepare(user=ADA)
        resp = await env.client.post(f"/api/v1/orders/{order_id}/cancel", headers=as_user(ADA))
        data = uniform(resp, 200, True)["data"]
        assert data["status"] == "cancelled" and data["cancel_reason"] == "cancelled by customer"
    assert len(await env.published("order.cancelled")) == 2  # inventory releases the stock on this


async def _awaiting(env, **kw):
    return await env.reserved(await env.checkout(**kw))


async def test_cancelling_twice_is_fine_but_publishes_once(env):
    order_id = await env.checkout(user=ADA)
    for _ in range(2):
        uniform(
            await env.client.post(f"/api/v1/orders/{order_id}/cancel", headers=as_user(ADA)),
            200,
            True,
        )
    assert len(await env.published("order.cancelled")) == 1


async def test_a_paid_order_cannot_be_cancelled_by_the_customer(env):
    order_id = await env.confirmed_order(user=ADA)
    uniform(
        await env.client.post(f"/api/v1/orders/{order_id}/cancel", headers=as_user(ADA)), 409, False
    )
    assert await env.status(order_id) == "confirmed"


async def test_cancelling_someone_elses_order_is_404_but_an_admin_may(env):
    order_id = await env.checkout(user=BOB)
    uniform(
        await env.client.post(f"/api/v1/orders/{order_id}/cancel", headers=as_user(ADA)), 404, False
    )
    resp = await env.client.post(f"/api/v1/orders/{order_id}/cancel", headers=ADMIN)
    assert uniform(resp, 200, True)["data"]["cancel_reason"] == "cancelled by administrator"


# ---- fulfilment -----------------------------------------------------------------------
async def test_only_admins_ship_and_deliver(env):
    order_id = await env.confirmed_order(user=ADA)
    for action in ("ship", "deliver"):
        resp = await env.client.post(f"/api/v1/orders/{order_id}/{action}", headers=as_user(ADA))
        uniform(resp, 403, False)


async def test_orders_move_through_fulfilment_in_order_only(env):
    unpaid = await env.checkout()
    uniform(await env.client.post(f"/api/v1/orders/{unpaid}/ship", headers=ADMIN), 409, False)

    paid = await env.confirmed_order()
    uniform(
        await env.client.post(f"/api/v1/orders/{paid}/deliver", headers=ADMIN), 409, False
    )  # not shipped
    uniform(await env.client.post(f"/api/v1/orders/{paid}/ship", headers=ADMIN), 200, True)
    uniform(
        await env.client.post(f"/api/v1/orders/{paid}/ship", headers=ADMIN), 409, False
    )  # twice
    uniform(await env.client.post(f"/api/v1/orders/{paid}/deliver", headers=ADMIN), 200, True)
    uniform(
        await env.client.post(f"/api/v1/orders/{paid}/cancel", headers=ADMIN), 409, False
    )  # too late


async def test_unknown_order(env):
    ghost = uuid.uuid4()
    uniform(await env.client.post(f"/api/v1/orders/{ghost}/ship", headers=ADMIN), 404, False)
    uniform(await env.client.post(f"/api/v1/orders/{ghost}/deliver", headers=ADMIN), 404, False)
    uniform(await get(env, ghost, ADMIN), 404, False)
    uniform(await env.client.post(f"/api/v1/orders/{ghost}/cancel", headers=ADMIN), 404, False)
    uniform(await env.client.get("/api/v1/orders/not-a-uuid", headers=ADMIN), 422, False)


async def test_health_and_ready(env):
    uniform(await env.client.get("/health"), 200, True)
    uniform(await env.client.get("/ready"), 200, True)
