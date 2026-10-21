import uuid
from datetime import UTC, datetime, timedelta

from app.models.payment import Payment
from conftest import ADA
from sqlalchemy import func, select, update


async def count(env) -> int:
    async with env.session_factory() as s:
        return await s.scalar(select(func.count()).select_from(Payment))


# ---- a payment is requested -----------------------------------------------------------
async def test_request_creates_a_payment_and_nothing_goes_to_the_provider_yet(env):
    order_id = await env.request(total=6500)
    payment = await env.payment(order_id)
    assert (payment.status, payment.amount_cents, payment.currency) == ("created", 6500, "GHS")
    assert str(payment.user_id) == ADA and payment.email == "ada@example.com"
    assert env.provider.initialized == []  # the worker does that, outside the event handler


async def test_the_same_request_never_makes_two_payments(env):
    order_id = str(uuid.uuid4())
    await env.request(order_id=order_id, event_id="evt-1")
    await env.request(order_id=order_id, event_id="evt-1")  # Kafka redelivery
    await env.request(order_id=order_id)  # a new event for the same order
    assert await count(env) == 1


async def test_bad_requests_go_to_the_dlq(env):
    good = {"order_id": str(uuid.uuid4()), "user_id": ADA, "total_cents": 100, "currency": "GHS"}
    for bad in (
        {**good, "order_id": "nope"},
        {**good, "total_cents": -1},
        {**good, "currency": "CEDIS"},
        {"order_id": good["order_id"]},
    ):
        await env.send("order.payment_requested", bad)
    assert len(env.dlq.events) == 4 and await count(env) == 0


async def test_requests_that_can_never_succeed_fail_straight_away(env):
    cases = {
        "usd": {"currency": "USD"},  # only GHS is accepted
        "no email": {"email": None},
        "free": {"total": 0},
    }
    for name, kw in cases.items():
        order_id = await env.request(**kw)
        assert (await env.payment(order_id)).status == "failed", name
    failures = await env.published("payment.failed")
    assert len(failures) == 3 and "not accepted" in failures[0]["reason"]
    assert env.provider.initialized == []  # the provider was never bothered


# ---- starting the payment -------------------------------------------------------------
async def test_the_worker_asks_the_provider_for_a_payment_link(env):
    order_id, reference = await env.started(total=6500)
    payment = await env.payment(order_id)
    assert payment.status == "pending" and payment.authorization_url.endswith(reference)
    (sent,) = env.provider.initialized
    assert sent["reference"] == f"{uuid.UUID(order_id).hex}-1"
    assert (sent["amount_cents"], sent["currency"], sent["email"]) == (
        6500,
        "GHS",
        "ada@example.com",
    )
    assert sent["metadata"]["order_id"] == order_id
    assert "mobile_money" in sent["channels"]  # how most customers in Ghana pay


async def test_starting_twice_does_nothing_new(env):
    order_id, _ = await env.started()
    assert await env.workers.init_once() == 0
    assert len(env.provider.initialized) == 1 and (await env.payment(order_id)).attempts == 1


async def test_a_provider_outage_is_retried_with_a_fresh_reference_each_time(env):
    env.provider.transient_failures = 2
    order_id = await env.request()
    for _ in range(3):
        await env.workers.init_once()
    payment = await env.payment(order_id)
    assert payment.status == "pending" and payment.attempts == 3
    assert payment.reference.endswith("-3")  # never reuses a reference the provider may have seen
    assert await env.published("payment.failed") == []


async def test_giving_up_after_the_last_attempt_fails_the_payment(env):
    env.provider.transient_failures = 99
    order_id = await env.request()
    for _ in range(5):
        await env.workers.init_once()
    payment = await env.payment(order_id)
    assert (payment.status, payment.attempts) == ("failed", env.settings.max_attempts)
    (event,) = await env.published("payment.failed")
    assert event["order_id"] == order_id and "could not reach" in event["reason"]


async def test_a_refusal_by_the_provider_fails_the_payment_at_once(env):
    env.provider.reject_with = "Invalid currency"
    order_id = await env.request()
    await env.workers.init_once()
    payment = await env.payment(order_id)
    assert payment.status == "failed" and payment.attempts == 1
    (event,) = await env.published("payment.failed")
    assert "Invalid currency" in event["reason"]


# ---- finding out the result (without a webhook) ---------------------------------------
async def test_a_missed_webhook_is_caught_by_the_reconcile_worker(env):
    order_id, reference = await env.started()
    env.provider.simulate(reference, "success", channel="card")  # paid, but no webhook arrived
    assert await env.workers.reconcile_once() == 1

    payment = await env.payment(order_id)
    assert (payment.status, payment.channel) == ("succeeded", "card") and payment.paid_at
    (event,) = await env.published("payment.completed")
    assert event["order_id"] == order_id and event["amount_cents"] == 6500
    assert event["currency"] == "GHS" and event["channel"] == "card"


async def test_a_declined_payment_is_reported(env):
    order_id, reference = await env.started()
    env.provider.simulate(reference, "failed")
    await env.workers.reconcile_once()
    assert (await env.payment(order_id)).status == "failed"
    (event,) = await env.published("payment.failed")
    assert event["reason"] == "Declined"


async def test_each_pending_payment_is_not_checked_every_few_seconds(env):
    await env.started()
    assert await env.workers.reconcile_once() == 1
    assert await env.workers.reconcile_once() == 0  # checked a moment ago


async def age(env, order_id: str, minutes: int) -> None:
    async with env.session_factory() as s:
        await s.execute(
            update(Payment)
            .where(Payment.order_id == uuid.UUID(order_id))
            .values(
                created_at=datetime.now(UTC) - timedelta(minutes=minutes),
                last_checked_at=None,
            )
        )
        await s.commit()


async def test_a_payment_the_customer_never_finishes_expires(env):
    order_id, reference = await env.started()
    env.provider.simulate(reference, "abandoned")
    await age(env, order_id, 60)
    await env.workers.reconcile_once()

    assert (await env.payment(order_id)).status == "failed"
    (event,) = await env.published("payment.failed")
    assert "not completed in time" in event["reason"]  # the order service will cancel the order


async def test_a_fresh_pending_payment_is_left_alone(env):
    order_id, reference = await env.started()
    env.provider.simulate(reference, "abandoned")
    await env.workers.reconcile_once()
    assert (await env.payment(order_id)).status == "pending"


async def test_money_arriving_after_expiry_is_still_recorded(env):
    order_id, reference = await env.started()
    await age(env, order_id, 60)
    await env.workers.reconcile_once()  # expired: failed
    env.provider.simulate(reference, "success")
    await env.workers.refund_once()
    await env.customer_pays(reference)

    assert (await env.payment(order_id)).status == "succeeded"
    # the order service sees payment.completed for a cancelled order and asks for the refund
    assert len(await env.published("payment.completed")) == 1


async def test_an_amount_that_does_not_match_is_refused_and_refunded(env):
    order_id, reference = await env.started(total=6500)
    env.provider.simulate(reference, "success", amount_cents=100)  # paid far too little
    await env.workers.reconcile_once()

    payment = await env.payment(order_id)
    assert payment.status == "failed" and "different amount" in payment.failure_reason
    assert await env.published("payment.completed") == []
    await env.workers.refund_once()
    assert env.provider.refunds == [{"reference": reference, "amount_cents": 100}]  # what came in


async def test_wrong_currency_is_treated_like_a_wrong_amount(env):
    order_id, reference = await env.started()
    env.provider.simulate(reference, "success", currency="USD")
    await env.workers.reconcile_once()
    assert (await env.payment(order_id)).status == "failed"
    assert await env.published("payment.completed") == []
