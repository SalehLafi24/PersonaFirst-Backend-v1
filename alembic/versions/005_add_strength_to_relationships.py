"""Add strength to attribute_value_relationships

Revision ID: 005
Revises: 004
Create Date: 2026-03-27
"""
from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add strength column; backfill from confidence so existing approved rows
    # already have a meaningful value without manual intervention.
    op.add_column(
        "attribute_value_relationships",
        sa.Column("strength", sa.Float(), nullable=False, server_default="0"),
    )
    op.execute("UPDATE attribute_value_relationships SET strength = confidence")


def downgrade() -> None:
    op.drop_column("attribute_value_relationships", "strength")
