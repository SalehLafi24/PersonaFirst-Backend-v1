"""Add merge_reason and review_note to proposal aggregates.

Revision ID: 009
Revises: 008
Create Date: 2026-04-17
"""
import sqlalchemy as sa
from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "proposed_attribute_value_aggregates",
        sa.Column("merge_reason", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "proposed_attribute_value_aggregates",
        sa.Column("review_note", sa.String(length=1000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("proposed_attribute_value_aggregates", "review_note")
    op.drop_column("proposed_attribute_value_aggregates", "merge_reason")
