"""initial tables (v1 baseline)

Revision ID: 001
Revises:
Create Date: 2026-03-26
"""
from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_table(
        "workspace_users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_table(
        "customer_attribute_affinities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("customer_id", sa.String(255), nullable=False),
        sa.Column("attribute_id", sa.String(255), nullable=False),
        sa.Column("attribute_value", sa.String(255), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("workspace_id", "customer_id", "attribute_id", "attribute_value",
                            name="uq_customer_affinity"),
    )
    op.create_table(
        "attribute_value_relationships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("attribute_id_1", sa.String(255), nullable=False),
        sa.Column("attribute_value_1", sa.String(255), nullable=False),
        sa.Column("attribute_id_2", sa.String(255), nullable=False),
        sa.Column("attribute_value_2", sa.String(255), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("lift", sa.Float(), nullable=False),
        sa.Column("pair_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="suggested"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("sku", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("group_id", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("workspace_id", "product_id"),
    )
    op.create_table(
        "product_attributes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("attribute_id", sa.String(255), nullable=False),
        sa.Column("attribute_value", sa.String(255), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("product_attributes")
    op.drop_table("products")
    op.drop_table("audit_log")
    op.drop_table("attribute_value_relationships")
    op.drop_table("customer_attribute_affinities")
    op.drop_table("workspace_users")
    op.drop_table("users")
    op.drop_table("workspaces")
