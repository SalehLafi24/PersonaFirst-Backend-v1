"""Add proposed_attribute_value_events and _aggregates tables.

Revision ID: 007
Revises: 006
Create Date: 2026-04-15
"""
import sqlalchemy as sa
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proposed_attribute_value_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False
        ),
        sa.Column("product_id", sa.String(length=255), nullable=False),
        sa.Column("attribute_name", sa.String(length=255), nullable=False),
        sa.Column("proposed_value_raw", sa.String(length=255), nullable=False),
        sa.Column("normalized_value", sa.String(length=255), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_propval_events_ws_attr",
        "proposed_attribute_value_events",
        ["workspace_id", "attribute_name", "normalized_value"],
    )

    op.create_table(
        "proposed_attribute_value_aggregates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False
        ),
        sa.Column("attribute_name", sa.String(length=255), nullable=False),
        sa.Column("canonical_value", sa.String(length=255), nullable=False),
        sa.Column("cluster_key", sa.String(length=255), nullable=False),
        sa.Column("proposal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "distinct_product_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("avg_confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("max_confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("sample_evidence", sa.JSON(), nullable=False),
        sa.Column("sample_product_ids", sa.JSON(), nullable=False),
        sa.Column(
            "status", sa.String(length=50), nullable=False, server_default="pending"
        ),
        sa.Column(
            "promoted_to_allowed_value", sa.String(length=255), nullable=True
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
            "cluster_key",
            name="uq_propval_agg",
        ),
    )
    op.create_index(
        "ix_propval_agg_ws_status",
        "proposed_attribute_value_aggregates",
        ["workspace_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_propval_agg_ws_status",
        table_name="proposed_attribute_value_aggregates",
    )
    op.drop_table("proposed_attribute_value_aggregates")
    op.drop_index(
        "ix_propval_events_ws_attr",
        table_name="proposed_attribute_value_events",
    )
    op.drop_table("proposed_attribute_value_events")
