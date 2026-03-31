from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RelationshipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    source_attribute_id: str
    source_value: str
    target_attribute_id: str
    target_value: str
    relationship_type: str | None = None
    source: str | None = None
    confidence: float
    lift: float
    pair_count: int
    status: str
    created_at: datetime
