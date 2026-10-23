import uuid
from typing import Annotated, Literal

from ecom_common import Forbidden, success_response
from fastapi import APIRouter, Query

from app.core.dependencies import AdminOnly, IsAdmin, ServiceDep, UserId
from app.schemas.notification import NotificationAdminOut, NotificationDetail, NotificationOut

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])

Status = Literal["queued", "sent", "failed"]
Kind = Literal[
    "welcome", "order_confirmed", "order_cancelled", "order_shipped", "order_delivered", "refund"
]


@router.get("")
async def list_notifications(
    service: ServiceDep,
    user_id: UserId,
    admin: IsAdmin,
    status: Status | None = None,
    kind: Kind | None = None,
    owner: Annotated[uuid.UUID | None, Query(description="Admins only: one customer's")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """Customers see the emails sent to them. Administrators see everyone's, with delivery info."""
    if owner is not None and owner != user_id and not admin:
        raise Forbidden("Only administrators can list another customer's notifications")
    scope = owner if admin else user_id  # None for an admin means "everyone"
    result = await service.list_notifications(
        user_id=scope, status=status, kind=kind, page=page, size=size
    )
    view = NotificationAdminOut if admin else NotificationOut
    result["items"] = [view.model_validate(n) for n in result["items"]]
    return success_response(result)


@router.get("/{notification_id}")
async def get_notification(
    notification_id: uuid.UUID, service: ServiceDep, user_id: UserId, admin: IsAdmin
):
    note = await service.get(notification_id, user_id, admin)
    detail = NotificationDetail.model_validate(note).model_dump(mode="json")
    if not admin:  # delivery internals are for administrators
        for private in ("user_id", "attempts", "last_error", "next_attempt_at"):
            detail.pop(private)
    return success_response(detail)


@router.post("/{notification_id}/retry", dependencies=[AdminOnly])
async def retry_notification(notification_id: uuid.UUID, service: ServiceDep):
    """Send a failed email again, after the cause (bad address, mail server) has been fixed."""
    note = await service.retry(notification_id)
    await service.commit()
    return success_response(NotificationAdminOut.model_validate(note), "Queued to send again")
