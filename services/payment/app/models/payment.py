import uuid
from datetime import datetime

from ecom_common.db import Base, TimestampMixin
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

# Payment lifecycle:
#   created --(sent to the provider)--> pending --(customer pays)--> succeeded --> refunded
#      \______________________________________\__(declined / abandoned / error)__> failed
# A "failed" payment can still turn "succeeded" if the money arrives late; the order service
# then notices the order is already cancelled and asks us for a refund.
CREATED, PENDING, SUCCEEDED, FAILED, REFUNDED = (
    "created",
    "pending",
    "succeeded",
    "failed",
    "refunded",
)

# Refund lifecycle: created -> requested (provider accepted it) -> processed | failed
REFUND_CREATED, REFUND_REQUESTED, REFUND_PROCESSED, REFUND_FAILED = (
    "created",
    "requested",
    "processed",
    "failed",
)


class Payment(TimestampMixin, Base):
    """One payment per order. ``order_id`` is unique, so an order is only ever billed once."""

    __tablename__ = "payments"

    order_id: Mapped[uuid.UUID] = mapped_column(unique=True, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    email: Mapped[str] = mapped_column(String(320))
    amount_cents: Mapped[int] = mapped_column(BigInteger)  # pesewas for GHS
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20), index=True)
    provider: Mapped[str] = mapped_column(String(20))

    # Each attempt to start the payment gets a fresh reference ("<order>-<attempt>"). If a
    # request to the provider is lost mid-flight we never reuse a reference it may have seen.
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    reference: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    authorization_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_code: Mapped[str | None] = mapped_column(String(100), nullable=True)

    channel: Mapped[str] = mapped_column(String(30), default="")  # card, mobile_money, ...
    provider_transaction_id: Mapped[str] = mapped_column(String(100), default="")
    failure_reason: Mapped[str] = mapped_column(String(300), default="")
    last_error: Mapped[str] = mapped_column(String(300), default="")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Refund(TimestampMixin, Base):
    """At most one (full) refund per payment: ``payment_id`` is unique."""

    __tablename__ = "refunds"

    payment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payments.id"), unique=True, index=True
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), index=True)
    reason: Mapped[str] = mapped_column(String(300), default="")
    provider_refund_id: Mapped[str] = mapped_column(String(100), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String(300), default="")
