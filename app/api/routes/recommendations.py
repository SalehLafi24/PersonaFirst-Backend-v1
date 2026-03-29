from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.recommendation import RecommendationRead
from app.services import recommendation_service
from app.services.recommendation_service import (
    _DEFAULT_BEHAVIORAL_WEIGHT,
    _DEFAULT_DIRECT_WEIGHT,
    _DEFAULT_POPULARITY_WEIGHT,
    _DEFAULT_RELATIONSHIP_WEIGHT,
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
    return recommendation_service.get_recommendations(
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
