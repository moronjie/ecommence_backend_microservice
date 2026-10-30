from cryptography.hazmat.primitives import serialization
from ecom_common.config import INSECURE_SECRETS, BaseServiceSettings
from ecom_common.keys import read_key


class Settings(BaseServiceSettings):
    service_name: str = "gateway"

    # How tokens are checked. Must match what the auth service signs with.
    #   HS256: one shared secret (JWT_SECRET). Simple, but anything that can check a token can
    #          also forge one. Fine for development.
    #   RS256: the auth service signs with a PRIVATE key; this gateway only holds the PUBLIC key
    #          (JWT_PUBLIC_KEY or JWT_PUBLIC_KEY_FILE), so it cannot forge tokens. For production.
    jwt_algorithm: str = "HS256"
    jwt_secret: str = "change-me-in-production"
    jwt_public_key: str = ""
    jwt_public_key_file: str = ""

    # Rate limiting: requests per window, per user (or per IP when anonymous)
    rate_limit_requests: int = 100
    rate_limit_window_seconds: int = 60

    # Stricter limits per IP on the routes attackers hammer (guessing passwords, mass sign-ups)
    login_rate_limit: int = 10
    register_rate_limit: int = 5
    refresh_rate_limit: int = 30
    auth_rate_window_seconds: int = 60

    # Upstream calls
    upstream_timeout_seconds: float = 10.0
    circuit_failure_threshold: int = 5
    circuit_recovery_seconds: float = 30.0

    cors_origins: list[str] = ["http://localhost:3000"]

    # The combined API docs page at /docs. On for development; OFF in production unless you set
    # DOCS_ENABLED=true, because it lists every endpoint, admin ones included, to anyone who asks.
    docs_enabled: bool | None = None

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

    @property
    def docs_active(self) -> bool:
        if self.docs_enabled is not None:
            return self.docs_enabled
        return self.environment.lower() != "production"

    @property
    def verification_key(self) -> str:
        """What a token's signature is checked against."""
        if self.jwt_algorithm.upper().startswith("HS"):
            return self.jwt_secret
        return read_key(self.jwt_public_key, self.jwt_public_key_file)

    def validate_keys(self) -> None:
        """Called at start-up: a missing or broken public key must stop the gateway at once,
        not make every authenticated request fail later."""
        if self.jwt_algorithm.upper().startswith("HS"):
            return
        try:
            key = self.verification_key
            if not key:
                raise ValueError("no key was provided")
            serialization.load_pem_public_key(key.encode())
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"JWT_ALGORITHM={self.jwt_algorithm} but the verification key cannot be used "
                f"({exc}). Check JWT_PUBLIC_KEY or JWT_PUBLIC_KEY_FILE (a path that exists "
                "INSIDE the container). Create keys with: python scripts/generate_keys.py"
            ) from exc

    def production_problems(self) -> list[str]:
        problems = super().production_problems()
        if not self.jwt_algorithm.upper().startswith("RS"):
            problems.append("JWT_ALGORITHM must be RS256 (HS256 lets the gateway forge tokens)")
        elif not self.verification_key:
            problems.append(
                "JWT_PUBLIC_KEY or JWT_PUBLIC_KEY_FILE must hold the auth service's public key"
            )
        if self.jwt_secret in INSECURE_SECRETS and self.jwt_algorithm.upper().startswith("HS"):
            problems.append("JWT_SECRET is still the default")
        return problems


settings = Settings()
