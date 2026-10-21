import uuid

from conftest import as_user, uniform


async def checkout(env, user="user-1", **body):
    return await env.client.post("/api/v1/cart/checkout", json=body, headers=as_user(user))


async def test_checkout_publishes_the_order_request_and_empties_the_cart(env):
    mug, pan = await env.product(price=1000, name="Mug"), await env.product(price=4500, name="Pan")
    await env.add(mug, 2)
    await env.add(pan)
    address = env.address()

    result = uniform(await checkout(env, address_id=address), 202, True)["data"]
    assert result["total_cents"] == 6500 and result["currency"] == "USD"

    ((topic, key, event),) = env.publisher.events
    payload = event["payload"]
    assert (topic, key, event["event_type"]) == ("cart.checked_out", "user-1", "cart.checked_out")
    assert payload["checkout_id"] == result["checkout_id"] and payload["user_id"] == "user-1"
    assert payload["address_id"] == address and payload["total_cents"] == 6500
    assert payload["email"] == "user-1@example.com"  # the payment provider needs it
    # a COPY of the address travels with the order, so later edits cannot change it
    assert payload["shipping_address"] == {
        "recipient": "Ada Lovelace",
        "line1": "12 Analytical St",
        "line2": "",
        "city": "London",
        "state": "",
        "postal_code": "N1 9GU",
        "country": "GB",
    }
    assert sorted((i["name"], i["quantity"], i["unit_price_cents"]) for i in payload["items"]) == [
        ("Mug", 2, 1000),
        ("Pan", 1, 4500),
    ]
    assert (await env.view())["items"] == []


async def test_empty_cart_cannot_check_out(env):
    uniform(await checkout(env), 409, False)
    assert env.publisher.events == []


async def test_checkout_uses_current_prices_not_the_added_price(env):
    pid = await env.product(price=1000)
    await env.add(pid, 2)
    await env.change_product(pid, price_cents=1100)
    assert uniform(await checkout(env), 202, True)["data"]["total_cents"] == 2200


async def test_expected_total_guards_against_surprise_price_changes(env):
    pid = await env.product(price=1000)
    await env.add(pid, 2)
    await env.change_product(pid, price_cents=1100)

    stale = uniform(await checkout(env, expected_total_cents=2000), 409, False)
    assert stale["data"]["total_cents"] == 2200 and env.publisher.events == []
    assert len((await env.view())["items"]) == 1  # cart kept
    uniform(await checkout(env, expected_total_cents=2200), 202, True)


async def test_out_of_stock_blocks_checkout_and_keeps_the_cart(env):
    ok, short = await env.product(name="OK"), await env.product(name="Short", stock=2)
    await env.add(ok)
    await env.add(short, 5)

    body = uniform(await checkout(env), 409, False)
    (issue,) = body["data"]["issues"]
    assert issue["product_id"] == short and issue["requested"] == 5 and issue["available"] == 2
    assert env.publisher.events == [] and len((await env.view())["items"]) == 2


async def test_hidden_product_blocks_checkout(env):
    pid = await env.product()
    await env.add(pid)
    await env.change_product(pid, is_active=False)
    body = uniform(await checkout(env), 409, False)
    assert body["data"]["issues"][0]["problem"] == "no longer available"


async def test_checkout_needs_a_working_inventory_service(env):
    pid = await env.product()
    await env.add(pid)
    env.backend.inventory_down = True
    uniform(await checkout(env), 503, False)  # never start an order on a guess
    assert env.publisher.events == [] and len((await env.view())["items"]) == 1


# ---- safe retries ---------------------------------------------------------------------
async def test_kafka_down_keeps_the_cart_and_a_retry_reuses_the_same_checkout_id(env):
    pid = await env.product()
    await env.add(pid, 2)

    env.publisher.fail = True
    uniform(await checkout(env), 503, False)
    assert len((await env.view())["items"]) == 1  # not lost

    env.publisher.fail = False
    result = uniform(await checkout(env), 202, True)["data"]
    first_attempt = env.publisher.attempts[0]["payload"]["checkout_id"]
    assert result["checkout_id"] == first_attempt  # the order service can dedupe on this
    assert len(env.publisher.events) == 1


async def test_changing_the_cart_gives_a_new_checkout_id(env):
    pid = await env.product()
    await env.add(pid)
    env.publisher.fail = True
    await checkout(env)
    await env.add(pid)  # the cart changed between attempts
    await checkout(env)
    ids = [a["payload"]["checkout_id"] for a in env.publisher.attempts]
    assert ids[0] != ids[1]


async def test_a_new_cart_after_checkout_never_reuses_an_old_checkout_id(env):
    pid = await env.product()
    ids = []
    for _ in range(2):
        await env.add(pid)  # same user, same shape of cart each time
        ids.append(uniform(await checkout(env), 202, True)["data"]["checkout_id"])
    assert ids[0] != ids[1]


async def test_checkout_validates_the_request(env):
    pid = await env.product()
    await env.add(pid)
    uniform(await checkout(env, address_id="not-a-uuid"), 422, False)
    uniform(await checkout(env, expected_total_cents=-1), 422, False)


async def test_health_and_ready(env):
    uniform(await env.client.get("/health"), 200, True)
    uniform(await env.client.get("/ready"), 200, True)


# ---- delivery address -----------------------------------------------------------------
async def test_checkout_without_an_address_still_works(env):
    await env.add(await env.product())
    uniform(await checkout(env), 202, True)
    payload = env.publisher.events[0][2]["payload"]
    assert payload["address_id"] is None and payload["shipping_address"] is None
    assert not any("/addresses" in c for c in env.backend.calls)


async def test_someone_elses_or_unknown_address_is_refused(env):
    await env.add(await env.product())
    bobs = env.address(user="bob")  # exists, but belongs to another user
    uniform(await checkout(env, address_id=bobs), 409, False)
    uniform(await checkout(env, address_id=str(uuid.uuid4())), 409, False)
    assert env.publisher.events == [] and len((await env.view())["items"]) == 1


async def test_checkout_with_an_address_needs_the_user_service(env):
    await env.add(await env.product())
    address = env.address()
    env.backend.user_service_down = True
    uniform(await checkout(env, address_id=address), 503, False)
    assert env.publisher.events == [] and len((await env.view())["items"]) == 1
    env.backend.user_service_down = False
    uniform(await checkout(env, address_id=address), 202, True)


async def test_checkout_needs_a_profile_to_know_the_email(env):
    await env.add(await env.product())
    env.backend.no_profile.add("user-1")
    uniform(await checkout(env), 409, False)  # profile not created yet: try again shortly
    assert env.publisher.events == [] and len((await env.view())["items"]) == 1
    env.backend.no_profile.clear()
    uniform(await checkout(env), 202, True)
