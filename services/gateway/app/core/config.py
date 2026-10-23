from ecom_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "gateway"

    # JWT (must match the auth service)
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"

    # Rate limiting: requests per window, per user (or per IP when anonymous)
    rate_limit_requests: int = 100
    rate_limit_window_seconds: int = 60

    # Upstream calls
    upstream_timeout_seconds: float = 10.0
    circuit_failure_threshold: int = 5
    circuit_recovery_seconds: float = 30.0

    cors_origins: list[str] = ["http://localhost:3000"]

    # Where each service lives. Locally: localhost ports. In Docker: container names.
    auth_url: str = "http://localhost:8001"
    user_url: str = "http://localhost:8002"
    product_url: str = "http://localhost:8003"
    inventory_url: str = "http://localhost:8004"
    cart_url: str = "http://localhost:8005"
    order_url: str = "http://localhost:8006"
    payment_url: str = "http://localhost:8007"
    notification_url: str = "http://localhost:8008"
    template_url: str = "http://localhost:8100"


settings = Settings()
