"""Reactions to events from the product and order services."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ecom_common.consumer import Handler
from ecom_common.inbox import first_time
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.schemas.inventory import OrderCreatedPayload, OrderRefPayload, ProductPayload
from app.services.inventory_service import InventoryService, Line

logger = logging.getLogger(__name__)

TOPICS = [
    "product.created",
    "product.updated",
    "order.created",
    "order.confirmed",
    "order.cancelled",
]


def build_handlers(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> dict[str, Handler]:
    def handler[P: BaseModel](
        payload_type: type[P], action: Callable[[InventoryService, P], Awaitable[Any]]
    ) -> Handler:
        async def handle(event: dict[str, Any]) -> None:
            payload = payload_type.model_validate(event["payload"])  # bad data -> retries -> DLQ
            async with session_factory() as session:
                # The "processed" marker and our changes commit together, or not at all
                if not await first_time(session, event["event_id"]):
                    return
                await action(InventoryService(session, settings), payload)
                await session.commit()

        return handle

    async def product_changed(svc: InventoryService, p: ProductPayload) -> None:
        await svc.ensure_item(p.product_id, p.sku)

    async def order_created(svc: InventoryService, p: OrderCreatedPayload) -> None:
        await svc.reserve(p.order_id, [Line(i.product_id, i.quantity) for i in p.items])

    async def order_confirmed(svc: InventoryService, p: OrderRefPayload) -> None:
        await svc.confirm(p.order_id)

    async def order_cancelled(svc: InventoryService, p: OrderRefPayload) -> None:
        await svc.release(p.order_id, "cancelled")

    return {
        "product.created": handler(ProductPayload, product_changed),
        "product.updated": handler(ProductPayload, product_changed),
        "order.created": handler(OrderCreatedPayload, order_created),
        "order.confirmed": handler(OrderRefPayload, order_confirmed),
        "order.cancelled": handler(OrderRefPayload, order_cancelled),
    }
