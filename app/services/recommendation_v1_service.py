"""V1 recommendation engine.

Four layers:
    1. select_cohort        attribute-key-driven candidate selection (Tier 1/2)
    2. filter_eligible      hard filters (self, age guard, dedup safety)
    3. score_pair           weighted similarity with structured `why`
    4. finalise             stable ordering, brand caps, near-dup dedup

Public entry point: `recommend(...)`.

Design notes:
    - The cohort key is a configured ordered list of attribute names. The
      engine never references `product_type` or `age_group` literally
      except through that list (and a small set of safety constants tied
      to age semantics, which are intentional). Adding `gender` or any
      future attribute is a config edit.
    - Score is a pure function with a `context` parameter (None / unused
      in V1). Personalization slots in by reading `context` for customer
      attributes and adding score components -- no rewrite required.
    - `Product` has no brand/price/stock columns today; the brand and
      price contributions therefore evaluate to 0 (not penalties). The
      service still references them so future schema additions slot in
      without code rework.
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.product import Product, ProductAttribute


# ---------------------------------------------------------------------------
# Configuration -- the cohort key. Anything else that today reads
# `product_type` / `age_group` literals does so *only* for age-safety
# semantics that are inherently age-specific.
# ---------------------------------------------------------------------------

DEFAULT_COHORT_ATTRIBUTES: list[str] = ["product_type", "age_group"]

# Age-safety guard. These constants ARE age-specific by design. Other
# attribute names are not.
_AGE_GROUP = "age_group"
_RESTRICTED_FOR_YOUNG = frozenset({"teen", "adult"})
_YOUNG_AGES = frozenset({"infant", "toddler"})

# Adjacent age-band pairs (symmetric). Anything not in this set and not
# equal is "non-adjacent".
_ADJACENT_AGE_PAIRS: frozenset[frozenset[str]] = frozenset({
    frozenset({"infant", "toddler"}),
    frozenset({"toddler", "kids"}),
    frozenset({"kids",   "teen"}),
})

# Books get a higher name-overlap weight (within-pool variance is the
# known weakness; titles and series share token signatures). The
# product_type literal is intentional here.
_BOOK_PT = "book"


# ---------------------------------------------------------------------------
# Telemetry. A dedicated logger; callers attach handlers (file, JSONL,
# observability sink) without coupling. MVP-acceptable per spec.
# ---------------------------------------------------------------------------

_telemetry_logger = logging.getLogger("personafirst.recommendations.v1")


def _log_telemetry(*, request_id: str, timestamp: str, workspace_id: int,
                   anchor_product_id: str, customer_id: str | None,
                   tier: str, escalated_from: str | None,
                   cohort_attributes_used: list[str], cohort_size_observed: int,
                   recommendations: list[dict]) -> None:
    """One log line per (request, candidate). Fields are flat for easy
    parsing into tabular telemetry."""
    for rec in recommendations:
        _telemetry_logger.info(
            "recommendation_served",
            extra={
                "request_id": request_id,
                "timestamp": timestamp,
                "workspace_id": workspace_id,
                "anchor_product_id": anchor_product_id,
                "candidate_product_id": rec["product_id"],
                "customer_id": customer_id,
                "tier": tier,
                "escalated_from": escalated_from,
                "score": rec["score"],
                "why": rec["why"],
                "cohort_attributes_used": cohort_attributes_used,
                "cohort_size_observed": cohort_size_observed,
            },
        )


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoreComponent:
    component: str
    contribution: float

    def to_json(self) -> dict:
        return {"component": self.component,
                "contribution": round(self.contribution, 4)}


@dataclass
class ScoredCandidate:
    product: Product
    candidate_attrs: dict[str, str]
    score: float
    why: list[ScoreComponent] = field(default_factory=list)


@dataclass
class RecommendationResponse:
    request_id: str
    timestamp: str
    anchor_product_id: str
    tier: str  # "tier_1" | "tier_2" | "partial" | "empty"
    cohort_attributes: list[str]
    cohort_size_observed: int
    escalated_from: str | None
    recommendations: list[dict]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "from", "into", "this", "that", "are",
    "was", "but", "not", "you", "your", "all", "any", "has", "had",
    "have", "use", "off", "out", "via", "per", "etc", "set", "kit", "pack",
})


def _normalize_brand(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.strip().lower())


def _name_tokens(name: str | None) -> set[str]:
    if not name:
        return set()
    return {
        t for t in _TOKEN_RE.findall(name.lower())
        if len(t) >= 3 and t not in _STOPWORDS
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _stable_hash(anchor_id: int, candidate_id: int) -> int:
    return int(
        hashlib.sha256(f"{anchor_id}|{candidate_id}".encode("utf-8")).hexdigest()[:12],
        16,
    )


def _name_signature(name: str | None) -> tuple[str, ...]:
    """Top 3 sorted non-stopword tokens, used as a near-dup key."""
    return tuple(sorted(_name_tokens(name))[:3])


def _get_brand(product: Product, attrs: dict[str, str]) -> str | None:
    """Read the canonical brand for a product from its loaded
    ProductAttribute map (attribute_id='brand'). Returns None when the
    product has no brand attribute. Does not fall back to raw metadata."""
    return attrs.get("brand")


def _get_gender(product: Product, attrs: dict[str, str]) -> str | None:
    """Read the canonical gender for a product from its loaded
    ProductAttribute map (attribute_id='gender'). Returns one of
    {'male', 'female', 'unisex'} or None if absent."""
    return attrs.get("gender")


def _get_price(product: Product) -> float | None:
    """Price source. Today: not stored on Product; falls through to None.
    See _get_brand for the future-extension story."""
    raw = getattr(product, "price", None)
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _age_relation(a: str | None, b: str | None) -> str:
    """Return one of 'same' | 'adjacent' | 'non_adjacent' | 'unknown'."""
    if not a or not b:
        return "unknown"
    if a == b:
        return "same"
    if frozenset({a, b}) in _ADJACENT_AGE_PAIRS:
        return "adjacent"
    return "non_adjacent"


# ---------------------------------------------------------------------------
# Layer 1: cohort selector
# ---------------------------------------------------------------------------

def _load_attrs_for_products(
    db: Session, workspace_id: int, attr_names: list[str],
    product_db_ids: list[int],
) -> dict[int, dict[str, str]]:
    """Bulk-load attribute values for many products in one query."""
    if not product_db_ids or not attr_names:
        return {}
    rows = (db.query(ProductAttribute.product_id,
                     ProductAttribute.attribute_id,
                     ProductAttribute.attribute_value)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == workspace_id,
                    ProductAttribute.attribute_id.in_(attr_names),
                    ProductAttribute.product_id.in_(product_db_ids))
            .all())
    out: dict[int, dict[str, str]] = {}
    for pid, attr, val in rows:
        out.setdefault(pid, {})[attr] = val
    return out


def _candidates_matching_attrs(
    db: Session, workspace_id: int, anchor_attrs: dict[str, str],
    cohort_attributes: list[str], depth: int,
) -> list[Product]:
    """Return all products in the workspace whose first `depth`
    cohort_attributes match the anchor's values for those same attrs.
    """
    keys = cohort_attributes[:depth]
    if not keys:
        return []
    # All keys must have a non-empty anchor value; otherwise the cohort
    # is empty by definition.
    for k in keys:
        if not anchor_attrs.get(k):
            return []

    matching: set[int] | None = None
    for k in keys:
        v = anchor_attrs[k]
        pids = {
            pa.product_id for pa in (
                db.query(ProductAttribute)
                .join(Product, ProductAttribute.product_id == Product.id)
                .filter(Product.workspace_id == workspace_id,
                        ProductAttribute.attribute_id == k,
                        ProductAttribute.attribute_value == v)
                .all()
            )
        }
        matching = pids if matching is None else (matching & pids)
        if not matching:
            return []
    if not matching:
        return []
    return (db.query(Product)
            .filter(Product.workspace_id == workspace_id,
                    Product.id.in_(matching))
            .all())


def select_cohort(
    *, db: Session, workspace_id: int, anchor: Product,
    anchor_attrs: dict[str, str], cohort_attributes: list[str],
    min_pool_size: int = 3,
) -> tuple[str, list[Product], int, str | None]:
    """Pick the candidate set + tier label.

    Returns (tier, candidates, cohort_size_observed, escalated_from).
        tier in {"tier_1", "tier_2", "partial", "empty"}
        cohort_size_observed counts the candidate pool BEFORE filtering
            (excluding the anchor itself, included in the count below).
    """
    full_depth = len(cohort_attributes)

    cands_t1: list[Product] = []
    if full_depth >= 1:
        cands_t1 = _candidates_matching_attrs(
            db, workspace_id, anchor_attrs, cohort_attributes, depth=full_depth,
        )
    # Exclude the anchor from the pool size we report.
    pool_t1 = [c for c in cands_t1 if c.id != anchor.id]
    if len(pool_t1) >= min_pool_size:
        return ("tier_1", pool_t1, len(pool_t1), None)

    # Fall to Tier 2: relax to the first cohort attribute only.
    cands_t2 = _candidates_matching_attrs(
        db, workspace_id, anchor_attrs, cohort_attributes, depth=1,
    )
    pool_t2 = [c for c in cands_t2 if c.id != anchor.id]
    if len(pool_t2) >= min_pool_size:
        return ("tier_2", pool_t2, len(pool_t2), "tier_1")
    if pool_t2:
        return ("partial", pool_t2, len(pool_t2), "tier_1")
    return ("empty", [], 0, "tier_1")


# ---------------------------------------------------------------------------
# Layer 2: eligibility filter
# ---------------------------------------------------------------------------

def filter_eligible(
    *, anchor: Product, anchor_attrs: dict[str, str],
    candidates: list[Product], candidate_attrs: dict[int, dict[str, str]],
) -> list[Product]:
    """Apply hard filters between cohort and scoring.

    Filters today:
      - drop the anchor itself (defensive; cohort already excludes it)
      - drop duplicate product DB ids (defensive)
      - hard age-band guard: anchor age in {infant, toddler} cannot match
        candidate age in {teen, adult}
      - skip stock filtering (no stock column on Product today)
    """
    anchor_age = anchor_attrs.get(_AGE_GROUP)
    seen_ids: set[int] = set()
    out: list[Product] = []
    for c in candidates:
        if c.id == anchor.id:
            continue
        if c.id in seen_ids:
            continue
        seen_ids.add(c.id)
        c_age = candidate_attrs.get(c.id, {}).get(_AGE_GROUP)
        if anchor_age in _YOUNG_AGES and c_age in _RESTRICTED_FOR_YOUNG:
            continue
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# Layer 3: similarity scorer
# ---------------------------------------------------------------------------

# Weights live near the function that uses them so they're easy to tune.
_W_BRAND = 0.40
_W_NAME_DEFAULT = 0.30
_W_NAME_BOOK = 0.40
_W_PRICE = 0.20
_PEN_AGE_ADJACENT = -0.15
_PEN_AGE_NON_ADJACENT = -0.40
# Gender ranking weights -- smaller than brand by design; gender is a
# soft preference, not a filter. Opposite-gender candidates remain in
# the candidate set; they are merely ranked lower.
_W_GENDER_SAME = 0.15
_W_GENDER_UNISEX = 0.05
_PEN_GENDER_OPPOSITE = -0.10


def score_pair(
    anchor_product: Product,
    candidate_product: Product,
    *,
    anchor_attrs: dict[str, str],
    candidate_attrs: dict[str, str],
    tier: str,
    context: dict | None = None,  # reserved for personalization; unused in V1
) -> ScoredCandidate:
    """Pure function: weighted similarity, structured `why`. No DB access.
    Personalization will read `context` and append components -- no rewrite.
    """
    components: list[ScoreComponent] = []
    score = 0.0

    # 1. Brand match -- read from ProductAttribute via the loaded attrs map.
    a_brand = _normalize_brand(_get_brand(anchor_product, anchor_attrs))
    c_brand = _normalize_brand(_get_brand(candidate_product, candidate_attrs))
    if a_brand and c_brand and a_brand == c_brand:
        components.append(ScoreComponent("same_brand", _W_BRAND))
        score += _W_BRAND

    # 1b. Gender ranking -- soft preference, not a filter. Opposite-gender
    # candidates remain in the pool; they are merely ranked lower. Only
    # one of the three components fires for any given pair.
    a_gender = _get_gender(anchor_product, anchor_attrs)
    c_gender = _get_gender(candidate_product, candidate_attrs)
    if a_gender and c_gender:
        if a_gender == c_gender:
            components.append(ScoreComponent("same_gender", _W_GENDER_SAME))
            score += _W_GENDER_SAME
        elif a_gender == "unisex" or c_gender == "unisex":
            components.append(ScoreComponent("unisex_compatible", _W_GENDER_UNISEX))
            score += _W_GENDER_UNISEX
        else:
            components.append(
                ScoreComponent("opposite_gender_penalty", _PEN_GENDER_OPPOSITE)
            )
            score += _PEN_GENDER_OPPOSITE

    # 2. Name token overlap.
    a_tokens = _name_tokens(anchor_product.name)
    c_tokens = _name_tokens(candidate_product.name)
    j = _jaccard(a_tokens, c_tokens)
    if j > 0:
        is_book = anchor_attrs.get("product_type") == _BOOK_PT
        weight = _W_NAME_BOOK if is_book else _W_NAME_DEFAULT
        contrib = weight * j
        components.append(ScoreComponent("name_overlap", contrib))
        score += contrib

    # 3. Symmetric price proximity -- 0 today (no price column); slot reserved.
    a_p = _get_price(anchor_product)
    c_p = _get_price(candidate_product)
    if a_p and c_p and a_p > 0 and c_p > 0:
        prox = min(a_p, c_p) / max(a_p, c_p)
        contrib = _W_PRICE * prox
        components.append(ScoreComponent("price_proximity", contrib))
        score += contrib

    # 4. Age penalty -- only at Tier 2 (Tier 1 already enforces same age).
    if tier == "tier_2":
        rel = _age_relation(anchor_attrs.get(_AGE_GROUP),
                            candidate_attrs.get(_AGE_GROUP))
        if rel == "adjacent":
            components.append(ScoreComponent("age_adjacent_penalty", _PEN_AGE_ADJACENT))
            score += _PEN_AGE_ADJACENT
        elif rel == "non_adjacent":
            components.append(ScoreComponent("age_non_adjacent_penalty", _PEN_AGE_NON_ADJACENT))
            score += _PEN_AGE_NON_ADJACENT
        # rel in {"same", "unknown"} -> no penalty

    # 5. Personalization slot. `context` is reserved; no V1 contributions.
    # Future implementations read context (customer attributes, brand
    # affinity, age preference) and append components here. The contract
    # is intentionally locked in now.

    return ScoredCandidate(
        product=candidate_product,
        candidate_attrs=candidate_attrs,
        score=score,
        why=components,
    )


# ---------------------------------------------------------------------------
# Layer 4: finaliser
# ---------------------------------------------------------------------------

def finalise(
    scored: list[ScoredCandidate],
    *,
    anchor: Product,
    anchor_attrs: dict[str, str],
    top_n: int = 5,
) -> list[ScoredCandidate]:
    """Apply ordering, dedup, brand cap. No score mutation."""
    # Stable ordering: score desc, then deterministic hash tie-break.
    scored.sort(key=lambda s: (-s.score, _stable_hash(anchor.id, s.product.id)))

    is_book = anchor_attrs.get("product_type") == _BOOK_PT
    max_per_brand = 3 if is_book else 2

    seen_dedup: set[tuple[str, tuple[str, ...]]] = set()
    brand_counts: dict[str, int] = {}
    out: list[ScoredCandidate] = []
    for sc in scored:
        brand_norm = _normalize_brand(_get_brand(sc.product, sc.candidate_attrs))
        sig = _name_signature(sc.product.name)
        dedup_key = (brand_norm, sig)
        if sig and dedup_key in seen_dedup:
            continue
        if sig:
            seen_dedup.add(dedup_key)
        if brand_norm:
            n = brand_counts.get(brand_norm, 0)
            if n >= max_per_brand:
                continue
            brand_counts[brand_norm] = n + 1
        out.append(sc)
        if len(out) >= top_n:
            break
    return out


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def recommend(
    *,
    db: Session,
    workspace_id: int,
    anchor_product_id: str,
    top_n: int = 5,
    customer_id: str | None = None,
    cohort_attributes: list[str] | None = None,
) -> RecommendationResponse:
    """Top-level entry. Returns a structured response; never raises for
    'not found' -- callers receive `tier='empty'` with empty results."""
    cohort_attributes = list(cohort_attributes or DEFAULT_COHORT_ATTRIBUTES)
    request_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    anchor = (
        db.query(Product).filter(
            Product.workspace_id == workspace_id,
            Product.product_id == anchor_product_id,
        ).first()
    )
    if anchor is None:
        return RecommendationResponse(
            request_id=request_id, timestamp=timestamp,
            anchor_product_id=anchor_product_id,
            tier="empty", cohort_attributes=cohort_attributes,
            cohort_size_observed=0, escalated_from=None,
            recommendations=[],
        )

    # Anchor's attribute values (cohort_attributes plus age_group for
    # safety guards plus brand and gender for the scoring signals).
    needed_attrs = list(set(cohort_attributes) | {_AGE_GROUP, "brand", "gender"})
    anchor_attrs_map = _load_attrs_for_products(
        db, workspace_id, needed_attrs, [anchor.id]
    ).get(anchor.id, {})

    # Layer 1: cohort.
    tier, cohort_pool, cohort_size_observed, escalated_from = select_cohort(
        db=db, workspace_id=workspace_id, anchor=anchor,
        anchor_attrs=anchor_attrs_map, cohort_attributes=cohort_attributes,
    )
    if tier == "empty":
        resp = RecommendationResponse(
            request_id=request_id, timestamp=timestamp,
            anchor_product_id=anchor_product_id,
            tier="empty", cohort_attributes=cohort_attributes,
            cohort_size_observed=0, escalated_from=escalated_from,
            recommendations=[],
        )
        _log_telemetry(
            request_id=request_id, timestamp=timestamp,
            workspace_id=workspace_id, anchor_product_id=anchor_product_id,
            customer_id=customer_id, tier="empty",
            escalated_from=escalated_from,
            cohort_attributes_used=cohort_attributes,
            cohort_size_observed=0, recommendations=[],
        )
        return resp

    # Bulk-load candidate attribute values.
    candidate_attrs_map = _load_attrs_for_products(
        db, workspace_id, needed_attrs, [c.id for c in cohort_pool]
    )

    # Layer 2: eligibility.
    eligible = filter_eligible(
        anchor=anchor, anchor_attrs=anchor_attrs_map,
        candidates=cohort_pool, candidate_attrs=candidate_attrs_map,
    )
    # If the safety filter shrunk the pool below 3, we still serve --
    # mark as 'partial' if tier was tier_2 and now < 3, otherwise keep
    # the tier label. Tier 1 is always strict equality, so safety filter
    # should rarely change its size.
    if not eligible:
        resp = RecommendationResponse(
            request_id=request_id, timestamp=timestamp,
            anchor_product_id=anchor_product_id,
            tier="empty", cohort_attributes=cohort_attributes,
            cohort_size_observed=cohort_size_observed,
            escalated_from=escalated_from,
            recommendations=[],
        )
        _log_telemetry(
            request_id=request_id, timestamp=timestamp,
            workspace_id=workspace_id, anchor_product_id=anchor_product_id,
            customer_id=customer_id, tier="empty",
            escalated_from=escalated_from,
            cohort_attributes_used=cohort_attributes,
            cohort_size_observed=cohort_size_observed,
            recommendations=[],
        )
        return resp

    # Layer 3: score.
    scored: list[ScoredCandidate] = []
    for cand in eligible:
        scored.append(score_pair(
            anchor_product=anchor, candidate_product=cand,
            anchor_attrs=anchor_attrs_map,
            candidate_attrs=candidate_attrs_map.get(cand.id, {}),
            tier=tier, context=None,
        ))

    # Layer 4: finalise.
    final = finalise(
        scored, anchor=anchor, anchor_attrs=anchor_attrs_map, top_n=top_n,
    )

    # Build response payload. Cohort-derived "why" entries (same_X) appear
    # first, with contribution=0 for transparency -- they're implied by
    # the cohort tier, not the score.
    recs: list[dict] = []
    for sc in final:
        why: list[dict] = []
        # Same-attribute markers from the cohort.
        for attr_name in cohort_attributes:
            anchor_val = anchor_attrs_map.get(attr_name)
            cand_val = sc.candidate_attrs.get(attr_name)
            if anchor_val and cand_val and anchor_val == cand_val:
                why.append({"component": f"same_{attr_name}",
                            "contribution": 0.0})
        # Scoring components.
        for c in sc.why:
            why.append(c.to_json())
        recs.append({
            "product_id": sc.product.product_id,
            "score": round(sc.score, 4),
            "why": why,
        })

    # If cohort returned tier_2/partial but eligibility shrank below 3,
    # downgrade label to "partial" so callers know the response is thin.
    final_tier = tier
    if tier == "tier_2" and len(recs) < 3:
        final_tier = "partial"

    response = RecommendationResponse(
        request_id=request_id, timestamp=timestamp,
        anchor_product_id=anchor_product_id,
        tier=final_tier, cohort_attributes=cohort_attributes,
        cohort_size_observed=cohort_size_observed,
        escalated_from=escalated_from,
        recommendations=recs,
    )
    _log_telemetry(
        request_id=request_id, timestamp=timestamp,
        workspace_id=workspace_id, anchor_product_id=anchor_product_id,
        customer_id=customer_id, tier=final_tier,
        escalated_from=escalated_from,
        cohort_attributes_used=cohort_attributes,
        cohort_size_observed=cohort_size_observed,
        recommendations=recs,
    )
    return response
