"""Phase 8.5b: synonym discipline audit.

Reads-only check that flags drift between approved values, the events
that backed them, and the synonym override table:

  1. APPROVED-VALUE-WITH-UNMAPPED-VARIANTS:
     For each APPROVED aggregate, list raw forms (from its events) that
     differ from the canonical AND don't have a synonym override row.
     These are candidates for adding to attribute_synonym_overrides --
     the next ingest of that raw form will create a duplicate pending
     aggregate otherwise.

  2. STALE-OVERRIDES:
     Override rows whose raw_value has not appeared in any
     ProposedAttributeValueEvent for the workspace+attribute. They may
     be stale (the variant is no longer used in the catalog) or
     pre-emptive (deliberately added before any product showed it).
     Flag, don't auto-remove.

  3. BROKEN-REFERENCES:
     Override rows whose canonical_value isn't in the active
     AttributeAllowedValue set. The override would route ingest events
     to a value that doesn't exist; the normalizer would still emit
     the event, but downstream backfill would skip it. Flag.

Output is a report. Nothing is auto-applied. The reviewer decides which
candidates to add or which overrides to retire.

Usage:
    python scripts/synonym_audit.py --workspace mumzworld_v3_sample
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from app.core.database import SessionLocal
from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.attribute_synonym_override import AttributeSynonymOverride
from app.models.proposed_attribute_value import (
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_MERGED,
    ProposedAttributeValueAggregate,
    ProposedAttributeValueEvent,
)
from app.models.workspace import Workspace


# ---------------------------------------------------------------------------
# Tier classification (Phase 8.5b follow-up)
#
# Tier 1: formatting-only variants. Differ by case, whitespace, hyphens,
#         underscores, or trailing punctuation. Safe for bulk apply --
#         no semantic ambiguity.
#
#         examples: "haircare" <-> "hair_care", "Lunch Box" <-> "lunch_box"
#
# Tier 2: semantic variants. Differ in actual tokens or word boundaries.
#         Each requires individual review because the mapping is a
#         judgement call, not a formatting normalisation.
#
#         examples: "bottle" -> "water_bottle"  (different concept)
#                   "ride_on_toy" -> "ride_on"  (parent/child)
# ---------------------------------------------------------------------------

_FORMATTING_STRIP_RE = re.compile(r"[\s_\-./,;:!?]+")


def _is_formatting_only_variant(raw: str, canonical: str) -> bool:
    """True when raw and canonical differ only by case, whitespace,
    hyphens, underscores, or common punctuation. Used by the audit to
    classify findings into Tier 1 (safe to bulk-apply) vs Tier 2."""
    if not raw or not canonical:
        return False
    a = _FORMATTING_STRIP_RE.sub("", raw.strip().lower())
    b = _FORMATTING_STRIP_RE.sub("", canonical.strip().lower())
    return bool(a) and a == b


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--attribute", default=None,
                        help="restrict audit to a single attribute")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == args.workspace).first()
        if ws is None:
            raise SystemExit(f"workspace not found: {args.workspace!r}")

        # ----- gather state -----
        agg_q = (
            db.query(ProposedAttributeValueAggregate)
            .filter(
                ProposedAttributeValueAggregate.workspace_id == ws.id,
                ProposedAttributeValueAggregate.status.in_(
                    [PROPOSAL_STATUS_APPROVED, PROPOSAL_STATUS_MERGED]
                ),
            )
        )
        if args.attribute:
            agg_q = agg_q.filter(
                ProposedAttributeValueAggregate.attribute_name == args.attribute
            )
        approved_aggs = agg_q.all()

        ovr_q = db.query(AttributeSynonymOverride).filter(
            AttributeSynonymOverride.workspace_id == ws.id
        )
        if args.attribute:
            ovr_q = ovr_q.filter(
                AttributeSynonymOverride.attribute_name == args.attribute
            )
        overrides = ovr_q.all()

        aav_q = db.query(AttributeAllowedValue).filter(
            AttributeAllowedValue.workspace_id == ws.id,
            AttributeAllowedValue.is_active == True,  # noqa: E712
        )
        if args.attribute:
            aav_q = aav_q.filter(
                AttributeAllowedValue.attribute_name == args.attribute
            )
        active_aav: dict[str, set[str]] = defaultdict(set)
        for r in aav_q.all():
            active_aav[r.attribute_name].add(r.value)

        # ----- 1. unmapped variants on approved aggregates -----
        # For each approved aggregate, pull its raw variants and check if
        # each is covered by either being identical to canonical or having
        # an override row.
        ovr_by_attr_raw: dict[tuple[str, str], AttributeSynonymOverride] = {
            (o.attribute_name, o.raw_value): o for o in overrides
        }

        unmapped: list[dict] = []
        for agg in approved_aggs:
            canonical = agg.promoted_to_allowed_value or agg.canonical_value
            if not canonical:
                continue
            ev_rows = (
                db.query(ProposedAttributeValueEvent.proposed_value_raw)
                .filter(
                    ProposedAttributeValueEvent.workspace_id == ws.id,
                    ProposedAttributeValueEvent.attribute_name == agg.attribute_name,
                    ProposedAttributeValueEvent.normalized_value == agg.cluster_key,
                )
                .distinct()
                .all()
            )
            raw_variants = sorted({r[0] for r in ev_rows if r[0]})
            for raw in raw_variants:
                if raw.strip().lower() == canonical.strip().lower():
                    continue
                if (agg.attribute_name, raw) in ovr_by_attr_raw:
                    continue
                unmapped.append({
                    "attribute": agg.attribute_name,
                    "canonical": canonical,
                    "raw_variant": raw,
                    "aggregate_id": agg.id,
                    "agg_status": agg.status,
                    "tier": "1" if _is_formatting_only_variant(raw, canonical) else "2",
                })

        # ----- 2. stale overrides -----
        # Overrides whose raw_value has no events in the workspace.
        stale_overrides: list[dict] = []
        for ovr in overrides:
            seen = (
                db.query(ProposedAttributeValueEvent.id)
                .filter(
                    ProposedAttributeValueEvent.workspace_id == ws.id,
                    ProposedAttributeValueEvent.attribute_name == ovr.attribute_name,
                    ProposedAttributeValueEvent.proposed_value_raw == ovr.raw_value,
                )
                .first()
            )
            if seen is None:
                stale_overrides.append({
                    "id": ovr.id,
                    "attribute": ovr.attribute_name,
                    "raw_value": ovr.raw_value,
                    "canonical_value": ovr.canonical_value,
                    "source": ovr.source,
                    "created_at": ovr.created_at.isoformat() if ovr.created_at else None,
                })

        # ----- 3. broken references -----
        broken_refs: list[dict] = []
        for ovr in overrides:
            valid_set = {v.lower() for v in active_aav.get(ovr.attribute_name, set())}
            if not valid_set:
                # No AAV entries at all; can't validate. Skip silently
                # rather than report every override as broken.
                continue
            if ovr.canonical_value.lower() not in valid_set:
                broken_refs.append({
                    "id": ovr.id,
                    "attribute": ovr.attribute_name,
                    "raw_value": ovr.raw_value,
                    "canonical_value": ovr.canonical_value,
                    "active_aav_count": len(valid_set),
                })
    finally:
        db.close()

    # ---------- print report ----------
    print("=" * 80)
    print(f"SYNONYM AUDIT  workspace={args.workspace} (id={ws.id})"
          + (f"  attribute={args.attribute}" if args.attribute else ""))
    print("=" * 80)
    print()
    print(f"Approved/merged aggregates inspected : {len(approved_aggs)}")
    print(f"Synonym overrides                    : {len(overrides)}")
    print()

    print("─" * 80)
    tier1 = [u for u in unmapped if u["tier"] == "1"]
    tier2 = [u for u in unmapped if u["tier"] == "2"]
    print(f"1. APPROVED VALUES WITH UNMAPPED RAW VARIANTS  "
          f"({len(unmapped)} total: {len(tier1)} tier-1, {len(tier2)} tier-2)")
    print("─" * 80)
    if not unmapped:
        print("   (none -- every raw variant on an approved aggregate is either")
        print("    equal to its canonical or already has a synonym override)")
    else:
        print()
        print(f"   TIER 1 -- formatting variants (safe to bulk-apply): {len(tier1)}")
        print()
        if tier1:
            print(f"   {'attribute':<14} {'raw_variant':<30} -> canonical")
            for u in tier1[:30]:
                print(f"   {u['attribute']:<14} {u['raw_variant'][:30]!r:<30} -> "
                      f"{u['canonical']}  (agg #{u['aggregate_id']})")
            if len(tier1) > 30:
                print(f"   ... +{len(tier1) - 30} more")
        else:
            print("   (none)")

        print()
        print(f"   TIER 2 -- semantic variants (review individually): {len(tier2)}")
        print()
        if tier2:
            print(f"   {'attribute':<14} {'raw_variant':<30} -> canonical")
            for u in tier2[:30]:
                print(f"   {u['attribute']:<14} {u['raw_variant'][:30]!r:<30} -> "
                      f"{u['canonical']}  (agg #{u['aggregate_id']})")
            if len(tier2) > 30:
                print(f"   ... +{len(tier2) - 30} more")
        else:
            print("   (none)")

    print()
    print("─" * 80)
    print(f"2. STALE SYNONYM OVERRIDES (no events match raw_value)  "
          f"({len(stale_overrides)})")
    print("─" * 80)
    if not stale_overrides:
        print("   (none)")
    else:
        for s in stale_overrides[:20]:
            print(f"   id={s['id']:<6} {s['attribute']:<14} "
                  f"{s['raw_value']!r:<24} -> {s['canonical_value']}  "
                  f"({s['source']}, {s['created_at']})")
        if len(stale_overrides) > 20:
            print(f"   ... +{len(stale_overrides) - 20} more")

    print()
    print("─" * 80)
    print(f"3. BROKEN REFERENCES (canonical not in active AAV)  "
          f"({len(broken_refs)})")
    print("─" * 80)
    if not broken_refs:
        print("   (none)")
    else:
        for b in broken_refs[:20]:
            print(f"   id={b['id']:<6} {b['attribute']:<14} "
                  f"{b['raw_value']!r:<24} -> {b['canonical_value']!r:<24} "
                  f"(active AAV count: {b['active_aav_count']})")

    print()
    print("Nothing was modified by this audit. Use the approval endpoint or")
    print("a follow-up tool to act on candidates.")


if __name__ == "__main__":
    main()
