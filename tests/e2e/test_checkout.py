import uuid

from conftest import eventually


def make_product(shop, admin, price_cents=12000, stock=10) -> str:
    sku = f"E2E-{uuid.uuid4().hex[:8]}"
    product = shop.data(
        "POST",
        "/api/v1/products",
        admin,
        ok=201,
        json={"sku": sku, "name": f"E2E Kente Scarf {sku}", "price_cents": price_cents},
    )
    pid = product["id"]
    # product.created travels through Kafka to the inventory service, which creates the stock row
    eventually(
        lambda: shop.call("GET", f"/api/v1/inventory/{pid}", admin).status_code == 200,
        "the inventory service to learn about the new product (Kafka)",
    )
    shop.data("PUT", f"/api/v1/inventory/{pid}", admin, json={"on_hand": stock})
    return pid


def stock(shop, admin, pid) -> tuple[int, int]:
    d = shop.data("GET", f"/api/v1/inventory/{pid}", admin)
    return d["on_hand"], d["reserved"]


def ready_customer(shop):
    """A customer with a profile (created from user.registered through Kafka) and an address."""
    email, token = shop.new_customer()
    eventually(
        lambda: shop.call("GET", "/api/v1/users/me", token).status_code == 200,
        "the user service to create the profile (Kafka)",
    )
    address = shop.data(
        "POST",
        "/api/v1/users/me/addresses",
        token,
        ok=201,
        json={
            "recipient": "E2E Customer",
            "line1": "12 Oxford Street",
            "city": "Accra",
            "postal_code": "GA-123",
            "country": "GH",
        },
    )
    return email, token, address["id"]


def place_order(shop, token, address_id, pid, quantity) -> str:
    shop.call("DELETE", "/api/v1/cart", token)
    shop.data("POST", "/api/v1/cart/items", token, json={"product_id": pid, "quantity": quantity})
    shop.data("POST", "/api/v1/cart/checkout", token, ok=202, json={"address_id": address_id})
    # cart -> order -> inventory -> order, all by events
    order = eventually(
        lambda: next(
            (
                o
                for o in shop.data("GET", "/api/v1/orders", token)["items"]
                if o["status"] == "awaiting_payment"
            ),
            None,
        ),
        "the order to be created and its stock reserved (Kafka, three services)",
    )
    return order["id"]


def pay(shop, token, order_id, outcome="success"):
    link = eventually(
        lambda: (lambda p: p if p.get("authorization_url") else None)(
            shop.data("GET", f"/api/v1/payments/order/{order_id}", token)
        ),
        "the payment service to start the payment (Kafka)",
    )
    reference = link["authorization_url"].rsplit("/", 1)[1]
    shop.data("POST", f"/api/v1/payments/dev/{reference}/pay", token, params={"outcome": outcome})
    return link


def test_a_paid_order_is_confirmed_stock_is_taken_and_the_customer_is_emailed(
    shop, admin_token, mailbox
):
    pid = make_product(shop, admin_token, price_cents=12000, stock=10)
    email, token, address = ready_customer(shop)

    order_id = place_order(shop, token, address, pid, quantity=2)
    assert stock(shop, admin_token, pid) == (10, 2)  # held while we wait for payment

    link = pay(shop, token, order_id)
    assert (link["amount_cents"], link["currency"]) == (24000, "GHS")  # GHS 240.00 in pesewas

    eventually(
        lambda: shop.data("GET", f"/api/v1/orders/{order_id}", token)["status"] == "confirmed",
        "the order to be confirmed after payment (Kafka)",
    )
    # order.confirmed reaches the inventory service through Kafka a moment after the order itself
    eventually(
        lambda: stock(shop, admin_token, pid) == (8, 0),
        "the two units to leave the shelf for good (Kafka)",
    )

    shop.data("POST", f"/api/v1/orders/{order_id}/ship", admin_token)
    shop.data("POST", f"/api/v1/orders/{order_id}/deliver", admin_token)
    history = [
        h["to_status"] for h in shop.data("GET", f"/api/v1/orders/{order_id}", token)["history"]
    ]
    assert history == ["pending", "awaiting_payment", "confirmed", "shipped", "delivered"]

    # every milestone reached the customer's inbox (Mailpit caught the real SMTP traffic)
    subjects = lambda: [m["Subject"] for m in mailbox(email)]  # noqa: E731
    eventually(lambda: len(subjects()) >= 4, "the welcome, confirmed, shipped and delivered emails")
    joined = " | ".join(subjects())
    for expected in ("Welcome", "confirmed", "shipped", "delivered"):
        assert expected in joined, f"no '{expected}' email among: {joined}"


def test_a_declined_payment_cancels_the_order_and_frees_the_stock(shop, admin_token, mailbox):
    pid = make_product(shop, admin_token, price_cents=5000, stock=5)
    email, token, address = ready_customer(shop)

    order_id = place_order(shop, token, address, pid, quantity=3)
    assert stock(shop, admin_token, pid) == (5, 3)

    pay(shop, token, order_id, outcome="failed")
    eventually(
        lambda: shop.data("GET", f"/api/v1/orders/{order_id}", token)["status"] == "cancelled",
        "the order to be cancelled after the declined payment",
    )
    eventually(lambda: stock(shop, admin_token, pid) == (5, 0), "the reserved stock to be released")

    eventually(
        lambda: any("cancelled" in m["Subject"] for m in mailbox(email)), "the cancellation email"
    )
    assert not any(
        "failed" in m["Subject"].lower() for m in mailbox(email)
    )  # no duplicate "payment failed" email


def test_nobody_can_buy_more_than_exists(shop, admin_token):
    pid = make_product(shop, admin_token, stock=1)
    _, token, address = ready_customer(shop)
    shop.data("POST", "/api/v1/cart/items", token, json={"product_id": pid, "quantity": 2})
    resp = shop.call("POST", "/api/v1/cart/checkout", token, json={"address_id": address})
    assert (
        resp.status_code == 409 and "not enough stock" in resp.text
    )  # refused before any order exists


def test_other_customers_cannot_see_an_order(shop, admin_token):
    pid = make_product(shop, admin_token)
    _, token, address = ready_customer(shop)
    order_id = place_order(shop, token, address, pid, quantity=1)
    _, intruder = shop.new_customer()
    assert shop.call("GET", f"/api/v1/orders/{order_id}", intruder).status_code == 404
    assert shop.call("GET", f"/api/v1/payments/order/{order_id}", intruder).status_code == 404
