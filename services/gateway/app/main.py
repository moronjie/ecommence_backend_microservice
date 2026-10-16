import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from ecom_common import NotFound, Unauthorized, error_response, success_response
from ecom_common.app_factory import create_service_app
from ecom_common.logging import request_id_ctx
from ecom_common.middleware import REQUEST_ID_HEADER
from ecom_common.redis import SlidingWindowRateLimiter, create_redis
from ecom_common.security import decode_token
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.config import settings as default_settings
from app.core.routes import build_routes, match_route
from app.errors import TooManyRequests
from app.proxy import build_upstream_headers, forward
from app.resilience import BreakerRegistry

logger = logging.getLogger(__name__)

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def create_app(
    settings: Settings = default_settings,
    *,
    http_client: httpx.AsyncClient | None = None,
    redis_client: Redis | None = None,
) -> FastAPI:
    """Build the gateway. Clients can be injected (tests); otherwise lifespan creates them."""
    routes = build_routes(settings)

    async def redis_ready() -> None:
        await app.state.redis.ping()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned: list = []
        if app.state.http is None:
            app.state.http = httpx.AsyncClient(
                timeout=settings.upstream_timeout_seconds, follow_redirects=False
            )
            owned.append(app.state.http.aclose)
        if app.state.redis is None:
            app.state.redis = create_redis(settings.redis_url)
            owned.append(app.state.redis.aclose)
        app.state.limiter = SlidingWindowRateLimiter(app.state.redis)
        yield
        for close in owned:
            await close()

    app = create_service_app(settings, lifespan=lifespan, readiness_checks={"redis": redis_ready})
    app.state.http = http_client
    app.state.redis = redis_client
    app.state.limiter = SlidingWindowRateLimiter(redis_client) if redis_client else None
    app.state.breakers = BreakerRegistry(
        settings.circuit_failure_threshold, settings.circuit_recovery_seconds
    )

    # Added last = outermost, so CORS preflights are answered before anything else runs
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            REQUEST_ID_HEADER,
            "Retry-After",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
        ],
    )

    @app.get("/health/services", tags=["health"])
    async def services_health():
        """Ask every upstream's /health. 200 only if all are up."""
        upstreams = {r.upstream: r.name for r in routes}

        async def probe(url: str) -> str:
            try:
                resp = await app.state.http.get(f"{url}/health", timeout=2.0)
                return "up" if resp.status_code == 200 else "down"
            except httpx.HTTPError:
                return "down"

        results = await asyncio.gather(*(probe(u) for u in upstreams))
        statuses = {name: status for name, status in zip(upstreams.values(), results, strict=True)}
        if all(s == "up" for s in statuses.values()):
            return success_response(statuses, "All services are up")
        down = ", ".join(n for n, s in statuses.items() if s == "down")
        return error_response("Some services are down", f"Down: {down}", 503, statuses)

    @app.api_route("/{full_path:path}", methods=METHODS, include_in_schema=False)
    async def gateway(request: Request, full_path: str) -> Response:
        path = request.url.path
        route = match_route(routes, path)
        if route is None:
            raise NotFound(f"No service handles {path}", message="Route not found")

        # Who is calling? A bad token only matters if the route is protected.
        user, token_error = None, "Missing bearer token"
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            try:
                claims = decode_token(
                    auth[7:].strip(),
                    secret=settings.jwt_secret,
                    algorithm=settings.jwt_algorithm,
                )
                user = {"id": str(claims["sub"]), "role": str(claims.get("role", "customer"))}
            except Unauthorized as exc:
                token_error = exc.error_message

        # Rate limit by user when known, otherwise by IP. Counts 401s too, so
        # token-guessing is limited.
        identity = (
            f"user:{user['id']}"
            if user
            else f"ip:{request.client.host if request.client else 'unknown'}"
        )
        rate_headers: dict[str, str] = {}
        try:
            result = await app.state.limiter.hit(
                identity, settings.rate_limit_requests, settings.rate_limit_window_seconds
            )
        except Exception:  # noqa: BLE001 - Redis trouble must not take the gateway down
            logger.warning("Rate limiter unavailable, allowing request", exc_info=True)
        else:
            rate_headers = {
                "X-RateLimit-Limit": str(result.limit),
                "X-RateLimit-Remaining": str(result.remaining),
            }
            if not result.allowed:
                raise TooManyRequests(
                    f"Limit is {result.limit} requests per {settings.rate_limit_window_seconds}s",
                    headers={**rate_headers, "Retry-After": str(result.retry_after)},
                )

        if user is None and not route.is_public(request.method, path):
            raise Unauthorized(token_error)

        headers = build_upstream_headers(request, request_id_ctx.get(), user)
        response = await forward(
            client=app.state.http,
            breaker=app.state.breakers.get(route.name),
            service=route.name,
            request=request,
            upstream_base=route.upstream,
            headers=headers,
        )
        response.headers.update(rate_headers)
        return response

    return app


app = create_app()
