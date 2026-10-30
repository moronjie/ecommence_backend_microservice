import os
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest

BASE_URL = os.environ.get("E2E_BASE_URL", "")
MAILPIT_URL = os.environ.get("E2E_MAILPIT_URL", "http://localhost:8025")
ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", os.environ.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", os.environ.get("ADMIN_PASSWORD", ""))
PASSWORD = "S3cure-passw0rd"

pytestmark = pytest.mark.skipif(not BASE_URL, reason="set E2E_BASE_URL to run against a live stack")


def eventually(check: Callable[[], Any], what: str, timeout: float = 45, every: float = 0.5) -> Any:
    """Services talk through Kafka, so results appear a moment later. Wait for them, but never
    forever: the last value (and a clear message) is reported if it never happens."""
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        try:
            last = check()
            if last:
                return last
        except (httpx.HTTPError, KeyError, IndexError, AssertionError) as exc:
            last = exc
        time.sleep(every)
    pytest.fail(f"Timed out after {timeout}s waiting for: {what} (last result: {last!r})")


class Shop:
    """A tiny client for the shop's public API."""

    def __init__(self) -> None:
        self.http = httpx.Client(base_url=BASE_URL, timeout=20)

    def call(self, method: str, path: str, token: str | None = None, **kw: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        headers.update(kw.pop("headers", {}))  # extra headers are added, not a second argument
        return self.http.request(method, path, headers=headers, **kw)

    def data(
        self, method: str, path: str, token: str | None = None, ok: int = 200, **kw: Any
    ) -> Any:
        resp = self.call(method, path, token, **kw)
        assert resp.status_code == ok, f"{method} {path} -> {resp.status_code} {resp.text}"
        body = resp.json()
        assert set(body) == {"success", "message", "data", "error_message"}  # the uniform response
        return body["data"]

    def login(self, email: str, password: str = PASSWORD) -> str:
        return self.data("POST", "/api/v1/auth/login", json={"email": email, "password": password})[
            "access_token"
        ]

    def new_customer(self) -> tuple[str, str]:
        """Register a fresh customer. Returns (email, token)."""
        email = f"e2e-{uuid.uuid4().hex[:10]}@example.com"
        self.data(
            "POST",
            "/api/v1/auth/register",
            ok=201,
            json={"email": email, "password": PASSWORD, "full_name": "E2E Customer"},
        )
        return email, self.login(email)


@pytest.fixture(scope="session")
def shop() -> Shop:
    client = Shop()
    eventually(
        lambda: client.http.get("/health/services").status_code == 200,
        "every service to report healthy through the gateway",
        timeout=120,
        every=2,
    )
    return client


@pytest.fixture(scope="session")
def admin_token(shop: Shop) -> str:
    if not ADMIN_PASSWORD:
        pytest.skip("set E2E_ADMIN_PASSWORD (and start the stack with the same ADMIN_PASSWORD)")
    return shop.login(ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture
def mailbox() -> Callable[[str], list[dict]]:
    """Emails Mailpit has caught for an address."""

    def messages_for(address: str) -> list[dict]:
        resp = httpx.get(f"{MAILPIT_URL}/api/v1/messages", params={"limit": 500}, timeout=10)
        resp.raise_for_status()
        return [
            m
            for m in resp.json().get("messages", [])
            if any(t["Address"] == address for t in m.get("To", []))
        ]

    return messages_for
