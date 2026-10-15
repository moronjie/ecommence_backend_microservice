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
