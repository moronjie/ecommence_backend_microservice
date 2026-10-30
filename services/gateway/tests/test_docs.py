"""The combined API docs page."""

import httpx
import pytest
from app.core.config import Settings
from app.docs import merge_specs
from app.main import create_app
from fakeredis import FakeAsyncRedis

# Tiny stand-ins for what each service publishes at /openapi.json
AUTH = {
    "openapi": "3.1.0",
    "paths": {
        "/health": {"get": {"tags": ["health"], "summary": "Health"}},
        "/api/v1/auth/login": {"post": {"tags": ["auth"], "summary": "Login"}},
        "/api/v1/auth/logout": {
            "post": {
                "tags": ["auth"],
                "summary": "Logout",
                "parameters": [
                    {"name": "x-user-id", "in": "header"},  # internal: must be hidden
                    {"name": "authorization", "in": "header"},  # replaced by Authorize
                    {"name": "page", "in": "query"},  # a real parameter: must stay
                ],
            }
        },
    },
    "components": {
        "schemas": {
            "Item": {"type": "object", "properties": {"auth_field": {"type": "string"}}},
            "HTTPValidationError": {"type": "object"},
        }
    },
}
PRODUCT = {
    "openapi": "3.1.0",
    "paths": {
        "/health": {"get": {"tags": ["health"]}},
        "/metrics": {"get": {"tags": ["x"]}},
        "/api/v1/products": {
            "get": {
                "tags": ["products"],
                "summary": "List",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Item"}}
                        }
                    }
                },
            },
            "post": {"tags": ["products"], "summary": "Create"},
        },
        "/api/v1/products/{product_id}": {"get": {"tags": ["products"]}},
        "/api/v1/categories": {"get": {"tags": ["categories"]}},
    },
    # the same name as in auth, but a different shape
    "components": {
        "schemas": {
            "Item": {"type": "object", "properties": {"product_field": {"type": "integer"}}},
            "HTTPValidationError": {"type": "object"},  # identical: must not be renamed
        }
    },
}
SPECS = {"auth": AUTH, "product": PRODUCT}


def routes():
    from app.core.routes import build_routes

    return build_routes(Settings())


def merged(specs=SPECS):
    return merge_specs(specs, routes())


# ---- the merge ---------------------------------------------------------------------------------
def test_only_endpoints_the_gateway_really_routes_are_listed():
    paths = set(merged()["paths"])
    assert "/api/v1/auth/login" in paths and "/api/v1/products/{product_id}" in paths
    assert "/api/v1/categories" in paths
    assert not paths & {"/health", "/metrics"}  # internal to each service, not reachable here


def test_protected_endpoints_get_a_padlock_and_public_ones_do_not():
    paths = merged()["paths"]
    assert "security" not in paths["/api/v1/auth/login"]["post"]  # public
    assert "security" not in paths["/api/v1/products"]["get"]  # browsing is public
    assert paths["/api/v1/products"]["post"]["security"] == [{"BearerAuth": []}]  # creating is not
    assert paths["/api/v1/auth/logout"]["post"]["security"] == [{"BearerAuth": []}]


def test_a_bearer_token_scheme_exists_for_the_authorize_button():
    scheme = merged()["components"]["securitySchemes"]["BearerAuth"]
    assert (scheme["type"], scheme["scheme"]) == ("http", "bearer")


def test_internal_headers_are_hidden_but_real_parameters_stay():
    params = merged()["paths"]["/api/v1/auth/logout"]["post"]["parameters"]
    assert [p["name"] for p in params] == ["page"]


def test_schemas_with_the_same_name_but_different_shapes_are_not_overwritten():
    spec = merged()
    schemas = spec["components"]["schemas"]
    assert "auth_field" in schemas["Item"]["properties"]  # auth was first
    renamed = schemas["product_Item"]  # product's version kept under a new name
    assert "product_field" in renamed["properties"]
    # and product's endpoints now point at it
    ref = spec["paths"]["/api/v1/products"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert ref == "#/components/schemas/product_Item"
    assert "product_HTTPValidationError" not in schemas  # identical schemas are shared, not renamed


def test_tags_are_listed_once_each_and_sorted():
    assert [t["name"] for t in merged()["tags"]] == ["auth", "categories", "products"]


def test_a_service_that_is_down_is_named_not_fatal():
    spec = merged({"auth": AUTH, "product": None})
    assert "/api/v1/auth/login" in spec["paths"] and "/api/v1/products" not in spec["paths"]
    assert (
        "Not shown right now" in spec["info"]["description"]
        and "product" in spec["info"]["description"]
    )


def test_the_original_specs_are_not_modified():
    import copy

    before = copy.deepcopy(SPECS)
    merged()
    assert SPECS == before


# ---- the gateway endpoints ---------------------------------------------------------------------
class ServiceSpecs:
    """Answers GET /openapi.json for each service, by which port it is asked on."""

    def __init__(self):
        self.calls = 0
        self.down: set[int] = set()
        self.by_port = {8001: AUTH, 8003: PRODUCT}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/openapi.json":
            return httpx.Response(404)
        self.calls += 1
        port = request.url.port
        if port in self.down:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json=self.by_port.get(port, {"openapi": "3.1.0", "paths": {}}))


def gateway(upstream: ServiceSpecs, **overrides):
    app = create_app(
        Settings(jwt_secret="test-secret-that-is-long-enough-for-hs256", **overrides),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        redis_client=FakeAsyncRedis(decode_responses=True),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


async def test_openapi_json_is_the_merged_description():
    async with gateway(ServiceSpecs()) as c:
        resp = await c.get("/openapi.json")
    assert resp.status_code == 200
    body = resp.json()
    assert "/api/v1/auth/login" in body["paths"] and "/api/v1/products" in body["paths"]


async def test_docs_page_is_swagger_ui_reading_the_merged_description():
    async with gateway(ServiceSpecs()) as c:
        resp = await c.get("/docs")
    assert resp.status_code == 200 and "swagger-ui" in resp.text.lower()
    assert "/openapi.json" in resp.text and "persistAuthorization" in resp.text


async def test_the_page_needs_no_login_but_proxied_apis_still_do():
    upstream = ServiceSpecs()
    async with gateway(upstream) as c:
        assert (await c.get("/docs")).status_code == 200  # open
        assert (await c.get("/api/v1/orders")).status_code == 401  # the API itself is not
    assert upstream.calls == 0  # /docs alone does not even contact the services; /openapi.json does


async def test_the_merged_description_is_kept_for_a_minute():
    upstream = ServiceSpecs()
    async with gateway(upstream) as c:
        await c.get("/openapi.json")
        first = upstream.calls
        await c.get("/openapi.json")
        await c.get("/openapi.json")
    assert upstream.calls == first  # not re-fetched every time


async def test_a_down_service_does_not_break_the_page_and_is_not_cached_while_missing():
    upstream = ServiceSpecs()
    upstream.down = {8003}
    async with gateway(upstream) as c:
        partial = (await c.get("/openapi.json")).json()
        assert (
            "/api/v1/auth/login" in partial["paths"] and "/api/v1/products" not in partial["paths"]
        )
        assert "Not shown right now" in partial["info"]["description"]
        upstream.down = set()  # the product service comes back
        healed = (await c.get("/openapi.json")).json()
        assert "/api/v1/products" in healed["paths"]  # picked up at once, not after a minute


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
async def test_docs_are_off_in_production_unless_switched_on(path):
    async with gateway(ServiceSpecs(), docs_enabled=False) as c:
        assert (await c.get(path)).status_code == 404  # falls through to "no such route"


def test_the_default_depends_on_the_environment():
    assert Settings(environment="local").docs_active is True
    assert Settings(environment="production").docs_active is False
    assert Settings(environment="production", docs_enabled=True).docs_active is True
    assert Settings(environment="local", docs_enabled=False).docs_active is False
