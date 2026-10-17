import httpx
from app.models.user import User
from ecom_common.security import decode_token
from sqlalchemy import select

KEYS = {"success", "message", "data", "error_message"}
PASSWORD = "S3cure-passw0rd"


def uniform(resp: httpx.Response, status: int, success: bool) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == KEYS and body["success"] is success
    return body


async def register(env, email="ada@example.com", password=PASSWORD, **extra):
    return await env.client.post(
        "/api/v1/auth/register", json={"email": email, "password": password, **extra}
    )


async def login(env, email="ada@example.com", password=PASSWORD):
    return await env.client.post("/api/v1/auth/login", json={"email": email, "password": password})


async def tokens(env) -> dict:
    await register(env)
    return uniform(await login(env), 200, True)["data"]


# ---- register -------------------------------------------------------------------------
async def test_register_creates_customer_without_leaking_password(env):
    body = uniform(await register(env, full_name="Ada L"), 201, True)
    user = body["data"]
    assert user["email"] == "ada@example.com" and user["role"] == "customer"
    assert "password" not in str(body).lower()


async def test_password_is_stored_hashed(env):
    await register(env)
    async with env.session_factory() as s:
        stored = (await s.scalars(select(User))).one().password_hash
    assert stored != PASSWORD and stored.startswith("$argon2")


async def test_register_publishes_user_registered_event(env):
    user_id = (await register(env)).json()["data"]["id"]
    assert await env.relay.publish_pending() == 1
    topic, key, event = env.publisher.events[0]
    assert topic == "user.registered" and key == user_id
    assert event["payload"]["email"] == "ada@example.com"
    assert event["payload"]["user_id"] == user_id


async def test_duplicate_email_conflicts_and_publishes_nothing_extra(env):
    await register(env)
    uniform(await register(env, email="ADA@Example.com"), 409, False)  # case-insensitive
    assert await env.relay.publish_pending() == 1


async def test_register_validation(env):
    uniform(await register(env, password="short"), 422, False)
    uniform(await register(env, email="not-an-email"), 422, False)


# ---- login ----------------------------------------------------------------------------
async def test_login_returns_verifiable_tokens(env):
    await register(env)
    data = uniform(await login(env), 200, True)["data"]
    assert data["token_type"] == "bearer" and data["expires_in"] == 900
    claims = decode_token(data["access_token"], secret=env.settings.jwt_secret)
    assert claims["role"] == "customer" and claims["type"] == "access"


async def test_login_errors_do_not_reveal_whether_email_exists(env):
    await register(env)
    wrong_password = uniform(await login(env, password="wrong-password"), 401, False)
    unknown_email = uniform(await login(env, email="nobody@example.com"), 401, False)
    assert wrong_password["error_message"] == unknown_email["error_message"]


async def test_inactive_user_cannot_login(env):
    await register(env)
    async with env.session_factory() as s:
        (await s.scalars(select(User))).one().is_active = False
        await s.commit()
    uniform(await login(env), 401, False)


# ---- refresh --------------------------------------------------------------------------
async def test_refresh_rotates_and_old_token_stops_working(env):
    first = await tokens(env)
    refresh = lambda t: env.client.post("/api/v1/auth/refresh", json={"refresh_token": t})  # noqa: E731

    second = uniform(await refresh(first["refresh_token"]), 200, True)["data"]
    assert second["refresh_token"] != first["refresh_token"]
    reuse = uniform(await refresh(first["refresh_token"]), 401, False)
    assert "already been used" in reuse["error_message"]
    uniform(await refresh(second["refresh_token"]), 200, True)  # the new one works


async def test_access_token_and_garbage_rejected_as_refresh(env):
    data = await tokens(env)
    for bad in (data["access_token"], "garbage"):
        resp = await env.client.post("/api/v1/auth/refresh", json={"refresh_token": bad})
        uniform(resp, 401, False)


# ---- logout ---------------------------------------------------------------------------
async def test_logout_revokes_refresh_token(env):
    data = await tokens(env)
    user_id = decode_token(data["access_token"], secret=env.settings.jwt_secret)["sub"]
    resp = await env.client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": data["refresh_token"]},
        headers={"X-User-Id": user_id},
    )
    uniform(resp, 200, True)
    after = await env.client.post(
        "/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]}
    )
    uniform(after, 401, False)


async def test_logout_needs_identity_and_matching_owner(env):
    data = await tokens(env)
    body = {"refresh_token": data["refresh_token"]}
    uniform(await env.client.post("/api/v1/auth/logout", json=body), 401, False)
    other = await env.client.post(
        "/api/v1/auth/logout", json=body, headers={"X-User-Id": "someone-else"}
    )
    uniform(other, 403, False)


# ---- me / admin / health --------------------------------------------------------------
async def test_me_uses_gateway_identity_header(env):
    user_id = (await register(env)).json()["data"]["id"]
    resp = await env.client.get("/api/v1/auth/me", headers={"X-User-Id": user_id})
    assert uniform(resp, 200, True)["data"]["email"] == "ada@example.com"
    uniform(await env.client.get("/api/v1/auth/me"), 401, False)


async def test_ensure_admin_creates_admin_once(env):
    from app.services.auth_service import AuthService

    async with env.session_factory() as s:
        svc = AuthService(s, env.client._transport.app.state.redis, env.settings)  # noqa: SLF001
        await svc.ensure_admin("root@example.com", PASSWORD)
        await svc.ensure_admin("root@example.com", PASSWORD)
    data = uniform(await login(env, "root@example.com"), 200, True)["data"]
    assert decode_token(data["access_token"], secret=env.settings.jwt_secret)["role"] == "admin"


async def test_health_and_ready(env):
    uniform(await env.client.get("/health"), 200, True)
    uniform(await env.client.get("/ready"), 200, True)
