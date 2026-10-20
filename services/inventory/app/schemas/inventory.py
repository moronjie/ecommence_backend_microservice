import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class StockOut(BaseModel):
    """Full view, for administrators."""

    model_config = ConfigDict(from_attributes=True)

    product_id: uuid.UUID
    sku: str
    on_hand: int
    reserved: int
    available: int
    updated_at: datetime


class AvailabilityOut(BaseModel):
    """What a shopper may see: no exact warehouse numbers."""

    model_config = ConfigDict(from_attributes=True)

    product_id: uuid.UUID
    sku: str
    available: int
    in_stock: bool


class SetStock(BaseModel):
    on_hand: int = Field(ge=0, le=10**9, description="Counted quantity on the shelf")


class AdjustStock(BaseModel):
    delta: int = Field(ge=-(10**9), le=10**9, description="+ for received goods, - for losses")
    reason: str = Field(default="", max_length=200)


class AvailabilityRequest(BaseModel):
    product_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


# ---- event payloads (what other services send us) ------------------------------------------
class OrderItem(BaseModel):
    product_id: uuid.UUID
    quantity: int = Field(gt=0, le=10**6)


class OrderCreatedPayload(BaseModel):
    order_id: uuid.UUID
    items: list[OrderItem] = Field(min_length=1)


class OrderRefPayload(BaseModel):
    order_id: uuid.UUID


class ProductPayload(BaseModel):
    product_id: uuid.UUID
    sku: str = ""
