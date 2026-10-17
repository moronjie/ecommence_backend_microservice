"""Idempotent consumption.

Kafka delivery is at-least-once, so the same event can arrive twice. A handler records the
event id in ``processed_events`` in the SAME transaction as its own changes. If the id is
already there, the event was handled before and is skipped.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from ecom_common.db import Base


class ProcessedEvent(Base):
    __tablename__ = "processed_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


async def first_time(session: AsyncSession, event_id: str) -> bool:
    """Claim an event id. False means it was already processed, so the caller should skip it.

    The claim is only durable if the caller commits the session afterwards.
    """
    if await session.get(ProcessedEvent, event_id) is not None:
        return False
    session.add(ProcessedEvent(event_id=event_id))
    return True
