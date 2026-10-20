import uuid
from typing import Annotated

from ecom_common import success_response
from ecom_common.responses import paginated
from fastapi import APIRouter, Query

from app.core.dependencies import AdminOnly, IsAdmin, ServiceDep, UserId
from app.schemas.inventory import (
    AdjustStock,
    AvailabilityOut,
    AvailabilityRequest,
    SetStock,
    StockOut,
)

router = APIRouter(prefix="/api/v1/inventory", tags=["inventory"])


def _public(item) -> AvailabilityOut:
    return AvailabilityOut(
        product_id=item.product_id,
        sku=item.sku,
        available=item.available,
        in_stock=item.available > 0,
    )


@router.post("/availability")
async def availability(body: AvailabilityRequest, service: ServiceDep, _: UserId):
    """Stock check for many products at once (for the cart). Unknown products count as 0."""
    found = await service.availability(body.product_ids)
    items = []
    for product_id in dict.fromkeys(body.product_ids):  # keep order, drop duplicates
        if product_id in found:
            items.append(_public(found[product_id]))
        else:
            items.append(
                AvailabilityOut(product_id=product_id, sku="", available=0, in_stock=False)
            )
    return success_response({"items": items})


@router.get("", dependencies=[AdminOnly])
async def list_stock(
    service: ServiceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    max_available: Annotated[int | None, Query(ge=0, description="Only low stock")] = None,
):
    rows, total = await service.list_items(page, size, max_available)
    items = [StockOut.model_validate(r) for r in rows]
    return success_response(paginated(items, total, page, size))


@router.get("/{product_id}")
async def get_stock(product_id: uuid.UUID, service: ServiceDep, admin: IsAdmin, _: UserId):
    item = await service.get_item(product_id)
    view = StockOut.model_validate(item) if admin else _public(item)
    return success_response(view)


@router.put("/{product_id}", dependencies=[AdminOnly])
async def set_stock(product_id: uuid.UUID, body: SetStock, service: ServiceDep, actor: UserId):
    item = await service.set_on_hand(product_id, body.on_hand, actor)
    await service.commit()
    return success_response(StockOut.model_validate(item), "Stock set")


@router.post("/{product_id}/adjust", dependencies=[AdminOnly])
async def adjust_stock(
    product_id: uuid.UUID, body: AdjustStock, service: ServiceDep, actor: UserId
):
    item = await service.adjust(product_id, body.delta, body.reason, actor)
    await service.commit()
    return success_response(StockOut.model_validate(item), "Stock adjusted")
