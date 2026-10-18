from collections.abc import AsyncIterator
from typing import Annotated

from ecom_common import Forbidden, Unauthorized
from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.profile_service import ProfileService


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_profile_service(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> ProfileService:
    return ProfileService(session, request.app.state.settings.max_addresses)


def current_user_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    """The gateway verifies the JWT and sets X-User-Id. This service is never exposed directly."""
    if not x_user_id:
        raise Unauthorized("Missing user identity")
    return x_user_id


def require_admin(x_user_role: Annotated[str | None, Header()] = None) -> None:
    if x_user_role != "admin":
        raise Forbidden("Administrator access required")


ServiceDep = Annotated[ProfileService, Depends(get_profile_service)]
CurrentUserId = Annotated[str, Depends(current_user_id)]
AdminOnly = Depends(require_admin)
