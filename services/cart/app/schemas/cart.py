import uuid

from pydantic import BaseModel, Field


class AddItem(BaseModel):
    product_id: uuid.UUID
    quantity: int = Field(default=1, ge=1, le=99)


class SetQuantity(BaseModel):
    quantity: int = Field(ge=1, le=99, description="Use DELETE to remove an item")


class CheckoutRequest(BaseModel):
    address_id: uuid.UUID | None = Field(default=None, description="Chosen delivery address")
    expected_total_cents: int | None = Field(
        default=None,
        ge=0,
        description="The total the shopper saw. If prices moved, checkout is refused.",
    )


class ProductEvent(BaseModel):
    """The part of product.created / product.updated that the cart needs."""

    product_id: uuid.UUID
    sku: str = ""
    name: str
    price_cents: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    is_active: bool
