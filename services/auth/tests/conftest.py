from dataclasses import dataclass

import app.models  # noqa: F401
import httpx
import pytest
from app.core.config import Settings
from app.main import create_app
from ecom_common.db import Base, create_engine, create_session_factory
from ecom_common.kafka import InMemoryPublisher
from ecom_common.outbox import OutboxRelay
from fakeredis import FakeAsyncRedis
from sqlalchemy.pool import StaticPool

SECRET = "test-secret-that-is-long-enough-for-hs256"


@dataclass
class Env:
    client: httpx.AsyncClient
    session_factory: object
    publisher: InMemoryPublisher
    relay: OutboxRelay
    settings: Settings


@pytest.fixture
async def env():
    settings = Settings(jwt_secret=SECRET, database_url="sqlite+aiosqlite://")
    engine = create_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    publisher = InMemoryPublisher()
    app = create_app(
        settings,
        session_factory=factory,
        redis_client=FakeAsyncRedis(decode_responses=True),
        publisher=publisher,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://auth"
    ) as client:
        yield Env(client, factory, publisher, OutboxRelay(factory, publisher), settings)
    await engine.dispose()
