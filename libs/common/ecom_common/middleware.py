import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from ecom_common.logging import request_id_ctx

REQUEST_ID_HEADER = "X-Request-ID"
logger = logging.getLogger("access")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info("%s %s (%.1f ms)", request.method, request.url.path, elapsed_ms)
            request_id_ctx.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
