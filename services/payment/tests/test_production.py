from app.core.config import Settings

SAFE = {
    "internal_auth_secret": "x" * 40,
    "metrics_token": "m" * 20,
    "database_url": "postgresql+asyncpg://shop:a-strong-pw@db/p",
}


def test_the_fake_provider_may_not_run_in_production():
    problems = Settings(environment="production", **SAFE).production_problems()
    assert any("fake moves no real money" in p for p in problems)


def test_production_needs_a_live_paystack_key_not_a_test_one():
    missing = Settings(environment="production", provider="paystack", **SAFE).production_problems()
    assert any("PAYSTACK_SECRET_KEY is missing" in p for p in missing)
    test_key = Settings(
        environment="production", provider="paystack", paystack_secret_key="sk_test_abc", **SAFE
    ).production_problems()
    assert any("TEST key" in p for p in test_key)


def test_a_live_key_is_accepted():
    live = Settings(
        environment="production", provider="paystack", paystack_secret_key="sk_live_abc", **SAFE
    )
    assert live.production_problems() == []
    live.assert_safe_for_environment()


def test_development_is_never_blocked():
    Settings(environment="local").assert_safe_for_environment()
