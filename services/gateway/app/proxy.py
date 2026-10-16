import asyncio
import logging

import httpx
from ecom_common.middleware import REQUEST_ID_HEADER
from fastapi import Request, Response

from app.errors import BadGateway, GatewayTimeout, ServiceUnavailable
from app.resilience import CircuitBreaker

logger = logging.getLogger(__name__)

# Headers that describe a single hop and must not be forwarded either way
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
# Identity headers are set by the gateway only. A client must never be able to spoof them.
IDENTITY_HEADERS = {"x-user-id", "x-user-role"}
IDEMPOTENT = {"GET", "HEAD"}


def build_upstream_headers(
    request: Request, request_id: str, user: dict[str, str] | None
) -> dict[str, str]:
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower()
        not in HOP_BY_HOP | IDENTITY_HEADERS | {"host", "content-length", REQUEST_ID_HEADER.lower()}
    }
    headers[REQUEST_ID_HEADER] = request_id
    if user:
        headers["X-User-Id"] = user["id"]
        headers["X-User-Role"] = user["role"]
    return headers


async def forward(
    *,
    client: httpx.AsyncClient,
    breaker: CircuitBreaker,
    service: str,
    request: Request,
    upstream_base: str,
    headers: dict[str, str],
) -> Response:
    if not breaker.allow():
        raise ServiceUnavailable(f"{service} is temporarily unavailable, try again shortly")

    url = upstream_base.rstrip("/") + request.url.path
    body = await request.body()
    attempts = 2 if request.method in IDEMPOTENT else 1

    for attempt in range(attempts):
        try:
            upstream = await client.request(
                request.method,
                url,
                params=request.query_params,
                headers=headers,
                content=body,
            )
            break
        except httpx.ConnectError as exc:
            if attempt + 1 < attempts:
                await asyncio.sleep(0.1)
                continue
            breaker.record_failure()
            logger.warning("%s unreachable: %s", service, exc)
            raise ServiceUnavailable(f"{service} is unreachable") from exc
        except httpx.TimeoutException as exc:
            breaker.record_failure()
            logger.warning("%s timed out: %s", service, exc)
            raise GatewayTimeout(f"{service} took too long to respond") from exc
        except httpx.HTTPError as exc:
            breaker.record_failure()
            logger.warning("%s bad response: %s", service, exc)
            raise BadGateway(f"{service} returned an invalid response") from exc

    if upstream.status_code >= 500:
        breaker.record_failure()
    else:
        breaker.record_success()

    # httpx already decoded the body, so drop headers describing the encoded one
    skip = HOP_BY_HOP | {"content-length", "content-encoding"}
    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in skip}
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )
