# E-Commerce Microservices Backend — Plan

## Context
Greenfield project (folder only contains an empty `main.py`). Goal: an e-commerce backend built as FastAPI microservices — PostgreSQL (DB), Kafka (events), Redis (cache + rate limiting), fronted by an API gateway. Built phase by phase, one service per phase. It should be approachable for a beginner (clear structure, readable code, docs) yet demonstrate senior-level practices (clean layering, event-driven design, idempotency, outbox pattern, observability, tests, CI).

On approval, this file is copied to `plan.md` in the project root and Phase 0 is scaffolded.

## Architecture
```
Client ─► Gateway (:8000) ─┬─► auth-service     (:8001)  DB: auth_db
         JWT verify        ├─► user-service     (:8002)  DB: user_db
         rate limit(Redis) ├─► product-service  (:8003)  DB: product_db   + Redis cache
         routing/CORS      ├─► inventory-service(:8004)  DB: inventory_db
                           ├─► cart-service     (:8005)  Redis (primary) / DB
                           ├─► order-service    (:8006)  DB: order_db
                           ├─► payment-service  (:8007)  DB: payment_db
                           └─► notification-svc (:8008)  Kafka consumer only
                         Kafka (events between services)
```
Rules: **database-per-service** (one Postgres instance, separate databases/schemas locally), no cross-service DB joins, sync HTTP only via gateway for clients, **async Kafka events between services**.

## Tech choices
- Python 3.12, FastAPI, Pydantic v2 + pydantic-settings
- SQLAlchemy 2.0 (async) + asyncpg, Alembic migrations (per service)
- Kafka via `aiokafka`; Redis via `redis-py` (asyncio)
- Auth: JWT (access + refresh), argon2/bcrypt hashing
- Gateway: custom FastAPI + `httpx` reverse proxy (teaches more than Kong/Traefik; can be swapped later)
- Tooling: `uv` (or pip + requirements), ruff, mypy, pytest + pytest-asyncio + testcontainers, pre-commit
- Docker Compose for everything; GitHub Actions CI

## Folder structure
```
e-commence/
├── plan.md
├── README.md
├── docker-compose.yml            # postgres, redis, kafka(KRaft), kafka-ui, all services
├── docker-compose.override.yml   # dev: hot reload, volumes
├── Makefile                      # make up / down / test / lint / migrate
├── .env.example
├── pyproject.toml                # ruff/mypy/pytest config (root)
├── .github/workflows/ci.yml
├── infra/
│   ├── postgres/init-databases.sql   # creates one DB per service
│   └── kafka/create-topics.sh
├── docs/                          # architecture.md, events.md (topic catalogue), adr/
├── libs/
│   └── common/                    # shared package `ecom_common` (installed into each service)
│       └── ecom_common/
│           ├── responses.py       # uniform response (below)
│           ├── exceptions.py      # AppException hierarchy + handlers
│           ├── config.py          # BaseServiceSettings
│           ├── logging.py         # structured JSON logging + request-id
│           ├── middleware.py      # request-id, timing, exception middleware
│           ├── db.py              # async engine/session factory, Base
│           ├── kafka.py           # Producer/Consumer wrappers, event envelope
│           ├── redis.py           # client factory, cache decorator, rate limiter
│           ├── security.py        # JWT encode/decode, password hashing
│           ├── pagination.py
│           └── health.py          # /health, /ready routers
├── services/
│   ├── gateway/
│   ├── auth/  user/  product/  inventory/  cart/  order/  payment/  notification/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   ├── alembic.ini + migrations/
│   │   ├── app/
│   │   │   ├── main.py            # app factory, lifespan
│   │   │   ├── core/              # config.py, dependencies.py
│   │   │   ├── api/v1/            # routers (thin: validate → call service → respond)
│   │   │   ├── schemas/           # Pydantic request/response models
│   │   │   ├── models/            # SQLAlchemy models
│   │   │   ├── repositories/      # DB access only
│   │   │   ├── services/          # business logic
│   │   │   ├── events/            # producers.py, consumers.py, handlers
│   │   │   └── tasks/             # outbox relay, background jobs
│   │   └── tests/ (unit/, integration/)
```
Layering inside each service: **router → service → repository → model**. Routers never touch the DB; services never build HTTP responses.

## Uniform response (in `libs/common/ecom_common/responses.py`)
Every endpoint (success and error) returns:
```json
{ "success": true, "message": "Product created", "data": {}, "error_message": "" }
```
- `ApiResponse[T]` generic Pydantic model: `success: bool`, `message: str`, `data: T | dict | None` (default `{}`), `error_message: str | None` (default `""`).
- Helpers: `success_response(data, message, status_code=200)` and `error_response(message, error_message, status_code)` returning `JSONResponse`.
- Global exception handlers registered by `register_exception_handlers(app)`: `AppException` (custom `NotFound`, `Conflict`, `Unauthorized`, `Forbidden`, `ValidationFailed`), `RequestValidationError` (422, field errors summarised in `error_message`, details in `data`), `HTTPException`, and catch-all `Exception` (500, generic message, details only logged).
- Lists: `data = {"items": [...], "total": n, "page": p, "size": s}` so `data` is always a dict.
- Gateway passes downstream responses through unchanged, and produces the same shape for its own errors (401, 429, 502/503/504).
- Routers declare `response_model=ApiResponse[XSchema]` so OpenAPI stays accurate.

## Cross-cutting (senior-level) features
- **Event envelope**: `{event_id, event_type, version, occurred_at, correlation_id, payload}`; topics named `<domain>.<event>` (e.g. `order.created`); documented in `docs/events.md`.
- **Transactional outbox** (order, payment, inventory): write event row in the same DB transaction, a relay publishes to Kafka → no dual-write loss.
- **Idempotent consumers**: `processed_events` table keyed by `event_id`; retries + dead-letter topic (`*.dlq`).
- **Saga (choreography)** for checkout with compensation: `order.created → inventory.reserved / inventory.failed → payment.completed / payment.failed → order.confirmed / order.cancelled` (stock released on failure).
- **Redis**: product/catalog cache-aside with TTL + invalidation on update events; cart storage; sliding-window/token-bucket rate limiter (per-IP and per-user) in gateway; refresh-token denylist.
- **Auth model**: auth-service issues JWT; gateway verifies signature and forwards `X-User-Id`/`X-User-Role` headers; services trust only gateway-injected headers (internal network only).
- **Observability**: structured JSON logs, `X-Request-ID` / correlation-id propagated through HTTP and Kafka headers, `/health` + `/ready`, Prometheus `/metrics` (stretch: OpenTelemetry tracing).
- **Resilience**: httpx timeouts, retries with backoff, simple circuit breaker in gateway.
- **Testing**: unit (services with fake repos), integration (testcontainers for Postgres/Redis/Kafka), contract tests for event schemas, one end-to-end checkout test via compose.
- **Quality gates**: ruff, mypy, pytest in pre-commit and CI; Dockerfiles multi-stage, non-root.

## Phases (one service per phase)
**Status:** Phase 0 done, Phase 1 (gateway) done, Phase 2 (auth) done, Phase 3 (user) done. Both verified end to end without Docker (Kafka transport itself not yet exercised). Docker stack not yet run (Docker Hub unreachable on dev machine).

Each phase ends with: Alembic migration, tests passing, Dockerfile + compose entry, gateway route, README section, and a Postman/`.http` collection entry.

| # | Phase | Scope | Done when |
|---|---|---|---|
| 0 | **Foundation** | Repo scaffold, `ecom_common` lib (responses, exceptions, config, logging, middleware, db, health), docker-compose with Postgres/Redis/Kafka/Kafka-UI, Makefile, CI, a `_template` service to copy | `make up` brings infra up; template service returns uniform responses incl. errors |
| 1 | **Gateway** | Reverse proxy routing table, JWT verification, Redis rate limiting, request-id, CORS, downstream error mapping, aggregated `/health` | Requests route to a stub service; 429/401/502 use uniform shape |
| 2 | **Auth service** | Register, login, refresh, logout, password hashing, JWT, roles (customer/admin), refresh denylist in Redis; emits `user.registered` | Full login flow through gateway; protected routes enforced |
| 3 | **User service** | Profile, addresses; consumes `user.registered` to create profile | Profile auto-created from Kafka event |
| 4 | **Product service** | Categories, products CRUD (admin), search/filter/pagination, Redis cache-aside; emits `product.created/updated` | Cache hit/miss visible; invalidation on update |
| 5 | **Inventory service** | Stock per product, reserve/release, consumes `product.created`, `order.created`; outbox | Concurrent reservations never oversell (row locking test) |
| 6 | **Cart service** | Redis-backed cart, add/update/remove, price snapshot, checkout trigger | Cart persists, TTL, merges on login |
| 7 | **Order service** | Create order from cart, status state machine, outbox, saga orchestration via events, idempotency-key header | Order moves PENDING→CONFIRMED/CANCELLED correctly |
| 8 | **Payment service** | Mock payment provider, intent/charge/refund, webhook endpoint, idempotent processing; `payment.completed/failed` | Failure path triggers stock release + order cancel |
| 9 | **Notification service** | Kafka consumer only; email/log notifier (console/MailHog) for order & payment events, retry + DLQ | Events produce notifications; poison messages land in DLQ |
| 10 | **Hardening** | Prometheus + Grafana, tracing, load test (locust), E2E tests, docs/ADRs, optional K8s manifests/Helm | Full checkout E2E green in CI; documented architecture |

## Conventions
- API prefix `/api/v1/<service>/...`; plural nouns; UUID primary keys; `created_at/updated_at` on all tables; soft delete only where required.
- Config only via env vars (`pydantic-settings`), `.env.example` per service; no secrets committed.
- Conventional commits; one branch/PR per phase.
- Beginner-friendliness: each service README explains its endpoints, events in/out, and how to run its tests; docstrings on shared lib.

## Phase 0 deliverables (first implementation step after approval)
1. Copy this plan to `plan.md`; create folder skeleton above (remove stray empty `main.py`).
2. Implement `libs/common/ecom_common/responses.py`, `exceptions.py`, `middleware.py`, `logging.py`, `config.py`, `db.py`, `health.py` with unit tests.
3. `docker-compose.yml` with Postgres (init script creating per-service DBs), Redis, Kafka KRaft, Kafka-UI.
4. `services/_template` minimal FastAPI service using the shared lib; Makefile; ruff/mypy/pytest config; CI workflow.

## Verification (Phase 0)
- `docker compose up -d` → all infra containers healthy; Kafka-UI reachable.
- `pytest libs/common` passes; tests assert response shape on success, 404, 422, 500 (all contain exactly `success, message, data, error_message`).
- `curl localhost:<port>/health` and a deliberately failing endpoint on the template service return the uniform JSON.
- `ruff check`, `mypy` clean.

## Open choices (defaults used unless you object)
- Gateway: custom FastAPI (vs Kong/Traefik) — default custom.
- Shared lib: local `libs/common` package (monorepo) — default yes.
- Package manager: `uv` — default yes.
