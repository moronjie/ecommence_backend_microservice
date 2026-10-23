"""Test sender: keeps what it was asked to send, and can be told to fail."""

import uuid

from app.senders.base import Email, PermanentSendError, TransientSendError


class MemorySender:
    name = "memory"

    def __init__(self) -> None:
        self.sent: list[Email] = []
        self.transient_failures = 0  # the next N sends fail as if the mail server were down
        self.refuse_with: str | None = None  # every send fails for good with this message

    async def send(self, email: Email) -> str:
        if self.transient_failures > 0:
            self.transient_failures -= 1
            raise TransientSendError("simulated outage")
        if self.refuse_with:
            raise PermanentSendError(self.refuse_with)
        self.sent.append(email)
        return f"mem-{uuid.uuid4().hex[:8]}"
