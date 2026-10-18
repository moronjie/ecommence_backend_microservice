# E-Commerce Microservices (FastAPI)

FastAPI microservices with PostgreSQL, Kafka, Redis and an API gateway. See [plan.md](plan.md) for the architecture and phases.

## Quick start
```bash
make install     # venv + shared lib + template service
make test        # unit tests
make up          # postgres, redis, kafka, kafka-ui, template service
curl localhost:8100/health
curl localhost:8100/api/v1/template/items/1   # uniform error response
```
Kafka UI: http://localhost:8080

## Uniform response
Every endpoint returns `{"success": bool, "message": str, "data": dict, "error_message": str}`.
Use `success_response(...)` or raise `NotFound` / `Conflict` / etc. from `ecom_common`.

## Adding a service
Copy `services/_template`, rename it, add a Dockerfile and compose entry, add a gateway route.

## Gateway (Phase 1)
All client traffic goes through `services/gateway` (port 8000). It routes by path prefix (see
`services/gateway/app/core/routes.py`), verifies the JWT, rate-limits per user/IP with Redis,
forwards `X-User-Id` / `X-User-Role` / `X-Request-ID`, and maps upstream failures to uniform
401 / 404 / 429 / 502 / 503 / 504 responses. `GET /health/services` reports every upstream.

Run locally (needs Redis for rate limiting; if Redis is down the gateway lets requests through):
```bash
cd services/gateway && python -m uvicorn app.main:app --reload --port 8000
cd services/_template && python -m uvicorn app.main:app --reload --port 8100   # stub upstream
curl localhost:8000/api/v1/template/ping
```

## Auth service (Phase 2)
`services/auth` (port 8001): `POST /api/v1/auth/register | login | refresh | logout`, `GET /me`.
Passwords use argon2. Access tokens last 15 min; refresh tokens last 7 days and are single-use
(rotated; used or logged-out tokens are denylisted in Redis). Registration writes a
`user.registered` event to an outbox table in the same transaction; a background relay publishes
it to Kafka (events queue safely if Kafka is down). Set `ADMIN_EMAIL`/`ADMIN_PASSWORD` to create
an admin on startup.

Run locally (from the service folder, because every service has a package named `app`):
```bash
cd services/auth
python -m alembic upgrade head          # needs Postgres (docker compose up -d postgres)
python -m uvicorn app.main:app --reload --port 8001
```
Never `pip install` a service into the shared venv; `make install` only installs the shared lib
and third-party packages.

## User service (Phase 3)
`services/user` (port 8002, route `/api/v1/users`): `GET/PATCH /me`, addresses under
`/me/addresses` (add, update, delete; the first becomes the default; max 10), and admin-only
`GET /` (paginated) and `GET /{user_id}`. The role comes from the gateway's `X-User-Role` header.

The profile is **not** created by an API call. The service consumes `user.registered` from Kafka
and creates it, so there is a short delay after sign-up during which `GET /me` returns 404
("try again in a moment"). Handling is idempotent (`processed_events` table), failures are retried
3 times, then the event is parked on `user.registered.dlq`.

Running a service locally: `docker compose` now publishes only the gateway (port 8000). The
`docker-compose.override.yml` file (loaded automatically) also opens Postgres, Redis, Kafka and the
services on localhost for development. For a locked-down run use `docker compose -f docker-compose.yml up`.
