"""v2: purchases, repurchase fields, group_id nullable

Revision ID: 002
Revises: 001
Create Date: 2026-03-26
"""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Make products.group_id nullable (was NOT NULL in v1)
    op.alter_column("products", "group_id", nullable=True)

    # Add repurchase fields to products
    op.add_column("products", sa.Column("repurchase_behavior", sa.String(50), nullable=True))
    op.add_column("products", sa.Column("repurchase_window_days", sa.Integer(), nullable=True))

    # New table: customer_purchases (product_id stored as external string at this revision)
    op.create_table(
        "customer_purchases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("customer_id", sa.String(255), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("group_id", sa.String(255), nullable=True),
        sa.Column("order_date", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("revenue", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("customer_purchases")
    op.drop_column("products", "repurchase_window_days")
    op.drop_column("products", "repurchase_behavior")
    op.alter_column("products", "group_id", nullable=False)
