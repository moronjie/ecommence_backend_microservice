from typing import Literal

from ecom_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "notification-service"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/notification_db"

    consumer_enabled: bool = True
    consumer_group: str = "notification-service"

    # "console" only writes the email to the log (local development, nothing is sent).
    # "smtp" really sends it, via the server below (Mailpit locally, your provider in production).
    email_provider: Literal["console", "smtp"] = "console"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False  # True for most real providers (port 587)
    smtp_timeout_seconds: float = 15.0

    # How the shop appears in the emails
    shop_name: str = "Our Shop"
    from_address: str = "shop@example.com"
    support_email: str = "support@example.com"

    # Sending is retried with growing waits: 30s, 60s, 120s ... then the message is marked failed
    max_attempts: int = 5
    retry_base_seconds: float = 30.0
    retry_max_seconds: float = 3600.0
    worker_interval_seconds: float = 2.0

    def production_problems(self) -> list[str]:
        problems = super().production_problems()
        if self.email_provider == "console":
            problems.append("EMAIL_PROVIDER=console only logs emails; customers would get nothing")
        elif self.smtp_host in {"localhost", "127.0.0.1", "mailpit"}:
            problems.append("SMTP_HOST points at a development mail catcher, not a real provider")
        if self.from_address.endswith("@example.com"):
            problems.append("FROM_ADDRESS must be an address on a domain you own")
        return problems


settings = Settings()
