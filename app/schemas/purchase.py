from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, field_validator


class PurchaseCreate(BaseModel):
    customer_id: str
    product_id: str
    order_date: date
    quantity: int = 1
    revenue: float | None = None

    @field_validator("quantity")
    @classmethod
    def quantity_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("quantity must be >= 1")
        return v


class PurchaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    customer_id: str
    product_id: str
    group_id: str | None
    order_date: date
    quantity: int
    revenue: float | None
    created_at: datetime
