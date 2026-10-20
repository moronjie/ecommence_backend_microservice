import uuid
from datetime import UTC, datetime, timedelta

from app.models.order import Order, StatusChange
from conftest import ADA, ADDRESS, ADMIN, as_user
from sqlalchemy import func, select, update


async def count(env, model) -> int:
    async with env.session_factory() as s:
        return await s.scalar(select(func.count()).select_from(model))


async def history(env, order_id: str) -> list[tuple[str | None, str]]:
    async with env.session_factory() as s:
        order = await s.get(Order, uuid.UUID(order_id))
        return [(h.from_status, h.to_status) for h in order.history]


# ---- creating an order ----------------------------------------------------------------
async def test_checkout_creates_a_pending_order_and_asks_inventory_to_reserve(env):
    order_id = await env.checkout(user=ADA)
    assert await env.status(order_id) == "pending"

    async with env.session_factory() as s:
        order = await s.get(Order, uuid.UUID(order_id))
    assert (order.total_cents, order.currency, str(order.user_id)) == (6500, "USD", ADA)
    assert order.shipping_address == ADDRESS  # kept with the order
    assert sorted((i.name, i.quantity, i.unit_price_cents) for i in order.items) == [
        ("Mug", 2, 1000),
        ("Pan", 1, 4500),
    ]

    (event,) = await env.published("order.created")
    assert event["order_id"] == order_id and event["total_cents"] == 6500
    # exactly what the inventory service needs
    assert sorted(i["quantity"] for i in event["items"]) == [1, 2]
    assert await history(env, order_id) == [(None, "pending")]


async def test_the_same_checkout_never_makes_two_orders(env):
    payload = env.checkout_payload()
    await env.send("cart.checked_out", payload, event_id="evt-1")
    await env.send("cart.checked_out", payload, event_id="evt-1")  # Kafka redelivery
    await env.send("cart.checked_out", payload)  # the cart retried with a new event id
    assert await count(env, Order) == 1
    assert len(await env.published("order.created")) == 1


async def test_bad_checkouts_go_to_the_dlq_and_create_nothing(env):
    good = env.checkout_payload()
    bad = [
        {**good, "total_cents": good["total_cents"] + 1},  # does not add up
        {**good, "items": []},
        {**good, "user_id": "not-a-uuid"},
        {**good, "currency": "DOLLARS"},
    ]
    for payload in bad:
        await env.send("cart.checked_out", payload)
    assert len(env.dlq.events) == len(bad) and await count(env, Order) == 0


# ---- the happy path -------------------------------------------------------------------
async def test_full_life_of_an_order(env):
    order_id = await env.checkout()

    await env.reserved(order_id)
    assert await env.status(order_id) == "awaiting_payment"
    (ask,) = await env.published("order.payment_requested")
    assert ask["order_id"] == order_id and ask["total_cents"] == 6500  # what to charge

    await env.paid(order_id)
    assert await env.status(order_id) == "confirmed"
    assert len(await env.published("order.confirmed")) == 1

    for action, expected, topic in (
        ("ship", "shipped", "order.shipped"),
        ("deliver", "delivered", "order.delivered"),
    ):
        resp = await env.client.post(f"/api/v1/orders/{order_id}/{action}", headers=ADMIN)
        assert resp.status_code == 200, resp.text
        assert await env.status(order_id) == expected
        assert len(await env.published(topic)) == 1

    assert await history(env, order_id) == [
        (None, "pending"),
        ("pending", "awaiting_payment"),
        ("awaiting_payment", "confirmed"),
        ("confirmed", "shipped"),
        ("shipped", "delivered"),
    ]


# ---- things going wrong ---------------------------------------------------------------
async def test_out_of_stock_cancels_the_order(env):
    order_id = await env.checkout()
    await env.send("inventory.failed", {"order_id": order_id, "reason": "insufficient stock"})
    assert await env.status(order_id) == "cancelled"
    (event,) = await env.published("order.cancelled")
    assert event["order_id"] == order_id and event["reason"] == "insufficient stock"


async def test_failed_payment_cancels_the_order(env):
    order_id = await env.reserved(await env.checkout())
    await env.send("payment.failed", {"order_id": order_id, "reason": "card declined"})
    assert await env.status(order_id) == "cancelled"
    (event,) = await env.published("order.cancelled")
    assert event["reason"] == "payment failed: card declined"  # inventory frees the stock on this


async def test_an_expired_stock_hold_cancels_an_unpaid_order(env):
    order_id = await env.reserved(await env.checkout())
    await env.send("inventory.released", {"order_id": order_id, "reason": "expired"})
    assert await env.status(order_id) == "cancelled"


async def test_a_hold_we_released_ourselves_changes_nothing(env):
    order_id = await env.reserved(await env.checkout())
    await env.send("inventory.released", {"order_id": order_id, "reason": "cancelled"})
    assert await env.status(order_id) == "awaiting_payment"


async def test_an_expired_hold_does_not_touch_a_paid_order(env):
    order_id = await env.confirmed_order()
    await env.send("inventory.released", {"order_id": order_id, "reason": "expired"})
    assert await env.status(order_id) == "confirmed"


async def test_events_for_an_unknown_order_are_dead_lettered(env):
    await env.send("inventory.reserved", {"order_id": str(uuid.uuid4())})
    assert env.dlq.events[0][0] == "inventory.reserved.dlq"


# ---- messages arriving late, twice, or in the wrong order ------------------------------
async def test_duplicate_messages_are_harmless(env):
    order_id = await env.checkout()
    for _ in range(2):
        await env.send("inventory.reserved", {"order_id": order_id}, event_id="same")
    for _ in range(2):
        await env.send("payment.completed", {"order_id": order_id}, event_id="same-pay")
    assert await env.status(order_id) == "confirmed"
    assert len(await env.published("order.confirmed")) == 1


async def test_the_same_result_with_a_new_event_id_is_ignored_too(env):
    order_id = await env.confirmed_order()
    await env.paid(order_id)  # a different event id for an already-paid order
    assert await env.status(order_id) == "confirmed"
    assert len(await env.published("order.confirmed")) == 1


async def test_payment_overtaking_the_reservation_still_confirms(env):
    order_id = await env.checkout()
    await env.paid(order_id)  # arrives before inventory.reserved
    assert await env.status(order_id) == "confirmed"
    await env.reserved(order_id)  # now the late one: ignored
    assert await env.status(order_id) == "confirmed"
    assert await env.published("order.payment_requested") == []


async def test_a_late_reservation_for_a_cancelled_order_is_ignored(env):
    order_id = await env.checkout()
    await env.send("inventory.failed", {"order_id": order_id, "reason": "out of stock"})
    await env.reserved(order_id)
    assert await env.status(order_id) == "cancelled"


async def test_payment_after_cancellation_requests_a_refund(env):
    order_id = await env.reserved(await env.checkout())
    await env.send("inventory.released", {"order_id": order_id, "reason": "expired"})  # cancelled
    await env.paid(order_id)  # the customer paid just too late

    assert await env.status(order_id) == "cancelled"
    (event,) = await env.published("order.refund_requested")
    assert event["order_id"] == order_id and event["total_cents"] == 6500


async def test_payment_after_a_user_cancel_requests_a_refund(env):
    order_id = await env.reserved(await env.checkout(user=ADA))
    await env.client.post(f"/api/v1/orders/{order_id}/cancel", headers=as_user(ADA))
    await env.paid(order_id)
    assert len(await env.published("order.refund_requested")) == 1


async def test_a_failure_after_confirmation_cannot_cancel_a_paid_order(env):
    order_id = await env.confirmed_order()
    await env.send("payment.failed", {"order_id": order_id, "reason": "late failure"})
    assert await env.status(order_id) == "confirmed"


# ---- the sweeper ----------------------------------------------------------------------
async def age(env, order_id: str, minutes: int) -> None:
    async with env.session_factory() as s:
        await s.execute(
            update(Order)
            .where(Order.id == uuid.UUID(order_id))
            .values(updated_at=datetime.now(UTC) - timedelta(minutes=minutes))
        )
        await s.commit()


async def test_orders_that_never_get_an_answer_are_cancelled(env):
    stuck_pending = await env.checkout()
    stuck_payment = await env.reserved(await env.checkout())
    fresh = await env.checkout()
    paid = await env.confirmed_order()
    for order_id in (stuck_pending, stuck_payment, paid):
        await age(env, order_id, 24 * 60)

    assert await env.sweeper.run_once() == 2
    assert await env.sweeper.run_once() == 0
    assert await env.status(stuck_pending) == "cancelled"
    assert await env.status(stuck_payment) == "cancelled"
    assert await env.status(fresh) == "pending"  # not old enough
    assert await env.status(paid) == "confirmed"  # paid orders are never swept
    assert len(await env.published("order.cancelled")) == 2


async def test_history_records_who_and_why(env):
    order_id = await env.checkout(user=ADA)
    await env.client.post(f"/api/v1/orders/{order_id}/cancel", headers=as_user(ADA))
    async with env.session_factory() as s:
        rows = list(await s.scalars(select(StatusChange).order_by(StatusChange.seq)))
    assert [(r.to_status, r.reason, r.actor) for r in rows] == [
        ("pending", "order placed", ADA),
        ("cancelled", "cancelled by customer", ADA),
    ]
