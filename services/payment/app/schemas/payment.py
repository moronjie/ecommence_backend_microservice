import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_id: uuid.UUID
    status: str
    amount_cents: int
    currency: str
    channel: str
    failure_reason: str
    created_at: datetime
    paid_at: datetime | None
    # Only filled while the customer still has to pay: send them here
    authorization_url: str | None = None


# ---- events other services send us ---------------------------------------------------------
class PaymentRequested(BaseModel):
    """order.payment_requested"""

    order_id: uuid.UUID
    user_id: uuid.UUID
    total_cents: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    email: str | None = None


class RefundRequested(BaseModel):
    """order.refund_requested"""

    order_id: uuid.UUID
    reason: str = ""
