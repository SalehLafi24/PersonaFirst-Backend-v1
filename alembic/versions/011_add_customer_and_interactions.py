"""Add customers and customer_interactions tables.

Revision ID: 011
Revises: 010
Create Date: 2026-05-05
"""
import sqlalchemy as sa
from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("customer_id", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("workspace_id", "customer_id",
                            name="uq_customer_ws_cid"),
    )
    op.create_index(
        "ix_customer_ws_cid", "customers",
        ["workspace_id", "customer_id"],
    )

    op.create_table(
        "customer_interactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("customer_id", sa.String(255), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column(
            "interaction_type", sa.String(50), nullable=False,
            server_default="purchase",
        ),
        sa.Column(
            "occurred_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_cinter_ws_cid_occurred", "customer_interactions",
        ["workspace_id", "customer_id", "occurred_at"],
    )
    op.create_index(
        "ix_cinter_ws_pid", "customer_interactions",
        ["workspace_id", "product_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_cinter_ws_pid", table_name="customer_interactions")
    op.drop_index("ix_cinter_ws_cid_occurred", table_name="customer_interactions")
    op.drop_table("customer_interactions")
    op.drop_index("ix_customer_ws_cid", table_name="customers")
    op.drop_table("customers")
