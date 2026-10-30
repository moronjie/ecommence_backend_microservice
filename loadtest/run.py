# ruff: noqa: E501
import argparse
import asyncio
import random
import time
import uuid
from collections import defaultdict

import httpx

PASSWORD = "S3cure-passw0rd"


class Stats:
    def __init__(self) -> None:
        self.times: dict[str, list[float]] = defaultdict(list)
        self.errors: dict[str, int] = defaultdict(int)
        self.statuses: dict[int, int] = defaultdict(int)

    def record(
        self, name: str, seconds: float, status: int, expected: tuple[int, ...] = (200, 201, 202)
    ) -> None:
        self.times[name].append(seconds)
        self.statuses[status] += 1
        if status not in expected:
            self.errors[name] += 1


async def timed(
    client: httpx.AsyncClient,
    stats: Stats,
    name: str,
    method: str,
    url: str,
    token: str | None = None,
    expected: tuple[int, ...] = (200, 201, 202),
    **kw,
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    start = time.perf_counter()
    try:
        resp = await client.request(method, url, headers=headers, **kw)
    except httpx.HTTPError:
        stats.record(name, time.perf_counter() - start, 0, expected)
        return httpx.Response(599)
    stats.record(name, time.perf_counter() - start, resp.status_code, expected)
    return resp


async def setup(client: httpx.AsyncClient, args) -> tuple[list[str], list[tuple[str, str]]]:
    """Create products with plenty of stock, and customers with a profile and an address."""
    login = await client.post(
        "/api/v1/auth/login", json={"email": args.admin_email, "password": args.admin_password}
    )
    login.raise_for_status()
    admin = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

    products = []
    for i in range(args.products):
        sku = f"LOAD-{uuid.uuid4().hex[:8]}"
        r = await client.post(
            "/api/v1/products",
            headers=admin,
            json={"sku": sku, "name": f"Load Test Item {i}", "price_cents": 1000 + i * 50},
        )
        pid = r.json()["data"]["id"]
        for _ in range(60):
            if (
                await client.put(
                    f"/api/v1/inventory/{pid}", headers=admin, json={"on_hand": 1_000_000}
                )
            ).status_code == 200:
                break
            await asyncio.sleep(0.5)
        products.append(pid)

    customers = []
    for _ in range(args.users):
        email = f"load-{uuid.uuid4().hex[:10]}@example.com"
        await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": PASSWORD, "full_name": "Load"},
        )
        token = (
            await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        ).json()["data"]["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        for _ in range(60):  # the profile is created from an event
            if (await client.get("/api/v1/users/me", headers=auth)).status_code == 200:
                break
            await asyncio.sleep(0.5)
        addr = await client.post(
            "/api/v1/users/me/addresses",
            headers=auth,
            json={
                "recipient": "Load",
                "line1": "1 Test St",
                "city": "Accra",
                "postal_code": "GA1",
                "country": "GH",
            },
        )
        customers.append((token, addr.json()["data"]["id"]))
    return products, customers


async def shopper(client, stats, products, token, address, deadline) -> None:
    while time.time() < deadline:
        roll = random.random()
        pid = random.choice(products)
        if roll < 0.60:
            await timed(
                client, stats, "browse: list", "GET", "/api/v1/products", params={"size": 20}
            )
            await timed(
                client,
                stats,
                "browse: search",
                "GET",
                "/api/v1/products",
                params={"q": "load", "sort": "price"},
            )
            await timed(client, stats, "browse: product", "GET", f"/api/v1/products/{pid}")
        elif roll < 0.90:
            await timed(
                client,
                stats,
                "cart: add item",
                "POST",
                "/api/v1/cart/items",
                token,
                json={"product_id": pid, "quantity": 1},
            )
            await timed(client, stats, "cart: view", "GET", "/api/v1/cart", token)
        else:
            await timed(
                client,
                stats,
                "checkout: add item",
                "POST",
                "/api/v1/cart/items",
                token,
                json={"product_id": pid, "quantity": 1},
            )
            await timed(
                client,
                stats,
                "checkout: place order",
                "POST",
                "/api/v1/cart/checkout",
                token,
                json={"address_id": address},
            )
            await timed(client, stats, "checkout: list orders", "GET", "/api/v1/orders", token)
        await asyncio.sleep(random.uniform(0.05, 0.3))  # a person thinks for a moment


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def report(stats: Stats, seconds: float) -> None:
    total = sum(len(v) for v in stats.times.values())
    print(
        f"\n{'request':<24}{'count':>7}{'errors':>8}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}   (milliseconds)"
    )
    print("-" * 82)
    for name in sorted(stats.times):
        t = [x * 1000 for x in stats.times[name]]
        print(
            f"{name:<24}{len(t):>7}{stats.errors[name]:>8}{percentile(t, 0.5):>9.0f}{percentile(t, 0.95):>9.0f}{percentile(t, 0.99):>9.0f}{max(t):>9.0f}"
        )
    everything = [x * 1000 for v in stats.times.values() for x in v]
    errors = sum(stats.errors.values())
    print("-" * 82)
    print(
        f"{'ALL':<24}{total:>7}{errors:>8}{percentile(everything, 0.5):>9.0f}{percentile(everything, 0.95):>9.0f}{percentile(everything, 0.99):>9.0f}{max(everything):>9.0f}"
    )
    print(
        f"\n{total / seconds:.1f} requests/second over {seconds:.0f}s, {errors / max(total, 1) * 100:.2f}% errors"
    )
    print("status codes:", dict(sorted(stats.statuses.items())))
    if stats.statuses.get(429):
        print(
            "NOTE: 429 = rate limited. Raise RATE_LIMIT_REQUESTS on the gateway for a real load test."
        )


async def main(args) -> None:
    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=30, limits=httpx.Limits(max_connections=args.users * 2 + 10)
    ) as client:
        print(f"preparing {args.products} products and {args.users} customers ...")
        products, customers = await setup(client, args)
        stats = Stats()
        print(f"running {args.users} shoppers for {args.duration}s ...")
        start = time.time()
        deadline = start + args.duration
        await asyncio.gather(
            *(shopper(client, stats, products, t, a, deadline) for t, a in customers)
        )
        report(stats, time.time() - start)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--admin-password", required=True)
    parser.add_argument("--users", type=int, default=20, help="concurrent shoppers")
    parser.add_argument("--products", type=int, default=15)
    parser.add_argument("--duration", type=int, default=30, help="seconds")
    asyncio.run(main(parser.parse_args()))
