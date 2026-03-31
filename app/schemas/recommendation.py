from pydantic import BaseModel, field_validator

VALID_FILTER_OPERATORS = {"eq", "in"}
VALID_FALLBACK_MODES = {"strict", "relax_filters"}


class SlotFilter(BaseModel):
    attribute_id: str
    operator: str
    value: str | list[str]

    @field_validator("operator")
    @classmethod
    def operator_must_be_valid(cls, v: str) -> str:
        if v not in VALID_FILTER_OPERATORS:
            raise ValueError(
                f"Invalid operator '{v}'. Must be one of: {', '.join(sorted(VALID_FILTER_OPERATORS))}"
            )
        return v


class SlotConfig(BaseModel):
    slot_id: str
    algorithm: str
    top_n: int
    filters: list[SlotFilter] = []
    fallback_mode: str = "strict"
    exclude_previous_slots: bool = False
    diversity_enabled: bool = False

    @field_validator("top_n")
    @classmethod
    def top_n_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("top_n must be greater than 0")
        return v

    @field_validator("fallback_mode")
    @classmethod
    def fallback_mode_must_be_valid(cls, v: str) -> str:
        if v not in VALID_FALLBACK_MODES:
            raise ValueError(
                f"Invalid fallback_mode '{v}'. Must be one of: {', '.join(sorted(VALID_FALLBACK_MODES))}"
            )
        return v


class SlotRequest(BaseModel):
    customer_id: str
    slot: SlotConfig


class MultiSlotRequest(BaseModel):
    customer_id: str
    slots: list[SlotConfig]

    @field_validator("slots")
    @classmethod
    def slots_must_be_non_empty(cls, v: list[SlotConfig]) -> list[SlotConfig]:
        if not v:
            raise ValueError("slots must be a non-empty list")
        seen: set[str] = set()
        for slot in v:
            if slot.slot_id in seen:
                raise ValueError(f"Duplicate slot_id '{slot.slot_id}'")
            seen.add(slot.slot_id)
        return v


class MatchedAttribute(BaseModel):
    attribute_id: str
    attribute_value: str
    score: float    # raw affinity score
    weight: float   # attribute weight applied during scoring (1.0 / 0.5 / 0.2)


class RelationshipMatch(BaseModel):
    source_attribute_id: str
    source_attribute_value: str
    target_attribute_id: str
    target_attribute_value: str
    source_score: float         # affinity score of the source attribute
    relationship_strength: float
    contribution: float         # source_score * relationship_strength


class BehavioralMatch(BaseModel):
    source_product_id: str      # external product_id of the purchased source product
    strength: float             # strength of the behavioral relationship (A→this)
    contribution: float         # = strength; accumulated into behavioral_score


class SlotResponse(BaseModel):
    slot_id: str
    algorithm: str
    fallback_mode: str
    fallback_applied: bool
    results: list["RecommendationRead"]


class MultiSlotResponse(BaseModel):
    customer_id: str
    slots: list[SlotResponse]


class RecommendationRead(BaseModel):
    product_id: str
    sku: str
    name: str
    group_id: str | None
    matched_attributes: list[MatchedAttribute]
    direct_score: float
    relationship_score: float
    popularity_score: float
    behavioral_score: float
    recommendation_score: float
    recommendation_source: str          # e.g. "direct" | "behavioral" | "direct+behavioral" | "popular"
    explanation: str
    relationship_matches: list[RelationshipMatch]
    behavioral_matches: list[BehavioralMatch]
