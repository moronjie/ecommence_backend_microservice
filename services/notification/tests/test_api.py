import uuid

from conftest import ADA, ADMIN, BOB, as_user, uniform


async def mine(env, user=ADA) -> dict:
    await env.confirmed(user=user)
    return (await env.notes())[-1]


async def test_a_customer_sees_their_own_emails_without_delivery_internals(env):
    await mine(env, ADA)
    await mine(env, BOB)
    page = uniform(await env.client.get("/api/v1/notifications", headers=as_user(ADA)), 200, True)[
        "data"
    ]
    assert page["total"] == 1
    (item,) = page["items"]
    assert item["kind"] == "order_confirmed" and item["to_email"] == "ada@example.com"
    assert "attempts" not in item and "last_error" not in item  # for administrators only


async def test_detail_shows_the_message_but_not_internals_to_the_customer(env):
    await mine(env, ADA)
    note = (await env.notes())[0]
    data = uniform(
        await env.client.get(f"/api/v1/notifications/{note.id}", headers=as_user(ADA)), 200, True
    )["data"]
    assert "Kente Scarf" in data["text_body"] and "<" in data["html_body"]
    assert not {"user_id", "attempts", "last_error", "next_attempt_at"} & set(data)


async def test_other_customers_cannot_read_it(env):
    await mine(env, ADA)
    note = (await env.notes())[0]
    other = uniform(
        await env.client.get(f"/api/v1/notifications/{note.id}", headers=as_user(BOB)), 404, False
    )
    missing = uniform(
        await env.client.get(f"/api/v1/notifications/{uuid.uuid4()}", headers=as_user(BOB)),
        404,
        False,
    )
    assert other["error_message"] == missing["error_message"]


async def test_admins_see_everything_with_delivery_details_and_can_filter(env):
    await mine(env, ADA)
    await env.send("order.confirmed", env.order_payload(user=BOB, email=None))  # will be failed
    everything = uniform(await env.client.get("/api/v1/notifications", headers=ADMIN), 200, True)[
        "data"
    ]
    assert everything["total"] == 2 and "attempts" in everything["items"][0]
    failed = uniform(
        await env.client.get("/api/v1/notifications?status=failed", headers=ADMIN), 200, True
    )["data"]
    assert failed["total"] == 1 and "no email" in failed["items"][0]["last_error"]
    bobs = uniform(
        await env.client.get(f"/api/v1/notifications?owner={BOB}", headers=ADMIN), 200, True
    )["data"]
    assert bobs["total"] == 1
    kind = uniform(
        await env.client.get("/api/v1/notifications?kind=welcome", headers=ADMIN), 200, True
    )["data"]
    assert kind["total"] == 0


async def test_customers_cannot_browse_other_customers(env):
    uniform(
        await env.client.get(f"/api/v1/notifications?owner={BOB}", headers=as_user(ADA)), 403, False
    )
    uniform(
        await env.client.get(f"/api/v1/notifications?owner={ADA}", headers=as_user(ADA)), 200, True
    )


async def test_paging_and_bad_parameters(env):
    for _ in range(3):
        await mine(env, ADA)
    page = uniform(
        await env.client.get("/api/v1/notifications?size=2", headers=as_user(ADA)), 200, True
    )["data"]
    assert page["total"] == 3 and len(page["items"]) == 2
    for query in ("size=1000", "page=0", "status=bogus", "kind=bogus", "owner=nope"):
        uniform(await env.client.get(f"/api/v1/notifications?{query}", headers=ADMIN), 422, False)


async def test_identity_is_required(env):
    uniform(await env.client.get("/api/v1/notifications"), 401, False)


# ---- retrying a failed email ----------------------------------------------------------
async def test_an_admin_can_retry_a_failed_email(env):
    await env.confirmed()
    env.sender.refuse_with = "mailbox full"
    await env.worker.run_once()
    note = (await env.notes())[0]
    assert note.status == "failed"

    env.sender.refuse_with = None  # the problem was fixed
    resp = await env.client.post(f"/api/v1/notifications/{note.id}/retry", headers=ADMIN)
    assert uniform(resp, 200, True)["data"]["status"] == "queued"
    await env.worker.run_once()
    assert (await env.notes())[0].status == "sent" and len(env.sender.sent) == 1


async def test_retry_is_admin_only_and_only_for_failed_emails(env):
    await env.confirmed()
    note = (await env.notes())[0]
    url = f"/api/v1/notifications/{note.id}/retry"
    uniform(await env.client.post(url, headers=as_user(ADA)), 403, False)
    uniform(await env.client.post(url, headers=ADMIN), 409, False)  # still queued, not failed
    uniform(
        await env.client.post(f"/api/v1/notifications/{uuid.uuid4()}/retry", headers=ADMIN),
        404,
        False,
    )


async def test_health_and_ready(env):
    uniform(await env.client.get("/health"), 200, True)
    uniform(await env.client.get("/ready"), 200, True)
