import uuid
from datetime import datetime

from ecom_common.db import Base, TimestampMixin
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

# queued --(sent)--> sent
#    \--(gave up, or the address was refused)--> failed --(admin retry)--> queued
QUEUED, SENT, FAILED = "queued", "sent", "failed"


class Notification(TimestampMixin, Base):
    """One email. It is written (with its final text) when the event arrives, and sent later by a
    background worker, so a slow or broken mail server never holds up event processing."""

    __tablename__ = "notifications"

    # One email per business moment, e.g. "order_confirmed:<order id>". Unique, so even if an
    # event is re-issued under a new event id the customer is only told once.
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(index=True, nullable=True)
    to_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(300))
    text_body: Mapped[str] = mapped_column(Text)
    html_body: Mapped[str] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(20), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(String(500), default="")
    provider_message_id: Mapped[str] = mapped_column(String(200), default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
