from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

Role = Literal["owner", "admin", "member"]


class WorkspaceUserCreate(BaseModel):
    user_id: int
    role: Role = "member"


class WorkspaceUserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    user_id: int
    role: str
    created_at: datetime
