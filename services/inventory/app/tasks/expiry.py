import asyncio
import contextlib
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.services.inventory_service import InventoryService

logger = logging.getLogger(__name__)


class ExpiryLoop:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], settings: Settings
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> int:
        async with self._session_factory() as session:
            released = await InventoryService(session, self._settings).expire_due()
            await session.commit()
        if released:
            logger.info("Released %d expired reservations", released)
        return released

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - one bad pass must not stop the job
                logger.exception("Expiry pass failed")
            await asyncio.sleep(self._settings.expiry_interval_seconds)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
