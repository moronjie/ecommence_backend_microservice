"""Attacks against the live stack. Each test does what an attacker would try and must fail."""

import base64
import hashlib
import hmac
import json
import os
import time
import uuid

import httpx
import pytest
from conftest import PASSWORD, eventually

# Direct access to a service, bypassing the gateway (the dev compose file publishes these ports)
ORDER_URL = os.environ.get("E2E_ORDER_URL", "http://localhost:8006")
PUBLIC_KEY_FILE = os.environ.get("E2E_PUBLIC_KEY_FILE", "")  # set when the stack runs RS256


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def reachable(url: str) -> bool:
    try:
        httpx.get(f"{url}/health", timeout=3)
        return True
    except httpx.HTTPError:
        return False


def test_a_service_reached_directly_ignores_a_made_up_identity(shop):
    """Someone who can reach a service (a mistake in the network, another compromised container)
    simply claims to be an admin. The service must not believe an unsigned identity."""
    if not reachable(ORDER_URL):
        pytest.skip(f"{ORDER_URL} is not reachable from here (ports not published)")
    forged = {"X-User-Id": str(uuid.uuid4()), "X-User-Role": "admin"}
    resp = httpx.get(f"{ORDER_URL}/api/v1/orders", headers=forged, timeout=10)
    assert resp.status_code == 401, "the order service trusted an unsigned identity!"

    wrong_signature = {
        **forged,
        "X-Identity-Timestamp": str(int(time.time())),
        "X-Identity-Signature": "0" * 64,
    }
    assert (
        httpx.get(f"{ORDER_URL}/api/v1/orders", headers=wrong_signature, timeout=10).status_code
        == 401
    )


def test_the_gateway_strips_identity_headers_sent_by_clients(shop, admin_token):
    """A customer adds 'X-User-Role: admin' to a request. The gateway must overwrite it."""
    _, token = shop.new_customer()
    resp = shop.call("GET", "/api/v1/payments", token, headers={"X-User-Role": "admin"})
    assert resp.status_code == 403  # still just a customer: the admin-only list stays closed


def test_logging_out_kills_the_access_token_immediately(shop):
    email, _ = shop.new_customer()
    login = shop.data("POST", "/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    access, refresh = login["access_token"], login["refresh_token"]
    assert shop.call("GET", "/api/v1/orders", access).status_code == 200

    shop.data("POST", "/api/v1/auth/logout", access, json={"refresh_token": refresh})

    # the access token is still within its 15 minutes, yet it must be dead right now
    after = shop.call("GET", "/api/v1/orders", access)
    assert after.status_code == 401 and "revoked" in after.text
    assert (
        shop.call("POST", "/api/v1/auth/refresh", json={"refresh_token": refresh}).status_code
        == 401
    )


def test_guessing_passwords_locks_the_account(shop):
    email, _ = shop.new_customer()
    codes = [
        shop.call(
            "POST", "/api/v1/auth/login", json={"email": email, "password": f"wrong-{i}"}
        ).status_code
        for i in range(5)
    ]
    assert codes == [401] * 5
    locked = shop.call("POST", "/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert locked.status_code == 429  # even the right password is refused for a while
    assert int(locked.headers["retry-after"]) > 0
    # the same for an address that does not exist: nothing reveals which emails are registered
    ghost = f"nobody-{uuid.uuid4().hex[:8]}@example.com"
    ghost_codes = [
        shop.call(
            "POST", "/api/v1/auth/login", json={"email": ghost, "password": "x" * 10}
        ).status_code
        for _ in range(6)
    ]
    assert ghost_codes == [401] * 5 + [429]


def test_the_payment_webhook_needs_a_real_signature(shop):
    body = json.dumps({"event": "charge.success", "data": {"reference": "made-up"}}).encode()
    no_login_needed = shop.call("POST", "/api/v1/payments/webhooks/paystack", content=body)
    assert (
        no_login_needed.status_code == 401
    )  # reachable without a token, but refused: no signature
    forged = shop.call(
        "POST",
        "/api/v1/payments/webhooks/paystack",
        content=body,
        headers={"x-paystack-signature": "f" * 128},
    )
    assert forged.status_code == 401


def test_metrics_are_not_readable_through_the_public_gateway_routes(shop):
    # /metrics belongs to the gateway process itself; per-service metrics have no public route
    for path in ("/api/v1/orders/metrics", "/api/v1/auth/metrics"):
        assert shop.call("GET", path).status_code in (401, 404)


@pytest.mark.skipif(not PUBLIC_KEY_FILE, reason="set E2E_PUBLIC_KEY_FILE when the stack runs RS256")
def test_a_token_forged_with_the_public_key_is_refused(shop):
    """The classic RS256 attack: sign with HS256 using the public key as if it were a secret."""
    public = open(PUBLIC_KEY_FILE, encoding="utf-8").read().strip()
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = {
        "sub": str(uuid.uuid4()),
        "role": "admin",
        "type": "access",
        "jti": uuid.uuid4().hex,
        "exp": int(time.time()) + 600,
    }
    payload = b64(json.dumps(claims).encode())
    mac = hmac.new(public.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    forged = f"{header}.{payload}.{b64(mac)}"
    assert shop.call("GET", "/api/v1/payments", forged).status_code == 401
    none_alg = f"{b64(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())}.{payload}."
    assert shop.call("GET", "/api/v1/payments", none_alg).status_code == 401


def test_real_tokens_still_work_after_all_that(shop, admin_token):
    assert shop.call("GET", "/api/v1/payments", admin_token).status_code == 200
    eventually(
        lambda: shop.call("GET", "/health").status_code == 200, "the gateway to stay healthy"
    )
