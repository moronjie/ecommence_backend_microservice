import pytest
from ecom_common.db import Base, create_engine, create_session_factory
from ecom_common.kafka import InMemoryPublisher
from ecom_common.outbox import OutboxEvent, OutboxRelay, add_event
from ecom_common.security import hash_password, verify_password
from sqlalchemy import select
from sqlalchemy.pool import StaticPool


@pytest.fixture
async def session_factory():
    engine = create_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


async def test_event_only_published_if_transaction_commits(session_factory):
    publisher = InMemoryPublisher()
    relay = OutboxRelay(session_factory, publisher)

    async with session_factory() as s:  # rolled back: nothing may be sent
        add_event(s, "user.registered", {"id": 1}, key="1")
        await s.rollback()
    async with session_factory() as s:
        add_event(s, "user.registered", {"id": 2}, key="2")
        await s.commit()

    assert await relay.publish_pending() == 1
    topic, key, event = publisher.events[0]
    assert (topic, key) == ("user.registered", "2")
    assert event["payload"] == {"id": 2} and event["version"] == 1 and event["event_id"]
    assert await relay.publish_pending() == 0  # already sent, not repeated


async def test_failed_publish_stays_pending_and_is_retried(session_factory):
    class Flaky(InMemoryPublisher):
        fail = True

        async def publish(self, topic, key, event):
            if self.fail:
                raise ConnectionError("kafka down")
            await super().publish(topic, key, event)

    publisher = Flaky()
    relay = OutboxRelay(session_factory, publisher)
    async with session_factory() as s:
        add_event(s, "order.created", {}, key="o1")
        await s.commit()

    assert await relay.publish_pending() == 0
    publisher.fail = False
    assert await relay.publish_pending() == 1
    async with session_factory() as s:
        row = (await s.scalars(select(OutboxEvent))).one()
        assert row.published_at is not None


def test_password_hashing():
    h = hash_password("correct horse")
    assert h != "correct horse" and verify_password("correct horse", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", "not-a-hash")
