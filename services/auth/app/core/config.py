from ecom_common.config import INSECURE_SECRETS, BaseServiceSettings
from ecom_common.keys import public_key_from_private, read_key


class Settings(BaseServiceSettings):
    service_name: str = "auth-service"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/auth_db"

    # How tokens are signed. The gateway must be set up to match.
    #   HS256: one shared secret (JWT_SECRET). Simple; fine for development.
    #   RS256: this service signs with a PRIVATE key (JWT_PRIVATE_KEY or JWT_PRIVATE_KEY_FILE) and
    #          nothing else ever holds it. The gateway gets only the public key, so it can check
    #          tokens but cannot create them. Use this in production (see scripts/generate_keys.py).
    jwt_algorithm: str = "HS256"
    jwt_secret: str = "change-me-in-production"
    jwt_private_key: str = ""
    jwt_private_key_file: str = ""
    access_token_minutes: int = 15
    refresh_token_days: int = 7

    # Too many wrong passwords for one email locks logins for that email for a while.
    # (Trade-off: someone can deliberately lock a victim out for 15 minutes. The alternative,
    # unlimited guessing, is worse, and the gateway also limits attempts per IP.)
    login_max_failures: int = 5
    login_lockout_seconds: int = 900

    outbox_relay_enabled: bool = True

    # Optional: create this admin on startup if it does not exist yet
    admin_email: str = ""
    admin_password: str = ""

    @property
    def _uses_rsa(self) -> bool:
        return not self.jwt_algorithm.upper().startswith("HS")

    @property
    def signing_key(self) -> str:
        """What tokens are signed with."""
        if not self._uses_rsa:
            return self.jwt_secret
        return read_key(self.jwt_private_key, self.jwt_private_key_file)

    @property
    def verification_key(self) -> str:
        """What this service checks its own tokens (refresh, logout) against."""
        if not self._uses_rsa:
            return self.jwt_secret
        return public_key_from_private(self.signing_key)

    def validate_keys(self) -> None:
        """Called at start-up so a bad key stops the service with a clear message, instead of
        every login failing with a vague 500 later."""
        if not self._uses_rsa:
            return
        try:
            key = self.signing_key
            if not key:
                raise ValueError("no key was provided")
            _ = self.verification_key  # loads the PEM: raises if it is not a private key
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"JWT_ALGORITHM={self.jwt_algorithm} but the signing key cannot be used ({exc}). "
                "Check JWT_PRIVATE_KEY or JWT_PRIVATE_KEY_FILE (a path that exists INSIDE the "
                "container). Create keys with: python scripts/generate_keys.py"
            ) from exc

    def production_problems(self) -> list[str]:
        problems = super().production_problems()
        if not self._uses_rsa:
            problems.append(
                "JWT_ALGORITHM must be RS256 (HS256 shares one secret with the gateway)"
            )
        elif not self.signing_key:
            problems.append("JWT_PRIVATE_KEY or JWT_PRIVATE_KEY_FILE must hold the signing key")
        if self.admin_password and self.admin_password in INSECURE_SECRETS | {"admin", "password"}:
            problems.append("ADMIN_PASSWORD is a well-known weak password")
        return problems


settings = Settings()
