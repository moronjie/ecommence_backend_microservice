from ecom_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "order-service"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/order_db"

    outbox_relay_enabled: bool = True
    consumer_enabled: bool = True
    consumer_group: str = "order-service"

    # Safety nets for orders that never get an answer from another service
    pending_timeout_minutes: int = 10  # waiting for the stock check
    awaiting_payment_timeout_minutes: int = 30  # waiting for payment
    sweep_interval_seconds: float = 60.0


settings = Settings()
