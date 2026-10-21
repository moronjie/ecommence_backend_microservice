"""Paystack (https://paystack.com). Amounts are in the currency's minor unit: pesewas for GHS.

Written from Paystack's public API as I know it; check each call against their current docs
before going live, and try it with a TEST secret key first.
"""

import hashlib
import hmac
import logging
from typing import Any, Literal

import httpx

from app.providers.base import (
    InitResult,
    PermanentProviderError,
    RefundResult,
    TransientProviderError,
    Verification,
)

logger = logging.getLogger(__name__)

_VERIFY_STATUS: dict[str, Literal["success", "failed", "abandoned", "pending"]] = {
    "success": "success",
    "failed": "failed",
    "reversed": "failed",
    "abandoned": "abandoned",
}
_REFUND_STATUS: dict[str, Literal["pending", "processed", "failed"]] = {
    "processed": "processed",
    "failed": "failed",
    "needs-attention": "failed",
}


class PaystackProvider:
    name = "paystack"

    def __init__(self, secret_key: str, http: httpx.AsyncClient, base_url: str) -> None:
        self._secret = secret_key
        self._http = http
        self._base = base_url.rstrip("/")

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            resp = await self._http.request(
                method,
                f"{self._base}{path}",
                headers={"Authorization": f"Bearer {self._secret}"},
                **kwargs,
            )
        except httpx.HTTPError as exc:  # timeouts, connection problems
            raise TransientProviderError(f"Paystack unreachable: {exc!r}") from exc

        if resp.status_code >= 500 or resp.status_code == 429:
            raise TransientProviderError(f"Paystack answered {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise TransientProviderError("Paystack sent an unreadable answer") from exc
        if resp.status_code >= 400 or not body.get("status"):
            message = body.get("message", f"HTTP {resp.status_code}")
            if resp.status_code == 404:
                raise LookupError(message)  # callers decide what "not found" means
            raise PermanentProviderError(message)
        return body["data"]

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
        payload: dict[str, Any] = {
            "email": email,
            "amount": amount_cents,
            "currency": currency,
            "reference": reference,
            "metadata": metadata,
        }
        if callback_url:
            payload["callback_url"] = callback_url
        if channels:
            payload["channels"] = channels
        try:
            data = await self._call("POST", "/transaction/initialize", json=payload)
        except LookupError as exc:
            raise PermanentProviderError(str(exc)) from exc
        return InitResult(data["authorization_url"], data.get("access_code", ""))

    async def verify(self, reference: str) -> Verification:
        try:
            data = await self._call("GET", f"/transaction/verify/{reference}")
        except LookupError:
            return Verification(status="pending")  # the customer has not started paying yet
        return Verification(
            status=_VERIFY_STATUS.get(data.get("status", ""), "pending"),
            amount_cents=int(data.get("amount", 0)),
            currency=str(data.get("currency", "")).upper(),
            channel=str(data.get("channel", "")),
            transaction_id=str(data.get("id", "")),
            reason=str(data.get("gateway_response", "")),
        )

    async def refund(self, *, reference: str, amount_cents: int, reason: str) -> RefundResult:
        try:
            data = await self._call(
                "POST",
                "/refund",
                json={"transaction": reference, "amount": amount_cents, "merchant_note": reason},
            )
        except LookupError as exc:
            raise PermanentProviderError(str(exc)) from exc
        return RefundResult(
            refund_id=str(data.get("id", "")),
            status=_REFUND_STATUS.get(str(data.get("status", "")), "pending"),
        )

    def valid_signature(self, body: bytes, signature: str | None) -> bool:
        """Paystack signs every webhook: HMAC-SHA512 of the raw body, keyed with the secret key."""
        if not signature:
            return False
        expected = hmac.new(self._secret.encode(), body, hashlib.sha512).hexdigest()
        return hmac.compare_digest(expected, signature)
