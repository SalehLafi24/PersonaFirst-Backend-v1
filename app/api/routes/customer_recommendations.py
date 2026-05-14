"""Customer recommendation routes.

Public:
    GET /api/recommendations/v1/customers/{customer_id}
        Pure JSON contract per the spec. Calls customer_recommendation_service
        and returns the response as-is. No display enrichment, no chips.

Admin:
    GET /admin/recommendations/customers
        Lightweight customer listing for the Explorer picker.

    GET /admin/recommendations/customers/{customer_id}/explore
        Explorer-friendly response: same data plus per-rec explanation chips
        and the customer's interaction list, so the UI can render without
        further round-trips. Reuses the same explanation_transformer chip
        vocabulary as product mode.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.customer import Customer, CustomerInteraction
from app.models.product import Product, ProductAttribute
from app.services.customer_recommendation_service import (
    recommend_for_customer,
)
from app.services.explanation_transformer import Chip
from app.services.persona_extraction_service import (
    PERSONA_ATTRIBUTES,
    extract_persona,
    list_customers,
)


public_router = APIRouter(prefix="/api/recommendations/v1", tags=["recommendations"])
admin_router = APIRouter(tags=["admin-explorer"])


# ---------------------------------------------------------------------------
# Public endpoint -- the V1 customer rec contract
# ---------------------------------------------------------------------------

@public_router.get("/customers/{customer_id}")
def recommend_for_customer_route(
    customer_id: str,
    workspace_id: int = Query(...),
    top_n: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    resp = recommend_for_customer(
        db, workspace_id=workspace_id,
        customer_id=customer_id, top_n=top_n,
    )
    return resp.to_json()


# ---------------------------------------------------------------------------
# Admin: customer listing for the picker
# ---------------------------------------------------------------------------

@admin_router.get("/admin/recommendations/customers")
def admin_list_customers(
    workspace_id: int = Query(...),
    db: Session = Depends(get_db),
):
    return list_customers(db, workspace_id=workspace_id)


# ---------------------------------------------------------------------------
# Admin: enriched explore for Customer Mode
# ---------------------------------------------------------------------------

# Map matched-signal contribution into the same chip vocabulary used by
# product mode, so the UI components stay reusable.
_SIGNAL_CHIP_KIND: dict[str, str] = {
    "product_type": "same_type",
    "age_group":    "same_age",
    "gender":       "same_gender",
    "use_case":     "same_use_case",
}
_SIGNAL_CHIP_LABEL: dict[str, str] = {
    "product_type": "Type Affinity",
    "age_group":    "Age Affinity",
    "gender":       "Gender Affinity",
    "use_case":     "Use Case Affinity",
}

# Chip vocabulary for intent-layer contributions. Today only one boost
# signal is registered (behavioral co-occurrence); future signals
# (saturation demoter, attribute-relationship booster, anchor booster)
# extend this map.
_INTENT_CHIP: dict[str, dict[str, str]] = {
    "behavioral_co_occurrence": {
        "kind": "bought_together",
        "label": "Bought Together",
        "tone": "positive",
    },
    "attribute_relationship": {
        "kind": "pairs_with",
        "label": "Pairs With",
        "tone": "positive",
    },
    "saturation_bypass": {
        "kind": "complementary_role",
        "label": "Adds to Your Set",
        "tone": "positive",
    },
    # SaturationDemoter (kind=demote) is intentionally not surfaced as a
    # chip in the default view. Demotes are visible in the debug panel
    # via the raw intent_contributions array.
}


def _chips_for_rec(
    matched_signals: list[dict],
    intent_contributions: list[dict] | None = None,
) -> list[dict]:
    """Build display chips from persona matched signals + intent contributions.

    Persona-fit chips come first (preserving prior order); intent-derived
    chips append. Only positive-kind intent contributions become chips
    (drops/demotes are reflected elsewhere -- intent_dropped, score).
    """
    chips: list[dict] = []
    for m in matched_signals:
        if m["persona_probability"] <= 0 or not m["candidate_value"]:
            continue
        attr = m["attribute_name"]
        chips.append({
            "kind": _SIGNAL_CHIP_KIND.get(attr, attr),
            "label": _SIGNAL_CHIP_LABEL.get(attr, attr),
            "tone": "positive" if m["contribution"] >= 0.10 else "neutral",
        })
    for c in (intent_contributions or []):
        if c.get("kind") != "boost":
            continue
        meta = _INTENT_CHIP.get(c.get("signal") or "")
        if meta is None:
            continue
        chips.append({**meta})
    return chips


def _intent_sentence_addendum(intent_contributions: list[dict] | None) -> str:
    """Human-readable add-on built from positive intent contributions.

    The persona-fit explanation is emitted by the customer rec service;
    we append intent reasons after it so the explorer's Why panel reads
    coherently without needing to render two sentences separately.
    """
    if not intent_contributions:
        return ""
    parts: list[str] = []
    for c in intent_contributions:
        if c.get("kind") != "boost":
            continue
        reason = c.get("reason") or ""
        if reason:
            parts.append(reason)
    if not parts:
        return ""
    return "  " + " ".join(parts)


def _strength_dots(score: float) -> int:
    """Score lives in [0, 1] (persona_fit damped by confidence in [0,1]).
    Calibrate so empty=0, weak<0.05=1, modest<0.15=2, decent<0.30=3,
    strong<0.50=4, very strong>=0.50=5."""
    if score <= 0:           return 0
    if score < 0.05:         return 1
    if score < 0.15:         return 2
    if score < 0.30:         return 3
    if score < 0.50:         return 4
    return 5


@admin_router.get("/admin/recommendations/customers/{customer_id}/explore")
def admin_explore_customer(
    customer_id: str,
    workspace_id: int = Query(...),
    top_n: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    resp = recommend_for_customer(
        db, workspace_id=workspace_id,
        customer_id=customer_id, top_n=top_n,
    )
    raw = resp.to_json()

    # Build interaction list (with attributes) for the persona card.
    interactions = (
        db.query(CustomerInteraction)
        .filter(CustomerInteraction.workspace_id == workspace_id,
                CustomerInteraction.customer_id == customer_id)
        .order_by(CustomerInteraction.occurred_at.asc())
        .all()
    )
    pids = list({i.product_id for i in interactions})
    products = []
    if pids:
        products = (
            db.query(Product)
            .filter(Product.workspace_id == workspace_id,
                    Product.product_id.in_(pids))
            .all()
        )
    by_pid = {p.product_id: p for p in products}
    attrs_by_dbid: dict[int, dict[str, str]] = {}
    if products:
        for pid, attr, val in (
            db.query(ProductAttribute.product_id,
                     ProductAttribute.attribute_id,
                     ProductAttribute.attribute_value)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == workspace_id,
                    ProductAttribute.attribute_id.in_(PERSONA_ATTRIBUTES),
                    ProductAttribute.product_id.in_([p.id for p in products]))
            .all()
        ):
            attrs_by_dbid.setdefault(pid, {})[attr] = val

    interaction_payload = []
    for i in interactions:
        p = by_pid.get(i.product_id)
        a = attrs_by_dbid.get(p.id, {}) if p is not None else {}
        interaction_payload.append({
            "product_id": i.product_id,
            "name": p.name if p is not None else i.product_id,
            "attributes": {k: a.get(k) for k in PERSONA_ATTRIBUTES},
            "interaction_type": i.interaction_type,
            "occurred_at": i.occurred_at.isoformat() if i.occurred_at else None,
        })

    # Re-shape recommendations into the explore-route contract so the
    # frontend can reuse RecommendationCard with no special-casing.
    enriched_recs = []
    for r in raw["recommendations"]:
        intent_contribs = r.get("intent_contributions") or []
        chips = _chips_for_rec(r["matched_signals"], intent_contribs)
        sentence = (r.get("explanation") or "") + _intent_sentence_addendum(intent_contribs)
        enriched_recs.append({
            "product_id": r["product_id"],
            "name": r["name"],
            "image_url": None,
            "attributes": r["attributes"],
            "explanation": {
                "chips": chips,
                "sentence": sentence,
                "strength_dots": _strength_dots(r["score"]),
                "has_warning": False,
                "warning_reasons": [],
            },
            "raw": {
                "score": r["score"],
                "persona_fit": r["persona_fit"],
                "matched_signals": r["matched_signals"],
                "intent_contributions": intent_contribs,
            },
        })

    return {
        "customer_id": customer_id,
        "persona": raw["persona"],
        "interactions": interaction_payload,
        "recommendations": enriched_recs,
        "excluded_count": raw["excluded_count"],
        "candidates_considered": raw["candidates_considered"],
        "intent_summary": raw.get("intent_summary"),
        "intent_dropped": raw.get("intent_dropped") or [],
        "computed_at": raw["computed_at"],
    }
