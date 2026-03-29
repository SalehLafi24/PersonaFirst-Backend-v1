from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.workspace import WorkspaceCreate, WorkspaceRead
from app.schemas.workspace_user import WorkspaceUserCreate, WorkspaceUserRead
from app.services import workspace_service, workspace_user_service

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.post("", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED)
def create_workspace(data: WorkspaceCreate, db: Session = Depends(get_db)):
    return workspace_service.create_workspace(db, data)


@router.get("", response_model=list[WorkspaceRead])
def list_workspaces(db: Session = Depends(get_db)):
    return workspace_service.list_workspaces(db)


@router.post(
    "/{workspace_id}/members",
    response_model=WorkspaceUserRead,
    status_code=status.HTTP_201_CREATED,
)
def add_member(
    workspace_id: int, data: WorkspaceUserCreate, db: Session = Depends(get_db)
):
    workspace = workspace_service.get_workspace(db, workspace_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace_user_service.assign_user_to_workspace(db, workspace_id, data)
