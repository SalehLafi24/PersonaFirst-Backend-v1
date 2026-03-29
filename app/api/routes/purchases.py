from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.purchase import PurchaseCreate, PurchaseRead
from app.services import purchase_service
from app.services.workspace_service import get_workspace

router = APIRouter(
    prefix="/workspaces/{workspace_id}/purchases",
    tags=["purchases"],
)


def _require_workspace(workspace_id: int, db: Session = Depends(get_db)) -> int:
    if not get_workspace(db, workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace_id


@router.post("", response_model=list[PurchaseRead], status_code=status.HTTP_201_CREATED)
def bulk_create_purchases(
    data: list[PurchaseCreate],
    workspace_id: int = Depends(_require_workspace),
    db: Session = Depends(get_db),
):
    return purchase_service.bulk_create_purchases(db, workspace_id, data)
