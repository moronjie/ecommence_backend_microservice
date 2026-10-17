import json

import pytest
from ecom_common.consumer import EventDispatcher
from ecom_common.db import Base, create_engine, create_session_factory
from ecom_common.inbox import first_time
from ecom_common.kafka import InMemoryPublisher
from sqlalchemy.pool import StaticPool


def event(event_type="user.registered", event_id="e1", **payload):
    return {"event_id": event_id, "event_type": event_type, "version": 1, "payload": payload}


def make(handlers, **kw):
    dlq = InMemoryPublisher()
    return EventDispatcher(handlers, dlq, backoff_seconds=0, **kw), dlq


async def test_routes_to_handler_by_event_type():
    seen = []

    async def on_registered(e):
        seen.append(e["event_id"])

    d, dlq = make({"user.registered": on_registered})
    await d.process("user.registered", "k", event())
    await d.process("user.registered", "k", json.dumps(event(event_id="e2")).encode())  # raw bytes
    await d.process("other", "k", event("something.else", "e3"))  # no handler: ignored
    assert seen == ["e1", "e2"] and dlq.events == []


async def test_transient_failure_is_retried_without_dlq():
    calls = []

    async def flaky(e):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("db hiccup")

    d, dlq = make({"user.registered": flaky}, max_attempts=3)
    await d.process("user.registered", "k", event())
    assert len(calls) == 3 and dlq.events == []


async def test_permanent_failure_goes_to_dlq_and_does_not_raise():
    async def broken(e):
        raise ValueError("bad data")

    d, dlq = make({"user.registered": broken}, max_attempts=2)
    await d.process("user.registered", "user-1", event())
    topic, key, parked = dlq.events[0]
    assert topic == "user.registered.dlq" and key == "user-1"
    assert "bad data" in parked["reason"] and parked["original"]["event_id"] == "e1"


@pytest.mark.parametrize("raw", [b"not json", b'{"no_type": 1}', b"[]"])
async def test_malformed_messages_are_dead_lettered(raw):
    d, dlq = make({"user.registered": lambda e: None})
    await d.process("user.registered", "k", raw)
    assert dlq.events[0][0] == "user.registered.dlq"
    assert "malformed" in dlq.events[0][2]["reason"]


async def test_first_time_claims_each_event_id_once():
    engine = create_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as s:
        assert await first_time(s, "a") is True
        assert await first_time(s, "a") is False  # same transaction
        await s.commit()
    async with factory() as s:
        assert await first_time(s, "a") is False  # after commit
        assert await first_time(s, "b") is True
    await engine.dispose()
