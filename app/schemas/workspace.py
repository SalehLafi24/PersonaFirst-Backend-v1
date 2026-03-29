from datetime import datetime

from pydantic import BaseModel, ConfigDict


class WorkspaceCreate(BaseModel):
    name: str
    slug: str


class WorkspaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    created_at: datetime
