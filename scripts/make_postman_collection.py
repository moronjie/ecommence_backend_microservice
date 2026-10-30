# ruff: noqa: E501
"""Generate a Postman collection from the gateway's combined API description.

    python scripts/make_postman_collection.py                 # needs the stack running
    python scripts/make_postman_collection.py --base-url http://localhost:8000 --out docs/postman

It reads  <base-url>/openapi.json  (the merged description of every service) and writes
docs/postman/shop.postman_collection.json, which you import into Postman (Import > File).

The collection is ready to use: a "Start here" folder registers and logs in a customer (and an admin),
a script saves the tokens, and further scripts save ids (product, order, address, payment reference)
so later requests work without copy-pasting. Everything goes through the gateway ({{baseUrl}}).
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
HTTP_METHODS = ("get", "post", "put", "patch", "delete")

# Path parameters become collection variables that the scripts below fill in as you go
PATH_VARIABLES = {
    "product_id": "{{product_id}}",
    "category_id": "{{category_id}}",
    "order_id": "{{order_id}}",
    "address_id": "{{address_id}}",
    "notification_id": "{{notification_id}}",
    "user_id": "{{user_id}}",
    "reference": "{{payment_reference}}",
}
# Sensible sample values, by field name, so bodies are not full of the word "string"
FIELD_HINTS: dict[str, Any] = {
    "email": "{{customerEmail}}",
    "password": "{{customerPassword}}",
    "full_name": "Ama Mensah",
    "recipient": "Ama Mensah",
    "name": "Kente Scarf",
    "sku": "SCARF-001",
    "description": "Hand-woven in Ghana",
    "price_cents": 12000,
    "currency": "GHS",
    "country": "GH",
    "city": "Accra",
    "line1": "12 Oxford Street",
    "postal_code": "GA-123",
    "phone": "+233 20 000 0000",
    "quantity": 1,
    "on_hand": 10,
    "delta": 5,
    "product_id": "{{product_id}}",
    "address_id": "{{address_id}}",
    "category_id": "{{category_id}}",
    "refresh_token": "{{refresh_token}}",
}

# Optional, but part of the normal flow (create an address first, then check out with it)
KEEP_OPTIONAL = {"address_id"}

SAVE_TOKENS = """\
// Save the tokens so every other request is logged in automatically
const d = pm.response.json().data;
if (d && d.access_token) {
  pm.collectionVariables.set("access_token", d.access_token);
  pm.collectionVariables.set("refresh_token", d.refresh_token);
}
"""
SAVE_BY_PATH = {
    ("POST", "/api/v1/products"): ("product_id", "d.id"),
    ("POST", "/api/v1/categories"): ("category_id", "d.id"),
    ("POST", "/api/v1/users/me/addresses"): ("address_id", "d.id"),
    ("GET", "/api/v1/orders"): (
        "order_id",
        "d.items && d.items.length ? d.items[0].id : undefined",
    ),
    ("GET", "/api/v1/payments/order/{order_id}"): (
        "payment_reference",
        'd.authorization_url ? d.authorization_url.split("/").pop() : undefined',
    ),
    ("GET", "/api/v1/auth/me"): ("user_id", "d.id"),
}


# ---- turning a schema into an example value ------------------------------------------------------
def resolve(schema: dict, components: dict) -> dict:
    seen = 0
    while "$ref" in schema and seen < 10:
        schema = components.get(schema["$ref"].rsplit("/", 1)[-1], {})
        seen += 1
    for key in ("allOf", "anyOf", "oneOf"):
        if key in schema:  # take the first real variant (not "null")
            options = [s for s in schema[key] if s.get("type") != "null"]
            if options:
                return resolve(options[0], components)
    return schema


def example(schema: dict, components: dict, name: str = "", depth: int = 0) -> Any:
    if depth > 4:
        return None
    schema = resolve(schema, components)
    if name in FIELD_HINTS:
        return FIELD_HINTS[name]
    if "example" in schema:
        return schema["example"]
    if schema.get("default") is not None:
        return schema["default"]
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        required = set(schema.get("required", []))

        # Required fields, plus optional ones we know a good sample for. An optional field whose
        # sample is a {{variable}} is left out: until that variable has a value it would send an
        # empty string and be rejected (for example an empty category_id).
        def wanted(key: str) -> bool:
            if key in required:
                return True
            if key not in FIELD_HINTS:
                return False
            return not str(FIELD_HINTS[key]).startswith("{{") or key in KEEP_OPTIONAL

        chosen = {k: v for k, v in props.items() if wanted(k)}
        return {k: example(v, components, k, depth + 1) for k, v in chosen.items()}
    if kind == "array":
        return [example(schema.get("items", {}), components, name, depth + 1)]
    if kind == "integer":
        return schema.get("minimum", 1)
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return False
    if schema.get("format") == "uuid":
        return "00000000-0000-0000-0000-000000000000"
    if schema.get("format") == "email":
        return "customer@example.com"
    return "string"


# ---- building the collection -----------------------------------------------------------------------
def event(script: str) -> dict:
    return {
        "listen": "test",
        "script": {"type": "text/javascript", "exec": script.rstrip("\n").split("\n")},
    }


def request_item(method: str, path: str, op: dict, components: dict) -> dict:
    parts = []
    for segment in path.strip("/").split("/"):
        parts.append(":" + segment[1:-1] if segment.startswith("{") else segment)
    query = [
        {
            "key": p["name"],
            "value": str(example(p.get("schema", {}), components, p["name"])),
            "disabled": True,  # off by default; tick the ones you want
            "description": p.get("description", ""),
        }
        for p in op.get("parameters", [])
        if p.get("in") == "query"
    ]
    variables = [
        {"key": p["name"], "value": PATH_VARIABLES.get(p["name"], "string")}
        for p in op.get("parameters", [])
        if p.get("in") == "path"
    ]
    url: dict[str, Any] = {
        "raw": "{{baseUrl}}/" + "/".join(parts),
        "host": ["{{baseUrl}}"],
        "path": parts,
    }
    if query:
        url["query"] = query
    if variables:
        url["variable"] = variables

    request: dict[str, Any] = {
        "method": method.upper(),
        "header": [],
        "url": url,
        "description": op.get("description") or op.get("summary", ""),
    }
    media = op.get("requestBody", {}).get("content", {}).get("application/json")
    if media:
        body = example(media.get("schema", {}), components)
        request["header"].append({"key": "Content-Type", "value": "application/json"})
        request["body"] = {
            "mode": "raw",
            "raw": json.dumps(body, indent=2),
            "options": {"raw": {"language": "json"}},
        }
    if "security" not in op:
        request["auth"] = {"type": "noauth"}  # public endpoint: send no token

    item: dict[str, Any] = {
        "name": op.get("summary") or f"{method.upper()} {path}",
        "request": request,
    }
    scripts = []
    if path in ("/api/v1/auth/login", "/api/v1/auth/refresh"):
        scripts.append(SAVE_TOKENS)
    if (method.upper(), path) in SAVE_BY_PATH:
        var, expression = SAVE_BY_PATH[(method.upper(), path)]
        scripts.append(
            f"const d = pm.response.json().data;\nconst v = {expression};\n"
            f'if (v) pm.collectionVariables.set("{var}", v);'
        )
    if scripts:
        item["event"] = [event("\n".join(scripts))]
    return item


def start_here() -> dict:
    def post(
        name: str, path: str, body: dict, script: str | None = None, public: bool = True
    ) -> dict:
        item: dict[str, Any] = {
            "name": name,
            "request": {
                "method": "POST",
                "header": [{"key": "Content-Type", "value": "application/json"}],
                "url": {
                    "raw": "{{baseUrl}}" + path,
                    "host": ["{{baseUrl}}"],
                    "path": path.strip("/").split("/"),
                },
                "body": {
                    "mode": "raw",
                    "raw": json.dumps(body, indent=2),
                    "options": {"raw": {"language": "json"}},
                },
            },
        }
        if public:
            item["request"]["auth"] = {"type": "noauth"}
        if script:
            item["event"] = [event(script)]
        return item

    return {
        "name": "Start here",
        "description": "Run these first. Logging in saves your tokens, and every other request uses them.",
        "item": [
            post(
                "1. Register a customer",
                "/api/v1/auth/register",
                {
                    "email": "{{customerEmail}}",
                    "password": "{{customerPassword}}",
                    "full_name": "Ama Mensah",
                },
            ),
            post(
                "2. Log in as the customer",
                "/api/v1/auth/login",
                {"email": "{{customerEmail}}", "password": "{{customerPassword}}"},
                SAVE_TOKENS,
            ),
            post(
                "2b. Log in as the admin (for adding products, shipping orders...)",
                "/api/v1/auth/login",
                {"email": "{{adminEmail}}", "password": "{{adminPassword}}"},
                SAVE_TOKENS,
            ),
            post(
                "3. Get new tokens (when a request says 401)",
                "/api/v1/auth/refresh",
                {"refresh_token": "{{refresh_token}}"},
                SAVE_TOKENS,
            ),
        ],
    }


def build_collection(spec: dict, base_url: str = "http://localhost:8000") -> dict:
    components = spec.get("components", {}).get("schemas", {})
    folders: dict[str, list[dict]] = {}
    for path, item in sorted(spec.get("paths", {}).items()):
        for method in HTTP_METHODS:
            op = item.get(method)
            if not op:
                continue
            tag = (op.get("tags") or ["other"])[0]
            folders.setdefault(tag, []).append(request_item(method, path, op, components))
    return {
        "info": {
            "name": "Shop API",
            "description": "Generated from the gateway's /openapi.json. Start with the 'Start here' folder.",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "auth": {
            "type": "bearer",
            "bearer": [{"key": "token", "value": "{{access_token}}", "type": "string"}],
        },
        "variable": [
            {"key": "baseUrl", "value": base_url},
            {"key": "customerEmail", "value": "ama@example.com"},
            {"key": "customerPassword", "value": "S3cure-passw0rd"},
            {"key": "adminEmail", "value": "admin@example.com"},
            {
                "key": "adminPassword",
                "value": "",
                "description": "The ADMIN_PASSWORD your stack was started with",
            },
            {"key": "access_token", "value": ""},
            {"key": "refresh_token", "value": ""},
            *({"key": k.strip("{}"), "value": ""} for k in dict.fromkeys(PATH_VARIABLES.values())),
        ],
        "item": [
            start_here(),
            *({"name": tag, "item": items} for tag, items in sorted(folders.items())),
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--out", default=str(ROOT / "docs" / "postman"))
    args = parser.parse_args()

    try:
        resp = httpx.get(f"{args.base_url}/openapi.json", timeout=20)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(
            f"Could not read {args.base_url}/openapi.json ({exc}). Is the stack running (make up)?"
        )
        return 1
    spec = resp.json()
    collection = build_collection(spec, args.base_url)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "shop.postman_collection.json"
    path.write_text(json.dumps(collection, indent=2) + "\n", encoding="utf-8")
    count = sum(len(f.get("item", [])) for f in collection["item"])
    missing = re.findall(r"Not shown right now.*", spec.get("info", {}).get("description", ""))
    print(
        f"Wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}: {count} requests in {len(collection['item'])} folders"
    )
    if missing:
        print("WARNING:", missing[0], "(start every service, then run this again)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
