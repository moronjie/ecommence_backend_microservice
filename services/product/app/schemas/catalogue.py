import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    description: str


class ProductCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10000)
    price_cents: int = Field(ge=0, le=10**12, description="Price in minor units, 1999 = 19.99")
    currency: str = Field(default="USD", pattern=r"^[A-Za-z]{3}$")
    category_id: uuid.UUID | None = None
    is_active: bool = True


class ProductUpdate(BaseModel):
    """Partial update: only the fields that are sent change. The SKU is permanent."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    price_cents: int | None = Field(default=None, ge=0, le=10**12)
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    category_id: uuid.UUID | None = None
    is_active: bool | None = None


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sku: str
    name: str
    description: str
    price_cents: int
    currency: str
    category_id: uuid.UUID | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
