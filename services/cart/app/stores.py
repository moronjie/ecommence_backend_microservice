import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from ecom_common import Conflict
from redis.asyncio import Redis
from redis.exceptions import WatchError

from app.domain import Cart, ProductInfo

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 10


class CartStore:
    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self.redis = redis
        self.ttl = ttl_seconds

    @staticmethod
    def key(user_id: str) -> str:
        return f"cart:{user_id}"

    async def get(self, user_id: str) -> Cart | None:
        return Cart.from_hash(cast("dict[str, str]", await self.redis.hgetall(self.key(user_id))))

    async def delete(self, user_id: str) -> None:
        await self.redis.delete(self.key(user_id))

    async def mutate(
        self, user_id: str, change: Callable[[Cart | None], Cart | None]
    ) -> Cart | None:
        """Read-change-write that is safe against concurrent requests for the same cart.

        WATCH makes Redis abort our write if anyone else touched the cart after we read it; we
        then re-read and try again. ``change`` must be a pure function of the cart it receives.
        Returning ``None`` or an empty cart removes the cart.
        """
        key = self.key(user_id)
        for attempt in range(MAX_ATTEMPTS):
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    current = Cart.from_hash(cast("dict[str, str]", await pipe.hgetall(key)))
                    updated = change(current)
                    pipe.multi()
                    pipe.delete(key)
                    if updated is not None and updated.items:
                        pipe.hset(key, mapping=cast("dict[Any, Any]", updated.to_hash()))
                        pipe.expire(key, self.ttl)
                    await pipe.execute()
                    return updated if updated and updated.items else None
                except WatchError:
                    await asyncio.sleep(0.005 * (attempt + 1))
        raise Conflict("Your cart is being changed elsewhere, please try again")


class ProductStore:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    @staticmethod
    def key(product_id: str) -> str:
        return f"cart-svc:product:{product_id}"

    async def get(self, product_id: str) -> ProductInfo | None:
        raw = await self.redis.get(self.key(product_id))
        return ProductInfo.from_json(raw) if raw else None

    async def save(self, info: ProductInfo) -> bool:
        """Store the copy unless we already hold a newer one. Returns whether it was stored."""
        existing = await self.get(info.product_id)
        if existing and datetime.fromisoformat(existing.event_at) > datetime.fromisoformat(
            info.event_at
        ):
            return False  # an older event arrived late
        await self.redis.set(self.key(info.product_id), info.to_json())
        return True
