"""Conservative normalizer for proposed attribute names.

Same philosophy as proposed_value_normalizer: strip formatting noise so
"Climate Suitability", "climate_suitability", and " climate suitability "
collapse into the same cluster_key. No semantic merging — that's the
reviewer's call.
"""
from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[\-_/.]+")
_EDGE_PUNCT_RE = re.compile(r"^[\-_/.]+|[\-_/.]+$")


def normalize_attribute_name(raw: str) -> str:
    """Return a deterministic, lowercase, underscore-joined form of *raw*.

    Rules:
    - lowercase
    - strip leading/trailing whitespace
    - replace internal hyphens, slashes, dots with underscores
    - collapse runs of underscores/whitespace into a single underscore
    - strip leading/trailing underscores
    - DO NOT stem, lemmatize, or translate
    - DO NOT merge synonyms
    """
    if raw is None:
        return ""
    value = str(raw).strip().lower()
    value = _PUNCT_RE.sub("_", value)
    value = _WHITESPACE_RE.sub("_", value)
    value = re.sub(r"_+", "_", value)
    value = value.strip("_")
    return value
