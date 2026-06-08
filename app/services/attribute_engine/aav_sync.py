"""Manifest -> AttributeAllowedValue sync planning.

Explicit operator-run repair tool. Closes the gap where a manifest declares
an `allowed_value` (e.g. `adult`) but the workspace's AAV catalog has no
row for it -- the engine emits events for that value, then backfill drops
them silently because cluster_to_canonical can't resolve them.

Behavior:
  - Closed taxonomies     : every manifest allowed_value should exist as
                            an AAV row (active OR inactive). Missing ones
                            are seeded as inactive on `apply`.
  - Open taxonomies       : skipped entirely. AAV catalog is governed by
                            the clustering/approval pipeline, not the
                            manifest. The manifest has no allowed_values
                            for them in the first place.
  - Hypothetical semi-open: would be treated like closed for the baseline
                            manifest values. The codebase has no semi-open
                            kind today; if added, this module's closed
                            branch is the right baseline.

Hard constraints (enforced in code + tests):
  - Inserted rows are always is_active=False.
  - Existing rows are never updated, never deactivated, never deleted.
  - No mutation happens unless apply_sync_plan() is called explicitly.
  - This module is NOT called from manifest load or pipeline run.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.attribute_allowed_value import AttributeAllowedValue
from app.services.attribute_engine.manifest import AttributeManifest


@dataclass
class AttributeSyncPlan:
    """Per-attribute sync plan. Fields are populated for closed
    taxonomies; for open taxonomies only `attribute_name`, `kind` and
    `skipped_reason` are set."""
    attribute_name: str
    kind: str  # 'closed' | 'open'
    skipped_reason: str | None = None
    manifest_values: list[str] = field(default_factory=list)
    missing_in_aav: list[str] = field(default_factory=list)
    already_active: list[str] = field(default_factory=list)
    already_inactive: list[str] = field(default_factory=list)


@dataclass
class SyncPlan:
    workspace_id: int
    attributes: list[AttributeSyncPlan] = field(default_factory=list)

    @property
    def total_missing(self) -> int:
        return sum(len(a.missing_in_aav) for a in self.attributes)

    @property
    def closed_attributes(self) -> list[AttributeSyncPlan]:
        return [a for a in self.attributes if a.kind == "closed"]

    @property
    def open_attributes(self) -> list[AttributeSyncPlan]:
        return [a for a in self.attributes if a.kind == "open"]


@dataclass
class ApplyResult:
    """Outcome of applying a SyncPlan. `inserted_by_attribute` only
    contains the attributes that actually had inserts."""
    workspace_id: int
    inserted: int
    inserted_by_attribute: dict[str, list[str]] = field(default_factory=dict)


def compute_sync_plan(
    db: Session,
    *,
    manifest: AttributeManifest,
    workspace_id: int,
) -> SyncPlan:
    """Compute the diff between manifest.allowed_values and AAV rows for
    each attribute in the manifest. Pure read."""
    plan = SyncPlan(workspace_id=workspace_id)

    aav_rows = db.query(
        AttributeAllowedValue.attribute_name,
        AttributeAllowedValue.value,
        AttributeAllowedValue.is_active,
    ).filter(
        AttributeAllowedValue.workspace_id == workspace_id,
    ).all()
    # Per-attribute case-insensitive lookup -> active flag.
    aav_by_attr: dict[str, dict[str, bool]] = {}
    for attr_name, value, is_active in aav_rows:
        if not value:
            continue
        aav_by_attr.setdefault(attr_name, {})[value.lower()] = bool(is_active)

    for attr_name in manifest.names():
        entry = manifest.get(attr_name)
        kind = entry.taxonomy.kind
        attr_plan = AttributeSyncPlan(attribute_name=attr_name, kind=kind)

        if kind == "open":
            attr_plan.skipped_reason = (
                "open taxonomy: AAV is governed by clustering/approval, "
                "not by manifest"
            )
            plan.attributes.append(attr_plan)
            continue

        # Closed taxonomy (or future semi-open: same baseline behavior).
        manifest_values = list(entry.taxonomy.allowed_values)
        attr_plan.manifest_values = manifest_values
        existing = aav_by_attr.get(attr_name, {})

        for v in manifest_values:
            present = existing.get(v.lower())
            if present is None:
                attr_plan.missing_in_aav.append(v)
            elif present:
                attr_plan.already_active.append(v)
            else:
                attr_plan.already_inactive.append(v)

        plan.attributes.append(attr_plan)

    return plan


def apply_sync_plan(
    db: Session,
    *,
    plan: SyncPlan,
) -> ApplyResult:
    """Insert the missing rows from `plan` as is_active=False. Idempotent
    in the sense that re-computing the plan after this returns 0 missing.

    Never updates, deactivates, or deletes existing rows. Caller is
    responsible for committing."""
    result = ApplyResult(workspace_id=plan.workspace_id, inserted=0)
    for attr_plan in plan.attributes:
        if not attr_plan.missing_in_aav:
            continue
        for value in attr_plan.missing_in_aav:
            db.add(AttributeAllowedValue(
                workspace_id=plan.workspace_id,
                attribute_name=attr_plan.attribute_name,
                value=value,
                is_active=False,
            ))
            result.inserted += 1
            result.inserted_by_attribute.setdefault(
                attr_plan.attribute_name, []
            ).append(value)
    if result.inserted:
        db.flush()
    return result
