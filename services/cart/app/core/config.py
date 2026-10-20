from ecom_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "cart-service"

    consumer_enabled: bool = True
    consumer_group: str = "cart-service"

    cart_ttl_days: int = 30  # an untouched cart disappears after this long
    max_lines: int = 50  # different products per cart
    max_per_line: int = 99  # units of one product

    # Other services, reached directly on the internal network (not through the gateway)
    product_url: str = "http://localhost:8003"
    inventory_url: str = "http://localhost:8004"
    user_url: str = "http://localhost:8002"
    http_timeout_seconds: float = 3.0


settings = Settings()
