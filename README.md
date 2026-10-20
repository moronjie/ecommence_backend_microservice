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

## Product service (Phase 4)
`services/product` (port 8003): categories (`/api/v1/categories`) and products (`/api/v1/products`).
Anyone can browse; creating, changing and deleting need an admin token (checked from the gateway's
`X-User-Role`). Prices are integers in cents (`1999` = 19.99). "Deleting" a product only hides it
(`is_active=false`) so old orders can still refer to it; admins see hidden products with
`?include_inactive=true`.

Search: `GET /api/v1/products?q=mug&category_id=...&min_price=500&max_price=5000&sort=price&order=asc&page=1&size=20`
(`q` matches name or description, case-insensitive; `sort` is `created_at`, `price` or `name`).

**Caching (Redis, cache-aside).** Every read returns an `X-Cache: HIT|MISS` header, so you can watch it work.
Product detail is cached 5 min, lists 1 min, categories 10 min. Any product write deletes that product's key
and bumps a version number that invalidates every cached list at once. If Redis is down, requests still work
(they just all miss). Admin listings are never cached.

Each create/update/delete writes `product.created` / `product.updated` (a full snapshot) to the outbox.
Search uses `LIKE '%q%'`, which is fine for a small catalogue; for a large one add a Postgres trigram
index or a search engine.

## Inventory service (Phase 5)
`services/inventory` (port 8004, route `/api/v1/inventory`). A stock row is created automatically from
`product.created`. Admins set (`PUT /{id}`) or adjust (`POST /{id}/adjust`) stock; shoppers see only
availability (`GET /{id}`, `POST /availability` for many products). Stock is `on_hand` minus `reserved`.

Reserving stock for orders is event-driven (see `docs/events.md`). It never oversells because of three layers:
rows are locked in a fixed order (`SELECT ... FOR UPDATE`), availability is checked under that lock and an order
is all-or-nothing, and database CHECK constraints refuse impossible numbers. Every change is also written to an
append-only `stock_movements` ledger. Unpaid holds expire after 15 minutes (a background job releases them).

The real concurrency test needs PostgreSQL (SQLite has no row locks), so it only runs when you ask:
```bash
cd services/inventory
TEST_POSTGRES_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/inventory_test   python -m pytest tests/test_concurrency_postgres.py
```
Use an empty throw-away database; the test creates and drops the tables.

## Cart service (Phase 6)
`services/cart` (port 8005, route `/api/v1/cart`, login required): `GET /` (view), `POST /items`, `PUT /items/{product_id}`,
`DELETE /items/{product_id}`, `DELETE /` (clear), `POST /checkout`. The cart lives only in Redis (a hash per user,
30 days from the last change). There is no database, so there is no migration and no outbox.

Each line remembers the price the shopper saw, but **viewing and checkout use the current price**: the view flags
`price_changed`, and checkout accepts an optional `expected_total_cents` and answers 409 (with the new total) if prices
moved. Stock comes from the inventory service: a view still works if inventory is down (stock shows `null`), but
checkout refuses to start an order without a real answer. The cart keeps a local copy of products, fed by
`product.created`/`product.updated`, and only asks the product service when it has never seen a product.

Checkout returns 202 and publishes `cart.checked_out` (see `docs/events.md`). If publishing fails, the cart is kept
and a retry reuses the same `checkout_id`, so the order service can ignore duplicates.

## Order service (Phase 7)
`services/order` (port 8006, route `/api/v1/orders`, login required). The order service is created by an event, not
by an API call: it consumes `cart.checked_out` and creates a `pending` order (once per `checkout_id`), then reacts to
the inventory and payment services as described in `docs/events.md`. Customers see their own orders
(`GET /`, `GET /{id}` with items, the address copy and the full status history; someone else's order is a 404) and can
`POST /{id}/cancel` before payment. Admins see all orders (`?owner=`, `?status=` filters) and move paid orders
`POST /{id}/ship` then `/deliver`; they can also cancel before payment.

The order keeps its own copy of what was bought (names, prices) and the delivery address, so later catalogue or
address edits never change history. Orders stuck waiting for another service are cancelled by a background sweeper.
If a payment arrives for an already-cancelled order, `order.refund_requested` is published for the payment service.
