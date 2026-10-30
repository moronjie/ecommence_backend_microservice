import asyncio
import copy
import logging
import time
from typing import Any

import httpx

from app.core.routes import Route, match_route

logger = logging.getLogger(__name__)

SCHEMA_PREFIX = "#/components/schemas/"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
# Headers the gateway sets itself. A client must never send them, so they do not belong in the docs.
HIDDEN_HEADERS = {
    "x-user-id",
    "x-user-role",
    "x-identity-timestamp",
    "x-identity-signature",
    "authorization",  # handled by the Authorize button
}
FETCH_TIMEOUT_SECONDS = 3.0
CACHE_SECONDS = 60

DESCRIPTION = """The whole shop's API, through the gateway.

**To call protected endpoints:** open `POST /api/v1/auth/login` below and press *Try it out*.
Copy the `access_token` from the response, press the **Authorize** button at the top and paste
it. The padlock on an endpoint shows it needs that login. Access tokens last 15 minutes; use
`POST /api/v1/auth/refresh` to get a new one.

Every response has the same shape: `{"success", "message", "data", "error_message"}`.
Prices are whole numbers in pesewas (`12000` = GHS 120.00).
"""


def _rewrite_refs(node: Any, renames: dict[str, str]) -> None:
    """Point every ``$ref`` at a renamed schema (in place)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith(SCHEMA_PREFIX):
                name = value[len(SCHEMA_PREFIX) :]
                node[key] = SCHEMA_PREFIX + renames.get(name, name)
            else:
                _rewrite_refs(value, renames)
    elif isinstance(node, list):
        for item in node:
            _rewrite_refs(item, renames)


def service_labels(routes: list[Route]) -> dict[str, str]:
    """upstream URL -> a short, stable name (several routes can share one service)."""
    names: dict[str, list[str]] = {}
    for route in routes:
        names.setdefault(route.upstream, []).append(route.name)
    return {upstream: min(group, key=len) for upstream, group in names.items()}


async def fetch_specs(http: httpx.AsyncClient, routes: list[Route]) -> dict[str, dict | None]:
    """Each service's own description. A service that cannot be reached maps to None."""
    labels = service_labels(routes)

    async def fetch(upstream: str) -> dict | None:
        try:
            resp = await http.get(f"{upstream}/openapi.json", timeout=FETCH_TIMEOUT_SECONDS)
            return resp.json() if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            logger.warning("Could not read the API description of %s", upstream)
            return None

    results = await asyncio.gather(*(fetch(u) for u in labels))
    return {labels[u]: spec for u, spec in zip(labels, results, strict=True)}


def merge_specs(
    specs: dict[str, dict | None], routes: list[Route], title: str = "Shop API"
) -> dict:
    merged: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": title, "version": "1.0.0", "description": DESCRIPTION},
        "paths": {},
        "components": {
            "schemas": {},
            "securitySchemes": {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                    "description": "The access_token from POST /api/v1/auth/login",
                }
            },
        },
        "tags": [],
    }
    unavailable: list[str] = []
    seen_tags: list[str] = []

    for service, original in sorted(specs.items()):
        if original is None:
            unavailable.append(service)
            continue
        spec = copy.deepcopy(original)
        schemas = spec.get("components", {}).get("schemas", {})

        # A name two services define differently gets this service's name in front of it
        renames = {
            name: f"{service}_{name}"
            for name, schema in schemas.items()
            if name in merged["components"]["schemas"]
            and merged["components"]["schemas"][name] != schema
        }
        _rewrite_refs(spec, renames)
        for name, schema in schemas.items():
            merged["components"]["schemas"].setdefault(renames.get(name, name), schema)

        for path, item in spec.get("paths", {}).items():
            route = match_route(routes, path)
            if route is None:  # /health, /metrics, ...: not reachable through the gateway
                continue
            for method, operation in item.items():
                if method not in HTTP_METHODS:
                    continue
                operation["parameters"] = [
                    p
                    for p in operation.get("parameters", [])
                    if not (p.get("in") == "header" and p.get("name", "").lower() in HIDDEN_HEADERS)
                ]
                if not operation["parameters"]:
                    del operation["parameters"]
                if not route.is_public(method.upper(), path):
                    operation["security"] = [{"BearerAuth": []}]  # shows the padlock
                for tag in operation.get("tags", []):
                    if tag not in seen_tags:
                        seen_tags.append(tag)
                merged["paths"].setdefault(path, {})[method] = operation

    merged["tags"] = [{"name": tag} for tag in sorted(seen_tags)]
    if unavailable:
        merged["info"]["description"] += (
            "\n\n**Not shown right now** (service unreachable): " + ", ".join(sorted(unavailable))
        )
    return merged


class DocsBuilder:
    """Builds the merged description and keeps it for a minute, so the page is quick."""

    def __init__(self, routes: list[Route], ttl_seconds: float = CACHE_SECONDS) -> None:
        self.routes = routes
        self.ttl = ttl_seconds
        self._cached: tuple[float, dict] | None = None

    async def spec(self, http: httpx.AsyncClient) -> dict:
        if self._cached and time.monotonic() - self._cached[0] < self.ttl:
            return self._cached[1]
        specs = await fetch_specs(http, self.routes)
        merged = merge_specs(specs, self.routes)
        if all(s is not None for s in specs.values()):  # do not keep a half-empty page for a minute
            self._cached = (time.monotonic(), merged)
        return merged
