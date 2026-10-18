import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import Address, Profile


class ProfileRepository:
    """Database access only. No business rules here."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_profile(self, user_id: uuid.UUID) -> Profile | None:
        return await self.session.get(Profile, user_id)

    async def list_profiles(self, offset: int, limit: int) -> tuple[list[Profile], int]:
        total = await self.session.scalar(select(func.count()).select_from(Profile)) or 0
        rows = await self.session.scalars(
            select(Profile)
            .order_by(Profile.created_at, Profile.user_id)
            .offset(offset)
            .limit(limit)
        )
        return list(rows), total

    async def list_addresses(self, user_id: uuid.UUID) -> list[Address]:
        rows = await self.session.scalars(
            select(Address)
            .where(Address.user_id == user_id)
            .order_by(Address.is_default.desc(), Address.created_at, Address.id)
        )
        return list(rows)

    async def get_address(self, user_id: uuid.UUID, address_id: uuid.UUID) -> Address | None:
        return await self.session.scalar(
            select(Address).where(Address.id == address_id, Address.user_id == user_id)
        )
