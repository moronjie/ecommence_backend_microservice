"""Business rules for profiles and addresses. Every call is scoped to one user id."""

import uuid

from ecom_common import Conflict, NotFound, ValidationFailed
from ecom_common.responses import paginated
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import Address, Profile
from app.repositories.profile_repository import ProfileRepository
from app.schemas.profile import AddressIn, ProfileUpdate


def _uid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise NotFound("No such user", message="Not found") from exc


class ProfileService:
    def __init__(self, session: AsyncSession, max_addresses: int = 10) -> None:
        self.session = session
        self.repo = ProfileRepository(session)
        self.max_addresses = max_addresses

    # ---- profile ------------------------------------------------------------------------
    async def get_profile(self, user_id: str) -> Profile:
        profile = await self.repo.get_profile(_uid(user_id))
        if profile is None:
            # The profile is created from the user.registered event, which arrives a moment
            # after sign-up. Telling the client to retry is the honest answer.
            raise NotFound(
                "Your profile is still being set up, try again in a moment",
                message="Profile not ready",
            )
        return profile

    async def update_profile(self, user_id: str, data: ProfileUpdate) -> Profile:
        profile = await self.get_profile(user_id)
        for field, value in data.model_dump(exclude_unset=True).items():
            if value is not None:
                setattr(profile, field, value.strip())
        await self.session.commit()
        return profile

    async def list_profiles(self, page: int, size: int) -> dict:
        rows, total = await self.repo.list_profiles((page - 1) * size, size)
        return paginated(rows, total, page, size)

    # ---- addresses ----------------------------------------------------------------------
    async def list_addresses(self, user_id: str) -> list[Address]:
        await self.get_profile(user_id)
        return await self.repo.list_addresses(_uid(user_id))

    async def _get_address(self, user_id: str, address_id: str) -> Address:
        try:
            aid = uuid.UUID(address_id)
        except ValueError:
            aid = None
        address = await self.repo.get_address(_uid(user_id), aid) if aid else None
        if address is None:
            raise NotFound("Address not found", message="Not found")
        return address

    async def _clear_default(self, user_id: uuid.UUID, except_id: uuid.UUID | None = None) -> None:
        for other in await self.repo.list_addresses(user_id):
            if other.is_default and other.id != except_id:
                other.is_default = False

    async def add_address(self, user_id: str, data: AddressIn) -> Address:
        await self.get_profile(user_id)
        uid = _uid(user_id)
        existing = await self.repo.list_addresses(uid)
        if len(existing) >= self.max_addresses:
            raise Conflict(f"You can save at most {self.max_addresses} addresses")

        address = Address(user_id=uid, **self._clean(data))
        address.is_default = data.is_default or not existing  # the first one is the default
        if address.is_default:
            await self._clear_default(uid)
        self.session.add(address)
        await self.session.commit()
        return address

    async def update_address(self, user_id: str, address_id: str, data: AddressIn) -> Address:
        address = await self._get_address(user_id, address_id)
        values = self._clean(data)
        was_default = address.is_default
        values["is_default"] = data.is_default or was_default  # can't unset by update
        for field, value in values.items():
            setattr(address, field, value)
        if address.is_default:
            await self._clear_default(address.user_id, except_id=address.id)
        await self.session.commit()
        return address

    async def delete_address(self, user_id: str, address_id: str) -> None:
        address = await self._get_address(user_id, address_id)
        was_default, uid = address.is_default, address.user_id
        await self.session.delete(address)
        await self.session.flush()
        if was_default:  # never leave a user with addresses but no default
            remaining = await self.repo.list_addresses(uid)
            if remaining:
                remaining[0].is_default = True
        await self.session.commit()

    @staticmethod
    def _clean(data: AddressIn) -> dict:
        values = data.model_dump()
        values["country"] = values["country"].upper()
        for key, value in values.items():
            if isinstance(value, str):
                values[key] = value.strip()
        if not values["recipient"] or not values["line1"]:
            raise ValidationFailed("Recipient and address line 1 cannot be blank")
        return values
