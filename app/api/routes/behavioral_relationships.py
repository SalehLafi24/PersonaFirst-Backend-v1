from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.product_behavior_relationship import ProductBehaviorRelationship
from app.schemas.product_behavior_relationship import (
    BehaviorEngineResult,
    ProductBehaviorRelationshipRead,
)
from app.services import behavior_engine_service
from app.services.workspace_service import get_workspace

router = APIRouter(
    prefix="/workspaces/{workspace_id}/behavioral-relationships",
    tags=["behavioral-relationships"],
)


def _require_workspace(workspace_id: int, db: Session = Depends(get_db)) -> int:
    if not get_workspace(db, workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace_id


@router.post("/generate", response_model=BehaviorEngineResult)
def generate_behavioral_relationships(
    workspace_id: int = Depends(_require_workspace),
    db: Session = Depends(get_db),
):
    created = behavior_engine_service.run_behavior_engine(db, workspace_id)
    return BehaviorEngineResult(relationships_created=created)


@router.get("/", response_model=list[ProductBehaviorRelationshipRead])
def list_behavioral_relationships(
    workspace_id: int = Depends(_require_workspace),
    db: Session = Depends(get_db),
):
    return (
        db.query(ProductBehaviorRelationship)
        .filter(ProductBehaviorRelationship.workspace_id == workspace_id)
        .order_by(
            ProductBehaviorRelationship.source_product_db_id,
            ProductBehaviorRelationship.target_product_db_id,
        )
        .all()
    )
