"""Taxonomy-evolution tables for attribute proposals.

Two-level design:

- ProposedAttributeValueEvent (raw): one row per `(product, attribute,
  proposed_value)` emission from an enrichment run. These are append-only
  evidence records. Nothing in the rest of the system reads them directly.

- ProposedAttributeValueAggregate (reviewable): one row per
  `(workspace, attribute, cluster_key)`, built by rolling up the raw events.
  This is what reviewers approve/reject/merge. Promotion to the allowed
  value set is never automatic — `status` flips only via an explicit review
  action.

Merge-reason vocabulary
    ``merge_reason`` records *why* a reviewer chose the action, so audits
    and future hierarchy work can reconstruct the intent. The taxonomy is
    flat today; these reasons carry the semantic signal that a hierarchy
    would formalise later.

    normalized_duplicate — formatting variant of an existing value
        (e.g. "HIIT" / "hiit"). Merge into the canonical form.
    synonym_to_existing — different word, same concept in the current
        taxonomy (e.g. "hiking" merged into "travel"). Merge.
    flattened_child — genuinely distinct child concept temporarily
        collapsed into a parent (e.g. "nursing" into "postpartum").
        Marked so a future hierarchy can split them back out.
    noise — low-signal or accidental value. Reject.

Merge decision rules (documented, not enforced in code)
    1. Exact formatting variants  -> merge  (normalized_duplicate)
    2. True synonyms              -> merge  (synonym_to_existing)
    3. Child concepts             -> merge temporarily (flattened_child),
       revisit when hierarchy support is added
    4. Distinct concepts that drive different recommendations -> approve
       as separate allowed values
    5. Low-signal one-offs        -> leave pending or reject (noise)

mom_stage flat-taxonomy decision record
    The initial review for the ``mom_stage`` attribute should follow these
    decisions:
        pregnancy  -> approve    (distinct stage, high evidence)
        postpartum -> approve    (distinct stage, high evidence)
        nursing    -> merge into postpartum as flattened_child
                      (nursing is a sub-stage of postpartum in a flat
                       taxonomy; when hierarchy arrives it becomes a
                       child of postpartum)
        newborn    -> hold pending (only 1 product, below thresholds)
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


# Aggregate statuses. A fresh aggregate starts as "pending"; reviewers can
# flip it to "approved", "rejected", or "merged" via the review service.
PROPOSAL_STATUS_PENDING = "pending"
PROPOSAL_STATUS_APPROVED = "approved"
PROPOSAL_STATUS_REJECTED = "rejected"
PROPOSAL_STATUS_MERGED = "merged"

# Merge reasons — optional tag recorded alongside a review action so audits
# and future hierarchy work can reconstruct the reviewer's intent.
MERGE_REASON_NORMALIZED_DUPLICATE = "normalized_duplicate"
MERGE_REASON_SYNONYM = "synonym_to_existing"
MERGE_REASON_FLATTENED_CHILD = "flattened_child"
MERGE_REASON_NOISE = "noise"


class ProposedAttributeValueEvent(Base):
    """Raw, append-only proposal event.

    One row is written per proposed_value emission from a single enrichment
    output. Events are the source of truth; aggregates are rebuilt from them.
    """

    __tablename__ = "proposed_attribute_value_events"
    __table_args__ = (
        Index(
            "ix_propval_events_ws_attr",
            "workspace_id",
            "attribute_name",
            "normalized_value",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id"), nullable=False
    )
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attribute_name: Mapped[str] = mapped_column(String(255), nullable=False)
    proposed_value_raw: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )


class ProposedAttributeValueAggregate(Base):
    """Rolled-up view of raw events, one row per (ws, attribute, cluster_key).

    `cluster_key` equals `normalized_value` today — keeping it as a distinct
    column leaves room for fuzzy clustering later (e.g. merging "hiking"
    and "hike" under one key) without changing the schema.

    `promoted_to_allowed_value` records the canonical string the reviewer
    chose during approval. It mirrors `canonical_value` for a plain
    approval, or the target allowed value during a merge.
    """

    __tablename__ = "proposed_attribute_value_aggregates"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "attribute_name",
            "cluster_key",
            name="uq_propval_agg",
        ),
        Index("ix_propval_agg_ws_status", "workspace_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id"), nullable=False
    )
    attribute_name: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_value: Mapped[str] = mapped_column(String(255), nullable=False)
    cluster_key: Mapped[str] = mapped_column(String(255), nullable=False)
    proposal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    distinct_product_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    avg_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sample_evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sample_product_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=PROPOSAL_STATUS_PENDING,
        server_default=PROPOSAL_STATUS_PENDING,
    )
    promoted_to_allowed_value: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    merge_reason: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    review_note: Mapped[str | None] = mapped_column(
        String(1000), nullable=True
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
    )
