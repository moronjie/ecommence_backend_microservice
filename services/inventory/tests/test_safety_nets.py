import uuid

import pytest
from app.models.inventory import StockItem
from app.services.inventory_service import InventoryService, Line
from sqlalchemy import update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError


@pytest.mark.parametrize(
    "values",
    [
        {"reserved": 11},  # promising more than exists
        {"on_hand": -1, "reserved": 0},
        {"reserved": -1},
    ],
)
async def test_database_itself_refuses_bad_stock_levels(env, values):
    pid = await env.product(10)
    async with env.session_factory() as s:
        with pytest.raises(IntegrityError):
            await s.execute(
                update(StockItem).where(StockItem.product_id == uuid.UUID(pid)).values(**values)
            )
            await s.commit()


async def test_reserve_locks_rows_in_a_fixed_order_on_postgres(env):
    """SQLite ignores FOR UPDATE, so check the SQL that Postgres would receive."""
    a, b = await env.product(5, "A"), await env.product(5, "B")
    captured = []
    async with env.session_factory() as session:

        def watch(method_name):
            real = getattr(session, method_name)

            async def spy(statement, *args, **kwargs):
                captured.append(statement)
                return await real(statement, *args, **kwargs)

            setattr(session, method_name, spy)

        watch("scalars")  # the stock-row lock
        watch("scalar")  # the reservation-row lock
        service = InventoryService(session, env.settings)
        await service.reserve(uuid.uuid4(), [Line(uuid.UUID(b), 1), Line(uuid.UUID(a), 1)])

    sql = [str(s.compile(dialect=postgresql.dialect())) for s in captured]
    lock = next(s for s in sql if "stock_items" in s and "FOR UPDATE" in s)
    assert "ORDER BY stock_items.product_id" in lock  # same order for everyone: no deadlocks
    assert any("FROM reservations" in s and "FOR UPDATE" in s for s in sql)
