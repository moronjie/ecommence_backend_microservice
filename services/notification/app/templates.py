from dataclasses import dataclass
from html import escape

from app.core.config import Settings
from app.schemas.notification import OrderEvent, RefundEvent, UserRegistered


@dataclass(frozen=True)
class Rendered:
    subject: str
    text: str
    html: str


def money(cents: int, currency: str) -> str:
    """24000 pesewas -> 'GHS 240.00' (amounts are always whole minor units)."""
    return f"{currency.upper()} {cents // 100:,}.{cents % 100:02d}"


def order_ref(order_id: object) -> str:
    """A short reference a customer can quote: the first 8 characters of the order id."""
    return str(order_id).replace("-", "")[:8].upper()


def friendly_reason(reason: str) -> str:
    """Internal reasons are for engineers; customers get plain language."""
    r = reason.lower()
    if r.startswith("payment failed") or "declined" in r:
        return "your payment did not go through"
    if "payment not received" in r or "not completed in time" in r:
        return "we did not receive your payment in time"
    if "stock" in r:
        return "some of the items ran out of stock"
    if "cancelled by customer" in r:
        return "you cancelled it"
    if "cancelled by administrator" in r:
        return "the shop had to cancel it"
    return "we were unable to complete it"


def _address_lines(address: dict | None) -> list[str]:
    if not address:
        return []
    keys = ("recipient", "line1", "line2", "city", "state", "postal_code", "country")
    return [str(address[k]) for k in keys if address.get(k)]


def _page(shop: str, heading: str, html_body: str, support: str) -> str:
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:auto;'
        'color:#222;line-height:1.5">'
        f'<h2 style="margin-bottom:4px">{escape(heading)}</h2>'
        f"{html_body}"
        '<hr style="border:none;border-top:1px solid #ddd;margin:24px 0">'
        f'<p style="color:#666;font-size:13px">{escape(shop)}. Questions? '
        f'Write to <a href="mailto:{escape(support)}">{escape(support)}</a>.</p></div>'
    )


def _footer(s: Settings) -> str:
    return f"\n--\n{s.shop_name}\nQuestions? Write to {s.support_email}\n"


def welcome(u: UserRegistered, s: Settings) -> Rendered:
    name = u.full_name.strip() or "there"
    text = (
        f"Hi {name},\n\nWelcome to {s.shop_name}! Your account is ready.\n"
        f"You can now browse, add items to your cart and order.\n" + _footer(s)
    )
    html = _page(
        s.shop_name,
        f"Welcome to {s.shop_name}!",
        f"<p>Hi {escape(name)},</p><p>Your account is ready. You can now browse, add items "
        "to your cart and order.</p>",
        s.support_email,
    )
    return Rendered(f"Welcome to {s.shop_name}", text, html)


def order_confirmed(o: OrderEvent, s: Settings) -> Rendered:
    ref = order_ref(o.order_id)
    lines = [
        f"- {i.quantity} x {i.name}  {money(i.unit_price_cents * i.quantity, o.currency)}"
        for i in o.items
    ]
    total = money(o.total_cents, o.currency)
    text = (
        f"Thank you! We have received your payment for order #{ref}.\n\n"
        + ("\n".join(lines) + "\n" if lines else "")
        + f"\nTotal paid: {total}\n\nWe will email you again when it ships.\n"
        + _footer(s)
    )
    rows = "".join(
        f"<tr><td>{i.quantity} &times; {escape(i.name)}</td>"
        f'<td style="text-align:right">'
        f"{escape(money(i.unit_price_cents * i.quantity, o.currency))}</td></tr>"
        for i in o.items
    )
    html = _page(
        s.shop_name,
        f"Order #{ref} confirmed",
        "<p>Thank you! We have received your payment.</p>"
        f'<table style="width:100%;border-collapse:collapse">{rows}'
        '<tr><td style="padding-top:8px"><strong>Total paid</strong></td>'
        f'<td style="text-align:right;padding-top:8px"><strong>{escape(total)}</strong></td></tr>'
        "</table><p>We will email you again when it ships.</p>",
        s.support_email,
    )
    return Rendered(f"Order #{ref} confirmed", text, html)


def order_cancelled(o: OrderEvent, s: Settings) -> Rendered:
    ref, why = order_ref(o.order_id), friendly_reason(o.reason)
    text = (
        f"Your order #{ref} was cancelled because {why}.\n\n"
        "If you were charged, the money will be returned to you automatically.\n"
        "You are welcome to place the order again at any time.\n" + _footer(s)
    )
    html = _page(
        s.shop_name,
        f"Order #{ref} cancelled",
        f"<p>Your order was cancelled because {escape(why)}.</p>"
        "<p>If you were charged, the money will be returned to you automatically. "
        "You are welcome to place the order again at any time.</p>",
        s.support_email,
    )
    return Rendered(f"Order #{ref} was cancelled", text, html)


def order_shipped(o: OrderEvent, s: Settings) -> Rendered:
    ref, address = order_ref(o.order_id), _address_lines(o.shipping_address)
    where = "\n".join(address)
    text = (
        f"Good news: order #{ref} is on its way.\n\n"
        + (f"Delivering to:\n{where}\n" if address else "")
        + _footer(s)
    )
    html = _page(
        s.shop_name,
        f"Order #{ref} is on its way",
        "<p>Good news: your order has shipped.</p>"
        + (
            "<p><strong>Delivering to</strong><br>"
            + "<br>".join(escape(line) for line in address)
            + "</p>"
            if address
            else ""
        ),
        s.support_email,
    )
    return Rendered(f"Order #{ref} has shipped", text, html)


def order_delivered(o: OrderEvent, s: Settings) -> Rendered:
    ref = order_ref(o.order_id)
    text = f"Order #{ref} has been delivered. We hope you enjoy it!\n" + _footer(s)
    html = _page(
        s.shop_name,
        f"Order #{ref} delivered",
        "<p>Your order has been delivered. We hope you enjoy it!</p>",
        s.support_email,
    )
    return Rendered(f"Order #{ref} was delivered", text, html)


def refund(r: RefundEvent, s: Settings) -> Rendered:
    ref, amount = order_ref(r.order_id), money(r.amount_cents, r.currency)
    text = (
        f"We have refunded {amount} for order #{ref}.\n\n"
        "It can take a few working days to show in your account or mobile money wallet.\n"
        + _footer(s)
    )
    html = _page(
        s.shop_name,
        f"Refund for order #{ref}",
        f"<p>We have refunded <strong>{escape(amount)}</strong>.</p>"
        "<p>It can take a few working days to show in your account or mobile money wallet.</p>",
        s.support_email,
    )
    return Rendered(f"Your refund for order #{ref}", text, html)
