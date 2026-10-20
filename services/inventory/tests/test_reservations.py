import uuid
from datetime import UTC, datetime, timedelta

from app.models.inventory import Reservation, StockMovement
from conftest import ADMIN
from sqlalchemy import select, update


# ---- reserving ------------------------------------------------------------------------
async def test_reserve_holds_stock_and_publishes_event(env):
    pid = await env.product(on_hand=10)
    order_id = await env.order([(pid, 3)])

    assert await env.stock(pid) == (10, 3)  # still on the shelf, but promised
    res = await env.reservation(order_id)
    assert res.status == "reserved" and res.expires_at is not None
    (event,) = await env.published("inventory.reserved")
    assert event["order_id"] == order_id and event["items"] == [{"product_id": pid, "quantity": 3}]
    assert event["expires_at"]
    assert await env.published("inventory.failed") == []


async def test_order_is_all_or_nothing(env):
    plenty, scarce = await env.product(5, "A"), await env.product(1, "B")
    order_id = await env.order([(plenty, 2), (scarce, 2)])

    assert await env.stock(plenty) == (5, 0)  # the line that WAS available is not held
    assert await env.stock(scarce) == (1, 0)
    assert (await env.reservation(order_id)).status == "failed"
    (event,) = await env.published("inventory.failed")
    assert event["shortages"] == [{"product_id": scarce, "requested": 2, "available": 1}]
    assert await env.published("inventory.reserved") == []


async def test_lines_for_the_same_product_are_merged(env):
    pid = await env.product(10)
    await env.order([(pid, 2), (pid, 3)])
    assert await env.stock(pid) == (10, 5)


async def test_unknown_product_fails_the_order(env):
    ghost = str(uuid.uuid4())
    await env.order([(ghost, 1)])
    (event,) = await env.published("inventory.failed")
    assert event["shortages"][0]["available"] == 0


async def test_cannot_oversell_across_orders(env):
    pid = await env.product(5)
    first = await env.order([(pid, 3)])
    second = await env.order([(pid, 3)])  # only 2 left
    third = await env.order([(pid, 2)])  # exactly what is left

    statuses = [(await env.reservation(o)).status for o in (first, second, third)]
    assert statuses == ["reserved", "failed", "reserved"]
    assert await env.stock(pid) == (5, 5)  # nothing left to promise


# ---- idempotency and ordering ---------------------------------------------------------
async def test_redelivered_event_reserves_once(env):
    pid = await env.product(10)
    order_id = str(uuid.uuid4())
    await env.order([(pid, 3)], order_id=order_id, event_id="evt-1")
    await env.order([(pid, 3)], order_id=order_id, event_id="evt-1")  # Kafka redelivery
    assert await env.stock(pid) == (10, 3)


async def test_same_order_with_new_event_id_does_not_double_reserve(env):
    pid = await env.product(10)
    order_id = str(uuid.uuid4())
    await env.order([(pid, 3)], order_id=order_id)
    await env.order([(pid, 3)], order_id=order_id)
    assert await env.stock(pid) == (10, 3)
    assert len(await env.published("inventory.reserved")) == 1


async def test_malformed_orders_go_to_dlq_and_change_nothing(env):
    pid = await env.product(10)
    bad_payloads = [
        {"order_id": str(uuid.uuid4()), "items": []},
        {"order_id": str(uuid.uuid4()), "items": [{"product_id": pid, "quantity": 0}]},
        {"order_id": str(uuid.uuid4()), "items": [{"product_id": pid, "quantity": -2}]},
        {"order_id": "not-a-uuid", "items": [{"product_id": pid, "quantity": 1}]},
    ]
    for payload in bad_payloads:
        await env.send("order.created", payload)
    assert len(env.dlq.events) == len(bad_payloads)
    assert await env.stock(pid) == (10, 0)


# ---- confirm --------------------------------------------------------------------------
async def test_confirm_removes_stock_permanently(env):
    pid = await env.product(10)
    order_id = await env.order([(pid, 3)])
    await env.send("order.confirmed", {"order_id": order_id})

    assert await env.stock(pid) == (7, 0)
    assert (await env.reservation(order_id)).status == "confirmed"
    await env.send("order.confirmed", {"order_id": order_id})  # again, new event id
    assert await env.stock(pid) == (7, 0)  # not deducted twice


async def test_confirming_an_unknown_order_is_dead_lettered(env):
    await env.send("order.confirmed", {"order_id": str(uuid.uuid4())})
    assert env.dlq.events[0][0] == "order.confirmed.dlq"


# ---- release --------------------------------------------------------------------------
async def test_cancel_returns_stock_once(env):
    pid = await env.product(10)
    order_id = await env.order([(pid, 4)])
    await env.send("order.cancelled", {"order_id": order_id})

    assert await env.stock(pid) == (10, 0)
    assert (await env.reservation(order_id)).status == "released"
    (event,) = await env.published("inventory.released")
    assert event == {"order_id": order_id, "reason": "cancelled"}

    await env.send("order.cancelled", {"order_id": order_id})  # again
    assert await env.stock(pid) == (10, 0) and len(await env.published("inventory.released")) == 1


async def test_cancelling_a_confirmed_order_does_not_touch_stock(env):
    pid = await env.product(10)
    order_id = await env.order([(pid, 4)])
    await env.send("order.confirmed", {"order_id": order_id})
    await env.send("order.cancelled", {"order_id": order_id})
    assert await env.stock(pid) == (6, 0)  # refunds/restocking are a separate flow


async def test_cancel_arriving_before_order_created_blocks_the_reservation(env):
    pid = await env.product(10)
    order_id = str(uuid.uuid4())
    await env.send("order.cancelled", {"order_id": order_id})  # overtook order.created
    await env.order([(pid, 4)], order_id=order_id)

    assert await env.stock(pid) == (10, 0)
    assert await env.published("inventory.reserved") == []


# ---- expiry ---------------------------------------------------------------------------
async def test_unpaid_reservations_expire_and_free_the_stock(env):
    pid = await env.product(10)
    stale, fresh = await env.order([(pid, 3)]), await env.order([(pid, 2)])
    async with env.session_factory() as s:  # make the first one overdue
        await s.execute(
            update(Reservation)
            .where(Reservation.order_id == uuid.UUID(stale))
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await s.commit()

    assert await env.expiry.run_once() == 1
    assert await env.expiry.run_once() == 0  # nothing more to do
    assert await env.stock(pid) == (10, 2)  # only the fresh order is still held
    assert (await env.reservation(stale)).status == "released"
    assert (await env.reservation(fresh)).status == "reserved"
    (event,) = await env.published("inventory.released")
    assert event == {"order_id": stale, "reason": "expired"}


async def _expire(env, order_id: str) -> None:
    async with env.session_factory() as s:
        await s.execute(
            update(Reservation)
            .where(Reservation.order_id == uuid.UUID(order_id))
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await s.commit()
    await env.expiry.run_once()


async def test_payment_after_expiry_retakes_the_stock_if_still_there(env):
    pid = await env.product(10)
    order_id = await env.order([(pid, 3)])
    await _expire(env, order_id)
    await env.send("order.confirmed", {"order_id": order_id})

    assert await env.stock(pid) == (7, 0)
    assert (await env.reservation(order_id)).status == "confirmed"


async def test_payment_after_expiry_reports_failure_if_stock_was_sold(env):
    pid = await env.product(3)
    order_id = await env.order([(pid, 3)])
    await _expire(env, order_id)
    other = await env.order([(pid, 3)])  # someone else takes it all
    await env.send("order.confirmed", {"order_id": order_id})

    assert (await env.reservation(order_id)).status == "released"
    failures = await env.published("inventory.failed")
    assert failures and failures[-1]["order_id"] == order_id
    assert (await env.reservation(other)).status == "reserved"
    assert await env.stock(pid) == (3, 3)  # the other order's hold is untouched


# ---- ledger ---------------------------------------------------------------------------
async def test_every_change_leaves_a_ledger_row(env):
    pid = await env.product(10)  # stocktake +10
    order_id = await env.order([(pid, 3)])  # reserve
    await env.send("order.confirmed", {"order_id": order_id})  # commit
    async with env.session_factory() as s:
        rows = (await s.scalars(select(StockMovement).order_by(StockMovement.created_at))).all()
    kinds = sorted(r.kind for r in rows)
    assert kinds == ["commit", "reserve", "stocktake"]
    assert sum(r.delta_on_hand for r in rows) == 10 - 3
    assert sum(r.delta_reserved for r in rows) == 0
    assert ADMIN  # (imported for the shared fixtures module)
