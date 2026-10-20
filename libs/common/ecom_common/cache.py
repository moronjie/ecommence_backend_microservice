import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class Cache:
    def __init__(self, redis: Redis, prefix: str = "cache") -> None:
        self.redis = redis
        self.prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self.redis.get(self._key(key))
        except Exception:  # noqa: BLE001 - fail open
            logger.warning("Cache read failed for %s", key, exc_info=True)
            return None
        return json.loads(raw) if raw is not None else None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        try:
            await self.redis.set(self._key(key), json.dumps(value), ex=ttl)
        except Exception:  # noqa: BLE001 - fail open
            logger.warning("Cache write failed for %s", key, exc_info=True)

    async def delete(self, *keys: str) -> None:
        if not keys:
            return
        try:
            await self.redis.delete(*(self._key(k) for k in keys))
        except Exception:  # noqa: BLE001 - fail open (TTL still bounds staleness)
            logger.warning("Cache delete failed for %s", keys, exc_info=True)

    async def version(self, name: str) -> int:
        try:
            return int(await self.redis.get(self._key(f"version:{name}")) or 0)
        except Exception:  # noqa: BLE001 - fail open
            logger.warning("Cache version read failed for %s", name, exc_info=True)
            return 0

    async def bump_version(self, name: str) -> None:
        try:
            await self.redis.incr(self._key(f"version:{name}"))
        except Exception:  # noqa: BLE001 - fail open
            logger.warning("Cache version bump failed for %s", name, exc_info=True)

    async def get_or_load(
        self, key: str, ttl: int, loader: Callable[[], Awaitable[Any]]
    ) -> tuple[Any, bool]:
        """Return ``(value, was_cache_hit)``. ``None`` is never cached, so misses stay misses."""
        cached = await self.get(key)
        if cached is not None:
            return cached, True
        value = await loader()
        if value is not None:
            await self.set(key, value, ttl)
        return value, False
