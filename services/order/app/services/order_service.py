import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from ecom_common import Conflict, NotFound
from ecom_common.outbox import add_event
from ecom_common.responses import paginated
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.order import (
    AWAITING_PAYMENT,
    CANCELLABLE_BY_USER,
    CANCELLED,
    CONFIRMED,
    DELIVERED,
    PENDING,
    SHIPPED,
    TRANSITIONS,
    Order,
    OrderItem,
    StatusChange,
)
from app.schemas.order import CheckoutPayload

logger = logging.getLogger(__name__)

# Which event announces each new status
EVENT_FOR_STATUS = {
    AWAITING_PAYMENT: "order.payment_requested",
    CONFIRMED: "order.confirmed",
    SHIPPED: "order.shipped",
    DELIVERED: "order.delivered",
    CANCELLED: "order.cancelled",
}


class OrderMissing(Exception):
    """An event named an order we do not have. Surfaces in the DLQ for investigation."""


class BadCheckout(Exception):
    """A checkout request that does not add up. Surfaces in the DLQ; never becomes an order."""


class OrderService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    async def commit(self) -> None:
        await self.session.commit()

    # ---- helpers ------------------------------------------------------------------------
    async def _lock(self, order_id: uuid.UUID) -> Order | None:
        """Load an order and lock its row, so two changes to one order happen one at a time."""
        stmt = (
            select(Order)
            .where(Order.id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self.session.scalar(stmt)

    async def _must_lock(self, order_id: uuid.UUID) -> Order:
        order = await self._lock(order_id)
        if order is None:
            raise OrderMissing(f"Unknown order {order_id}")
        return order

    @staticmethod
    def _payload(order: Order, **extra: Any) -> dict[str, Any]:
        return {
            "order_id": str(order.id),
            "user_id": str(order.user_id),
            "status": order.status,
            "currency": order.currency,
            "total_cents": order.total_cents,
            **extra,
        }

    def _record(self, order: Order, to: str, reason: str, actor: str) -> None:
        order.history.append(
            StatusChange(
                order_id=order.id,
                seq=len(order.history) + 1,
                from_status=order.status if order.history else None,
                to_status=to,
                reason=reason,
                actor=actor,
            )
        )

    def _apply(self, order: Order, to: str, reason: str = "", actor: str = "system") -> None:
        """Move the order. Callers must already have checked the move is allowed."""
        self._record(order, to, reason, actor)
        order.status = to
        if to == CANCELLED:
            order.cancel_reason = reason
        extra: dict[str, Any] = {"reason": reason} if to == CANCELLED else {}
        if to == AWAITING_PAYMENT:
            extra["email"] = order.customer_email  # the payment service bills this address
        add_event(
            self.session,
            EVENT_FOR_STATUS[to],
            self._payload(order, **extra),
            key=str(order.id),
        )

    def _react(self, order: Order, to: str, reason: str = "") -> bool:
        """Move because of another service's message, or ignore it if it no longer applies."""
        if to not in TRANSITIONS[order.status]:
            logger.info(
                "Ignoring stale event: order %s is %s, not moving to %s", order.id, order.status, to
            )
            return False
        self._apply(order, to, reason)
        return True

    # ---- creating an order --------------------------------------------------------------
    async def create_from_checkout(self, p: CheckoutPayload) -> Order:
        existing = await self.session.scalar(
            select(Order).where(Order.checkout_id == p.checkout_id)
        )
        if existing is not None:
            return existing  # a repeated checkout: same order, nothing new

        computed = sum(i.quantity * i.unit_price_cents for i in p.items)
        if computed != p.total_cents:
            raise BadCheckout(f"Total {p.total_cents} does not match the items ({computed})")

        order = Order(
            checkout_id=p.checkout_id,
            user_id=p.user_id,
            status=PENDING,
            currency=p.currency.upper(),
            customer_email=p.email,
            total_cents=p.total_cents,
            shipping_address=p.shipping_address,
            history=[],  # a new order has none yet; avoids a database read on first use
            items=[
                OrderItem(
                    product_id=i.product_id,
                    sku=i.sku,
                    name=i.name,
                    quantity=i.quantity,
                    unit_price_cents=i.unit_price_cents,
                )
                for i in p.items
            ],
        )
        self.session.add(order)
        await self.session.flush()
        self._record(order, PENDING, "order placed", str(p.user_id))
        # The inventory service reserves stock when it sees this
        add_event(
            self.session,
            "order.created",
            self._payload(
                order,
                items=[
                    {
                        "product_id": str(i.product_id),
                        "quantity": i.quantity,
                        "unit_price_cents": i.unit_price_cents,
                    }
                    for i in order.items
                ],
            ),
            key=str(order.id),
        )
        return order

    # ---- reactions to other services ----------------------------------------------------
    async def on_stock_reserved(self, order_id: uuid.UUID) -> None:
        order = await self._must_lock(order_id)
        if order.status == PENDING:
            self._apply(order, AWAITING_PAYMENT, "stock reserved")  # payment is asked for now
        # anything else: the order was already cancelled or moved on. The inventory service
        # releases the hold itself when it sees order.cancelled.

    async def on_stock_failed(self, order_id: uuid.UUID, reason: str) -> None:
        order = await self._must_lock(order_id)
        self._react(order, CANCELLED, reason or "out of stock")

    async def on_hold_released(self, order_id: uuid.UUID, reason: str) -> None:
        if reason != "expired":
            return  # "cancelled": we asked for that ourselves
        order = await self._must_lock(order_id)
        self._react(order, CANCELLED, "payment not received in time")

    async def on_payment_completed(self, order_id: uuid.UUID) -> None:
        order = await self._must_lock(order_id)
        if order.status == CANCELLED:
            # Money arrived for an order we had already given up on: it must be returned
            add_event(
                self.session,
                "order.refund_requested",
                self._payload(order, reason="payment arrived after the order was cancelled"),
                key=str(order.id),
            )
            return
        self._react(order, CONFIRMED, "payment received")

    async def on_payment_failed(self, order_id: uuid.UUID, reason: str) -> None:
        order = await self._must_lock(order_id)
        self._react(order, CANCELLED, f"payment failed: {reason}" if reason else "payment failed")

    # ---- people (API) -------------------------------------------------------------------
    async def get(self, order_id: uuid.UUID, user_id: uuid.UUID, is_admin: bool) -> Order:
        order = await self.session.get(Order, order_id)
        # Someone else's order looks exactly like one that does not exist
        if order is None or (order.user_id != user_id and not is_admin):
            raise NotFound("Order not found", message="Not found")
        return order

    async def list_orders(
        self,
        *,
        user_id: uuid.UUID | None,
        status: str | None,
        page: int,
        size: int,
    ) -> dict[str, Any]:
        conditions = []
        if user_id is not None:
            conditions.append(Order.user_id == user_id)
        if status:
            conditions.append(Order.status == status)
        total = await self.session.scalar(
            select(func.count()).select_from(Order).where(*conditions)
        )
        rows = await self.session.scalars(
            select(Order)
            .where(*conditions)
            .order_by(Order.created_at.desc(), Order.id)
            .offset((page - 1) * size)
            .limit(size)
        )
        return paginated(list(rows), total or 0, page, size)

    async def _owned_locked(self, order_id: uuid.UUID, user_id: uuid.UUID, is_admin: bool) -> Order:
        order = await self._lock(order_id)
        if order is None or (order.user_id != user_id and not is_admin):
            raise NotFound("Order not found", message="Not found")
        return order

    async def cancel(self, order_id: uuid.UUID, user_id: uuid.UUID, is_admin: bool) -> Order:
        order = await self._owned_locked(order_id, user_id, is_admin)
        if order.status == CANCELLED:
            return order  # asking twice is fine
        if order.status not in CANCELLABLE_BY_USER:
            raise Conflict(f"A {order.status} order can no longer be cancelled")
        who = "administrator" if is_admin and order.user_id != user_id else "customer"
        self._apply(order, CANCELLED, f"cancelled by {who}", actor=str(user_id))
        return order

    async def _advance(self, order_id: uuid.UUID, actor: uuid.UUID, to: str, needs: str) -> Order:
        order = await self._lock(order_id)
        if order is None:
            raise NotFound("Order not found", message="Not found")
        if order.status != needs:
            raise Conflict(f"Only a {needs} order can become {to} (this one is {order.status})")
        self._apply(order, to, actor=str(actor))
        return order

    async def ship(self, order_id: uuid.UUID, actor: uuid.UUID) -> Order:
        return await self._advance(order_id, actor, SHIPPED, CONFIRMED)

    async def deliver(self, order_id: uuid.UUID, actor: uuid.UUID) -> Order:
        return await self._advance(order_id, actor, DELIVERED, SHIPPED)

    # ---- housekeeping -------------------------------------------------------------------
    async def cancel_stale(self, now: datetime | None = None, limit: int = 100) -> int:
        """Cancel orders that never got an answer, so nothing waits forever."""
        now = now or datetime.now(UTC)
        s = self.settings
        cutoffs = {
            PENDING: (now - timedelta(minutes=s.pending_timeout_minutes), "stock check timed out"),
            AWAITING_PAYMENT: (
                now - timedelta(minutes=s.awaiting_payment_timeout_minutes),
                "payment not received in time",
            ),
        }
        cancelled = 0
        for status, (cutoff, reason) in cutoffs.items():
            stmt = (
                select(Order)
                .where(Order.status == status, Order.updated_at <= cutoff)
                .order_by(Order.updated_at)
                .limit(limit)
                .with_for_update(skip_locked=True)  # another worker may be handling some
                .execution_options(populate_existing=True)
            )
            for order in await self.session.scalars(stmt):
                self._apply(order, CANCELLED, reason)
                cancelled += 1
        return cancelled
