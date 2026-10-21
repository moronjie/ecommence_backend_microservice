import logging

import httpx

from app.domain import ProductInfo
from app.errors import ServiceUnavailable

logger = logging.getLogger(__name__)


class ProductClient:
    """Fallback for when the local product copy has no entry (e.g. Redis was wiped)."""

    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self.http, self.base = http, base_url.rstrip("/")

    async def fetch(self, product_id: str) -> ProductInfo | None:
        """None means the product does not exist or is hidden from the public."""
        try:
            resp = await self.http.get(f"{self.base}/api/v1/products/{product_id}")
        except httpx.HTTPError as exc:
            logger.warning("Product service unreachable: %s", exc)
            raise ServiceUnavailable("Could not look up the product, try again shortly") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ServiceUnavailable("Could not look up the product, try again shortly")
        d = resp.json()["data"]
        return ProductInfo(
            product_id=d["id"],
            sku=d["sku"],
            name=d["name"],
            price_cents=d["price_cents"],
            currency=d["currency"],
            is_active=d["is_active"],
            event_at=d["updated_at"],
        )


class UserClient:
    """Looks up a delivery address so the order can keep a copy of it."""

    FIELDS = ("recipient", "line1", "line2", "city", "state", "postal_code", "country")

    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self.http, self.base = http, base_url.rstrip("/")

    async def email(self, user_id: str) -> str | None:
        """The customer's email from their profile, or None if the profile does not exist yet."""
        try:
            resp = await self.http.get(
                f"{self.base}/api/v1/users/me", headers={"X-User-Id": user_id}
            )
        except httpx.HTTPError as exc:
            logger.warning("User service unreachable: %s", exc)
            raise ServiceUnavailable("Could not look up your account, try again shortly") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ServiceUnavailable("Could not look up your account, try again shortly")
        return resp.json()["data"]["email"]

    async def address(self, user_id: str, address_id: str) -> dict[str, str] | None:
        """The address's details, or None if this user has no such address."""
        try:
            resp = await self.http.get(
                f"{self.base}/api/v1/users/me/addresses", headers={"X-User-Id": user_id}
            )
        except httpx.HTTPError as exc:
            logger.warning("User service unreachable: %s", exc)
            raise ServiceUnavailable("Could not look up your address, try again shortly") from exc
        if resp.status_code == 404:  # no profile yet, so no addresses
            return None
        if resp.status_code != 200:
            raise ServiceUnavailable("Could not look up your address, try again shortly")
        for item in resp.json()["data"]["items"]:
            if item["id"] == address_id:
                return {name: item.get(name, "") for name in self.FIELDS}
        return None


class InventoryClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self.http, self.base = http, base_url.rstrip("/")

    async def availability(self, product_ids: list[str], user_id: str) -> dict[str, int]:
        """Units available per product. Raises ServiceUnavailable if inventory cannot answer."""
        try:
            resp = await self.http.post(
                f"{self.base}/api/v1/inventory/availability",
                json={"product_ids": product_ids},
                headers={"X-User-Id": user_id},  # the inventory service wants to know who asks
            )
        except httpx.HTTPError as exc:
            logger.warning("Inventory service unreachable: %s", exc)
            raise ServiceUnavailable("Stock levels are unavailable right now") from exc
        if resp.status_code != 200:
            raise ServiceUnavailable("Stock levels are unavailable right now")
        return {i["product_id"]: i["available"] for i in resp.json()["data"]["items"]}
