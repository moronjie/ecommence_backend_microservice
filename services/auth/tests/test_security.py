"""Auth hardening: login lockout, logout that kills the access token, RS256 signing."""

import httpx
import jwt
import pytest
from app.core.config import Settings
from app.main import create_app
from conftest import SECRET, Env
from ecom_common.db import Base, create_engine, create_session_factory
from ecom_common.kafka import InMemoryPublisher
from ecom_common.keys import generate_rsa_keypair, public_key_from_private
from ecom_common.outbox import OutboxRelay
from ecom_common.security import decode_token
from fakeredis import FakeAsyncRedis
from sqlalchemy.pool import StaticPool

PASSWORD = "S3cure-passw0rd"


def redis_of(env: Env) -> FakeAsyncRedis:
    return env.client._transport.app.state.redis  # noqa: SLF001


async def register(env: Env, email="ada@example.com") -> str:
    resp = await env.client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    return resp.json()["data"]["id"]


async def login(env: Env, email="ada@example.com", password=PASSWORD) -> httpx.Response:
    return await env.client.post("/api/v1/auth/login", json={"email": email, "password": password})


# ---- lockout after repeated failures -------------------------------------------------------
async def test_five_wrong_passwords_lock_the_account_even_for_the_right_password(env):
    await register(env)
    for _ in range(5):
        assert (await login(env, password="wrong-password")).status_code == 401
    locked = await login(env)  # the CORRECT password, but the account is locked
    assert locked.status_code == 429 and locked.json()["success"] is False
    assert int(locked.headers["retry-after"]) > 0
    assert "try again" in locked.json()["error_message"].lower()


async def test_the_lockout_is_per_email_and_does_not_reveal_which_emails_exist(env):
    await register(env, "ada@example.com")
    await register(env, "bob@example.com")
    for email in ("ada@example.com", "nobody@example.com"):  # a real account and an invented one
        statuses = [(await login(env, email, "wrong-password")).status_code for _ in range(6)]
        assert statuses == [401] * 5 + [429], email  # identical behaviour either way
    assert (await login(env, "bob@example.com")).status_code == 200  # others are unaffected


async def test_a_successful_login_resets_the_count(env):
    await register(env)
    for _ in range(4):
        await login(env, password="wrong-password")
    assert (await login(env)).status_code == 200  # one short of the limit, then success
    for _ in range(4):  # the count started again from zero
        assert (await login(env, password="wrong-password")).status_code == 401
    assert (await login(env)).status_code == 200


async def test_the_lockout_ends_by_itself(env):
    await register(env)
    for _ in range(5):
        await login(env, password="wrong-password")
    redis = redis_of(env)
    key = "auth:login-failures:ada@example.com"
    assert 0 < await redis.ttl(key) <= env.settings.login_lockout_seconds
    await redis.delete(key)  # what the passing of the lockout period does
    assert (await login(env)).status_code == 200


async def test_logins_keep_working_if_redis_is_down(env):
    await register(env)

    async def down(*a, **k):
        raise ConnectionError("redis down")

    redis = redis_of(env)
    redis.get = redis.incr = redis.delete = redis.expire = redis.ttl = down
    assert (await login(env)).status_code == 200


# ---- logout kills the access token as well ---------------------------------------------------
async def tokens_for(env: Env) -> tuple[str, dict]:
    user_id = await register(env)
    return user_id, (await login(env)).json()["data"]


async def logout(env: Env, user_id: str, tokens: dict, bearer: str | None = None):
    headers = {"X-User-Id": user_id}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return await env.client.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}, headers=headers
    )


async def test_logout_revokes_the_access_token_so_the_gateway_can_refuse_it(env):
    user_id, tokens = await tokens_for(env)
    access = decode_token(tokens["access_token"], secret=SECRET)
    refresh = decode_token(tokens["refresh_token"], secret=SECRET, expected_type="refresh")

    assert (await logout(env, user_id, tokens, bearer=tokens["access_token"])).status_code == 200
    redis = redis_of(env)
    # the gateway looks for exactly this key
    assert await redis.exists(f"auth:revoked:{access['jti']}") == 1
    assert (
        0
        < await redis.ttl(f"auth:revoked:{access['jti']}")
        <= env.settings.access_token_minutes * 60
    )
    assert await redis.exists(f"auth:revoked:{refresh['jti']}") == 1


async def test_logout_without_an_access_token_still_revokes_the_refresh_token(env):
    user_id, tokens = await tokens_for(env)
    assert (await logout(env, user_id, tokens)).status_code == 200
    refreshed = await env.client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 401


async def test_someone_elses_or_garbage_access_tokens_are_ignored_not_revoked(env):
    user_id, tokens = await tokens_for(env)
    other_id = await register(env, "bob@example.com")
    other = (await login(env, "bob@example.com")).json()["data"]
    other_jti = decode_token(other["access_token"], secret=SECRET)["jti"]

    assert (await logout(env, user_id, tokens, bearer=other["access_token"])).status_code == 200
    assert (await logout(env, user_id, tokens, bearer="not-a-token")).status_code == 200
    assert await redis_of(env).exists(f"auth:revoked:{other_jti}") == 0  # Bob stays logged in
    assert other_id


# ---- RS256 -----------------------------------------------------------------------------------
@pytest.fixture
async def rs256():
    private, public = generate_rsa_keypair()
    engine = create_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    settings = Settings(
        jwt_algorithm="RS256", jwt_private_key=private, database_url="sqlite+aiosqlite://"
    )
    publisher = InMemoryPublisher()
    app = create_app(
        settings,
        session_factory=factory,
        redis_client=FakeAsyncRedis(decode_responses=True),
        publisher=publisher,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://auth"
    ) as client:
        yield (
            Env(client, factory, publisher, OutboxRelay(factory, publisher), settings),
            private,
            public,
        )
    await engine.dispose()


async def test_tokens_are_signed_with_the_private_key_and_verified_with_the_public_one(rs256):
    env, private, public = rs256
    await register(env)
    data = (await login(env)).json()["data"]
    assert jwt.get_unverified_header(data["access_token"])["alg"] == "RS256"
    claims = decode_token(data["access_token"], secret=public, algorithm="RS256")
    assert claims["role"] == "customer" and claims["type"] == "access"
    assert public_key_from_private(private) == public


async def test_refresh_and_logout_work_with_rs256(rs256):
    env, _, _ = rs256
    user_id, tokens = await tokens_for(env)
    second = await env.client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert second.status_code == 200
    assert (
        await logout(
            env, user_id, second.json()["data"], bearer=second.json()["data"]["access_token"]
        )
    ).status_code == 200


async def test_a_token_forged_with_the_shared_secret_is_refused_by_an_rs256_service(rs256):
    env, _, _ = rs256
    forged = jwt.encode(
        {"sub": "x", "role": "admin", "type": "refresh", "jti": "j", "exp": 9999999999},
        SECRET,
        algorithm="HS256",
    )
    resp = await env.client.post("/api/v1/auth/refresh", json={"refresh_token": forged})
    assert resp.status_code == 401


# ---- production safety -----------------------------------------------------------------------
def test_production_needs_rs256_a_private_key_and_a_real_internal_secret():
    problems = Settings(environment="production").production_problems()
    assert any("RS256" in p for p in problems) and any(
        "INTERNAL_AUTH_SECRET" in p for p in problems
    )
    private, _ = generate_rsa_keypair()
    ok = Settings(
        environment="production",
        jwt_algorithm="RS256",
        jwt_private_key=private,
        internal_auth_secret="x" * 40,
        metrics_token="m" * 20,
        database_url="postgresql+asyncpg://shop:a-strong-password@db/auth_db",
    )
    assert ok.production_problems() == []


def test_production_refuses_a_weak_admin_password():
    bad = Settings(environment="production", admin_password="password")
    assert any("ADMIN_PASSWORD" in p for p in bad.production_problems())
