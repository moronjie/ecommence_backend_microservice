import uuid
from datetime import datetime

from ecom_common.db import Base, TimestampMixin
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

# Reservation lifecycle:
#   reserved --(order.confirmed)--> confirmed          stock permanently leaves the shelf
#   reserved --(cancelled/expired)-> released          stock goes back on the shelf
#   failed                                             not enough stock; nothing was held
RESERVED, CONFIRMED, RELEASED, FAILED = "reserved", "confirmed", "released", "failed"


class StockItem(Base):
    """Stock for one product. ``available`` = ``on_hand`` - ``reserved``.

    The CHECK constraints are the last line of defence: even if application code has a bug,
    the database refuses to let stock go negative or to promise more than exists.
    """

    __tablename__ = "stock_items"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012
    __table_args__ = (
        CheckConstraint("on_hand >= 0", name="ck_on_hand_non_negative"),
        CheckConstraint("reserved >= 0", name="ck_reserved_non_negative"),
        CheckConstraint("reserved <= on_hand", name="ck_reserved_within_on_hand"),
    )

    product_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), default="")
    on_hand: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


class Reservation(TimestampMixin, Base):
    """Stock held for one order. ``order_id`` is unique, so an order is only ever reserved once."""

    __tablename__ = "reservations"

    order_id: Mapped[uuid.UUID] = mapped_column(unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(String(200), default="")

    items: Mapped[list["ReservationItem"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan"
    )


class ReservationItem(Base):
    __tablename__ = "reservation_items"
    __table_args__ = (CheckConstraint("quantity > 0", name="ck_quantity_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    reservation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reservations.id"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column()
    quantity: Mapped[int] = mapped_column(Integer)


class StockMovement(TimestampMixin, Base):
    """Append-only ledger: every change to a stock level leaves a row saying why."""

    __tablename__ = "stock_movements"

    product_id: Mapped[uuid.UUID] = mapped_column(index=True)
    delta_on_hand: Mapped[int] = mapped_column(Integer, default=0)
    delta_reserved: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(30))
    reference: Mapped[str] = mapped_column(String(200), default="")
