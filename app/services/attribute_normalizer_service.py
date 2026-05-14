"""Reusable attribute normalization layer (MVP).

Sits in front of the proposal pipeline. Translates messy raw input into
canonical values BEFORE a ProposedAttributeValueEvent is written, or
discards irrelevant tokens.

Single public function:

    normalize_cell(attribute_name, raw_cell) -> list[NormalizationResult]

Each result carries a `decision`:

    matched      canonical_value populated; caller emits a ProposedAttribute-
                 ValueEvent with normalized_value=canonical and the raw
                 token preserved in evidence.
    discarded    discard_reason populated; caller emits NO event. Discard
                 is also written to a structured logger.
    passthrough  no rule fired and unmatched_policy=propose; caller falls
                 back to its existing direct-mode behaviour (split + match
                 against active AttributeAllowedValue).

This module deliberately:
  - reads its rules from `seed_data/attribute_normalization.json` only
    (no DB; no per-workspace overrides yet -- both are explicit
    deferred items in the design)
  - never imports SQLAlchemy or touches the engine models
  - never writes ProductAttribute, ProposedAttributeValueEvent, or any
    other engine row -- it returns a decision object and the caller
    routes it through the pipeline.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Pattern

# ---------------------------------------------------------------------------
# Rule loading. Cached on first read; safe to call from many threads since
# we only ever read.
# ---------------------------------------------------------------------------

_DEFAULTS_PATH = (
    Path(__file__).resolve().parents[2] / "seed_data" / "attribute_normalization.json"
)

_VALID_POLICIES = frozenset({"discard", "propose", "audit_only"})


@dataclass(frozen=True)
class _AttributeRules:
    """Normalised (compiled) form of one attribute's rule block."""
    split_chars: str
    case_sensitive: bool
    trim_whitespace: bool
    unmatched_policy: str
    synonym_lookup: dict[str, str]   # token -> canonical
    discard_patterns: list[tuple[str, Pattern[str]]]   # (raw_pattern, compiled)
    # Cleaning options (additive; default to disabled for back-compat).
    collapse_whitespace: bool          # collapse internal whitespace runs to one space
    strip_trailing_punctuation: bool   # strip trailing .,;:!?- characters
    strip_suffix_words: frozenset[str] # iteratively drop trailing tokens in this set


_rules_cache: dict[str, _AttributeRules] | None = None
_rules_path_used: Path | None = None


def _load_rules_from_disk(path: Path) -> dict[str, _AttributeRules]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, _AttributeRules] = {}
    for attr_name, block in (raw.get("attributes") or {}).items():
        policy = block.get("unmatched_policy", "propose")
        if policy not in _VALID_POLICIES:
            raise ValueError(
                f"attribute {attr_name!r}: unmatched_policy={policy!r} "
                f"not in {sorted(_VALID_POLICIES)}"
            )
        case_sensitive = bool(block.get("case_sensitive", False))
        trim_ws = bool(block.get("trim_whitespace", True))

        # Flatten synonyms into a token -> canonical map.
        synonym_lookup: dict[str, str] = {}
        for canonical, syns in (block.get("synonyms") or {}).items():
            for s in syns:
                key = s if case_sensitive else s.lower()
                if trim_ws:
                    key = key.strip()
                if not key:
                    continue
                if key in synonym_lookup and synonym_lookup[key] != canonical:
                    raise ValueError(
                        f"attribute {attr_name!r}: synonym {key!r} maps to "
                        f"both {synonym_lookup[key]!r} and {canonical!r}"
                    )
                synonym_lookup[key] = canonical

        # Compile discard patterns.
        flags = 0 if case_sensitive else re.IGNORECASE
        compiled: list[tuple[str, Pattern[str]]] = []
        for p in (block.get("discard_patterns") or []):
            compiled.append((p, re.compile(p, flags)))

        # Cleaning options (additive; default off for back-compat).
        collapse_ws = bool(block.get("collapse_whitespace", False))
        strip_trail_punct = bool(block.get("strip_trailing_punctuation", False))
        suffix_words_raw = block.get("strip_suffix_words") or []
        suffix_words = frozenset(
            (s if case_sensitive else s.lower()).strip()
            for s in suffix_words_raw if s and s.strip()
        )

        out[attr_name] = _AttributeRules(
            split_chars=block.get("split_chars", "|"),
            case_sensitive=case_sensitive,
            trim_whitespace=trim_ws,
            unmatched_policy=policy,
            synonym_lookup=synonym_lookup,
            discard_patterns=compiled,
            collapse_whitespace=collapse_ws,
            strip_trailing_punctuation=strip_trail_punct,
            strip_suffix_words=suffix_words,
        )
    return out


def _get_rules(attribute_name: str, rules_path: Path | None = None) -> _AttributeRules | None:
    global _rules_cache, _rules_path_used
    path = rules_path or _DEFAULTS_PATH
    if _rules_cache is None or _rules_path_used != path:
        _rules_cache = _load_rules_from_disk(path)
        _rules_path_used = path
    return _rules_cache.get(attribute_name)


def reload_rules(rules_path: Path | None = None) -> None:
    """Force a re-read of the rule config. Tests use this; production
    callers don't need it."""
    global _rules_cache, _rules_path_used
    _rules_cache = None
    _rules_path_used = None
    _override_cache.clear()
    if rules_path is not None:
        _get_rules("__warm__", rules_path)


# ---------------------------------------------------------------------------
# Per-workspace synonym overrides (Phase 8.5b).
#
# Layered on top of the JSON-loaded rules. Override rows are applied as
# extra synonyms (raw_value -> canonical_value) without modifying the
# discard patterns or unmatched_policy. Cache is keyed by (workspace_id,
# attribute_name) and refreshed when a reviewer adds new overrides.
# ---------------------------------------------------------------------------

# (workspace_id, attribute_name) -> (last_loaded_count, layered_rules)
_override_cache: dict[tuple[int, str], tuple[int, _AttributeRules]] = {}


def _layer_workspace_overrides(
    base: _AttributeRules, attribute_name: str,
    workspace_id: int, db,
) -> _AttributeRules:
    """Return a copy of `base` with workspace overrides merged into the
    synonym lookup. Reads `attribute_synonym_overrides` for the
    (workspace, attribute) pair. Cached per request key; cache is
    invalidated by row count change (cheap; correct enough)."""
    # Imported lazily to avoid a top-level dependency on the ORM model.
    from app.models.attribute_synonym_override import AttributeSynonymOverride

    cache_key = (workspace_id, attribute_name)
    rows = (
        db.query(AttributeSynonymOverride)
        .filter(
            AttributeSynonymOverride.workspace_id == workspace_id,
            AttributeSynonymOverride.attribute_name == attribute_name,
        )
        .all()
    )
    cached = _override_cache.get(cache_key)
    if cached is not None and cached[0] == len(rows):
        return cached[1]

    if not rows:
        # Cache the base so the next call short-circuits.
        _override_cache[cache_key] = (0, base)
        return base

    # Build a synonym map that layers overrides over the base. Overrides
    # win on conflict.
    merged: dict[str, str] = dict(base.synonym_lookup)
    for r in rows:
        key = r.raw_value if base.case_sensitive else r.raw_value.lower()
        if base.trim_whitespace:
            key = key.strip()
        if not key:
            continue
        merged[key] = r.canonical_value

    layered = _AttributeRules(
        split_chars=base.split_chars,
        case_sensitive=base.case_sensitive,
        trim_whitespace=base.trim_whitespace,
        unmatched_policy=base.unmatched_policy,
        synonym_lookup=merged,
        discard_patterns=base.discard_patterns,
        collapse_whitespace=base.collapse_whitespace,
        strip_trailing_punctuation=base.strip_trailing_punctuation,
        strip_suffix_words=base.strip_suffix_words,
    )
    _override_cache[cache_key] = (len(rows), layered)
    return layered


def _maybe_build_override_only_rules(
    workspace_id: int, attribute_name: str, db,
) -> _AttributeRules | None:
    """Build a minimal rules object from override rows alone.

    Used when an attribute has no JSON normalization config but workspace
    synonym overrides exist (e.g., product_type before any JSON synonyms
    are configured). Returns None when there are no overrides -- callers
    interpret None as "no rules; passthrough" to preserve legacy behaviour.

    Defaults: case-insensitive, trim whitespace, no splitting, no
    discards, no cleaning, unmatched_policy='propose' so unmatched
    tokens still flow through to the caller's existing logic.
    """
    from app.models.attribute_synonym_override import AttributeSynonymOverride

    rows = (
        db.query(AttributeSynonymOverride)
        .filter(
            AttributeSynonymOverride.workspace_id == workspace_id,
            AttributeSynonymOverride.attribute_name == attribute_name,
        )
        .all()
    )
    if not rows:
        return None

    synonyms: dict[str, str] = {}
    for r in rows:
        key = r.raw_value.strip().lower()
        if not key:
            continue
        synonyms[key] = r.canonical_value
    if not synonyms:
        return None
    return _AttributeRules(
        split_chars="",
        case_sensitive=False,
        trim_whitespace=True,
        unmatched_policy="propose",
        synonym_lookup=synonyms,
        discard_patterns=[],
        collapse_whitespace=False,
        strip_trailing_punctuation=False,
        strip_suffix_words=frozenset(),
    )


def invalidate_override_cache(
    *, workspace_id: int | None = None,
    attribute_name: str | None = None,
) -> None:
    """Clear the override cache. Called by approval endpoints after
    inserting new override rows so the next normalize_cell() call sees
    them."""
    if workspace_id is None and attribute_name is None:
        _override_cache.clear()
        return
    keys = list(_override_cache.keys())
    for k in keys:
        ws, attr = k
        if workspace_id is not None and ws != workspace_id:
            continue
        if attribute_name is not None and attr != attribute_name:
            continue
        _override_cache.pop(k, None)


# ---------------------------------------------------------------------------
# Public dataclass & function.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizationResult:
    raw: str                           # the input token (or whole cell on passthrough)
    decision: str                      # "matched" | "discarded" | "passthrough"
    canonical_value: str | None = None
    # Cleaned form when the rule applied cleaning (whitespace collapse,
    # trailing-punct strip, suffix-word strip). When set on a passthrough
    # result, callers should emit ProposedAttributeValueEvent with this
    # value as the normalized_value AND proposed_value_raw -- preserving
    # only the original raw token in evidence. None means the normalizer
    # did not transform the token; callers should use legacy logic.
    proposed_value: str | None = None
    discard_reason: str | None = None
    rule_id: str | None = None


# Dedicated logger for discard auditing. Callers can attach handlers
# (e.g. JSONL file, observability backend) without coupling.
_discard_logger = logging.getLogger("personafirst.normalizer.discard")


def _log_discard(*, attribute: str, raw: str, reason: str, rule_id: str | None) -> None:
    _discard_logger.info(
        "normalizer_discard",
        extra={
            "attribute": attribute,
            "raw_value": raw,
            "reason": reason,
            "rule_id": rule_id,
        },
    )


def normalize_cell(
    attribute_name: str, raw_cell: str,
    *,
    workspace_id: int | None = None,
    db: "Session | None" = None,  # type: ignore[name-defined]
) -> list[NormalizationResult]:
    """Normalise one CSV cell against the rules for `attribute_name`.

    Returns one result per token after splitting. If no rules are
    configured for the attribute, returns a single passthrough result
    carrying the unsplit cell -- the caller's existing splitting/matching
    path then handles it (so attributes without rules behave exactly as
    before).

    Per-workspace synonym overrides (Phase 8.5b): when both `workspace_id`
    and `db` are provided, AttributeSynonymOverride rows for that
    workspace are layered on top of the JSON synonym map. Override rows
    win over JSON for the same raw_value. Callers without `workspace_id`
    behave exactly as before (JSON-only) -- backwards compatible.
    """
    if raw_cell is None:
        return []
    cell = str(raw_cell)
    if cell.strip() == "":
        return []

    rules = _get_rules(attribute_name)
    if workspace_id is not None and db is not None:
        if rules is None:
            # No JSON rules but workspace overrides may still exist
            # (Phase 8.5b: attributes like product_type that historically
            # have no JSON normalization can still receive synonym
            # overrides from approvals).
            rules = _maybe_build_override_only_rules(
                workspace_id, attribute_name, db,
            )
        else:
            rules = _layer_workspace_overrides(
                rules, attribute_name, workspace_id, db,
            )
    if rules is None:
        return [NormalizationResult(raw=cell, decision="passthrough")]

    # Split per the rule's split_chars, then per-token classification.
    parts = _split(cell, rules.split_chars)
    out: list[NormalizationResult] = []
    has_cleaning = (rules.collapse_whitespace
                    or rules.strip_trailing_punctuation
                    or bool(rules.strip_suffix_words))
    for p in parts:
        token = p.strip() if rules.trim_whitespace else p
        if not token:
            continue
        cleaned = _clean_token(token, rules) if has_cleaning else token
        if not cleaned:
            # Cleaning consumed the entire token (e.g. it was just "Inc").
            # Treat as discarded so we keep an audit trail.
            reason = "cleaning produced empty value"
            rule_id = f"{attribute_name}:cleaned_to_empty"
            out.append(NormalizationResult(
                raw=token, decision="discarded",
                discard_reason=reason, rule_id=rule_id,
            ))
            _log_discard(attribute=attribute_name, raw=token, reason=reason, rule_id=rule_id)
            continue
        lookup_key = cleaned if rules.case_sensitive else cleaned.lower()

        canonical = rules.synonym_lookup.get(lookup_key)
        if canonical is not None:
            out.append(NormalizationResult(
                raw=token, decision="matched",
                canonical_value=canonical,
                rule_id=f"{attribute_name}:synonym:{canonical}",
            ))
            continue

        discard_match = _first_discard_match(rules.discard_patterns, lookup_key)
        if discard_match is not None:
            reason = f"matched discard_pattern {discard_match!r}"
            rule_id = f"{attribute_name}:discard:{discard_match}"
            out.append(NormalizationResult(
                raw=token, decision="discarded",
                discard_reason=reason, rule_id=rule_id,
            ))
            _log_discard(attribute=attribute_name, raw=token, reason=reason, rule_id=rule_id)
            continue

        # No rule fired -- apply unmatched_policy.
        if rules.unmatched_policy == "discard":
            reason = "no rule matched; unmatched_policy=discard"
            rule_id = f"{attribute_name}:unmatched_policy=discard"
            out.append(NormalizationResult(
                raw=token, decision="discarded",
                discard_reason=reason, rule_id=rule_id,
            ))
            _log_discard(attribute=attribute_name, raw=token, reason=reason, rule_id=rule_id)
        elif rules.unmatched_policy == "audit_only":
            reason = "audit_only"
            rule_id = f"{attribute_name}:unmatched_policy=audit_only"
            out.append(NormalizationResult(
                raw=token, decision="discarded",
                discard_reason=reason, rule_id=rule_id,
            ))
            _log_discard(attribute=attribute_name, raw=token, reason=reason, rule_id=rule_id)
        else:
            # propose: when the rule applied cleaning, surface the cleaned
            # form via `proposed_value` so the caller emits a single event
            # with the canonical lower-case stripped form. Otherwise the
            # caller falls back to existing direct-mode logic.
            proposed = lookup_key if has_cleaning else None
            out.append(NormalizationResult(
                raw=token, decision="passthrough",
                proposed_value=proposed,
                rule_id=f"{attribute_name}:unmatched_policy=propose",
            ))
    return out


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


_TRAILING_PUNCT_RE = re.compile(r"[.,;:!?\-_/]+$")
_INTERNAL_WS_RE = re.compile(r"\s+")


def _clean_token(token: str, rules: _AttributeRules) -> str:
    """Apply the rule's cleaning options. Always preserves case-sensitivity
    semantics of the rule. Returns the cleaned form."""
    s = token
    if rules.trim_whitespace:
        s = s.strip()
    if rules.collapse_whitespace:
        s = _INTERNAL_WS_RE.sub(" ", s)
    if rules.strip_trailing_punctuation:
        s = _TRAILING_PUNCT_RE.sub("", s)
        s = s.rstrip()
    if rules.strip_suffix_words:
        # Iteratively drop trailing whitespace-separated words that are
        # in the suffix list. "Foo Inc Co" -> "Foo Inc" -> "Foo".
        # Compare in normalised case.
        while True:
            s = s.rstrip()
            if rules.strip_trailing_punctuation:
                s = _TRAILING_PUNCT_RE.sub("", s)
                s = s.rstrip()
            idx = s.rfind(" ")
            if idx < 0:
                break
            tail = s[idx + 1:]
            tail_cmp = tail if rules.case_sensitive else tail.lower()
            if tail_cmp in rules.strip_suffix_words:
                s = s[:idx]
            else:
                break
    if rules.trim_whitespace:
        s = s.strip()
    return s


def _split(cell: str, split_chars: str) -> list[str]:
    if not split_chars:
        return [cell]
    parts = [cell]
    for ch in split_chars:
        next_parts: list[str] = []
        for p in parts:
            next_parts.extend(p.split(ch))
        parts = next_parts
    return parts


def _first_discard_match(
    patterns: list[tuple[str, Pattern[str]]], token: str,
) -> str | None:
    for raw, compiled in patterns:
        if compiled.search(token):
            return raw
    return None
