"""Kafka consuming: retries, a dead-letter queue, and a reconnecting runner.

``EventDispatcher`` holds the logic (and is unit-testable without Kafka).
``KafkaConsumerRunner`` is the thin loop that feeds it messages from a broker.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from ecom_common.kafka import EventPublisher, KafkaPublisher
from ecom_common.logging import request_id_ctx
from ecom_common.metrics import count_event

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class EventDispatcher:
    """Routes an event to its handler by ``event_type``. Failed events are retried, then sent
    to ``<topic>.dlq`` so one poison message can never block the topic."""

    def __init__(
        self,
        handlers: dict[str, Handler],
        dlq_publisher: EventPublisher,
        *,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
    ) -> None:
        self.handlers = handlers
        self.dlq = dlq_publisher
        self.max_attempts = max_attempts
        self.backoff = backoff_seconds

    async def process(self, topic: str, key: str, raw: bytes | dict[str, Any]) -> None:
        """Never raises: the event is either handled, ignored or parked in the DLQ."""
        try:
            event = json.loads(raw) if isinstance(raw, bytes | str) else raw
            handler = self.handlers.get(event["event_type"])
            event_id = event["event_id"]
        except (ValueError, KeyError, TypeError) as exc:
            count_event(topic, "dead_lettered")
            await self._dead_letter(topic, key, raw, f"malformed event: {exc!r}")
            return
        if handler is None:
            count_event(topic, "ignored")
            return  # not an event this service cares about

        # Work under the id of the request that started all this. Logs, and any events we publish
        # in turn, then carry the same id, so one checkout can be followed across every service.
        token = request_id_ctx.set(str(event.get("correlation_id") or request_id_ctx.get()))
        try:
            await self._handle(topic, key, event, event_id, handler)
        finally:
            request_id_ctx.reset(token)

    async def _handle(
        self, topic: str, key: str, event: dict[str, Any], event_id: str, handler: Handler
    ) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                await handler(event)
                count_event(topic, "handled")
                logger.info(
                    "Handled %s (event %s)", topic, event_id
                )  # shows up under the request id
                return
            except Exception as exc:  # noqa: BLE001 - any handler failure is retried
                last_error = exc
                count_event(topic, "retried")
                logger.warning(
                    "Handler failed for %s (attempt %d/%d): %r",
                    event_id,
                    attempt,
                    self.max_attempts,
                    exc,
                )
                if attempt < self.max_attempts:
                    await asyncio.sleep(self.backoff * attempt)
        count_event(topic, "dead_lettered")
        await self._dead_letter(topic, key, event, f"failed after retries: {last_error!r}")

    async def _dead_letter(self, topic: str, key: str, original: Any, reason: str) -> None:
        if isinstance(original, bytes):
            original = original.decode(errors="replace")
        logger.error("Dead-lettering message from %s: %s", topic, reason)
        await self.dlq.publish(
            f"{topic}.dlq",
            key or "unknown",
            {"reason": reason, "failed_at": datetime.now(UTC).isoformat(), "original": original},
        )


class KafkaConsumerRunner:
    """Reads topics and feeds the dispatcher. Reconnects if Kafka goes away.

    Offsets are committed only after the dispatcher finishes (handled or dead-lettered), so a
    crash mid-handling re-delivers the message instead of losing it.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        group_id: str,
        topics: list[str],
        handlers: dict[str, Handler],
        *,
        retry_connect_seconds: float = 5.0,
    ) -> None:
        self._bootstrap = bootstrap_servers
        self._group_id = group_id
        self._topics = topics
        self._handlers = handlers
        self._retry = retry_connect_seconds
        self._task: asyncio.Task[None] | None = None

    async def _consume_once(self) -> None:
        from aiokafka import AIOKafkaConsumer

        publisher = KafkaPublisher(self._bootstrap)
        await publisher.start()
        consumer = AIOKafkaConsumer(
            *self._topics,
            bootstrap_servers=self._bootstrap,
            group_id=self._group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        await consumer.start()
        try:
            dispatcher = EventDispatcher(self._handlers, publisher)
            async for msg in consumer:
                key = msg.key.decode() if msg.key else ""
                await dispatcher.process(msg.topic, key, msg.value)
                await consumer.commit()
        finally:
            await consumer.stop()
            await publisher.stop()

    async def _run(self) -> None:
        while True:
            try:
                await self._consume_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep trying until Kafka is reachable
                logger.exception("Kafka consumer stopped; reconnecting in %ss", self._retry)
                await asyncio.sleep(self._retry)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
