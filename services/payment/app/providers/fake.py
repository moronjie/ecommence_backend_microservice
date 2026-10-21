"""A pretend provider for local development and tests. No network, no account needed.

A payment stays "pending" until someone calls ``simulate`` (in development, through the
``/dev`` endpoint; in tests, directly), which plays the part of the customer paying.
"""

import hashlib
import hmac
from typing import Any

from app.providers.base import (
    InitResult,
    PermanentProviderError,
    RefundResult,
    TransientProviderError,
    Verification,
)

FAKE_SECRET = "fake-webhook-secret"


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.initialized: list[dict[str, Any]] = []
        self.refunds: list[dict[str, Any]] = []
        self._outcomes: dict[str, Verification] = {}
        # knobs for tests
        self.transient_failures = 0  # next N provider calls fail as if the network were down
        self.reject_with: str | None = None  # make initialize() fail permanently
        self.refund_status = "pending"
        self.refund_rejects: str | None = None

    def _maybe_fail(self) -> None:
        if self.transient_failures > 0:
            self.transient_failures -= 1
            raise TransientProviderError("simulated outage")

    async def initialize(
        self,
        *,
        reference: str,
        amount_cents: int,
        currency: str,
        email: str,
        metadata: dict[str, Any],
        callback_url: str,
        channels: list[str],
    ) -> InitResult:
        self._maybe_fail()
        if self.reject_with:
            raise PermanentProviderError(self.reject_with)
        self.initialized.append(
            {
                "reference": reference,
                "amount_cents": amount_cents,
                "currency": currency,
                "email": email,
                "metadata": metadata,
                "channels": channels,
            }
        )
        self._outcomes[reference] = Verification(status="pending")
        return InitResult(f"https://fake-pay.local/pay/{reference}", f"code-{reference}")

    async def verify(self, reference: str) -> Verification:
        self._maybe_fail()
        return self._outcomes.get(reference, Verification(status="pending"))

    async def refund(self, *, reference: str, amount_cents: int, reason: str) -> RefundResult:
        self._maybe_fail()
        if self.refund_rejects:
            raise PermanentProviderError(self.refund_rejects)
        self.refunds.append({"reference": reference, "amount_cents": amount_cents})
        return RefundResult(f"refund-{len(self.refunds)}", self.refund_status)  # type: ignore[arg-type]

    def valid_signature(self, body: bytes, signature: str | None) -> bool:
        expected = hmac.new(FAKE_SECRET.encode(), body, hashlib.sha512).hexdigest()
        return bool(signature) and hmac.compare_digest(expected, signature or "")

    # ---- playing the customer ---------------------------------------------------------
    def simulate(
        self,
        reference: str,
        status: str = "success",
        *,
        amount_cents: int | None = None,
        currency: str | None = None,
        channel: str = "mobile_money",
    ) -> None:
        paid = next((i for i in self.initialized if i["reference"] == reference), None)
        self._outcomes[reference] = Verification(
            status=status,  # type: ignore[arg-type]
            amount_cents=amount_cents
            if amount_cents is not None
            else (paid or {}).get("amount_cents", 0),
            currency=currency or (paid or {}).get("currency", "GHS"),
            channel=channel,
            transaction_id=f"tx-{reference}",
            reason="Approved" if status == "success" else "Declined",
        )
