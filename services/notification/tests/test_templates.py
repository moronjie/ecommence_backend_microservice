import uuid

import pytest
from app import templates
from app.core.config import Settings
from app.schemas.notification import OrderEvent, RefundEvent, UserRegistered

S = Settings(shop_name="Accra Crafts", support_email="help@accracrafts.test")
ORDER_ID = uuid.UUID("a1b2c3d4-0000-4000-8000-000000000001")
USER_ID = uuid.uuid4()


def order(**kw) -> OrderEvent:
    base = {"order_id": ORDER_ID, "user_id": USER_ID, "currency": "GHS", "total_cents": 24000}
    return OrderEvent(**{**base, **kw})


@pytest.mark.parametrize(
    ("cents", "currency", "shown"),
    [
        (24000, "GHS", "GHS 240.00"),
        (5, "ghs", "GHS 0.05"),
        (123456789, "GHS", "GHS 1,234,567.89"),
        (0, "GHS", "GHS 0.00"),
    ],
)
def test_money_is_shown_in_major_units(cents, currency, shown):
    assert templates.money(cents, currency) == shown


def test_order_reference_is_short_and_stable():
    assert templates.order_ref(ORDER_ID) == "A1B2C3D4"


@pytest.mark.parametrize(
    ("internal", "customer_sees"),
    [
        ("payment failed: Declined", "payment did not go through"),
        ("the payment was not completed in time", "did not receive your payment in time"),
        ("payment not received in time", "did not receive your payment in time"),
        ("insufficient stock", "ran out of stock"),
        ("cancelled by customer", "you cancelled it"),
        ("cancelled by administrator", "shop had to cancel"),
        ("something engineers wrote", "unable to complete"),
    ],
)
def test_internal_reasons_become_plain_language(internal, customer_sees):
    assert customer_sees in templates.friendly_reason(internal)


def test_welcome():
    r = templates.welcome(
        UserRegistered(user_id=USER_ID, email="a@b.co", full_name="Ama Mensah"), S
    )
    assert r.subject == "Welcome to Accra Crafts"
    assert "Hi Ama Mensah" in r.text and "Hi Ama Mensah" in r.html


def test_welcome_without_a_name_still_reads_well():
    r = templates.welcome(UserRegistered(user_id=USER_ID, email="a@b.co"), S)
    assert "Hi there," in r.text


def test_confirmation_is_a_receipt():
    items = [
        {"name": "Kente Scarf", "quantity": 2, "unit_price_cents": 12000},
        {"name": "Mug", "quantity": 1, "unit_price_cents": 3500},
    ]
    r = templates.order_confirmed(order(items=items, total_cents=27500), S)
    assert r.subject == "Order #A1B2C3D4 confirmed"
    for fact in ("2 x Kente Scarf  GHS 240.00", "1 x Mug  GHS 35.00", "Total paid: GHS 275.00"):
        assert fact in r.text
    assert "GHS 275.00" in r.html and "Kente Scarf" in r.html


def test_cancellation_explains_in_plain_language_and_reassures_about_money():
    r = templates.order_cancelled(order(reason="payment failed: Declined"), S)
    assert r.subject == "Order #A1B2C3D4 was cancelled"
    assert "your payment did not go through" in r.text
    assert "returned to you automatically" in r.text
    assert "Declined" not in r.text  # the raw reason is not shown to customers


def test_shipping_email_shows_where_it_is_going():
    address = {
        "recipient": "Ama",
        "line1": "12 Oxford St",
        "city": "Accra",
        "country": "GH",
        "state": "",
    }
    r = templates.order_shipped(order(shipping_address=address), S)
    assert r.subject == "Order #A1B2C3D4 has shipped"
    assert "Ama\n12 Oxford St\nAccra\nGH" in r.text and "12 Oxford St" in r.html


def test_shipping_email_without_an_address_is_still_valid():
    r = templates.order_shipped(order(), S)
    assert "Delivering to" not in r.text and "on its way" in r.text


def test_delivery_and_refund():
    assert "delivered" in templates.order_delivered(order(), S).subject
    refund = templates.refund(
        RefundEvent(
            order_id=ORDER_ID,
            payment_id=uuid.uuid4(),
            user_id=USER_ID,
            amount_cents=24000,
            currency="GHS",
        ),
        S,
    )
    assert refund.subject == "Your refund for order #A1B2C3D4"
    assert "GHS 240.00" in refund.text and "mobile money" in refund.text


def test_everything_from_customers_and_products_is_escaped_in_html():
    nasty = '<script>alert("x")</script>'
    items = [{"name": nasty, "quantity": 1, "unit_price_cents": 100}]
    confirmed = templates.order_confirmed(order(items=items), S)
    shipped = templates.order_shipped(
        order(shipping_address={"recipient": nasty, "city": "<b>"}), S
    )
    welcome = templates.welcome(UserRegistered(user_id=USER_ID, email="a@b.co", full_name=nasty), S)
    for rendered in (confirmed, shipped, welcome):
        assert "<script>" not in rendered.html
        assert "&lt;script&gt;" in rendered.html
    assert "<b>" not in shipped.html


def test_subjects_never_contain_customer_text():
    evil = "Ama\r\nBcc: attacker@evil.test"
    welcome = templates.welcome(UserRegistered(user_id=USER_ID, email="a@b.co", full_name=evil), S)
    assert "\n" not in welcome.subject and "Ama" not in welcome.subject
