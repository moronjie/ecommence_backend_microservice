import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from app.core.config import Settings
from app.events.handlers import build_handlers
from app.main import create_app
from app.stores import ProductStore
from ecom_common.consumer import EventDispatcher
from ecom_common.kafka import InMemoryPublisher
from fakeredis import FakeAsyncRedis

KEYS = {"success", "message", "data", "error_message"}


def as_user(user_id: str = "user-1") -> dict[str, str]:
    return {"X-User-Id": user_id, "X-User-Role": "customer"}


class Backend:
    """Stands in for the product and inventory services, and records what it was asked."""

    def __init__(self) -> None:
        self.products: dict[str, dict[str, Any]] = {}
        self.stock: dict[str, int] = {}
        self.calls: list[str] = []
        self.inventory_down = False
        self.user_service_down = False
        self.emails: dict[str, str] = {}
        self.no_profile: set[str] = set()
        self.addresses: dict[str, list[dict[str, Any]]] = {}
        self.product_service_down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path.startswith("/api/v1/products/"):
            if self.product_service_down:
                raise httpx.ConnectError("down")
            product = self.products.get(path.rsplit("/", 1)[1])
            if product is None or not product["is_active"]:
                return httpx.Response(404, json={"success": False, "data": {}})
            return httpx.Response(200, json={"success": True, "data": product})
        if path == "/api/v1/users/me":
            if self.user_service_down:
                raise httpx.ConnectError("down")
            user = request.headers["x-user-id"]
            if user in self.no_profile:
                return httpx.Response(404, json={"success": False, "data": {}})
            email = self.emails.get(user, f"{user}@example.com")
            return httpx.Response(200, json={"success": True, "data": {"email": email}})
        if path == "/api/v1/users/me/addresses":
            if self.user_service_down:
                raise httpx.ConnectError("down")
            user = request.headers["x-user-id"]
            return httpx.Response(
                200, json={"success": True, "data": {"items": self.addresses.get(user, [])}}
            )
        if path == "/api/v1/inventory/availability":
            if self.inventory_down:
                raise httpx.ConnectError("down")
            ids = request.read().decode()
            import json

            wanted = json.loads(ids)["product_ids"]
            items = [
                {
                    "product_id": p,
                    "available": self.stock.get(p, 0),
                    "in_stock": self.stock.get(p, 0) > 0,
                }
                for p in wanted
            ]
            return httpx.Response(200, json={"success": True, "data": {"items": items}})
        return httpx.Response(404)


class RecordingPublisher(InMemoryPublisher):
    """Remembers every attempt, and can be told to fail."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = False
        self.attempts: list[dict[str, Any]] = []

    async def publish(self, topic: str, key: str, event: dict[str, Any]) -> None:
        self.attempts.append(event)
        if self.fail:
            raise ConnectionError("kafka down")
        await super().publish(topic, key, event)


@dataclass
class Env:
    client: httpx.AsyncClient
    redis: FakeAsyncRedis
    backend: Backend
    publisher: RecordingPublisher
    dispatcher: EventDispatcher
    dlq: InMemoryPublisher
    settings: Settings

    async def send(self, topic: str, payload: dict, occurred_at: str | None = None) -> None:
        await self.dispatcher.process(
            topic,
            payload.get("product_id", "k"),
            {
                "event_id": str(uuid.uuid4()),
                "event_type": topic,
                "version": 1,
                "occurred_at": occurred_at or datetime.now(UTC).isoformat(),
                "payload": payload,
            },
        )

    async def product(
        self,
        price: int = 1000,
        name: str = "Mug",
        currency: str = "USD",
        active: bool = True,
        stock: int = 10,
        via_event: bool = True,
    ) -> str:
        """Create a product. ``via_event=False`` leaves the cart's local copy empty."""
        pid = str(uuid.uuid4())
        data = {
            "product_id": pid,
            "sku": f"SKU-{pid[:4]}",
            "name": name,
            "price_cents": price,
            "currency": currency,
            "is_active": active,
        }
        self.backend.products[pid] = {
            "id": pid,
            **{k: v for k, v in data.items() if k != "product_id"},
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self.backend.stock[pid] = stock
        if via_event:
            await self.send("product.created", data)
        return pid

    async def change_product(self, pid: str, **changes: Any) -> None:
        p = self.backend.products[pid]
        p.update(changes)
        data = {
            "product_id": pid,
            **{k: p[k] for k in ("sku", "name", "price_cents", "currency", "is_active")},
        }
        await self.send("product.updated", data)

    def address(self, user: str = "user-1") -> str:
        """Give a user a saved delivery address. Returns its id."""
        address_id = str(uuid.uuid4())
        self.backend.addresses.setdefault(user, []).append(
            {
                "id": address_id,
                "recipient": "Ada Lovelace",
                "line1": "12 Analytical St",
                "line2": "",
                "city": "London",
                "state": "",
                "postal_code": "N1 9GU",
                "country": "GB",
                "is_default": True,
            }
        )
        return address_id

    async def add(self, pid: str, quantity: int = 1, user: str = "user-1") -> httpx.Response:
        return await self.client.post(
            "/api/v1/cart/items",
            json={"product_id": pid, "quantity": quantity},
            headers=as_user(user),
        )

    async def view(self, user: str = "user-1") -> dict:
        resp = await self.client.get("/api/v1/cart", headers=as_user(user))
        assert resp.status_code == 200, resp.text
        return resp.json()["data"]


def uniform(resp: httpx.Response, status: int, success: bool) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == KEYS and body["success"] is success
    return body


@pytest.fixture
async def env():
    redis = FakeAsyncRedis(decode_responses=True)
    backend, publisher, dlq = Backend(), RecordingPublisher(), InMemoryPublisher()
    settings = Settings(
        product_url="http://product", inventory_url="http://inventory", user_url="http://user"
    )
    app = create_app(
        settings,
        redis_client=redis,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(backend)),
        publisher=publisher,
    )
    dispatcher = EventDispatcher(
        build_handlers(ProductStore(redis)), dlq, max_attempts=2, backoff_seconds=0
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cart"
    ) as client:
        yield Env(client, redis, backend, publisher, dispatcher, dlq, settings)
