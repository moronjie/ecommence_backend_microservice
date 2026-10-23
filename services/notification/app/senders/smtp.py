"""Sends through any SMTP server: Mailpit while developing, a real provider (Gmail, Brevo,
Mailgun, SendGrid, Resend... all offer SMTP) in production.

``smtplib`` is blocking, so it runs in a thread and never stalls the event loop.
"""

import asyncio
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

from app.senders.base import Email, PermanentSendError, SendError, TransientSendError


def _classify(exc: Exception) -> SendError:
    """Decide whether trying again could help."""
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        # Python raises this for any recipient problem, "no such user" (5xx) and "try again
        # later" (4xx) alike. Only give up if every refusal was a definite no.
        codes = [code for code, _ in exc.recipients.values()]
        if codes and all(code >= 500 for code in codes):
            return PermanentSendError(f"the mail server refused the address: {exc}")
        return TransientSendError(f"the mail server could not take the address yet: {exc}")
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        # Wrong login: retrying the same login just repeats the failure. A person must fix it.
        return PermanentSendError(f"the mail server rejected our login: {exc}")
    if isinstance(exc, smtplib.SMTPResponseException):
        # (includes a refused sender) 4xx means "try again later", 5xx means "no"
        error = PermanentSendError if exc.smtp_code >= 500 else TransientSendError
        return error(f"SMTP {exc.smtp_code}: {exc.smtp_error!r}")
    return TransientSendError(f"could not reach the mail server: {exc!r}")  # network, timeout...


class SmtpSender:
    name = "smtp"

    def __init__(
        self,
        host: str,
        port: int,
        *,
        username: str = "",
        password: str = "",
        starttls: bool = False,
        timeout: float = 15.0,
    ) -> None:
        self._host, self._port = host, port
        self._username, self._password = username, password
        self._starttls, self._timeout = starttls, timeout

    def _build(self, email: Email) -> EmailMessage:
        message = EmailMessage()
        message["From"] = email.from_address
        message["To"] = email.to
        message["Subject"] = email.subject
        message["Message-ID"] = make_msgid(domain=email.from_address.rpartition("@")[2] or None)
        message.set_content(email.text)  # plain-text version for clients that need it
        message.add_alternative(email.html, subtype="html")
        return message

    def _send_blocking(self, email: Email) -> str:
        message = self._build(email)
        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as server:
                if self._starttls:
                    server.starttls()
                if self._username:
                    server.login(self._username, self._password)
                server.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise _classify(exc) from exc
        return str(message["Message-ID"])

    async def send(self, email: Email) -> str:
        return await asyncio.to_thread(self._send_blocking, email)
