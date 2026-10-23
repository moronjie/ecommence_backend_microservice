import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import app.models  # noqa: F401
import httpx
import pytest
from app.core.config import Settings
from app.events.handlers import build_handlers
from app.main import create_app
from app.models.notification import Notification
from app.senders.memory import MemorySender
from app.tasks.worker import EmailWorker
from ecom_common.consumer import EventDispatcher
from ecom_common.db import Base, create_engine, create_session_factory
from ecom_common.kafka import InMemoryPublisher
from sqlalchemy import select, update
from sqlalchemy.pool import StaticPool

KEYS = {"success", "message", "data", "error_message"}
ADA, BOB = str(uuid.uuid4()), str(uuid.uuid4())


def as_user(user_id: str, role: str = "customer") -> dict[str, str]:
    return {"X-User-Id": user_id, "X-User-Role": role}


ADMIN = as_user(str(uuid.uuid4()), "admin")


@dataclass
class Env:
    client: httpx.AsyncClient
    session_factory: Any
    settings: Settings
    sender: MemorySender
    dispatcher: EventDispatcher
    dlq: InMemoryPublisher
    worker: EmailWorker

    async def send(self, topic: str, payload: dict, event_id: str | None = None) -> None:
        """Deliver an event the way Kafka would."""
        await self.dispatcher.process(
            topic,
            "k",
            {
                "event_id": event_id or str(uuid.uuid4()),
                "event_type": topic,
                "version": 1,
                "payload": payload,
            },
        )

    def order_payload(self, order_id: str | None = None, user: str = ADA, **extra: Any) -> dict:
        return {
            "order_id": order_id or str(uuid.uuid4()),
            "user_id": user,
            "email": "ada@example.com",
            "currency": "GHS",
            "total_cents": 24000,
            **extra,
        }

    async def confirmed(self, **kw: Any) -> dict:
        payload = self.order_payload(
            items=[{"name": "Kente Scarf", "quantity": 2, "unit_price_cents": 12000}], **kw
        )
        await self.send("order.confirmed", payload)
        return payload

    async def notes(self) -> list[Notification]:
        async with self.session_factory() as s:
            return list(await s.scalars(select(Notification).order_by(Notification.created_at)))

    async def only_note(self) -> Notification:
        (note,) = await self.notes()
        return note

    async def make_due(self) -> None:
        """Pretend time has passed: every queued email is now due for its next attempt."""
        async with self.session_factory() as s:
            await s.execute(
                update(Notification)
                .where(Notification.status == "queued")
                .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await s.commit()


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
    settings = Settings(database_url="sqlite+aiosqlite://", shop_name="Accra Crafts")
    sender, dlq = MemorySender(), InMemoryPublisher()
    app = create_app(settings, session_factory=factory, sender=sender)
    dispatcher = EventDispatcher(
        build_handlers(factory, settings), dlq, max_attempts=2, backoff_seconds=0
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://notification"
    ) as client:
        yield Env(
            client,
            factory,
            settings,
            sender,
            dispatcher,
            dlq,
            EmailWorker(factory, sender, settings),
        )
    await engine.dispose()
