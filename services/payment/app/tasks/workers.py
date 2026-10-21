"""Background jobs. All talking to the provider happens HERE, never while a database row is
locked: claim the work (short transaction), call the provider, then record the result (short
transaction). A slow provider therefore never blocks the database.
"""

import asyncio
import contextlib
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.errors import ServiceUnavailable
from app.providers.base import (
    InitResult,
    PaymentProvider,
    PermanentProviderError,
    TransientProviderError,
)
from app.services.payment_service import PaymentService

logger = logging.getLogger(__name__)


class PaymentWorkers:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: PaymentProvider,
        settings: Settings,
    ) -> None:
        self._sf = session_factory
        self._provider = provider
        self._settings = settings
        self._task: asyncio.Task[None] | None = None

    def _service(self, session: AsyncSession) -> PaymentService:
        return PaymentService(session, self._provider, self._settings)

    # ---- start new payments -------------------------------------------------------------
    async def init_once(self) -> int:
        async with self._sf() as s:
            ids = await self._service(s).due_for_init()
        for payment_id in ids:
            await self._init_one(payment_id)
        return len(ids)

    async def _init_one(self, payment_id) -> None:
        async with self._sf() as s:
            args = await self._service(s).begin_attempt(payment_id)
            await s.commit()  # the attempt is on record BEFORE we call the provider
        if args is None:
            return
        result: InitResult | None = None
        error, permanent = "", False
        try:
            result = await self._provider.initialize(**args)
        except TransientProviderError as exc:
            logger.warning("Provider unavailable starting %s: %s", args["reference"], exc)
            error = str(exc)
        except PermanentProviderError as exc:
            logger.error("Provider refused %s: %s", args["reference"], exc)
            error, permanent = str(exc), True

        async with self._sf() as s:
            service = self._service(s)
            if result is not None:
                await service.attempt_succeeded(
                    payment_id, result.authorization_url, result.access_code
                )
            else:
                await service.attempt_failed(payment_id, error, permanent=permanent)
            await s.commit()

    # ---- check payments whose webhook may have been missed -----------------------------
    async def reconcile_once(self) -> int:
        async with self._sf() as s:
            ids = await self._service(s).due_for_reconcile()
        for payment_id in ids:
            async with self._sf() as s:
                try:
                    await self._service(s).reconcile(payment_id)
                    await s.commit()
                except ServiceUnavailable:
                    logger.warning("Provider unavailable while checking payment %s", payment_id)
        return len(ids)

    # ---- send refunds ---------------------------------------------------------------------
    async def refund_once(self) -> int:
        async with self._sf() as s:
            ids = await self._service(s).refunds_due()
        for refund_id in ids:
            async with self._sf() as s:
                args = await self._service(s).begin_refund(refund_id)
                await s.commit()
            if args is None:
                continue
            kwargs: dict[str, str] = {}
            try:
                result = await self._provider.refund(**args)
                kwargs = {"provider_id": result.refund_id, "status": result.status}
            except TransientProviderError as exc:
                kwargs = {"error": str(exc), "status": "retry"}
            except PermanentProviderError as exc:
                kwargs = {"error": str(exc), "status": "permanent"}
            async with self._sf() as s:
                await self._service(s).refund_result(refund_id, **kwargs)
                await s.commit()
        return len(ids)

    async def run_once(self) -> dict[str, int]:
        return {
            "started": await self.init_once(),
            "checked": await self.reconcile_once(),
            "refunds": await self.refund_once(),
        }

    # ---- loop ---------------------------------------------------------------------------
    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - one bad pass must not stop the job
                logger.exception("Payment worker pass failed")
            await asyncio.sleep(self._settings.worker_interval_seconds)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
