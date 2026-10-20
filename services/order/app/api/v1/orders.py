import uuid
from typing import Annotated, Literal

from ecom_common import Forbidden, success_response
from fastapi import APIRouter, Query

from app.core.dependencies import AdminOnly, IsAdmin, ServiceDep, UserId
from app.schemas.order import OrderDetail, OrderSummary

router = APIRouter(prefix="/api/v1/orders", tags=["orders"])

Status = Literal["pending", "awaiting_payment", "confirmed", "shipped", "delivered", "cancelled"]


@router.get("")
async def list_orders(
    service: ServiceDep,
    user_id: UserId,
    admin: IsAdmin,
    status: Status | None = None,
    owner: Annotated[
        uuid.UUID | None, Query(description="Admins only: one customer's orders")
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """Customers see their own orders. Administrators see everyone's (optionally for one owner)."""
    if owner is not None and owner != user_id and not admin:
        raise Forbidden("Only administrators can list another customer's orders")
    scope = owner if admin else user_id  # None for an admin means "everyone"
    result = await service.list_orders(user_id=scope, status=status, page=page, size=size)
    result["items"] = [OrderSummary.model_validate(o) for o in result["items"]]
    return success_response(result)


@router.get("/{order_id}")
async def get_order(order_id: uuid.UUID, service: ServiceDep, user_id: UserId, admin: IsAdmin):
    order = await service.get(order_id, user_id, admin)
    return success_response(OrderDetail.model_validate(order))


@router.post("/{order_id}/cancel")
async def cancel_order(order_id: uuid.UUID, service: ServiceDep, user_id: UserId, admin: IsAdmin):
    order = await service.cancel(order_id, user_id, admin)
    await service.commit()
    return success_response(OrderDetail.model_validate(order), "Order cancelled")


@router.post("/{order_id}/ship", dependencies=[AdminOnly])
async def ship_order(order_id: uuid.UUID, service: ServiceDep, user_id: UserId):
    order = await service.ship(order_id, user_id)
    await service.commit()
    return success_response(OrderDetail.model_validate(order), "Order shipped")


@router.post("/{order_id}/deliver", dependencies=[AdminOnly])
async def deliver_order(order_id: uuid.UUID, service: ServiceDep, user_id: UserId):
    order = await service.deliver(order_id, user_id)
    await service.commit()
    return success_response(OrderDetail.model_validate(order), "Order delivered")
