import uuid
from typing import Any

from ecom_common.db import Base, TimestampMixin
from sqlalchemy import JSON, BigInteger, CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

PENDING = "pending"  # created; waiting to hear whether the stock could be reserved
AWAITING_PAYMENT = "awaiting_payment"  # stock is held; waiting for the payment result
CONFIRMED = "confirmed"  # paid
SHIPPED = "shipped"
DELIVERED = "delivered"
CANCELLED = "cancelled"

# The only moves an order may make. Anything else is refused (API) or ignored as stale (events).
# PENDING -> CONFIRMED exists because events from different services can arrive out of order:
# the payment result may overtake the "stock reserved" message.
TRANSITIONS: dict[str, set[str]] = {
    PENDING: {AWAITING_PAYMENT, CONFIRMED, CANCELLED},
    AWAITING_PAYMENT: {CONFIRMED, CANCELLED},
    CONFIRMED: {SHIPPED},
    SHIPPED: {DELIVERED},
    DELIVERED: set(),
    CANCELLED: set(),
}
CANCELLABLE_BY_USER = {PENDING, AWAITING_PAYMENT}  # after payment it becomes a refund matter


class Order(TimestampMixin, Base):
    __tablename__ = "orders"

    # The cart's checkout id. Unique, so a repeated checkout can never create a second order.
    checkout_id: Mapped[uuid.UUID] = mapped_column(unique=True, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    currency: Mapped[str] = mapped_column(String(3))
    customer_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    total_cents: Mapped[int] = mapped_column(BigInteger)
    # A copy of the address at checkout time, so editing an address later cannot change old orders
    shipping_address: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    cancel_reason: Mapped[str] = mapped_column(String(300), default="")

    items: Mapped[list["OrderItem"]] = relationship(lazy="selectin", cascade="all, delete-orphan")
    history: Mapped[list["StatusChange"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan", order_by="StatusChange.seq"
    )


class OrderItem(Base):
    """What was bought, at the name and price it had when the order was placed."""

    __tablename__ = "order_items"
    __table_args__ = (CheckConstraint("quantity > 0", name="ck_order_quantity_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column()
    sku: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(BigInteger)


class StatusChange(TimestampMixin, Base):
    """Append-only trail of how an order moved, and why."""

    __tablename__ = "order_status_history"

    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(300), default="")
    actor: Mapped[str] = mapped_column(String(64), default="system")
