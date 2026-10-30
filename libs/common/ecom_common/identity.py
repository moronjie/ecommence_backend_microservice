import hashlib
import hmac
import time
from collections.abc import Mapping

USER_ID = "x-user-id"
USER_ROLE = "x-user-role"
TIMESTAMP = "x-identity-timestamp"
SIGNATURE = "x-identity-signature"
IDENTITY_HEADERS = (USER_ID, USER_ROLE, TIMESTAMP, SIGNATURE)
MAX_AGE_SECONDS = 60


def _mac(secret: str, user_id: str, role: str, timestamp: str, method: str, path: str) -> str:
    message = "|".join((user_id, role, timestamp, method.upper(), path)).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign_identity(
    secret: str, user_id: str, role: str, method: str, path: str, now: float | None = None
) -> dict[str, str]:
    """The four headers to send to a service on behalf of a user."""
    timestamp = str(int(now if now is not None else time.time()))
    return {
        "X-User-Id": user_id,
        "X-User-Role": role,
        "X-Identity-Timestamp": timestamp,
        "X-Identity-Signature": _mac(secret, user_id, role, timestamp, method, path),
    }


def identity_is_valid(
    secret: str,
    headers: Mapping[str, str],
    method: str,
    path: str,
    now: float | None = None,
) -> bool:
    """``headers`` must use lower-case names. True only for a fresh, untampered signature."""
    user_id, role = headers.get(USER_ID), headers.get(USER_ROLE)
    timestamp, signature = headers.get(TIMESTAMP), headers.get(SIGNATURE)
    if not (user_id and role and timestamp and signature):
        return False
    try:
        age = (now if now is not None else time.time()) - int(timestamp)
    except ValueError:
        return False
    if not -MAX_AGE_SECONDS <= age <= MAX_AGE_SECONDS:  # tolerate small clock differences
        return False
    expected = _mac(secret, user_id, role, timestamp, method, path)
    return hmac.compare_digest(expected, signature)


class VerifyIdentityMiddleware:
    """Pure ASGI middleware. A request whose identity headers are missing a valid signature has
    ALL of them removed, so the service sees an anonymous caller (and its own checks answer 401),
    exactly as if the caller had not claimed an identity at all."""

    def __init__(self, app, secret: str) -> None:
        self.app = app
        self.secret = secret

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            claims_identity = any(name in headers for name in IDENTITY_HEADERS)
            if claims_identity and not identity_is_valid(
                self.secret, headers, scope["method"], scope["path"]
            ):
                scope = dict(scope)
                scope["headers"] = [
                    (k, v)
                    for k, v in scope["headers"]
                    if k.decode().lower() not in IDENTITY_HEADERS
                ]
        await self.app(scope, receive, send)
