from pydantic import BaseModel, field_validator, model_validator

VALID_FILTER_OPERATORS = {"eq", "in"}
VALID_FALLBACK_MODES = {"strict", "relax_filters"}
VALID_EXCLUSION_LEVELS = {"product", "group"}
VALID_DIVERSITY_MODES = {"off", "strict", "adaptive"}
VALID_FALLBACK_BEHAVIORS = {"none", "direct", "balanced"}


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
    exclusion_level: str = "product"
    diversity_enabled: bool = False
    diversity_mode: str = "off"
    fallback_behavior: str = "none"

    @model_validator(mode="after")
    def _resolve_diversity_compat(self) -> "SlotConfig":
        """Legacy diversity_enabled=true maps to diversity_mode='strict'
        unless diversity_mode was explicitly set to something other than 'off'."""
        # If caller set diversity_mode explicitly (not default), it takes precedence.
        # If only diversity_enabled was set, map it.
        if self.diversity_enabled and self.diversity_mode == "off":
            self.diversity_mode = "strict"
        return self

    @field_validator("diversity_mode")
    @classmethod
    def diversity_mode_must_be_valid(cls, v: str) -> str:
        if v not in VALID_DIVERSITY_MODES:
            raise ValueError(
                f"Invalid diversity_mode '{v}'. Must be one of: {', '.join(sorted(VALID_DIVERSITY_MODES))}"
            )
        return v

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

    @field_validator("exclusion_level")
    @classmethod
    def exclusion_level_must_be_valid(cls, v: str) -> str:
        if v not in VALID_EXCLUSION_LEVELS:
            raise ValueError(
                f"Invalid exclusion_level '{v}'. Must be one of: {', '.join(sorted(VALID_EXCLUSION_LEVELS))}"
            )
        return v

    @field_validator("fallback_behavior")
    @classmethod
    def fallback_behavior_must_be_valid(cls, v: str) -> str:
        if v not in VALID_FALLBACK_BEHAVIORS:
            raise ValueError(
                f"Invalid fallback_behavior '{v}'. Must be one of: {', '.join(sorted(VALID_FALLBACK_BEHAVIORS))}"
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
