from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AffinityCreate(BaseModel):
    customer_id: str
    attribute_name: str
    value_label: str
    score: float


class AffinityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    customer_id: str
    attribute_id: str
    attribute_value: str
    score: float
    created_at: datetime
