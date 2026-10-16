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
