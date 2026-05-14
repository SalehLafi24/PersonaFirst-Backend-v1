"""Intent re-rank wrapper.

Sits AFTER the existing recommenders (V1 product, persona-fit customer)
and re-ranks their outputs with intent-flavored signals. NEVER modifies
the wrapped recommenders' scoring logic.

Public API:

    build_context(db, *, workspace_id, customer_id=None,
                  anchor_product_id=None, persona=None) -> IntentContext

    rerank(candidates, ctx) -> RerankResult

`candidates` is a list of objects (or dicts) with at minimum:
    .product_id  : str   external product id
    .score       : float current ranking score (will be modified)
The wrapper attaches:
    .intent_contributions : list[Contribution]
    .intent_dropped       : bool
    .score (updated)

Phase 1 includes ONE signal: RepurchaseSuppression. Future signals slot
in via the IntentSignal interface; the rerank loop is signal-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Protocol

from sqlalchemy.orm import Session

from app.models.attribute_value_relationship import AttributeValueRelationship
from app.models.customer import CustomerInteraction, INTERACTION_PURCHASE
from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.product import Product, ProductAttribute
from app.models.product_behavior_relationship import ProductBehaviorRelationship
from app.services.attribute_engine import load_manifest


# ---------------------------------------------------------------------------
# Combination caps. Configurable in one place; not per-signal yet because
# Phase 1 only ships one signal.
# ---------------------------------------------------------------------------

MAX_INTENT_BOOST_PER_SIGNAL = 0.30
MAX_INTENT_BOOST_TOTAL = 0.50


# ---------------------------------------------------------------------------
# Contribution + signal interface
# ---------------------------------------------------------------------------

CONTRIB_DROP = "drop"
CONTRIB_DEMOTE = "demote"  # multiplier <= 1.0 applied to score
CONTRIB_BOOST = "boost"    # additive; capped per signal + total


@dataclass
class Contribution:
    signal_name: str
    kind: str              # CONTRIB_DROP | CONTRIB_DEMOTE | CONTRIB_BOOST
    value: float           # 0.0 for drop; multiplier for demote; addend for boost
    reason: str            # human-readable, fed to `why`
    debug: dict[str, Any] | None = None

    def to_json(self) -> dict:
        out = {
            "signal": self.signal_name,
            "kind": self.kind,
            "reason": self.reason,
        }
        if self.kind != CONTRIB_DROP:
            out["value"] = round(self.value, 4)
        if self.debug:
            out["debug"] = self.debug
        return out


class IntentSignal(Protocol):
    name: str

    def applies(self, ctx: "IntentContext") -> bool: ...
    def evaluate(self, candidate: Any, ctx: "IntentContext") -> Contribution | None: ...


# ---------------------------------------------------------------------------
# Intent context
# ---------------------------------------------------------------------------

@dataclass
class _CandidateProductInfo:
    """Catalog metadata the signals need per candidate. Bulk-loaded once
    during build_context so signals never query the DB."""
    db_id: int
    product_id: str
    name: str | None
    group_id: str | None
    repurchase_behavior: str | None
    repurchase_window_days: int | None
    recommendation_role: str
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class IntentContext:
    workspace_id: int
    now: datetime

    # Product mode
    anchor_product_id: str | None = None
    anchor_attributes: dict[str, str] | None = None

    # Customer mode
    customer_id: str | None = None
    # External product_id -> latest occurred_at among the customer's purchase
    # interactions. Empty when no customer context.
    customer_purchase_history: dict[str, datetime] = field(default_factory=dict)
    # group_id -> latest occurred_at across the customer's purchases.
    customer_purchase_groups: dict[str, datetime] = field(default_factory=dict)
    customer_affinities: dict[str, dict[str, float]] = field(default_factory=dict)

    # Behavioral co-occurrence edges leaving the customer's purchased products.
    # target_db_id -> list[BehavioralEdge]. Populated by build_context() for
    # the BehavioralCoOccurrenceBooster signal.
    behavioral_targets: dict[int, list["BehavioralEdge"]] = field(default_factory=dict)

    # Approved attribute-value relationship edges keyed by (source_attr,
    # source_value) for fast lookup against the customer's affinities.
    # Populated by build_context() for AttributeRelationshipBooster.
    attr_relationships: dict[tuple[str, str], list["AttributeRelationshipEdge"]] = field(
        default_factory=dict
    )

    # Catalog product info, keyed by external product_id. Populated lazily
    # in rerank() for just the candidates seen.
    catalog_index: dict[str, _CandidateProductInfo] = field(default_factory=dict)

    persona: Any = None  # opaque CustomerPersona — kept generic for now


@dataclass(frozen=True)
class BehavioralEdge:
    """One row of ProductBehaviorRelationship, resolved to display fields.

    Used by BehavioralCoOccurrenceBooster: when a candidate is the target
    of an edge whose source is in the customer's history, the candidate
    gets a boost proportional to the edge strength.
    """
    source_db_id: int
    source_product_id: str
    source_name: str
    strength: float
    customer_overlap_count: int
    source_customer_count: int


@dataclass(frozen=True)
class AttributeRelationshipEdge:
    """One approved AttributeValueRelationship row, resolved.

    Used by AttributeRelationshipBooster: when a customer has affinity
    for (source_attr, source_value), candidates whose attributes match
    (target_attr, target_value) get a boost proportional to the
    relationship strength × the customer's affinity score.
    """
    source_attr: str
    source_value: str
    target_attr: str
    target_value: str
    strength: float
    confidence: float
    lift: float
    pair_count: int


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _load_purchase_history(
    db: Session, workspace_id: int, customer_id: str,
) -> tuple[dict[str, datetime], list[str]]:
    """Returns (latest_occurred_at_per_product_id, list_of_external_pids)."""
    rows = (
        db.query(CustomerInteraction)
        .filter(CustomerInteraction.workspace_id == workspace_id,
                CustomerInteraction.customer_id == customer_id,
                CustomerInteraction.interaction_type == INTERACTION_PURCHASE)
        .all()
    )
    latest: dict[str, datetime] = {}
    for r in rows:
        prev = latest.get(r.product_id)
        ts = r.occurred_at
        # Make timezone-aware (SQLite/Postgres mixed).
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts is None:
            continue
        if prev is None or ts > prev:
            latest[r.product_id] = ts
    return latest, list(latest.keys())


def _load_customer_affinities(
    db: Session, workspace_id: int, customer_id: str,
) -> dict[str, dict[str, float]]:
    rows = (
        db.query(CustomerAttributeAffinity)
        .filter(CustomerAttributeAffinity.workspace_id == workspace_id,
                CustomerAttributeAffinity.customer_id == customer_id)
        .all()
    )
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        out.setdefault(r.attribute_id, {})[r.attribute_value] = float(r.score)
    return out


def _attach_catalog_index(
    db: Session, workspace_id: int, ctx: IntentContext,
    candidate_pids: Iterable[str],
) -> None:
    """Bulk-load Product + a small set of attributes for every candidate
    product_id we'll evaluate. Also resolves group_id timestamps for any
    customer-purchased products that aren't already in the candidate list
    (so group-based suppression can fire on candidates whose siblings were
    bought)."""
    pids: set[str] = set(candidate_pids)
    pids.update(ctx.customer_purchase_history.keys())
    if not pids:
        return
    products = (
        db.query(Product)
        .filter(Product.workspace_id == workspace_id,
                Product.product_id.in_(pids))
        .all()
    )
    by_db_id = {p.id: p for p in products}
    by_pid = {p.product_id: p for p in products}

    # Load every attribute the manifest knows about so signals (including
    # AttributeRelationshipBooster) can read any candidate value, not
    # only the original product_type/age_group/gender trio.
    try:
        manifest_attrs = tuple(load_manifest().entries.keys())
    except Exception:
        manifest_attrs = ("product_type", "age_group", "gender")
    attr_rows = (
        db.query(ProductAttribute.product_id,
                 ProductAttribute.attribute_id,
                 ProductAttribute.attribute_value)
        .filter(ProductAttribute.product_id.in_(by_db_id.keys()),
                ProductAttribute.attribute_id.in_(manifest_attrs))
        .all()
    )
    attrs_by_db: dict[int, dict[str, str]] = {}
    for db_id, attr, val in attr_rows:
        attrs_by_db.setdefault(db_id, {})[attr] = val

    for p in products:
        ctx.catalog_index[p.product_id] = _CandidateProductInfo(
            db_id=p.id,
            product_id=p.product_id,
            name=p.name,
            group_id=p.group_id,
            repurchase_behavior=p.repurchase_behavior,
            repurchase_window_days=p.repurchase_window_days,
            recommendation_role=p.recommendation_role or "main",
            attributes=attrs_by_db.get(p.id, {}),
        )

    # Build group_id -> latest_occurred_at from the customer's purchase history.
    if ctx.customer_purchase_history:
        for pid, ts in ctx.customer_purchase_history.items():
            prod = by_pid.get(pid)
            if prod is None:
                continue
            key = prod.group_id or pid
            prev = ctx.customer_purchase_groups.get(key)
            if prev is None or ts > prev:
                ctx.customer_purchase_groups[key] = ts


def build_context(
    db: Session,
    *,
    workspace_id: int,
    customer_id: str | None = None,
    anchor_product_id: str | None = None,
    persona: Any = None,
) -> IntentContext:
    """Assemble an IntentContext. Bulk-loads everything signals will need
    (except the per-candidate catalog index, which is filled in rerank()
    after the candidate list is known)."""
    ctx = IntentContext(
        workspace_id=workspace_id,
        now=datetime.now(timezone.utc),
        anchor_product_id=anchor_product_id,
        customer_id=customer_id,
        persona=persona,
    )
    if customer_id:
        history, _ = _load_purchase_history(db, workspace_id, customer_id)
        ctx.customer_purchase_history = history
        ctx.customer_affinities = _load_customer_affinities(
            db, workspace_id, customer_id,
        )
        if history:
            ctx.behavioral_targets = _load_behavioral_targets(
                db, workspace_id, list(history.keys()),
            )
        if ctx.customer_affinities:
            ctx.attr_relationships = _load_attr_relationships(db, workspace_id)
    return ctx


def _load_attr_relationships(
    db: Session, workspace_id: int,
) -> dict[tuple[str, str], list[AttributeRelationshipEdge]]:
    """Load APPROVED AttributeValueRelationship rows, keyed by source
    (attribute, value) for fast lookup against customer affinities.

    `status='suggested'` rows are deliberately excluded -- the booster
    fires only on human-approved edges, preserving the review-gated
    invariant from `taxonomy_admin`.
    """
    rows = (
        db.query(AttributeValueRelationship)
        .filter(AttributeValueRelationship.workspace_id == workspace_id,
                AttributeValueRelationship.status == "approved")
        .all()
    )
    out: dict[tuple[str, str], list[AttributeRelationshipEdge]] = {}
    for r in rows:
        edge = AttributeRelationshipEdge(
            source_attr=r.source_attribute_id,
            source_value=r.source_value,
            target_attr=r.target_attribute_id,
            target_value=r.target_value,
            strength=float(r.strength or 0.0),
            confidence=float(r.confidence or 0.0),
            lift=float(r.lift or 0.0),
            pair_count=int(r.pair_count or 0),
        )
        out.setdefault((edge.source_attr, edge.source_value), []).append(edge)
    return out


def _load_behavioral_targets(
    db: Session, workspace_id: int, customer_history_pids: list[str],
) -> dict[int, list[BehavioralEdge]]:
    """Bulk-load ProductBehaviorRelationship edges leaving the customer's
    purchased products. Returns target_db_id -> list[BehavioralEdge].

    Source product names are resolved in the same pass for explanation
    text. The signal never queries the DB itself.
    """
    if not customer_history_pids:
        return {}

    # Resolve external product_ids -> Product DB rows in one query.
    src_products = (
        db.query(Product)
        .filter(Product.workspace_id == workspace_id,
                Product.product_id.in_(customer_history_pids))
        .all()
    )
    if not src_products:
        return {}
    src_db_ids = [p.id for p in src_products]
    src_meta_by_db_id: dict[int, tuple[str, str]] = {
        p.id: (p.product_id, p.name or p.product_id) for p in src_products
    }

    edges_rows = (
        db.query(ProductBehaviorRelationship)
        .filter(ProductBehaviorRelationship.workspace_id == workspace_id,
                ProductBehaviorRelationship.source_product_db_id.in_(src_db_ids))
        .all()
    )
    out: dict[int, list[BehavioralEdge]] = {}
    for row in edges_rows:
        meta = src_meta_by_db_id.get(row.source_product_db_id)
        if meta is None:
            continue
        src_pid, src_name = meta
        edge = BehavioralEdge(
            source_db_id=row.source_product_db_id,
            source_product_id=src_pid,
            source_name=src_name,
            strength=float(row.strength or 0.0),
            customer_overlap_count=int(row.customer_overlap_count or 0),
            source_customer_count=int(row.source_customer_count or 0),
        )
        out.setdefault(row.target_product_db_id, []).append(edge)
    return out


# ---------------------------------------------------------------------------
# Signal: RepurchaseSuppression
# ---------------------------------------------------------------------------

class RepurchaseSuppression:
    """Drops candidates the customer has already bought (or whose group
    they've bought) within the relevant repurchase window.

    Rules:
      - candidate.recommendation_role == 'complementary'  ->  bypass.
      - candidate.repurchase_behavior is None             ->  no decision.
      - candidate.repurchase_behavior == 'one_time'       ->  drop if
            customer ever bought any product in the same group_id (or
            this exact product_id when group_id is null).
      - candidate.repurchase_behavior == 'repurchasable'  ->  drop only
            when most recent purchase of the same group is inside the
            window_days. Outside the window, no suppression.
    """

    name = "repurchase_suppression"

    def applies(self, ctx: IntentContext) -> bool:
        return bool(ctx.customer_purchase_history)

    def evaluate(self, candidate: Any, ctx: IntentContext) -> Contribution | None:
        info = ctx.catalog_index.get(_pid_of(candidate))
        if info is None:
            return None
        if info.recommendation_role == "complementary":
            return None  # bypass
        behavior = info.repurchase_behavior
        if behavior is None:
            return None  # no rule -> no decision
        group_key = info.group_id or info.product_id
        last = ctx.customer_purchase_groups.get(group_key)
        if last is None:
            return None  # never bought this group

        if behavior == "one_time":
            return Contribution(
                signal_name=self.name,
                kind=CONTRIB_DROP, value=0.0,
                reason="One-time purchase already in customer's history.",
                debug={"group_key": group_key, "last_purchased": last.isoformat()},
            )
        if behavior == "repurchasable":
            window = info.repurchase_window_days
            if window is None:
                return None
            elapsed_days = (ctx.now - last).days
            if elapsed_days <= window:
                return Contribution(
                    signal_name=self.name,
                    kind=CONTRIB_DROP, value=0.0,
                    reason=(
                        f"Repurchasable; last bought {elapsed_days} day(s) ago, "
                        f"inside the {window}-day window."
                    ),
                    debug={
                        "group_key": group_key,
                        "elapsed_days": elapsed_days,
                        "window_days": window,
                        "last_purchased": last.isoformat(),
                    },
                )
            # outside window -- explicitly NOT suppressed; let the candidate
            # re-appear (window-aware re-suggestion).
        return None


class FallbackInteractionExclude:
    """Conservative safety net for products without any repurchase rule.

    When `Product.repurchase_behavior` is null we have no policy, so we
    fall back to the legacy "don't re-recommend something the customer
    already bought" behaviour. Drops only on exact product_id match (not
    group), and only when no other intent signal has decided.
    """

    name = "fallback_interaction_exclude"

    def applies(self, ctx: IntentContext) -> bool:
        return bool(ctx.customer_purchase_history)

    def evaluate(self, candidate, ctx: IntentContext) -> Contribution | None:
        info = ctx.catalog_index.get(_pid_of(candidate))
        if info is None:
            return None
        # Only fires when there's no repurchase policy configured.
        if info.repurchase_behavior is not None:
            return None
        if info.recommendation_role == "complementary":
            return None
        last = ctx.customer_purchase_history.get(info.product_id)
        if last is None:
            return None
        return Contribution(
            signal_name=self.name,
            kind=CONTRIB_DROP, value=0.0,
            reason="Already in your purchase history (no repurchase policy configured).",
            debug={"last_purchased": last.isoformat()},
        )


# Behavioral booster: maps an edge strength in [0, 1] into a candidate
# score boost in [0, MAX_INTENT_BOOST_PER_SIGNAL]. With multiplier 0.30
# a strength of 1.0 produces the per-signal cap (0.30); a strength of
# 0.5 produces 0.15. Values are configurable in one place.
_BEHAVIORAL_STRENGTH_MULTIPLIER = 0.30
# Edges below this support floor are treated as noise and ignored. Tunable
# upwards as the customer base grows; 1 is appropriate during bootstrap.
_BEHAVIORAL_MIN_OVERLAP = 1


class BehavioralCoOccurrenceBooster:
    """Boosts candidates that the customer's purchased products tend to
    co-occur with, using ProductBehaviorRelationship edges.

    For each candidate:
      - Look up edges where target == candidate AND source in customer's
        purchase history (preloaded into ctx.behavioral_targets).
      - Take the strongest edge (max strength).
      - Boost = strength * _BEHAVIORAL_STRENGTH_MULTIPLIER, capped per the
        rerank loop's MAX_INTENT_BOOST_PER_SIGNAL.

    The signal never modifies persona-fit or V1 scoring. It only adds a
    Contribution(kind=boost) which the rerank loop accumulates.
    """

    name = "behavioral_co_occurrence"

    def applies(self, ctx: IntentContext) -> bool:
        return bool(ctx.customer_purchase_history) and bool(ctx.behavioral_targets)

    def evaluate(self, candidate: Any, ctx: IntentContext) -> Contribution | None:
        info = ctx.catalog_index.get(_pid_of(candidate))
        if info is None:
            return None
        edges = ctx.behavioral_targets.get(info.db_id) or []
        if not edges:
            return None
        # Cross-segment guard: same rationale as in AttributeRelationship-
        # Booster. Co-occurrence edges with low overlap can fire on
        # cross-segment pairings (kids book + adult skincare from a
        # single shared customer in synthetic data). For every attribute
        # tagged `cross_segment_guard` in the manifest, require the
        # candidate's value to be present in the customer's affinity
        # distribution at >= floor.
        for guard_attr in _cross_segment_guard_attrs_from_ctx(ctx):
            cand_guard_value = (info.attributes or {}).get(guard_attr)
            persona_dist = ctx.customer_affinities.get(guard_attr) or {}
            if cand_guard_value is not None and persona_dist:
                persona_weight = float(persona_dist.get(cand_guard_value, 0.0) or 0.0)
                if persona_weight < _ATTR_REL_CROSS_SEGMENT_GUARD_FLOOR:
                    return None
        # Filter by minimum support; pick the strongest remaining edge.
        eligible = [e for e in edges
                    if e.customer_overlap_count >= _BEHAVIORAL_MIN_OVERLAP]
        if not eligible:
            return None
        best = max(eligible, key=lambda e: e.strength)
        if best.strength <= 0:
            return None
        boost_value = best.strength * _BEHAVIORAL_STRENGTH_MULTIPLIER
        # Trim source name for the reason text.
        src_label = best.source_name
        if len(src_label) > 50:
            src_label = src_label[:49] + "…"
        return Contribution(
            signal_name=self.name,
            kind=CONTRIB_BOOST,
            value=boost_value,
            reason=(
                f"Often bought together with {src_label!r} from your history "
                f"(strength={best.strength:.2f})."
            ),
            debug={
                "source_product_id": best.source_product_id,
                "strength": round(best.strength, 4),
                "customer_overlap_count": best.customer_overlap_count,
                "source_customer_count": best.source_customer_count,
                "edge_count_for_target": len(edges),
            },
        )


# Attribute-relationship booster knobs. strength * affinity_score becomes
# a per-edge contribution, summed across all firing edges, capped per the
# rerank loop's MAX_INTENT_BOOST_PER_SIGNAL.
_ATTR_REL_MULTIPLIER = 0.30
_ATTR_REL_MIN_AFFINITY = 0.10  # ignore weak customer affinities
_ATTR_REL_MIN_STRENGTH = 0.30  # ignore weak relationship edges

# Cross-segment guard. Approved attribute-relationship edges declare a
# (source_attr, source_value) -> (target_attr, target_value) pair without
# any constraint on the candidate's OTHER attributes. That allows a
# kids/learning customer's `use_case=learning` affinity to surface adult
# skincare candidates -- catalog cross-segment contamination found in
# the rubric pass on synthetic_book_heavy (adult skincare at #2). Guard:
# for every attribute tagged `cross_segment_guard` in the manifest, the
# candidate's value on that attribute must appear in the customer's
# affinity distribution at >= the floor below. Empty distribution -> no
# guard. Candidate without the attribute -> no guard. Multi-tenant safe:
# B2B workspaces don't tag age_group; consumer workspaces do.
_ATTR_REL_CROSS_SEGMENT_GUARD_FLOOR = 0.05


class AttributeRelationshipBooster:
    """Boosts candidates whose attributes are reachable from the
    customer's affinities via approved AttributeValueRelationship edges.

    For every customer affinity (attr=A, value=V, score=p) where p is
    above the floor:
      - Look up approved edges from (A, V).
      - For each edge to (target_attr=T, target_value=W), check whether
        the candidate has T=W.
      - Contribution = p * strength * multiplier.

    Contributions sum across all firing edges; the rerank loop caps the
    total at MAX_INTENT_BOOST_PER_SIGNAL.

    Why this is non-redundant with persona-fit:
      persona-fit gives a candidate credit only for matching the same
      attribute value the customer's persona is dominant on. This signal
      gives credit for matching a *different* attribute's value reachable
      via an approved cross-category relationship -- e.g., a customer
      with `use_case=feeding` affinity gets `product_type=pacifier`
      candidates boosted, even though their persona's product_type
      distribution may be dominated by something else entirely.
    """

    name = "attribute_relationship"

    def applies(self, ctx: IntentContext) -> bool:
        return bool(ctx.customer_affinities) and bool(ctx.attr_relationships)

    def evaluate(self, candidate: Any, ctx: IntentContext) -> Contribution | None:
        info = ctx.catalog_index.get(_pid_of(candidate))
        if info is None:
            return None
        cand_attrs = info.attributes
        if not cand_attrs:
            return None

        # Cross-segment guard: for every attribute tagged
        # `cross_segment_guard` in the manifest, the candidate's value
        # must appear in the customer's affinity distribution at >= floor.
        # See module-level constant for the floor.
        for guard_attr in _cross_segment_guard_attrs_from_ctx(ctx):
            cand_guard_value = cand_attrs.get(guard_attr)
            persona_dist = ctx.customer_affinities.get(guard_attr) or {}
            if cand_guard_value is not None and persona_dist:
                persona_weight = float(persona_dist.get(cand_guard_value, 0.0) or 0.0)
                if persona_weight < _ATTR_REL_CROSS_SEGMENT_GUARD_FLOOR:
                    return None

        contributions: list[tuple[float, AttributeRelationshipEdge, float]] = []
        for src_attr, value_scores in ctx.customer_affinities.items():
            for src_value, affinity_score in value_scores.items():
                if affinity_score < _ATTR_REL_MIN_AFFINITY:
                    continue
                edges = ctx.attr_relationships.get((src_attr, src_value)) or []
                for edge in edges:
                    if edge.strength < _ATTR_REL_MIN_STRENGTH:
                        continue
                    cand_value = cand_attrs.get(edge.target_attr)
                    if cand_value != edge.target_value:
                        continue
                    contribution = (
                        float(affinity_score) * float(edge.strength) * _ATTR_REL_MULTIPLIER
                    )
                    if contribution <= 0:
                        continue
                    contributions.append((contribution, edge, affinity_score))

        if not contributions:
            return None

        # Pick the strongest single edge for the chip / reason text;
        # the value reported is the SUM (capped by the rerank loop).
        contributions.sort(key=lambda x: -x[0])
        best_contrib, best_edge, best_affinity = contributions[0]
        boost_value = sum(c for c, _, _ in contributions)
        # Cap pre-emptively so the debug payload reflects the actual
        # value the rerank loop will accumulate.
        boost_value = min(boost_value, MAX_INTENT_BOOST_PER_SIGNAL)

        reason = (
            f"Pairs with your {best_edge.source_attr}={best_edge.source_value!r} "
            f"affinity ({best_affinity:.0%}) via approved relationship "
            f"to {best_edge.target_attr}={best_edge.target_value!r} "
            f"(strength={best_edge.strength:.2f})."
        )
        return Contribution(
            signal_name=self.name,
            kind=CONTRIB_BOOST,
            value=boost_value,
            reason=reason,
            debug={
                "edges_fired": len(contributions),
                "best_source": f"{best_edge.source_attr}={best_edge.source_value}",
                "best_target": f"{best_edge.target_attr}={best_edge.target_value}",
                "best_affinity": round(best_affinity, 4),
                "best_strength": round(best_edge.strength, 4),
                "summed_contribution": round(sum(c for c, _, _ in contributions), 4),
            },
        )


# --------------------------------------------------------------------------
# Saturation knobs. Tunable per-deployment; defaults are calibrated
# against synthetic data and will need adjustment from real traffic.
# --------------------------------------------------------------------------
_SATURATION_THRESHOLD = 0.50    # demote only when persona prob > this
_SATURATION_MAX_DEMOTE = 0.40   # at prob=1.0, multiplier = 1 - 0.40 = 0.60

# Distinct synthetic signal name used when the complementary-role
# bypass fires. Chip mapping uses this to render "Adds to your set"
# while saturation_demoter (the demote case) renders no chip in
# default view. Both come from the same SaturationDemoter class.
BYPASS_SIGNAL_NAME = "saturation_bypass"


class SaturationDemoter:
    """Soft demote on candidates whose attribute values are over-represented
    in the customer's persona.

    For each saturatable attribute the candidate has a value for:
      - read the customer's persona probability for that value
      - if probability > _SATURATION_THRESHOLD, apply a multiplier
            multiplier = 1 - _SATURATION_MAX_DEMOTE × (prob - threshold)
                                                    / (1 - threshold)
        which is 1.0 at the threshold and 1 - max_demote at prob=1.0.

    Multipliers compound across attributes. The rerank loop applies them
    multiplicatively after the boost stack.

    Bypass: candidates with `recommendation_role == 'complementary'` are
    exempt from the demote (a stroller-buyer should still see
    stroller_accessory). The bypass surfaces as its own Contribution
    so the UI can show "Adds to your set" when triggered.

    Multi-tenant note: which attributes are saturatable is declared in
    the manifest via `recommendation.signal_tags = ['saturatable']`,
    not hardcoded by attribute name. New clients tag their own
    attributes; the signal applies automatically.
    """

    name = "saturation_demoter"

    def applies(self, ctx: IntentContext) -> bool:
        if ctx.persona is None or not ctx.customer_purchase_history:
            return False
        # If no manifest attribute opts in, the signal has nothing to do.
        return bool(_saturatable_attrs_from_ctx(ctx))

    def evaluate(self, candidate: Any, ctx: IntentContext) -> Contribution | None:
        info = ctx.catalog_index.get(_pid_of(candidate))
        if info is None:
            return None

        # Complementary-role bypass: emit a positive contribution that
        # carries no score change but signals "this candidate is allowed
        # through saturation by design." The chip layer surfaces it as
        # 'Adds to your set'. We still compute the demote below to put
        # it in the debug payload for observability; the multiplier
        # returned to the rerank loop stays at 1.0.
        is_complementary = info.recommendation_role == "complementary"

        sat_attrs = _saturatable_attrs_from_ctx(ctx)
        if not sat_attrs:
            return None

        affinities = (ctx.persona.attribute_affinities
                      if hasattr(ctx.persona, "attribute_affinities") else {})

        compounded = 1.0
        per_attr: list[dict] = []
        any_demote = False
        for attr in sat_attrs:
            cand_value = info.attributes.get(attr)
            if not cand_value:
                continue
            aff = affinities.get(attr)
            if aff is None:
                continue
            prob = aff.distribution.get(cand_value, 0.0) if hasattr(aff, "distribution") else 0.0
            if prob <= _SATURATION_THRESHOLD:
                continue
            scale = (prob - _SATURATION_THRESHOLD) / (1.0 - _SATURATION_THRESHOLD)
            multiplier = 1.0 - (_SATURATION_MAX_DEMOTE * scale)
            multiplier = max(0.0, min(1.0, multiplier))
            compounded *= multiplier
            any_demote = True
            per_attr.append({
                "attribute": attr,
                "value": cand_value,
                "persona_probability": round(prob, 4),
                "multiplier": round(multiplier, 4),
            })

        if not any_demote:
            return None

        if is_complementary:
            # Bypass: report a positive contribution under a distinct
            # synthetic signal name so the chip layer can render
            # "Adds to your set" while keeping the score unchanged.
            top_attr = per_attr[0]["attribute"] if per_attr else "?"
            return Contribution(
                signal_name=BYPASS_SIGNAL_NAME,
                kind=CONTRIB_BOOST,  # zero-value boost = positive marker
                value=0.0,
                reason=(
                    f"Complementary role: bypasses saturation on "
                    f"{top_attr}. Adds to your set."
                ),
                debug={
                    "bypass": True,
                    "would_have_compounded": round(compounded, 4),
                    "per_attribute": per_attr,
                },
            )

        return Contribution(
            signal_name=self.name,
            kind=CONTRIB_DEMOTE,
            value=compounded,
            reason=(
                f"You already buy a lot of "
                f"{per_attr[0]['attribute']}={per_attr[0]['value']!r} "
                f"({per_attr[0]['persona_probability']:.0%}); demoted by "
                f"{(1 - compounded) * 100:.0f}%."
            ),
            debug={
                "compounded_multiplier": round(compounded, 4),
                "per_attribute": per_attr,
            },
        )


def _saturatable_attrs_from_ctx(ctx: IntentContext) -> list[str]:
    """Return the list of attribute names tagged `saturatable` in the
    manifest. Cached on the context after first call so signals don't
    re-read the manifest per candidate."""
    cached = getattr(ctx, "_saturatable_attrs_cache", None)
    if cached is not None:
        return cached
    try:
        manifest = load_manifest()
        out = [
            name for name, entry in manifest.entries.items()
            if "saturatable" in entry.recommendation.signal_tags
        ]
    except Exception:
        out = []
    try:
        object.__setattr__(ctx, "_saturatable_attrs_cache", out)
    except Exception:
        pass
    return out


def _cross_segment_guard_attrs_from_ctx(ctx: IntentContext) -> list[str]:
    """Return the list of attribute names tagged `cross_segment_guard`
    in the manifest. Cached on context per call. AttributeRelationship-
    Booster uses this list to suppress edge fires when the candidate's
    value for any guarded attribute is below floor in the customer's
    affinity distribution."""
    cached = getattr(ctx, "_cross_segment_guard_attrs_cache", None)
    if cached is not None:
        return cached
    try:
        manifest = load_manifest()
        out = [
            name for name, entry in manifest.entries.items()
            if "cross_segment_guard" in entry.recommendation.signal_tags
        ]
    except Exception:
        out = []
    try:
        object.__setattr__(ctx, "_cross_segment_guard_attrs_cache", out)
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# Signal-tag registry. Read by the manifest validator at load time.
# Each tag corresponds 1:1 to a signal class. Adding a new tag REQUIRES
# adding the consuming signal in the same change; the validator will
# refuse manifests carrying tags that aren't registered.
# --------------------------------------------------------------------------

REGISTERED_SIGNAL_TAGS: dict[str, type] = {
    "saturatable": SaturationDemoter,
    "cross_segment_guard": AttributeRelationshipBooster,
}


_REGISTERED_SIGNALS: list[IntentSignal] = [
    RepurchaseSuppression(),
    FallbackInteractionExclude(),
    BehavioralCoOccurrenceBooster(),
    AttributeRelationshipBooster(),
    SaturationDemoter(),
]


# ---------------------------------------------------------------------------
# rerank()
# ---------------------------------------------------------------------------

@dataclass
class RerankResult:
    kept: list[Any]
    dropped: list[Any]
    contributions_by_pid: dict[str, list[Contribution]]
    signals_applied: list[str]

    def to_summary(self) -> dict:
        return {
            "kept_count": len(self.kept),
            "dropped_count": len(self.dropped),
            "signals_applied": list(self.signals_applied),
            "drops_by_signal": _count_by_signal(self.dropped, self.contributions_by_pid),
        }


def _count_by_signal(
    dropped: list[Any], contribs: dict[str, list[Contribution]],
) -> dict[str, int]:
    counter: dict[str, int] = {}
    for c in dropped:
        for k in contribs.get(_pid_of(c), []):
            if k.kind == CONTRIB_DROP:
                counter[k.signal_name] = counter.get(k.signal_name, 0) + 1
    return counter


def _pid_of(candidate: Any) -> str:
    """Tolerate both dict and dataclass candidates."""
    if isinstance(candidate, dict):
        return candidate.get("product_id") or ""
    return getattr(candidate, "product_id", "") or ""


def _set_score(candidate: Any, score: float) -> None:
    if isinstance(candidate, dict):
        candidate["score"] = score
    else:
        try:
            candidate.score = score
        except AttributeError:
            pass


def _get_score(candidate: Any) -> float:
    if isinstance(candidate, dict):
        return float(candidate.get("score") or 0.0)
    return float(getattr(candidate, "score", 0.0) or 0.0)


def rerank(
    db: Session,
    candidates: list[Any],
    ctx: IntentContext,
) -> RerankResult:
    """Apply enabled intent signals to a candidate list. Returns the kept
    candidates (with score adjusted) plus a structured drop report.

    Pure read-only. No DB writes."""
    result = RerankResult(
        kept=[], dropped=[],
        contributions_by_pid={},
        signals_applied=[],
    )
    if not candidates:
        return result

    # Bulk-load catalog metadata for the candidates (and customer-bought
    # products, so group-based suppression has its lookup data).
    candidate_pids = [_pid_of(c) for c in candidates]
    _attach_catalog_index(db, ctx.workspace_id, ctx, candidate_pids)

    enabled = [s for s in _REGISTERED_SIGNALS if s.applies(ctx)]
    result.signals_applied = [s.name for s in enabled]

    for c in candidates:
        pid = _pid_of(c)
        contributions: list[Contribution] = []
        boost_total = 0.0
        demote_multiplier = 1.0
        dropped = False

        for s in enabled:
            contrib = s.evaluate(c, ctx)
            if contrib is None:
                continue
            contributions.append(contrib)
            if contrib.kind == CONTRIB_DROP:
                dropped = True
                break
            elif contrib.kind == CONTRIB_BOOST:
                boost_total += min(contrib.value, MAX_INTENT_BOOST_PER_SIGNAL)
            elif contrib.kind == CONTRIB_DEMOTE:
                demote_multiplier *= max(0.0, min(1.0, contrib.value))

        if contributions:
            result.contributions_by_pid[pid] = contributions

        # Attach to candidate (works for dict and dataclass).
        _attach(c, "intent_contributions", [k.to_json() for k in contributions])
        _attach(c, "intent_dropped", dropped)

        if dropped:
            result.dropped.append(c)
            continue

        # Apply boost + demote, capped.
        boost_total = min(boost_total, MAX_INTENT_BOOST_TOTAL)
        new_score = (_get_score(c) + boost_total) * demote_multiplier
        _set_score(c, new_score)
        result.kept.append(c)

    return result


def _attach(candidate: Any, name: str, value: Any) -> None:
    if isinstance(candidate, dict):
        candidate[name] = value
    else:
        try:
            setattr(candidate, name, value)
        except (AttributeError, Exception):
            pass
