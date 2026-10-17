"""Business rules for registration, login, refresh and logout."""

import time
import uuid
from datetime import timedelta

from ecom_common import Conflict, Forbidden, Unauthorized
from ecom_common.outbox import add_event
from ecom_common.security import (
    ACCESS,
    REFRESH,
    create_token,
    decode_token,
    hash_password,
    hash_password_async,
    verify_password_async,
)
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.user import ADMIN, CUSTOMER, User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import RegisterRequest, TokenPair

# Verified when the email is unknown, so "no such user" costs as long as "wrong password"
_DUMMY_HASH = hash_password("not-a-real-password")
_BAD_LOGIN = "Invalid email or password"


class AuthService:
    def __init__(self, session: AsyncSession, redis: Redis, settings: Settings) -> None:
        self.session = session
        self.users = UserRepository(session)
        self.redis = redis
        self.settings = settings

    # ---- registration -------------------------------------------------------------------
    async def register(self, data: RegisterRequest, *, role: str = CUSTOMER) -> User:
        email = data.email.strip().lower()
        if await self.users.get_by_email(email):
            raise Conflict("An account with this email already exists")

        user = User(
            email=email,
            password_hash=await hash_password_async(data.password),
            full_name=data.full_name.strip(),
            role=role,
        )
        self.users.add(user)
        await self.session.flush()  # assigns the id
        # Same transaction as the user row: the event exists if and only if the user does
        add_event(
            self.session,
            "user.registered",
            {
                "user_id": str(user.id),
                "email": user.email,
                "full_name": user.full_name,
                "role": user.role,
            },
            key=str(user.id),
        )
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise Conflict("An account with this email already exists") from exc
        return user

    async def ensure_admin(self, email: str, password: str) -> None:
        if await self.users.get_by_email(email.strip().lower()):
            return
        await self.register(
            RegisterRequest(email=email, password=password, full_name="Administrator"),
            role=ADMIN,
        )

    # ---- tokens -------------------------------------------------------------------------
    def _issue(self, user: User) -> TokenPair:
        s = self.settings
        access_seconds = s.access_token_minutes * 60
        common = {"secret": s.jwt_secret, "algorithm": s.jwt_algorithm, "role": user.role}
        return TokenPair(
            access_token=create_token(
                str(user.id),
                expires_in=timedelta(seconds=access_seconds),
                token_type=ACCESS,
                **common,
            ),
            refresh_token=create_token(
                str(user.id),
                expires_in=timedelta(seconds=s.refresh_token_days * 86400),
                token_type=REFRESH,
                **common,
            ),
            expires_in=access_seconds,
        )

    def _decode_refresh(self, token: str) -> dict:
        return decode_token(
            token,
            secret=self.settings.jwt_secret,
            algorithm=self.settings.jwt_algorithm,
            expected_type=REFRESH,
        )

    async def _revoke(self, claims: dict) -> bool:
        """Mark a refresh token as used. False if it was already revoked (atomic)."""
        ttl = max(1, int(claims["exp"] - time.time()))
        return bool(await self.redis.set(f"auth:revoked:{claims['jti']}", "1", ex=ttl, nx=True))

    # ---- login / refresh / logout -------------------------------------------------------
    async def login(self, email: str, password: str) -> TokenPair:
        user = await self.users.get_by_email(email.strip().lower())
        valid = await verify_password_async(password, user.password_hash if user else _DUMMY_HASH)
        if not (user and valid and user.is_active):
            raise Unauthorized(_BAD_LOGIN, message="Login failed")
        return self._issue(user)

    async def refresh(self, refresh_token: str) -> TokenPair:
        claims = self._decode_refresh(refresh_token)
        # Rotation: each refresh token works exactly once
        if not await self._revoke(claims):
            raise Unauthorized("Refresh token has already been used or revoked")
        user = await self.users.get_by_id(uuid.UUID(claims["sub"]))
        if not user or not user.is_active:
            raise Unauthorized("Account is not available")
        return self._issue(user)

    async def logout(self, refresh_token: str, caller_id: str) -> None:
        claims = self._decode_refresh(refresh_token)
        if claims["sub"] != caller_id:
            raise Forbidden("This token belongs to another user")
        await self._revoke(claims)  # already revoked is fine: logout is idempotent

    async def get_user(self, user_id: str) -> User:
        try:
            user = await self.users.get_by_id(uuid.UUID(user_id))
        except ValueError:
            user = None
        if not user:
            raise Unauthorized("Account not found")
        return user
