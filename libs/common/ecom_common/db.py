import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, func
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    """UUID primary key plus created_at / updated_at on every table."""

    # Read database-generated values (created_at, the updated_at refresh) back right after each
    # INSERT/UPDATE. Without this they are lazy-loaded on first access, which fails in async code.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


def create_engine(database_url: str, echo: bool = False, **kwargs: Any) -> AsyncEngine:
    return create_async_engine(database_url, echo=echo, pool_pre_ping=True, **kwargs)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def session_dependency(
    factory: async_sessionmaker[AsyncSession],
) -> Callable[[], AsyncIterator[AsyncSession]]:
    """Build a FastAPI dependency: ``Depends(session_dependency(factory))``."""

    async def get_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    return get_session
