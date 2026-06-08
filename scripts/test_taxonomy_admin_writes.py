"""End-to-end test of the new approve/reject mutation endpoints.

TestClient only — no uvicorn. Creates an isolated workspace named
'taxonomy_admin_ui_test'; does NOT touch mumzworld_v3_sample or any
other existing workspace. Idempotent: re-running wipes the test
workspace state and reseeds.
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

WS_SLUG = "taxonomy_admin_ui_test"
ATTR = "product_type"
client = TestClient(app)
results: list[tuple[str, str, str]] = []


def step(name: str, ok: bool, detail: str = ""):
    mark = "PASS" if ok else "FAIL"
    results.append((mark, name, detail))
    print(f"  [{mark}] {name}  {detail}")


def main() -> None:
    print("=== A. Route registration ===")
    paths = {(r.path, tuple(sorted(r.methods or {})))
             for r in app.routes if hasattr(r, "path")}
    expected = [
        ("/admin/taxonomy/api/aggregates/{aggregate_id}/approve", ("POST",)),
        ("/admin/taxonomy/api/aggregates/{aggregate_id}/reject", ("POST",)),
    ]
    for p, m in expected:
        step(f"route {p} {m}", (p, m) in paths)
    step("HTML page renders 200",
         client.get("/admin/taxonomy/").status_code == 200)

    print()
    print("=== Setup: isolated test workspace ===")
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
            ws = Workspace(slug=WS_SLUG, name="Taxonomy Admin UI test")
            db.add(ws); db.flush()
        ws_id = ws.id

        # 3 products → 1 ready aggregate (test_widget, count=3, conf=0.95).
        for pid in ("TEST-A1", "TEST-A2", "TEST-A3"):
            db.add(Product(workspace_id=ws_id, product_id=pid, sku=pid,
                           name=f"Test product {pid}"))
        db.flush()
        for pid in ("TEST-A1", "TEST-A2", "TEST-A3"):
            record_events_from_output(
                db, workspace_id=ws_id, product_id=pid,
                output=EnrichmentOutput(
                    attribute_name=ATTR, attribute_class="compatibility",
                    values=[],
                    proposed_values=[ProposedValue(
                        value="test_widget", confidence=0.95,
                        evidence=[f"seeded for ui-test {pid}"])],
                    warnings=[], source=EnrichmentSource.TEXT,
                ),
            )
        # 1 product → 1 not-ready aggregate (rare_singleton, count=1).
        db.add(Product(workspace_id=ws_id, product_id="TEST-B1",
                       sku="TEST-B1", name="Test product TEST-B1"))
        db.flush()
        record_events_from_output(
            db, workspace_id=ws_id, product_id="TEST-B1",
            output=EnrichmentOutput(
                attribute_name=ATTR, attribute_class="compatibility",
                values=[],
                proposed_values=[ProposedValue(
                    value="rare_singleton", confidence=0.95,
                    evidence=["seeded for ui-test TEST-B1"])],
                warnings=[], source=EnrichmentSource.TEXT,
            ),
        )
        refresh_aggregates(db, workspace_id=ws_id, attribute_name=ATTR)
        db.commit()
        ready_id = db.query(A).filter(
            A.workspace_id == ws_id, A.cluster_key == "test_widget").one().id
        nr_id = db.query(A).filter(
            A.workspace_id == ws_id, A.cluster_key == "rare_singleton").one().id
        step("seeded test workspace", True,
             f"ws_id={ws_id} ready_id={ready_id} not_ready_id={nr_id}")
    finally:
        db.close()

    print()
    print("=== B. Approve happy path ===")
    r = client.post(
        f"/admin/taxonomy/api/aggregates/{ready_id}/approve",
        params={"workspace_id": ws_id, "attribute": ATTR},
    )
    step("POST approve -> 200", r.status_code == 200, f"body={r.json()}")
    body = r.json()
    step("status flipped to approved", body.get("status") == "approved")
    step("test_widget in allowed_values_after",
         "test_widget" in (body.get("allowed_values_after") or []))
    step("review_note recorded",
         body.get("review_note") == "Approved from taxonomy admin UI")

    print()
    print("=== C. Negative-path safety ===")
    r = client.post(
        f"/admin/taxonomy/api/aggregates/{ready_id}/approve",
        params={"workspace_id": ws_id, "attribute": ATTR},
    )
    step("re-approve already-approved -> 409", r.status_code == 409, r.text[:80])

    r = client.post(
        f"/admin/taxonomy/api/aggregates/{nr_id}/approve",
        params={"workspace_id": ws_id + 9999, "attribute": ATTR},
    )
    step("workspace mismatch -> 400", r.status_code == 400, r.text[:80])

    r = client.post(
        f"/admin/taxonomy/api/aggregates/{nr_id}/approve",
        params={"workspace_id": ws_id, "attribute": "color"},
    )
    step("attribute mismatch -> 400", r.status_code == 400, r.text[:80])

    r = client.post(
        f"/admin/taxonomy/api/aggregates/{nr_id}/approve",
        params={"workspace_id": ws_id, "attribute": ATTR},
    )
    step("not-ready no-force -> 422", r.status_code == 422, r.text[:80])

    r = client.post(
        "/admin/taxonomy/api/aggregates/9999999/approve",
        params={"workspace_id": ws_id, "attribute": ATTR},
    )
    step("not-found -> 404", r.status_code == 404, r.text[:80])

    r = client.post(
        f"/admin/taxonomy/api/aggregates/{nr_id}/approve",
        params={"workspace_id": ws_id, "attribute": ATTR, "force": "true"},
    )
    step("not-ready force=true -> 200", r.status_code == 200, r.text[:80])

    print()
    print("=== D. Reject path ===")
    db = SessionLocal()
    try:
        db.add(Product(workspace_id=ws_id, product_id="TEST-C1",
                       sku="TEST-C1", name="Test product TEST-C1"))
        db.flush()
        record_events_from_output(
            db, workspace_id=ws_id, product_id="TEST-C1",
            output=EnrichmentOutput(
                attribute_name=ATTR, attribute_class="compatibility",
                values=[],
                proposed_values=[ProposedValue(
                    value="reject_me", confidence=0.95,
                    evidence=["seeded for reject test"])],
                warnings=[], source=EnrichmentSource.TEXT,
            ),
        )
        refresh_aggregates(db, workspace_id=ws_id, attribute_name=ATTR)
        db.commit()
        rid = db.query(A).filter(
            A.workspace_id == ws_id, A.cluster_key == "reject_me").one().id
    finally:
        db.close()

    r = client.post(
        f"/admin/taxonomy/api/aggregates/{rid}/reject",
        params={"workspace_id": ws_id, "attribute": ATTR,
                "merge_reason": "noise"},
    )
    step("POST reject -> 200", r.status_code == 200, f"body={r.json()}")
    body = r.json()
    step("status flipped to rejected", body.get("status") == "rejected")
    step("merge_reason recorded", body.get("merge_reason") == "noise")
    step("review_note recorded",
         body.get("review_note") == "Rejected from taxonomy admin UI")

    r = client.post(
        f"/admin/taxonomy/api/aggregates/{rid}/reject",
        params={"workspace_id": ws_id, "attribute": ATTR},
    )
    step("re-reject already-rejected -> 409",
         r.status_code == 409, r.text[:80])

    print()
    print("=== E. UI smoke ===")
    html = client.get("/admin/taxonomy/").text
    step("HTML contains actions column header",
         "<th>actions</th>" in html)
    step("HTML contains act-approve class", "act-approve" in html)
    step("HTML contains act-reject class", "act-reject" in html)
    step("HTML contains postAction handler",
         "function postAction" in html)

    print()
    print("=== F. mumzworld_v3_sample untouched ===")
    db = SessionLocal()
    try:
        prod_ws = db.query(Workspace).filter(
            Workspace.slug == "mumzworld_v3_sample").first()
        n_approved = db.query(A).filter(
            A.workspace_id == prod_ws.id,
            A.attribute_name == ATTR,
            A.status == "approved").count()
        n_rejected = db.query(A).filter(
            A.workspace_id == prod_ws.id,
            A.attribute_name == ATTR,
            A.status == "rejected").count()
        n_av = db.query(AttributeAllowedValue).filter(
            AttributeAllowedValue.workspace_id == prod_ws.id,
            AttributeAllowedValue.attribute_name == ATTR).count()
        step("v3 approved=24 (unchanged)", n_approved == 24,
             f"actual={n_approved}")
        step("v3 rejected=0 (unchanged)", n_rejected == 0,
             f"actual={n_rejected}")
        step("v3 AttributeAllowedValue=24 (unchanged)", n_av == 24,
             f"actual={n_av}")
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
