from ecom_common.db import Base, TimestampMixin
from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

CUSTOMER = "customer"
ADMIN = "admin"


class User(TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(20), default=CUSTOMER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
