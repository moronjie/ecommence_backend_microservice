import uuid

from conftest import ADMIN, CUSTOMER, uniform


# ---- permissions ----------------------------------------------------------------------
async def test_only_admins_can_write(env):
    cat = await env.category()
    pid = (await env.product())["id"]
    body = {"sku": "X1", "name": "X", "price_cents": 1}
    writes = [
        ("post", "/api/v1/products", {"json": body}),
        ("patch", f"/api/v1/products/{pid}", {"json": {"name": "Y"}}),
        ("delete", f"/api/v1/products/{pid}", {}),
        ("post", "/api/v1/categories", {"json": {"name": "N"}}),
        ("put", f"/api/v1/categories/{cat['id']}", {"json": {"name": "N"}}),
        ("delete", f"/api/v1/categories/{cat['id']}", {}),
    ]
    for method, url, kwargs in writes:
        for headers in ({}, CUSTOMER):  # anonymous and an ordinary customer
            resp = await getattr(env.client, method)(url, headers=headers, **kwargs)
            uniform(resp, 403, False)


async def test_reads_are_public(env):
    pid = (await env.product())["id"]
    uniform(await env.client.get("/api/v1/products"), 200, True)
    uniform(await env.client.get(f"/api/v1/products/{pid}"), 200, True)
    uniform(await env.client.get("/api/v1/categories"), 200, True)


# ---- create / validate ----------------------------------------------------------------
async def test_create_normalises_sku_and_currency(env):
    p = await env.product(sku="mug-01", currency="eur")
    assert p["sku"] == "MUG-01" and p["currency"] == "EUR" and p["price_cents"] == 1999


async def test_duplicate_sku_conflicts_case_insensitively(env):
    await env.product(sku="abc")
    body = {"sku": "ABC", "name": "Other", "price_cents": 5}
    uniform(await env.client.post("/api/v1/products", json=body, headers=ADMIN), 409, False)


async def test_validation(env):
    bad_bodies = [
        {"sku": "A", "name": "N", "price_cents": -1},
        {"sku": "A", "name": "N", "price_cents": 19.99},  # floats are not money
        {"sku": "has space", "name": "N", "price_cents": 1},
        {"sku": "A", "name": "", "price_cents": 1},
        {"sku": "A", "name": "N", "price_cents": 1, "currency": "DOLLARS"},
    ]
    for body in bad_bodies:
        uniform(await env.client.post("/api/v1/products", json=body, headers=ADMIN), 422, False)


async def test_unknown_category_rejected(env):
    body = {"sku": "A", "name": "N", "price_cents": 1, "category_id": str(uuid.uuid4())}
    uniform(await env.client.post("/api/v1/products", json=body, headers=ADMIN), 422, False)


# ---- update / delete ------------------------------------------------------------------
async def test_partial_update_changes_only_sent_fields(env):
    p = await env.product(name="Mug", price_cents=1000)
    resp = await env.client.patch(
        f"/api/v1/products/{p['id']}", json={"price_cents": 1500}, headers=ADMIN
    )
    data = uniform(resp, 200, True)["data"]
    assert data["price_cents"] == 1500 and data["name"] == "Mug" and data["sku"] == p["sku"]


async def test_category_can_be_removed_with_null(env):
    cat = await env.category()
    p = await env.product(category_id=cat["id"])
    resp = await env.client.patch(
        f"/api/v1/products/{p['id']}", json={"category_id": None}, headers=ADMIN
    )
    assert uniform(resp, 200, True)["data"]["category_id"] is None


async def test_update_missing_product_is_404(env):
    resp = await env.client.patch(
        f"/api/v1/products/{uuid.uuid4()}", json={"name": "x"}, headers=ADMIN
    )
    uniform(resp, 404, False)


async def test_delete_hides_product_from_public_but_not_admin(env):
    p = await env.product()
    uniform(await env.client.delete(f"/api/v1/products/{p['id']}", headers=ADMIN), 200, True)

    uniform(await env.client.get(f"/api/v1/products/{p['id']}"), 404, False)
    public = uniform(await env.client.get("/api/v1/products"), 200, True)["data"]
    assert public["total"] == 0

    seen = await env.client.get(f"/api/v1/products/{p['id']}", headers=ADMIN)
    assert uniform(seen, 200, True)["data"]["is_active"] is False
    listing = await env.client.get("/api/v1/products?include_inactive=true", headers=ADMIN)
    assert uniform(listing, 200, True)["data"]["total"] == 1


async def test_include_inactive_is_admin_only(env):
    uniform(await env.client.get("/api/v1/products?include_inactive=true"), 403, False)
    resp = await env.client.get("/api/v1/products?include_inactive=true", headers=CUSTOMER)
    uniform(resp, 403, False)


# ---- search / filter / sort / paginate ------------------------------------------------
async def seed(env):
    books, toys = await env.category("Books"), await env.category("Toys")
    await env.product(
        "B1", "Python Cookbook", 4500, description="Recipes for code", category_id=books["id"]
    )
    await env.product("B2", "Rust Handbook", 3000, category_id=books["id"])
    await env.product(
        "T1", "Wooden Train", 2500, description="A classic toy", category_id=toys["id"]
    )
    await env.product("T2", "Puzzle 100%", 1200, category_id=toys["id"])
    return books, toys


def names(resp) -> list[str]:
    return [p["name"] for p in resp.json()["data"]["items"]]


async def test_search_matches_name_and_description_case_insensitively(env):
    await seed(env)
    assert names(await env.client.get("/api/v1/products?q=PYTHON")) == ["Python Cookbook"]
    assert names(await env.client.get("/api/v1/products?q=recipes")) == ["Python Cookbook"]
    assert names(await env.client.get("/api/v1/products?q=nothing-matches")) == []


async def test_search_treats_wildcards_as_plain_text(env):
    await seed(env)
    assert names(await env.client.get("/api/v1/products?q=%25")) == ["Puzzle 100%"]
    assert names(await env.client.get("/api/v1/products?q=_")) == []


async def test_filter_by_category_and_price(env):
    books, _ = await seed(env)
    resp = await env.client.get(f"/api/v1/products?category_id={books['id']}&sort=price&order=asc")
    assert names(resp) == ["Rust Handbook", "Python Cookbook"]
    resp = await env.client.get(
        "/api/v1/products?min_price=2000&max_price=3000&sort=name&order=asc"
    )
    assert names(resp) == ["Rust Handbook", "Wooden Train"]


async def test_sorting(env):
    await seed(env)
    asc = await env.client.get("/api/v1/products?sort=price&order=asc")
    desc = await env.client.get("/api/v1/products?sort=price&order=desc")
    assert names(asc) == names(desc)[::-1]
    assert names(asc)[0] == "Puzzle 100%"


async def test_pagination_covers_everything_once(env):
    await seed(env)
    seen = []
    for page in (1, 2, 3):
        resp = await env.client.get(f"/api/v1/products?size=2&page={page}&sort=name&order=asc")
        data = resp.json()["data"]
        assert data["total"] == 4 and data["page"] == page and data["size"] == 2
        seen += names(resp)
    assert seen == sorted(seen) and len(seen) == 4  # page 3 is empty


async def test_bad_query_parameters_are_422(env):
    for query in (
        "size=1000",
        "page=0",
        "sort=color",
        "order=sideways",
        "min_price=-1",
        "min_price=500&max_price=100",
        "category_id=not-a-uuid",
    ):
        uniform(await env.client.get(f"/api/v1/products?{query}"), 422, False)


# ---- categories -----------------------------------------------------------------------
async def test_category_slug_and_duplicates(env):
    cat = await env.category("Home & Garden")
    assert cat["slug"] == "home-garden"
    resp = await env.client.post("/api/v1/categories", json={"name": "home garden"}, headers=ADMIN)
    uniform(resp, 409, False)  # same slug
    uniform(
        await env.client.post("/api/v1/categories", json={"name": "!!!"}, headers=ADMIN), 422, False
    )


async def test_category_update_and_delete_rules(env):
    cat = await env.category("Books")
    resp = await env.client.put(
        f"/api/v1/categories/{cat['id']}", json={"name": "Reading"}, headers=ADMIN
    )
    assert uniform(resp, 200, True)["data"]["slug"] == "reading"

    await env.product(category_id=cat["id"])
    uniform(await env.client.delete(f"/api/v1/categories/{cat['id']}", headers=ADMIN), 409, False)

    empty = await env.category("Empty")
    uniform(await env.client.delete(f"/api/v1/categories/{empty['id']}", headers=ADMIN), 200, True)
    names_left = [
        c["name"] for c in (await env.client.get("/api/v1/categories")).json()["data"]["items"]
    ]
    assert names_left == ["Reading"]


async def test_health_and_ready(env):
    uniform(await env.client.get("/health"), 200, True)
    uniform(await env.client.get("/ready"), 200, True)
