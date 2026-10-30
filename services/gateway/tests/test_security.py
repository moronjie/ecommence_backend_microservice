import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest
from app.core.config import Settings
from app.main import create_app
from ecom_common.identity import identity_is_valid
from ecom_common.keys import generate_rsa_keypair
from ecom_common.security import create_token, decode_token
from fakeredis import FakeAsyncRedis
from test_gateway import SECRET, Upstream, assert_uniform

INTERNAL = "internal-secret-that-is-long-enough-for-hmac"


def build(upstream: Upstream, ip: str = "10.0.0.1", **overrides):
    """A gateway plus its fake Redis, for a client that connects from ``ip``."""
    redis = FakeAsyncRedis(decode_responses=True)
    settings = Settings(**{"jwt_secret": SECRET, "rate_limit_requests": 1000, **overrides})
    app = create_app(
        settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        redis_client=redis,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 5000)), base_url="http://gw"
    )
    return client, redis


def hs_token(sub="user-1", role="customer") -> str:
    return create_token(sub, secret=SECRET, role=role)


# ---- signed identity -------------------------------------------------------------------------
async def test_services_receive_a_signed_identity_for_exactly_this_request():
    upstream = Upstream()
    client, _ = build(upstream, internal_auth_secret=INTERNAL)
    async with client:
        await client.get(
            "/api/v1/orders", headers={"Authorization": f"Bearer {hs_token('u9', 'admin')}"}
        )
    sent = {k.lower(): v for k, v in upstream.requests[0].headers.items()}
    assert sent["x-user-id"] == "u9" and sent["x-user-role"] == "admin"
    assert identity_is_valid(INTERNAL, sent, "GET", "/api/v1/orders")
    assert not identity_is_valid(INTERNAL, sent, "GET", "/api/v1/payments")  # useless elsewhere


async def test_a_client_cannot_bring_its_own_signature():
    upstream = Upstream()
    client, _ = build(upstream, internal_auth_secret=INTERNAL)
    forged = {
        "X-User-Id": "victim",
        "X-User-Role": "admin",
        "X-Identity-Timestamp": str(int(time.time())),
        "X-Identity-Signature": "0" * 64,
    }
    async with client:
        await client.get("/api/v1/template/ping", headers=forged)  # anonymous, public route
    sent = {k.lower() for k in upstream.requests[0].headers}
    assert not sent & {"x-user-id", "x-user-role", "x-identity-timestamp", "x-identity-signature"}


async def test_without_an_internal_secret_identity_is_plain_headers_for_development():
    upstream = Upstream()
    client, _ = build(upstream)
    async with client:
        await client.get("/api/v1/orders", headers={"Authorization": f"Bearer {hs_token('u1')}"})
    sent = {k.lower(): v for k, v in upstream.requests[0].headers.items()}
    assert sent["x-user-id"] == "u1" and "x-identity-signature" not in sent


async def test_a_logged_out_access_token_is_refused_immediately():
    upstream = Upstream()
    client, redis = build(upstream)
    token = hs_token()
    jti = decode_token(token, secret=SECRET)["jti"]
    async with client:
        auth = {"Authorization": f"Bearer {token}"}
        assert (await client.get("/api/v1/orders", headers=auth)).status_code == 200
        await redis.set(f"auth:revoked:{jti}", "1", ex=600)
        body = assert_uniform(await client.get("/api/v1/orders", headers=auth), 401, False)
        assert "revoked" in body["error_message"]
        assert (
            await client.get("/api/v1/orders", headers={"Authorization": f"Bearer {hs_token()}"})
        ).status_code == 200


async def test_a_revoked_token_is_still_fine_on_public_routes_but_never_identifies_anyone():
    upstream = Upstream()
    client, redis = build(upstream)
    token = hs_token()
    await redis.set(f"auth:revoked:{decode_token(token, secret=SECRET)['jti']}", "1")
    async with client:
        await client.get("/api/v1/template/ping", headers={"Authorization": f"Bearer {token}"})
    assert "x-user-id" not in {k.lower() for k in upstream.requests[0].headers}


async def test_if_redis_is_down_tokens_are_still_accepted():
    upstream = Upstream()
    client, redis = build(upstream)

    async def down(*a, **k):
        raise ConnectionError("redis down")

    redis.exists = down
    redis.zremrangebyscore = down
    async with client:
        resp = await client.get("/api/v1/orders", headers={"Authorization": f"Bearer {hs_token()}"})
    assert resp.status_code == 200


async def test_password_guessing_is_slowed_down_per_ip():
    upstream = Upstream()
    client, _ = build(upstream, login_rate_limit=3)
    async with client:
        codes = [(await client.post("/api/v1/auth/login", json={})).status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        blocked = await client.post("/api/v1/auth/login", json={})
        assert_uniform(blocked, 429, False)
        assert int(blocked.headers["retry-after"]) >= 1
        assert len(upstream.requests) == 3  # blocked attempts never reached the auth service


async def test_the_limit_is_per_ip_and_only_on_those_routes():
    upstream = Upstream()
    one, redis = build(upstream, ip="10.0.0.1", login_rate_limit=1)
    async with one:
        assert (await one.post("/api/v1/auth/login", json={})).status_code == 200
        assert (await one.post("/api/v1/auth/login", json={})).status_code == 429
        # other auth routes and other paths are untouched
        assert (await one.post("/api/v1/auth/refresh", json={})).status_code == 200
        assert (await one.get("/api/v1/template/ping")).status_code == 200
    # a different visitor is not punished for it
    other = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=one._transport.app, client=("10.0.0.2", 5000)),
        base_url="http://gw",
    )
    async with other:
        assert (await other.post("/api/v1/auth/login", json={})).status_code == 200


async def test_mass_registration_is_limited_more_tightly_than_login():
    upstream = Upstream()
    client, _ = build(upstream, register_rate_limit=2)
    async with client:
        codes = [
            (await client.post("/api/v1/auth/register", json={})).status_code for _ in range(3)
        ]
    assert codes == [200, 200, 429]


@pytest.fixture(scope="module")
def keys():
    return generate_rsa_keypair()


def rs256_client(upstream, public_pem):
    return build(upstream, jwt_algorithm="RS256", jwt_public_key=public_pem)


async def test_the_gateway_accepts_tokens_signed_by_the_auth_services_private_key(keys):
    private, public = keys
    upstream = Upstream()
    client, _ = rs256_client(upstream, public)
    token = create_token("user-7", secret=private, algorithm="RS256", role="customer")
    async with client:
        assert (
            await client.get("/api/v1/orders", headers={"Authorization": f"Bearer {token}"})
        ).status_code == 200
    assert upstream.requests[0].headers["x-user-id"] == "user-7"


async def test_the_gateway_cannot_forge_tokens_because_it_has_no_private_key(keys):
    _, public = keys
    # signing needs the private key; a public key cannot sign
    with pytest.raises(Exception):  # noqa: B017, PT011 - any failure to sign is the point
        create_token("attacker", secret=public, algorithm="RS256", role="admin")


async def test_tokens_from_another_key_pair_and_hs256_forgeries_are_refused(keys):
    _, public = keys
    upstream = Upstream()
    client, _ = rs256_client(upstream, public)

    other_private, _ = generate_rsa_keypair()
    foreign = create_token("attacker", secret=other_private, algorithm="RS256", role="admin")

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = {
        "sub": "attacker",
        "role": "admin",
        "type": "access",
        "jti": "x",
        "exp": int(time.time()) + 600,
    }
    payload = b64(json.dumps(claims).encode())
    mac = hmac.new(public.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    confused = f"{header}.{payload}.{b64(mac)}"

    async with client:
        for bad in (foreign, confused, hs_token()):
            resp = await client.get("/api/v1/orders", headers={"Authorization": f"Bearer {bad}"})
            assert_uniform(resp, 401, False)
    assert upstream.requests == []


def test_production_requires_rs256_and_a_public_key_and_a_real_internal_secret(keys):
    _, public = keys
    problems = Settings(environment="production").production_problems()
    assert any("RS256" in p for p in problems) and any(
        "INTERNAL_AUTH_SECRET" in p for p in problems
    )
    assert any(
        "PUBLIC_KEY" in p
        for p in Settings(environment="production", jwt_algorithm="RS256").production_problems()
    )

    ok = Settings(
        environment="production",
        jwt_algorithm="RS256",
        jwt_public_key=public,
        internal_auth_secret="x" * 40,
        metrics_token="m" * 20,
    )
    assert ok.production_problems() == []
    ok.assert_safe_for_environment()


def test_the_gateway_refuses_to_start_in_production_with_defaults():
    with pytest.raises(RuntimeError, match="refuses to start in production"):
        create_app(Settings(environment="production"))
