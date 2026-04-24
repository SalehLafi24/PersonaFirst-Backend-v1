"""Add attribute_allowed_values table for DB-backed taxonomy.

Revision ID: 008
Revises: 007
Create Date: 2026-04-16
"""
import sqlalchemy as sa
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attribute_allowed_values",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("attribute_name", sa.String(length=255), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "attribute_name",
            "value",
            name="uq_attr_allowed_value",
        ),
    )
    op.create_index(
        "ix_attr_allowed_ws_attr_active",
        "attribute_allowed_values",
        ["workspace_id", "attribute_name", "is_active"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_attr_allowed_ws_attr_active",
        table_name="attribute_allowed_values",
    )
    op.drop_table("attribute_allowed_values")
