import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---- what the API returns ------------------------------------------------------------------


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    subject: str
    to_email: str
    status: str
    created_at: datetime
    sent_at: datetime | None


class NotificationAdminOut(NotificationOut):
    """Administrators also see how delivery is going."""

    user_id: uuid.UUID | None
    attempts: int
    last_error: str
    next_attempt_at: datetime | None


class NotificationDetail(NotificationAdminOut):
    text_body: str
    html_body: str


# ---- events other services send us ---------------------------------------------------------
class UserRegistered(BaseModel):
    user_id: uuid.UUID
    email: str | None = None
    full_name: str = ""


class ReceiptItem(BaseModel):
    name: str
    quantity: int = Field(ge=0)
    unit_price_cents: int = Field(ge=0)


class OrderEvent(BaseModel):
    """order.confirmed / order.cancelled / order.shipped / order.delivered"""

    order_id: uuid.UUID
    user_id: uuid.UUID
    email: str | None = None
    currency: str = "GHS"
    total_cents: int = Field(default=0, ge=0)
    reason: str = ""
    items: list[ReceiptItem] = []
    shipping_address: dict[str, Any] | None = None


class RefundEvent(BaseModel):
    """payment.refunded"""

    order_id: uuid.UUID
    payment_id: uuid.UUID
    user_id: uuid.UUID
    email: str | None = None
    amount_cents: int = Field(ge=0)
    currency: str = "GHS"
