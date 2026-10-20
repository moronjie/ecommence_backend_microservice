from ecom_common.cache import Cache
from fakeredis import FakeAsyncRedis


class BrokenRedis:
    """Every call fails, like Redis being down."""

    def __getattr__(self, name):
        async def fail(*a, **k):
            raise ConnectionError("redis down")

        return fail


async def test_get_or_load_hits_after_first_load():
    cache, calls = Cache(FakeAsyncRedis(decode_responses=True)), []

    async def load():
        calls.append(1)
        return {"n": 1}

    assert await cache.get_or_load("k", 60, load) == ({"n": 1}, False)
    assert await cache.get_or_load("k", 60, load) == ({"n": 1}, True)
    assert len(calls) == 1


async def test_none_is_not_cached():
    cache, calls = Cache(FakeAsyncRedis(decode_responses=True)), []

    async def load():
        calls.append(1)

    assert await cache.get_or_load("k", 60, load) == (None, False)
    assert await cache.get_or_load("k", 60, load) == (None, False)
    assert len(calls) == 2


async def test_delete_and_ttl():
    redis = FakeAsyncRedis(decode_responses=True)
    cache = Cache(redis)
    await cache.set("k", {"a": 1}, ttl=30)
    assert 0 < await redis.ttl("cache:k") <= 30
    await cache.delete("k")
    assert await cache.get("k") is None


async def test_version_bump_orphans_old_keys():
    cache = Cache(FakeAsyncRedis(decode_responses=True))
    v1 = await cache.version("lists")
    await cache.set(f"lists:v{v1}:x", {"old": True}, 60)
    await cache.bump_version("lists")
    v2 = await cache.version("lists")
    assert v2 == v1 + 1 and await cache.get(f"lists:v{v2}:x") is None


async def test_redis_down_fails_open():
    cache = Cache(BrokenRedis())

    async def load():
        return {"from": "db"}

    assert await cache.get_or_load("k", 60, load) == ({"from": "db"}, False)
    await cache.delete("k")
    await cache.bump_version("v")
    assert await cache.version("v") == 0
