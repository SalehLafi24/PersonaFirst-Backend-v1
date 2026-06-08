"""Labeling guide schema, loader, and validator (Phase 8 / labeling guide v1).

Per-workspace YAML guides explain what each attribute value MEANS in that
workspace. Labelers use them; future tools (LLM prompt builders, eval
scripts, the Explorer's hover-text) can also consume them.

Architecture:
    - Schema is global and stable (this file).
    - Per-workspace instance documents live in
      seed_data/eval/labeling_guides/{workspace_slug}.yaml.
    - The guide is a CONSUMER of vocabulary (manifest's allowed_values
      for closed taxonomies, AttributeAllowedValue rows for open ones),
      never a producer of it.
    - Closed-taxonomy attributes use a `values:` block where every
      manifest value gets an entry.
    - Open-taxonomy attributes use a `canonical_examples:` block with
      8-10 anchor values (NOT exhaustive). The remaining values for
      open attributes are governed by taxonomy_admin / AAV review notes.

The loader returns typed dataclasses; the validator cross-references
against the AttributeManifest and emits warnings (not errors) for
incomplete coverage.

Boundary: this module reads files. It does not write. It does not run
queries. It is safe to call at startup and from any tool.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import yaml

from app.services.attribute_engine.manifest import AttributeManifest


_DEFAULT_GUIDE_DIR = (
    Path(__file__).resolve().parents[3] / "seed_data" / "eval" / "labeling_guides"
)

# Recognised cardinality / kind values; mirror the manifest's vocabulary.
_VALID_CARDINALITY = frozenset({"single", "multi"})
_VALID_TAXONOMY_KIND = frozenset({"closed", "open"})


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GuideValue:
    """One value entry: definition + examples + optional notes.

    Used inside `values` (closed) AND `canonical_examples` (open) blocks
    -- the structure is the same; what differs is whether every manifest
    value is required to appear (closed) or only the chosen anchors (open).
    """
    name: str
    definition: str
    positive_examples: tuple[str, ...] = ()
    negative_examples: tuple[str, ...] = ()
    notes: str | None = None


@dataclass(frozen=True)
class GuideAttribute:
    """One attribute's documentation block.

    Closed attributes populate `values`; open attributes populate
    `canonical_examples` (and `values` is empty). The validator enforces
    the correct shape against the manifest's `taxonomy.kind`.
    """
    name: str
    description: str
    cardinality: str
    taxonomy_kind: str
    out_of_vocabulary_policy: str
    edge_cases: str
    values: Mapping[str, GuideValue]              # closed only
    canonical_examples: Mapping[str, GuideValue]  # open only

    def is_closed(self) -> bool:
        return self.taxonomy_kind == "closed"


@dataclass(frozen=True)
class LabelingGuide:
    version: str
    workspace_slug: str
    manifest_version_at_authoring: str | None
    authored_at: str | None
    authors: tuple[str, ...]
    domain: str
    labeler_brief: str
    attributes: Mapping[str, GuideAttribute]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _parse_value(name: str, raw: dict) -> GuideValue:
    if not isinstance(raw, dict):
        raise ValueError(
            f"value {name!r} block must be a mapping, got {type(raw).__name__}"
        )
    definition = (raw.get("definition") or "").strip()
    if not definition:
        raise ValueError(
            f"value {name!r}: definition is required and cannot be empty"
        )
    return GuideValue(
        name=name,
        definition=definition,
        positive_examples=tuple(raw.get("positive_examples") or []),
        negative_examples=tuple(raw.get("negative_examples") or []),
        notes=(raw.get("notes") or None),
    )


def _parse_attribute(name: str, raw: dict) -> GuideAttribute:
    if not isinstance(raw, dict):
        raise ValueError(
            f"attribute {name!r} block must be a mapping, got {type(raw).__name__}"
        )
    cardinality = (raw.get("cardinality") or "single").strip()
    if cardinality not in _VALID_CARDINALITY:
        raise ValueError(
            f"attribute {name!r}: cardinality={cardinality!r} not in "
            f"{sorted(_VALID_CARDINALITY)}"
        )
    taxonomy_kind = (raw.get("taxonomy_kind") or "").strip()
    if taxonomy_kind not in _VALID_TAXONOMY_KIND:
        raise ValueError(
            f"attribute {name!r}: taxonomy_kind={taxonomy_kind!r} not in "
            f"{sorted(_VALID_TAXONOMY_KIND)}"
        )
    description = (raw.get("description") or "").strip()
    if not description:
        raise ValueError(f"attribute {name!r}: description is required")

    values_raw = raw.get("values") or {}
    canonical_raw = raw.get("canonical_examples") or {}
    if taxonomy_kind == "closed" and not values_raw:
        raise ValueError(
            f"attribute {name!r}: closed taxonomy requires a non-empty "
            f"`values:` block (one entry per manifest allowed_value)"
        )
    if taxonomy_kind == "open" and not canonical_raw and not values_raw:
        raise ValueError(
            f"attribute {name!r}: open taxonomy requires a "
            f"`canonical_examples:` block (8-10 anchor values)"
        )
    if taxonomy_kind == "open" and values_raw:
        # We accept this defensively but the validator will warn.
        pass

    values = {n: _parse_value(n, b) for n, b in (values_raw or {}).items()}
    canonical_examples = {
        n: _parse_value(n, b) for n, b in (canonical_raw or {}).items()
    }

    return GuideAttribute(
        name=name,
        description=description,
        cardinality=cardinality,
        taxonomy_kind=taxonomy_kind,
        out_of_vocabulary_policy=(
            raw.get("out_of_vocabulary_policy") or ""
        ).strip(),
        edge_cases=(raw.get("edge_cases") or "").strip(),
        values=values,
        canonical_examples=canonical_examples,
    )


def load_labeling_guide(
    workspace_slug: str | None = None,
    *,
    path: Path | None = None,
) -> LabelingGuide:
    """Load a labeling guide YAML file. Either pass `workspace_slug`
    (looks up `seed_data/eval/labeling_guides/{slug}.yaml`) or `path`.

    Raises FileNotFoundError / ValueError on bad paths or malformed
    documents. The output is fully typed; downstream consumers don't
    need to re-parse YAML.
    """
    if path is None:
        if not workspace_slug:
            raise ValueError("either workspace_slug or path is required")
        path = _DEFAULT_GUIDE_DIR / f"{workspace_slug}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"labeling guide not found at {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path}: top-level YAML must be a mapping, got {type(raw).__name__}"
        )

    attrs_raw = raw.get("attributes") or {}
    if not isinstance(attrs_raw, dict):
        raise ValueError(f"{path}: `attributes` must be a mapping")
    attributes = {n: _parse_attribute(n, b) for n, b in attrs_raw.items()}

    return LabelingGuide(
        version=str(raw.get("version") or "1.0"),
        workspace_slug=raw.get("workspace_slug") or workspace_slug or "",
        manifest_version_at_authoring=raw.get("manifest_version_at_authoring"),
        authored_at=raw.get("authored_at"),
        authors=tuple(raw.get("authors") or []),
        domain=raw.get("domain") or "",
        labeler_brief=(raw.get("labeler_brief") or "").strip(),
        attributes=attributes,
    )


# ---------------------------------------------------------------------------
# Validator (warnings, not errors)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GuideWarning:
    severity: str   # "warning" | "info"
    attribute: str | None
    code: str
    message: str


def validate_guide_against_manifest(
    guide: LabelingGuide, manifest: AttributeManifest,
) -> list[GuideWarning]:
    """Cross-reference a loaded guide against the manifest. Returns a
    list of warnings; empty list = clean.

    Checks:
      W001  manifest attribute has no guide entry
      W002  guide attribute not in manifest
      W003  closed-taxonomy guide value not in manifest's allowed_values
      W004  closed-taxonomy manifest value missing from guide
      W005  open-taxonomy attribute has `values:` block (should be `canonical_examples`)
      W006  guide cardinality differs from manifest cardinality
      W007  guide taxonomy_kind differs from manifest taxonomy.kind
      INFO  open-taxonomy attribute documents <3 canonical examples
    """
    warnings: list[GuideWarning] = []

    # W001: manifest has it, guide doesn't.
    for name in manifest.entries:
        if name not in guide.attributes:
            warnings.append(GuideWarning(
                severity="warning", attribute=name, code="W001",
                message=(
                    f"manifest declares attribute {name!r} but the guide has "
                    f"no entry; labelers will have no documentation for it"
                ),
            ))

    # W002: guide has it, manifest doesn't.
    for name in guide.attributes:
        if name not in manifest.entries:
            warnings.append(GuideWarning(
                severity="warning", attribute=name, code="W002",
                message=(
                    f"guide documents attribute {name!r} but the manifest "
                    f"has no entry for it"
                ),
            ))

    # Per-attribute cross-checks.
    for name, ga in guide.attributes.items():
        me = manifest.entries.get(name)
        if me is None:
            continue

        # W007: kind mismatch.
        if ga.taxonomy_kind != me.taxonomy.kind:
            warnings.append(GuideWarning(
                severity="warning", attribute=name, code="W007",
                message=(
                    f"guide taxonomy_kind={ga.taxonomy_kind!r} but manifest "
                    f"taxonomy.kind={me.taxonomy.kind!r}; semantics will "
                    f"diverge"
                ),
            ))

        # W006: cardinality mismatch.
        if ga.cardinality != me.taxonomy.cardinality:
            warnings.append(GuideWarning(
                severity="warning", attribute=name, code="W006",
                message=(
                    f"guide cardinality={ga.cardinality!r} but manifest "
                    f"taxonomy.cardinality={me.taxonomy.cardinality!r}"
                ),
            ))

        if me.taxonomy.kind == "closed":
            allowed = set(me.taxonomy.allowed_values or [])
            guide_values = set(ga.values.keys())
            # W003: guide value not in manifest.
            for v in guide_values - allowed:
                warnings.append(GuideWarning(
                    severity="warning", attribute=name, code="W003",
                    message=(
                        f"guide documents value {v!r} but it's not in the "
                        f"manifest's allowed_values for {name!r}"
                    ),
                ))
            # W004: manifest value missing from guide.
            for v in allowed - guide_values:
                warnings.append(GuideWarning(
                    severity="warning", attribute=name, code="W004",
                    message=(
                        f"manifest declares value {v!r} for {name!r} but "
                        f"the guide has no entry; labelers will see this "
                        f"value but have no definition"
                    ),
                ))
        else:  # open
            # W005: open-taxonomy guide using `values:` instead of canonical_examples.
            if ga.values:
                warnings.append(GuideWarning(
                    severity="warning", attribute=name, code="W005",
                    message=(
                        f"open-taxonomy attribute {name!r} has a `values:` "
                        f"block; open attributes should use "
                        f"`canonical_examples:` instead (the full value set "
                        f"is governed by AAV / taxonomy_admin)"
                    ),
                ))
            # INFO: too few anchors.
            if len(ga.canonical_examples) < 3:
                warnings.append(GuideWarning(
                    severity="info", attribute=name, code="INFO",
                    message=(
                        f"open-taxonomy attribute {name!r} has only "
                        f"{len(ga.canonical_examples)} canonical examples; "
                        f"3-10 anchors typically give labelers enough to "
                        f"calibrate"
                    ),
                ))

    return warnings


def format_warnings(warnings: list[GuideWarning]) -> str:
    """Human-readable rendering for CLI output."""
    if not warnings:
        return "(none -- guide validates cleanly against the manifest)"
    lines: list[str] = []
    for w in warnings:
        prefix = "WARN" if w.severity == "warning" else "INFO"
        attr = f"[{w.attribute}] " if w.attribute else ""
        lines.append(f"  {prefix}  {w.code}  {attr}{w.message}")
    return "\n".join(lines)
