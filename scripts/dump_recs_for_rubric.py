"""Dump per-customer rec details for human-rubric review.

Reads the customer fixture, runs the rec engine for each, prints a
readable markdown block per customer:
  - persona summary
  - top-N recs with attributes + intent contributions
  - the customer's recent purchase history (so the reviewer can sanity
    check saturation behaviour without separate queries)

Output goes to stdout. Pipe to a file for scrolling:
    python scripts/dump_recs_for_rubric.py --workspace mumzworld_v3_sample > /tmp/rubric_dump.md
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from app.core.database import SessionLocal
from app.models.customer import CustomerInteraction
from app.models.product import Product
from app.models.workspace import Workspace
from app.services.customer_recommendation_service import recommend_for_customer


_DEFAULT_FIXTURE = ROOT / "seed_data" / "eval" / "recommendation_customers.json"


def _short_attrs(attrs: dict | None) -> str:
    if not attrs:
        return ""
    parts = []
    for k in ("product_type", "age_group", "gender", "use_case"):
        v = attrs.get(k)
        if v is not None:
            parts.append(f"{k}={v}")
    return " ".join(parts)


def _short_intent(contribs) -> str:
    if not contribs:
        return ""
    parts = []
    for c in contribs:
        sig = c.get("signal", "?")
        kind = c.get("kind", "?")
        val = c.get("value", 0)
        parts.append(f"{sig}/{kind}{f'(+{val:.2f})' if kind=='boost' else ''}")
    return ", ".join(parts)


def _print_persona(persona) -> None:
    print(f"  cold_start: {persona.cold_start}")
    print(f"  confidence_overall: {persona.confidence_overall:.3f}")
    print(f"  interactions (contributing/total): "
          f"{persona.interaction_count_contributing}/{persona.interaction_count_total}")
    for attr, aff in (persona.attribute_affinities or {}).items():
        top3 = sorted(aff.distribution.items(), key=lambda x: -x[1])[:3]
        top3s = ", ".join(f"{v}={p:.2f}" for v, p in top3)
        print(f"  {attr}: focus={aff.focus_score:.2f}  top: {top3s}")


def _print_history(db, workspace_id: int, customer_id: str) -> None:
    rows = (
        db.query(CustomerInteraction.product_id, CustomerInteraction.occurred_at)
        .filter(
            CustomerInteraction.workspace_id == workspace_id,
            CustomerInteraction.customer_id == customer_id,
        )
        .order_by(CustomerInteraction.occurred_at.desc())
        .all()
    )
    if not rows:
        print("  (no interactions)")
        return
    pids = [r[0] for r in rows]
    name_by_pid = {
        p.product_id: p.name for p in db.query(Product).filter(
            Product.workspace_id == workspace_id,
            Product.product_id.in_(pids),
        ).all()
    }
    now = datetime.now(timezone.utc)
    for pid, occurred in rows:
        if occurred and occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        elapsed = (now - occurred).days if occurred else None
        nm = (name_by_pid.get(pid) or "")[:65]
        print(f"  -{elapsed:>4}d  {pid:<24} {nm}")


def _print_customer(db, workspace_id: int, customer: dict, top_n: int) -> None:
    cid = customer["customer_id"]
    print()
    print("=" * 78)
    print(f"# {cid}")
    print(f"profile: {customer.get('profile_label')}")
    if customer.get("_note"):
        print(f"note: {customer['_note']}")
    print("=" * 78)

    print()
    print("## persona")
    response = recommend_for_customer(
        db, workspace_id=workspace_id, customer_id=cid, top_n=top_n,
    )
    _print_persona(response.persona)

    print()
    print(f"## history ({len(list(db.query(CustomerInteraction).filter(CustomerInteraction.workspace_id==workspace_id, CustomerInteraction.customer_id==cid).all()))} interactions, recent first)")
    _print_history(db, workspace_id, cid)

    print()
    print(f"## top-{top_n} recs")
    print(f"{'#':<3} {'product_id':<24} {'pf':<6} {'score':<6} attrs / intent")
    for i, rec in enumerate(response.recommendations):
        pf = f"{rec.persona_fit:.3f}"
        sc = f"{rec.score:.3f}"
        attrs = _short_attrs(rec.attributes)
        intent = _short_intent(rec.intent_contributions)
        nm = (rec.name or "")[:60]
        print(f"{i:<3} {rec.product_id:<24} {pf:<6} {sc:<6} {attrs}")
        print(f"      name: {nm}")
        if intent:
            print(f"      intent: {intent}")

    print()
    print(f"## intent_summary: {response.intent_summary}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dump per-customer rec details for human-rubric review.",
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--fixture", type=Path, default=_DEFAULT_FIXTURE)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--customer", default=None,
                        help="optional single-customer filter")
    args = parser.parse_args()

    if not args.fixture.exists():
        raise SystemExit(f"fixture not found: {args.fixture}")
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    customers = fixture.get("customers") or []
    if args.customer:
        customers = [c for c in customers if c["customer_id"] == args.customer]
        if not customers:
            raise SystemExit(f"customer {args.customer!r} not in fixture")

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == args.workspace).first()
        if ws is None:
            raise SystemExit(f"workspace not found: {args.workspace!r}")
        print(f"# rubric dump")
        print(f"workspace: {args.workspace} (id={ws.id})")
        print(f"fixture:   {args.fixture.name}")
        print(f"top_n:     {args.top_n}")
        print(f"generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
        for c in customers:
            _print_customer(db, ws.id, c, args.top_n)
    finally:
        db.close()


if __name__ == "__main__":
    main()
