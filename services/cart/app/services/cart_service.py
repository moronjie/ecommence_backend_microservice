import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from ecom_common import Conflict, NotFound
from ecom_common.kafka import EventPublisher, build_event

from app.clients import InventoryClient, ProductClient, UserClient
from app.core.config import Settings
from app.domain import Cart, CartItem, ProductInfo
from app.errors import ServiceUnavailable
from app.stores import CartStore, ProductStore

logger = logging.getLogger(__name__)

CHECKOUT_TOPIC = "cart.checked_out"
# Fixed namespace so the same (cart, version) always produces the same checkout id
_CHECKOUT_NS = uuid.UUID("6f1a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b")


class CartService:
    def __init__(
        self,
        carts: CartStore,
        products: ProductStore,
        product_client: ProductClient,
        inventory: InventoryClient,
        users: UserClient,
        publisher: EventPublisher,
        settings: Settings,
    ) -> None:
        self.carts = carts
        self.products = products
        self.product_client = product_client
        self.inventory = inventory
        self.users = users
        self.publisher = publisher
        self.settings = settings

    # ---- product lookup -----------------------------------------------------------------
    async def _product(self, product_id: str) -> ProductInfo | None:
        """Local copy first; ask the product service only if we have never seen the product."""
        info = await self.products.get(product_id)
        if info is None:
            info = await self.product_client.fetch(product_id)
            if info is not None:
                await self.products.save(info)
        return info

    # ---- changing the cart --------------------------------------------------------------
    async def add_item(self, user_id: str, product_id: uuid.UUID, quantity: int) -> Cart | None:
        pid = str(product_id)
        info = await self._product(pid)
        if info is None or not info.is_active:
            raise NotFound("This product is not available", message="Product unavailable")
        s = self.settings

        def change(cart: Cart | None) -> Cart:
            cart = cart or Cart(cart_id=uuid.uuid4().hex, currency=info.currency)
            if cart.currency != info.currency:
                raise Conflict(
                    f"Your cart is in {cart.currency}; this product is sold in {info.currency}"
                )
            existing = cart.items.get(pid)
            total = (existing.quantity if existing else 0) + quantity
            if total > s.max_per_line:
                raise Conflict(f"You can order at most {s.max_per_line} of one product")
            if existing is None and len(cart.items) >= s.max_lines:
                raise Conflict(f"A cart can hold at most {s.max_lines} different products")
            cart.items[pid] = CartItem(
                product_id=pid,
                quantity=total,
                name=info.name,
                sku=info.sku,
                price_cents=info.price_cents,  # the price the shopper sees right now
                added_at=existing.added_at if existing else datetime.now(UTC).isoformat(),
            )
            cart.version += 1
            return cart

        return await self.carts.mutate(user_id, change)

    async def set_quantity(self, user_id: str, product_id: uuid.UUID, quantity: int) -> Cart | None:
        pid = str(product_id)

        def change(cart: Cart | None) -> Cart:
            if cart is None or pid not in cart.items:
                raise NotFound("That product is not in your cart", message="Not in cart")
            cart.items[pid].quantity = quantity
            cart.version += 1
            return cart

        return await self.carts.mutate(user_id, change)

    async def remove_item(self, user_id: str, product_id: uuid.UUID) -> Cart | None:
        pid = str(product_id)

        def change(cart: Cart | None) -> Cart:
            if cart is None or pid not in cart.items:
                raise NotFound("That product is not in your cart", message="Not in cart")
            del cart.items[pid]
            cart.version += 1
            return cart

        return await self.carts.mutate(user_id, change)

    async def clear(self, user_id: str) -> None:
        await self.carts.delete(user_id)

    # ---- viewing ------------------------------------------------------------------------
    async def _current_products(self, cart: Cart, strict: bool) -> dict[str, ProductInfo | None]:
        async def lookup(pid: str) -> ProductInfo | None:
            try:
                return await self._product(pid)
            except ServiceUnavailable:
                if strict:
                    raise
                return None  # a view can still be shown using what the cart remembers

        pids = list(cart.items)
        return dict(zip(pids, await asyncio.gather(*(lookup(p) for p in pids)), strict=True))

    async def _stock(self, cart: Cart, user_id: str, strict: bool) -> dict[str, int] | None:
        try:
            return await self.inventory.availability(list(cart.items), user_id)
        except ServiceUnavailable:
            if strict:
                raise
            return None  # "unknown" is better than failing to show the cart at all

    async def view(self, user_id: str) -> dict[str, Any]:
        cart = await self.carts.get(user_id)
        if cart is None:
            return {
                "items": [],
                "currency": None,
                "item_count": 0,
                "subtotal_cents": 0,
                "has_issues": False,
            }

        products = await self._current_products(cart, strict=False)
        stock = await self._stock(cart, user_id, strict=False)
        items, subtotal, has_issues = [], 0, False
        for pid, line in cart.items.items():
            current = products.get(pid)
            unit = current.price_cents if current else line.price_cents
            available = stock.get(pid) if stock is not None else None
            purchasable = bool(current is None or current.is_active) and (
                available is None or available >= line.quantity
            )
            price_changed = current is not None and current.price_cents != line.price_cents
            has_issues = has_issues or not purchasable or price_changed
            if purchasable:
                subtotal += unit * line.quantity
            items.append(
                {
                    "product_id": pid,
                    "sku": line.sku,
                    "name": current.name if current else line.name,
                    "quantity": line.quantity,
                    "unit_price_cents": unit,
                    "added_price_cents": line.price_cents,
                    "price_changed": price_changed,
                    "line_total_cents": unit * line.quantity,
                    "product_available": current is None or current.is_active,
                    "stock_available": available,  # None = could not check
                    "purchasable": purchasable,
                }
            )
        return {
            "currency": cart.currency,
            "items": items,
            "item_count": sum(i["quantity"] for i in items),
            "subtotal_cents": subtotal,  # only items that can actually be bought
            "has_issues": has_issues,
        }

    # ---- checkout -----------------------------------------------------------------------
    async def checkout(
        self,
        user_id: str,
        address_id: uuid.UUID | None,
        expected_total_cents: int | None,
    ) -> dict[str, Any]:
        cart = await self.carts.get(user_id)
        if cart is None:
            raise Conflict("Your cart is empty", message="Cannot check out")

        # Strict: unlike viewing, an order must not start on guesses about price or stock
        products = await self._current_products(cart, strict=True)
        stock = await self._stock(cart, user_id, strict=True) or {}

        problems: list[dict[str, Any]] = []
        lines: list[dict[str, Any]] = []
        total = 0
        for pid, line in cart.items.items():
            current = products.get(pid)
            if current is None or not current.is_active:
                problems.append({"product_id": pid, "problem": "no longer available"})
                continue
            available = stock.get(pid, 0)
            if available < line.quantity:
                problems.append(
                    {
                        "product_id": pid,
                        "problem": "not enough stock",
                        "requested": line.quantity,
                        "available": available,
                    }
                )
                continue
            total += current.price_cents * line.quantity
            lines.append(
                {
                    "product_id": pid,
                    "sku": current.sku,
                    "name": current.name,
                    "quantity": line.quantity,
                    "unit_price_cents": current.price_cents,
                }
            )
        if problems:
            raise Conflict(
                "Some items cannot be ordered; update your cart and try again",
                message="Cannot check out",
                data={"issues": problems},
            )
        if expected_total_cents is not None and expected_total_cents != total:
            raise Conflict(
                f"Prices changed: the total is now {total}, not {expected_total_cents}",
                message="Prices changed",
                data={"total_cents": total},
            )

        shipping_address = None
        if (
            address_id is not None
        ):  # copied now, so editing the address later cannot alter the order
            shipping_address = await self.users.address(user_id, str(address_id))
            if shipping_address is None:
                raise Conflict("That delivery address was not found", message="Cannot check out")

        # Same cart contents -> same id, so a retry after a failure cannot create a second order
        checkout_id = str(uuid.uuid5(_CHECKOUT_NS, f"{cart.cart_id}:{cart.version}"))
        payload = {
            "checkout_id": checkout_id,
            "user_id": user_id,
            "address_id": str(address_id) if address_id else None,
            "shipping_address": shipping_address,
            "currency": cart.currency,
            "total_cents": total,
            "items": lines,
        }
        try:
            await self.publisher.publish(
                CHECKOUT_TOPIC, user_id, build_event(CHECKOUT_TOPIC, payload)
            )
        except Exception as exc:  # noqa: BLE001 - any failure: keep the cart, let them retry
            logger.exception("Could not publish checkout %s", checkout_id)
            raise ServiceUnavailable("Checkout is temporarily unavailable, please retry") from exc

        # Only now is the cart emptied. If this failed, a retry reuses the same checkout id.
        await self.carts.delete(user_id)
        return {"checkout_id": checkout_id, "total_cents": total, "currency": cart.currency}
