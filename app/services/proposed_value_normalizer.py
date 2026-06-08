"""Deterministic, conservative normalization for proposed attribute values.

Kept intentionally lightweight: this is ONLY used to build the cluster_key
used when aggregating raw events. It must never invent or translate — just
strip obvious formatting noise so "HIIT", "hiit", and " HIIT " all collapse
into the same bucket.

Not applied to actual `values` going into the taxonomy — those are already
validated against allowed_values upstream.
"""
from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCT_EDGE_RE = re.compile(r"^[\-_/.]+|[\-_/.]+$")


def normalize_proposed_value(raw: str) -> str:
    """Return a deterministic, lowercase, whitespace-collapsed form of `raw`.

    Rules (kept conservative on purpose):
    - lowercase
    - strip leading/trailing whitespace
    - collapse internal whitespace to a single space
    - strip leading/trailing punctuation from the common set (-, _, /, .)
    - DO NOT alter word order, stem, lemmatize, or translate
    - DO NOT merge synonyms — that is the reviewer's call
    """
    if raw is None:
        return ""
    value = str(raw).strip().lower()
    value = _WHITESPACE_RE.sub(" ", value)
    value = _PUNCT_EDGE_RE.sub("", value)
    return value.strip()
