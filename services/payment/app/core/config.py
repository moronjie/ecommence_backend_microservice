from typing import Literal

from ecom_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "payment-service"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/payment_db"

    outbox_relay_enabled: bool = True
    consumer_enabled: bool = True
    consumer_group: str = "payment-service"

    # "fake" runs without any account (local development and tests).
    # "paystack" talks to the real API and needs PAYSTACK_SECRET_KEY (use a TEST key first).
    provider: Literal["fake", "paystack"] = "fake"
    paystack_secret_key: str = ""
    paystack_base_url: str = "https://api.paystack.co"
    paystack_timeout_seconds: float = 15.0
    # Where Paystack sends the customer after paying (your shop's "thank you" page). Optional.
    payment_callback_url: str = ""
    # Mobile money matters most in Ghana; cards are the other main way to pay
    payment_channels: list[str] = ["card", "mobile_money"]

    # Only these currencies are accepted. Ghana: GHS.
    supported_currencies: list[str] = ["GHS"]

    # Retries and timings
    max_attempts: int = 3  # tries to start a payment, or to send a refund
    reconcile_after_seconds: float = 30.0  # how often one pending payment is double-checked
    payment_expiry_minutes: int = 25  # give up on a payment the customer never completed
    worker_interval_seconds: float = 3.0

    def production_problems(self) -> list[str]:
        problems = super().production_problems()
        if self.provider == "fake":
            problems.append(
                "PAYMENT_PROVIDER=fake moves no real money "
                "(set it to paystack and add PAYSTACK_SECRET_KEY)"
            )
        elif not self.paystack_secret_key:
            problems.append("PAYSTACK_SECRET_KEY is missing")
        elif self.paystack_secret_key.startswith("sk_test_"):
            problems.append(
                "PAYSTACK_SECRET_KEY is a TEST key; production needs a live key (sk_live_...)"
            )
        return problems


settings = Settings()
