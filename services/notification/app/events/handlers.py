import logging
from collections.abc import Callable
from typing import Any

from ecom_common.consumer import Handler
from ecom_common.inbox import first_time
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import templates
from app.core.config import Settings
from app.schemas.notification import OrderEvent, RefundEvent, UserRegistered
from app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)

# Note: there is deliberately no email for payment.failed. A failed payment always leads to
# order.cancelled, and that email already tells the customer why. Two emails would be noise.
TOPICS = [
    "user.registered",
    "order.confirmed",
    "order.cancelled",
    "order.shipped",
    "order.delivered",
    "payment.refunded",
]

# builder: (payload, settings) -> what to queue
Builder = Callable[[Any, Settings], dict[str, Any]]


def _order_email(
    kind: str, render: Callable[[OrderEvent, Settings], templates.Rendered]
) -> Builder:
    def build(p: OrderEvent, s: Settings) -> dict[str, Any]:
        return {
            "kind": kind,
            "dedupe_key": f"{kind}:{p.order_id}",  # one email per order per milestone
            "user_id": p.user_id,
            "to_email": p.email,
            "rendered": render(p, s),
        }

    return build


def _welcome(p: UserRegistered, s: Settings) -> dict[str, Any]:
    return {
        "kind": "welcome",
        "dedupe_key": f"welcome:{p.user_id}",
        "user_id": p.user_id,
        "to_email": p.email,
        "rendered": templates.welcome(p, s),
    }


def _refund(p: RefundEvent, s: Settings) -> dict[str, Any]:
    return {
        "kind": "refund",
        "dedupe_key": f"refund:{p.payment_id}",
        "user_id": p.user_id,
        "to_email": p.email,
        "rendered": templates.refund(p, s),
    }


def build_handlers(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> dict[str, Handler]:
    def handler(payload_type: type[BaseModel], builder: Builder) -> Handler:
        async def handle(event: dict[str, Any]) -> None:
            payload = payload_type.model_validate(event["payload"])  # bad data -> retries -> DLQ
            async with session_factory() as session:
                # The "processed" marker and the queued email commit together, or not at all
                if not await first_time(session, event["event_id"]):
                    return
                await NotificationService(session, settings).queue(**builder(payload, settings))
                await session.commit()

        return handle

    return {
        "user.registered": handler(UserRegistered, _welcome),
        "order.confirmed": handler(
            OrderEvent, _order_email("order_confirmed", templates.order_confirmed)
        ),
        "order.cancelled": handler(
            OrderEvent, _order_email("order_cancelled", templates.order_cancelled)
        ),
        "order.shipped": handler(
            OrderEvent, _order_email("order_shipped", templates.order_shipped)
        ),
        "order.delivered": handler(
            OrderEvent, _order_email("order_delivered", templates.order_delivered)
        ),
        "payment.refunded": handler(RefundEvent, _refund),
    }
