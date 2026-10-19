from ecom_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "product-service"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/product_db"

    outbox_relay_enabled: bool = True

    # Cache lifetimes. Invalidation on writes keeps data fresh; the TTL is the safety net.
    product_cache_ttl: int = 300
    list_cache_ttl: int = 60
    category_cache_ttl: int = 600


settings = Settings()
