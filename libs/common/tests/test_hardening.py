import base64
import hashlib
import hmac
import json
import time

import httpx
import jwt
import pytest
from ecom_common import Unauthorized
from ecom_common.config import BaseServiceSettings
from ecom_common.identity import (
    IDENTITY_HEADERS,
    VerifyIdentityMiddleware,
    identity_is_valid,
    sign_identity,
)
from ecom_common.keys import generate_rsa_keypair, public_key_from_private, read_key
from ecom_common.security import create_token, decode_token
from fastapi import FastAPI, Request

SECRET = "internal-secret-that-is-long-enough-for-hmac"


@pytest.fixture(scope="module")
def keypair() -> tuple[str, str]:
    return generate_rsa_keypair()


def test_a_token_signed_with_the_private_key_verifies_with_the_public_key(keypair):
    private, public = keypair
    token = create_token("user-1", secret=private, algorithm="RS256", role="admin")
    claims = decode_token(token, secret=public, algorithm="RS256")
    assert claims["sub"] == "user-1" and claims["role"] == "admin"


def test_the_public_key_can_be_derived_from_the_private_one(keypair):
    private, public = keypair
    assert public_key_from_private(private) == public


def test_a_different_key_pair_cannot_forge_tokens(keypair):
    _, public = keypair
    other_private, _ = generate_rsa_keypair()
    forged = create_token("attacker", secret=other_private, algorithm="RS256", role="admin")
    with pytest.raises(Unauthorized):
        decode_token(forged, secret=public, algorithm="RS256")


def test_algorithm_confusion_attack_is_blocked(keypair):
    """The classic attack on RS256: the public key is public, so sign a token with HS256 using the
    public key's text as the 'shared secret' and hope the verifier accepts it. It must not."""
    _, public = keypair

    # PyJWT refuses to build this token (a safety feature), so craft it by hand like an attacker
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
    signature = hmac.new(public.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    forged = f"{header}.{payload}.{b64(signature)}"

    with pytest.raises(Unauthorized):
        decode_token(forged, secret=public, algorithm="RS256")  # the verifier only accepts RS256


def test_an_unsigned_token_is_blocked(keypair):
    _, public = keypair
    unsigned = jwt.encode(
        {"sub": "attacker", "role": "admin", "type": "access", "exp": int(time.time()) + 600},
        key=None,
        algorithm="none",
    )
    with pytest.raises(Unauthorized):
        decode_token(unsigned, secret=public, algorithm="RS256")


def test_keys_can_come_from_a_file_or_an_env_variable(tmp_path, keypair):
    private, _ = keypair
    path = tmp_path / "private.pem"
    path.write_text(private)
    assert read_key(path=str(path)) == private.strip()
    assert read_key(inline=private.replace("\n", "\\n")) == private.strip()  # one-line env var form
    assert read_key(inline="", path="") == ""


def lower(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def test_a_signed_identity_is_accepted():
    h = lower(sign_identity(SECRET, "user-1", "customer", "GET", "/api/v1/orders"))
    assert identity_is_valid(SECRET, h, "GET", "/api/v1/orders")


@pytest.mark.parametrize(
    "tamper",
    [
        lambda h: {**h, "x-user-id": "someone-else"},
        lambda h: {**h, "x-user-role": "admin"},  # promoting yourself
        lambda h: {**h, "x-identity-signature": "0" * 64},
        lambda h: {k: v for k, v in h.items() if k != "x-identity-signature"},
        lambda h: {k: v for k, v in h.items() if k != "x-identity-timestamp"},
        lambda h: {**h, "x-identity-timestamp": "not-a-number"},
    ],
)
def test_any_change_to_a_signed_identity_is_rejected(tamper):
    h = lower(sign_identity(SECRET, "user-1", "customer", "GET", "/api/v1/orders"))
    assert not identity_is_valid(SECRET, tamper(h), "GET", "/api/v1/orders")


def test_a_signature_is_only_good_for_its_own_endpoint_secret_and_moment():
    now = 1_700_000_000
    h = lower(sign_identity(SECRET, "user-1", "admin", "POST", "/api/v1/orders/1/ship", now=now))
    assert identity_is_valid(SECRET, h, "POST", "/api/v1/orders/1/ship", now=now + 30)
    assert not identity_is_valid(SECRET, h, "POST", "/api/v1/orders/2/ship", now=now)  # other path
    assert not identity_is_valid(SECRET, h, "GET", "/api/v1/orders/1/ship", now=now)  # other method
    assert not identity_is_valid(
        "another-secret-of-adequate-length!!", h, "POST", "/api/v1/orders/1/ship", now=now
    )
    assert not identity_is_valid(
        SECRET, h, "POST", "/api/v1/orders/1/ship", now=now + 3600
    )  # replayed later


def make_app(secret: str | None) -> FastAPI:
    app = FastAPI()
    if secret:
        app.add_middleware(VerifyIdentityMiddleware, secret=secret)

    @app.get("/whoami")
    async def whoami(request: Request):
        return {name: request.headers.get(name) for name in IDENTITY_HEADERS}

    return app


async def call(app: FastAPI, headers: dict[str, str]) -> dict:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://svc"
    ) as c:
        return (await c.get("/whoami", headers=headers)).json()


async def test_the_service_sees_a_properly_signed_identity():
    headers = sign_identity(SECRET, "user-1", "customer", "GET", "/whoami")
    seen = await call(make_app(SECRET), headers)
    assert seen["x-user-id"] == "user-1" and seen["x-user-role"] == "customer"


async def test_a_forged_identity_is_erased_so_the_caller_is_anonymous():
    seen = await call(make_app(SECRET), {"X-User-Id": "victim", "X-User-Role": "admin"})
    assert all(value is None for value in seen.values())


async def test_a_signature_for_another_endpoint_does_not_work_here():
    headers = sign_identity(SECRET, "user-1", "admin", "GET", "/something-else")
    seen = await call(make_app(SECRET), headers)
    assert all(value is None for value in seen.values())


async def test_without_a_secret_identity_headers_pass_through_for_local_development():
    seen = await call(make_app(None), {"X-User-Id": "dev-user", "X-User-Role": "admin"})
    assert seen["x-user-id"] == "dev-user"


def test_local_is_never_blocked():
    BaseServiceSettings(environment="local").assert_safe_for_environment()


def test_production_refuses_unsafe_defaults_and_says_why():
    settings = BaseServiceSettings(
        service_name="auth-service",
        environment="production",
        database_url="postgresql+asyncpg://postgres:postgres@db/auth_db",
        debug=True,
    )
    with pytest.raises(RuntimeError) as error:
        settings.assert_safe_for_environment()
    message = str(error.value)
    assert "auth-service refuses to start in production" in message
    assert "INTERNAL_AUTH_SECRET" in message and "default 'postgres' password" in message
    assert "DEBUG" in message


@pytest.mark.parametrize("weak", ["", "change-me-in-production", "dev-internal-secret", "short"])
def test_weak_internal_secrets_are_refused_in_production(weak):
    settings = BaseServiceSettings(environment="production", internal_auth_secret=weak)
    with pytest.raises(RuntimeError, match="INTERNAL_AUTH_SECRET"):
        settings.assert_safe_for_environment()


def test_production_accepts_a_safe_configuration():
    BaseServiceSettings(
        environment="production",
        internal_auth_secret="x" * 40,
        metrics_token="m" * 20,
        database_url="postgresql+asyncpg://shop:a-strong-password@db/shop_db",
    ).assert_safe_for_environment()
