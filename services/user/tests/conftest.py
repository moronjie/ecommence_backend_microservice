import uuid
from dataclasses import dataclass

import app.models  # noqa: F401
import httpx
import pytest
from app.core.config import Settings
from app.events.handlers import build_handlers
from app.main import create_app
from ecom_common.consumer import EventDispatcher
from ecom_common.db import Base, create_engine, create_session_factory
from ecom_common.kafka import InMemoryPublisher
from sqlalchemy.pool import StaticPool


@dataclass
class Env:
    client: httpx.AsyncClient
    session_factory: object
    dispatcher: EventDispatcher
    dlq: InMemoryPublisher

    async def register_user(self, email="ada@example.com", full_name="Ada", event_id=None) -> str:
        """Simulate the user.registered event from the auth service arriving."""
        user_id = str(uuid.uuid4())
        await self.send_registered(user_id, email, full_name, event_id)
        return user_id

    async def send_registered(
        self, user_id, email="ada@example.com", full_name="Ada", event_id=None
    ):
        await self.dispatcher.process(
            "user.registered",
            user_id,
            {
                "event_id": event_id or str(uuid.uuid4()),
                "event_type": "user.registered",
                "version": 1,
                "payload": {"user_id": user_id, "email": email, "full_name": full_name},
            },
        )


def as_user(user_id: str, role: str = "customer") -> dict[str, str]:
    return {"X-User-Id": user_id, "X-User-Role": role}


@pytest.fixture
async def env():
    engine = create_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    app = create_app(Settings(database_url="sqlite+aiosqlite://"), session_factory=factory)
    dlq = InMemoryPublisher()
    dispatcher = EventDispatcher(build_handlers(factory), dlq, backoff_seconds=0)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://user"
    ) as client:
        yield Env(client, factory, dispatcher, dlq)
    await engine.dispose()
