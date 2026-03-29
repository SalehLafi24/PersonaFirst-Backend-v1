from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ProductBehaviorRelationshipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    source_product_db_id: int
    target_product_db_id: int
    strength: float
    customer_overlap_count: int
    source_customer_count: int
    created_at: datetime


class BehaviorEngineResult(BaseModel):
    relationships_created: int
