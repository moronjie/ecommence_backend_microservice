"""Queueing, delivering and reading notifications.

Receiving an event only WRITES the email to the database (fast, always works). A background
worker delivers it afterwards: claim it (short transaction), send it (no database lock held),
record the outcome (short transaction). A slow mail server therefore never blocks anything.

Delivery is "at least once": if the service crashes in the instant between the mail server
accepting a message and us recording that, the message is sent again after a restart. That is
rare, and a duplicate email is a better failure than a customer who never hears from us.
"""

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from ecom_common import Conflict, NotFound
from ecom_common.responses import paginated
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.notification import FAILED, QUEUED, SENT, Notification
from app.senders.base import Email
from app.templates import Rendered

logger = logging.getLogger(__name__)

# Deliberately simple: no spaces or line breaks (which would let someone inject mail headers)
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


class NotificationService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    async def commit(self) -> None:
        await self.session.commit()

    # ---- writing a notification (called by event handlers) -------------------------------
    async def queue(
        self,
        *,
        kind: str,
        dedupe_key: str,
        user_id: uuid.UUID | None,
        to_email: str | None,
        rendered: Rendered,
    ) -> Notification:
        existing = await self.session.scalar(
            select(Notification).where(Notification.dedupe_key == dedupe_key)
        )
        if existing is not None:
            return existing  # this customer was already told about this

        note = Notification(
            dedupe_key=dedupe_key,
            kind=kind,
            user_id=user_id,
            to_email=(to_email or "").strip(),
            subject=rendered.subject.replace("\n", " ")[:300],
            text_body=rendered.text,
            html_body=rendered.html,
            status=QUEUED,
            next_attempt_at=datetime.now(UTC),
        )
        if not note.to_email:
            self._give_up(note, "the event carried no email address")
        elif not _EMAIL.match(note.to_email):
            self._give_up(note, "the email address is not valid")
        self.session.add(note)
        return note

    @staticmethod
    def _give_up(note: Notification, error: str) -> None:
        note.status = FAILED
        note.last_error = error[:500]
        note.next_attempt_at = None
        logger.warning("Notification %s cannot be sent: %s", note.dedupe_key, error)

    # ---- delivering (called by the worker) -----------------------------------------------
    async def due(self, now: datetime | None = None, limit: int = 20) -> list[uuid.UUID]:
        now = now or datetime.now(UTC)
        rows = await self.session.scalars(
            select(Notification.id)
            .where(Notification.status == QUEUED, Notification.next_attempt_at <= now)
            .order_by(Notification.next_attempt_at)
            .limit(limit)
        )
        return list(rows)

    async def _lock(self, notification_id: uuid.UUID) -> Notification | None:
        stmt = (
            select(Notification)
            .where(Notification.id == notification_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self.session.scalar(stmt)

    async def begin_send(self, notification_id: uuid.UUID) -> Email | None:
        """Claim an attempt. Returns what to send, or None if it is no longer due."""
        note = await self._lock(notification_id)
        if note is None or note.status != QUEUED:
            return None
        note.attempts += 1
        # Push the next attempt out of the way while we send, so a second worker skips it
        note.next_attempt_at = datetime.now(UTC) + timedelta(minutes=5)
        return Email(
            to=note.to_email,
            subject=note.subject,
            text=note.text_body,
            html=note.html_body,
            from_address=self.settings.from_address,
        )

    async def record_sent(self, notification_id: uuid.UUID, message_id: str) -> None:
        note = await self._lock(notification_id)
        if note is not None and note.status == QUEUED:
            note.status = SENT
            note.sent_at = datetime.now(UTC)
            note.provider_message_id = message_id[:200]
            note.last_error = ""
            note.next_attempt_at = None

    async def record_failure(
        self, notification_id: uuid.UUID, error: str, *, permanent: bool
    ) -> None:
        note = await self._lock(notification_id)
        if note is None or note.status != QUEUED:
            return
        if permanent:
            self._give_up(note, error)
        elif note.attempts >= self.settings.max_attempts:
            self._give_up(note, f"gave up after {note.attempts} attempts: {error}")
        else:
            wait = min(
                self.settings.retry_base_seconds * 2 ** (note.attempts - 1),
                self.settings.retry_max_seconds,
            )
            note.last_error = error[:500]
            note.next_attempt_at = datetime.now(UTC) + timedelta(seconds=wait)

    # ---- reading and fixing ---------------------------------------------------------------
    async def get(
        self, notification_id: uuid.UUID, user_id: uuid.UUID, is_admin: bool
    ) -> Notification:
        note = await self.session.get(Notification, notification_id)
        # Someone else's email looks exactly like one that does not exist
        if note is None or (note.user_id != user_id and not is_admin):
            raise NotFound("Notification not found", message="Not found")
        return note

    async def list_notifications(
        self,
        *,
        user_id: uuid.UUID | None,
        status: str | None,
        kind: str | None,
        page: int,
        size: int,
    ) -> dict[str, Any]:
        conditions = []
        if user_id is not None:
            conditions.append(Notification.user_id == user_id)
        if status:
            conditions.append(Notification.status == status)
        if kind:
            conditions.append(Notification.kind == kind)
        total = await self.session.scalar(
            select(func.count()).select_from(Notification).where(*conditions)
        )
        rows = await self.session.scalars(
            select(Notification)
            .where(*conditions)
            .order_by(Notification.created_at.desc(), Notification.id)
            .offset((page - 1) * size)
            .limit(size)
        )
        return paginated(list(rows), total or 0, page, size)

    async def retry(self, notification_id: uuid.UUID) -> Notification:
        """An administrator sends a failed email again (after fixing the cause)."""
        note = await self._lock(notification_id)
        if note is None:
            raise NotFound("Notification not found", message="Not found")
        if note.status != FAILED:
            raise Conflict(f"Only failed notifications can be retried (this one is {note.status})")
        note.status = QUEUED
        note.attempts = 0
        note.last_error = ""
        note.next_attempt_at = datetime.now(UTC)
        return note
