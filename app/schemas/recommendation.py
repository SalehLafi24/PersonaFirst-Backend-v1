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


class SlotAudienceConfig(BaseModel):
    """Audience / visibility filters.

    Schema-only in this phase — these filters describe who should see this
    slot, not which products are eligible.  Runtime enforcement is a future
    step; the schema is defined now so clients can start sending it.
    """
    filters: list[SlotFilter] = []


class SlotStrategyConfig(BaseModel):
    """Strategy: how the slot makes scoring decisions."""
    algorithm: str | None = None
    fallback_behavior: str | None = None


class SlotConstraintsConfig(BaseModel):
    """Product-level candidate pool filters.

    Reuses the existing SlotFilter logic — maps directly to the flat
    ``filters`` field used by the execution engine.
    """
    filters: list[SlotFilter] = []


class SlotControlsConfig(BaseModel):
    """Execution / tuning controls."""
    top_n: int | None = None
    diversity_mode: str | None = None


class SlotExclusionConfig(BaseModel):
    """Cross-slot coordination settings."""
    exclude_previous_slots: bool | None = None
    exclusion_level: str | None = None


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

    # Nested structure (optional).  When present, nested values override flat
    # equivalents via ``_flatten_nested_fields`` below.  Parsed and stored so
    # clients can round-trip them, but the execution path reads flat fields.
    audience: SlotAudienceConfig | None = None
    strategy: SlotStrategyConfig | None = None
    constraints: SlotConstraintsConfig | None = None
    controls: SlotControlsConfig | None = None
    exclusion: SlotExclusionConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten_nested_fields(cls, values):
        """Translate nested Slot config into flat fields before validation.

        When both nested and flat are provided, nested takes precedence.
        Nested ``None`` values leave flat fields untouched.  Nested
        ``constraints.filters`` overrides flat ``filters`` even when empty,
        since an explicit empty list is a meaningful "no constraints" signal.
        Nested ``audience.filters`` is parsed but not mapped to execution
        (schema-only in this phase).
        """
        if not isinstance(values, dict):
            return values

        strategy = values.get("strategy")
        if isinstance(strategy, dict):
            if strategy.get("algorithm") is not None:
                values["algorithm"] = strategy["algorithm"]
            if strategy.get("fallback_behavior") is not None:
                values["fallback_behavior"] = strategy["fallback_behavior"]

        controls = values.get("controls")
        if isinstance(controls, dict):
            if controls.get("top_n") is not None:
                values["top_n"] = controls["top_n"]
            if controls.get("diversity_mode") is not None:
                values["diversity_mode"] = controls["diversity_mode"]

        constraints = values.get("constraints")
        if isinstance(constraints, dict):
            if "filters" in constraints:
                values["filters"] = constraints["filters"]

        exclusion = values.get("exclusion")
        if isinstance(exclusion, dict):
            if exclusion.get("exclude_previous_slots") is not None:
                values["exclude_previous_slots"] = exclusion["exclude_previous_slots"]
            if exclusion.get("exclusion_level") is not None:
                values["exclusion_level"] = exclusion["exclusion_level"]

        return values

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
    targeting_mode: str | None = None  # how this attribute is used in scoring


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


class SignalSummary(BaseModel):
    """Lightweight explanation metadata for a recommendation.

    Summarises the inputs that drove the multi-source signal layer so that
    clients can surface a one-shot explanation without re-deriving anything.
    Populated alongside product_signal_strength / match_confidence.
    """
    matched_attribute_count: int
    compatibility_positive: float
    compatibility_negative: float
    conflict_indicators: list[str] = []


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
    direct_score: float                # categorical_affinity contributions only
    relationship_score: float
    popularity_score: float
    behavioral_score: float
    affinity_contribution: float = 0.0                 # categorical_affinity: soft preference signal
    compatibility_positive_contribution: float = 0.0   # compatibility_signal: positive suitability/fit
    compatibility_negative_contribution: float = 0.0   # compatibility_signal: penalty for mismatched values
    contextual_negative_contribution: float = 0.0     # contextual_semantic: mismatch penalty (occasion/activity)
    low_signal_penalty: float = 0.0                   # penalty for weak product enrichment coverage
    recommendation_score: float
    recommendation_source: str          # e.g. "direct" | "behavioral" | "direct+behavioral" | "popular"
    explanation: str
    relationship_matches: list[RelationshipMatch]
    behavioral_matches: list[BehavioralMatch]
    # Multi-source signal strength (additive / observational). None when the
    # caller has not fed enrichment outputs through the signal pipeline — keeps
    # existing recommendation_score behavior unchanged for legacy callers.
    product_signal_strength: float | None = None
    customer_signal_strength: float | None = None
    match_confidence: float | None = None
    signal_summary: SignalSummary | None = None
