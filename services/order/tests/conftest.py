import uuid
from dataclasses import dataclass
from typing import Any

import app.models  # noqa: F401
import httpx
import pytest
from app.core.config import Settings
from app.events.handlers import build_handlers
from app.main import create_app
from app.models.order import Order
from app.tasks.sweeper import StaleOrderSweeper
from ecom_common.consumer import EventDispatcher
from ecom_common.db import Base, create_engine, create_session_factory
from ecom_common.kafka import InMemoryPublisher
from ecom_common.outbox import OutboxRelay
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

KEYS = {"success", "message", "data", "error_message"}
ADA, BOB = str(uuid.uuid4()), str(uuid.uuid4())
ADDRESS = {
    "recipient": "Ada Lovelace",
    "line1": "12 Analytical St",
    "city": "London",
    "postal_code": "N1 9GU",
    "country": "GB",
}


def as_user(user_id: str, role: str = "customer") -> dict[str, str]:
    return {"X-User-Id": user_id, "X-User-Role": role}


ADMIN = as_user(str(uuid.uuid4()), "admin")


@dataclass
class Env:
    client: httpx.AsyncClient
    session_factory: Any
    settings: Settings
    dispatcher: EventDispatcher
    dlq: InMemoryPublisher
    publisher: InMemoryPublisher
    relay: OutboxRelay
    sweeper: StaleOrderSweeper

    async def send(self, topic: str, payload: dict, event_id: str | None = None) -> None:
        """Deliver an event the way Kafka would."""
        await self.dispatcher.process(
            topic,
            payload.get("order_id") or payload.get("user_id") or "k",
            {
                "event_id": event_id or str(uuid.uuid4()),
                "event_type": topic,
                "version": 1,
                "payload": payload,
            },
        )

    def checkout_payload(
        self,
        user: str = ADA,
        items: list[tuple[str, int, int]] | None = None,
        checkout_id: str | None = None,
        **extra: Any,
    ) -> dict:
        items = items or [("Mug", 2, 1000), ("Pan", 1, 4500)]
        lines = [
            {
                "product_id": str(uuid.uuid4()),
                "sku": name.upper(),
                "name": name,
                "quantity": qty,
                "unit_price_cents": price,
            }
            for name, qty, price in items
        ]
        return {
            "checkout_id": checkout_id or str(uuid.uuid4()),
            "user_id": user,
            "email": "ada@example.com",
            "address_id": str(uuid.uuid4()),
            "shipping_address": ADDRESS,
            "currency": "USD",
            "total_cents": sum(q * p for _, q, p in items),
            "items": lines,
            **extra,
        }

    async def checkout(self, event_id: str | None = None, **kw: Any) -> str:
        """The cart checks out. Returns the new order's id."""
        payload = self.checkout_payload(**kw)
        await self.send("cart.checked_out", payload, event_id=event_id)
        async with self.session_factory() as s:
            order = await s.scalar(
                select(Order).where(Order.checkout_id == uuid.UUID(payload["checkout_id"]))
            )
            return str(order.id)

    async def reserved(self, order_id: str) -> str:
        await self.send("inventory.reserved", {"order_id": order_id})
        return order_id

    async def paid(self, order_id: str) -> str:
        await self.send("payment.completed", {"order_id": order_id})
        return order_id

    async def confirmed_order(self, **kw: Any) -> str:
        return await self.paid(await self.reserved(await self.checkout(**kw)))

    async def status(self, order_id: str) -> str:
        async with self.session_factory() as s:
            return (await s.get(Order, uuid.UUID(order_id))).status

    async def published(self, topic: str) -> list[dict]:
        await self.relay.publish_pending()
        return [e["payload"] for t, _, e in self.publisher.events if t == topic]


def uniform(resp: httpx.Response, status: int, success: bool) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == KEYS and body["success"] is success
    return body


@pytest.fixture
async def env():
    engine = create_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    settings = Settings(database_url="sqlite+aiosqlite://")
    publisher, dlq = InMemoryPublisher(), InMemoryPublisher()
    app = create_app(settings, session_factory=factory, publisher=publisher)
    dispatcher = EventDispatcher(
        build_handlers(factory, settings), dlq, max_attempts=2, backoff_seconds=0
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://order"
    ) as client:
        yield Env(
            client,
            factory,
            settings,
            dispatcher,
            dlq,
            publisher,
            OutboxRelay(factory, publisher),
            StaleOrderSweeper(factory, settings),
        )
    await engine.dispose()
