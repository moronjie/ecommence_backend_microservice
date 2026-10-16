from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class ApiResponse[T](BaseModel):
    """Envelope model. Use as ``response_model=ApiResponse[MySchema]``."""

    success: bool
    message: str
    data: T | dict[str, Any] = Field(default_factory=dict)
    error_message: str = ""


def success_response(
    data: Any = None,
    message: str = "Success",
    status_code: int = 200,
) -> JSONResponse:
    body = ApiResponse[Any](
        success=True,
        message=message,
        data=jsonable_encoder(data) if data is not None else {},
        error_message="",
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def error_response(
    message: str,
    error_message: str = "",
    status_code: int = 400,
    data: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ApiResponse[Any](
        success=False,
        message=message,
        data=jsonable_encoder(data) if data is not None else {},
        error_message=error_message or message,
    )
    return JSONResponse(
        status_code=status_code, content=body.model_dump(mode="json"), headers=headers
    )


def paginated(items: list[Any], total: int, page: int, size: int) -> dict[str, Any]:
    """Shape for list endpoints so ``data`` is always a dict."""
    return {"items": items, "total": total, "page": page, "size": size}
