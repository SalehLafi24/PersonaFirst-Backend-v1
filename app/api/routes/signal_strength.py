from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.signal_strength import BatchSignalStrengthRead, SignalStrengthRead
from app.services import signal_strength_service
from app.services.workspace_service import get_workspace

router = APIRouter(
    prefix="/workspaces/{workspace_id}/signal-strength",
    tags=["signal-strength"],
)


def _require_workspace(workspace_id: int, db: Session = Depends(get_db)) -> int:
    if not get_workspace(db, workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace_id


@router.get("/{customer_id}", response_model=SignalStrengthRead)
def get_signal_strength(
    customer_id: str,
    workspace_id: int = Depends(_require_workspace),
    db: Session = Depends(get_db),
):
    return signal_strength_service.compute_customer_signal_strength(
        db, workspace_id, customer_id,
    )


@router.get("", response_model=BatchSignalStrengthRead)
def get_batch_signal_strength(
    workspace_id: int = Depends(_require_workspace),
    db: Session = Depends(get_db),
):
    results = signal_strength_service.batch_compute_customer_signal_strength(
        db, workspace_id,
    )
    return BatchSignalStrengthRead(workspace_id=workspace_id, results=results)
