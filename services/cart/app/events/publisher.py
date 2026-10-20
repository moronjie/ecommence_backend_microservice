import asyncio
from typing import Any

from ecom_common.kafka import KafkaPublisher


class LazyKafkaPublisher:
    """Connects to Kafka on first use, so the service can start before Kafka is ready.

    The cart has no database, so there is no outbox: a checkout is published directly, and the
    request fails (and the cart is kept) if Kafka cannot be reached.
    """

    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap = bootstrap_servers
        self._kafka: KafkaPublisher | None = None
        self._lock = asyncio.Lock()

    async def _connected(self) -> KafkaPublisher:
        async with self._lock:
            if self._kafka is None:
                kafka = KafkaPublisher(self._bootstrap)
                await kafka.start()
                self._kafka = kafka
            return self._kafka

    async def publish(self, topic: str, key: str, event: dict[str, Any]) -> None:
        kafka = await self._connected()
        try:
            await kafka.publish(topic, key, event)
        except Exception:
            await self.stop()  # reconnect next time
            raise

    async def stop(self) -> None:
        kafka, self._kafka = self._kafka, None
        if kafka is not None:
            await kafka.stop()
