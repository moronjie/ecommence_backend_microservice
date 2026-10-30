from typing import Annotated

from ecom_common import Unauthorized
from fastapi import Depends, Header, Request

from app.clients import InventoryClient, ProductClient, UserClient
from app.services.cart_service import CartService
from app.stores import CartStore, ProductStore


def get_cart_service(request: Request) -> CartService:
    state = request.app.state
    settings = state.settings
    return CartService(
        CartStore(state.redis, settings.cart_ttl_days * 86400),
        ProductStore(state.redis),
        ProductClient(state.http, settings.product_url),
        InventoryClient(state.http, settings.inventory_url, settings.internal_auth_secret),
        UserClient(state.http, settings.user_url, settings.internal_auth_secret),
        state.publisher,
        settings,
    )


def current_user_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    """The gateway verifies the JWT and sets X-User-Id. This service is never exposed directly."""
    if not x_user_id:
        raise Unauthorized("Missing user identity")
    return x_user_id


ServiceDep = Annotated[CartService, Depends(get_cart_service)]
UserId = Annotated[str, Depends(current_user_id)]
