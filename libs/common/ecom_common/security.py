from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from ecom_common.exceptions import Unauthorized

ACCESS = "access"
REFRESH = "refresh"


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
