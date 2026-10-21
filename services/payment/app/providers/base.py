"""What the rest of the service needs from a payment provider. Paystack is one implementation."""

from dataclasses import dataclass
from typing import Any, Literal, Protocol


class ProviderError(Exception):
    pass


class TransientProviderError(ProviderError):
    """Could not get an answer (network, timeout, provider outage). Safe to try again later."""


class PermanentProviderError(ProviderError):
    """The provider understood and said no (bad request, unsupported currency, bad key)."""


@dataclass(frozen=True)
class InitResult:
    authorization_url: str  # where to send the customer to pay
    access_code: str


@dataclass(frozen=True)
class Verification:
    """The provider's own account of one payment."""

    status: Literal["success", "failed", "abandoned", "pending"]
    amount_cents: int = 0
    currency: str = ""
    channel: str = ""
    transaction_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class RefundResult:
    refund_id: str
    status: Literal["pending", "processed", "failed"]


class PaymentProvider(Protocol):
    name: str

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
    ) -> InitResult: ...

    async def verify(self, reference: str) -> Verification: ...

    async def refund(self, *, reference: str, amount_cents: int, reason: str) -> RefundResult: ...

    def valid_signature(self, body: bytes, signature: str | None) -> bool: ...
