import uuid
from typing import Annotated, Literal

from ecom_common import Forbidden, ValidationFailed, success_response
from fastapi import APIRouter, Query

from app.core.dependencies import AdminOnly, IsAdmin, ServiceDep
from app.repositories.catalogue_repository import ProductFilters
from app.schemas.catalogue import (
    CategoryIn,
    CategoryOut,
    ProductCreate,
    ProductOut,
    ProductUpdate,
)

categories = APIRouter(prefix="/api/v1/categories", tags=["categories"])
products = APIRouter(prefix="/api/v1/products", tags=["products"])


def cache_header(hit: bool) -> dict[str, str]:
    """Makes caching visible: look at the X-Cache response header."""
    return {"X-Cache": "HIT" if hit else "MISS"}


# ---- categories ---------------------------------------------------------------------------
@categories.get("")
async def list_categories(service: ServiceDep):
    items, hit = await service.list_categories()
    return success_response({"items": items}, headers=cache_header(hit))


@categories.post("", dependencies=[AdminOnly])
async def create_category(body: CategoryIn, service: ServiceDep):
    category = await service.create_category(body)
    return success_response(CategoryOut.model_validate(category), "Category created", 201)


@categories.put("/{category_id}", dependencies=[AdminOnly])
async def update_category(category_id: uuid.UUID, body: CategoryIn, service: ServiceDep):
    category = await service.update_category(category_id, body)
    return success_response(CategoryOut.model_validate(category), "Category updated")


@categories.delete("/{category_id}", dependencies=[AdminOnly])
async def delete_category(category_id: uuid.UUID, service: ServiceDep):
    await service.delete_category(category_id)
    return success_response(message="Category deleted")


# ---- products -----------------------------------------------------------------------------
@products.get("")
async def search_products(
    service: ServiceDep,
    admin: IsAdmin,
    q: Annotated[
        str | None, Query(max_length=100, description="Matches name or description")
    ] = None,
    category_id: uuid.UUID | None = None,
    min_price: Annotated[int | None, Query(ge=0, description="In cents")] = None,
    max_price: Annotated[int | None, Query(ge=0, description="In cents")] = None,
    sort: Literal["created_at", "price", "name"] = "created_at",
    order: Literal["asc", "desc"] = "desc",
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    include_inactive: bool = False,
):
    if include_inactive and not admin:
        raise Forbidden("Only administrators can list hidden products")
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ValidationFailed("min_price cannot be greater than max_price")
    filters = ProductFilters(
        q=q.strip() if q else None,
        category_id=category_id,
        min_price=min_price,
        max_price=max_price,
        sort=sort,
        descending=order == "desc",
        include_inactive=include_inactive,
    )
    result, hit = await service.search_products(filters, page, size)
    return success_response(result, headers=cache_header(hit))


@products.get("/{product_id}")
async def get_product(product_id: uuid.UUID, service: ServiceDep, admin: IsAdmin):
    product, hit = await service.get_product(product_id, is_admin=admin)
    return success_response(product, headers=cache_header(hit))


@products.post("", dependencies=[AdminOnly])
async def create_product(body: ProductCreate, service: ServiceDep):
    product = await service.create_product(body)
    return success_response(ProductOut.model_validate(product), "Product created", 201)


@products.patch("/{product_id}", dependencies=[AdminOnly])
async def update_product(product_id: uuid.UUID, body: ProductUpdate, service: ServiceDep):
    product = await service.update_product(product_id, body)
    return success_response(ProductOut.model_validate(product), "Product updated")


@products.delete("/{product_id}", dependencies=[AdminOnly])
async def delete_product(product_id: uuid.UUID, service: ServiceDep):
    await service.deactivate_product(product_id)
    return success_response(message="Product hidden from the catalogue")
