"""Background job: deliver queued emails."""

import asyncio
import contextlib
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.senders.base import EmailSender, PermanentSendError, TransientSendError
from app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)


class EmailWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sender: EmailSender,
        settings: Settings,
    ) -> None:
        self._sf = session_factory
        self._sender = sender
        self._settings = settings
        self._task: asyncio.Task[None] | None = None

    def _service(self, session: AsyncSession) -> NotificationService:
        return NotificationService(session, self._settings)

    async def run_once(self) -> int:
        """Send everything that is due. Returns how many were attempted."""
        async with self._sf() as s:
            ids = await self._service(s).due()
        for notification_id in ids:
            await self._send_one(notification_id)
        return len(ids)

    async def _send_one(self, notification_id) -> None:
        async with self._sf() as s:
            email = await self._service(s).begin_send(notification_id)
            await s.commit()  # the attempt is on record BEFORE we talk to the mail server
        if email is None:
            return

        message_id, error, permanent = "", "", False
        try:
            message_id = await self._sender.send(email)
        except TransientSendError as exc:
            logger.warning("Will retry email to %s: %s", email.to, exc)
            error = str(exc)
        except PermanentSendError as exc:
            logger.error("Email to %s cannot be sent: %s", email.to, exc)
            error, permanent = str(exc), True

        async with self._sf() as s:
            service = self._service(s)
            if error:
                await service.record_failure(notification_id, error, permanent=permanent)
            else:
                await service.record_sent(notification_id, message_id)
            await s.commit()

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - one bad pass must not stop the job
                logger.exception("Email worker pass failed")
            await asyncio.sleep(self._settings.worker_interval_seconds)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
