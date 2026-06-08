"""Workspace-scoped attribute taxonomy backed by the DB.

Read path
    ``get_allowed_values`` is the single entry-point for all enrichment
    and scoring code that needs an attribute's value list. It checks the
    ``attribute_allowed_values`` table first; if no *active* rows exist
    for the (workspace, attribute) pair, it returns ``default_values``
    unchanged. This means existing static definitions keep working until
    a workspace actively populates its taxonomy.

Write path
    ``upsert_allowed_value`` and ``set_allowed_values`` are used by the
    proposal-approval pipeline and by seed/bootstrap scripts. Writes are
    idempotent — re-inserting an existing active value is a no-op.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.attribute_allowed_value import AttributeAllowedValue


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------

def get_allowed_values(
    db: Session,
    workspace_id: int,
    attribute_name: str,
    *,
    default_values: list[str] | None = None,
) -> list[str]:
    """Return active allowed values for *attribute_name* in *workspace_id*.

    Falls back to *default_values* (typically ``AttributeDefinition.allowed_values``)
    when the DB has no active rows for this pair — so the static seed
    behaviour is preserved until a workspace explicitly populates its
    taxonomy.

    Ordering is deterministic: rows come back sorted by creation time
    (earliest first), which matches insertion order and keeps any
    human-chosen ordering stable.
    """
    rows = (
        db.query(AttributeAllowedValue.value)
        .filter(
            AttributeAllowedValue.workspace_id == workspace_id,
            AttributeAllowedValue.attribute_name == attribute_name,
            AttributeAllowedValue.is_active.is_(True),
        )
        .order_by(AttributeAllowedValue.created_at.asc())
        .all()
    )
    if rows:
        return [r[0] for r in rows]
    return list(default_values) if default_values else []


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------

def upsert_allowed_value(
    db: Session,
    workspace_id: int,
    attribute_name: str,
    value: str,
) -> AttributeAllowedValue:
    """Ensure *value* exists and is active for the given workspace + attribute.

    Idempotent: if the row already exists and is active, it is returned
    unchanged. If it exists but was deactivated, it is reactivated.
    """
    existing = (
        db.query(AttributeAllowedValue)
        .filter(
            AttributeAllowedValue.workspace_id == workspace_id,
            AttributeAllowedValue.attribute_name == attribute_name,
            AttributeAllowedValue.value == value,
        )
        .first()
    )
    if existing is not None:
        if not existing.is_active:
            existing.is_active = True
            db.flush()
        return existing

    row = AttributeAllowedValue(
        workspace_id=workspace_id,
        attribute_name=attribute_name,
        value=value,
        is_active=True,
    )
    db.add(row)
    db.flush()
    return row


def set_allowed_values(
    db: Session,
    workspace_id: int,
    attribute_name: str,
    values: list[str],
) -> list[str]:
    """Bulk-set the active value list for a workspace + attribute.

    Values not in *values* are deactivated (not deleted). Values already
    present are left untouched; new values are inserted. Order is
    preserved — new values are appended in list order.

    Returns the final active list.
    """
    target_set = set(values)

    existing = (
        db.query(AttributeAllowedValue)
        .filter(
            AttributeAllowedValue.workspace_id == workspace_id,
            AttributeAllowedValue.attribute_name == attribute_name,
        )
        .all()
    )
    existing_by_value = {row.value: row for row in existing}

    # Deactivate values not in the target set.
    for row in existing:
        if row.value not in target_set:
            row.is_active = False

    # Upsert values in the target set.
    for v in values:
        row = existing_by_value.get(v)
        if row is not None:
            if not row.is_active:
                row.is_active = True
        else:
            db.add(
                AttributeAllowedValue(
                    workspace_id=workspace_id,
                    attribute_name=attribute_name,
                    value=v,
                    is_active=True,
                )
            )

    db.flush()
    return get_allowed_values(db, workspace_id, attribute_name)


def deactivate_allowed_value(
    db: Session,
    workspace_id: int,
    attribute_name: str,
    value: str,
) -> bool:
    """Mark a value as inactive. Returns True if a row was modified."""
    row = (
        db.query(AttributeAllowedValue)
        .filter(
            AttributeAllowedValue.workspace_id == workspace_id,
            AttributeAllowedValue.attribute_name == attribute_name,
            AttributeAllowedValue.value == value,
        )
        .first()
    )
    if row is None or not row.is_active:
        return False
    row.is_active = False
    db.flush()
    return True
