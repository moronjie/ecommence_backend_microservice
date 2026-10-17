import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from ecom_common.logging import request_id_ctx

logger = logging.getLogger(__name__)


def build_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    version: int = 1,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """The envelope every Kafka message uses (see docs/events.md)."""
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "version": version,
        "occurred_at": datetime.now(UTC).isoformat(),
        "correlation_id": correlation_id or request_id_ctx.get(),
        "payload": payload,
    }


class EventPublisher(Protocol):
    async def publish(self, topic: str, key: str, event: dict[str, Any]) -> None: ...


class KafkaPublisher:
    """Real publisher. ``start()`` before use, ``stop()`` on shutdown."""

    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap = bootstrap_servers
        self._producer: Any = None

    async def start(self) -> None:
        from aiokafka import AIOKafkaProducer

        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap,
            acks="all",
            enable_idempotence=True,
        )
        await self._producer.start()

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()

    async def publish(self, topic: str, key: str, event: dict[str, Any]) -> None:
        await self._producer.send_and_wait(
            topic,
            key=key.encode(),
            value=json.dumps(event).encode(),
            headers=[("correlation_id", str(event.get("correlation_id", "")).encode())],
        )


class InMemoryPublisher:
    """For tests and local runs without Kafka."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, topic: str, key: str, event: dict[str, Any]) -> None:
        self.events.append((topic, key, event))
