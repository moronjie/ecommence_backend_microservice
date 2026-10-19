import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalogue import Category, Product

SORT_COLUMNS = {
    "created_at": Product.created_at,
    "price": Product.price_cents,
    "name": Product.name,
}


@dataclass(frozen=True)
class ProductFilters:
    q: str | None = None
    category_id: uuid.UUID | None = None
    min_price: int | None = None
    max_price: int | None = None
    sort: str = "created_at"
    descending: bool = True
    include_inactive: bool = False


def _like_pattern(text: str) -> str:
    escaped = text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class CatalogueRepository:
    """Database access only. No business rules and no caching here."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- categories ---------------------------------------------------------------------
    async def list_categories(self) -> list[Category]:
        return list(await self.session.scalars(select(Category).order_by(Category.name)))

    async def get_category(self, category_id: uuid.UUID) -> Category | None:
        return await self.session.get(Category, category_id)

    async def category_by_name_or_slug(self, name: str, slug: str) -> Category | None:
        return await self.session.scalar(
            select(Category).where(or_(Category.name == name, Category.slug == slug))
        )

    async def count_products_in(self, category_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(Product).where(Product.category_id == category_id)
        return await self.session.scalar(stmt) or 0

    # ---- products -----------------------------------------------------------------------
    async def get_product(self, product_id: uuid.UUID) -> Product | None:
        return await self.session.get(Product, product_id)

    async def get_by_sku(self, sku: str) -> Product | None:
        return await self.session.scalar(select(Product).where(Product.sku == sku))

    async def search(self, f: ProductFilters, offset: int, limit: int) -> tuple[list[Product], int]:
        conditions: list[ColumnElement[bool]] = []
        if not f.include_inactive:
            conditions.append(Product.is_active.is_(True))
        if f.q:
            pattern = _like_pattern(f.q)
            conditions.append(
                or_(
                    func.lower(Product.name).like(pattern, escape="\\"),
                    func.lower(Product.description).like(pattern, escape="\\"),
                )
            )
        if f.category_id:
            conditions.append(Product.category_id == f.category_id)
        if f.min_price is not None:
            conditions.append(Product.price_cents >= f.min_price)
        if f.max_price is not None:
            conditions.append(Product.price_cents <= f.max_price)

        total = await self.session.scalar(
            select(func.count()).select_from(Product).where(*conditions)
        )
        column = SORT_COLUMNS[f.sort]
        ordering = column.desc() if f.descending else column.asc()
        rows = await self.session.scalars(
            select(Product)
            .where(*conditions)
            .order_by(ordering, Product.id)  # id breaks ties so pages never overlap
            .offset(offset)
            .limit(limit)
        )
        return list(rows), total or 0
