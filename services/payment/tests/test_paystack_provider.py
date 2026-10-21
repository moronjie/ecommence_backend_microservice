"""The real Paystack client, run against a pretend Paystack so no account is needed."""

import hashlib
import hmac
import json

import httpx
import pytest
from app.providers.base import PermanentProviderError, TransientProviderError
from app.providers.paystack import PaystackProvider

SECRET = "sk_test_abc123"


class Paystack:
    """Answers like Paystack and remembers what it was asked."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.reply = lambda request: httpx.Response(200, json={"status": True, "data": {}})

    def __call__(self, request):
        self.requests.append(request)
        return self.reply(request)


def make(api: Paystack) -> PaystackProvider:
    return PaystackProvider(
        SECRET, httpx.AsyncClient(transport=httpx.MockTransport(api)), "https://api.paystack.co/"
    )


INIT = dict(
    reference="ord-1",
    amount_cents=6500,
    currency="GHS",
    email="ada@example.com",
    metadata={"order_id": "o1"},
    callback_url="",
    channels=["card", "mobile_money"],
)


async def test_initialize_sends_what_paystack_expects():
    api = Paystack()
    api.reply = lambda r: httpx.Response(
        200,
        json={
            "status": True,
            "data": {
                "authorization_url": "https://checkout.paystack.com/x",
                "access_code": "x",
                "reference": "ord-1",
            },
        },
    )
    result = await make(api).initialize(**INIT)

    (request,) = api.requests
    assert (request.method, str(request.url)) == (
        "POST",
        "https://api.paystack.co/transaction/initialize",
    )
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    body = json.loads(request.content)
    assert body == {
        "email": "ada@example.com",
        "amount": 6500,  # pesewas
        "currency": "GHS",
        "reference": "ord-1",
        "metadata": {"order_id": "o1"},
        "channels": ["card", "mobile_money"],
    }  # no callback_url when none is configured
    assert result.authorization_url == "https://checkout.paystack.com/x"


async def test_initialize_includes_the_callback_when_configured():
    api = Paystack()
    api.reply = lambda r: httpx.Response(
        200, json={"status": True, "data": {"authorization_url": "u", "access_code": "c"}}
    )
    await make(api).initialize(**{**INIT, "callback_url": "https://shop.example/thanks"})
    assert json.loads(api.requests[0].content)["callback_url"] == "https://shop.example/thanks"


@pytest.mark.parametrize("status", [500, 502, 503, 429])
async def test_outages_and_rate_limits_are_transient(status):
    api = Paystack()
    api.reply = lambda r: httpx.Response(status, json={"status": False, "message": "busy"})
    with pytest.raises(TransientProviderError):
        await make(api).initialize(**INIT)


async def test_timeouts_and_connection_errors_are_transient():
    api = Paystack()

    def boom(request):
        raise httpx.ConnectTimeout("slow")

    api.reply = boom
    with pytest.raises(TransientProviderError):
        await make(api).verify("ord-1")


async def test_garbage_answers_are_transient():
    api = Paystack()
    api.reply = lambda r: httpx.Response(200, content=b"<html>oops</html>")
    with pytest.raises(TransientProviderError):
        await make(api).verify("ord-1")


@pytest.mark.parametrize("status", [400, 401, 403, 422])
async def test_refusals_are_permanent_and_carry_paystacks_message(status):
    api = Paystack()
    api.reply = lambda r: httpx.Response(
        status, json={"status": False, "message": "Currency not supported by merchant"}
    )
    with pytest.raises(PermanentProviderError, match="Currency not supported"):
        await make(api).initialize(**INIT)


async def test_a_200_that_says_status_false_is_a_refusal():
    api = Paystack()
    api.reply = lambda r: httpx.Response(
        200, json={"status": False, "message": "Duplicate Transaction Reference"}
    )
    with pytest.raises(PermanentProviderError, match="Duplicate"):
        await make(api).initialize(**INIT)


@pytest.mark.parametrize(
    ("paystack_says", "we_say"),
    [
        ("success", "success"),
        ("failed", "failed"),
        ("reversed", "failed"),
        ("abandoned", "abandoned"),
        ("ongoing", "pending"),
        ("pending", "pending"),
        ("queued", "pending"),
        ("something-new", "pending"),
    ],
)
async def test_verify_maps_statuses(paystack_says, we_say):
    api = Paystack()
    api.reply = lambda r: httpx.Response(
        200,
        json={
            "status": True,
            "data": {
                "status": paystack_says,
                "amount": 6500,
                "currency": "ghs",
                "channel": "mobile_money",
                "id": 42,
                "gateway_response": "Approved",
            },
        },
    )
    v = await make(api).verify("ord-1")
    assert v.status == we_say
    assert (v.amount_cents, v.currency, v.channel, v.transaction_id, v.reason) == (
        6500,
        "GHS",
        "mobile_money",
        "42",
        "Approved",
    )
    assert str(api.requests[0].url) == "https://api.paystack.co/transaction/verify/ord-1"


async def test_verify_of_a_transaction_the_customer_never_opened_is_just_pending():
    api = Paystack()
    api.reply = lambda r: httpx.Response(
        404, json={"status": False, "message": "Transaction reference not found"}
    )
    assert (await make(api).verify("ord-1")).status == "pending"


@pytest.mark.parametrize(
    ("says", "we_say"),
    [
        ("pending", "pending"),
        ("processing", "pending"),
        ("processed", "processed"),
        ("failed", "failed"),
    ],
)
async def test_refund_request_and_status_mapping(says, we_say):
    api = Paystack()
    api.reply = lambda r: httpx.Response(
        200, json={"status": True, "data": {"id": 7, "status": says}}
    )
    result = await make(api).refund(reference="ord-1", amount_cents=6500, reason="cancelled")
    body = json.loads(api.requests[0].content)
    assert (api.requests[0].url.path, body["transaction"], body["amount"]) == (
        "/refund",
        "ord-1",
        6500,
    )
    assert (result.refund_id, result.status) == ("7", we_say)


def test_webhook_signature_is_hmac_sha512_of_the_raw_body():
    provider = make(Paystack())
    body = b'{"event":"charge.success","data":{"reference":"ord-1"}}'
    good = hmac.new(SECRET.encode(), body, hashlib.sha512).hexdigest()
    assert provider.valid_signature(body, good)
    assert not provider.valid_signature(body + b" ", good)  # body changed
    assert not provider.valid_signature(body, good.upper())  # not the exact signature
    assert not provider.valid_signature(body, None) and not provider.valid_signature(body, "")
    other_key = hmac.new(b"another-key", body, hashlib.sha512).hexdigest()
    assert not provider.valid_signature(body, other_key)
