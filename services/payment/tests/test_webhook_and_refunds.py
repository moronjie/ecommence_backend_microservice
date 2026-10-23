import uuid

from app.models.payment import Refund
from conftest import uniform
from sqlalchemy import select


# ---- webhook --------------------------------------------------------------------------
async def test_a_signed_charge_success_confirms_the_payment(env):
    order_id, reference = await env.started()
    uniform(await env.customer_pays(reference, channel="mobile_money"), 200, True)

    payment = await env.payment(order_id)
    assert (payment.status, payment.channel) == ("succeeded", "mobile_money")
    (event,) = await env.published("payment.completed")
    assert event["order_id"] == order_id and event["reference"] == reference


async def test_the_webhook_needs_no_login_but_needs_a_valid_signature(env):
    order_id, reference = await env.started()
    env.provider.simulate(reference, "success")
    body = {"event": "charge.success", "data": {"reference": reference}}
    uniform(await env.webhook(body, signature=None), 401, False)  # none
    uniform(await env.webhook(body, signature="0" * 128), 401, False)  # wrong
    assert (await env.payment(order_id)).status == "pending"  # nothing happened
    assert await env.published("payment.completed") == []


async def test_a_tampered_body_is_rejected(env):
    _, reference = await env.started()
    raw = b'{"event":"charge.success","data":{"reference":"%s"}}' % reference.encode()
    from conftest import sign

    resp = await env.client.post(
        "/api/v1/payments/webhooks/paystack",
        content=raw.replace(b"charge", b"chargX"),  # changed after signing
        headers={"x-paystack-signature": sign(raw)},
    )
    uniform(resp, 401, False)


async def test_the_webhook_confirms_with_the_provider_instead_of_trusting_itself(env):
    order_id, reference = await env.started()
    # Claims success, but the provider says the customer has not paid
    uniform(
        await env.webhook(
            {"event": "charge.success", "data": {"reference": reference, "amount": 1}}
        ),
        200,
        True,
    )
    assert (await env.payment(order_id)).status == "pending"
    assert await env.published("payment.completed") == []


async def test_a_repeated_webhook_publishes_once(env):
    _, reference = await env.started()
    for _ in range(3):
        uniform(await env.customer_pays(reference), 200, True)
    assert len(await env.published("payment.completed")) == 1


async def test_unknown_references_and_other_events_are_acknowledged_and_ignored(env):
    uniform(
        await env.webhook({"event": "charge.success", "data": {"reference": "nope"}}), 200, True
    )
    uniform(await env.webhook({"event": "transfer.success", "data": {}}), 200, True)
    uniform(await env.webhook({"nonsense": True}), 200, True)


async def test_a_provider_outage_during_confirmation_asks_for_a_resend(env):
    _, reference = await env.started()
    env.provider.transient_failures = 1
    uniform(await env.customer_pays(reference), 503, False)  # Paystack will retry the webhook
    uniform(await env.customer_pays(reference), 200, True)


async def test_an_unreadable_body_is_a_422(env):
    from conftest import sign

    raw = b"this is not json"
    resp = await env.client.post(
        "/api/v1/payments/webhooks/paystack",
        content=raw,
        headers={"x-paystack-signature": sign(raw)},
    )
    uniform(resp, 422, False)


# ---- refunds --------------------------------------------------------------------------
async def paid_order(env, **kw):
    order_id, reference = await env.started(**kw)
    await env.customer_pays(reference)
    return order_id, reference


async def refund_row(env) -> Refund | None:
    async with env.session_factory() as s:
        return await s.scalar(select(Refund))


async def test_refund_requested_sends_the_full_amount_back(env):
    order_id, reference = await paid_order(env, total=6500)
    await env.send("order.refund_requested", {"order_id": order_id, "reason": "paid too late"})
    assert (await refund_row(env)).status == "created"

    assert await env.workers.refund_once() == 1
    assert env.provider.refunds == [{"reference": reference, "amount_cents": 6500}]
    assert (await refund_row(env)).status == "requested"
    assert (await env.payment(order_id)).status == "refunded"
    (event,) = await env.published("payment.refunded")
    assert event["order_id"] == order_id and event["amount_cents"] == 6500
    assert event["email"] == "ada@example.com" and event["user_id"]  # who to tell


async def test_refunding_twice_refunds_once(env):
    order_id, _ = await paid_order(env)
    for _ in range(3):
        await env.send("order.refund_requested", {"order_id": order_id})
    await env.workers.refund_once()
    await env.workers.refund_once()
    assert len(env.provider.refunds) == 1


async def test_the_refund_webhook_marks_it_processed(env):
    order_id, reference = await paid_order(env)
    await env.send("order.refund_requested", {"order_id": order_id})
    await env.workers.refund_once()
    uniform(
        await env.webhook(
            {"event": "refund.processed", "data": {"transaction_reference": reference}}
        ),
        200,
        True,
    )
    assert (await refund_row(env)).status == "processed"


async def test_a_refund_is_retried_through_an_outage(env):
    order_id, _ = await paid_order(env)
    await env.send("order.refund_requested", {"order_id": order_id})
    env.provider.transient_failures = 1
    await env.workers.refund_once()
    assert (await refund_row(env)).status == "created"  # still to do
    await env.workers.refund_once()
    assert (await refund_row(env)).status == "requested"


async def test_a_refund_the_provider_rejects_is_marked_failed_not_retried_forever(env):
    order_id, _ = await paid_order(env)
    await env.send("order.refund_requested", {"order_id": order_id})
    env.provider.refund_rejects = "Transaction already reversed"
    await env.workers.refund_once()
    await env.workers.refund_once()
    row = await refund_row(env)
    assert row.status == "failed" and "reversed" in row.last_error
    assert (await env.payment(order_id)).status == "succeeded"  # a human needs to look at it


async def test_nothing_to_refund_when_nothing_was_collected(env):
    order_id, _ = await env.started()  # still pending: no money taken
    await env.send("order.refund_requested", {"order_id": order_id})
    assert await refund_row(env) is None


async def test_refund_for_an_order_with_no_payment_goes_to_the_dlq(env):
    await env.send("order.refund_requested", {"order_id": str(uuid.uuid4())})
    assert env.dlq.events[0][0] == "order.refund_requested.dlq"
