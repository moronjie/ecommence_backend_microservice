import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Any

import app.models  # noqa: F401
import httpx
import pytest
from app.core.config import Settings
from app.events.handlers import build_handlers
from app.main import create_app
from app.models.payment import Payment
from app.providers.fake import FAKE_SECRET, FakeProvider
from app.tasks.workers import PaymentWorkers
from ecom_common.consumer import EventDispatcher
from ecom_common.db import Base, create_engine, create_session_factory
from ecom_common.kafka import InMemoryPublisher
from ecom_common.outbox import OutboxRelay
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

KEYS = {"success", "message", "data", "error_message"}
ADA, BOB = str(uuid.uuid4()), str(uuid.uuid4())


def as_user(user_id: str, role: str = "customer") -> dict[str, str]:
    return {"X-User-Id": user_id, "X-User-Role": role}


ADMIN = as_user(str(uuid.uuid4()), "admin")


def sign(body: bytes, secret: str = FAKE_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()


@dataclass
class Env:
    client: httpx.AsyncClient
    session_factory: Any
    settings: Settings
    provider: FakeProvider
    dispatcher: EventDispatcher
    dlq: InMemoryPublisher
    publisher: InMemoryPublisher
    relay: OutboxRelay
    workers: PaymentWorkers

    async def send(self, topic: str, payload: dict, event_id: str | None = None) -> None:
        await self.dispatcher.process(
            topic,
            payload.get("order_id", "k"),
            {
                "event_id": event_id or str(uuid.uuid4()),
                "event_type": topic,
                "version": 1,
                "payload": payload,
            },
        )

    async def request(
        self,
        order_id: str | None = None,
        user: str = ADA,
        total: int = 6500,
        currency: str = "GHS",
        email: str | None = "ada@example.com",
        event_id: str | None = None,
    ) -> str:
        """The order service asks for a payment."""
        order_id = order_id or str(uuid.uuid4())
        payload = {
            "order_id": order_id,
            "user_id": user,
            "total_cents": total,
            "currency": currency,
            "email": email,
        }
        await self.send("order.payment_requested", payload, event_id=event_id)
        return order_id

    async def started(self, **kw: Any) -> tuple[str, str]:
        """Request a payment and let the worker start it. Returns (order_id, reference)."""
        order_id = await self.request(**kw)
        await self.workers.init_once()
        return order_id, (await self.payment(order_id)).reference

    async def payment(self, order_id: str) -> Payment:
        async with self.session_factory() as s:
            return await s.scalar(select(Payment).where(Payment.order_id == uuid.UUID(order_id)))

    async def webhook(self, body: dict, signature: str | None = "valid") -> httpx.Response:
        raw = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if signature:
            headers["x-paystack-signature"] = sign(raw) if signature == "valid" else signature
        return await self.client.post(
            "/api/v1/payments/webhooks/paystack", content=raw, headers=headers
        )

    async def customer_pays(self, reference: str, **kw: Any) -> httpx.Response:
        """The customer pays on the provider's page; the provider calls our webhook."""
        self.provider.simulate(reference, "success", **kw)
        return await self.webhook({"event": "charge.success", "data": {"reference": reference}})

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
    provider, publisher, dlq = FakeProvider(), InMemoryPublisher(), InMemoryPublisher()
    app = create_app(settings, session_factory=factory, provider=provider, publisher=publisher)
    dispatcher = EventDispatcher(
        build_handlers(factory, provider, settings), dlq, max_attempts=2, backoff_seconds=0
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://payment"
    ) as client:
        yield Env(
            client,
            factory,
            settings,
            provider,
            dispatcher,
            dlq,
            publisher,
            OutboxRelay(factory, publisher),
            PaymentWorkers(factory, provider, settings),
        )
    await engine.dispose()
