import uuid

from pydantic import BaseModel, ConfigDict, Field


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    email: str
    full_name: str
    phone: str


class ProfileUpdate(BaseModel):
    """Only the fields that are sent change. Email belongs to the auth service."""

    full_name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=32, pattern=r"^\+?[0-9 ()\-]*$")


class AddressIn(BaseModel):
    label: str = Field(default="", max_length=50)
    recipient: str = Field(min_length=1, max_length=120)
    line1: str = Field(min_length=1, max_length=200)
    line2: str = Field(default="", max_length=200)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field(default="", max_length=100)
    postal_code: str = Field(min_length=1, max_length=20)
    country: str = Field(min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")
    is_default: bool = False


class AddressOut(AddressIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
