import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ecom_common import Conflict, NotFound
from ecom_common.outbox import add_event
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.inventory import (
    CONFIRMED,
    FAILED,
    RELEASED,
    RESERVED,
    Reservation,
    ReservationItem,
    StockItem,
    StockMovement,
)

logger = logging.getLogger(__name__)


class ReservationMissing(Exception):
    """An order was confirmed that we never reserved. Surfaces in the DLQ for investigation."""


@dataclass(frozen=True)
class Line:
    product_id: uuid.UUID
    quantity: int


def _merge(lines: Iterable[Line]) -> dict[uuid.UUID, int]:
    """Two lines for the same product become one."""
    wanted: dict[uuid.UUID, int] = {}
    for line in lines:
        wanted[line.product_id] = wanted.get(line.product_id, 0) + line.quantity
    return wanted


class InventoryService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    async def commit(self) -> None:
        await self.session.commit()

    # ---- locking helpers ----------------------------------------------------------------
    async def _lock_stock(self, product_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, StockItem]:
        ids = sorted(set(product_ids))  # same order everywhere = no deadlocks
        stmt = (
            select(StockItem)
            .where(StockItem.product_id.in_(ids))
            .order_by(StockItem.product_id)
            .with_for_update()
            .execution_options(populate_existing=True)  # never trust a stale in-memory copy
        )
        return {item.product_id: item for item in await self.session.scalars(stmt)}

    async def _lock_reservation(self, order_id: uuid.UUID) -> Reservation | None:
        stmt = (
            select(Reservation)
            .where(Reservation.order_id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self.session.scalar(stmt)

    def _move(
        self, product_id: uuid.UUID, on_hand: int, reserved: int, kind: str, ref: str
    ) -> None:
        self.session.add(
            StockMovement(
                product_id=product_id,
                delta_on_hand=on_hand,
                delta_reserved=reserved,
                kind=kind,
                reference=ref,
            )
        )

    # ---- stock levels (admin and product events) ----------------------------------------
    async def ensure_item(self, product_id: uuid.UUID, sku: str) -> StockItem:
        """Create the (empty) stock row for a new product, or keep the SKU in sync."""
        item = await self.session.get(StockItem, product_id)
        if item is None:
            item = StockItem(product_id=product_id, sku=sku, on_hand=0, reserved=0)
            self.session.add(item)
        elif sku and item.sku != sku:
            item.sku = sku
        return item

    async def get_item(self, product_id: uuid.UUID) -> StockItem:
        item = await self.session.get(StockItem, product_id)
        if item is None:
            raise NotFound("No stock record for this product", message="Not found")
        return item

    async def availability(self, product_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, StockItem]:
        ids = list(set(product_ids))
        rows = await self.session.scalars(select(StockItem).where(StockItem.product_id.in_(ids)))
        return {item.product_id: item for item in rows}

    async def list_items(
        self, page: int, size: int, max_available: int | None
    ) -> tuple[list[StockItem], int]:
        conditions = []
        if max_available is not None:
            conditions.append(StockItem.on_hand - StockItem.reserved <= max_available)
        total = await self.session.scalar(
            select(func.count()).select_from(StockItem).where(*conditions)
        )
        rows = await self.session.scalars(
            select(StockItem)
            .where(*conditions)
            .order_by(StockItem.sku, StockItem.product_id)
            .offset((page - 1) * size)
            .limit(size)
        )
        return list(rows), total or 0

    async def _locked_item(self, product_id: uuid.UUID) -> StockItem:
        item = (await self._lock_stock([product_id])).get(product_id)
        if item is None:
            raise NotFound("No stock record for this product", message="Not found")
        return item

    async def set_on_hand(self, product_id: uuid.UUID, on_hand: int, actor: str) -> StockItem:
        item = await self._locked_item(product_id)
        if on_hand < item.reserved:
            raise Conflict(f"{item.reserved} units are reserved for orders; cannot count fewer")
        self._move(product_id, on_hand - item.on_hand, 0, "stocktake", actor)
        item.on_hand = on_hand
        return item

    async def adjust(self, product_id: uuid.UUID, delta: int, reason: str, actor: str) -> StockItem:
        item = await self._locked_item(product_id)
        new_total = item.on_hand + delta
        if new_total < item.reserved:
            raise Conflict(
                f"Cannot remove {-delta}: {item.reserved} of {item.on_hand} units are reserved"
            )
        self._move(product_id, delta, 0, "adjustment", f"{actor}: {reason}".strip(": "))
        item.on_hand = new_total
        return item

    # ---- reservations (driven by order events) ------------------------------------------
    async def reserve(self, order_id: uuid.UUID, lines: Iterable[Line]) -> Reservation:
        """Hold stock for an order, all lines or none. Safe to call twice for the same order."""
        existing = await self._lock_reservation(order_id)
        if existing is not None:
            return existing  # redelivered event, or the order was cancelled before we got to it

        wanted = _merge(lines)
        stock = await self._lock_stock(wanted)

        shortages = []
        for product_id, quantity in wanted.items():
            item = stock.get(product_id)
            available = item.available if item else 0  # an unknown product has no stock
            if available < quantity:
                shortages.append(
                    {"product_id": str(product_id), "requested": quantity, "available": available}
                )

        if shortages:
            reservation = Reservation(order_id=order_id, status=FAILED, reason="insufficient stock")
            self.session.add(reservation)
            add_event(
                self.session,
                "inventory.failed",
                {"order_id": str(order_id), "reason": "insufficient stock", "shortages": shortages},
                key=str(order_id),
            )
            return reservation

        expires_at = datetime.now(UTC) + timedelta(minutes=self.settings.reservation_ttl_minutes)
        reservation = Reservation(
            order_id=order_id,
            status=RESERVED,
            expires_at=expires_at,
            items=[ReservationItem(product_id=p, quantity=q) for p, q in wanted.items()],
        )
        self.session.add(reservation)
        for product_id, quantity in wanted.items():
            stock[product_id].reserved += quantity
            self._move(product_id, 0, quantity, "reserve", str(order_id))
        add_event(
            self.session,
            "inventory.reserved",
            {
                "order_id": str(order_id),
                "items": [{"product_id": str(p), "quantity": q} for p, q in wanted.items()],
                "expires_at": expires_at.isoformat(),
            },
            key=str(order_id),
        )
        return reservation

    async def confirm(self, order_id: uuid.UUID) -> None:
        """The order is paid: the reserved units permanently leave the shelf."""
        reservation = await self._lock_reservation(order_id)
        if reservation is None:
            raise ReservationMissing(f"Order {order_id} was confirmed but never reserved")
        if reservation.status in (CONFIRMED, FAILED):
            return  # already done (redelivery), or nothing was ever held

        stock = await self._lock_stock(i.product_id for i in reservation.items)
        if reservation.status == RESERVED:
            for line in reservation.items:
                item = stock[line.product_id]
                item.on_hand -= line.quantity
                item.reserved -= line.quantity
                self._move(line.product_id, -line.quantity, -line.quantity, "commit", str(order_id))
        else:  # RELEASED: the hold expired before payment arrived. Try to take the stock again.
            short = [
                line
                for line in reservation.items
                if line.product_id not in stock or stock[line.product_id].available < line.quantity
            ]
            if short:
                reservation.reason = "reservation expired and the stock was sold"
                add_event(
                    self.session,
                    "inventory.failed",
                    {"order_id": str(order_id), "reason": reservation.reason, "shortages": []},
                    key=str(order_id),
                )
                return
            for line in reservation.items:
                stock[line.product_id].on_hand -= line.quantity
                self._move(line.product_id, -line.quantity, 0, "commit_after_expiry", str(order_id))
        reservation.status = CONFIRMED
        reservation.expires_at = None

    async def release(self, order_id: uuid.UUID, reason: str) -> None:
        """Give the held stock back (order cancelled or payment failed)."""
        reservation = await self._lock_reservation(order_id)
        if reservation is None:
            # Cancelled before we processed order.created (different topics aren't ordered).
            # Leave a marker so that late order.created finds it and reserves nothing.
            self.session.add(
                Reservation(order_id=order_id, status=RELEASED, reason=f"{reason} before reserve")
            )
            return
        if reservation.status == RESERVED:
            await self._release_locked(reservation, reason)

    async def _release_locked(self, reservation: Reservation, reason: str) -> None:
        stock = await self._lock_stock(i.product_id for i in reservation.items)
        for line in reservation.items:
            stock[line.product_id].reserved -= line.quantity
            self._move(line.product_id, 0, -line.quantity, "release", str(reservation.order_id))
        reservation.status = RELEASED
        reservation.reason = reason
        reservation.expires_at = None
        add_event(
            self.session,
            "inventory.released",
            {"order_id": str(reservation.order_id), "reason": reason},
            key=str(reservation.order_id),
        )

    async def expire_due(self, now: datetime | None = None, limit: int = 100) -> int:
        """Release holds that were never paid for. Returns how many were released."""
        stmt = (
            select(Reservation)
            .where(
                Reservation.status == RESERVED, Reservation.expires_at <= (now or datetime.now(UTC))
            )
            .order_by(Reservation.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)  # another worker may be handling some of them
            .execution_options(populate_existing=True)
        )
        due: list[Any] = list(await self.session.scalars(stmt))
        for reservation in due:
            await self._release_locked(reservation, "expired")
        return len(due)
