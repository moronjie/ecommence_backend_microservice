"""Payment rules. Money moves here, so every path is idempotent: doing something twice (a
repeated event, a repeated webhook, a retry after a timeout) must never charge or refund twice.

How a payment travels:
 1. order.payment_requested  -> a ``created`` payment row (this class, in the event handler)
 2. the init worker          -> asks the provider for a payment link, payment becomes ``pending``
 3. the customer pays on the provider's page
 4. a webhook (or, if it never comes, the reconcile worker) -> we ASK THE PROVIDER what happened
    and only then publish payment.completed / payment.failed

We never trust a webhook's contents on their own: the signature proves it came from the provider,
and verifying with the provider confirms the amount and status.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from ecom_common import NotFound
from ecom_common.outbox import add_event
from ecom_common.responses import paginated
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.errors import ServiceUnavailable
from app.models.payment import (
    CREATED,
    FAILED,
    PENDING,
    REFUND_CREATED,
    REFUND_FAILED,
    REFUND_PROCESSED,
    REFUND_REQUESTED,
    REFUNDED,
    SUCCEEDED,
    Payment,
    Refund,
)
from app.providers.base import PaymentProvider, TransientProviderError, Verification
from app.schemas.payment import PaymentRequested

logger = logging.getLogger(__name__)

AMOUNT_MISMATCH = "the provider reports a different amount or currency than was requested"


class PaymentMissing(Exception):
    """An event named an order we never started a payment for. Surfaces in the DLQ."""


class PaymentService:
    def __init__(
        self, session: AsyncSession, provider: PaymentProvider, settings: Settings
    ) -> None:
        self.session = session
        self.provider = provider
        self.settings = settings

    async def commit(self) -> None:
        await self.session.commit()

    # ---- helpers ------------------------------------------------------------------------
    async def _lock(self, payment_id: uuid.UUID) -> Payment | None:
        stmt = (
            select(Payment)
            .where(Payment.id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self.session.scalar(stmt)

    async def by_reference(self, reference: str) -> Payment | None:
        return await self.session.scalar(select(Payment).where(Payment.reference == reference))

    async def by_order(self, order_id: uuid.UUID) -> Payment | None:
        return await self.session.scalar(select(Payment).where(Payment.order_id == order_id))

    @staticmethod
    def _payload(payment: Payment, **extra: Any) -> dict[str, Any]:
        return {
            "order_id": str(payment.order_id),
            "payment_id": str(payment.id),
            "amount_cents": payment.amount_cents,
            "currency": payment.currency,
            **extra,
        }

    def _fail(self, payment: Payment, reason: str) -> None:
        payment.status = FAILED
        payment.failure_reason = reason
        add_event(
            self.session,
            "payment.failed",
            self._payload(payment, reason=reason),
            key=str(payment.order_id),
        )

    # ---- 1. a payment is requested ------------------------------------------------------
    async def request_payment(self, p: PaymentRequested) -> Payment:
        existing = await self.by_order(p.order_id)
        if existing is not None:
            return existing  # the same request again: same payment, nothing new

        payment = Payment(
            order_id=p.order_id,
            user_id=p.user_id,
            email=p.email or "",
            amount_cents=p.total_cents,
            currency=p.currency.upper(),
            status=CREATED,
            provider=self.provider.name,
        )
        self.session.add(payment)
        await self.session.flush()

        # Reject what can never succeed now, instead of failing later at the provider
        if payment.currency not in self.settings.supported_currencies:
            self._fail(payment, f"{payment.currency} payments are not accepted")
        elif not p.email:
            self._fail(payment, "the order has no customer email")
        elif p.total_cents <= 0:
            self._fail(payment, "there is nothing to pay")
        return payment

    # ---- 2. starting the payment (called by the init worker) ----------------------------
    async def begin_attempt(self, payment_id: uuid.UUID) -> dict[str, Any] | None:
        """Claim an attempt and give it a fresh reference. Returns what to send the provider."""
        payment = await self._lock(payment_id)
        if payment is None or payment.status != CREATED:
            return None
        payment.attempts += 1
        payment.reference = f"{payment.order_id.hex}-{payment.attempts}"
        return {
            "reference": payment.reference,
            "amount_cents": payment.amount_cents,
            "currency": payment.currency,
            "email": payment.email,
            "metadata": {"order_id": str(payment.order_id), "payment_id": str(payment.id)},
            "callback_url": self.settings.payment_callback_url,
            "channels": self.settings.payment_channels,
        }

    async def attempt_succeeded(
        self, payment_id: uuid.UUID, authorization_url: str, access_code: str
    ) -> None:
        payment = await self._lock(payment_id)
        if payment is not None and payment.status == CREATED:
            payment.status = PENDING
            payment.authorization_url = authorization_url
            payment.access_code = access_code
            payment.last_error = ""

    async def attempt_failed(self, payment_id: uuid.UUID, error: str, *, permanent: bool) -> None:
        payment = await self._lock(payment_id)
        if payment is None or payment.status != CREATED:
            return
        payment.last_error = error[:300]
        if permanent:
            self._fail(payment, f"the payment provider refused it: {error}"[:300])
        elif payment.attempts >= self.settings.max_attempts:
            self._fail(payment, "could not reach the payment provider")

    # ---- 3/4. finding out what happened --------------------------------------------------
    async def apply_verification(self, payment: Payment, v: Verification) -> None:
        """Update a (locked) payment from the provider's own report."""
        if v.status == "success":
            if payment.status in (SUCCEEDED, REFUNDED):
                return  # already known
            if v.amount_cents != payment.amount_cents or v.currency.upper() != payment.currency:
                if payment.status == FAILED and payment.failure_reason == AMOUNT_MISMATCH:
                    return  # already dealt with
                # Money moved, but not what we asked for: refuse the order and give it back
                logger.error("Amount mismatch for payment %s: %s", payment.id, v)
                self._fail(payment, AMOUNT_MISMATCH)
                self._add_refund(payment, v.amount_cents, "amount mismatch")
                return
            payment.status = SUCCEEDED
            payment.channel = v.channel
            payment.provider_transaction_id = v.transaction_id
            payment.paid_at = datetime.now(UTC)
            add_event(
                self.session,
                "payment.completed",
                self._payload(payment, channel=v.channel, reference=payment.reference),
                key=str(payment.order_id),
            )
        elif v.status == "failed" and payment.status in (CREATED, PENDING):
            self._fail(payment, v.reason or "the payment was declined")
        # "pending" and "abandoned" change nothing here; abandoned payments expire by age

    async def settle(self, reference: str) -> bool:
        """Ask the provider about ``reference`` and update our record. False if we do not know it.

        Used by the webhook, the reconcile worker and the development simulator.
        """
        known = await self.by_reference(reference)
        if known is None:
            return False
        try:
            verification = await self.provider.verify(reference)  # outside any row lock
        except TransientProviderError as exc:
            raise ServiceUnavailable("The payment provider could not be reached") from exc
        payment = await self._lock(known.id)
        if payment is not None:
            await self.apply_verification(payment, verification)
        return True

    async def reconcile(self, payment_id: uuid.UUID, now: datetime | None = None) -> None:
        """Double-check one pending payment (a webhook may have been missed) and expire it
        if the customer never finished."""
        now = now or datetime.now(UTC)
        payment = await self.session.get(Payment, payment_id)
        if payment is None or payment.status != PENDING or payment.reference is None:
            return
        await self.settle(payment.reference)
        payment = await self._lock(payment_id)
        if payment is None or payment.status != PENDING:
            return
        payment.last_checked_at = now
        age = now - _aware(payment.created_at)
        if age >= timedelta(minutes=self.settings.payment_expiry_minutes):
            self._fail(payment, "the payment was not completed in time")

    async def due_for_reconcile(
        self, now: datetime | None = None, limit: int = 50
    ) -> list[uuid.UUID]:
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(seconds=self.settings.reconcile_after_seconds)
        rows = await self.session.scalars(
            select(Payment.id)
            .where(
                Payment.status == PENDING,
                (Payment.last_checked_at.is_(None)) | (Payment.last_checked_at <= cutoff),
            )
            .limit(limit)
        )
        return list(rows)

    async def due_for_init(self, limit: int = 50) -> list[uuid.UUID]:
        rows = await self.session.scalars(
            select(Payment.id)
            .where(Payment.status == CREATED, Payment.attempts < self.settings.max_attempts)
            .limit(limit)
        )
        return list(rows)

    # ---- webhook -------------------------------------------------------------------------
    async def handle_provider_event(self, body: dict[str, Any]) -> None:
        """A (signature-checked) message from the provider. Unknown kinds are ignored."""
        kind, data = body.get("event"), body.get("data") or {}
        if kind == "charge.success":
            await self.settle(str(data.get("reference", "")))
        elif kind in ("refund.processed", "refund.failed"):
            reference = str(data.get("transaction_reference") or data.get("reference") or "")
            payment = await self.by_reference(reference)
            if payment is not None:
                refund = await self.session.scalar(
                    select(Refund).where(Refund.payment_id == payment.id)
                )
                if refund is not None and refund.status != REFUND_PROCESSED:
                    refund.status = (
                        REFUND_PROCESSED if kind == "refund.processed" else REFUND_FAILED
                    )
                    if kind == "refund.failed":
                        logger.error("Provider reports refund %s failed", refund.id)

    # ---- refunds -------------------------------------------------------------------------
    def _add_refund(self, payment: Payment, amount_cents: int, reason: str) -> None:
        self.session.add(
            Refund(
                payment_id=payment.id,
                amount_cents=amount_cents,
                status=REFUND_CREATED,
                reason=reason[:300],
            )
        )

    async def request_refund(self, order_id: uuid.UUID, reason: str) -> None:
        payment = await self.by_order(order_id)
        if payment is None:
            raise PaymentMissing(f"No payment for order {order_id}")
        payment = await self._lock(payment.id)
        if payment is None or payment.status != SUCCEEDED:
            return  # nothing was collected, or it is already refunded
        existing = await self.session.scalar(select(Refund).where(Refund.payment_id == payment.id))
        if existing is None:
            self._add_refund(payment, payment.amount_cents, reason or "order cancelled")

    async def refunds_due(self, limit: int = 20) -> list[uuid.UUID]:
        rows = await self.session.scalars(
            select(Refund.id)
            .where(Refund.status == REFUND_CREATED, Refund.attempts < self.settings.max_attempts)
            .limit(limit)
        )
        return list(rows)

    async def begin_refund(self, refund_id: uuid.UUID) -> dict[str, Any] | None:
        stmt = (
            select(Refund)
            .where(Refund.id == refund_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        refund = await self.session.scalar(stmt)
        if refund is None or refund.status != REFUND_CREATED:
            return None
        payment = await self.session.get(Payment, refund.payment_id)
        if payment is None or not payment.reference:
            return None
        refund.attempts += 1
        return {
            "reference": payment.reference,
            "amount_cents": refund.amount_cents,
            "reason": refund.reason,
        }

    async def refund_result(
        self, refund_id: uuid.UUID, *, provider_id: str = "", status: str = "", error: str = ""
    ) -> None:
        refund = await self.session.get(Refund, refund_id, with_for_update=True)
        if refund is None or refund.status != REFUND_CREATED:
            return
        payment = await self.session.get(Payment, refund.payment_id)
        if error:
            refund.last_error = error[:300]
            if status == "permanent" or refund.attempts >= self.settings.max_attempts:
                refund.status = REFUND_FAILED
                logger.error("Refund %s failed for good: %s", refund.id, error)
            return
        refund.provider_refund_id = provider_id
        refund.status = {"processed": REFUND_PROCESSED, "failed": REFUND_FAILED}.get(
            status, REFUND_REQUESTED
        )
        if refund.status != REFUND_FAILED and payment is not None and payment.status != REFUNDED:
            payment.status = REFUNDED
            add_event(
                self.session,
                "payment.refunded",
                self._payload(payment, refund_id=str(refund.id), refund_status=refund.status),
                key=str(payment.order_id),
            )

    # ---- reading -------------------------------------------------------------------------
    async def for_order(self, order_id: uuid.UUID, user_id: uuid.UUID, is_admin: bool) -> Payment:
        payment = await self.by_order(order_id)
        # Someone else's payment looks exactly like one that does not exist
        if payment is None or (payment.user_id != user_id and not is_admin):
            raise NotFound("No payment found for this order", message="Not found")
        return payment

    async def list_payments(self, status: str | None, page: int, size: int) -> dict[str, Any]:
        conditions = [Payment.status == status] if status else []
        total = await self.session.scalar(
            select(func.count()).select_from(Payment).where(*conditions)
        )
        rows = await self.session.scalars(
            select(Payment)
            .where(*conditions)
            .order_by(Payment.created_at.desc(), Payment.id)
            .offset((page - 1) * size)
            .limit(size)
        )
        return paginated(list(rows), total or 0, page, size)


def _aware(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes; we always store UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
