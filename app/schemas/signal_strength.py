from pydantic import BaseModel, field_validator


class SignalStrengthComponents(BaseModel):
    purchase_depth: float
    attribute_richness: float
    behavioral_graph: float


class SignalStrengthRead(BaseModel):
    customer_id: str
    customer_signal_strength: float
    components: SignalStrengthComponents


class BatchSignalStrengthRead(BaseModel):
    workspace_id: int
    results: list[SignalStrengthRead]


class AudienceSignalRequest(BaseModel):
    customer_ids: list[str]

    @field_validator("customer_ids")
    @classmethod
    def customer_ids_must_be_non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("customer_ids must be a non-empty list")
        return v


class SignalDistributionRead(BaseModel):
    low: int
    medium: int
    high: int


class AudienceSignalRead(BaseModel):
    audience_size: int
    average_signal_strength: float
    min_signal_strength: float
    max_signal_strength: float
    signal_distribution: SignalDistributionRead
