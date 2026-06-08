"""Attribute-discovery tables — proposing entirely new taxonomy dimensions.

Parallel to the proposed-value pipeline but one level up: instead of
proposing new *values* for existing attributes, this pipeline proposes
new *attributes* that the taxonomy doesn't cover yet.

Same three-stage lifecycle: propose -> aggregate -> review -> promote.
Nothing is auto-promoted.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

ATTR_PROPOSAL_STATUS_PENDING = "pending"
ATTR_PROPOSAL_STATUS_APPROVED = "approved"
ATTR_PROPOSAL_STATUS_REJECTED = "rejected"
ATTR_PROPOSAL_STATUS_MERGED = "merged"


class ProposedAttributeEvent(Base):
    """Raw, append-only attribute-discovery event.

    One row per proposed attribute per product per discovery run.
    """
    __tablename__ = "proposed_attribute_events"
    __table_args__ = (
        Index(
            "ix_propattr_events_ws_name",
            "workspace_id",
            "normalized_attribute_name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id"), nullable=False,
    )
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    proposed_attribute_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_attribute_name: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    suggested_values: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    suggested_class_name: Mapped[str] = mapped_column(String(100), nullable=False)
    suggested_targeting_mode: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )


class ProposedAttributeAggregate(Base):
    """Rolled-up view of attribute proposals, one row per
    (workspace, cluster_key).

    ``cluster_key`` equals ``normalized_attribute_name`` today.
    ``merged_suggested_values`` unions the suggested_values from all
    contributing events (deduplicated, sorted).
    """
    __tablename__ = "proposed_attribute_aggregates"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "cluster_key",
            name="uq_propattr_agg",
        ),
        Index("ix_propattr_agg_ws_status", "workspace_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id"), nullable=False,
    )
    cluster_key: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_attribute_name: Mapped[str] = mapped_column(String(255), nullable=False)
    proposal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    distinct_product_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    avg_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sample_evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sample_product_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    merged_suggested_values: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list,
    )
    suggested_class_name: Mapped[str] = mapped_column(String(100), nullable=False)
    suggested_targeting_mode: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=ATTR_PROPOSAL_STATUS_PENDING,
        server_default=ATTR_PROPOSAL_STATUS_PENDING,
    )
    promoted_attribute_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True,
    )
    merge_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
