from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.recommendation import (
    MultiSlotRequest,
    MultiSlotResponse,
    RecommendationRead,
    SlotConfig,
    SlotRequest,
    SlotResponse,
)
from app.services import recommendation_service, signal_strength_service
from app.services.recommendation_service import (
    _DEFAULT_BEHAVIORAL_WEIGHT,
    _DEFAULT_DIRECT_WEIGHT,
    _DEFAULT_POPULARITY_WEIGHT,
    _DEFAULT_RELATIONSHIP_WEIGHT,
    get_algorithm_preset,
)
from app.services.workspace_service import get_workspace


def _get_signal_strength(db: Session, workspace_id: int, customer_id: str) -> float:
    """Compute signal strength once for reuse across diversity + scan depth."""
    try:
        result = signal_strength_service.compute_customer_signal_strength(
            db, workspace_id, customer_id,
        )
        return result.customer_signal_strength
    except Exception:
        return 0.0


def _resolve_max_per_group(
    slot: SlotConfig, signal_strength: float,
) -> int | None:
    """Resolve diversity_mode to a concrete max_per_group value (or None for unlimited)."""
    mode = slot.diversity_mode
    if mode == "off":
        return None
    if mode == "strict":
        return 1
    # adaptive — use customer signal strength
    if signal_strength < 0.4:
        return 1
    if signal_strength <= 0.7:
        return 2
    return None


def _resolve_max_scan_depth(signal_strength: float) -> int:
    """Limit selection scan depth based on customer signal strength."""
    if signal_strength < 0.4:
        return 100
    if signal_strength <= 0.7:
        return 50
    return 20


def _resolve_min_score_threshold(signal_strength: float) -> float:
    """Minimum recommendation_score to keep, based on customer signal strength."""
    if signal_strength < 0.4:
        return 0.2
    if signal_strength <= 0.7:
        return 0.4
    return 0.6

router = APIRouter(
    prefix="/workspaces/{workspace_id}/recommendations",
    tags=["recommendations"],
)


def _require_workspace(workspace_id: int, db: Session = Depends(get_db)) -> int:
    if not get_workspace(db, workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace_id


@router.get("/{customer_id}", response_model=list[RecommendationRead])
def get_recommendations(
    customer_id: str,
    workspace_id: int = Depends(_require_workspace),
    min_score: float | None = Query(None, ge=0.0, le=1.0),
    top_n: int = Query(10, ge=1),
    direct_weight: float = Query(_DEFAULT_DIRECT_WEIGHT, ge=0.0),
    relationship_weight: float = Query(_DEFAULT_RELATIONSHIP_WEIGHT, ge=0.0),
    popularity_weight: float = Query(_DEFAULT_POPULARITY_WEIGHT, ge=0.0),
    behavioral_weight: float = Query(_DEFAULT_BEHAVIORAL_WEIGHT, ge=0.0),
    db: Session = Depends(get_db),
):
    results, _ = recommendation_service.get_recommendations(
        db,
        workspace_id=workspace_id,
        customer_id=customer_id,
        min_score=min_score,
        top_n=top_n,
        direct_weight=direct_weight,
        relationship_weight=relationship_weight,
        popularity_weight=popularity_weight,
        behavioral_weight=behavioral_weight,
    )
    return results


def _process_slot(
    db: Session,
    workspace_id: int,
    customer_id: str,
    slot: SlotConfig,
    *,
    min_score: float | None = None,
    weight_overrides: dict[str, float | None] | None = None,
    excluded_product_ids: set[str] | None = None,
    excluded_group_ids: set[str] | None = None,
    max_per_group: int | None = None,
    max_scan_depth: int | None = None,
    min_score_threshold: float | None = None,
) -> SlotResponse:
    """Process a single slot config and return a SlotResponse."""
    preset = get_algorithm_preset(slot.algorithm)
    if preset is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown algorithm '{slot.algorithm}'. "
            f"Valid algorithms: {', '.join(sorted(recommendation_service.ALGORITHM_PRESETS))}",
        )

    ov = weight_overrides or {}
    weights = {
        "direct_weight": ov.get("direct_weight") if ov.get("direct_weight") is not None else preset["direct_weight"],
        "relationship_weight": ov.get("relationship_weight") if ov.get("relationship_weight") is not None else preset["relationship_weight"],
        "popularity_weight": ov.get("popularity_weight") if ov.get("popularity_weight") is not None else preset["popularity_weight"],
        "behavioral_weight": ov.get("behavioral_weight") if ov.get("behavioral_weight") is not None else preset["behavioral_weight"],
    }

    results, fallback_applied = recommendation_service.get_recommendations(
        db,
        workspace_id=workspace_id,
        customer_id=customer_id,
        min_score=min_score,
        top_n=slot.top_n,
        tie_break_priority=preset["tie_break_priority"],
        slot_filters=slot.filters or None,
        fallback_mode=slot.fallback_mode,
        algorithm=slot.algorithm,
        fallback_behavior=slot.fallback_behavior,
        max_per_group=max_per_group,
        max_scan_depth=max_scan_depth,
        min_score_threshold=min_score_threshold,
        excluded_product_ids=excluded_product_ids,
        excluded_group_ids=excluded_group_ids,
        **weights,
    )

    return SlotResponse(
        slot_id=slot.slot_id,
        algorithm=slot.algorithm,
        fallback_mode=slot.fallback_mode,
        fallback_applied=fallback_applied,
        results=results,
    )


@router.post("/slot", response_model=SlotResponse)
def get_slot_recommendations(
    body: SlotRequest,
    workspace_id: int = Depends(_require_workspace),
    min_score: float | None = Query(None, ge=0.0, le=1.0),
    direct_weight: float | None = Query(None, ge=0.0),
    relationship_weight: float | None = Query(None, ge=0.0),
    popularity_weight: float | None = Query(None, ge=0.0),
    behavioral_weight: float | None = Query(None, ge=0.0),
    db: Session = Depends(get_db),
):
    signal_strength = _get_signal_strength(db, workspace_id, body.customer_id)
    mpg = _resolve_max_per_group(body.slot, signal_strength)
    msd = _resolve_max_scan_depth(signal_strength)
    mst = _resolve_min_score_threshold(signal_strength)
    return _process_slot(
        db, workspace_id, body.customer_id, body.slot,
        min_score=min_score,
        weight_overrides={
            "direct_weight": direct_weight,
            "relationship_weight": relationship_weight,
            "popularity_weight": popularity_weight,
            "behavioral_weight": behavioral_weight,
        },
        max_per_group=mpg,
        max_scan_depth=msd,
        min_score_threshold=mst,
    )


@router.post("/slots", response_model=MultiSlotResponse)
def get_multi_slot_recommendations(
    body: MultiSlotRequest,
    workspace_id: int = Depends(_require_workspace),
    min_score: float | None = Query(None, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
):
    slot_responses: list[SlotResponse] = []
    returned_product_ids: set[str] = set()
    returned_group_ids: set[str] = set()

    signal_strength = _get_signal_strength(db, workspace_id, body.customer_id)

    for slot in body.slots:
        if slot.exclude_previous_slots:
            excl_pids = returned_product_ids
            excl_gids = returned_group_ids if slot.exclusion_level == "group" else None
        else:
            excl_pids = None
            excl_gids = None

        mpg = _resolve_max_per_group(slot, signal_strength)
        msd = _resolve_max_scan_depth(signal_strength)
        mst = _resolve_min_score_threshold(signal_strength)
        response = _process_slot(
            db, workspace_id, body.customer_id, slot,
            min_score=min_score,
            excluded_product_ids=excl_pids,
            excluded_group_ids=excl_gids,
            max_per_group=mpg,
            max_scan_depth=msd,
            min_score_threshold=mst,
        )

        returned_product_ids.update(r.product_id for r in response.results)
        returned_group_ids.update(
            r.group_id for r in response.results if r.group_id is not None
        )
        slot_responses.append(response)

    return MultiSlotResponse(customer_id=body.customer_id, slots=slot_responses)
