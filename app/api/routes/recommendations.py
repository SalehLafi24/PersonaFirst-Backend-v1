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
from app.services import recommendation_service
from app.services.recommendation_service import (
    _DEFAULT_BEHAVIORAL_WEIGHT,
    _DEFAULT_DIRECT_WEIGHT,
    _DEFAULT_POPULARITY_WEIGHT,
    _DEFAULT_RELATIONSHIP_WEIGHT,
    get_algorithm_preset,
)
from app.services.workspace_service import get_workspace

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
        diversity_enabled=slot.diversity_enabled,
        excluded_product_ids=excluded_product_ids,
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
    return _process_slot(
        db, workspace_id, body.customer_id, body.slot,
        min_score=min_score,
        weight_overrides={
            "direct_weight": direct_weight,
            "relationship_weight": relationship_weight,
            "popularity_weight": popularity_weight,
            "behavioral_weight": behavioral_weight,
        },
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

    for slot in body.slots:
        exclude_ids = returned_product_ids if slot.exclude_previous_slots else None
        response = _process_slot(
            db, workspace_id, body.customer_id, slot,
            min_score=min_score,
            excluded_product_ids=exclude_ids,
        )

        returned_product_ids.update(r.product_id for r in response.results)
        slot_responses.append(response)

    return MultiSlotResponse(customer_id=body.customer_id, slots=slot_responses)
