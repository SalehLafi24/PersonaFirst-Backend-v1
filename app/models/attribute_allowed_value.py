"""Workspace-scoped taxonomy table for attribute allowed values.

Replaces the static ``allowed_values`` list on ``AttributeDefinition`` with
a mutable, DB-backed taxonomy that enrichment services query at prompt-build
time. When no rows exist for a (workspace, attribute) pair, callers fall
back to the definition's static ``allowed_values`` — so existing behaviour
is preserved until a workspace actively populates or promotes values.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AttributeAllowedValue(Base):
    __tablename__ = "attribute_allowed_values"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "attribute_name",
            "value",
            name="uq_attr_allowed_value",
        ),
        Index(
            "ix_attr_allowed_ws_attr_active",
            "workspace_id",
            "attribute_name",
            "is_active",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id"), nullable=False,
    )
    attribute_name: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )
