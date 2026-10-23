"""Development sender: shows the email in the log instead of sending it."""

import logging
import uuid

from app.senders.base import Email

logger = logging.getLogger("email")


class ConsoleSender:
    name = "console"

    async def send(self, email: Email) -> str:
        logger.info(
            "EMAIL (not sent, console mode)\nTo: %s\nFrom: %s\nSubject: %s\n\n%s",
            email.to,
            email.from_address,
            email.subject,
            email.text,
        )
        return f"console-{uuid.uuid4().hex[:12]}"
