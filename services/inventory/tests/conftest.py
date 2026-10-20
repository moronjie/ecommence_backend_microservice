import uuid
from dataclasses import dataclass
from typing import Any

import app.models  # noqa: F401
import httpx
import pytest
from app.core.config import Settings
from app.events.handlers import build_handlers
from app.main import create_app
from app.models.inventory import Reservation, StockItem
from app.tasks.expiry import ExpiryLoop
from ecom_common.consumer import EventDispatcher
from ecom_common.db import Base, create_engine, create_session_factory
from ecom_common.kafka import InMemoryPublisher
from ecom_common.outbox import OutboxRelay
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

ADMIN = {"X-User-Id": "admin-1", "X-User-Role": "admin"}
CUSTOMER = {"X-User-Id": "cust-1", "X-User-Role": "customer"}
KEYS = {"success", "message", "data", "error_message"}


@dataclass
class Env:
    client: httpx.AsyncClient
    session_factory: Any
    settings: Settings
    dispatcher: EventDispatcher
    dlq: InMemoryPublisher
    publisher: InMemoryPublisher
    relay: OutboxRelay
    expiry: ExpiryLoop

    async def send(self, topic: str, payload: dict, event_id: str | None = None) -> None:
        """Deliver an event the way Kafka would."""
        await self.dispatcher.process(
            topic,
            payload.get("order_id") or payload.get("product_id") or "k",
            {
                "event_id": event_id or str(uuid.uuid4()),
                "event_type": topic,
                "version": 1,
                "payload": payload,
            },
        )

    async def product(self, on_hand: int = 0, sku: str = "SKU-1") -> str:
        product_id = str(uuid.uuid4())
        await self.send("product.created", {"product_id": product_id, "sku": sku})
        if on_hand:
            resp = await self.client.put(
                f"/api/v1/inventory/{product_id}", json={"on_hand": on_hand}, headers=ADMIN
            )
            assert resp.status_code == 200, resp.text
        return product_id

    async def order(self, items: list[tuple[str, int]], order_id: str | None = None, **kw) -> str:
        order_id = order_id or str(uuid.uuid4())
        lines = [{"product_id": p, "quantity": q} for p, q in items]
        await self.send("order.created", {"order_id": order_id, "items": lines}, **kw)
        return order_id

    async def stock(self, product_id: str) -> tuple[int, int]:
        async with self.session_factory() as s:
            item = await s.get(StockItem, uuid.UUID(product_id))
            return item.on_hand, item.reserved

    async def reservation(self, order_id: str) -> Reservation | None:
        async with self.session_factory() as s:
            return await s.scalar(
                select(Reservation).where(Reservation.order_id == uuid.UUID(order_id))
            )

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
        transport=httpx.ASGITransport(app=app), base_url="http://inventory"
    ) as client:
        yield Env(
            client,
            factory,
            settings,
            dispatcher,
            dlq,
            publisher,
            OutboxRelay(factory, publisher),
            ExpiryLoop(factory, settings),
        )
    await engine.dispose()
