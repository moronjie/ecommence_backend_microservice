import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from ecom_common.responses import error_response

logger = logging.getLogger(__name__)


class AppException(Exception):
    """Base class for expected, client-facing errors."""

    status_code: int = 400
    message: str = "Request failed"

    def __init__(
        self,
        error_message: str = "",
        *,
        message: str | None = None,
        status_code: int | None = None,
        data: Any = None,
    ) -> None:
        self.error_message = error_message or (message or self.message)
        self.message = message or self.message
        self.status_code = status_code or self.status_code
        self.data = data
        super().__init__(self.error_message)


class NotFound(AppException):
    status_code = 404
    message = "Resource not found"


class Conflict(AppException):
    status_code = 409
    message = "Resource conflict"


class Unauthorized(AppException):
    status_code = 401
    message = "Authentication required"


class Forbidden(AppException):
    status_code = 403
    message = "Permission denied"


class ValidationFailed(AppException):
    status_code = 422
    message = "Validation failed"


async def _app_exception_handler(_: Request, exc: AppException):
    return error_response(exc.message, exc.error_message, exc.status_code, exc.data)


async def _validation_handler(_: Request, exc: RequestValidationError):
    errors = [
        {
            "field": ".".join(str(p) for p in e["loc"] if p != "body"),
            "message": e["msg"],
            "type": e["type"],
        }
        for e in exc.errors()
    ]
    summary = "; ".join(f"{e['field']}: {e['message']}" for e in errors)
    return error_response("Validation failed", summary, 422, {"errors": errors})


async def _http_exception_handler(_: Request, exc: StarletteHTTPException):
    detail = str(exc.detail)
    return error_response(detail, detail, exc.status_code)


async def _unhandled_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return error_response("Internal server error", "Something went wrong", 500)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppException, _app_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_handler)
