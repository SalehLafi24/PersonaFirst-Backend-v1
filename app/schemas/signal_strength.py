from pydantic import BaseModel


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
