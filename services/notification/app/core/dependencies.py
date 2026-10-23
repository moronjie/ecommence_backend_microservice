import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from ecom_common import Forbidden, Unauthorized
from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.notification_service import NotificationService


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_service(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> NotificationService:
    return NotificationService(session, request.app.state.settings)


def current_user_id(x_user_id: Annotated[str | None, Header()] = None) -> uuid.UUID:
    """The gateway verifies the JWT and sets X-User-Id. This service is never exposed directly."""
    try:
        return uuid.UUID(x_user_id or "")
    except ValueError as exc:
        raise Unauthorized("Missing or invalid user identity") from exc


def is_admin(x_user_role: Annotated[str | None, Header()] = None) -> bool:
    return x_user_role == "admin"


def require_admin(admin: Annotated[bool, Depends(is_admin)]) -> None:
    if not admin:
        raise Forbidden("Administrator access required")


ServiceDep = Annotated[NotificationService, Depends(get_service)]
UserId = Annotated[uuid.UUID, Depends(current_user_id)]
IsAdmin = Annotated[bool, Depends(is_admin)]
AdminOnly = Depends(require_admin)
