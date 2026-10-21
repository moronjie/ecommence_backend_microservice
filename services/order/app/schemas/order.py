import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: uuid.UUID
    sku: str
    name: str
    quantity: int
    unit_price_cents: int


class HistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_status: str | None
    to_status: str
    reason: str
    actor: str
    created_at: datetime


class OrderSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    status: str
    currency: str
    total_cents: int
    created_at: datetime
    updated_at: datetime


class OrderDetail(OrderSummary):
    shipping_address: dict[str, Any] | None
    cancel_reason: str
    items: list[ItemOut]
    history: list[HistoryOut]


# ---- events other services send us ---------------------------------------------------------
class CheckoutItem(BaseModel):
    product_id: uuid.UUID
    sku: str = ""
    name: str
    quantity: int = Field(gt=0, le=10**6)
    unit_price_cents: int = Field(ge=0)


class CheckoutPayload(BaseModel):
    """cart.checked_out"""

    checkout_id: uuid.UUID
    user_id: uuid.UUID
    email: str | None = Field(default=None, max_length=320)
    address_id: uuid.UUID | None = None
    shipping_address: dict[str, Any] | None = None
    currency: str = Field(min_length=3, max_length=3)
    total_cents: int = Field(ge=0)
    items: list[CheckoutItem] = Field(min_length=1)


class OrderRefPayload(BaseModel):
    """The shape shared by the inventory.* and payment.* events: which order, and why."""

    order_id: uuid.UUID
    reason: str = ""
