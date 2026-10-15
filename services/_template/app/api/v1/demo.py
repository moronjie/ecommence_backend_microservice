"""Demo routes that exercise the uniform response. Delete in real services."""

from ecom_common import NotFound, success_response
from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/template", tags=["demo"])


@router.get("/ping")
async def ping():
    return success_response({"pong": True}, "Pong")


@router.get("/items/{item_id}")
async def get_item(item_id: int):
    raise NotFound(f"Item {item_id} does not exist")


@router.get("/boom")
async def boom():
    raise RuntimeError("deliberate failure to demo the 500 handler")
