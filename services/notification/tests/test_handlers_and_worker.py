import uuid

from conftest import ADA


# ---- events become queued emails ------------------------------------------------------
async def test_each_kind_of_event_queues_one_email_to_the_right_person(env):
    order_id, payment_id, user = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    await env.send(
        "user.registered", {"user_id": user, "email": "new@example.com", "full_name": "Kofi"}
    )
    for topic in ("order.confirmed", "order.cancelled", "order.shipped", "order.delivered"):
        await env.send(
            topic, env.order_payload(order_id=order_id, user=user, email="kofi@example.com")
        )
    await env.send(
        "payment.refunded",
        {
            "order_id": order_id,
            "payment_id": payment_id,
            "user_id": user,
            "email": "kofi@example.com",
            "amount_cents": 500,
            "currency": "GHS",
        },
    )

    notes = await env.notes()
    assert sorted(n.kind for n in notes) == [
        "order_cancelled",
        "order_confirmed",
        "order_delivered",
        "order_shipped",
        "refund",
        "welcome",
    ]
    assert {str(n.user_id) for n in notes} == {user}
    assert {n.to_email for n in notes if n.kind != "welcome"} == {"kofi@example.com"}
    assert next(n for n in notes if n.kind == "welcome").to_email == "new@example.com"
    assert all(n.status == "queued" and n.subject for n in notes)


async def test_a_failed_payment_gets_no_email_of_its_own(env):
    # order.cancelled already explains it; two emails for one problem would be noise
    await env.send("payment.failed", {"order_id": str(uuid.uuid4()), "reason": "Declined"})
    assert await env.notes() == [] and env.dlq.events == []


async def test_a_redelivered_event_sends_one_email(env):
    payload = env.order_payload()
    await env.send("order.confirmed", payload, event_id="evt-1")
    await env.send("order.confirmed", payload, event_id="evt-1")
    assert len(await env.notes()) == 1


async def test_the_same_milestone_under_a_new_event_id_is_still_one_email(env):
    payload = env.order_payload()
    await env.send("order.confirmed", payload)
    await env.send("order.confirmed", payload)  # re-issued by the order service
    assert len(await env.notes()) == 1


async def test_different_orders_and_milestones_each_get_their_own_email(env):
    a, b = env.order_payload(), env.order_payload()
    await env.send("order.confirmed", a)
    await env.send("order.confirmed", b)
    await env.send("order.shipped", a)
    assert len(await env.notes()) == 3


async def test_an_event_with_no_email_is_recorded_as_failed_not_lost(env):
    await env.send("order.confirmed", env.order_payload(email=None))
    note = await env.only_note()
    assert note.status == "failed" and "no email" in note.last_error
    assert await env.worker.run_once() == 0  # nothing to send, nothing to retry forever


async def test_an_invalid_email_is_failed_too(env):
    for bad in (
        "not-an-email",
        "a@b",
        "two@example.com, other@example.com",
        "x@y.com\r\nBcc: z@evil.test",
    ):
        await env.send("order.confirmed", env.order_payload(email=bad))
    notes = await env.notes()
    assert len(notes) == 4 and {n.status for n in notes} == {"failed"}


async def test_malformed_events_go_to_the_dlq(env):
    good = env.order_payload()
    for bad in ({**good, "order_id": "nope"}, {**good, "user_id": None}, {"email": "a@b.co"}):
        await env.send("order.confirmed", bad)
    assert len(env.dlq.events) == 3 and await env.notes() == []


# ---- sending --------------------------------------------------------------------------
async def test_the_worker_sends_the_email_and_records_it(env):
    await env.confirmed(user=ADA)
    assert await env.worker.run_once() == 1

    (email,) = env.sender.sent
    assert email.to == "ada@example.com" and email.from_address == env.settings.from_address
    assert (
        email.subject.endswith("confirmed")
        and "Kente Scarf" in email.text
        and "Kente Scarf" in email.html
    )
    note = await env.only_note()
    assert (
        note.status == "sent" and note.sent_at and note.attempts == 1 and note.provider_message_id
    )
    assert await env.worker.run_once() == 0  # already done


async def test_a_mail_server_outage_is_retried_later_with_growing_waits(env):
    await env.confirmed()
    env.sender.transient_failures = 2

    await env.worker.run_once()  # fails
    note = await env.only_note()
    assert (note.status, note.attempts) == ("queued", 1) and "outage" in note.last_error
    first_wait = note.next_attempt_at
    assert await env.worker.run_once() == 0  # not due yet: it is not hammered every few seconds

    await env.make_due()
    await env.worker.run_once()  # fails again
    note = await env.only_note()
    assert note.attempts == 2 and note.next_attempt_at > first_wait.replace(
        tzinfo=note.next_attempt_at.tzinfo
    )

    await env.make_due()
    await env.worker.run_once()  # works
    note = await env.only_note()
    assert (note.status, note.attempts, note.last_error) == ("sent", 3, "")
    assert len(env.sender.sent) == 1  # one email in the end


async def test_it_gives_up_after_the_last_attempt_but_keeps_the_record(env):
    await env.confirmed()
    env.sender.transient_failures = 99
    for _ in range(env.settings.max_attempts):
        await env.make_due()
        await env.worker.run_once()
    note = await env.only_note()
    assert note.status == "failed" and note.attempts == env.settings.max_attempts
    assert "gave up" in note.last_error and env.sender.sent == []
    assert await env.worker.run_once() == 0


async def test_a_refused_address_fails_at_once_without_retrying(env):
    await env.confirmed()
    env.sender.refuse_with = "mailbox does not exist"
    await env.worker.run_once()
    note = await env.only_note()
    assert note.status == "failed" and note.attempts == 1 and "mailbox" in note.last_error


async def test_one_failing_email_does_not_block_the_others(env):
    await env.confirmed()
    await env.confirmed()
    env.sender.transient_failures = 1  # only the first send fails
    await env.worker.run_once()
    assert len(env.sender.sent) == 1
    notes = await env.notes()
    assert sorted(n.status for n in notes) == ["queued", "sent"]


async def test_a_claimed_email_is_not_picked_up_again_by_another_pass(env):
    await env.confirmed()
    async with env.session_factory() as s:
        from app.services.notification_service import NotificationService

        service = NotificationService(s, env.settings)
        (nid,) = await service.due()
        assert await service.begin_send(nid) is not None
        await s.commit()
    assert await env.worker.run_once() == 0  # another worker is already sending it
