"""Attribute gold-set sampler (Phase 8).

Four-layer deterministic sampling that produces a list of products to be
hand-labeled. Output schema matches the gold-set format documented in
the Phase 8 scope; the labels are filled in by humans afterwards.

Layers (in order, deduped at the end):

    1. Stratified by product_type
       For each product_type with >= 1 product, take the first N (config:
       products_per_type) by hash(product_id || seed) ordering. Removes
       Product.id-based import bias while staying deterministic.

    2. Rare-value top-up
       For each (attribute, value) present in the workspace, ensure the
       sample contains >= rare_value_floor products with that value.
       Tops up by adding the first hash-ordered product with that value
       not already in the sample. Skips values with fewer products in
       the catalog than the floor (can't fix).

    3. Hard cases (split 6/6 by default)
       3a. Low-confidence: the latest ProposedAttributeValueEvent on any
           persona attribute has confidence < HARD_LOW_CONF_THRESHOLD.
       3b. Missing-attributes: at least 2 of (age_group, gender, use_case)
           are missing on the product.
       Both halves hash-ordered, take first N/2 each.

    3.5. Untyped products
       Products without ANY product_type ProductAttribute row. The
       largest blind spot in v3 is the 449-product untyped tail; this
       layer guarantees representation so coverage-expansion work has
       something to evaluate against.

Boundary: this module reads from the DB but never writes. The CLI
(scripts/sample_attribute_gold.py) handles file output.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import ProposedAttributeValueEvent


# Persona attributes the gold set captures. Kept aligned with the
# manifest's persona_relevant set; passed in by the caller so sampling
# stays decoupled from manifest specifics.
DEFAULT_LABELED_ATTRIBUTES: tuple[str, ...] = (
    "product_type", "age_group", "gender", "use_case",
)

# Threshold for "low confidence" hard-case selection. Confidence is the
# strongest signal of "system might be wrong"; values just above the
# proposal floor (0.70) are the borderline cases we want labeled.
HARD_LOW_CONF_THRESHOLD: float = 0.85


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SamplingConfig:
    products_per_type: int = 2
    rare_value_floor: int = 3
    hard_cases_count: int = 12
    untyped_products_count: int = 8
    seed: int = 42

    def __post_init__(self) -> None:
        if self.products_per_type < 1:
            raise ValueError(f"products_per_type={self.products_per_type} must be >= 1")
        if self.rare_value_floor < 1:
            raise ValueError(f"rare_value_floor={self.rare_value_floor} must be >= 1")
        if self.hard_cases_count < 0:
            raise ValueError(f"hard_cases_count={self.hard_cases_count} must be >= 0")
        if self.untyped_products_count < 0:
            raise ValueError(f"untyped_products_count={self.untyped_products_count} must be >= 0")


@dataclass(frozen=True)
class SampledProduct:
    product_id: str
    name: str
    selection_reason: str  # which layer + why
    current_system_values: dict[str, str | None]   # per labeled attribute
    current_confidences: dict[str, float]           # per labeled attribute (when known)


@dataclass
class SamplingResult:
    products: list[SampledProduct]
    config: SamplingConfig
    labeled_attributes: tuple[str, ...]
    layer_counts: dict[str, int]                    # per-layer additions before dedup
    value_distribution: dict[str, dict[str, int]]   # per-attribute value counts in final sample
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_order_key(product_id: str, seed: int) -> str:
    """Deterministic hash-based ordering key. Same product_id+seed always
    produces the same key, but the keys aren't correlated with import
    order. SHA-256 hex prefix is sufficient resolution for sample sizes
    far smaller than the catalog."""
    return hashlib.sha256(
        f"{product_id}|{seed}".encode("utf-8")
    ).hexdigest()


def _attrs_by_db_id(
    db: Session, workspace_id: int, labeled_attributes: tuple[str, ...],
) -> dict[int, dict[str, str]]:
    """Per-product attribute snapshot for the labeled attributes."""
    rows = (
        db.query(ProductAttribute.product_id,
                 ProductAttribute.attribute_id,
                 ProductAttribute.attribute_value)
        .join(Product, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id,
                ProductAttribute.attribute_id.in_(labeled_attributes))
        .all()
    )
    out: dict[int, dict[str, str]] = {}
    for db_id, attr, val in rows:
        out.setdefault(db_id, {})[attr] = val
    return out


def _latest_confidences(
    db: Session, workspace_id: int, product_ids: Iterable[str],
    labeled_attributes: tuple[str, ...],
) -> dict[str, dict[str, float]]:
    """Per-product latest-event confidence per attribute, keyed by external
    product_id. Only the highest-confidence event per (product, attribute)
    is reported -- the same value backfill picks for ProductAttribute."""
    pids = list(set(product_ids))
    if not pids:
        return {}
    rows = (
        db.query(ProposedAttributeValueEvent.product_id,
                 ProposedAttributeValueEvent.attribute_name,
                 func.max(ProposedAttributeValueEvent.confidence))
        .filter(ProposedAttributeValueEvent.workspace_id == workspace_id,
                ProposedAttributeValueEvent.product_id.in_(pids),
                ProposedAttributeValueEvent.attribute_name.in_(labeled_attributes))
        .group_by(ProposedAttributeValueEvent.product_id,
                  ProposedAttributeValueEvent.attribute_name)
        .all()
    )
    out: dict[str, dict[str, float]] = {}
    for pid, attr, conf in rows:
        if conf is None:
            continue
        out.setdefault(pid, {})[attr] = float(conf)
    return out


def _sort_by_hash(
    products: list[Product], seed: int,
) -> list[Product]:
    return sorted(products, key=lambda p: _hash_order_key(p.product_id, seed))


def _make_sampled(
    p: Product, reason: str,
    attrs_by_db_id: dict[int, dict[str, str]],
    confs_by_pid: dict[str, dict[str, float]],
    labeled_attributes: tuple[str, ...],
) -> SampledProduct:
    attrs = attrs_by_db_id.get(p.id, {})
    return SampledProduct(
        product_id=p.product_id,
        name=p.name or p.product_id,
        selection_reason=reason,
        current_system_values={a: attrs.get(a) for a in labeled_attributes},
        current_confidences={
            a: c for a, c in (confs_by_pid.get(p.product_id) or {}).items()
            if a in labeled_attributes
        },
    )


# ---------------------------------------------------------------------------
# Layer implementations
# ---------------------------------------------------------------------------

def _layer_1_stratified_by_product_type(
    db: Session, workspace_id: int, config: SamplingConfig,
) -> tuple[list[Product], list[str]]:
    """Layer 1: per product_type, take config.products_per_type by hash order.

    Returns (selected_products, reasons) where reasons[i] describes how
    products[i] was selected.
    """
    pt_rows = (
        db.query(Product, ProductAttribute.attribute_value)
        .join(ProductAttribute, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id,
                ProductAttribute.attribute_id == "product_type")
        .all()
    )
    by_type: dict[str, list[Product]] = defaultdict(list)
    seen: set[int] = set()
    for prod, ptype in pt_rows:
        if prod.id in seen:
            continue
        seen.add(prod.id)
        by_type[ptype].append(prod)

    selected: list[Product] = []
    reasons: list[str] = []
    for ptype in sorted(by_type):
        ordered = _sort_by_hash(by_type[ptype], config.seed)
        for rank, p in enumerate(ordered[: config.products_per_type], start=1):
            selected.append(p)
            reasons.append(f"layer_1: product_type={ptype} (rank {rank})")
    return selected, reasons


def _layer_2_rare_value_topup(
    db: Session, workspace_id: int, config: SamplingConfig,
    already_selected_ids: set[int], labeled_attributes: tuple[str, ...],
) -> tuple[list[Product], list[str]]:
    """Layer 2: for each (attribute, value) under the floor in the current
    sample, add a hash-ordered product with that value.

    `product_type` is deliberately excluded -- Layer 1 stratifies by
    product_type with `products_per_type` as the explicit count. Topping
    up product_type in Layer 2 would re-apply that logic at a different
    threshold and inflate the sample. Layer 2 covers age_group, gender,
    use_case, and any other non-stratified attributes only.
    """
    # Per-product attribute snapshot, scoped to the workspace, excluding
    # the stratification axis (product_type).
    rare_attrs = tuple(a for a in labeled_attributes if a != "product_type")
    if not rare_attrs:
        return [], []
    attr_rows = (
        db.query(Product, ProductAttribute.attribute_id,
                 ProductAttribute.attribute_value)
        .join(ProductAttribute, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id,
                ProductAttribute.attribute_id.in_(rare_attrs))
        .all()
    )
    # (attr, value) -> list[Product] across the catalog (dedup products).
    catalog_by_value: dict[tuple[str, str], list[Product]] = defaultdict(list)
    seen_per_value: dict[tuple[str, str], set[int]] = defaultdict(set)
    for prod, attr, value in attr_rows:
        key = (attr, value)
        if prod.id in seen_per_value[key]:
            continue
        seen_per_value[key].add(prod.id)
        catalog_by_value[key].append(prod)

    # Count per-(attr, value) coverage in the already-selected set.
    sample_value_counts: dict[tuple[str, str], int] = defaultdict(int)
    for prod, attr, value in attr_rows:
        if prod.id in already_selected_ids:
            sample_value_counts[(attr, value)] += 1

    selected: list[Product] = []
    reasons: list[str] = []
    used_ids: set[int] = set(already_selected_ids)
    for (attr, value), in_sample in sorted(sample_value_counts.items()):
        # nothing — included so loop covers all keys; replaced below
        pass
    # Iterate over every (attr, value) in catalog deterministically.
    for key in sorted(catalog_by_value.keys()):
        attr, value = key
        catalog_count = len(catalog_by_value[key])
        in_sample = sample_value_counts.get(key, 0)
        if catalog_count < config.rare_value_floor:
            # Cannot meet the floor; not the layer's fault. Skip silently.
            continue
        if in_sample >= config.rare_value_floor:
            continue
        needed = config.rare_value_floor - in_sample
        ordered = _sort_by_hash(catalog_by_value[key], config.seed)
        for p in ordered:
            if needed <= 0:
                break
            if p.id in used_ids:
                continue
            selected.append(p)
            reasons.append(
                f"layer_2: rare_value_topup {attr}={value!r} "
                f"(in-sample {in_sample} < floor {config.rare_value_floor})"
            )
            used_ids.add(p.id)
            needed -= 1
    return selected, reasons


def _layer_3_hard_cases(
    db: Session, workspace_id: int, config: SamplingConfig,
    already_selected_ids: set[int], labeled_attributes: tuple[str, ...],
) -> tuple[list[Product], list[str], list[str]]:
    """Layer 3: hard cases, split 50/50 between low-confidence and
    missing-attribute products."""
    half = config.hard_cases_count // 2
    other_half = config.hard_cases_count - half
    selected: list[Product] = []
    reasons: list[str] = []
    warnings: list[str] = []
    used_ids: set[int] = set(already_selected_ids)

    # 3a — low confidence: products with at least one labeled-attribute
    # event whose max confidence is below the threshold.
    low_conf_pids_rows = (
        db.query(ProposedAttributeValueEvent.product_id)
        .filter(ProposedAttributeValueEvent.workspace_id == workspace_id,
                ProposedAttributeValueEvent.attribute_name.in_(labeled_attributes),
                ProposedAttributeValueEvent.confidence < HARD_LOW_CONF_THRESHOLD)
        .distinct()
        .all()
    )
    low_conf_pids = {r[0] for r in low_conf_pids_rows}
    if low_conf_pids:
        candidates = (
            db.query(Product)
            .filter(Product.workspace_id == workspace_id,
                    Product.product_id.in_(low_conf_pids))
            .all()
        )
        ordered = _sort_by_hash(candidates, config.seed)
        added = 0
        for p in ordered:
            if added >= half:
                break
            if p.id in used_ids:
                continue
            selected.append(p)
            reasons.append(
                f"layer_3a: low_confidence (at least one event < "
                f"{HARD_LOW_CONF_THRESHOLD})"
            )
            used_ids.add(p.id)
            added += 1
        if added < half:
            warnings.append(
                f"layer_3a: requested {half} low-confidence hard cases; "
                f"only {added} available in workspace"
            )

    # 3b — missing attributes: products missing >= 2 of (age_group, gender,
    # use_case). product_type is excluded from this set because untyped
    # products are handled by layer 3.5 separately.
    miss_targets: tuple[str, ...] = tuple(
        a for a in labeled_attributes if a != "product_type"
    )
    if miss_targets:
        # Per-product count of populated targets.
        rows = (
            db.query(Product.id, ProductAttribute.attribute_id)
            .outerjoin(
                ProductAttribute,
                (ProductAttribute.product_id == Product.id)
                & (ProductAttribute.attribute_id.in_(miss_targets)),
            )
            .filter(Product.workspace_id == workspace_id)
            .all()
        )
        populated_count: dict[int, int] = defaultdict(int)
        for db_id, attr in rows:
            if attr is not None:
                populated_count[db_id] += 1
        # Missing >= 2 of N targets -> populated <= len(miss_targets) - 2.
        missing_threshold = len(miss_targets) - 2
        candidate_ids = {
            db_id for db_id, c in populated_count.items() if c <= missing_threshold
        }
        # Also include products with NO matched rows at all (db_id present
        # in `rows` with attr=None and not in populated_count).
        for db_id, attr in rows:
            if attr is None and db_id not in populated_count:
                candidate_ids.add(db_id)
        if candidate_ids:
            candidates = (
                db.query(Product)
                .filter(Product.workspace_id == workspace_id,
                        Product.id.in_(candidate_ids))
                .all()
            )
            ordered = _sort_by_hash(candidates, config.seed)
            added = 0
            for p in ordered:
                if added >= other_half:
                    break
                if p.id in used_ids:
                    continue
                selected.append(p)
                reasons.append(
                    f"layer_3b: missing_attributes (>=2 of "
                    f"{list(miss_targets)} unset)"
                )
                used_ids.add(p.id)
                added += 1
            if added < other_half:
                warnings.append(
                    f"layer_3b: requested {other_half} missing-attribute "
                    f"hard cases; only {added} available in workspace"
                )
    return selected, reasons, warnings


def _layer_3_5_untyped(
    db: Session, workspace_id: int, config: SamplingConfig,
    already_selected_ids: set[int],
) -> tuple[list[Product], list[str], list[str]]:
    """Layer 3.5: products without any product_type ProductAttribute row."""
    selected: list[Product] = []
    reasons: list[str] = []
    warnings: list[str] = []
    if config.untyped_products_count == 0:
        return selected, reasons, warnings

    pt_rows = (
        db.query(ProductAttribute.product_id)
        .join(Product, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id,
                ProductAttribute.attribute_id == "product_type")
        .distinct()
        .all()
    )
    typed_db_ids = {r[0] for r in pt_rows}

    untyped_products = (
        db.query(Product)
        .filter(Product.workspace_id == workspace_id,
                ~Product.id.in_(typed_db_ids) if typed_db_ids else Product.id.is_(Product.id))
        .all()
    )
    if not untyped_products:
        warnings.append(
            f"layer_3_5: requested {config.untyped_products_count} untyped "
            f"products; workspace has 0 untyped products"
        )
        return selected, reasons, warnings

    ordered = _sort_by_hash(untyped_products, config.seed)
    added = 0
    used_ids = set(already_selected_ids)
    for p in ordered:
        if added >= config.untyped_products_count:
            break
        if p.id in used_ids:
            continue
        selected.append(p)
        reasons.append("layer_3_5: untyped (no product_type tagged)")
        used_ids.add(p.id)
        added += 1
    if added < config.untyped_products_count:
        warnings.append(
            f"layer_3_5: requested {config.untyped_products_count}; "
            f"only {added} available"
        )
    return selected, reasons, warnings


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def sample_attribute_gold(
    db: Session,
    *,
    workspace_id: int,
    labeled_attributes: tuple[str, ...] = DEFAULT_LABELED_ATTRIBUTES,
    config: SamplingConfig | None = None,
) -> SamplingResult:
    """Run the four-layer sampler. Pure read-only.

    Returns a SamplingResult: ordered list of SampledProduct with
    selection_reason, current_system_values, and current_confidences
    pre-filled. Caller is responsible for serialisation."""
    cfg = config or SamplingConfig()
    selected_products: list[Product] = []
    reasons: list[str] = []
    warnings: list[str] = []
    layer_counts: dict[str, int] = {}

    # Layer 1
    p1, r1 = _layer_1_stratified_by_product_type(db, workspace_id, cfg)
    layer_counts["layer_1_stratified"] = len(p1)
    selected_ids = set()
    for p, r in zip(p1, r1):
        if p.id in selected_ids:
            continue
        selected_ids.add(p.id)
        selected_products.append(p)
        reasons.append(r)

    # Layer 2 — rare-value top-up
    p2, r2 = _layer_2_rare_value_topup(
        db, workspace_id, cfg, selected_ids, labeled_attributes,
    )
    layer_counts["layer_2_rare_value"] = len(p2)
    for p, r in zip(p2, r2):
        if p.id in selected_ids:
            continue
        selected_ids.add(p.id)
        selected_products.append(p)
        reasons.append(r)

    # Layer 3 — hard cases
    p3, r3, w3 = _layer_3_hard_cases(
        db, workspace_id, cfg, selected_ids, labeled_attributes,
    )
    layer_counts["layer_3_hard_cases"] = len(p3)
    warnings.extend(w3)
    for p, r in zip(p3, r3):
        if p.id in selected_ids:
            continue
        selected_ids.add(p.id)
        selected_products.append(p)
        reasons.append(r)

    # Layer 3.5 — untyped
    p35, r35, w35 = _layer_3_5_untyped(
        db, workspace_id, cfg, selected_ids,
    )
    layer_counts["layer_3_5_untyped"] = len(p35)
    warnings.extend(w35)
    for p, r in zip(p35, r35):
        if p.id in selected_ids:
            continue
        selected_ids.add(p.id)
        selected_products.append(p)
        reasons.append(r)

    # Pre-fetch attribute snapshot + confidences for the selected products.
    attrs_by_db_id = _attrs_by_db_id(db, workspace_id, labeled_attributes)
    confs_by_pid = _latest_confidences(
        db, workspace_id,
        [p.product_id for p in selected_products],
        labeled_attributes,
    )
    sampled = [
        _make_sampled(
            p, reasons[i], attrs_by_db_id, confs_by_pid, labeled_attributes,
        )
        for i, p in enumerate(selected_products)
    ]

    # Compute value distribution in the final sample.
    value_distribution: dict[str, dict[str, int]] = {
        a: defaultdict(int) for a in labeled_attributes
    }
    missing_distribution: dict[str, int] = defaultdict(int)
    for sp in sampled:
        for a in labeled_attributes:
            v = sp.current_system_values.get(a)
            if v is None:
                missing_distribution[a] += 1
            else:
                value_distribution[a][v] += 1
    # Promote "(missing)" entries explicitly.
    for a in labeled_attributes:
        if missing_distribution[a]:
            value_distribution[a]["(missing)"] = missing_distribution[a]

    return SamplingResult(
        products=sampled,
        config=cfg,
        labeled_attributes=labeled_attributes,
        layer_counts=layer_counts,
        value_distribution={
            a: dict(value_distribution[a]) for a in labeled_attributes
        },
        warnings=warnings,
    )
