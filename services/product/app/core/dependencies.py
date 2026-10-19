from collections.abc import AsyncIterator
from typing import Annotated

from ecom_common import Forbidden
from ecom_common.cache import Cache
from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.catalogue_service import CatalogueService


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_catalogue_service(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> CatalogueService:
    state = request.app.state
    return CatalogueService(session, Cache(state.redis, prefix="product"), state.settings)


def is_admin(x_user_role: Annotated[str | None, Header()] = None) -> bool:
    """The gateway sets X-User-Role from the verified token (and strips it from clients)."""
    return x_user_role == "admin"


def require_admin(admin: Annotated[bool, Depends(is_admin)]) -> None:
    if not admin:
        raise Forbidden("Administrator access required")


ServiceDep = Annotated[CatalogueService, Depends(get_catalogue_service)]
IsAdmin = Annotated[bool, Depends(is_admin)]
AdminOnly = Depends(require_admin)
