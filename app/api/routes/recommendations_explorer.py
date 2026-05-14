"""Admin Recommendation Explorer routes.

Routes:
    GET  /admin/explorer/                        HTML page (single-page UI)
    GET  /admin/recommendations/anchors          anchor list for the picker
    GET  /admin/recommendations/curated_anchors  curated demo anchors
    GET  /admin/recommendations/explore/{id}     V1 result enriched with
                                                 display fields + chips

The explore route DELEGATES to recommendation_v1_service.recommend() and
adds display data (name, attributes, chips, strength). It never modifies
the underlying scoring or rec shape -- the V1 endpoint at
/api/recommendations/products/{id} continues to work unchanged.

Curated anchors are read from seed_data/explorer_curated_anchors.json.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.product import Product, ProductAttribute
from app.models.workspace import Workspace
from app.services.explanation_transformer import (
    humanize_tier,
    transform,
)
from app.services.recommendation_v1_service import recommend


router = APIRouter(tags=["admin-explorer"])


_REPO_ROOT = Path(__file__).resolve().parents[3]
_CURATED_PATH = _REPO_ROOT / "seed_data" / "explorer_curated_anchors.json"
_STATIC_DIR = _REPO_ROOT / "static" / "explorer"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DISPLAY_ATTRS = ("product_type", "age_group", "gender", "brand")


def _load_attrs_for_products(
    db: Session, workspace_id: int, product_db_ids: list[int],
) -> dict[int, dict[str, str]]:
    if not product_db_ids:
        return {}
    rows = (
        db.query(ProductAttribute.product_id,
                 ProductAttribute.attribute_id,
                 ProductAttribute.attribute_value)
        .join(Product, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id,
                ProductAttribute.product_id.in_(product_db_ids),
                ProductAttribute.attribute_id.in_(_DISPLAY_ATTRS))
        .all()
    )
    out: dict[int, dict[str, str]] = {}
    for pid, attr, val in rows:
        out.setdefault(pid, {})[attr] = val
    return out


def _coverage_flags(attrs: dict[str, str]) -> list[str]:
    """Returns the list of *missing* core display attributes for a product.
    Used by the UI's coverage indicator on the anchor card."""
    return [a for a in ("product_type", "age_group", "gender") if not attrs.get(a)]


def _is_anomalous(attrs: dict[str, str], product_name: str | None) -> tuple[bool, list[str]]:
    """Heuristic: flag anchors whose enriched attributes look suspicious.
    Used for the 'attributes may be incorrect' warning in weak-case UI.

    These are conservative checks — true positives, low precision is fine,
    we just want to surface review candidates, not block recommendations.
    """
    notes: list[str] = []
    name = (product_name or "").lower()
    gender = (attrs.get("gender") or "").lower()
    age = (attrs.get("age_group") or "").lower()

    # Female-tagged but contains no female-leaning tokens AND name has
    # masculine-leaning tokens.
    if gender == "female" and any(t in name for t in (" boy", "boys", " men ", "mens")):
        notes.append("gender=female but name suggests male")
    if gender == "male" and any(t in name for t in (" girl", "girls", " women", "womens")):
        notes.append("gender=male but name suggests female")
    # Adult-tagged toys / age contradictions
    if age in ("adult",) and any(t in name for t in ("baby", "infant", "toddler")):
        notes.append("age=adult but name suggests baby/infant/toddler")
    return (bool(notes), notes)


# ---------------------------------------------------------------------------
# GET /admin/recommendations/anchors  — picker list
# ---------------------------------------------------------------------------

@router.get("/admin/recommendations/anchors")
def list_anchors(
    workspace_id: int = Query(...),
    search: str | None = Query(None, description="case-insensitive substring of name/sku"),
    product_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Filterable anchor list for the Explorer's picker.

    has_strong_recs is a cheap heuristic: True iff the product has both
    product_type AND age_group attributes (the engine's tier_1 cohort
    requires both, so without them the user is guaranteed tier_2 or
    weaker). Real "strong" classification belongs to the validation
    harness; this is a hint, not a contract.
    """
    q = db.query(Product).filter(Product.workspace_id == workspace_id)
    if search:
        like = f"%{search.strip()}%"
        q = q.filter(or_(Product.name.ilike(like), Product.sku.ilike(like)))

    if product_type:
        # Restrict to products having product_type=<value>.
        q = q.join(
            ProductAttribute, ProductAttribute.product_id == Product.id,
        ).filter(
            ProductAttribute.attribute_id == "product_type",
            ProductAttribute.attribute_value == product_type,
        )

    products = q.order_by(Product.name.asc()).limit(limit).all()
    if not products:
        return []

    attrs_by_id = _load_attrs_for_products(
        db, workspace_id, [p.id for p in products]
    )

    out: list[dict] = []
    for p in products:
        a = attrs_by_id.get(p.id, {})
        has_strong = bool(a.get("product_type")) and bool(a.get("age_group"))
        out.append({
            "product_id": p.product_id,
            "name": p.name,
            "product_type": a.get("product_type"),
            "age_group": a.get("age_group"),
            "gender": a.get("gender"),
            "has_strong_recs": has_strong,
        })
    return out


# ---------------------------------------------------------------------------
# GET /admin/recommendations/curated_anchors
# ---------------------------------------------------------------------------

@router.get("/admin/recommendations/curated_anchors")
def list_curated_anchors(
    workspace_id: int = Query(...),
    db: Session = Depends(get_db),
):
    """Pre-selected demo-safe anchors per workspace.

    Read from seed_data/explorer_curated_anchors.json. The file maps
    workspace slug -> a list of anchor product_ids (curated by the
    validation harness). Missing products are silently filtered out.
    """
    if not _CURATED_PATH.exists():
        return []
    raw = json.loads(_CURATED_PATH.read_text(encoding="utf-8"))
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if ws is None:
        return []
    ids = list((raw.get(ws.slug) or {}).get("anchors") or [])
    if not ids:
        return []
    products = db.query(Product).filter(
        Product.workspace_id == workspace_id,
        Product.product_id.in_(ids),
    ).all()
    attrs_by_id = _load_attrs_for_products(
        db, workspace_id, [p.id for p in products]
    )
    by_pid = {p.product_id: p for p in products}
    out: list[dict] = []
    for pid in ids:
        p = by_pid.get(pid)
        if p is None:
            continue
        a = attrs_by_id.get(p.id, {})
        out.append({
            "product_id": p.product_id,
            "name": p.name,
            "product_type": a.get("product_type"),
            "age_group": a.get("age_group"),
            "gender": a.get("gender"),
            "has_strong_recs": True,
        })
    return out


# ---------------------------------------------------------------------------
# GET /admin/recommendations/explore/{product_id}
# ---------------------------------------------------------------------------

@router.get("/admin/recommendations/explore/{product_id}")
def explore_product(
    product_id: str,
    workspace_id: int = Query(...),
    top_n: int = Query(5, ge=1, le=20),
    debug: bool = Query(False),
    db: Session = Depends(get_db),
):
    """V1 result enriched with display fields and explanation chips.

    Internally calls recommendation_v1_service.recommend() — does NOT
    duplicate or modify scoring. Adds:
      - anchor: name, attributes, coverage_missing, anomaly notes
      - recommendations[i]: name, attributes, chips, sentence,
                            strength_dots, has_warning, warning_reasons
      - tier_label_human (for demo-mode display)

    Raw fields (score, raw why array, tier name, cohort_size_observed)
    are always returned so the frontend can render the debug panel
    when the user opts in. The frontend hides them unless debug toggle
    is on.
    """
    resp = recommend(
        db=db, workspace_id=workspace_id,
        anchor_product_id=product_id, top_n=top_n, customer_id=None,
    )

    # Anchor enrichment.
    anchor = db.query(Product).filter(
        Product.workspace_id == workspace_id,
        Product.product_id == product_id,
    ).first()
    anchor_attrs: dict[str, str] = {}
    coverage_missing: list[str] = ["product_type", "age_group", "gender"]
    anomaly = (False, [])
    anchor_payload: dict | None = None
    if anchor is not None:
        anchor_attrs = _load_attrs_for_products(
            db, workspace_id, [anchor.id]
        ).get(anchor.id, {})
        coverage_missing = _coverage_flags(anchor_attrs)
        anomaly = _is_anomalous(anchor_attrs, anchor.name)
        anchor_payload = {
            "product_id": anchor.product_id,
            "name": anchor.name,
            "image_url": None,  # Product model has no image column today
            "attributes": {k: anchor_attrs.get(k) for k in _DISPLAY_ATTRS},
            "coverage_missing": coverage_missing,
            "is_anomalous": anomaly[0],
            "anomaly_notes": anomaly[1],
        }

    # Recommendation enrichment.
    rec_pids = [r["product_id"] for r in resp.recommendations]
    rec_products = []
    if rec_pids:
        rec_products = db.query(Product).filter(
            Product.workspace_id == workspace_id,
            Product.product_id.in_(rec_pids),
        ).all()
    rec_by_pid = {p.product_id: p for p in rec_products}
    rec_attrs = _load_attrs_for_products(
        db, workspace_id, [p.id for p in rec_products]
    )

    enriched_recs: list[dict] = []
    for r in resp.recommendations:
        p = rec_by_pid.get(r["product_id"])
        a = rec_attrs.get(p.id, {}) if p is not None else {}
        t = transform(
            why=r.get("why") or [],
            score=float(r.get("score") or 0.0),
            tier=resp.tier,
        )
        item = {
            "product_id": r["product_id"],
            "name": p.name if p is not None else r["product_id"],
            "image_url": None,
            "attributes": {k: a.get(k) for k in _DISPLAY_ATTRS},
            "explanation": t.to_json(include_debug=debug),
            # Always include raw fields; frontend gates by debug toggle.
            "raw": {
                "score": float(r.get("score") or 0.0),
                "why": r.get("why") or [],
            },
        }
        enriched_recs.append(item)

    return {
        "anchor": anchor_payload,
        "tier": resp.tier,
        "tier_label_human": humanize_tier(resp.tier),
        "tier_label_raw": resp.tier,
        "cohort_size_observed": resp.cohort_size_observed,
        "escalated_from": resp.escalated_from,
        "request_id": resp.request_id,
        "recommendations": enriched_recs,
    }


# ---------------------------------------------------------------------------
# GET /admin/explorer/  — single-page UI
# ---------------------------------------------------------------------------

@router.get("/admin/explorer/", response_class=HTMLResponse)
def explorer_html():
    """Serve the Explorer single-page HTML.

    Static assets (JS modules, CSS) are mounted at /admin/explorer/static
    via app.mount in main.py.
    """
    html_path = _STATIC_DIR / "explorer.html"
    if not html_path.exists():
        return HTMLResponse(
            "<h1>Recommendation Explorer</h1>"
            f"<p>Static assets not found at {html_path}.</p>",
            status_code=500,
        )
    return HTMLResponse(html_path.read_text(encoding="utf-8"))
