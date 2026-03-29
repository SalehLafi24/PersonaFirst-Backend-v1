"""Add product_db_id FK to customer_purchases; add indexes

Adds the real FK column product_db_id (Integer → products.id) to
customer_purchases, backfills it from the denormalized product_id string,
then enforces NOT NULL and the FK constraint.

Also adds performance indexes for common query patterns.

Revision ID: 003
Revises: 002
Create Date: 2026-03-27
"""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add column nullable so existing rows don't violate NOT NULL yet
    op.add_column(
        "customer_purchases",
        sa.Column("product_db_id", sa.Integer(), nullable=True),
    )

    # 2. Backfill: resolve external product_id string → products.id
    #    Matches on same workspace_id + product_id string
    op.execute("""
        UPDATE customer_purchases cp
        SET product_db_id = p.id
        FROM products p
        WHERE cp.product_id   = p.product_id
          AND cp.workspace_id = p.workspace_id
    """)

    # 3. Enforce NOT NULL now that all rows are backfilled
    op.alter_column("customer_purchases", "product_db_id", nullable=False)

    # 4. Add FK constraint
    op.create_foreign_key(
        "fk_purchases_product_db_id",
        "customer_purchases",
        "products",
        ["product_db_id"],
        ["id"],
    )

    # 5. Indexes for customer_purchases query patterns
    op.create_index("ix_purchases_ws_cust",       "customer_purchases", ["workspace_id", "customer_id"])
    op.create_index("ix_purchases_ws_cust_prod",  "customer_purchases", ["workspace_id", "customer_id", "product_db_id"])
    op.create_index("ix_purchases_ws_cust_group", "customer_purchases", ["workspace_id", "customer_id", "group_id"])

    # 6. Index for customer_attribute_affinities lookup pattern
    op.create_index("ix_affinities_ws_cust", "customer_attribute_affinities", ["workspace_id", "customer_id"])


def downgrade() -> None:
    op.drop_index("ix_affinities_ws_cust",       table_name="customer_attribute_affinities")
    op.drop_index("ix_purchases_ws_cust_group",  table_name="customer_purchases")
    op.drop_index("ix_purchases_ws_cust_prod",   table_name="customer_purchases")
    op.drop_index("ix_purchases_ws_cust",        table_name="customer_purchases")
    op.drop_constraint("fk_purchases_product_db_id", "customer_purchases", type_="foreignkey")
    op.drop_column("customer_purchases", "product_db_id")
