"""Add recommendation_role to products

Revision ID: 004
Revises: 003
Create Date: 2026-03-27
"""
from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "recommendation_role",
            sa.String(50),
            nullable=False,
            server_default="same_use_case",
        ),
    )


def downgrade() -> None:
    op.drop_column("products", "recommendation_role")
