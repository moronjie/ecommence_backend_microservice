import uuid

from app.core.config import Settings
from app.main import create_app
from conftest import ADA, ADMIN, BOB, as_user, uniform


async def test_the_customer_finds_the_payment_link_for_their_order(env):
    order_id, reference = await env.started(user=ADA)
    data = uniform(
        await env.client.get(f"/api/v1/payments/order/{order_id}", headers=as_user(ADA)), 200, True
    )["data"]
    assert data["status"] == "pending" and data["amount_cents"] == 6500
    assert data["authorization_url"].endswith(reference)  # send the customer here to pay


async def test_the_link_disappears_once_the_payment_is_over(env):
    order_id, reference = await env.started(user=ADA)
    await env.customer_pays(reference)
    data = uniform(
        await env.client.get(f"/api/v1/payments/order/{order_id}", headers=as_user(ADA)), 200, True
    )["data"]
    assert data["status"] == "succeeded" and data["authorization_url"] is None and data["paid_at"]


async def test_before_the_worker_runs_the_payment_exists_without_a_link(env):
    order_id = await env.request(user=ADA)
    data = uniform(
        await env.client.get(f"/api/v1/payments/order/{order_id}", headers=as_user(ADA)), 200, True
    )["data"]
    assert data["status"] == "created" and data["authorization_url"] is None  # try again shortly


async def test_other_customers_cannot_see_it_but_admins_can(env):
    order_id, _ = await env.started(user=ADA)
    url = f"/api/v1/payments/order/{order_id}"
    other = uniform(await env.client.get(url, headers=as_user(BOB)), 404, False)
    missing = uniform(
        await env.client.get(f"/api/v1/payments/order/{uuid.uuid4()}", headers=as_user(BOB)),
        404,
        False,
    )
    assert other["error_message"] == missing["error_message"]
    uniform(await env.client.get(url, headers=ADMIN), 200, True)


async def test_identity_is_required_for_everything_but_the_webhook(env):
    order_id, _ = await env.started()
    uniform(await env.client.get(f"/api/v1/payments/order/{order_id}"), 401, False)
    uniform(await env.client.get("/api/v1/payments"), 403, False)  # no admin role


async def test_admins_list_and_filter_payments(env):
    a, ref = await env.started(user=ADA)
    await env.customer_pays(ref)
    await env.started(user=BOB)
    page = uniform(await env.client.get("/api/v1/payments?size=1", headers=ADMIN), 200, True)[
        "data"
    ]
    assert page["total"] == 2 and len(page["items"]) == 1
    paid = uniform(
        await env.client.get("/api/v1/payments?status=succeeded", headers=ADMIN), 200, True
    )["data"]
    assert [p["order_id"] for p in paid["items"]] == [a]
    uniform(await env.client.get("/api/v1/payments", headers=as_user(ADA)), 403, False)
    for query in ("size=1000", "page=0", "status=bogus"):
        uniform(await env.client.get(f"/api/v1/payments?{query}", headers=ADMIN), 422, False)


async def test_the_development_simulator_plays_the_customer(env):
    order_id, reference = await env.started(user=ADA)
    resp = await env.client.post(f"/api/v1/payments/dev/{reference}/pay", headers=as_user(ADA))
    assert uniform(resp, 200, True)["data"]["known"] is True
    assert (await env.payment(order_id)).status == "succeeded"
    assert len(await env.published("payment.completed")) == 1


async def test_the_development_simulator_does_not_exist_next_to_real_money(env):
    live = create_app(
        Settings(provider="paystack", paystack_secret_key="sk_test_x"),
        session_factory=env.session_factory,
        provider=env.provider,
    )
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=live), base_url="http://p") as c:
        resp = await c.post("/api/v1/payments/dev/anything/pay", headers=as_user(ADA))
    assert resp.status_code == 404


async def test_health_and_ready(env):
    uniform(await env.client.get("/health"), 200, True)
    uniform(await env.client.get("/ready"), 200, True)
