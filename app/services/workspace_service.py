from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.workspace import Workspace
from app.schemas.workspace import WorkspaceCreate


def create_workspace(db: Session, data: WorkspaceCreate) -> Workspace:
    workspace = Workspace(name=data.name, slug=data.slug)
    db.add(workspace)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Slug already exists")
    db.refresh(workspace)
    return workspace


def list_workspaces(db: Session) -> list[Workspace]:
    return db.query(Workspace).all()


def get_workspace(db: Session, workspace_id: int) -> Workspace | None:
    return db.query(Workspace).filter(Workspace.id == workspace_id).first()
