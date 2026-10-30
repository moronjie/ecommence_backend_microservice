# End-to-end tests

These run a real checkout through the whole stack: gateway, every service, Postgres, Redis, **real Kafka**,
and emails caught by Mailpit. Nothing is simulated, so they are slower than the unit tests (about a minute) and
they need Docker.

```bash
# 1. start the shop (an admin account is created from these two variables)
ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD='Adm1n-Passw0rd!' docker compose up -d --build

# 2. run the tests against the gateway
E2E_BASE_URL=http://localhost:8000 E2E_ADMIN_EMAIL=admin@example.com E2E_ADMIN_PASSWORD='Adm1n-Passw0rd!' \
  python -m pytest tests/e2e -v
```

Without `E2E_BASE_URL` they are skipped, so a plain `pytest` is unaffected. Mailpit's address defaults to
`http://localhost:8025` (`E2E_MAILPIT_URL` to change it). The tests create their own customers and products with
random names, so you can run them as often as you like against the same stack.

Services talk through Kafka, so results appear a moment later; the tests wait for them (`eventually`) and fail with a
clear message about *what* never happened if it takes longer than 45 seconds.

CI runs these on every push (see `.github/workflows/ci.yml`, job `e2e`).
