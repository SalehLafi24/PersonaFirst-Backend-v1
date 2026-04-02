import math

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.customer_purchase import CustomerPurchase
from app.models.product_behavior_relationship import ProductBehaviorRelationship
from app.schemas.signal_strength import (
    SignalStrengthComponents,
    SignalStrengthRead,
)


def _min_max_normalize(value: float, min_val: float, max_val: float) -> float:
    if max_val == min_val:
        return 1.0 if value > 0 else 0.0
    return (value - min_val) / (max_val - min_val)


def _log_min_max_normalize(value: float, min_val: float, max_val: float) -> float:
    """Apply log1p transform before min-max normalization (for count-like metrics)."""
    return _min_max_normalize(math.log1p(value), math.log1p(min_val), math.log1p(max_val))


# ---------------------------------------------------------------------------
# Workspace-wide stats (for min-max normalization)
# ---------------------------------------------------------------------------

def _workspace_purchase_stats(
    db: Session, workspace_id: int,
) -> dict[str, dict[str, float]]:
    """Return per-customer purchase_count and unique_product_count, plus min/max."""
    rows = (
        db.query(
            CustomerPurchase.customer_id,
            func.count(CustomerPurchase.id).label("purchase_count"),
            func.count(func.distinct(CustomerPurchase.product_id)).label("unique_products"),
        )
        .filter(CustomerPurchase.workspace_id == workspace_id)
        .group_by(CustomerPurchase.customer_id)
        .all()
    )
    if not rows:
        return {"by_customer": {}, "min_max": {
            "purchase_count_min": 0, "purchase_count_max": 0,
            "unique_products_min": 0, "unique_products_max": 0,
        }}

    by_customer: dict[str, dict[str, int]] = {}
    purchase_counts: list[int] = []
    unique_counts: list[int] = []
    for r in rows:
        by_customer[r.customer_id] = {
            "purchase_count": r.purchase_count,
            "unique_products": r.unique_products,
        }
        purchase_counts.append(r.purchase_count)
        unique_counts.append(r.unique_products)

    return {
        "by_customer": by_customer,
        "min_max": {
            "purchase_count_min": min(purchase_counts),
            "purchase_count_max": max(purchase_counts),
            "unique_products_min": min(unique_counts),
            "unique_products_max": max(unique_counts),
        },
    }


def _workspace_affinity_stats(
    db: Session, workspace_id: int,
) -> dict[str, dict[str, float]]:
    """Return per-customer affinity_count and attribute_type_count, plus min/max."""
    rows = (
        db.query(
            CustomerAttributeAffinity.customer_id,
            func.count(CustomerAttributeAffinity.id).label("affinity_count"),
            func.count(func.distinct(CustomerAttributeAffinity.attribute_id)).label("attribute_types"),
        )
        .filter(CustomerAttributeAffinity.workspace_id == workspace_id)
        .group_by(CustomerAttributeAffinity.customer_id)
        .all()
    )
    if not rows:
        return {"by_customer": {}, "min_max": {
            "affinity_count_min": 0, "affinity_count_max": 0,
            "attribute_types_min": 0, "attribute_types_max": 0,
        }}

    by_customer: dict[str, dict[str, int]] = {}
    aff_counts: list[int] = []
    type_counts: list[int] = []
    for r in rows:
        by_customer[r.customer_id] = {
            "affinity_count": r.affinity_count,
            "attribute_types": r.attribute_types,
        }
        aff_counts.append(r.affinity_count)
        type_counts.append(r.attribute_types)

    return {
        "by_customer": by_customer,
        "min_max": {
            "affinity_count_min": min(aff_counts),
            "affinity_count_max": max(aff_counts),
            "attribute_types_min": min(type_counts),
            "attribute_types_max": max(type_counts),
        },
    }


def _workspace_behavioral_stats(
    db: Session, workspace_id: int,
) -> dict[str, dict[str, float]]:
    """
    Return per-customer edge_count and avg_edge_strength for behavioral graph,
    plus min/max.  Edges are reachable from the customer's purchased products.
    """
    # Subquery: distinct product_db_ids per customer
    cust_products = (
        db.query(
            CustomerPurchase.customer_id,
            CustomerPurchase.product_db_id,
        )
        .filter(CustomerPurchase.workspace_id == workspace_id)
        .distinct()
        .subquery()
    )

    rows = (
        db.query(
            cust_products.c.customer_id,
            func.count(ProductBehaviorRelationship.id).label("edge_count"),
            func.avg(ProductBehaviorRelationship.strength).label("avg_strength"),
        )
        .join(
            ProductBehaviorRelationship,
            (ProductBehaviorRelationship.workspace_id == workspace_id)
            & (ProductBehaviorRelationship.source_product_db_id == cust_products.c.product_db_id),
        )
        .group_by(cust_products.c.customer_id)
        .all()
    )

    if not rows:
        return {"by_customer": {}, "min_max": {
            "edge_count_min": 0, "edge_count_max": 0,
            "avg_strength_min": 0.0, "avg_strength_max": 0.0,
        }}

    by_customer: dict[str, dict[str, float]] = {}
    edge_counts: list[int] = []
    avg_strengths: list[float] = []
    for r in rows:
        by_customer[r.customer_id] = {
            "edge_count": r.edge_count,
            "avg_strength": float(r.avg_strength) if r.avg_strength else 0.0,
        }
        edge_counts.append(r.edge_count)
        avg_strengths.append(float(r.avg_strength) if r.avg_strength else 0.0)

    return {
        "by_customer": by_customer,
        "min_max": {
            "edge_count_min": min(edge_counts),
            "edge_count_max": max(edge_counts),
            "avg_strength_min": min(avg_strengths),
            "avg_strength_max": max(avg_strengths),
        },
    }


# ---------------------------------------------------------------------------
# Score computation
# ---------------------------------------------------------------------------

def _compute_from_stats(
    customer_id: str,
    purchase_stats: dict,
    affinity_stats: dict,
    behavioral_stats: dict,
) -> SignalStrengthRead:
    # -- Purchase depth (count metrics use log1p normalization) --
    cust_p = purchase_stats["by_customer"].get(customer_id)
    if cust_p is None:
        purchase_depth = 0.0
    else:
        mm = purchase_stats["min_max"]
        norm_count = _log_min_max_normalize(
            cust_p["purchase_count"], mm["purchase_count_min"], mm["purchase_count_max"],
        )
        norm_unique = _log_min_max_normalize(
            cust_p["unique_products"], mm["unique_products_min"], mm["unique_products_max"],
        )
        purchase_depth = 0.7 * norm_count + 0.3 * norm_unique

    # -- Attribute richness (count metrics use log1p normalization) --
    cust_a = affinity_stats["by_customer"].get(customer_id)
    if cust_a is None:
        attribute_richness = 0.0
    else:
        mm = affinity_stats["min_max"]
        norm_aff = _log_min_max_normalize(
            cust_a["affinity_count"], mm["affinity_count_min"], mm["affinity_count_max"],
        )
        norm_types = _log_min_max_normalize(
            cust_a["attribute_types"], mm["attribute_types_min"], mm["attribute_types_max"],
        )
        attribute_richness = 0.6 * norm_aff + 0.4 * norm_types

    # -- Behavioral graph (edge_count uses log1p; avg_strength uses raw min-max) --
    cust_b = behavioral_stats["by_customer"].get(customer_id)
    if cust_b is None:
        behavioral_graph = 0.0
    else:
        mm = behavioral_stats["min_max"]
        norm_edges = _log_min_max_normalize(
            cust_b["edge_count"], mm["edge_count_min"], mm["edge_count_max"],
        )
        norm_avg = _min_max_normalize(
            cust_b["avg_strength"], mm["avg_strength_min"], mm["avg_strength_max"],
        )
        behavioral_graph = 0.5 * norm_edges + 0.5 * norm_avg

    # -- Final --
    signal_strength = 0.5 * purchase_depth + 0.3 * attribute_richness + 0.2 * behavioral_graph
    signal_strength = max(0.0, min(1.0, signal_strength))

    return SignalStrengthRead(
        customer_id=customer_id,
        customer_signal_strength=round(signal_strength, 6),
        components=SignalStrengthComponents(
            purchase_depth=round(purchase_depth, 6),
            attribute_richness=round(attribute_richness, 6),
            behavioral_graph=round(behavioral_graph, 6),
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_customer_signal_strength(
    db: Session, workspace_id: int, customer_id: str,
) -> SignalStrengthRead:
    purchase_stats = _workspace_purchase_stats(db, workspace_id)
    affinity_stats = _workspace_affinity_stats(db, workspace_id)
    behavioral_stats = _workspace_behavioral_stats(db, workspace_id)

    return _compute_from_stats(customer_id, purchase_stats, affinity_stats, behavioral_stats)


def batch_compute_customer_signal_strength(
    db: Session, workspace_id: int,
) -> list[SignalStrengthRead]:
    purchase_stats = _workspace_purchase_stats(db, workspace_id)
    affinity_stats = _workspace_affinity_stats(db, workspace_id)
    behavioral_stats = _workspace_behavioral_stats(db, workspace_id)

    # Union of all customer_ids across all data sources
    all_customers: set[str] = set()
    all_customers.update(purchase_stats["by_customer"].keys())
    all_customers.update(affinity_stats["by_customer"].keys())
    all_customers.update(behavioral_stats["by_customer"].keys())

    return [
        _compute_from_stats(cid, purchase_stats, affinity_stats, behavioral_stats)
        for cid in sorted(all_customers)
    ]
