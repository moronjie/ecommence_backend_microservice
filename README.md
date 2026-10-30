# E-Commerce Microservices (FastAPI)

FastAPI microservices with PostgreSQL, Kafka, Redis and an API gateway. See [docs/architecture.md](docs/architecture.md) for the architecture and [docs/adr/](docs/adr/) for the design decisions.

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

## Gateway
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

### One place to try every API

With the stack running, open http://localhost:8000/docs: a single Swagger page for all services, with an
**Authorize** button (log in, paste the `access_token`). It is on by default and off when
`ENVIRONMENT=production` (override with `DOCS_ENABLED`). For Postman, import
`docs/postman/shop.postman_collection.json` (see `docs/postman/README.md`; `make postman` regenerates it).

## Auth service
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

## User service
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

## Product service
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

## Inventory service
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

## Cart service
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

## Order service
`services/order` (port 8006, route `/api/v1/orders`, login required). The order service is created by an event, not
by an API call: it consumes `cart.checked_out` and creates a `pending` order (once per `checkout_id`), then reacts to
the inventory and payment services as described in `docs/events.md`. Customers see their own orders
(`GET /`, `GET /{id}` with items, the address copy and the full status history; someone else's order is a 404) and can
`POST /{id}/cancel` before payment. Admins see all orders (`?owner=`, `?status=` filters) and move paid orders
`POST /{id}/ship` then `/deliver`; they can also cancel before payment.

The order keeps its own copy of what was bought (names, prices) and the delivery address, so later catalogue or
address edits never change history. Orders stuck waiting for another service are cancelled by a background sweeper.
If a payment arrives for an already-cancelled order, `order.refund_requested` is published for the payment service.

## Payment service - Paystack, for Ghana
`services/payment` (port 8007, route `/api/v1/payments`). It reacts to `order.payment_requested`, starts a payment
with the provider, and reports `payment.completed` / `payment.failed` back to the order service. It never sees card or
mobile-money details: the customer pays on Paystack's own page (card or mobile money: MTN MoMo, Vodafone Cash,
AirtelTigo). Amounts are in **pesewas** (`24000` = GHS 240.00) and only **GHS** is accepted (`SUPPORTED_CURRENCIES`).

How it works: a background worker asks Paystack for a payment link (retrying through outages, never reusing a
reference it may already have sent), and the customer reads it from `GET /api/v1/payments/order/{order_id}` and is sent
there. When they pay, Paystack calls `POST /api/v1/payments/webhooks/paystack`. That route needs no login (the gateway
lets it through) but the request must carry a valid `x-paystack-signature`; even then the service asks Paystack to
confirm the amount before it believes the webhook. If a webhook is missed, a reconcile worker re-checks pending
payments; payments the customer never finishes expire after 25 minutes. If money arrives for an order that was already
cancelled, the order service asks for a refund and the payment service sends it back.

**Try it with no account** (default `PROVIDER=fake`): after a checkout, read the link, then play the customer with
`POST /api/v1/payments/dev/{reference}/pay` (add `?outcome=failed` for a decline). That endpoint only exists with the
fake provider.

**Switch to real Paystack** (use a TEST key first):
1. Create a Paystack account, switch the dashboard to Test mode and copy the test secret key (`sk_test_...`).
2. Set `PAYMENT_PROVIDER=paystack` and `PAYSTACK_SECRET_KEY=sk_test_...` (in `.env` for Docker, or in the environment).
3. Give Paystack a URL it can reach for webhooks. Locally use a tunnel (for example `ngrok http 8000`) and set the
   webhook URL in the dashboard to `https://<tunnel>/api/v1/payments/webhooks/paystack`.
4. Optionally set `PAYMENT_CALLBACK_URL` to your shop's thank-you page.
5. Check the provider code against Paystack's current docs before going live: [providers/paystack.py](services/payment/app/providers/paystack.py)
   was written from memory of their API and has only been run against a pretend Paystack. In particular confirm the
   refund webhook's payload and which currencies/channels your account has enabled.

Not built yet: a customer-facing "pay now" page (your frontend redirects to the link), partial refunds, and
restricting the webhook to Paystack's IP addresses.

## Notification service - email only
`services/notification` (port 8008, route `/api/v1/notifications`). It listens for events and emails the customer:
a welcome message, **order confirmed** (a receipt listing the items and total in cedis), **cancelled** (with the reason
in plain language), **shipped** (with the delivery address), **delivered**, and **refund**. It sends no separate email for
a failed payment, since the cancellation email already explains it.

An event only *writes* the email to the database; a background worker sends it. If the mail server is down the email waits
and is retried with growing delays (30 s, 1 min, 2 min ... up to 5 attempts), and after that it is marked `failed`, kept,
and visible to admins, who can fix the cause and `POST /api/v1/notifications/{id}/retry`. One email is sent per milestone
per order, even if the event is delivered twice. Addresses that are missing or invalid are recorded as `failed` rather than
lost, and refused addresses are not retried. Customers can read their own emails (`GET /`, `GET /{id}`); admins see all of
them with delivery details (`?status=failed`, `?kind=`, `?owner=`). Everything customer-supplied is HTML-escaped in the email.

**Trying it.** `EMAIL_PROVIDER=console` (the default outside Docker) only prints each email in the log. With Docker, compose
starts **Mailpit**, a fake mail server: the emails really go over SMTP and you read them at http://localhost:8025.

**Sending real email.** Any provider that offers SMTP works (Brevo, Mailgun, SendGrid, Resend, Gmail, your hosting company).
Set `EMAIL_PROVIDER=smtp`, `SMTP_HOST`, `SMTP_PORT` (587 for most), `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS=true`,
and `FROM_ADDRESS` to an address on a domain you own. Before sending to real customers, set up SPF/DKIM/DMARC for that
domain with your provider, or your emails will land in spam. Also set `SHOP_NAME` and `SUPPORT_EMAIL`.

Known limits: delivery is "at least once", so if the service crashes in the instant between the mail server accepting an
email and us recording it, that one email can be sent twice after the restart. There is no unsubscribe or marketing
preference yet, because nothing marketing-related is sent (only messages about the customer's own orders and account).
Email only: no SMS or WhatsApp.

## Security and operations
Full overview and diagrams: [docs/architecture.md](docs/architecture.md). The reasoning behind the choices: [docs/adr/](docs/adr/).

### Security
* **RS256 login tokens.** Run `make keys` (or `python scripts/generate_keys.py`). It writes the private key to `.keys/auth/` and the
  public key to `.keys/gateway/` (git-ignored) and prints the lines for your `.env`. Docker mounts each folder into only the
  container that needs it, so the gateway can never read the private key. Without keys the stack uses HS256 (one shared secret),
  fine for development only.
* **Signed identities.** Set `INTERNAL_AUTH_SECRET` to a long random value: the gateway signs who is calling and every service discards
  unsigned identities, so reaching a service directly no longer lets anyone pretend to be an admin.
* **Logins.** Five wrong passwords lock an email for 15 minutes (the same for emails that do not exist, so nothing reveals who is
  registered); the gateway also limits login to 10 and sign-up to 5 per minute per IP; logging out kills the access token at once.
* **`ENVIRONMENT=production`** makes every service refuse to start while any unsafe default remains, and tells you exactly which: weak
  or missing secrets, HS256 tokens, the default Postgres password, the fake payment provider or a Paystack test key, console-only
  email or a development mail catcher, no `METRICS_TOKEN`. A bad signing key also stops the service at start-up with a clear message.

Put these settings in `.env` (Docker reads it automatically). On Windows Git Bash, do not `export` a value like
`JWT_PRIVATE_KEY_FILE=/run/keys/jwt_private.pem`: Git Bash rewrites it into a Windows path before Docker sees it.

### Seeing what is happening
* Every service serves Prometheus metrics at `/metrics` (requests, errors, latency by route template, events handled / retried / dead-lettered).
  Set `METRICS_TOKEN` to require a bearer token (the gateway is on the public port; production insists on it).
* With the dev override: **Grafana** at http://localhost:3001 (admin / admin) shows a ready-made "Shop overview" dashboard;
  **Prometheus** at http://localhost:9090 (Status > Targets shows every service).
* **One id follows a checkout across services.** Send `X-Request-ID: my-test-1` with a checkout, then
  `docker compose logs | grep my-test-1`: you will see the gateway, cart, order, inventory and payment all under that id.
* Every service logs JSON, and has `/health` and `/ready`.

### Proving it works
| Command | What it checks | Needs |
|---|---|---|
| `make test` | all unit tests (fast, SQLite and fakes) | nothing |
| `make lint` | style and types | nothing |
| `make migrations-check` | every migration applies, matches the models, rolls back, re-applies on **real Postgres** | Docker Postgres (`POSTGRES_PORT=5433 make ...` if you moved it) |
| `make e2e` | a real purchase through everything with **real Kafka**, plus attacks on the live stack | the whole stack running (`make up`) |
| `make loadtest` | 20 simulated shoppers for 30 s: requests per second, errors, latency percentiles | the stack, started with `RATE_LIMIT_REQUESTS=1000000` |

CI runs all of these (jobs `test`, `postgres`, `e2e`).

