"""Business rules for the catalogue, with Redis cache-aside on reads.

Write path:  change rows -> add outbox event -> commit -> invalidate cache.
Read path:   cache -> (miss) database -> store in cache.
Invalidating AFTER the commit means a reader can briefly see old data, never data that was
rolled back. The TTL bounds that window even if an invalidation is lost.
"""

import hashlib
import json
import re
import uuid
from typing import Any

from ecom_common import Conflict, NotFound, ValidationFailed
from ecom_common.cache import Cache
from ecom_common.outbox import add_event
from ecom_common.responses import paginated
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.catalogue import Category, Product
from app.repositories.catalogue_repository import CatalogueRepository, ProductFilters
from app.schemas.catalogue import (
    CategoryIn,
    CategoryOut,
    ProductCreate,
    ProductOut,
    ProductUpdate,
)

CATEGORIES_KEY = "categories:all"
LIST_VERSION = "product-lists"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _product_dict(product: Product) -> dict[str, Any]:
    return ProductOut.model_validate(product).model_dump(mode="json")


def _event_payload(product: Product) -> dict[str, Any]:
    """A full snapshot, so consumers never need to ask us for more."""
    return {
        "product_id": str(product.id),
        "sku": product.sku,
        "name": product.name,
        "price_cents": product.price_cents,
        "currency": product.currency,
        "category_id": str(product.category_id) if product.category_id else None,
        "is_active": product.is_active,
    }


class CatalogueService:
    def __init__(self, session: AsyncSession, cache: Cache, settings: Settings) -> None:
        self.session = session
        self.repo = CatalogueRepository(session)
        self.cache = cache
        self.settings = settings

    # ---- categories ---------------------------------------------------------------------
    async def list_categories(self) -> tuple[list[dict[str, Any]], bool]:
        async def load() -> list[dict[str, Any]]:
            rows = await self.repo.list_categories()
            return [CategoryOut.model_validate(c).model_dump(mode="json") for c in rows]

        return await self.cache.get_or_load(CATEGORIES_KEY, self.settings.category_cache_ttl, load)

    async def _check_category_free(self, data: CategoryIn, ignore: uuid.UUID | None = None) -> str:
        slug = slugify(data.name)
        if not slug:
            raise ValidationFailed("Category name must contain letters or numbers")
        existing = await self.repo.category_by_name_or_slug(data.name.strip(), slug)
        if existing and existing.id != ignore:
            raise Conflict("A category with this name already exists")
        return slug

    async def create_category(self, data: CategoryIn) -> Category:
        slug = await self._check_category_free(data)
        category = Category(name=data.name.strip(), slug=slug, description=data.description.strip())
        self.session.add(category)
        await self._commit("A category with this name already exists")
        await self.cache.delete(CATEGORIES_KEY)
        return category

    async def _get_category(self, category_id: uuid.UUID) -> Category:
        category = await self.repo.get_category(category_id)
        if category is None:
            raise NotFound("Category not found", message="Not found")
        return category

    async def update_category(self, category_id: uuid.UUID, data: CategoryIn) -> Category:
        category = await self._get_category(category_id)
        category.slug = await self._check_category_free(data, ignore=category.id)
        category.name = data.name.strip()
        category.description = data.description.strip()
        await self._commit("A category with this name already exists")
        await self.cache.delete(CATEGORIES_KEY)
        return category

    async def delete_category(self, category_id: uuid.UUID) -> None:
        category = await self._get_category(category_id)
        if await self.repo.count_products_in(category.id):
            raise Conflict("Move or remove this category's products first")
        await self.session.delete(category)
        await self.session.commit()
        await self.cache.delete(CATEGORIES_KEY)

    # ---- products: reads ----------------------------------------------------------------
    async def get_product(
        self, product_id: uuid.UUID, *, is_admin: bool
    ) -> tuple[dict[str, Any], bool]:
        async def load() -> dict[str, Any] | None:
            product = await self.repo.get_product(product_id)
            return _product_dict(product) if product else None

        # Hidden products are cached too; visibility is decided per caller, after the lookup
        product, hit = await self.cache.get_or_load(
            f"product:{product_id}", self.settings.product_cache_ttl, load
        )
        if product is None or (not product["is_active"] and not is_admin):
            raise NotFound("Product not found", message="Not found")
        return product, hit

    async def search_products(
        self, filters: ProductFilters, page: int, size: int
    ) -> tuple[dict[str, Any], bool]:
        async def load() -> dict[str, Any]:
            rows, total = await self.repo.search(filters, (page - 1) * size, size)
            items = [_product_dict(p) for p in rows]
            return paginated(items, total, page, size)

        if filters.include_inactive:  # admin view: always fresh, never cached
            return await load(), False

        params = json.dumps(
            [
                filters.q and filters.q.strip().lower(),
                str(filters.category_id),
                filters.min_price,
                filters.max_price,
                filters.sort,
                filters.descending,
                page,
                size,
            ]
        )
        digest = hashlib.sha1(params.encode()).hexdigest()[:16]  # noqa: S324 - not security
        version = await self.cache.version(LIST_VERSION)
        return await self.cache.get_or_load(
            f"list:v{version}:{digest}", self.settings.list_cache_ttl, load
        )

    # ---- products: writes ---------------------------------------------------------------
    async def _require_category(self, category_id: uuid.UUID | None) -> None:
        if category_id is not None and await self.repo.get_category(category_id) is None:
            raise ValidationFailed("Category does not exist", message="Invalid category")

    async def _commit(self, conflict_message: str) -> None:
        try:
            await self.session.commit()
        except IntegrityError as exc:  # a concurrent request got there first
            await self.session.rollback()
            raise Conflict(conflict_message) from exc

    async def _invalidate_product(self, product_id: uuid.UUID) -> None:
        await self.cache.delete(f"product:{product_id}")
        await self.cache.bump_version(LIST_VERSION)  # every cached list is now out of date

    async def create_product(self, data: ProductCreate) -> Product:
        sku = data.sku.strip().upper()
        if await self.repo.get_by_sku(sku):
            raise Conflict(f"SKU {sku} is already used")
        await self._require_category(data.category_id)

        product = Product(
            sku=sku,
            name=data.name.strip(),
            description=data.description.strip(),
            price_cents=data.price_cents,
            currency=(data.currency or self.settings.default_currency).upper(),
            category_id=data.category_id,
            is_active=data.is_active,
        )
        self.session.add(product)
        await self.session.flush()
        add_event(self.session, "product.created", _event_payload(product), key=str(product.id))
        await self._commit(f"SKU {sku} is already used")
        await self._invalidate_product(product.id)
        return product

    async def _get_product_row(self, product_id: uuid.UUID) -> Product:
        product = await self.repo.get_product(product_id)
        if product is None:
            raise NotFound("Product not found", message="Not found")
        return product

    async def update_product(self, product_id: uuid.UUID, data: ProductUpdate) -> Product:
        product = await self._get_product_row(product_id)
        before = _event_payload(product)

        for field, value in data.model_dump(exclude_unset=True).items():
            if value is None and field != "category_id":
                continue  # null only means something for category_id (= remove the category)
            if field == "category_id":
                await self._require_category(value)
            elif isinstance(value, str):
                value = value.strip().upper() if field == "currency" else value.strip()
            setattr(product, field, value)

        if _event_payload(product) == before:
            return product  # nothing changed: no event, no cache churn

        add_event(self.session, "product.updated", _event_payload(product), key=str(product.id))
        await self.session.commit()
        await self._invalidate_product(product.id)
        return product

    async def deactivate_product(self, product_id: uuid.UUID) -> None:
        """Soft delete: hide it from the shop but keep the row for order history."""
        await self.update_product(product_id, ProductUpdate(is_active=False))
