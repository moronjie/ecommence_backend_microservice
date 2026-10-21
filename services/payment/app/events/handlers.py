"""Reactions to events from the order service."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ecom_common.consumer import Handler
from ecom_common.inbox import first_time
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.providers.base import PaymentProvider
from app.schemas.payment import PaymentRequested, RefundRequested
from app.services.payment_service import PaymentService

logger = logging.getLogger(__name__)

TOPICS = ["order.payment_requested", "order.refund_requested"]


def build_handlers(
    session_factory: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    settings: Settings,
) -> dict[str, Handler]:
    def handler[P: BaseModel](
        payload_type: type[P], action: Callable[[PaymentService, P], Awaitable[Any]]
    ) -> Handler:
        async def handle(event: dict[str, Any]) -> None:
            payload = payload_type.model_validate(event["payload"])  # bad data -> retries -> DLQ
            async with session_factory() as session:
                # The "processed" marker and our changes commit together, or not at all
                if not await first_time(session, event["event_id"]):
                    return
                await action(PaymentService(session, provider, settings), payload)
                await session.commit()

        return handle

    async def payment_requested(svc: PaymentService, p: PaymentRequested) -> None:
        await svc.request_payment(p)

    async def refund_requested(svc: PaymentService, p: RefundRequested) -> None:
        await svc.request_refund(p.order_id, p.reason)

    return {
        "order.payment_requested": handler(PaymentRequested, payment_requested),
        "order.refund_requested": handler(RefundRequested, refund_requested),
    }
