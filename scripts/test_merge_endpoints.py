"""End-to-end validation for the new merge-suggestion endpoints.

Runs entirely under FastAPI TestClient against an isolated workspace
``taxonomy_merge_test``. Production workspaces (mumzworld_v3_sample,
taxonomy_admin_ui_test) are NOT mutated by this script -- we only call
the read-only GET on mumzworld_v3_sample at the end as a real-world
demonstration of the suggestion detector.

The 7 PART-5 test cases:
    1. normalization_variant merge          (Pair A: widget_pro / widgetpro)
    2. parent_child merge                   (Pair B: tool_chest / tool)
    3. semantic_duplicate suggestion only   (Pair C: gizmo / dohickey)
    4. source approved -> target approved   (Pair B; source AAV deactivated)
    5. source pending  -> target approved   (Pair A; no AAV deactivation)
    6. ProductAttribute update after merge  (Pair A; WP-1/2/3 -> widgetpro)
    7. duplicate ProductAttribute prevent.  (Pair A; WP-4 has both, source dropped)

Idempotent: re-running clears the test workspace and reseeds.
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import (
    ProposedAttributeValueAggregate as A,
    ProposedAttributeValueEvent as E,
)
from app.models.workspace import Workspace
from app.schemas.attribute_enrichment import (
    EnrichmentOutput, EnrichmentSource, ProposedValue,
)
from app.services.proposed_attribute_value_service import (
    refresh_aggregates, record_events_from_output,
)

WS_SLUG = "taxonomy_merge_test"
ATTR = "product_type"
client = TestClient(app)
results: list[tuple[str, str, str]] = []


def step(name: str, ok: bool, detail: str = ""):
    mark = "PASS" if ok else "FAIL"
    results.append((mark, name, detail))
    print(f"  [{mark}] {name}  {detail}")


def seed_cluster(db, ws_id, product_ids, raw_value, evidence_words):
    """Seed N products + N events with the same proposed value, so refresh
    creates one aggregate with cluster_key=normalize(raw_value).
    Evidence is the joined evidence_words list so the semantic detector
    has stable text to score."""
    for pid in product_ids:
        if not db.query(Product).filter(
            Product.workspace_id == ws_id, Product.product_id == pid
        ).first():
            db.add(Product(workspace_id=ws_id, product_id=pid, sku=pid,
                           name=f"Test product {pid}"))
    db.flush()
    for pid in product_ids:
        record_events_from_output(
            db, workspace_id=ws_id, product_id=pid,
            output=EnrichmentOutput(
                attribute_name=ATTR, attribute_class="compatibility",
                values=[],
                proposed_values=[ProposedValue(
                    value=raw_value, confidence=0.95,
                    evidence=[" ".join(evidence_words)])],
                warnings=[], source=EnrichmentSource.TEXT,
            ),
        )


def main() -> None:
    print("=== A. Setup: isolated test workspace ===")
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).first()
        if ws is not None:
            db.query(E).filter(E.workspace_id == ws.id).delete(
                synchronize_session=False)
            db.query(A).filter(A.workspace_id == ws.id).delete(
                synchronize_session=False)
            db.query(AttributeAllowedValue).filter(
                AttributeAllowedValue.workspace_id == ws.id
            ).delete(synchronize_session=False)
            prod_ids = [p.id for p in db.query(Product).filter(
                Product.workspace_id == ws.id).all()]
            if prod_ids:
                db.query(ProductAttribute).filter(
                    ProductAttribute.product_id.in_(prod_ids)
                ).delete(synchronize_session=False)
            db.query(Product).filter(Product.workspace_id == ws.id).delete(
                synchronize_session=False)
            db.flush()
        else:
            ws = Workspace(slug=WS_SLUG, name="Taxonomy Merge test")
            db.add(ws); db.flush()
        ws_id = ws.id

        # Pair A: normalization variant (widget_pro vs widgetpro).
        # Source widget_pro carries PA rows so we can test PA update + dedup.
        seed_cluster(db, ws_id, ["WP-A1", "WP-A2", "WP-A3", "WP-A4"],
                     "widget_pro",
                     ["pro grade widget for industrial use case"])
        seed_cluster(db, ws_id, ["WP-B1", "WP-B2", "WP-B3"],
                     "widgetpro",
                     ["pro grade widget for industrial use case"])

        # Pair B: parent/child (tool_chest -> tool). Both will be approved
        # so we can verify source AAV deactivation.
        seed_cluster(db, ws_id, ["TC-1", "TC-2", "TC-3"],
                     "tool_chest",
                     ["heavy duty storage container for hand tools"])
        seed_cluster(db, ws_id, ["TL-1", "TL-2", "TL-3"],
                     "tool",
                     ["general purpose hand tool for repair"])

        # Pair C: semantic duplicate (gizmo vs dohickey). Same evidence
        # vocabulary, no shared cluster-key tokens. Suggestion only -- not
        # executed (target is not approved).
        seed_cluster(db, ws_id, ["GZ-1", "GZ-2", "GZ-3"],
                     "gizmo",
                     ["sparkle glow lume shine flicker novelty"])
        seed_cluster(db, ws_id, ["DH-1", "DH-2", "DH-3"],
                     "dohickey",
                     ["sparkle glow lume shine flicker novelty"])

        refresh_aggregates(db, workspace_id=ws_id, attribute_name=ATTR)
        db.commit()

        # Seed ProductAttribute rows for Pair A.
        # WP-A1, WP-A2, WP-A3 carry only the source (widget_pro).
        # WP-A4 carries BOTH source and target (duplicate scenario).
        prod_db = {p.product_id: p.id for p in db.query(Product).filter(
            Product.workspace_id == ws_id).all()}
        for ext_pid in ("WP-A1", "WP-A2", "WP-A3", "WP-A4"):
            db.add(ProductAttribute(product_id=prod_db[ext_pid],
                                    attribute_id=ATTR,
                                    attribute_value="widget_pro"))
        db.add(ProductAttribute(product_id=prod_db["WP-A4"],
                                attribute_id=ATTR,
                                attribute_value="widgetpro"))
        db.commit()

        agg_widget_pro = db.query(A).filter(
            A.workspace_id == ws_id, A.cluster_key == "widget_pro").one()
        agg_widgetpro = db.query(A).filter(
            A.workspace_id == ws_id, A.cluster_key == "widgetpro").one()
        agg_tool_chest = db.query(A).filter(
            A.workspace_id == ws_id, A.cluster_key == "tool_chest").one()
        agg_tool = db.query(A).filter(
            A.workspace_id == ws_id, A.cluster_key == "tool").one()
        agg_gizmo = db.query(A).filter(
            A.workspace_id == ws_id, A.cluster_key == "gizmo").one()
        agg_doh = db.query(A).filter(
            A.workspace_id == ws_id, A.cluster_key == "dohickey").one()

        step("seeded 6 aggregates", True,
             f"ws_id={ws_id} pair_a=({agg_widget_pro.id},{agg_widgetpro.id}) "
             f"pair_b=({agg_tool_chest.id},{agg_tool.id}) "
             f"pair_c=({agg_gizmo.id},{agg_doh.id})")
    finally:
        db.close()

    print()
    print("=== B. Approve targets (and Pair B source) ===")
    # Pair A target: widgetpro
    r = client.post(
        f"/admin/taxonomy/api/aggregates/{agg_widgetpro.id}/approve",
        params={"workspace_id": ws_id, "attribute": ATTR},
    )
    step("approve widgetpro -> 200", r.status_code == 200,
         f"status={r.json().get('status')}")

    # Pair B source AND target both approved -- tests source AAV deactivation.
    r = client.post(
        f"/admin/taxonomy/api/aggregates/{agg_tool.id}/approve",
        params={"workspace_id": ws_id, "attribute": ATTR},
    )
    step("approve tool -> 200", r.status_code == 200)
    r = client.post(
        f"/admin/taxonomy/api/aggregates/{agg_tool_chest.id}/approve",
        params={"workspace_id": ws_id, "attribute": ATTR},
    )
    step("approve tool_chest -> 200", r.status_code == 200)

    print()
    print("=== C. GET /merge_suggestions: detector output ===")
    r = client.get("/admin/taxonomy/api/merge_suggestions",
                   params={"workspace_id": ws_id, "attribute": ATTR})
    step("GET merge_suggestions -> 200", r.status_code == 200)
    body = r.json()
    items = body.get("items", [])
    by_type = body.get("by_type_count", {})
    norm_pairs = [s for s in items if s["merge_type"] == "normalization_variant"]
    pc_pairs   = [s for s in items if s["merge_type"] == "parent_child"]
    sem_pairs  = [s for s in items if s["merge_type"] == "semantic_duplicate"]
    step(">=1 normalization_variant detected", len(norm_pairs) >= 1,
         f"count={by_type.get('normalization_variant')}")
    step(">=1 parent_child detected", len(pc_pairs) >= 1,
         f"count={by_type.get('parent_child')}")
    step("semantic_duplicate detected for gizmo/dohickey",
         any({s["source_cluster"], s["target_cluster"]} == {"gizmo", "dohickey"}
             for s in sem_pairs),
         f"count={by_type.get('semantic_duplicate')}")

    # Find the widget_pro / widgetpro suggestion.
    pair_a = next((s for s in norm_pairs
                   if {s["source_cluster"], s["target_cluster"]}
                       == {"widget_pro", "widgetpro"}), None)
    step("Pair A suggestion found", pair_a is not None)
    if pair_a:
        step("Pair A target=widgetpro (approved preferred)",
             pair_a["target_cluster"] == "widgetpro",
             f"target={pair_a['target_cluster']}")
        step("Pair A executable=True (target approved)",
             pair_a["executable"] is True)
        step("Pair A confidence=high",
             pair_a["confidence"] == "high")

    # Find the tool_chest / tool suggestion (parent_child).
    pair_b = next((s for s in pc_pairs
                   if {s["source_cluster"], s["target_cluster"]}
                       == {"tool_chest", "tool"}), None)
    step("Pair B parent_child suggestion found", pair_b is not None)
    if pair_b:
        step("Pair B target=tool (parent)",
             pair_b["target_cluster"] == "tool")
        step("Pair B executable=True", pair_b["executable"] is True)

    # Pair C must NOT be marked executable (target not approved).
    pair_c = next((s for s in sem_pairs
                   if {s["source_cluster"], s["target_cluster"]}
                       == {"gizmo", "dohickey"}), None)
    if pair_c:
        step("Pair C executable=False (no approved target)",
             pair_c["executable"] is False)
        step("Pair C has 'target is not approved' risk note",
             any("not approved" in n for n in pair_c.get("risk_notes", [])))

    print()
    print("=== D. Negative-path safety on /merge_suggestions/execute ===")
    base_body = {
        "workspace_id": ws_id, "attribute": ATTR,
        "source_cluster": "widget_pro", "target_cluster": "widgetpro",
        "merge_type": "normalization_variant",
    }

    # workspace_id missing.
    r = client.post("/admin/taxonomy/api/merge_suggestions/execute",
                    json={**base_body, "workspace_id": 0})
    step("missing workspace_id -> 400", r.status_code == 400, r.text[:80])

    # invalid merge_type.
    r = client.post("/admin/taxonomy/api/merge_suggestions/execute",
                    json={**base_body, "merge_type": "bogus"})
    step("invalid merge_type -> 400", r.status_code == 400, r.text[:80])

    # source==target.
    r = client.post("/admin/taxonomy/api/merge_suggestions/execute",
                    json={**base_body, "source_cluster": "widgetpro"})
    step("source==target -> 400", r.status_code == 400, r.text[:80])

    # nonexistent source.
    r = client.post("/admin/taxonomy/api/merge_suggestions/execute",
                    json={**base_body, "source_cluster": "no_such_cluster"})
    step("nonexistent source -> 404", r.status_code == 404, r.text[:80])

    # target not approved (Pair C: gizmo -> dohickey).
    r = client.post("/admin/taxonomy/api/merge_suggestions/execute",
                    json={"workspace_id": ws_id, "attribute": ATTR,
                          "source_cluster": "gizmo",
                          "target_cluster": "dohickey",
                          "merge_type": "semantic_duplicate"})
    step("target not approved -> 422", r.status_code == 422, r.text[:80])

    print()
    print("=== E. EXECUTE Pair A (norm variant; pending->approved) ===")
    # Snapshot PA state.
    db = SessionLocal()
    try:
        wp_pa_before = (
            db.query(ProductAttribute)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR,
                    ProductAttribute.attribute_value == "widget_pro")
            .count())
        wpo_pa_before = (
            db.query(ProductAttribute)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR,
                    ProductAttribute.attribute_value == "widgetpro")
            .count())
        step("PA before: widget_pro=4, widgetpro=1",
             wp_pa_before == 4 and wpo_pa_before == 1,
             f"widget_pro={wp_pa_before} widgetpro={wpo_pa_before}")
    finally:
        db.close()

    r = client.post("/admin/taxonomy/api/merge_suggestions/execute",
                    json={"workspace_id": ws_id, "attribute": ATTR,
                          "source_cluster": "widget_pro",
                          "target_cluster": "widgetpro",
                          "merge_type": "normalization_variant"})
    step("execute Pair A -> 200", r.status_code == 200, r.text[:120])
    body = r.json()
    if r.status_code == 200:
        step("Pair A source.status=merged",
             body["source"]["status"] == "merged")
        step("Pair A merge_reason=normalized_duplicate",
             body["merge_reason"] == "normalized_duplicate")
        step("Pair A source_allowed_value_deactivated=False (was pending)",
             body["source_allowed_value_deactivated"] is False)
        step("Pair A PA rows_updated=3 (WP-A1/2/3)",
             body["product_attribute"]["rows_updated"] == 3,
             f"updated={body['product_attribute']['rows_updated']}")
        step("Pair A PA duplicates_dropped=1 (WP-A4)",
             body["product_attribute"]["duplicates_dropped"] == 1,
             f"dropped={body['product_attribute']['duplicates_dropped']}")

    # Verify post-state in DB directly.
    db = SessionLocal()
    try:
        wp_pa_after = (
            db.query(ProductAttribute)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR,
                    ProductAttribute.attribute_value == "widget_pro")
            .count())
        wpo_pa_after = (
            db.query(ProductAttribute)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR,
                    ProductAttribute.attribute_value == "widgetpro")
            .count())
        step("PA after Pair A: widget_pro=0",
             wp_pa_after == 0, f"widget_pro={wp_pa_after}")
        step("PA after Pair A: widgetpro=4 (3 updated + 1 pre-existing)",
             wpo_pa_after == 4, f"widgetpro={wpo_pa_after}")

        # WP-A4 should have exactly one row, value=widgetpro.
        wp_a4 = db.query(Product).filter(
            Product.workspace_id == ws_id,
            Product.product_id == "WP-A4").one()
        wp_a4_rows = db.query(ProductAttribute).filter(
            ProductAttribute.product_id == wp_a4.id,
            ProductAttribute.attribute_id == ATTR).all()
        step("WP-A4 has exactly 1 PA row (duplicate dropped)",
             len(wp_a4_rows) == 1,
             f"rows={[r.attribute_value for r in wp_a4_rows]}")
        step("WP-A4 final value = widgetpro",
             len(wp_a4_rows) == 1 and wp_a4_rows[0].attribute_value == "widgetpro")

        # widget_pro AAV should NOT exist (was never approved).
        wp_aav = db.query(AttributeAllowedValue).filter(
            AttributeAllowedValue.workspace_id == ws_id,
            AttributeAllowedValue.attribute_name == ATTR,
            AttributeAllowedValue.value == "widget_pro").first()
        step("widget_pro AAV does not exist", wp_aav is None)
    finally:
        db.close()

    print()
    print("=== F. EXECUTE Pair B (parent/child; approved->approved) ===")
    db = SessionLocal()
    try:
        # Confirm both AAVs active before.
        tc_aav_before = db.query(AttributeAllowedValue).filter(
            AttributeAllowedValue.workspace_id == ws_id,
            AttributeAllowedValue.attribute_name == ATTR,
            AttributeAllowedValue.value == "tool_chest").first()
        step("tool_chest AAV active before merge",
             tc_aav_before is not None and tc_aav_before.is_active)
    finally:
        db.close()

    r = client.post("/admin/taxonomy/api/merge_suggestions/execute",
                    json={"workspace_id": ws_id, "attribute": ATTR,
                          "source_cluster": "tool_chest",
                          "target_cluster": "tool",
                          "merge_type": "parent_child"})
    step("execute Pair B -> 200", r.status_code == 200, r.text[:120])
    body = r.json()
    if r.status_code == 200:
        step("Pair B source.status=merged",
             body["source"]["status"] == "merged")
        step("Pair B merge_reason=flattened_child",
             body["merge_reason"] == "flattened_child")
        step("Pair B source_allowed_value_deactivated=True (was approved)",
             body["source_allowed_value_deactivated"] is True)
        step("Pair B 'tool_chest' not in allowed_values_after",
             "tool_chest" not in [v.lower() for v in body.get("allowed_values_after", [])])

    db = SessionLocal()
    try:
        tc_aav_after = db.query(AttributeAllowedValue).filter(
            AttributeAllowedValue.workspace_id == ws_id,
            AttributeAllowedValue.attribute_name == ATTR,
            AttributeAllowedValue.value == "tool_chest").first()
        step("tool_chest AAV row preserved (not deleted)",
             tc_aav_after is not None)
        step("tool_chest AAV is_active=False",
             tc_aav_after is not None and tc_aav_after.is_active is False)
    finally:
        db.close()

    print()
    print("=== G. Pair C (semantic_duplicate) NOT executed ===")
    db = SessionLocal()
    try:
        gizmo_status = db.query(A).filter(
            A.workspace_id == ws_id, A.cluster_key == "gizmo").one().status
        doh_status = db.query(A).filter(
            A.workspace_id == ws_id, A.cluster_key == "dohickey").one().status
        step("gizmo aggregate still pending",  gizmo_status == "pending")
        step("dohickey aggregate still pending", doh_status == "pending")
    finally:
        db.close()

    print()
    print("=== H. Re-execute already-merged source -> 409/422 ===")
    r = client.post("/admin/taxonomy/api/merge_suggestions/execute",
                    json={"workspace_id": ws_id, "attribute": ATTR,
                          "source_cluster": "widget_pro",
                          "target_cluster": "widgetpro",
                          "merge_type": "normalization_variant"})
    step("re-merge already-merged source -> 4xx",
         r.status_code in (409, 422), r.text[:80])

    print()
    print("=== I. mumzworld_v3_sample untouched (read-only check) ===")
    db = SessionLocal()
    try:
        prod_ws = db.query(Workspace).filter(
            Workspace.slug == "mumzworld_v3_sample").first()
        if prod_ws is not None:
            n_approved = db.query(A).filter(
                A.workspace_id == prod_ws.id,
                A.attribute_name == ATTR,
                A.status == "approved").count()
            n_merged = db.query(A).filter(
                A.workspace_id == prod_ws.id,
                A.attribute_name == ATTR,
                A.status == "merged").count()
            n_aav_active = db.query(AttributeAllowedValue).filter(
                AttributeAllowedValue.workspace_id == prod_ws.id,
                AttributeAllowedValue.attribute_name == ATTR,
                AttributeAllowedValue.is_active == True).count()
            print(f"  v3 stats: approved={n_approved}, merged={n_merged}, "
                  f"active_aav={n_aav_active}")

            # Read-only GET to demonstrate detector against real data.
            r = client.get("/admin/taxonomy/api/merge_suggestions",
                           params={"workspace_id": prod_ws.id, "attribute": ATTR})
            if r.status_code == 200:
                v3 = r.json()
                print(f"  v3 merge suggestions: total={v3['total']} "
                      f"by_type={v3['by_type_count']}")
                # Print top 5 high-confidence suggestions for the report.
                top = [s for s in v3["items"] if s["confidence"] == "high"][:5]
                for s in top:
                    print(f"    [{s['merge_type']:<22} {s['confidence']:<6}] "
                          f"{s['source_cluster']:<25} -> {s['target_cluster']:<25} "
                          f"executable={s['executable']}")
        else:
            print("  mumzworld_v3_sample workspace not found -- skipping")
    finally:
        db.close()

    print()
    print("=" * 60)
    fails = [r for r in results if r[0] == "FAIL"]
    print(f"TOTAL: {len(results)} checks, {len(fails)} failures")
    for f in fails:
        print(f"  FAIL  {f[1]}  {f[2]}")


if __name__ == "__main__":
    main()
