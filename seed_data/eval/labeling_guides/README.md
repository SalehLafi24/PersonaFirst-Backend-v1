# Labeling guides

Per-workspace YAML guides explain what each attribute value MEANS in that
workspace. Labelers use them; future tools (LLM prompt builders, eval
scripts, the Explorer's hover-text) can also consume them.

## File convention

```
seed_data/eval/labeling_guides/{workspace_slug}.yaml
```

One file per workspace. Filename = workspace slug. Schema is identical
across files; contents (attributes, values, definitions, examples,
edge cases) are workspace-specific.

## Schema (v1)

Top-level fields:

| field | required | what it carries |
|---|---|---|
| `version` | yes | guide schema version (`"1.0"` today) |
| `workspace_slug` | yes | matches the directory + DB row |
| `manifest_version_at_authoring` | recommended | catches drift later |
| `authored_at` | recommended | ISO date |
| `authors` | recommended | list of names / initials |
| `domain` | recommended | one-line domain context |
| `labeler_brief` | yes | top-level instruction for new labelers (markdown allowed inside the multiline string) |
| `attributes` | yes | mapping of attribute name → guide block |

Per-attribute fields:

| field | required | what it carries |
|---|---|---|
| `description` | yes | what this attribute means in this workspace |
| `cardinality` | yes | `single` or `multi`; mirrors manifest |
| `taxonomy_kind` | yes | `closed` or `open`; mirrors manifest |
| `out_of_vocabulary_policy` | yes | what a labeler does when no value fits |
| `edge_cases` | yes | cross-cutting rules (markdown allowed) |
| `values` | closed only | mapping of value → block; one entry per manifest allowed_value |
| `canonical_examples` | open only | mapping of value → block; 8-10 anchor values (NOT exhaustive) |

Per-value block (used inside both `values:` and `canonical_examples:`):

| field | required | what it carries |
|---|---|---|
| `definition` | yes | precise, short definition |
| `positive_examples` | recommended | 1-3 product names that ARE this value |
| `negative_examples` | recommended | 1-3 cases that LOOK like this value but aren't |
| `notes` | optional | free-form prose for clarification |

## Closed vs open: critical distinction

**Closed taxonomies** (e.g., `age_group`, `gender`, `use_case`):
- Manifest declares the values in `taxonomy.allowed_values`.
- Guide MUST document EVERY manifest value in the `values:` block.
- The validator warns if the manifest declares a value the guide doesn't
  define (W004).

**Open taxonomies** (e.g., `product_type`, `brand`):
- Manifest's `allowed_values` is empty; values are discovered by the
  attribute engine and approved through `taxonomy_admin`.
- Guide does NOT enumerate every value (the catalog has 49+ for
  `product_type`).
- Guide describes the attribute + 8-10 canonical examples + edge cases.
- The full value list lives in `AttributeAllowedValue` rows; the per-value
  semantics (when not in canonical_examples) live in the review notes
  for those rows.

The labeling guide is a **CONSUMER of vocabulary**, not a producer of
it. Values come from the manifest (closed) or the catalog + taxonomy_admin
(open). The guide explains them.

## Out-of-vocabulary policy

Three documented patterns:

- `leave_null` — closed taxonomy with no fitting value. Don't guess.
- `closest_value_with_note` — open taxonomy with a near-fit. Pick closest;
  flag in `notes`.
- `propose_via_taxonomy_admin` — open taxonomy with a genuinely new
  value. Mark `null`; raise a separate proposal through the admin
  pipeline.

**Labelers never extend the value set during labeling.** That breaks
consistency across labelers and breaks the gold set (precision against a
value that didn't exist when other labels were assigned is meaningless).

## Recommended `_weight_reason`-style tags

The same tagging convention used elsewhere in the system applies to guide
authorship:

- `initial_v1` — first hand-written guide for a workspace
- `revised_<context>` — substantive update (e.g., `revised_post_review_2026Q3`)
- `migrated_from_<source>` — converted from a previous documentation form

These are informational; the `authored_at` + `authors` fields are the
audit trail.

## Loading and validating

```python
from app.services.eval import (
    load_labeling_guide,
    validate_guide_against_manifest,
    format_warnings,
)
from app.services.attribute_engine import load_manifest

guide = load_labeling_guide("mumzworld_v3_sample")
manifest = load_manifest()
warnings = validate_guide_against_manifest(guide, manifest)
print(format_warnings(warnings))
```

Validator codes:

| code | meaning |
|---|---|
| W001 | manifest declares an attribute the guide doesn't document |
| W002 | guide documents an attribute not in the manifest |
| W003 | guide value not in the manifest's allowed_values (closed only) |
| W004 | manifest value missing from guide (closed only) |
| W005 | open-taxonomy attribute used `values:` instead of `canonical_examples:` |
| W006 | guide cardinality differs from manifest cardinality |
| W007 | guide taxonomy_kind differs from manifest taxonomy.kind |
| INFO | open-taxonomy attribute has fewer than 3 canonical examples |

Warnings, not errors. The guide can lag the manifest temporarily; the
warnings tell you exactly where.

## Onboarding a new workspace

1. Copy a guide from a workspace in a similar industry as a starting point.
2. Replace `workspace_slug`, `domain`, `authored_at`, `authors`.
3. Walk the manifest's persona-relevant + open attributes; document each.
4. Run the validator. Fix any W001 / W004 / W005 / W007 warnings before
   shipping.
5. Hand the YAML to a labeler with the gold-set sample file.

## What this guide is NOT for

- It is NOT a registry of values. The manifest + AttributeAllowedValue is.
- It is NOT a permission system. Authorisation lives in the API layer.
- It is NOT used for runtime recommendation logic today. Future
  consumers (LLM prompts, Explorer hover-text) will read it; current
  recommenders don't.
- It is NOT versioned per-attribute. The whole guide has one `version`
  field; bump when you make breaking changes to definitions.
