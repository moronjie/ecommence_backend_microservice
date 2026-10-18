from typing import Annotated

from ecom_common import success_response
from fastapi import APIRouter, Query

from app.core.dependencies import AdminOnly, CurrentUserId, ServiceDep
from app.schemas.profile import AddressIn, AddressOut, ProfileOut, ProfileUpdate

router = APIRouter(prefix="/api/v1/users", tags=["users"])

Page = Annotated[int, Query(ge=1)]
Size = Annotated[int, Query(ge=1, le=100)]


# NOTE: fixed paths (/me...) are declared before /{user_id} so they are not mistaken for ids.
@router.get("/me")
async def get_me(service: ServiceDep, user_id: CurrentUserId):
    return success_response(ProfileOut.model_validate(await service.get_profile(user_id)))


@router.patch("/me")
async def update_me(body: ProfileUpdate, service: ServiceDep, user_id: CurrentUserId):
    profile = await service.update_profile(user_id, body)
    return success_response(ProfileOut.model_validate(profile), "Profile updated")


@router.get("/me/addresses")
async def list_addresses(service: ServiceDep, user_id: CurrentUserId):
    rows = await service.list_addresses(user_id)
    return success_response({"items": [AddressOut.model_validate(a) for a in rows]})


@router.post("/me/addresses")
async def add_address(body: AddressIn, service: ServiceDep, user_id: CurrentUserId):
    address = await service.add_address(user_id, body)
    return success_response(AddressOut.model_validate(address), "Address added", 201)


@router.put("/me/addresses/{address_id}")
async def update_address(
    address_id: str, body: AddressIn, service: ServiceDep, user_id: CurrentUserId
):
    address = await service.update_address(user_id, address_id, body)
    return success_response(AddressOut.model_validate(address), "Address updated")


@router.delete("/me/addresses/{address_id}")
async def delete_address(address_id: str, service: ServiceDep, user_id: CurrentUserId):
    await service.delete_address(user_id, address_id)
    return success_response(message="Address deleted")


@router.get("", dependencies=[AdminOnly])
async def list_users(service: ServiceDep, page: Page = 1, size: Size = 20):
    result = await service.list_profiles(page, size)
    result["items"] = [ProfileOut.model_validate(p) for p in result["items"]]
    return success_response(result)


@router.get("/{user_id}", dependencies=[AdminOnly])
async def get_user(user_id: str, service: ServiceDep):
    return success_response(ProfileOut.model_validate(await service.get_profile(user_id)))
