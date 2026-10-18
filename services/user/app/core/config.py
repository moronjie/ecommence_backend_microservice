from ecom_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "user-service"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/user_db"

    consumer_enabled: bool = True
    consumer_group: str = "user-service"
    max_addresses: int = 10


settings = Settings()
