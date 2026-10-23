"""The SMTP sender, run against a REAL SMTP server (aiosmtpd) on a local port."""

import email
import logging
import smtplib
import socket
from email import policy

import pytest
from aiosmtpd.controller import Controller
from app.senders import smtp as smtp_module
from app.senders.base import Email, PermanentSendError, TransientSendError
from app.senders.console import ConsoleSender
from app.senders.smtp import SmtpSender

MAIL = Email(
    to="ama@example.com",
    subject="Order #A1B2C3D4 confirmed",
    text="Thank you! Total paid: GHS 240.00",
    html="<p>Thank you! <strong>GHS 240.00</strong></p>",
    from_address="shop@accracrafts.test",
)


class Inbox:
    """A tiny SMTP server that records what it receives and can be told to refuse."""

    def __init__(self):
        self.received = []
        self.rcpt_reply = "250 OK"

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if not self.rcpt_reply.startswith("250"):
            return self.rcpt_reply
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.received.append(envelope)
        return "250 Message accepted"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server():
    inbox, port = Inbox(), free_port()
    controller = Controller(inbox, hostname="127.0.0.1", port=port)
    controller.start()
    yield inbox, port
    controller.stop()


async def test_a_real_smtp_server_receives_a_proper_two_part_email(server):
    inbox, port = server
    message_id = await SmtpSender("127.0.0.1", port).send(MAIL)

    (envelope,) = inbox.received
    assert envelope.mail_from == "shop@accracrafts.test" and envelope.rcpt_tos == [
        "ama@example.com"
    ]
    parsed = email.message_from_bytes(envelope.content, policy=policy.default)
    assert (
        parsed["Subject"] == MAIL.subject
        and parsed["To"] == MAIL.to
        and parsed["From"] == MAIL.from_address
    )
    assert parsed["Message-ID"] == message_id and message_id.startswith("<")
    text = parsed.get_body(preferencelist=("plain",)).get_content()
    html = parsed.get_body(preferencelist=("html",)).get_content()
    assert "GHS 240.00" in text and "<strong>GHS 240.00</strong>" in html  # both versions are there


async def test_non_ascii_names_and_symbols_survive(server):
    inbox, port = server
    accented = Email(
        **{
            **MAIL.__dict__,
            "text": "Akwaaba, Kofi! Total: GH₵240.00",
            "html": "<p>GH₵240.00 — Akwaaba</p>",
        }
    )
    await SmtpSender("127.0.0.1", port).send(accented)
    parsed = email.message_from_bytes(inbox.received[0].content, policy=policy.default)
    assert "GH₵240.00" in parsed.get_body(preferencelist=("plain",)).get_content()


async def test_a_refused_recipient_is_permanent(server):
    inbox, port = server
    inbox.rcpt_reply = "550 5.1.1 No such user here"
    with pytest.raises(PermanentSendError, match="refused"):
        await SmtpSender("127.0.0.1", port).send(MAIL)


async def test_a_busy_server_is_transient(server):
    inbox, port = server
    inbox.rcpt_reply = "451 4.3.0 Try again later"
    with pytest.raises(TransientSendError, match="451"):
        await SmtpSender("127.0.0.1", port).send(MAIL)


async def test_a_server_that_is_down_is_transient():
    with pytest.raises(TransientSendError, match="could not reach"):
        await SmtpSender("127.0.0.1", free_port(), timeout=2).send(MAIL)  # nothing is listening


# ---- details a local server cannot show: login and encryption -------------------------
class FakeSmtp:
    calls: list = []

    def __init__(self, host, port, timeout=None):
        FakeSmtp.calls = [("connect", host, port)]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        FakeSmtp.calls.append(("starttls",))

    def login(self, user, password):
        FakeSmtp.calls.append(("login", user, password))

    def send_message(self, message):
        FakeSmtp.calls.append(("send", message["To"]))


async def test_it_encrypts_before_logging_in(monkeypatch):
    monkeypatch.setattr(smtp_module.smtplib, "SMTP", FakeSmtp)
    sender = SmtpSender("smtp.provider.test", 587, username="key", password="secret", starttls=True)
    await sender.send(MAIL)
    # the password must never travel before the connection is encrypted
    assert [c[0] for c in FakeSmtp.calls] == ["connect", "starttls", "login", "send"]
    assert FakeSmtp.calls[2] == ("login", "key", "secret")


async def test_no_login_when_no_username_is_set(monkeypatch):
    monkeypatch.setattr(smtp_module.smtplib, "SMTP", FakeSmtp)
    await SmtpSender("localhost", 1025).send(MAIL)
    assert [c[0] for c in FakeSmtp.calls] == ["connect", "send"]


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (smtplib.SMTPAuthenticationError(535, b"bad credentials"), PermanentSendError),
        (smtplib.SMTPSenderRefused(553, b"sender not allowed", "x@y.z"), PermanentSendError),
        (smtplib.SMTPDataError(554, b"rejected as spam"), PermanentSendError),
        (smtplib.SMTPDataError(452, b"insufficient storage"), TransientSendError),
        (smtplib.SMTPServerDisconnected("dropped"), TransientSendError),
        (TimeoutError("slow"), TransientSendError),
        (ConnectionResetError("reset"), TransientSendError),
    ],
)
async def test_failures_are_classified_by_whether_retrying_can_help(monkeypatch, raised, expected):
    class Broken(FakeSmtp):
        def send_message(self, message):
            raise raised

    monkeypatch.setattr(smtp_module.smtplib, "SMTP", Broken)
    with pytest.raises(expected):
        await SmtpSender("localhost", 1025).send(MAIL)


async def test_console_sender_shows_the_email_in_the_log(caplog):
    with caplog.at_level(logging.INFO, logger="email"):
        message_id = await ConsoleSender().send(MAIL)
    assert "ama@example.com" in caplog.text and "GHS 240.00" in caplog.text
    assert message_id.startswith("console-")
