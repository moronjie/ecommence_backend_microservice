# ruff: noqa: E501
"""The Postman collection generator (pure functions; no running stack needed)."""

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_postman_collection.py"
spec_ = importlib.util.spec_from_file_location("make_postman_collection", SCRIPT)
gen = importlib.util.module_from_spec(spec_)
spec_.loader.exec_module(gen)

SPEC = {
    "paths": {
        "/api/v1/auth/login": {
            "post": {
                "tags": ["auth"],
                "summary": "Login",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/LoginRequest"}
                        }
                    }
                },
            }
        },
        "/api/v1/products": {
            "get": {
                "tags": ["products"],
                "summary": "List products",
                "parameters": [
                    {"name": "size", "in": "query", "schema": {"type": "integer", "default": 20}}
                ],
            },
            "post": {
                "tags": ["products"],
                "summary": "Create a product",
                "security": [{"BearerAuth": []}],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ProductCreate"}
                        }
                    }
                },
            },
        },
        "/api/v1/products/{product_id}": {
            "get": {
                "tags": ["products"],
                "summary": "Get one",
                "parameters": [{"name": "product_id", "in": "path"}],
            }
        },
        "/api/v1/orders": {
            "get": {"tags": ["orders"], "summary": "My orders", "security": [{"BearerAuth": []}]}
        },
    },
    "components": {
        "schemas": {
            "LoginRequest": {
                "type": "object",
                "required": ["email", "password"],
                "properties": {
                    "email": {"type": "string", "format": "email"},
                    "password": {"type": "string"},
                },
            },
            "ProductCreate": {
                "type": "object",
                "required": ["sku", "name", "price_cents"],
                "properties": {
                    "sku": {"type": "string"},
                    "name": {"type": "string"},
                    "price_cents": {"type": "integer"},
                    "category_id": {
                        "anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]
                    },
                    "is_active": {"type": "boolean", "default": True},
                },
            },
        }
    },
}


def collection():
    return gen.build_collection(SPEC)


def folder(name):
    return next(f for f in collection()["item"] if f["name"] == name)


def request_named(folder_name, summary):
    return next(i for i in folder(folder_name)["item"] if i["name"] == summary)


def test_it_is_valid_json_in_postmans_format():
    parsed = json.loads(json.dumps(collection()))
    assert parsed["info"]["schema"].endswith("collection.json")
    assert parsed["auth"]["type"] == "bearer"  # one login for everything


def test_there_is_a_start_here_folder_first_and_one_folder_per_service():
    names = [f["name"] for f in collection()["item"]]
    assert names == ["Start here", "auth", "orders", "products"]
    start = [i["name"] for i in folder("Start here")["item"]]
    assert start[0].startswith("1. Register") and any("admin" in n for n in start)


def test_public_endpoints_send_no_token_and_protected_ones_inherit_it():
    assert request_named("products", "List products")["request"]["auth"] == {"type": "noauth"}
    assert "auth" not in request_named("products", "Create a product")["request"]
    assert "auth" not in request_named("orders", "My orders")["request"]


def test_paths_use_the_base_url_and_postman_path_variables():
    req = request_named("products", "Get one")["request"]
    assert req["url"]["raw"] == "{{baseUrl}}/api/v1/products/:product_id"
    assert req["url"]["variable"] == [{"key": "product_id", "value": "{{product_id}}"}]


def test_query_parameters_are_listed_but_switched_off():
    query = request_named("products", "List products")["request"]["url"]["query"]
    assert query == [{"key": "size", "value": "20", "disabled": True, "description": ""}]


def test_bodies_have_useful_example_values_not_just_the_word_string():
    login = json.loads(request_named("auth", "Login")["request"]["body"]["raw"])
    assert login == {"email": "{{customerEmail}}", "password": "{{customerPassword}}"}
    product = json.loads(request_named("products", "Create a product")["request"]["body"]["raw"])
    assert (
        product["sku"] == "SCARF-001"
        and product["price_cents"] == 12000
        and product["name"] == "Kente Scarf"
    )
    # an optional id that points at a variable is left out: until you create a category it would be
    # sent empty and rejected by the API
    assert "category_id" not in product and "is_active" not in product
    assert (
        request_named("products", "Create a product")["request"]["header"][0]["value"]
        == "application/json"
    )


def test_scripts_save_tokens_and_ids_so_later_requests_just_work():
    login = request_named("auth", "Login")["event"][0]["script"]["exec"]
    assert any("access_token" in line for line in login)
    create = request_named("products", "Create a product")["event"][0]["script"]["exec"]
    assert any('"product_id"' in line for line in create)
    orders = request_named("orders", "My orders")["event"][0]["script"]["exec"]
    assert any('"order_id"' in line for line in orders)


def test_every_variable_the_requests_use_is_declared_with_a_value():
    declared = {v["key"] for v in collection()["variable"]}
    text = json.dumps(collection())
    used = {part.split("}}")[0] for part in text.split("{{")[1:]}
    assert used <= declared, used - declared


def test_no_real_credentials_are_embedded():
    variables = {v["key"]: v["value"] for v in collection()["variable"]}
    assert variables["adminPassword"] == ""  # you fill this in; it is not shipped
    assert variables["access_token"] == "" and variables["refresh_token"] == ""
