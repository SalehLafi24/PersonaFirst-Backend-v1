"""Synthetic-customer evaluation against the 49-product real catalog.

Pipeline (all stages are existing engine code — this script is glue only):

  1. Load `products_enriched_real.json` and seed Product + ProductAttribute
     rows for confidence>=0.8 enriched values.
  2. Build 5 customer carts via deterministic predicates over enriched
     values (no manual product picks — selection rule is `activity_type
     contains X`, sorted by product_id, take first N).
  3. Insert CustomerPurchase rows.
  4. Run `affinity_service.generate_affinities_from_purchases` to derive
     each customer's affinities programmatically (count / max-count
     normalization — existing logic, untouched).
  5. Run `recommendation_service.get_recommendations` to score each
     customer against the 49-product catalog.
  6. Print derived affinities, top-5 recommendations, and score breakdown.

The script uses a dedicated workspace slug "real-catalog-eval" and wipes
that workspace's data on each run to stay idempotent.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product, ProductAttribute
from app.models.workspace import Workspace
from app.schemas.attribute_enrichment import AttributeBehavior, AttributeDefinition
from app.services.affinity_service import generate_affinities_from_purchases
from app.services.recommendation_service import get_recommendations
from app.services.signal_strength_service import compute_customer_signal_strength

ROOT = Path(__file__).resolve().parent.parent
ENRICHED_PATH = ROOT / "products_enriched_real.json"
ATTR_DEFS_PATH = ROOT / "seed_data" / "attribute_definitions.json"

WORKSPACE_SLUG = "real-catalog-eval"
WORKSPACE_NAME = "Real Catalog Evaluation"
CONF_THRESHOLD = 0.8
TOP_N = 5
ORDER_DATE = date(2026, 4, 26)

ENRICHED_ATTRS = ["activity_type", "workout_intensity", "environment", "layering_role"]


# ---------------------------------------------------------------------------
# 1. Catalog loading + DB seeding
# ---------------------------------------------------------------------------

def _load_enriched_catalog() -> list[dict]:
    with ENRICHED_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _kept_values(attr_obj: dict) -> list[str]:
    """Confidence>=0.8 values from either `values` or `proposed_values`."""
    out: list[str] = []
    for v in attr_obj.get("values", []) or []:
        if v.get("confidence", 0.0) >= CONF_THRESHOLD and v.get("evidence"):
            out.append(v["value"])
    for v in attr_obj.get("proposed_values", []) or []:
        if v.get("confidence", 0.0) >= CONF_THRESHOLD and v.get("evidence"):
            out.append(v["value"])
    return out


def _wipe_workspace(db, ws_id: int) -> None:
    db.query(CustomerAttributeAffinity).filter(
        CustomerAttributeAffinity.workspace_id == ws_id
    ).delete(synchronize_session=False)
    db.query(CustomerPurchase).filter(
        CustomerPurchase.workspace_id == ws_id
    ).delete(synchronize_session=False)
    prod_ids = [p.id for p in db.query(Product).filter(
        Product.workspace_id == ws_id
    ).all()]
    if prod_ids:
        db.query(ProductAttribute).filter(
            ProductAttribute.product_id.in_(prod_ids)
        ).delete(synchronize_session=False)
    db.query(Product).filter(
        Product.workspace_id == ws_id
    ).delete(synchronize_session=False)
    db.flush()


def _seed_catalog(db, ws_id: int, catalog: list[dict]) -> dict[str, Product]:
    by_pid: dict[str, Product] = {}
    for entry in catalog:
        pid = entry["product_id"]
        prod = Product(
            workspace_id=ws_id,
            product_id=pid,
            sku=entry.get("style_code") or pid,
            name=entry["name"],
            group_id=None,
        )
        db.add(prod)
        db.flush()
        by_pid[pid] = prod
        for attr_name, attr_obj in (entry.get("attributes") or {}).items():
            for value in _kept_values(attr_obj):
                db.add(ProductAttribute(
                    product_id=prod.id,
                    attribute_id=attr_name,
                    attribute_value=value,
                ))
    db.flush()
    return by_pid


# ---------------------------------------------------------------------------
# 2. Customer carts — deterministic predicates over enriched values only
# ---------------------------------------------------------------------------

def _product_values(entry: dict, attr_name: str) -> set[str]:
    obj = (entry.get("attributes") or {}).get(attr_name) or {}
    return set(_kept_values(obj))


def _select_by_activity(
    catalog: list[dict],
    activity: str,
    count: int,
    secondary_attr: str | None = None,
    secondary_value: str | None = None,
) -> list[str]:
    """Pick `count` product_ids whose enriched activity_type contains `activity`,
    sorted by (-secondary_match, product_id) for determinism."""
    pool = [
        e for e in catalog
        if activity in _product_values(e, "activity_type")
    ]
    def sort_key(e: dict):
        sec = 1 if (
            secondary_attr is not None
            and secondary_value is not None
            and secondary_value in _product_values(e, secondary_attr)
        ) else 0
        return (-sec, e["product_id"])
    pool.sort(key=sort_key)
    return [e["product_id"] for e in pool[:count]]


def _select_mixed(catalog: list[dict], activities: list[str]) -> list[str]:
    out: list[str] = []
    for a in activities:
        pool = sorted(
            [e for e in catalog if a in _product_values(e, "activity_type")],
            key=lambda e: e["product_id"],
        )
        if pool:
            chosen = next(
                (e["product_id"] for e in pool if e["product_id"] not in out),
                None,
            )
            if chosen:
                out.append(chosen)
    return out


def _build_carts(catalog: list[dict]) -> dict[str, list[str]]:
    return {
        "C_REAL_1_yoga_indoor_low": _select_by_activity(
            catalog, "yoga", 3,
            secondary_attr="environment", secondary_value="indoor",
        ),
        "C_REAL_2_training_moderate": _select_by_activity(
            catalog, "training", 3,
            secondary_attr="workout_intensity", secondary_value="moderate",
        ),
        "C_REAL_3_running": _select_by_activity(catalog, "running", 3),
        "C_REAL_4_lounge": _select_by_activity(catalog, "lounge", 3),
        "C_REAL_5_mixed": _select_mixed(
            catalog, ["yoga", "running", "training", "lounge"]
        ),
    }


# ---------------------------------------------------------------------------
# 3. Insert purchases (uses CustomerPurchase model — affinity_service reads this)
# ---------------------------------------------------------------------------

def _insert_purchases(
    db, ws_id: int, by_pid: dict[str, Product], carts: dict[str, list[str]]
) -> None:
    for cust_id, pids in carts.items():
        for pid in pids:
            prod = by_pid[pid]
            db.add(CustomerPurchase(
                workspace_id=ws_id,
                customer_id=cust_id,
                product_db_id=prod.id,
                product_id=prod.product_id,
                group_id=prod.group_id,
                order_date=ORDER_DATE,
                quantity=1,
                revenue=None,
            ))
    db.flush()


# ---------------------------------------------------------------------------
# 4. Reporting helpers
# ---------------------------------------------------------------------------

def _fmt_attrs(entry: dict) -> str:
    parts = []
    for a in ENRICHED_ATTRS:
        vals = sorted(_product_values(entry, a))
        parts.append(f"{a}={'|'.join(vals) if vals else '-'}")
    return "  ".join(parts)


def _print_cart(cust_id: str, pids: list[str], catalog: list[dict]) -> None:
    by_pid = {e["product_id"]: e for e in catalog}
    print(f"  customer_id        : {cust_id}")
    print(f"  purchased products :")
    for pid in pids:
        e = by_pid[pid]
        print(f"    - {pid}  {e['name']}")
        print(f"      {_fmt_attrs(e)}")


def _print_derived_affinities(db, ws_id: int, cust_id: str) -> None:
    rows = (
        db.query(CustomerAttributeAffinity)
        .filter(
            CustomerAttributeAffinity.workspace_id == ws_id,
            CustomerAttributeAffinity.customer_id == cust_id,
        )
        .all()
    )
    if not rows:
        print("    (no affinities)")
        return
    by_attr: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in rows:
        by_attr[r.attribute_id].append((r.attribute_value, r.score))
    for attr in ENRICHED_ATTRS:
        items = sorted(by_attr.get(attr, []), key=lambda t: -t[1])
        if not items:
            print(f"    {attr:18s} (none derived)")
            continue
        s = ", ".join(f"{v}={sc:.3f}" for v, sc in items)
        print(f"    {attr:18s} {s}")


def _top_contrib_attrs(rec, k: int = 3) -> str:
    """Top-k matched attributes ranked by |score * weight|, with their effect."""
    sorted_m = sorted(
        rec.matched_attributes,
        key=lambda m: -abs(m.score * m.weight),
    )
    out = []
    for m in sorted_m[:k]:
        eff = round(m.score * m.weight, 3)
        out.append(f"{m.attribute_id}={m.attribute_value} (s={m.score:.2f}*w={m.weight}={eff:+.3f})")
    return " | ".join(out) if out else "-"


def _penalty_notes(rec) -> str:
    notes: list[str] = []
    if rec.compatibility_negative_contribution > 0:
        # which compat attrs were mismatched / duplicated?
        roles = [
            f"{m.attribute_id}={m.attribute_value}"
            for m in rec.matched_attributes
            if m.targeting_mode == "compatibility_signal"
        ]
        if roles:
            notes.append(
                f"compat- {rec.compatibility_negative_contribution:.3f} "
                f"(complementary duplicate on {','.join(roles)})"
            )
        else:
            notes.append(
                f"compat- {rec.compatibility_negative_contribution:.3f} "
                f"(compatibility mismatch — engine penalty pass)"
            )
    if rec.contextual_negative_contribution > 0:
        notes.append(
            f"ctx- {rec.contextual_negative_contribution:.3f} "
            f"(contextual mismatch on occasion/activity/environment)"
        )
    if rec.low_signal_penalty > 0:
        notes.append(
            f"low_signal- {rec.low_signal_penalty:.3f} "
            f"(weak enrichment coverage)"
        )
    return "; ".join(notes) if notes else "-"


def _print_top5(rows) -> None:
    if not rows:
        print("    (no recommendations)")
        return
    header = (
        f"{'#':<3}{'product_id':<10} {'name':<34} "
        f"{'final':>8} {'direct':>8} {'rel':>6} {'beh':>6} "
        f"{'comp+':>7} {'comp-':>7} {'ctx-':>7} {'lowsig-':>8} {'source':<20}"
    )
    print(header)
    print("-" * len(header))
    for i, r in enumerate(rows[:TOP_N], 1):
        name = (r.name[:32] + "..") if len(r.name) > 34 else r.name
        print(
            f"#{i:<2}{r.product_id:<10} {name:<34} "
            f"{r.recommendation_score:>+8.3f} "
            f"{r.direct_score:>+8.3f} "
            f"{r.relationship_score:>+6.3f} "
            f"{r.behavioral_score:>+6.3f} "
            f"{r.compatibility_positive_contribution:>+7.3f} "
            f"{r.compatibility_negative_contribution:>+7.3f} "
            f"{r.contextual_negative_contribution:>+7.3f} "
            f"{r.low_signal_penalty:>+8.3f} "
            f"{r.recommendation_source:<20}"
        )
        print(f"     top3_attrs : {_top_contrib_attrs(r, 3)}")
        print(f"     penalties  : {_penalty_notes(r)}")


# ---------------------------------------------------------------------------
# 5. Main
# ---------------------------------------------------------------------------

def main() -> None:
    catalog = _load_enriched_catalog()
    print(f"Loaded {len(catalog)} products from {ENRICHED_PATH.name}")

    raw_defs = json.loads(ATTR_DEFS_PATH.read_text(encoding="utf-8"))
    attr_defs = [AttributeDefinition(**d) for d in raw_defs]
    targeting_modes = {d.name: d.targeting_mode.value for d in attr_defs}
    behaviors = {d.name: d.behavior for d in attr_defs}

    db = SessionLocal()
    try:
        # Workspace setup (idempotent)
        ws = db.query(Workspace).filter(Workspace.slug == WORKSPACE_SLUG).first()
        if ws is None:
            ws = Workspace(slug=WORKSPACE_SLUG, name=WORKSPACE_NAME)
            db.add(ws)
            db.flush()
        _wipe_workspace(db, ws.id)
        by_pid = _seed_catalog(db, ws.id, catalog)

        # Carts
        carts = _build_carts(catalog)
        _insert_purchases(db, ws.id, by_pid, carts)

        # Derive affinities (existing engine)
        result = generate_affinities_from_purchases(db, workspace_id=ws.id)
        print(f"Affinity derivation: customers={result.customers_processed} "
              f"upserts={result.affinities_upserted}")

        # Per-customer report
        for cust_id, pids in carts.items():
            print()
            print("=" * 100)
            print(f"CUSTOMER: {cust_id}")
            print("=" * 100)
            print()
            print("CART (programmatically selected):")
            _print_cart(cust_id, pids, catalog)
            print()
            print("DERIVED AFFINITIES (count/max-count via affinity_service):")
            _print_derived_affinities(db, ws.id, cust_id)
            print()
            sig = compute_customer_signal_strength(db, ws.id, cust_id)
            print(
                f"CUSTOMER SIGNAL STRENGTH: {sig.customer_signal_strength:.3f} "
                f"(purchase_depth={sig.components.purchase_depth:.3f}, "
                f"attribute_richness={sig.components.attribute_richness:.3f}, "
                f"behavioral_graph={sig.components.behavioral_graph:.3f})"
            )
            print()
            print("TOP 5 RECOMMENDATIONS (via recommendation_service.get_recommendations):")
            recs, _ = get_recommendations(
                db,
                workspace_id=ws.id,
                customer_id=cust_id,
                top_n=TOP_N,
                attribute_targeting_modes=targeting_modes,
                attribute_behaviors=behaviors,
                reference_date=ORDER_DATE,
                customer_signal_strength=sig.customer_signal_strength,
            )
            _print_top5(recs)
            print()
            print("WHY #1 (engine explanation field):")
            if recs:
                print(f"    {recs[0].product_id}: {recs[0].explanation}")
            else:
                print("    (no recommendations)")

        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
