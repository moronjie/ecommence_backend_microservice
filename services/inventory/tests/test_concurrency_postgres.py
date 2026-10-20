import asyncio
import os
import uuid

import app.models  # noqa: F401
import pytest
from app.core.config import Settings
from app.events.handlers import build_handlers
from app.models.inventory import Reservation, StockItem
from ecom_common.consumer import EventDispatcher
from ecom_common.db import Base, create_engine, create_session_factory
from ecom_common.kafka import InMemoryPublisher
from sqlalchemy import func, select

URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set TEST_POSTGRES_URL to run against Postgres")


@pytest.fixture
async def pg():
    engine = create_engine(URL, pool_size=30, max_overflow=0)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    dlq = InMemoryPublisher()
    dispatcher = EventDispatcher(build_handlers(factory, Settings()), dlq, backoff_seconds=0)
    yield factory, dispatcher, dlq
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def make_product(factory, on_hand: int) -> uuid.UUID:
    product_id = uuid.uuid4()
    async with factory() as s:
        s.add(StockItem(product_id=product_id, sku="X", on_hand=on_hand, reserved=0))
        await s.commit()
    return product_id


def order_event(items: list[tuple[uuid.UUID, int]]) -> dict:
    order_id = str(uuid.uuid4())
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "order.created",
        "payload": {
            "order_id": order_id,
            "items": [{"product_id": str(p), "quantity": q} for p, q in items],
        },
    }


async def test_concurrent_orders_never_oversell(pg):
    factory, dispatcher, dlq = pg
    product_id = await make_product(factory, on_hand=5)

    events = [order_event([(product_id, 1)]) for _ in range(25)]
    await asyncio.gather(*(dispatcher.process("order.created", "k", e) for e in events))

    async with factory() as s:
        item = await s.get(StockItem, product_id)
        statuses = list(await s.scalars(select(Reservation.status)))
    assert statuses.count("reserved") == 5 and statuses.count("failed") == 20
    assert item.reserved == 5 and item.on_hand == 5  # promised exactly what exists
    assert dlq.events == []


async def test_opposite_lock_order_does_not_deadlock(pg):
    factory, dispatcher, dlq = pg
    a, b = await make_product(factory, 50), await make_product(factory, 50)

    events = [order_event([(a, 1), (b, 1)] if i % 2 else [(b, 1), (a, 1)]) for i in range(40)]
    await asyncio.wait_for(
        asyncio.gather(*(dispatcher.process("order.created", "k", e) for e in events)), timeout=30
    )

    async with factory() as s:
        reserved = await s.scalar(select(func.sum(StockItem.reserved)))
    assert reserved == 80 and dlq.events == []
