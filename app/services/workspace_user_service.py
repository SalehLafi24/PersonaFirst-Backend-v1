from sqlalchemy.orm import Session

from app.models.workspace_user import WorkspaceUser
from app.schemas.workspace_user import WorkspaceUserCreate


def assign_user_to_workspace(
    db: Session, workspace_id: int, data: WorkspaceUserCreate
) -> WorkspaceUser:
    membership = WorkspaceUser(
        workspace_id=workspace_id,
        user_id=data.user_id,
        role=data.role,
    )
    db.add(membership)
    db.commit()
    db.refresh(membership)
    return membership
