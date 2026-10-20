from ecom_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "inventory-service"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/inventory_db"

    outbox_relay_enabled: bool = True
    consumer_enabled: bool = True
    consumer_group: str = "inventory-service"

    # Stock held for an order that is never paid is given back after this long
    reservation_ttl_minutes: int = 15
    expiry_interval_seconds: float = 30.0


settings = Settings()
