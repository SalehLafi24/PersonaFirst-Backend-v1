"""Per-workspace synonym overrides (Phase 8.5b).

Layered on top of the global synonym map in
seed_data/attribute_normalization.json. Override rows are written by the
approval / merge endpoints when a reviewer decides "from now on, this
raw form should collapse to this canonical."

This is the discipline mechanism that prevents variant drift: when
'hydrating' is approved as a synonym of 'hydration', the next time
'hydrating' shows up in any product name, the normalizer collapses it
on entry rather than creating a duplicate pending aggregate.

Source values:
    "approval"      — added when a reviewer approves an aggregate
    "manual"        — added by an operator outside the approval flow
    "import"        — bulk-imported (future use)
"""
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


SYNONYM_SOURCE_APPROVAL = "approval"
SYNONYM_SOURCE_MANUAL = "manual"
SYNONYM_SOURCE_IMPORT = "import"


class AttributeSynonymOverride(Base):
    __tablename__ = "attribute_synonym_overrides"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "attribute_name", "raw_value",
            name="uq_synonym_override",
        ),
        Index("ix_synonym_override_ws_attr",
              "workspace_id", "attribute_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id"), nullable=False,
    )
    attribute_name: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_value: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_value: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default=SYNONYM_SOURCE_MANUAL,
    )
    source_aggregate_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
