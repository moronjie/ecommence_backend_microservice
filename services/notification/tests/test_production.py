from app.core.config import Settings

SAFE = {
    "internal_auth_secret": "x" * 40,
    "metrics_token": "m" * 20,
    "database_url": "postgresql+asyncpg://shop:a-strong-pw@db/n",
}


def test_console_only_email_is_refused_in_production():
    problems = Settings(environment="production", **SAFE).production_problems()
    assert any("console" in p for p in problems)


def test_a_development_mail_catcher_and_a_placeholder_sender_are_refused():
    problems = Settings(
        environment="production", email_provider="smtp", smtp_host="mailpit", **SAFE
    ).production_problems()
    assert any("development mail catcher" in p for p in problems)
    assert any("FROM_ADDRESS" in p for p in problems)  # shop@example.com


def test_a_real_provider_and_sender_are_accepted():
    ok = Settings(
        environment="production",
        email_provider="smtp",
        smtp_host="smtp.brevo.com",
        smtp_port=587,
        smtp_starttls=True,
        from_address="orders@mybrand.com.gh",
        **SAFE,
    )
    assert ok.production_problems() == []
