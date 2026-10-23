"""What the service needs from whatever actually delivers email."""

from dataclasses import dataclass
from typing import Protocol


class SendError(Exception):
    pass


class TransientSendError(SendError):
    """Could not deliver right now (server down, timeout, busy). Worth trying again later."""


class PermanentSendError(SendError):
    """Will never work as it is (address refused, bad password). Retrying would only repeat it."""


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    text: str
    html: str
    from_address: str


class EmailSender(Protocol):
    name: str

    async def send(self, email: Email) -> str:
        """Deliver the email. Returns an id for the message. Raises a SendError on failure."""
        ...
