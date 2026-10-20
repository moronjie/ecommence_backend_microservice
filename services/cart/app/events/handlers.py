import logging
from typing import Any

from ecom_common.consumer import Handler

from app.domain import ProductInfo
from app.schemas.cart import ProductEvent
from app.stores import ProductStore

logger = logging.getLogger(__name__)

TOPICS = ["product.created", "product.updated"]


def build_handlers(products: ProductStore) -> dict[str, Handler]:
    async def product_changed(event: dict[str, Any]) -> None:
        p = ProductEvent.model_validate(event["payload"])  # bad data -> retries -> DLQ
        # A full snapshot, so saving it twice does no harm; stale ones are ignored by the store
        await products.save(
            ProductInfo(
                product_id=str(p.product_id),
                sku=p.sku,
                name=p.name,
                price_cents=p.price_cents,
                currency=p.currency.upper(),
                is_active=p.is_active,
                event_at=event["occurred_at"],
            )
        )

    return {"product.created": product_changed, "product.updated": product_changed}
