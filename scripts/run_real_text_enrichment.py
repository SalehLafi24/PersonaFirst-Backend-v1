"""Run real model-backed text enrichment over the cleaned 49-product catalog.

Pipeline per (product, attribute):
    1. Build the real prompt via get_prompt_for_attribute().
    2. Call the Anthropic API via call_model_json().
    3. Validate + shape the raw JSON into EnrichmentOutput / ProposedValue /
       EnrichedValue.
    4. Collect outputs into products_enriched_real.json.

Modes:
    --dry-run   Print the prompts that would be sent, do NOT call the model.
    --limit N   Process only the first N products.
    --attributes a,b,c
                Comma-separated attribute names to run. Default is the four
                discovery-oriented attributes: activity_type, workout_intensity,
                environment, layering_role.

On any model call or JSON error, the script prints the error and exits
non-zero — no rule-based or hand-coded fallback.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.schemas.attribute_enrichment import (
    AttributeDefinition,
    EnrichedValue,
    EnrichmentOutput,
    EnrichmentSource,
    ProposedValue,
)
from app.services.attribute_enrichment_service import get_prompt_for_attribute
from app.services.model_client import (
    ApiKeyMissingError,
    ModelCallError,
    ModelClientError,
    ModelResponseError,
    call_model_json,
)

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "seed_data"
CATALOG_PATH = ROOT / "products_normalized.json"
OUTPUT_PATH = ROOT / "products_enriched_real.json"

DEFAULT_ATTRIBUTES = [
    "activity_type",
    "workout_intensity",
    "environment",
    "layering_role",
]


def _product_obj(p: dict) -> dict:
    """Shape a normalized product into the object the prompt expects."""
    return {
        "product_id": p["product_id"],
        "style_code": p.get("style_code"),
        "name": p.get("name"),
        "brand": p.get("brand"),
        "gender": p.get("gender"),
        "description": p.get("clean_description") or p.get("description"),
        "functional_categories": p.get("functional_categories") or [],
        "keywords": p.get("keywords") or [],
        "colors": p.get("colors") or [],
    }


def _parse_proposed_values(
    raw_proposed: list,
    allowed_values: list[str] | None,
    value_keys: set,
) -> list[ProposedValue]:
    allowed_set = {v.lower() for v in (allowed_values or [])}
    out: list[ProposedValue] = []
    for item in raw_proposed or []:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if not isinstance(value, str) or not value.strip():
            continue
        if value.lower() in allowed_set:
            continue
        if value in value_keys:
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if confidence < 0.8:
            continue
        evidence = [
            e for e in (item.get("evidence") or [])
            if isinstance(e, str) and e.strip()
        ]
        if not evidence:
            continue
        out.append(ProposedValue(value=value, confidence=confidence, evidence=evidence))
    return out


# For value_mode=single attributes, the enrichment output occasionally yields
# multiple candidates above the confidence threshold (the model may emit both a
# styling axis and a garment shape, e.g. "long_sleeve" + "hoodie").  To honour
# the value_mode=single contract we collapse to exactly one winner via the
# rules below.  Generic-styling values lose to structural-shape values when
# both are present; among remaining candidates the highest confidence wins,
# with deterministic alphabetical tie-break + an ambiguous_<attr> warning.
_GENERIC_STYLING_VALUES: dict[str, set[str]] = {
    "product_type": {"long_sleeve"},
}


def _collapse_to_single(
    attr: AttributeDefinition,
    values: list[EnrichedValue],
    proposed: list[ProposedValue],
    warnings: list[str],
) -> tuple[list[EnrichedValue], list[ProposedValue], list[str]]:
    """Enforce value_mode=single. Returns (values, proposed, warnings)
    with at most one entry total across values+proposed."""
    if attr.value_mode != "single":
        return values, proposed, warnings

    # Combine candidates, tagging origin so we can rebuild the right list.
    candidates: list[tuple[str, str, float]] = []  # (origin, value, confidence)
    for v in values:
        candidates.append(("value", v.value, float(v.confidence)))
    for p in proposed:
        candidates.append(("proposed", p.value, float(p.confidence)))

    if len(candidates) <= 1:
        return values, proposed, warnings

    # Rule 1: drop generic-styling values when a non-generic candidate exists.
    generics = _GENERIC_STYLING_VALUES.get(attr.name, set())
    non_generic = [c for c in candidates if c[1] not in generics]
    if non_generic:
        candidates = non_generic

    # Rule 2: highest confidence wins; rule 3: alphabetical tie-break + warn.
    candidates.sort(key=lambda c: (-c[2], c[1]))
    winner_origin, winner_value, _ = candidates[0]
    confidence_tied = sum(1 for c in candidates if c[2] == candidates[0][2]) > 1
    new_warnings = list(warnings)
    if confidence_tied:
        warning = f"ambiguous_{attr.name}"
        if warning not in new_warnings:
            new_warnings.append(warning)

    new_values: list[EnrichedValue] = []
    new_proposed: list[ProposedValue] = []
    if winner_origin == "value":
        new_values = [v for v in values if v.value == winner_value][:1]
    else:
        new_proposed = [p for p in proposed if p.value == winner_value][:1]
    return new_values, new_proposed, new_warnings


def _build_enrichment_output(attr: AttributeDefinition, raw: dict) -> EnrichmentOutput:
    values: list[EnrichedValue] = []
    for item in raw.get("values") or []:
        values.append(
            EnrichedValue(
                value=item.get("value"),
                confidence=float(item.get("confidence", 0.0)),
                evidence=list(item.get("evidence") or []),
                reasoning_mode=item.get("reasoning_mode"),
                source=EnrichmentSource.TEXT,
                contributing_sources=[EnrichmentSource.TEXT],
            )
        )
    value_keys = {v.value for v in values if isinstance(v.value, str)}
    proposed = _parse_proposed_values(
        raw.get("proposed_values") or [],
        attr.allowed_values,
        value_keys,
    )
    warnings = list(raw.get("warnings") or [])
    values, proposed, warnings = _collapse_to_single(attr, values, proposed, warnings)
    return EnrichmentOutput(
        attribute_name=raw.get("attribute_name") or attr.name,
        attribute_class=raw.get("attribute_class") or attr.class_name,
        values=values,
        proposed_values=proposed,
        warnings=warnings,
        source=EnrichmentSource.TEXT,
    )


def _load_catalog() -> list[dict]:
    if not CATALOG_PATH.exists():
        raise FileNotFoundError(f"Catalog not found: {CATALOG_PATH}")
    with CATALOG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _load_attribute_defs(names: list[str]) -> list[AttributeDefinition]:
    with (SEED_DIR / "attribute_definitions.json").open(encoding="utf-8") as f:
        raw = json.load(f)
    by_name = {d["name"]: d for d in raw}
    missing = [n for n in names if n not in by_name]
    if missing:
        raise KeyError(f"Attribute(s) not found in seed_data: {missing}")
    return [AttributeDefinition(**by_name[n]) for n in names]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="Print prompts instead of calling the model.")
    p.add_argument("--limit", type=int, default=None,
                   help="Process only the first N products.")
    p.add_argument("--attributes", type=str, default=",".join(DEFAULT_ATTRIBUTES),
                   help="Comma-separated attribute names to enrich.")
    p.add_argument("--model", type=str, default=None,
                   help="Override the Claude model id (default: claude-sonnet-4-5).")
    p.add_argument("--out", type=str, default=str(OUTPUT_PATH),
                   help="Output JSON path.")
    return p.parse_args()


def _dry_run(products: list[dict], attrs: list[AttributeDefinition]) -> None:
    print(f"[DRY RUN] {len(products)} product(s) x {len(attrs)} attribute(s) "
          f"= {len(products) * len(attrs)} prompts would be sent.\n")
    shown = 0
    for p in products:
        obj = _product_obj(p)
        for attr in attrs:
            prompt = get_prompt_for_attribute(attr, obj)
            print("=" * 78)
            print(f"PROMPT  product={p['style_code']}  attribute={attr.name}  "
                  f"class={attr.class_name}")
            print("=" * 78)
            print(prompt)
            print()
            shown += 1
    print(f"[DRY RUN] printed {shown} prompt(s); no model call made.")


def _run_real(
    products: list[dict],
    attrs: list[AttributeDefinition],
    *,
    model: str | None,
    out_path: Path,
) -> None:
    report: list[dict] = []
    total = len(products) * len(attrs)
    done = 0
    for p in products:
        obj = _product_obj(p)
        per_attr: dict[str, dict] = {}
        for attr in attrs:
            done += 1
            prompt = get_prompt_for_attribute(attr, obj)
            print(f"[{done:>3}/{total}] {p['style_code']} :: {attr.name} ... ",
                  end="", flush=True)
            try:
                raw = call_model_json(prompt, model=model)
            except ModelClientError as e:
                print("ERROR")
                print(f"\nStopping. {type(e).__name__}: {e}", file=sys.stderr)
                sys.exit(2)
            output = _build_enrichment_output(attr, raw)
            per_attr[attr.name] = output.model_dump(mode="json")
            print(f"values={len(output.values)} proposed={len(output.proposed_values)}")
        report.append({
            "product_id": p["product_id"],
            "style_code": p["style_code"],
            "name": p["name"],
            "clean_description": p.get("clean_description"),
            "functional_categories": p.get("functional_categories"),
            "attributes": per_attr,
        })

    with out_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {len(report)} enriched products to {out_path}")

    print("\n=== 5 SAMPLE OUTPUTS ===")
    for entry in report[:5]:
        print(f"\n-- {entry['style_code']} | {entry['name']}")
        print(f"   clean_description     : {entry['clean_description']}")
        print(f"   functional_categories : {entry['functional_categories']}")
        for name, out in entry["attributes"].items():
            vals = out.get("values", [])
            props = out.get("proposed_values", [])
            warns = out.get("warnings", [])
            val_str = (
                ", ".join(f"{v['value']}({v['confidence']})" for v in vals)
                if vals else "-"
            )
            prop_str = (
                ", ".join(f"{pv['value']}({pv['confidence']})" for pv in props)
                if props else "-"
            )
            print(f"   {name:18s} values={val_str}  proposed={prop_str}"
                  f"{'  warnings=' + ','.join(warns) if warns else ''}")


def main() -> None:
    args = _parse_args()
    attr_names = [a.strip() for a in args.attributes.split(",") if a.strip()]
    attrs = _load_attribute_defs(attr_names)

    products = _load_catalog()
    if args.limit is not None:
        products = products[: args.limit]

    if args.dry_run:
        _dry_run(products, attrs)
        return

    try:
        _run_real(products, attrs, model=args.model, out_path=Path(args.out))
    except ApiKeyMissingError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(3)
    except (ModelCallError, ModelResponseError) as e:
        # _run_real already handles per-call failures; this catches edge cases.
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
