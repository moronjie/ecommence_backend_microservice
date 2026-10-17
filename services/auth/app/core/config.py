from ecom_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "auth-service"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/auth_db"

    # Must match the gateway's JWT_SECRET / JWT_ALGORITHM
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 7

    outbox_relay_enabled: bool = True

    # Optional: create this admin on startup if it does not exist yet
    admin_email: str = ""
    admin_password: str = ""


settings = Settings()
