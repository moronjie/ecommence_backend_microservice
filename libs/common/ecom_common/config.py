from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_SECRETS = {"", "change-me-in-production", "dev-internal-secret", "secret", "changeme"}


class BaseServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "service"
    environment: str = "local"
    debug: bool = False
    log_level: str = "INFO"

    database_url: str = ""
    redis_url: str = "redis://localhost:6379/0"
    kafka_bootstrap_servers: str = "localhost:9092"

    internal_auth_secret: str = ""

    # If set, /metrics only answers requests carrying "Authorization: Bearer <this token>". The
    # gateway sits on the public port, so without it anybody could read your traffic statistics.
    metrics_token: str = ""

    def production_problems(self) -> list[str]:
        """Things that must be fixed before this service may run with ENVIRONMENT=production.
        Services add their own checks by extending this list."""
        problems = []
        if self.internal_auth_secret in INSECURE_SECRETS or len(self.internal_auth_secret) < 32:
            problems.append(
                "INTERNAL_AUTH_SECRET must be set to a random value of at least 32 characters"
            )
        if self.database_url and ":postgres@" in self.database_url:
            problems.append("DATABASE_URL still uses the default 'postgres' password")
        if len(self.metrics_token) < 16:
            problems.append("METRICS_TOKEN must be set (16+ characters): it protects /metrics")
        if self.debug:
            problems.append("DEBUG must be off")
        return problems

    def assert_safe_for_environment(self) -> None:
        if self.environment.lower() != "production":
            return
        problems = self.production_problems()
        if problems:
            raise RuntimeError(
                f"{self.service_name} refuses to start in production:\n  - "
                + "\n  - ".join(problems)
            )
