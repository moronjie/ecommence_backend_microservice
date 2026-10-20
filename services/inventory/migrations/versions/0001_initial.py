import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_now = sa.func.now()


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now, nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "stock_items",
        sa.Column("product_id", sa.Uuid(), primary_key=True),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("on_hand", sa.Integer(), nullable=False),
        sa.Column("reserved", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("on_hand >= 0", name="ck_on_hand_non_negative"),
        sa.CheckConstraint("reserved >= 0", name="ck_reserved_non_negative"),
        sa.CheckConstraint("reserved <= on_hand", name="ck_reserved_within_on_hand"),
    )

    op.create_table(
        "reservations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(200), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_reservations_order_id", "reservations", ["order_id"], unique=True)
    op.create_index("ix_reservations_status", "reservations", ["status"])

    op.create_table(
        "reservation_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("reservation_id", sa.Uuid(), sa.ForeignKey("reservations.id"), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_quantity_positive"),
    )
    op.create_index("ix_reservation_items_reservation_id", "reservation_items", ["reservation_id"])

    op.create_table(
        "stock_movements",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("delta_on_hand", sa.Integer(), nullable=False),
        sa.Column("delta_reserved", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("reference", sa.String(200), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_stock_movements_product_id", "stock_movements", ["product_id"])

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("topic", sa.String(200), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("event", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now, nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "processed_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=_now, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("processed_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_stock_movements_product_id", table_name="stock_movements")
    op.drop_table("stock_movements")
    op.drop_index("ix_reservation_items_reservation_id", table_name="reservation_items")
    op.drop_table("reservation_items")
    op.drop_index("ix_reservations_status", table_name="reservations")
    op.drop_index("ix_reservations_order_id", table_name="reservations")
    op.drop_table("reservations")
    op.drop_table("stock_items")
