"""Add attribute_synonym_overrides table (Phase 8.5b).

Per-workspace synonym layer that the normalizer reads in addition to the
global JSON synonyms. Written by the approval / merge endpoints when a
reviewer hardens variant -> canonical mappings.

Revision ID: 012
Revises: 011
Create Date: 2026-05-08
"""
import sqlalchemy as sa
from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attribute_synonym_overrides",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("attribute_name", sa.String(255), nullable=False),
        sa.Column("raw_value", sa.String(255), nullable=False),
        sa.Column("canonical_value", sa.String(255), nullable=False),
        sa.Column(
            "source", sa.String(50), nullable=False,
            server_default="manual",
        ),
        sa.Column("source_aggregate_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.UniqueConstraint(
            "workspace_id", "attribute_name", "raw_value",
            name="uq_synonym_override",
        ),
    )
    op.create_index(
        "ix_synonym_override_ws_attr",
        "attribute_synonym_overrides",
        ["workspace_id", "attribute_name"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_synonym_override_ws_attr",
        table_name="attribute_synonym_overrides",
    )
    op.drop_table("attribute_synonym_overrides")
