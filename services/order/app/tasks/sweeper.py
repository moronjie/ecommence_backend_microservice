import asyncio
import contextlib
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.services.order_service import OrderService

logger = logging.getLogger(__name__)


class StaleOrderSweeper:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], settings: Settings
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> int:
        async with self._session_factory() as session:
            cancelled = await OrderService(session, self._settings).cancel_stale()
            await session.commit()
        if cancelled:
            logger.info("Cancelled %d stale orders", cancelled)
        return cancelled

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - one bad pass must not stop the job
                logger.exception("Sweep failed")
            await asyncio.sleep(self._settings.sweep_interval_seconds)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
