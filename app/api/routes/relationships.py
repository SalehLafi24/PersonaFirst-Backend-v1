from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.attribute_value_relationship import RelationshipRead
from app.services import relationship_engine_service, relationship_service
from app.services.workspace_service import get_workspace

router = APIRouter(
    prefix="/workspaces/{workspace_id}/relationships",
    tags=["relationships"],
)


def _require_workspace(workspace_id: int, db: Session = Depends(get_db)) -> int:
    if not get_workspace(db, workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace_id


@router.get("", response_model=list[RelationshipRead])
def list_relationships(
    workspace_id: int = Depends(_require_workspace),
    status: str | None = Query(None),
    db: Session = Depends(get_db),
):
    return relationship_service.list_relationships(db, workspace_id, status)


@router.post("/generate")
def generate_relationships(
    workspace_id: int = Depends(_require_workspace),
    min_confidence: float = Query(0.1, ge=0.0, le=1.0),
    min_lift: float = Query(1.0, ge=0.0),
    min_pair_count: int = Query(2, ge=1),
    db: Session = Depends(get_db),
):
    created = relationship_engine_service.run_relationship_engine(
        db, workspace_id, min_confidence, min_lift, min_pair_count
    )
    return {"created": created}


@router.post("/{relationship_id}/approve", response_model=RelationshipRead)
def approve_relationship(
    relationship_id: int,
    workspace_id: int = Depends(_require_workspace),
    db: Session = Depends(get_db),
):
    return relationship_service.approve_relationship(db, workspace_id, relationship_id)


@router.post("/{relationship_id}/reject", response_model=RelationshipRead)
def reject_relationship(
    relationship_id: int,
    workspace_id: int = Depends(_require_workspace),
    db: Session = Depends(get_db),
):
    return relationship_service.reject_relationship(db, workspace_id, relationship_id)


@router.post("/{relationship_id}/archive", response_model=RelationshipRead)
def archive_relationship(
    relationship_id: int,
    workspace_id: int = Depends(_require_workspace),
    db: Session = Depends(get_db),
):
    return relationship_service.archive_relationship(db, workspace_id, relationship_id)
