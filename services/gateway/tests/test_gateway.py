import json
from datetime import timedelta

import httpx
import pytest
from app.core.config import Settings
from app.main import create_app
from ecom_common.security import create_token
from fakeredis import FakeAsyncRedis

SECRET = "test-secret-that-is-long-enough-for-hs256"
KEYS = {"success", "message", "data", "error_message"}


class Upstream:
    """Stands in for the downstream services and records what the gateway sent."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.behaviour = self.ok

    def ok(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": True, "message": "from upstream", "data": {}, "error_message": ""},
            headers={"X-Upstream": "yes"},
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.behaviour(request)


def make_settings(**overrides) -> Settings:
    return Settings(**{"jwt_secret": SECRET, "rate_limit_requests": 100, **overrides})


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


def make_client(upstream: Upstream, **overrides) -> httpx.AsyncClient:
    app = create_app(
        make_settings(**overrides),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        redis_client=FakeAsyncRedis(decode_responses=True),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


@pytest.fixture
async def client(upstream):
    async with make_client(upstream) as c:
        yield c


def bearer(sub="user-1", role="customer", **kw) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_token(sub, secret=SECRET, role=role, **kw)}"}


def assert_uniform(resp: httpx.Response, status: int, success: bool):
    assert resp.status_code == status
    body = resp.json()
    assert set(body) == KEYS and body["success"] is success
    return body


async def test_public_route_is_proxied_and_response_passes_through(client, upstream):
    resp = await client.get("/api/v1/template/ping?x=1")
    body = assert_uniform(resp, 200, True)
    assert body["message"] == "from upstream"
    assert resp.headers["x-upstream"] == "yes"
    sent = upstream.requests[0]
    assert sent.url.path == "/api/v1/template/ping" and sent.url.params["x"] == "1"


async def test_unknown_route_is_404_uniform(client):
    assert_uniform(await client.get("/api/v1/nothing"), 404, False)


async def test_protected_route_without_token_is_401(client, upstream):
    body = assert_uniform(await client.get("/api/v1/users/me"), 401, False)
    assert "bearer" in body["error_message"].lower()
    assert upstream.requests == []


async def test_invalid_and_expired_tokens_are_401(client, upstream):
    bad = {"Authorization": "Bearer garbage"}
    assert_uniform(await client.get("/api/v1/users/me", headers=bad), 401, False)
    expired = bearer(expires_in=timedelta(seconds=-5))
    body = assert_uniform(await client.get("/api/v1/users/me", headers=expired), 401, False)
    assert "expired" in body["error_message"]
    assert upstream.requests == []


async def test_valid_token_forwards_identity(client, upstream):
    await client.get("/api/v1/users/me", headers=bearer("abc", role="admin"))
    sent = upstream.requests[0]
    assert sent.headers["x-user-id"] == "abc"
    assert sent.headers["x-user-role"] == "admin"


async def test_client_cannot_spoof_identity_headers(client, upstream):
    spoof = {"X-User-Id": "someone-else", "X-User-Role": "admin"}
    await client.get("/api/v1/template/ping", headers=spoof)  # anonymous
    sent = upstream.requests[0]
    assert "x-user-id" not in sent.headers and "x-user-role" not in sent.headers

    await client.get("/api/v1/users/me", headers={**bearer("real", role="customer"), **spoof})
    sent = upstream.requests[1]
    assert sent.headers["x-user-id"] == "real" and sent.headers["x-user-role"] == "customer"


async def test_auth_login_public_but_logout_protected(client):
    assert (await client.post("/api/v1/auth/login", json={})).status_code == 200
    assert_uniform(await client.post("/api/v1/auth/logout"), 401, False)


async def test_catalogue_reads_public_writes_protected(client):
    assert (await client.get("/api/v1/products")).status_code == 200
    assert_uniform(await client.post("/api/v1/products", json={}), 401, False)
    assert (await client.post("/api/v1/products", json={}, headers=bearer())).status_code == 200


async def test_request_body_and_method_forwarded(client, upstream):
    await client.post("/api/v1/orders", json={"a": 1}, headers=bearer())
    sent = upstream.requests[0]
    assert sent.method == "POST" and json.loads(sent.content) == {"a": 1}


async def test_request_id_generated_and_forwarded(client, upstream):
    resp = await client.get("/api/v1/template/ping", headers={"X-Request-ID": "req-42"})
    assert resp.headers["x-request-id"] == "req-42"
    assert upstream.requests[0].headers["x-request-id"] == "req-42"


async def test_rate_limit_returns_429(upstream):
    async with make_client(upstream, rate_limit_requests=3) as c:
        statuses = [(await c.get("/api/v1/template/ping")).status_code for _ in range(5)]
        assert statuses == [200, 200, 200, 429, 429]
        resp = await c.get("/api/v1/template/ping")
        body = assert_uniform(resp, 429, False)
        assert int(resp.headers["retry-after"]) >= 1
        assert "3 requests" in body["error_message"]


async def test_rate_limit_is_per_user(upstream):
    async with make_client(upstream, rate_limit_requests=1) as c:
        assert (await c.get("/api/v1/orders", headers=bearer("a"))).status_code == 200
        assert (await c.get("/api/v1/orders", headers=bearer("a"))).status_code == 429
        assert (await c.get("/api/v1/orders", headers=bearer("b"))).status_code == 200


async def test_rate_limit_headers_on_success(client):
    resp = await client.get("/api/v1/template/ping")
    assert resp.headers["x-ratelimit-limit"] == "100"
    assert resp.headers["x-ratelimit-remaining"] == "99"


async def test_downstream_errors_pass_through_unchanged(client, upstream):
    upstream.behaviour = lambda r: httpx.Response(
        404, json={"success": False, "message": "nope", "data": {}, "error_message": "gone"}
    )
    body = assert_uniform(await client.get("/api/v1/template/x"), 404, False)
    assert body["error_message"] == "gone"


async def test_upstream_down_is_503_uniform(client, upstream):
    def refuse(request):
        raise httpx.ConnectError("refused")

    upstream.behaviour = refuse
    assert_uniform(await client.post("/api/v1/template/x"), 503, False)


async def test_upstream_timeout_is_504_uniform(client, upstream):
    def slow(request):
        raise httpx.ReadTimeout("slow")

    upstream.behaviour = slow
    assert_uniform(await client.get("/api/v1/template/x"), 504, False)


async def test_get_retried_once_on_connect_error_post_is_not(client, upstream):
    def refuse(request):
        raise httpx.ConnectError("refused")

    upstream.behaviour = refuse
    await client.get("/api/v1/template/x")
    assert len(upstream.requests) == 2
    upstream.requests.clear()
    await client.post("/api/v1/template/x")
    assert len(upstream.requests) == 1


async def test_circuit_opens_then_fails_fast(upstream):
    def boom(request):
        return httpx.Response(500, json={})

    upstream.behaviour = boom
    async with make_client(upstream, circuit_failure_threshold=3) as c:
        for _ in range(3):
            await c.get("/api/v1/template/x")
        calls = len(upstream.requests)
        resp = await c.get("/api/v1/template/x")
        assert_uniform(resp, 503, False)
        assert len(upstream.requests) == calls  # no call reached the service


async def test_services_health_aggregates(client, upstream):
    assert_uniform(await client.get("/health/services"), 200, True)

    def down(request):
        raise httpx.ConnectError("refused")

    upstream.behaviour = down
    body = assert_uniform(await client.get("/health/services"), 503, False)
    assert set(body["data"].values()) == {"down"}


async def test_gateway_own_health_and_ready(client):
    assert_uniform(await client.get("/health"), 200, True)
    assert_uniform(await client.get("/ready"), 200, True)


async def test_cors_preflight(client):
    resp = await client.options(
        "/api/v1/products",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"


async def test_the_payment_webhook_is_public_but_only_for_post_and_only_that_path(client, upstream):
    # Paystack carries no JWT; the payment service checks the signature instead
    resp = await client.post(
        "/api/v1/payments/webhooks/paystack",
        content=b'{"event":"charge.success"}',
        headers={"x-paystack-signature": "abc"},
    )
    assert resp.status_code == 200
    sent = upstream.requests[0]
    assert (
        sent.content == b'{"event":"charge.success"}'
    )  # forwarded byte for byte, or the signature breaks
    assert sent.headers["x-paystack-signature"] == "abc"

    assert_uniform(
        await client.get("/api/v1/payments/webhooks/paystack"), 401, False
    )  # GET is not exempt
    assert_uniform(
        await client.get("/api/v1/payments/order/123"), 401, False
    )  # the rest needs a login
    assert_uniform(await client.get("/api/v1/payments"), 401, False)
    assert (await client.get("/api/v1/payments", headers=bearer())).status_code == 200
