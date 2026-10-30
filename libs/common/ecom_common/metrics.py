"""Prometheus metrics: how many requests, how many fail, how slow, how many events.

Every service exposes ``/metrics``. The gateway has no route for it, so it is NOT reachable from
the outside; Prometheus scrapes each service over the internal network.
"""

import hmac
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

_service = "unknown"

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["service", "method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "How long requests take to answer",
    ["service", "method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
# outcome: handled | ignored | retried | dead_lettered
EVENTS = Counter("events_consumed_total", "Kafka events processed", ["service", "topic", "outcome"])

# Not worth measuring: they would drown out real traffic
_SKIPPED = {"/metrics", "/health", "/ready"}


def set_service_name(name: str) -> None:
    """One service runs per process, so a module-level name labels every metric it records."""
    global _service
    _service = name


def service_name() -> str:
    return _service


def count_event(topic: str, outcome: str) -> None:
    EVENTS.labels(_service, topic, outcome).inc()


class MetricsMiddleware:
    """Pure ASGI (no per-request task, so it adds almost nothing to latency)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in _SKIPPED:
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = {"code": 500}  # if the app crashes before answering, that is a 500

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # Label by the route TEMPLATE ("/products/{id}"), never the real path, so a million
            # different ids do not create a million time series. The gateway overrides it with the
            # upstream service, since all its traffic matches one catch-all route.
            route = (
                scope.get("metrics_route")
                or getattr(scope.get("route"), "path", None)
                or "unmatched"
            )
            method = scope["method"]
            HTTP_REQUESTS.labels(_service, method, route, str(status["code"])).inc()
            HTTP_LATENCY.labels(_service, method, route).observe(time.perf_counter() - started)


def add_metrics(app: FastAPI, service: str, token: str = "") -> None:
    """Measure every request and serve the numbers at /metrics (protected if ``token`` is set)."""
    set_service_name(service)
    app.add_middleware(MetricsMiddleware)

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        if token:
            given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(given, token):
                return Response("unauthorized", status_code=401)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
