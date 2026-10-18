import uuid

from conftest import as_user

KEYS = {"success", "message", "data", "error_message"}
ADDR = {
    "recipient": "Ada Lovelace",
    "line1": "12 Analytical St",
    "city": "London",
    "postal_code": "N1 9GU",
    "country": "gb",
}


def uniform(resp, status, success):
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == KEYS and body["success"] is success
    return body


async def add(env, uid, **over):
    return await env.client.post(
        "/api/v1/users/me/addresses", json={**ADDR, **over}, headers=as_user(uid)
    )


# ---- profile --------------------------------------------------------------------------
async def test_get_own_profile(env):
    uid = await env.register_user("ada@example.com", "Ada")
    body = uniform(await env.client.get("/api/v1/users/me", headers=as_user(uid)), 200, True)
    assert body["data"]["email"] == "ada@example.com" and body["data"]["user_id"] == uid


async def test_profile_not_ready_yet_is_404_with_hint(env):
    resp = await env.client.get("/api/v1/users/me", headers=as_user(str(uuid.uuid4())))
    assert "try again" in uniform(resp, 404, False)["error_message"]


async def test_requires_identity_header(env):
    uniform(await env.client.get("/api/v1/users/me"), 401, False)


async def test_update_profile_changes_only_sent_fields(env):
    uid = await env.register_user(full_name="Ada")
    resp = await env.client.patch(
        "/api/v1/users/me", json={"phone": "+44 20 7946 0000"}, headers=as_user(uid)
    )
    data = uniform(resp, 200, True)["data"]
    assert data["phone"] == "+44 20 7946 0000" and data["full_name"] == "Ada"


async def test_update_profile_validation(env):
    uid = await env.register_user()
    resp = await env.client.patch("/api/v1/users/me", json={"phone": "abc"}, headers=as_user(uid))
    uniform(resp, 422, False)


# ---- addresses ------------------------------------------------------------------------
async def test_first_address_becomes_default_and_country_uppercased(env):
    uid = await env.register_user()
    data = uniform(await add(env, uid), 201, True)["data"]
    assert data["is_default"] is True and data["country"] == "GB"


async def test_new_default_replaces_old_default(env):
    uid = await env.register_user()
    first = (await add(env, uid)).json()["data"]
    second = (await add(env, uid, line1="2 Other Rd", is_default=True)).json()["data"]
    listing = (await env.client.get("/api/v1/users/me/addresses", headers=as_user(uid))).json()
    defaults = [a["id"] for a in listing["data"]["items"] if a["is_default"]]
    assert defaults == [second["id"]] and first["id"] != second["id"]


async def test_update_address(env):
    uid = await env.register_user()
    addr = (await add(env, uid)).json()["data"]
    resp = await env.client.put(
        f"/api/v1/users/me/addresses/{addr['id']}",
        json={**ADDR, "city": "Leeds"},
        headers=as_user(uid),
    )
    data = uniform(resp, 200, True)["data"]
    assert data["city"] == "Leeds" and data["is_default"] is True  # default is kept


async def test_deleting_default_promotes_another(env):
    uid = await env.register_user()
    first = (await add(env, uid)).json()["data"]
    second = (await add(env, uid, line1="2 Other Rd")).json()["data"]
    resp = await env.client.delete(
        f"/api/v1/users/me/addresses/{first['id']}", headers=as_user(uid)
    )
    uniform(resp, 200, True)
    items = (await env.client.get("/api/v1/users/me/addresses", headers=as_user(uid))).json()
    assert [(a["id"], a["is_default"]) for a in items["data"]["items"]] == [(second["id"], True)]


async def test_cannot_touch_someone_elses_address(env):
    ada, bob = await env.register_user("a@x.com"), await env.register_user("b@x.com")
    addr = (await add(env, ada)).json()["data"]
    url = f"/api/v1/users/me/addresses/{addr['id']}"
    uniform(await env.client.put(url, json=ADDR, headers=as_user(bob)), 404, False)
    uniform(await env.client.delete(url, headers=as_user(bob)), 404, False)
    bobs = (await env.client.get("/api/v1/users/me/addresses", headers=as_user(bob))).json()
    assert bobs["data"]["items"] == []


async def test_address_limit_and_validation(env):
    uid = await env.register_user()
    uniform(await add(env, uid, country="GBR"), 422, False)
    uniform(await add(env, uid, line1=""), 422, False)
    for i in range(10):
        uniform(await add(env, uid, line1=f"{i} Road"), 201, True)
    uniform(await add(env, uid, line1="one too many"), 409, False)


async def test_bad_address_id_is_404(env):
    uid = await env.register_user()
    resp = await env.client.delete("/api/v1/users/me/addresses/not-a-uuid", headers=as_user(uid))
    uniform(resp, 404, False)


# ---- admin ----------------------------------------------------------------------------
async def test_admin_can_list_and_get_users_customers_cannot(env):
    uid = await env.register_user("a@x.com")
    for i in range(3):
        await env.register_user(f"u{i}@x.com")
    admin = as_user(str(uuid.uuid4()), "admin")

    page = uniform(await env.client.get("/api/v1/users?page=1&size=2", headers=admin), 200, True)
    assert page["data"]["total"] == 4 and len(page["data"]["items"]) == 2
    assert page["data"]["page"] == 1 and page["data"]["size"] == 2
    one = uniform(await env.client.get(f"/api/v1/users/{uid}", headers=admin), 200, True)
    assert one["data"]["email"] == "a@x.com"

    uniform(await env.client.get("/api/v1/users", headers=as_user(uid)), 403, False)
    uniform(await env.client.get(f"/api/v1/users/{uid}", headers=as_user(uid)), 403, False)


async def test_admin_list_validates_paging(env):
    admin = as_user(str(uuid.uuid4()), "admin")
    uniform(await env.client.get("/api/v1/users?size=1000", headers=admin), 422, False)
    uniform(await env.client.get("/api/v1/users?page=0", headers=admin), 422, False)


async def test_health_and_ready(env):
    uniform(await env.client.get("/health"), 200, True)
    uniform(await env.client.get("/ready"), 200, True)
