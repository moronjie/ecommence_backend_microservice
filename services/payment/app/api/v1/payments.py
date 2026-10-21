import json
import uuid
from typing import Annotated, Literal

from ecom_common import Unauthorized, ValidationFailed, success_response
from fastapi import APIRouter, Query, Request

from app.core.dependencies import AdminOnly, IsAdmin, ServiceDep, UserId
from app.models.payment import PENDING
from app.schemas.payment import PaymentOut

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])
dev_router = APIRouter(prefix="/api/v1/payments/dev", tags=["development only"])

Status = Literal["created", "pending", "succeeded", "failed", "refunded"]


@router.post("/webhooks/paystack")
async def paystack_webhook(request: Request, service: ServiceDep):
    """Called by Paystack, not by a logged-in user, so the gateway lets it through without a
    token. Anyone on the internet could call this URL, so the signature is what protects it."""
    body = await request.body()
    if not service.provider.valid_signature(body, request.headers.get("x-paystack-signature")):
        raise Unauthorized("Invalid webhook signature")
    try:
        message = json.loads(body)
    except ValueError as exc:
        raise ValidationFailed("The webhook body is not valid JSON") from exc
    await service.handle_provider_event(message)
    await service.commit()
    return success_response(message="Received")  # a 200 tells Paystack not to resend it


@router.get("/order/{order_id}")
async def payment_for_order(
    order_id: uuid.UUID, service: ServiceDep, user_id: UserId, admin: IsAdmin
):
    """Where the customer finds the link to pay. It appears a moment after checkout."""
    payment = await service.for_order(order_id, user_id, admin)
    view = PaymentOut.model_validate(payment)
    # The link only makes sense while the customer still has to pay. The model copies the stored
    # value straight from the row, so it has to be cleared explicitly in every other state.
    view.authorization_url = payment.authorization_url if payment.status == PENDING else None
    return success_response(view)


@router.get("", dependencies=[AdminOnly])
async def list_payments(
    service: ServiceDep,
    status: Status | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    result = await service.list_payments(status, page, size)
    result["items"] = [PaymentOut.model_validate(p) for p in result["items"]]
    return success_response(result)


@dev_router.post("/{reference}/pay")
async def simulate_payment(
    reference: str,
    service: ServiceDep,
    _: UserId,
    outcome: Literal["success", "failed"] = "success",
):
    """DEVELOPMENT ONLY (exists only with PROVIDER=fake): plays the customer paying, then runs
    the same settlement the real webhook would."""
    service.provider.simulate(reference, outcome)  # type: ignore[attr-defined]
    found = await service.settle(reference)
    await service.commit()
    return success_response({"reference": reference, "known": found}, f"Simulated {outcome}")
