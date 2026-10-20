import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CartItem:
    product_id: str
    quantity: int
    # What the product looked like when it was added. The live price is checked again later.
    name: str
    sku: str
    price_cents: int
    added_at: str

    @classmethod
    def from_json(cls, raw: str) -> "CartItem":
        return cls(**json.loads(raw))


@dataclass
class Cart:
    cart_id: str  # new for every cart, so checkout ids never repeat across carts
    currency: str
    version: int = 0  # +1 on every change
    items: dict[str, CartItem] = field(default_factory=dict)

    # Redis hash layout: cart_id, currency, version, and one "item:<product id>" field per line
    def to_hash(self) -> dict[str, str]:
        mapping = {
            "cart_id": self.cart_id,
            "currency": self.currency,
            "version": str(self.version),
        }
        for product_id, item in self.items.items():
            mapping[f"item:{product_id}"] = json.dumps(asdict(item))
        return mapping

    @classmethod
    def from_hash(cls, data: dict[str, str]) -> "Cart | None":
        if not data or "cart_id" not in data:
            return None
        items = {
            key.removeprefix("item:"): CartItem.from_json(value)
            for key, value in data.items()
            if key.startswith("item:")
        }
        return cls(data["cart_id"], data["currency"], int(data["version"]), items)


@dataclass(frozen=True)
class ProductInfo:
    """This service's local copy of a product, kept up to date from product events."""

    product_id: str
    sku: str
    name: str
    price_cents: int
    currency: str
    is_active: bool
    event_at: str  # when the product changed; used to ignore stale, out-of-order events

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> "ProductInfo":
        data: dict[str, Any] = json.loads(raw)
        return cls(**data)
