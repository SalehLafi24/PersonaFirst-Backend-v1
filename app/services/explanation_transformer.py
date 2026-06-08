"""Transform raw V1 'why' components into UX-ready chips and prose.

Pure function. No DB access. Used by the admin explorer route to enrich
each recommendation with display-ready metadata so the frontend never
has to parse component names directly.

Two outputs per recommendation:

    chips         ordered list of {kind, label, tone} for the default view.
                  Penalties are tagged tone='warning' and excluded unless
                  the caller is in debug mode.

    sentence      one-line human prose summary; built from the same
                  components, suitable for the "Why" expandable panel.

The transformer also derives a 0..5 strength dot count from tier + score,
calibrated to feel honest (e.g. tier_1 ties at 0.150 = 3 dots, brand
match = 5 dots) rather than reflecting raw score arithmetic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# Mapping table: raw V1 component name -> chip metadata.
#
# tone in {"positive", "neutral", "warning"}.
#   - positive  shown by default; emphasises a real signal (brand, name overlap)
#   - neutral   shown by default; cohort-derived (same product_type, age, gender)
#   - warning   hidden in default view; surfaced only in debug or weak-case mode
#
# debug_only=True chips are NEVER shown in default view.
_COMPONENT_MAP: dict[str, dict] = {
    "same_product_type":      {"kind": "same_type",   "label": "Same Type",
                               "tone": "neutral",  "debug_only": False},
    "same_age_group":         {"kind": "same_age",    "label": "Same Age",
                               "tone": "neutral",  "debug_only": False},
    "same_gender":            {"kind": "same_gender", "label": "Same Gender",
                               "tone": "neutral",  "debug_only": False},
    "same_brand":             {"kind": "same_brand",  "label": "Same Brand",
                               "tone": "positive", "debug_only": False},
    "name_overlap":           {"kind": "name_match",  "label": "Name Match",
                               "tone": "positive", "debug_only": False},
    "unisex_compatible":      {"kind": "unisex",      "label": "Unisex Compatible",
                               "tone": "neutral",  "debug_only": False},
    "price_proximity":        {"kind": "price_close", "label": "Similar Price",
                               "tone": "positive", "debug_only": False},
    # Penalties: hidden in default; surfaced as warning chips only when the
    # caller asks for them (debug or weak-case banners on the card).
    "opposite_gender_penalty": {"kind": "gender_mismatch", "label": "Different Gender",
                                "tone": "warning", "debug_only": True},
    "age_adjacent_penalty":    {"kind": "age_adjacent",    "label": "Adjacent Age",
                                "tone": "warning", "debug_only": True},
    "age_non_adjacent_penalty": {"kind": "age_mismatch",   "label": "Different Age Group",
                                 "tone": "warning", "debug_only": True},
}


# Tier label translation. Demo mode uses humanised; debug shows raw.
_TIER_HUMAN_LABEL: dict[str, str] = {
    "tier_1":  "Strong match",
    "tier_2":  "Broader match",
    "partial": "Limited matches",
    "empty":   "No recommendations",
}


@dataclass
class Chip:
    kind: str
    label: str
    tone: str
    debug_only: bool = False
    raw_component: str = ""
    raw_contribution: float = 0.0

    def to_json(self, *, include_raw: bool = False) -> dict:
        out = {"kind": self.kind, "label": self.label, "tone": self.tone}
        if self.debug_only:
            out["debug_only"] = True
        if include_raw:
            out["raw_component"] = self.raw_component
            out["raw_contribution"] = round(self.raw_contribution, 4)
        return out


@dataclass
class TransformedRecommendation:
    chips: list[Chip] = field(default_factory=list)
    sentence: str = ""
    strength_dots: int = 0  # 0..5
    has_warning: bool = False  # any debug_only chip exists
    warning_reasons: list[str] = field(default_factory=list)

    def to_json(self, *, include_debug: bool) -> dict:
        return {
            "chips": [
                c.to_json(include_raw=include_debug)
                for c in self.chips
                if include_debug or not c.debug_only
            ],
            "sentence": self.sentence,
            "strength_dots": self.strength_dots,
            "has_warning": self.has_warning,
            "warning_reasons": list(self.warning_reasons),
        }


def humanize_tier(tier: str) -> str:
    """Demo-mode label for a tier value. Falls back to the raw tier
    string for unknown values so callers don't crash on schema drift."""
    return _TIER_HUMAN_LABEL.get(tier, tier)


def _strength_dots_from_chips(chips: list[Chip], score: float, tier: str) -> int:
    """Map score + chip composition to 0..5 dots.

    Calibration choices (deliberate, not derived):
      - empty / no recs:          0 dots
      - cohort-only (tier_1 score == 0.150 etc.): 3 dots — solid match
      - cohort + name overlap:    4 dots
      - cohort + same brand:      5 dots — explicit brand sibling is the
                                  strongest in-catalog signal V1 produces
      - tier_2 with no penalties: 2 dots
      - tier_2 with penalty:      1 dot
      - score < 0:                1 dot (still surfaced but visibly weak)
    """
    if tier == "empty":
        return 0
    has_brand = any(c.kind == "same_brand" for c in chips)
    has_name_match = any(c.kind == "name_match" for c in chips)
    has_warning = any(c.tone == "warning" for c in chips)

    if has_warning and score <= 0:
        return 1
    if has_warning:
        return 2
    if has_brand:
        return 5
    if has_name_match and tier == "tier_1":
        return 4
    if tier == "tier_1":
        return 3
    if tier == "tier_2":
        return 2 if score >= 0 else 1
    return 2  # partial / unknown


def _make_sentence(chips: list[Chip]) -> str:
    """One-line prose for the expandable Why panel.

    Builds: 'Same product_type, same age_group, same brand. Names share <token>, <token>.'
    """
    visible = [c for c in chips if not c.debug_only]
    if not visible:
        return "No specific signals — included by cohort match only."
    cohort = [c.label.lower() for c in visible if c.tone == "neutral"]
    positive = [c.label.lower() for c in visible if c.tone == "positive"]
    parts: list[str] = []
    if cohort:
        parts.append(_oxford_join(cohort))
    if positive:
        parts.append(_oxford_join(positive))
    if not parts:
        return "Included by cohort match."
    return ". ".join(s.capitalize() for s in parts) + "."


def _oxford_join(items: Iterable[str]) -> str:
    items = list(items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def transform(
    *,
    why: list[dict],
    score: float,
    tier: str,
) -> TransformedRecommendation:
    """Build chips + sentence + strength from one rec's raw `why` array.

    Unknown components are passed through as a raw chip so the UI can
    still surface them (rather than silently dropping). They're tagged
    debug_only=True so demo viewers don't see them.
    """
    chips: list[Chip] = []
    warning_reasons: list[str] = []
    seen_kinds: set[str] = set()

    for w in why or []:
        comp = str(w.get("component") or "")
        contrib = float(w.get("contribution") or 0.0)
        meta = _COMPONENT_MAP.get(comp)
        if meta is None:
            chip = Chip(
                kind=comp or "unknown",
                label=comp.replace("_", " ").title() if comp else "Unknown",
                tone="neutral",
                debug_only=True,
                raw_component=comp,
                raw_contribution=contrib,
            )
        else:
            chip = Chip(
                kind=meta["kind"],
                label=meta["label"],
                tone=meta["tone"],
                debug_only=meta["debug_only"],
                raw_component=comp,
                raw_contribution=contrib,
            )
        if chip.kind in seen_kinds:
            continue
        seen_kinds.add(chip.kind)
        chips.append(chip)
        if chip.tone == "warning":
            warning_reasons.append(chip.label)

    return TransformedRecommendation(
        chips=chips,
        sentence=_make_sentence(chips),
        strength_dots=_strength_dots_from_chips(chips, score, tier),
        has_warning=any(c.tone == "warning" for c in chips),
        warning_reasons=warning_reasons,
    )
