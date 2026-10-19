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

ADMIN = {"X-User-Id": "admin-1", "X-User-Role": "admin"}
CUSTOMER = {"X-User-Id": "cust-1", "X-User-Role": "customer"}
KEYS = {"success", "message", "data", "error_message"}


@dataclass
class Env:
    client: httpx.AsyncClient
    redis: FakeAsyncRedis
    publisher: InMemoryPublisher
    relay: OutboxRelay

    async def category(self, name="Books", **kw) -> dict:
        resp = await self.client.post(
            "/api/v1/categories", json={"name": name, **kw}, headers=ADMIN
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["data"]

    async def product(self, sku="SKU-1", name="Blue Mug", price_cents=1999, **kw) -> dict:
        body = {"sku": sku, "name": name, "price_cents": price_cents, **kw}
        resp = await self.client.post("/api/v1/products", json=body, headers=ADMIN)
        assert resp.status_code == 201, resp.text
        return resp.json()["data"]


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
    redis = FakeAsyncRedis(decode_responses=True)
    publisher = InMemoryPublisher()
    app = create_app(
        Settings(database_url="sqlite+aiosqlite://"),
        session_factory=factory,
        redis_client=redis,
        publisher=publisher,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://product"
    ) as client:
        yield Env(client, redis, publisher, OutboxRelay(factory, publisher))
    await engine.dispose()
