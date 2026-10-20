import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ecom_common.consumer import Handler
from ecom_common.inbox import first_time
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.schemas.order import CheckoutPayload, OrderRefPayload
from app.services.order_service import OrderService

logger = logging.getLogger(__name__)

TOPICS = [
    "cart.checked_out",
    "inventory.reserved",
    "inventory.failed",
    "inventory.released",
    "payment.completed",
    "payment.failed",
]


def build_handlers(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> dict[str, Handler]:
    def handler[P: BaseModel](
        payload_type: type[P], action: Callable[[OrderService, P], Awaitable[Any]]
    ) -> Handler:
        async def handle(event: dict[str, Any]) -> None:
            payload = payload_type.model_validate(event["payload"])  # bad data -> retries -> DLQ
            async with session_factory() as session:
                # The "processed" marker and our changes commit together, or not at all
                if not await first_time(session, event["event_id"]):
                    return
                await action(OrderService(session, settings), payload)
                await session.commit()

        return handle

    async def checked_out(svc: OrderService, p: CheckoutPayload) -> None:
        await svc.create_from_checkout(p)

    async def stock_reserved(svc: OrderService, p: OrderRefPayload) -> None:
        await svc.on_stock_reserved(p.order_id)

    async def stock_failed(svc: OrderService, p: OrderRefPayload) -> None:
        await svc.on_stock_failed(p.order_id, p.reason)

    async def hold_released(svc: OrderService, p: OrderRefPayload) -> None:
        await svc.on_hold_released(p.order_id, p.reason)

    async def payment_completed(svc: OrderService, p: OrderRefPayload) -> None:
        await svc.on_payment_completed(p.order_id)

    async def payment_failed(svc: OrderService, p: OrderRefPayload) -> None:
        await svc.on_payment_failed(p.order_id, p.reason)

    return {
        "cart.checked_out": handler(CheckoutPayload, checked_out),
        "inventory.reserved": handler(OrderRefPayload, stock_reserved),
        "inventory.failed": handler(OrderRefPayload, stock_failed),
        "inventory.released": handler(OrderRefPayload, hold_released),
        "payment.completed": handler(OrderRefPayload, payment_completed),
        "payment.failed": handler(OrderRefPayload, payment_failed),
    }
