import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from ecom_common.db import Base
from ecom_common.kafka import EventPublisher, build_event

logger = logging.getLogger(__name__)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    topic: Mapped[str] = mapped_column(String(200))
    key: Mapped[str] = mapped_column(String(200))
    event: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def add_event(
    session: AsyncSession,
    event_type: str,
    payload: dict[str, Any],
    *,
    key: str,
    topic: str | None = None,
) -> dict[str, Any]:
    """Stage an event in the caller's transaction. It is sent only if that transaction commits."""
    event = build_event(event_type, payload)
    session.add(OutboxEvent(topic=topic or event_type, key=key, event=event))
    return event


class OutboxRelay:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: EventPublisher,
        *,
        interval_seconds: float = 1.0,
        batch_size: int = 100,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._interval = interval_seconds
        self._batch_size = batch_size
        self._task: asyncio.Task[None] | None = None

    async def publish_pending(self) -> int:
        """Send one batch. Returns how many events were published."""
        sent = 0
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.published_at.is_(None))
                    .order_by(OutboxEvent.created_at)
                    .limit(self._batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in rows:
                try:
                    await self._publisher.publish(row.topic, row.key, row.event)
                except Exception:  # noqa: BLE001 - leave the row for the next pass
                    logger.exception("Could not publish outbox event %s", row.id)
                    break  # keep ordering: don't skip ahead of a failed event
                row.published_at = datetime.now(UTC)
                sent += 1
            await session.commit()
        return sent

    async def _run(self) -> None:
        while True:
            try:
                if await self.publish_pending() == self._batch_size:
                    continue  # more waiting, don't sleep
            except Exception:  # noqa: BLE001 - the relay must survive DB/Kafka blips
                logger.exception("Outbox relay pass failed")
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
