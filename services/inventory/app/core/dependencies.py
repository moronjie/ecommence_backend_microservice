from collections.abc import AsyncIterator
from typing import Annotated

from ecom_common import Forbidden, Unauthorized
from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.inventory_service import InventoryService


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_service(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> InventoryService:
    return InventoryService(session, request.app.state.settings)


def current_user_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    """The gateway verifies the JWT and sets X-User-Id. This service is never exposed directly."""
    if not x_user_id:
        raise Unauthorized("Missing user identity")
    return x_user_id


def is_admin(x_user_role: Annotated[str | None, Header()] = None) -> bool:
    return x_user_role == "admin"


def require_admin(admin: Annotated[bool, Depends(is_admin)]) -> None:
    if not admin:
        raise Forbidden("Administrator access required")


ServiceDep = Annotated[InventoryService, Depends(get_service)]
UserId = Annotated[str, Depends(current_user_id)]
IsAdmin = Annotated[bool, Depends(is_admin)]
AdminOnly = Depends(require_admin)
