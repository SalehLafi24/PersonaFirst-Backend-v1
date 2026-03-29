"""Add product_behavior_relationships table

Revision ID: 006
Revises: 005
Create Date: 2026-03-28
"""
import sqlalchemy as sa
from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_behavior_relationships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("source_product_db_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("target_product_db_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("strength", sa.Float(), nullable=False),
        sa.Column("customer_overlap_count", sa.Integer(), nullable=False),
        sa.Column("source_customer_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "workspace_id", "source_product_db_id", "target_product_db_id",
            name="uq_behavior_rel",
        ),
    )
    op.create_index(
        "ix_behavior_rel_ws_source",
        "product_behavior_relationships",
        ["workspace_id", "source_product_db_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_behavior_rel_ws_source", table_name="product_behavior_relationships")
    op.drop_table("product_behavior_relationships")
