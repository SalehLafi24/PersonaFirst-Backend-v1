"""Mumz import validation v2 — batched LLM enrichment, same engine
pipeline.

Same engine path as v1 (`run_mumz_product_type_clustering.py`):
    record_events_from_output → normalize_proposed_value → refresh_aggregates

What v2 changes (orchestration only):
    - Batched LLM calls (one prompt covers N products instead of one).
    - Resume support keyed on existing ProposedAttributeValueEvent rows
      for the workspace + attribute_name = "product_type".
    - Per-batch commits instead of per-product commits.
    - Flushed progress lines with elapsed / ETA.
    - Missing-product retry: if the batch response omits a product_id,
      retry those products as a smaller batch (size 1 by default) before
      moving on.

What v2 does NOT change:
    - app/services/recommendation_service.py
    - app/services/proposed_attribute_value_service.py
    - app/services/proposed_value_normalizer.py
    - app/services/attribute_enrichment_service.py
    - app/services/attribute_taxonomy_service.py
    - Any threshold or scoring constant.
    - The ProposedAttributeValueEvent / ProposedAttributeValueAggregate
      schema or write path.

Note on the model call: this script calls the Anthropic SDK directly so
the response can be a top-level JSON ARRAY. `model_client.call_model_json`
intentionally rejects arrays (returns dict only), and modifying it would
violate the "do not change engine services" rule. The scripts/run_*
layer is orchestration, not an engine service, so calling the SDK
directly here is consistent with v1's separation of concerns.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from app.core.database import SessionLocal
from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import (
    ProposedAttributeValueAggregate,
    ProposedAttributeValueEvent,
)
from app.models.workspace import Workspace
from app.schemas.attribute_enrichment import (
    EnrichedValue,
    EnrichmentOutput,
    EnrichmentSource,
    ProposedValue,
)
from app.services.proposed_attribute_value_service import (
    PROMOTION_MIN_AVG_CONFIDENCE,
    PROMOTION_MIN_DISTINCT_PRODUCTS,
    PROMOTION_MIN_PROPOSAL_COUNT,
    promotion_readiness,
    record_events_from_output,
    refresh_aggregates,
)

SAMPLE_PATH = ROOT / "seed_data" / "mumz_sample.json"
DEFAULT_WORKSPACE_SLUG = "mumzworld_v2_sample"
ATTRIBUTE = "product_type"
DEFAULT_MODEL = "claude-sonnet-4-5"
MAX_TOKENS_PER_BATCH = 6000

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


# ---------------------------------------------------------------------------
# Batch prompt
# ---------------------------------------------------------------------------

_BATCH_INSTRUCTIONS = """\
You are an attribute extraction engine.

You will receive a JSON array of products. For EACH product, infer the
`product_type` — a BROAD, REUSABLE PRODUCT FAMILY suitable for grouping
many similar products in a recommendation system.

product_type answers:
    "What BROAD kind of product is this?"
It does NOT answer:
    "What exact item is this?"

A good product_type:
    - groups many recommendation-relevant products under one label
    - uses a generic noun or compound noun, not the literal item name
    - is reusable across brands, variants, sizes, and minor sub-types
    - is the kind of label a merchandiser would use to define a section,
      not the kind of label that names a single SKU

EXAMPLES (input concept → product_type):
    monster truck         → vehicle_toy
    remote control car    → vehicle_toy
    karaoke microphone    → musical_toy
    acoustic guitar       → musical_toy
    arcade machine        → game
    chess set             → game
    plush teddy bear      → plush_toy
    stuffed elephant      → plush_toy
    napkin                → party_supply
    party hat             → party_supply
    foundation            → makeup
    nail polish           → makeup
    lipstick              → makeup
    watch                 → accessory
    bracelet              → accessory
    baby bottle           → bottle
    sippy cup             → bottle
    stroller              → stroller
    diaper bag            → baby_bag
    swaddle blanket       → blanket
    receiving blanket     → blanket
    picture book          → book
    activity book         → book
    jigsaw puzzle         → puzzle
    learning puzzle       → puzzle
    dollhouse             → playset
    dress-up costume      → playset
    building blocks       → construction_toy
    magnetic tiles        → construction_toy
    crib                  → furniture
    high chair            → furniture
    body wash             → bath_product
    shampoo               → bath_product
    pacifier              → pacifier
    teether               → teether
    baby food jar         → baby_food
    formula               → baby_food
    vitamin supplement    → supplement
    backpack              → bag
    lunchbox              → bag
    coloring set          → art_supply
    crayons               → art_supply

VALUE RULES
- LOWERCASE only.
- snake_case for multi-word values: "vehicle_toy", "party_supply",
  "art_supply", "baby_bag". Do NOT use spaces, slashes, or hyphens for
  separators.
- One or two tokens strongly preferred. Three-token values are allowed
  only when no shorter family fits. Avoid four or more tokens.
- NEVER include brand names, model codes, sizes, colors, age tags, or
  the literal item name (no "monster_truck", "karaoke_microphone",
  "spellbound_potion_napkin").
- NEVER return compound/alternated values ("toy/game", "shirt_and_pants").
  If two independent broad families are both clearly supported, return
  them as TWO separate entries in proposed_values.
- A product SHOULD have one proposed_value. Return two only when two
  broad families are each independently and strongly justified by the
  product data.
- Drop values with confidence < 0.8.
- Each `evidence` entry must quote exact text from the product input
  (name, categories, or keywords). No paraphrasing.

OUTPUT FORMAT (STRICT)
Return a SINGLE JSON ARRAY. One element per input product, matched by
`product_id`. Every input product_id MUST appear exactly once. Order
does not matter.

[
  {
    "product_id": "<exactly as input>",
    "proposed_values": [
      {
        "value": "<broad product family, snake_case if multi-word>",
        "confidence": <0.8..1.0>,
        "evidence": ["<exact phrase from name / categories / keywords>"]
      }
    ],
    "warnings": []
  }
]

NO-SIGNAL CASE
- If signal is absent, contradictory, or weak, return:
    "proposed_values": [],
    "warnings": ["no_supported_value_found"]

OUTPUT STRICTNESS
- Return ONLY the JSON array. No markdown fences. No commentary.
- Do not invent product_ids. Do not duplicate them.
"""

# Maintenance note: this prompt block is intentionally inline in v2 only.
# When the broad-family product_type enrichment becomes the engine's
# canonical product_type prompt, lift this block into
# app/services/attribute_enrichment_service.py (or a sibling builder)
# so v1/v2/future callers share one source of truth and cannot drift.


def _build_batch_prompt(products: list[dict]) -> str:
    payload = [
        {
            "product_id": p["product_id"],
            "name": p["name"],
            "brand": p.get("brand", ""),
            "categories": (p.get("attributes") or {}).get("category_path") or [],
            "keywords": (p.get("attributes") or {}).get("keywords") or [],
        }
        for p in products
    ]
    return (
        _BATCH_INSTRUCTIONS
        + "\nPRODUCTS:\n"
        + json.dumps(payload, indent=2, ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# Direct SDK call (accepts JSON arrays)
# ---------------------------------------------------------------------------

def _call_model_array(prompt: str, *, model: str, max_tokens: int) -> list[dict]:
    """Issue a single Claude call expecting a top-level JSON array.
    Mirrors model_client.call_model_json's error-surfacing behavior but
    relaxes the dict-only constraint."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.0,
        system=(
            "You are a structured-output engine. Respond with a single "
            "valid JSON array and nothing else. No markdown fences. "
            "No commentary."
        ),
        messages=[{"role": "user", "content": prompt}],
    )
    parts: list[str] = []
    for block in (resp.content or []):
        t = getattr(block, "text", None)
        if isinstance(t, str):
            parts.append(t)
    text = "".join(parts).strip()
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Invalid JSON in model response: {e.msg} at pos {e.pos}. "
            f"First 400 chars: {text[:400]!r}"
        ) from e
    if not isinstance(parsed, list):
        raise ValueError(
            f"Expected JSON array; got {type(parsed).__name__}. "
            f"First 400 chars: {text[:400]!r}"
        )
    return parsed


# ---------------------------------------------------------------------------
# Per-item conversion → EnrichmentOutput → engine path
# ---------------------------------------------------------------------------

def _parse_proposed(raw: list) -> list[ProposedValue]:
    out: list[ProposedValue] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        v = item.get("value")
        if not isinstance(v, str) or not v.strip():
            continue
        if v in seen:
            continue
        try:
            conf = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if conf < 0.8:
            continue
        evidence = [
            e for e in (item.get("evidence") or [])
            if isinstance(e, str) and e.strip()
        ]
        if not evidence:
            continue
        seen.add(v)
        out.append(ProposedValue(value=v, confidence=conf, evidence=evidence))
    return out


def _build_enrichment_output(item: dict) -> EnrichmentOutput:
    return EnrichmentOutput(
        attribute_name=ATTRIBUTE,
        attribute_class="compatibility",
        values=[],
        proposed_values=_parse_proposed(item.get("proposed_values") or []),
        warnings=list(item.get("warnings") or []),
        source=EnrichmentSource.TEXT,
    )


# ---------------------------------------------------------------------------
# Workspace setup / resume
# ---------------------------------------------------------------------------

def _ensure_workspace(db, slug: str) -> Workspace:
    ws = db.query(Workspace).filter(Workspace.slug == slug).first()
    if ws is None:
        ws = Workspace(slug=slug, name=f"Mumz validation {slug}")
        db.add(ws)
        db.flush()
        db.commit()
    return ws


def _processed_product_ids(db, ws_id: int) -> set[str]:
    """Resume signal: any product with at least one product_type event is
    considered processed. Products that produced no proposals during a
    prior run will be re-attempted (acceptable cost vs. complexity)."""
    rows = (
        db.query(ProposedAttributeValueEvent.product_id)
        .filter(
            ProposedAttributeValueEvent.workspace_id == ws_id,
            ProposedAttributeValueEvent.attribute_name == ATTRIBUTE,
        )
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def _seed_missing_products(db, ws_id: int, sample: list[dict]) -> int:
    """Insert Product rows that don't yet exist in the workspace."""
    existing_pids = {
        p[0] for p in db.query(Product.product_id).filter(
            Product.workspace_id == ws_id
        ).all()
    }
    n = 0
    for p in sample:
        if p["product_id"] in existing_pids:
            continue
        db.add(Product(
            workspace_id=ws_id,
            product_id=p["product_id"],
            sku=p["product_id"],
            name=(p["name"] or p["product_id"])[:255],
            group_id=p.get("group_id") or None,
        ))
        n += 1
    db.flush()
    db.commit()
    return n


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------

def _process_batch(
    db, ws_id: int, batch: list[dict],
    *, model: str, max_tokens: int,
) -> tuple[int, int, list[str]]:
    """Run one batch through the engine pipeline. Returns
    (events_created, products_with_proposals, missing_pids)."""
    expected = {p["product_id"] for p in batch}
    by_pid = {p["product_id"]: p for p in batch}
    prompt = _build_batch_prompt(batch)
    try:
        items = _call_model_array(prompt, model=model, max_tokens=max_tokens)
    except Exception as e:
        # Whole-batch failure → caller will retry with a smaller batch.
        print(f"  [batch] model error: {type(e).__name__}: {e}", flush=True)
        return 0, 0, list(expected)

    events_created = 0
    answered: set[str] = set()
    products_with_proposals = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        pid = item.get("product_id")
        if pid not in by_pid or pid in answered:
            continue
        answered.add(pid)
        output = _build_enrichment_output(item)
        evs = record_events_from_output(
            db, workspace_id=ws_id, product_id=pid, output=output,
        )
        if evs:
            events_created += len(evs)
            products_with_proposals += 1
    missing = sorted(expected - answered)
    return events_created, products_with_proposals, missing


def _run(
    db, ws_id: int, to_process: list[dict],
    *, batch_size: int, model: str,
):
    total = len(to_process)
    n_batches = (total + batch_size - 1) // batch_size
    events_total = 0
    products_with_proposals = 0
    missing_total = 0
    t0 = time.time()
    for bi in range(n_batches):
        batch = to_process[bi * batch_size : (bi + 1) * batch_size]
        evts, with_props, missing = _process_batch(
            db, ws_id, batch,
            model=model, max_tokens=MAX_TOKENS_PER_BATCH,
        )
        events_total += evts
        products_with_proposals += with_props
        # Per-batch retry for missing IDs, individually (size 1).
        if missing:
            print(f"  [retry] batch {bi+1}: {len(missing)} missing → individual retry: "
                  f"{missing[:5]}{'...' if len(missing) > 5 else ''}", flush=True)
            by_pid = {p["product_id"]: p for p in batch}
            still_missing: list[str] = []
            for pid in missing:
                p = by_pid.get(pid)
                if p is None:
                    continue
                evts2, with_props2, miss2 = _process_batch(
                    db, ws_id, [p],
                    model=model, max_tokens=2000,
                )
                events_total += evts2
                products_with_proposals += with_props2
                still_missing.extend(miss2)
            if still_missing:
                missing_total += len(still_missing)
                print(f"  [retry] batch {bi+1}: still missing after retry: "
                      f"{still_missing}", flush=True)
        # Per-batch commit
        db.commit()
        elapsed = time.time() - t0
        rate_p = ((bi + 1) * batch_size) / elapsed if elapsed else 0.0
        eta = (n_batches - (bi + 1)) * elapsed / (bi + 1) if (bi + 1) else 0.0
        elapsed_min = elapsed / 60.0
        eta_min = eta / 60.0
        print(
            f"[progress] batch {bi+1}/{n_batches}\n"
            f"products processed: {min((bi+1) * batch_size, total)}\n"
            f"events created: {events_total}\n"
            f"elapsed: {elapsed_min:.1f}m\n"
            f"eta: {eta_min:.1f}m\n",
            flush=True,
        )
    return {
        "elapsed_s": time.time() - t0,
        "events_total": events_total,
        "products_with_proposals": products_with_proposals,
        "missing_total": missing_total,
        "n_batches": n_batches,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-size", type=int, default=30, help="20–50 recommended")
    ap.add_argument("--limit", type=int, default=None, help="cap input to this many products")
    ap.add_argument("--resume", type=str, default="true",
                    help="true|false; default true (skip already-processed product_ids)")
    ap.add_argument("--workspace", type=str, default=DEFAULT_WORKSPACE_SLUG)
    ap.add_argument("--model", type=str, default=DEFAULT_MODEL)
    args = ap.parse_args()

    if args.batch_size < 1 or args.batch_size > 80:
        raise SystemExit("batch-size must be between 1 and 80")
    resume = str(args.resume).strip().lower() in ("1", "true", "yes", "y")

    sample = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    if args.limit is not None:
        sample = sample[: args.limit]
    print(f"[{datetime.utcnow().isoformat()}Z] sample loaded: {len(sample)} products  "
          f"(workspace={args.workspace}, batch_size={args.batch_size}, "
          f"resume={resume}, model={args.model})", flush=True)

    db = SessionLocal()
    try:
        ws = _ensure_workspace(db, args.workspace)
        seeded = _seed_missing_products(db, ws.id, sample)
        print(f"  seeded products: {seeded} new (workspace_id={ws.id})", flush=True)

        already = _processed_product_ids(db, ws.id) if resume else set()
        if already:
            print(f"  resume: skipping {len(already)} already-processed product_ids",
                  flush=True)
        to_process = [p for p in sample if p["product_id"] not in already]
        print(f"  to process: {len(to_process)} products", flush=True)
        if not to_process:
            print("  nothing to enrich; refreshing aggregates only", flush=True)
            stats = {"elapsed_s": 0.0, "events_total": 0,
                     "products_with_proposals": 0, "missing_total": 0,
                     "n_batches": 0}
        else:
            stats = _run(db, ws.id, to_process,
                         batch_size=args.batch_size, model=args.model)

        # Refresh aggregates (engine call, unchanged).
        aggs = refresh_aggregates(db, workspace_id=ws.id, attribute_name=ATTRIBUTE)
        aggs.sort(key=lambda a: (-a.proposal_count, a.canonical_value))
        db.commit()

        # Validation output.
        print()
        print("=" * 80)
        print("VALIDATION OUTPUT")
        print("=" * 80)
        old_throughput = 12.0  # products per minute (v1 measured)
        elapsed_min = max(stats["elapsed_s"] / 60.0, 0.001)
        new_throughput = (
            len(to_process) / elapsed_min if to_process else 0.0
        )
        speedup = (new_throughput / old_throughput) if new_throughput else 0.0

        print(f"  workspace                      : {args.workspace}")
        print(f"  batch_size used                : {args.batch_size}")
        print(f"  total products in input        : {len(sample)}")
        print(f"  resumed (skipped)              : {len(already)}")
        print(f"  processed this run             : {len(to_process)}")
        print(f"  products with ≥1 proposal      : {stats['products_with_proposals']}")
        print(f"  events created this run        : {stats['events_total']}")
        print(f"  batches issued                 : {stats['n_batches']}")
        print(f"  missing-after-retry (dropped)  : {stats['missing_total']}")
        print(f"  total runtime                  : {stats['elapsed_s']:.1f}s "
              f"({elapsed_min:.2f}m)")
        print(f"  old throughput (v1)            : ~{old_throughput:.1f} products/min")
        print(f"  new throughput (v2)            : {new_throughput:.1f} products/min "
              f"({speedup:.1f}× v1)")
        print(f"  aggregates after refresh       : {len(aggs)}")
        print(f"  promotion thresholds (unchanged): "
              f"count≥{PROMOTION_MIN_PROPOSAL_COUNT}, "
              f"distinct≥{PROMOTION_MIN_DISTINCT_PRODUCTS}, "
              f"avg_conf≥{PROMOTION_MIN_AVG_CONFIDENCE}")
        n_ready = sum(1 for a in aggs if promotion_readiness(a).ready)
        print(f"  aggregates fully ready         : {n_ready} (NOT approving — "
              f"per task constraint)")
        print()
        print("  Top 10 cluster_keys (engine output, unmodified):")
        for a in aggs[:10]:
            print(f"    {a.cluster_key[:30]:<32} count={a.proposal_count:>4} "
                  f"distinct={a.distinct_product_count:>4} "
                  f"avg_conf={a.avg_confidence:.3f}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
