from collections.abc import AsyncIterator
from typing import Annotated

from ecom_common import Unauthorized
from fastapi import Depends, Header, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.services.auth_service import AuthService


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_auth_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthService:
    return AuthService(session, redis, settings)


def current_user_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    """The gateway verifies the JWT and sets X-User-Id. This service is never exposed directly."""
    if not x_user_id:
        raise Unauthorized("Missing user identity")
    return x_user_id


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
CurrentUserId = Annotated[str, Depends(current_user_id)]
