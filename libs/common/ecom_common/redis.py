import time
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis


def create_redis(url: str) -> Redis:
    return Redis.from_url(url, decode_responses=True)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int  # seconds until a slot frees up (0 when allowed)


class SlidingWindowRateLimiter:
    """At most ``limit`` hits per ``window`` seconds per key, using a sorted set."""

    def __init__(self, redis: Redis, prefix: str = "ratelimit") -> None:
        self.redis = redis
        self.prefix = prefix

    async def hit(self, key: str, limit: int, window: int) -> RateLimitResult:
        redis_key = f"{self.prefix}:{key}"
        now = time.time()
        member = f"{now}:{uuid.uuid4().hex}"

        pipe = self.redis.pipeline()
        pipe.zremrangebyscore(redis_key, 0, now - window)
        pipe.zadd(redis_key, {member: now})
        pipe.zcard(redis_key)
        pipe.zrange(redis_key, 0, 0, withscores=True)
        pipe.expire(redis_key, window)
        _, _, count, oldest, _ = await pipe.execute()

        if count > limit:
            await self.redis.zrem(redis_key, member)  # a rejected hit must not extend the block
            oldest_ts = oldest[0][1] if oldest else now
            retry_after = max(1, int(oldest_ts + window - now) + 1)
            return RateLimitResult(False, limit, 0, retry_after)
        return RateLimitResult(True, limit, limit - count, 0)
