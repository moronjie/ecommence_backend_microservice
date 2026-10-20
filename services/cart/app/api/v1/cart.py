import uuid

from ecom_common import success_response
from fastapi import APIRouter

from app.core.dependencies import ServiceDep, UserId
from app.schemas.cart import AddItem, CheckoutRequest, SetQuantity

router = APIRouter(prefix="/api/v1/cart", tags=["cart"])


@router.get("")
async def view_cart(service: ServiceDep, user_id: UserId):
    return success_response(await service.view(user_id))


@router.post("/items")
async def add_item(body: AddItem, service: ServiceDep, user_id: UserId):
    await service.add_item(user_id, body.product_id, body.quantity)
    return success_response(await service.view(user_id), "Item added")


@router.put("/items/{product_id}")
async def set_quantity(
    product_id: uuid.UUID, body: SetQuantity, service: ServiceDep, user_id: UserId
):
    await service.set_quantity(user_id, product_id, body.quantity)
    return success_response(await service.view(user_id), "Quantity updated")


@router.delete("/items/{product_id}")
async def remove_item(product_id: uuid.UUID, service: ServiceDep, user_id: UserId):
    await service.remove_item(user_id, product_id)
    return success_response(await service.view(user_id), "Item removed")


@router.delete("")
async def clear_cart(service: ServiceDep, user_id: UserId):
    await service.clear(user_id)
    return success_response(message="Cart cleared")


@router.post("/checkout")
async def checkout(body: CheckoutRequest, service: ServiceDep, user_id: UserId):
    result = await service.checkout(user_id, body.address_id, body.expected_total_cents)
    return success_response(result, "Checkout started", 202)
