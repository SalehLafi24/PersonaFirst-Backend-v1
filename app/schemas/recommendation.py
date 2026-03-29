from pydantic import BaseModel


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
