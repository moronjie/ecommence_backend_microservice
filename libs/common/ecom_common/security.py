import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from ecom_common.exceptions import Unauthorized

ACCESS = "access"
REFRESH = "refresh"

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# Argon2 is deliberately slow (tens of ms, ~64 MB). Calling it directly in an async handler would
# freeze the event loop, so run it in a thread and cap how many run at once (memory).
_hash_slots = asyncio.Semaphore(4)


async def hash_password_async(password: str) -> str:
    async with _hash_slots:
        return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, password_hash: str) -> bool:
    async with _hash_slots:
        return await asyncio.to_thread(verify_password, password, password_hash)


def create_token(
    subject: str,
    *,
    secret: str,
    algorithm: str = "HS256",
    expires_in: timedelta = timedelta(minutes=15),
    role: str = "customer",
    token_type: str = ACCESS,
    extra: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": subject,
        "role": role,
        "type": token_type,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + expires_in,
        **(extra or {}),
    }
    return jwt.encode(claims, secret, algorithm=algorithm)


def decode_token(
    token: str,
    *,
    secret: str,
    algorithm: str = "HS256",
    expected_type: str = ACCESS,
) -> dict[str, Any]:
    """Return the claims, or raise ``Unauthorized`` for any invalid token."""
    try:
        claims = jwt.decode(
            token, secret, algorithms=[algorithm], options={"require": ["exp", "sub"]}
        )
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("Token has expired", message="Invalid token") from exc
    except jwt.InvalidTokenError as exc:
        raise Unauthorized("Token is invalid", message="Invalid token") from exc
    if claims.get("type") != expected_type:
        raise Unauthorized(f"Expected a {expected_type} token", message="Invalid token")
    return claims
