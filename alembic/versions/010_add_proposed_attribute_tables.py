"""Add proposed_attribute_events and proposed_attribute_aggregates tables.

Revision ID: 010
Revises: 009
Create Date: 2026-04-17
"""
import sqlalchemy as sa
from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proposed_attribute_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False,
        ),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("proposed_attribute_name", sa.String(255), nullable=False),
        sa.Column("normalized_attribute_name", sa.String(255), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("suggested_values", sa.JSON(), nullable=False),
        sa.Column("suggested_class_name", sa.String(100), nullable=False),
        sa.Column("suggested_targeting_mode", sa.String(100), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_propattr_events_ws_name",
        "proposed_attribute_events",
        ["workspace_id", "normalized_attribute_name"],
    )

    op.create_table(
        "proposed_attribute_aggregates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False,
        ),
        sa.Column("cluster_key", sa.String(255), nullable=False),
        sa.Column("canonical_attribute_name", sa.String(255), nullable=False),
        sa.Column("proposal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("distinct_product_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("max_confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("sample_evidence", sa.JSON(), nullable=False),
        sa.Column("sample_product_ids", sa.JSON(), nullable=False),
        sa.Column("merged_suggested_values", sa.JSON(), nullable=False),
        sa.Column("suggested_class_name", sa.String(100), nullable=False),
        sa.Column("suggested_targeting_mode", sa.String(100), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("promoted_attribute_name", sa.String(255), nullable=True),
        sa.Column("merge_reason", sa.String(50), nullable=True),
        sa.Column("review_note", sa.String(1000), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("workspace_id", "cluster_key", name="uq_propattr_agg"),
    )
    op.create_index(
        "ix_propattr_agg_ws_status",
        "proposed_attribute_aggregates",
        ["workspace_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_propattr_agg_ws_status", table_name="proposed_attribute_aggregates")
    op.drop_table("proposed_attribute_aggregates")
    op.drop_index("ix_propattr_events_ws_name", table_name="proposed_attribute_events")
    op.drop_table("proposed_attribute_events")
