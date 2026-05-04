"""Wave B brand approvals (count 5-9) with per-candidate validation.

For each Wave B candidate brand:
  1. Pull 3 sample product names + product_ids.
  2. Validate:
       (a) cluster_key length >= 3 (drops e.g. 'me')
       (b) not in a small generic/seasonal-word blocklist
       (c) brand string appears as substring in >=1 sample product name
           (credible-brand signal: products carry the brand in their name)
  3. Classify: approve | reject (with reason).
  4. Approve validated brands via the engine endpoint (no force=true).
  5. Run brand backfill (one PA row per product, single-value).

No merges. No recommendations. No approvals outside Wave B.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import (
    ProposedAttributeValueAggregate as A,
    ProposedAttributeValueEvent as E,
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_MERGED,
)
from app.models.workspace import Workspace
from app.services.proposed_attribute_value_service import promotion_readiness

WS_SLUG = "mumzworld_v3_sample"
ATTR = "brand"
client = TestClient(app)

# Wave B candidates per the prior import snapshot.
WAVE_B = [
    "byft", "disney", "amscan", "ajooba", "meri meri", "megastar",
    "penguin books", "usborne books", "ginger ray", "twinkle hands",
    "bestway", "me", "neon", "fissman",
]

# Filter rules.
GENERIC_BLOCKLIST = frozenset({
    "me", "my", "you", "new", "old", "the", "all", "top", "big",
    "unique", "classic", "premium", "basic", "generic",
    "christmas", "easter", "summer", "winter", "halloween",
    "neon", "pink", "blue", "red", "green",  # colour words masquerading as brands
})
MIN_LEN = 3


def status_counts(db, ws_id, attribute):
    out = {}
    for s, in db.query(A.status).filter(
        A.workspace_id == ws_id, A.attribute_name == attribute
    ).all():
        out[s] = out.get(s, 0) + 1
    return out


def aav_active_count(db, ws_id, attribute):
    return db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == attribute,
        AttributeAllowedValue.is_active == True).count()


def coverage(db, ws_id, attribute):
    total = db.query(Product).filter(Product.workspace_id == ws_id).count()
    with_attr = (db.query(ProductAttribute.product_id)
                 .join(Product, ProductAttribute.product_id == Product.id)
                 .filter(Product.workspace_id == ws_id,
                         ProductAttribute.attribute_id == attribute)
                 .distinct().count())
    return total, with_attr


def sample_products_for_brand(db, ws_id: int, cluster_key: str, n: int = 3):
    """Return up to n sample (product_id, name) tuples drawn from events
    for this cluster_key."""
    evs = (db.query(E)
           .filter(E.workspace_id == ws_id, E.attribute_name == ATTR,
                   E.normalized_value == cluster_key)
           .all())
    seen: set[str] = set()
    samples: list[tuple[str, str]] = []
    for ev in evs:
        if ev.product_id in seen:
            continue
        seen.add(ev.product_id)
        prod = db.query(Product).filter(
            Product.workspace_id == ws_id,
            Product.product_id == ev.product_id).first()
        if prod is None:
            continue
        samples.append((prod.product_id, prod.name or ""))
        if len(samples) >= n:
            break
    return samples


def validate(cluster_key: str, samples: list[tuple[str, str]]) -> tuple[str, str]:
    """Return (decision, reason)."""
    if len(cluster_key) < MIN_LEN:
        return ("reject", f"cluster_key length {len(cluster_key)} < {MIN_LEN} (likely tagging artefact)")
    if cluster_key in GENERIC_BLOCKLIST:
        return ("reject", "generic / colour / seasonal token, not a credible brand")
    # Substring check: brand must appear in at least one product name.
    needle = cluster_key.lower()
    in_name = sum(1 for _pid, name in samples if needle in (name or "").lower())
    if in_name == 0:
        return ("reject", f"brand string {needle!r} not found in any of {len(samples)} sample names")
    return ("approve", f"appears in {in_name}/{len(samples)} sample names; credible brand")


def run_brand_backfill(db, ws_id) -> tuple[int, Counter]:
    """Same logic as Wave A: one PA(brand) per product whose
    highest-confidence event resolves into the active brand AAV."""
    active_aav = {
        v for (v,) in db.query(AttributeAllowedValue.value).filter(
            AttributeAllowedValue.workspace_id == ws_id,
            AttributeAllowedValue.attribute_name == ATTR,
            AttributeAllowedValue.is_active == True,
        ).all()
    }
    active_lower = {v.lower() for v in active_aav}
    cluster_to_canonical: dict[str, str] = {}
    for agg in db.query(A).filter(
        A.workspace_id == ws_id, A.attribute_name == ATTR
    ).all():
        if agg.status == PROPOSAL_STATUS_APPROVED:
            canonical = agg.canonical_value
        elif agg.status == PROPOSAL_STATUS_MERGED:
            canonical = agg.promoted_to_allowed_value
        else:
            continue
        if canonical and canonical.lower() in active_lower:
            cluster_to_canonical[agg.cluster_key] = next(
                (v for v in active_aav if v.lower() == canonical.lower()),
                canonical,
            )

    products = db.query(Product).filter(Product.workspace_id == ws_id).all()
    already = {
        pid for (pid,) in db.query(ProductAttribute.product_id)
        .join(Product, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == ws_id,
                ProductAttribute.attribute_id == ATTR)
        .distinct().all()
    }
    events_by_pid: dict[str, list] = defaultdict(list)
    for ev in db.query(E).filter(
        E.workspace_id == ws_id, E.attribute_name == ATTR
    ).all():
        events_by_pid[ev.product_id].append(ev)

    inserted = 0
    by_canonical: Counter = Counter()
    for prod in products:
        if prod.id in already:
            continue
        evs = events_by_pid.get(prod.product_id, [])
        if not evs:
            continue
        assignable = []
        for ev in evs:
            canonical = cluster_to_canonical.get(ev.normalized_value)
            if canonical is None:
                continue
            assignable.append((ev, canonical))
        if not assignable:
            continue
        ev, canonical = max(
            assignable,
            key=lambda x: (float(x[0].confidence or 0), x[0].created_at),
        )
        db.add(ProductAttribute(
            product_id=prod.id, attribute_id=ATTR, attribute_value=canonical,
        ))
        inserted += 1
        by_canonical[canonical] += 1
    if inserted:
        db.commit()
    return inserted, by_canonical


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).one()
        ws_id = ws.id
        before = {
            "agg_brand_status": status_counts(db, ws_id, ATTR),
            "aav_brand_active": aav_active_count(db, ws_id, ATTR),
            "events_brand": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
            "pa_brand": (db.query(ProductAttribute)
                         .join(Product, ProductAttribute.product_id == Product.id)
                         .filter(Product.workspace_id == ws_id,
                                 ProductAttribute.attribute_id == ATTR).count()),
            "agg_pt_status": status_counts(db, ws_id, "product_type"),
            "agg_age_status": status_counts(db, ws_id, "age_group"),
            "aav_pt_active": aav_active_count(db, ws_id, "product_type"),
            "aav_age_active": aav_active_count(db, ws_id, "age_group"),
            "pa_pt": (db.query(ProductAttribute)
                      .join(Product, ProductAttribute.product_id == Product.id)
                      .filter(Product.workspace_id == ws_id,
                              ProductAttribute.attribute_id == "product_type").count()),
            "pa_age": (db.query(ProductAttribute)
                       .join(Product, ProductAttribute.product_id == Product.id)
                       .filter(Product.workspace_id == ws_id,
                               ProductAttribute.attribute_id == "age_group").count()),
        }

        # Per-candidate validation.
        validations: list[dict] = []
        for ck in WAVE_B:
            agg = db.query(A).filter(
                A.workspace_id == ws_id, A.attribute_name == ATTR,
                A.cluster_key == ck).first()
            if agg is None:
                validations.append({
                    "cluster_key": ck, "agg_id": None, "samples": [],
                    "decision": "reject", "reason": "aggregate not found",
                })
                continue
            samples = sample_products_for_brand(db, ws_id, ck)
            decision, reason = validate(ck, samples)
            check = promotion_readiness(agg)
            if not check.ready:
                decision = "reject"
                reason = f"not ready ({check.reasons}); skipping"
            elif agg.status != "pending":
                decision = "reject"
                reason = f"status={agg.status} (not pending)"
            validations.append({
                "cluster_key": ck,
                "agg_id": agg.id,
                "count": agg.proposal_count,
                "distinct": agg.distinct_product_count,
                "avg_conf": agg.avg_confidence,
                "samples": samples,
                "decision": decision,
                "reason": reason,
            })
    finally:
        db.close()

    # ----- Per-candidate validation print -----
    print("=" * 88)
    print(f"WAVE B BRAND VALIDATION  (ws_id={ws_id})")
    print("=" * 88)
    for v in validations:
        marker = "[APPROVE]" if v["decision"] == "approve" else "[REJECT] "
        print(f"\n{marker} {v['cluster_key']!r:<22} count={v.get('count','-'):<3} "
              f"distinct={v.get('distinct','-'):<3} avg_conf={v.get('avg_conf','-')}")
        print(f"          reason: {v['reason']}")
        for pid, name in v["samples"]:
            print(f"          sample: {pid:<28} {(name or '')[:62]!r}")

    # ----- Approvals -----
    to_approve = [v for v in validations if v["decision"] == "approve"]
    rejected = [v for v in validations if v["decision"] == "reject"]
    print()
    print("=" * 88)
    print(f"APPROVING {len(to_approve)} OF {len(WAVE_B)} CANDIDATES")
    print("=" * 88)
    approved_ok: list[str] = []
    for v in to_approve:
        r = client.post(
            f"/admin/taxonomy/api/aggregates/{v['agg_id']}/approve",
            params={"workspace_id": ws_id, "attribute": ATTR},
        )
        if r.status_code != 200:
            print(f"  {v['cluster_key']:<22} HTTP {r.status_code}  body={r.text[:120]}")
            raise SystemExit(f"approval for {v['cluster_key']} failed")
        body = r.json()
        approved_ok.append(v["cluster_key"])
        print(f"  {v['cluster_key']:<22} agg.id={v['agg_id']} HTTP 200  -> "
              f"status={body['status']}")

    # ----- Backfill -----
    print()
    print("=" * 88)
    print("BACKFILL")
    print("=" * 88)
    db = SessionLocal()
    try:
        inserted, by_canonical = run_brand_backfill(db, ws_id)
    finally:
        db.close()
    print(f"  PA(brand) rows inserted : {inserted}")
    for v, n in sorted(by_canonical.items(), key=lambda x: -x[1]):
        print(f"    {v:<22} {n}")

    # ----- Coverage + samples -----
    db = SessionLocal()
    try:
        total, with_brand = coverage(db, ws_id, ATTR)
        # Sample 5 products newly carrying brands from approved Wave B set.
        wave_b_canonicals = approved_ok
        sample_rows = []
        if wave_b_canonicals:
            sample_rows = (db.query(ProductAttribute, Product)
                           .join(Product, ProductAttribute.product_id == Product.id)
                           .filter(Product.workspace_id == ws_id,
                                   ProductAttribute.attribute_id == ATTR,
                                   ProductAttribute.attribute_value.in_(wave_b_canonicals))
                           .order_by(Product.id)
                           .limit(5).all())
        # Per-product max (must be 1).
        per_product = Counter(
            pa.product_id for pa in db.query(ProductAttribute)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR).all()
        )
        max_per_product = max(per_product.values()) if per_product else 0
        # Final state.
        after = {
            "agg_brand_status": status_counts(db, ws_id, ATTR),
            "aav_brand_active": aav_active_count(db, ws_id, ATTR),
            "events_brand": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
            "pa_brand": (db.query(ProductAttribute)
                         .join(Product, ProductAttribute.product_id == Product.id)
                         .filter(Product.workspace_id == ws_id,
                                 ProductAttribute.attribute_id == ATTR).count()),
            "agg_pt_status": status_counts(db, ws_id, "product_type"),
            "agg_age_status": status_counts(db, ws_id, "age_group"),
            "aav_pt_active": aav_active_count(db, ws_id, "product_type"),
            "aav_age_active": aav_active_count(db, ws_id, "age_group"),
            "pa_pt": (db.query(ProductAttribute)
                      .join(Product, ProductAttribute.product_id == Product.id)
                      .filter(Product.workspace_id == ws_id,
                              ProductAttribute.attribute_id == "product_type").count()),
            "pa_age": (db.query(ProductAttribute)
                       .join(Product, ProductAttribute.product_id == Product.id)
                       .filter(Product.workspace_id == ws_id,
                               ProductAttribute.attribute_id == "age_group").count()),
        }
    finally:
        db.close()

    # ----- Output -----
    print()
    print("=" * 88)
    print("OUTPUT")
    print("=" * 88)
    print(f"\n1. APPROVED BRANDS ({len(approved_ok)}):")
    for ck in approved_ok:
        print(f"     {ck}")

    print(f"\n2. REJECTED BRANDS ({len(rejected)}):")
    for v in rejected:
        print(f"     {v['cluster_key']!r:<22} reason: {v['reason']}")

    print(f"\n3. ProductAttribute rows added: {inserted}")
    for cv, n in sorted(by_canonical.items(), key=lambda x: -x[1]):
        print(f"     {cv:<22} {n}")

    print(f"\n4. NEW BRAND COVERAGE:")
    print(f"     products with brand : {with_brand}/{total} = "
          f"{100*with_brand/max(total,1):.2f}%  "
          f"(was {before['pa_brand']}/{total} = "
          f"{100*before['pa_brand']/max(total,1):.2f}%)")
    print(f"     max brand rows per product : {max_per_product}  (must be 1)")

    print(f"\n5. 5 SAMPLE ASSIGNMENTS:")
    for pa, prod in sample_rows:
        name = (prod.name or "")[:64]
        print(f"     {prod.product_id:<28} brand={pa.attribute_value!r:<18} "
              f"name={name!r}")

    # ----- Confirmation -----
    print()
    print("=" * 88)
    print("CONFIRMATION  (no other data modified)")
    print("=" * 88)
    def cmp(label, b, a):
        ok = b == a
        marker = "OK  " if ok else "FAIL"
        print(f"  {marker}  {label:<32} before={b}  after={a}")
    cmp("product_type aggregates",   before["agg_pt_status"],   after["agg_pt_status"])
    cmp("age_group   aggregates",    before["agg_age_status"],  after["agg_age_status"])
    cmp("AAV active product_type",   before["aav_pt_active"],   after["aav_pt_active"])
    cmp("AAV active age_group",      before["aav_age_active"],  after["aav_age_active"])
    cmp("PA rows product_type",      before["pa_pt"],           after["pa_pt"])
    cmp("PA rows age_group",         before["pa_age"],          after["pa_age"])
    cmp("brand events (preserved)",  before["events_brand"],    after["events_brand"])
    print()
    print("  no merges executed.")
    print("  no recommendations re-run.")
    print("  no force=true used.")
    print("  no scoring changes.")
    print("  one brand per product enforced.")


if __name__ == "__main__":
    main()
