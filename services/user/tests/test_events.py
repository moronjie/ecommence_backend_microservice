import uuid

from app.models.profile import Profile
from sqlalchemy import func, select


async def count_profiles(env) -> int:
    async with env.session_factory() as s:
        return await s.scalar(select(func.count()).select_from(Profile))


async def test_user_registered_creates_profile(env):
    user_id = await env.register_user("ada@example.com", "Ada Lovelace")
    async with env.session_factory() as s:
        profile = await s.get(Profile, uuid.UUID(user_id))
    assert profile.email == "ada@example.com" and profile.full_name == "Ada Lovelace"
    assert env.dlq.events == []


async def test_same_event_delivered_twice_creates_one_profile(env):
    user_id = str(uuid.uuid4())
    await env.send_registered(user_id, event_id="evt-1")
    await env.send_registered(user_id, event_id="evt-1")  # Kafka redelivery
    assert await count_profiles(env) == 1


async def test_reissued_event_with_new_id_does_not_duplicate_or_fail(env):
    user_id = str(uuid.uuid4())
    await env.send_registered(user_id, event_id="evt-1")
    await env.send_registered(user_id, event_id="evt-2")
    assert await count_profiles(env) == 1 and env.dlq.events == []


async def test_bad_payload_ends_in_dlq_without_crashing(env):
    await env.dispatcher.process(
        "user.registered",
        "k",
        {"event_id": "x", "event_type": "user.registered", "payload": {"user_id": "not-a-uuid"}},
    )
    assert env.dlq.events[0][0] == "user.registered.dlq"
    assert await count_profiles(env) == 0


async def test_unrelated_events_are_ignored(env):
    event = {"event_id": "1", "event_type": "order.created"}
    await env.dispatcher.process("order.created", "k", event)
    assert env.dlq.events == [] and await count_profiles(env) == 0
