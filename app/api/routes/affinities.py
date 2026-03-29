from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.affinity_generate import AffinityGenerateResult
from app.schemas.customer_attribute_affinity import AffinityCreate, AffinityRead
from app.services import affinity_service
from app.services.workspace_service import get_workspace

router = APIRouter(
    prefix="/workspaces/{workspace_id}/affinities",
    tags=["affinities"],
)


def _require_workspace(workspace_id: int, db: Session = Depends(get_db)) -> int:
    if not get_workspace(db, workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace_id


@router.post("", response_model=list[AffinityRead], status_code=status.HTTP_201_CREATED)
def bulk_create_affinities(
    data: list[AffinityCreate],
    workspace_id: int = Depends(_require_workspace),
    db: Session = Depends(get_db),
):
    return affinity_service.bulk_create_affinities(db, workspace_id, data)


@router.post("/generate", response_model=AffinityGenerateResult)
def generate_affinities(
    workspace_id: int = Depends(_require_workspace),
    customer_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    return affinity_service.generate_affinities_from_purchases(db, workspace_id, customer_id)


@router.get("", response_model=list[AffinityRead])
def list_affinities(
    workspace_id: int = Depends(_require_workspace),
    min_score: float | None = Query(None, ge=0.0, le=1.0),
    sort: str | None = Query(None),
    db: Session = Depends(get_db),
):
    return affinity_service.list_affinities(
        db,
        workspace_id,
        min_score=min_score,
        sort_by_score_desc=(sort == "score_desc"),
    )
