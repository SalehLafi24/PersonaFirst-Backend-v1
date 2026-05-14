"""Ingest dispatcher: runs input modes per the manifest, with precedence.

For each attribute, three mode handlers are available:

    csv_direct      delegates to csv_mapping_import_service.import_csv_with_mapping
                    using a single MappingRule built from the manifest. Goes
                    through attribute_normalizer_service automatically.
    regex_extract   scans declared product fields with regex patterns; for
                    each match emits a ProposedAttributeValueEvent with the
                    pattern's canonical value, confidence, and quoted match
                    as evidence.
    llm_evidence    builds the standard prompt via attribute_enrichment_service
                    and routes the LLM's proposed_values through
                    record_events_from_output (with light defense-in-depth
                    normalization via attribute_normalizer_service).

Precedence (cardinality=single): if a product already has at least one event
for this attribute from a higher-precedence mode in *this run*, lower-precedence
modes skip it. Across runs, callers are responsible for not duplicating work
(idempotency comes from the pipeline runner refreshing aggregates).
"""
from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import ProposedAttributeValueEvent
from app.schemas.attribute_enrichment import (
    AttributeBehavior,
    AttributeDefinition,
    EnrichmentOutput,
    EnrichmentSource,
    ProposedValue,
    TargetingMode,
)
from app.services.attribute_engine.manifest import AttributeManifestEntry
from app.services.attribute_normalizer_service import normalize_cell
from app.services.csv_mapping_import_service import (
    MAPPING_MODE_DIRECT,
    MappingRule,
    import_csv_with_mapping,
)
from app.services.proposed_attribute_value_service import record_events_from_output


_log = logging.getLogger("personafirst.attribute_engine.dispatcher")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ModeResult:
    mode: str
    events_created: int = 0
    products_with_event: set[str] = field(default_factory=set)
    objects_processed: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class DispatcherResult:
    attribute_name: str
    started_at: str
    ended_at: str
    per_mode: dict[str, ModeResult] = field(default_factory=dict)
    total_events_created: int = 0
    total_products_decided: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_attribute_definition_for_llm(
    entry: AttributeManifestEntry,
) -> AttributeDefinition:
    """Compose an AttributeDefinition from the manifest for the LLM prompt builder."""
    return AttributeDefinition(
        name=entry.name,
        object_type="product",
        class_name=entry.llm_evidence.class_name if entry.llm_evidence else "contextual_semantic",
        value_mode="single" if entry.taxonomy.cardinality == "single" else "multi",
        allowed_values=list(entry.taxonomy.allowed_values),
        description=f"Attribute managed by attribute_engine ({entry.name})",
        evidence_sources=["text"],
        behavior=AttributeBehavior(
            taxonomy_sensitive=(entry.taxonomy.kind == "closed"),
            ordered_values=False,
            can_propose_values=(entry.taxonomy.kind == "open"),
            multi_value_allowed=(entry.taxonomy.cardinality == "multi"),
            prefer_conservative_inference=True,
        ),
        targeting_mode=TargetingMode.CATEGORICAL_AFFINITY,
    )


def _products_with_events(
    db: Session, workspace_id: int, attribute_name: str,
) -> set[str]:
    """Return the set of product_ids that already have any event for this
    attribute. Used to enforce cross-mode precedence on cardinality=single."""
    rows = db.query(ProposedAttributeValueEvent.product_id).filter(
        ProposedAttributeValueEvent.workspace_id == workspace_id,
        ProposedAttributeValueEvent.attribute_name == attribute_name,
    ).distinct().all()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Mode: csv_direct
# ---------------------------------------------------------------------------

def _run_csv_direct(
    *, db: Session, workspace_id: int, entry: AttributeManifestEntry,
    repo_root: Path, skip_product_ids: set[str],
) -> ModeResult:
    """Delegate to import_csv_with_mapping. Filters to existing products and
    rows whose product_id is not in skip_product_ids."""
    res = ModeResult(mode="csv_direct")
    if entry.csv_direct is None:
        res.notes.append("csv_direct not configured")
        return res

    csv_path = (repo_root / entry.csv_direct.csv_path).resolve()
    if not csv_path.exists():
        res.notes.append(f"csv not found: {csv_path}")
        return res

    # Workspace product_ids (we do NOT create new products here; the engine
    # operates on the existing catalog).
    existing_pids: set[str] = {
        p.product_id for p in db.query(Product).filter(
            Product.workspace_id == workspace_id
        ).all()
    }

    pid_col = entry.csv_direct.product_id_column
    rules = [
        MappingRule(
            source_column=col,
            target_attribute=entry.name,
            mode=MAPPING_MODE_DIRECT,
        )
        for col in entry.csv_direct.source_columns
    ]

    rows: list[dict] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pid = (row.get(pid_col) or "").strip()
            if not pid or pid not in existing_pids:
                continue
            if pid in skip_product_ids:
                continue
            rows.append(row)

    res.objects_processed = len(rows)
    if not rows:
        res.notes.append("no eligible rows in csv")
        return res

    before_pids = _products_with_events(db, workspace_id, entry.name)

    # Snapshot event count before the call so we can compute the diff.
    pre_count = db.query(ProposedAttributeValueEvent).filter(
        ProposedAttributeValueEvent.workspace_id == workspace_id,
        ProposedAttributeValueEvent.attribute_name == entry.name,
    ).count()

    import_csv_with_mapping(
        db=db, workspace_id=workspace_id,
        rows=rows, mapping_rules=rules,
        attribute_definitions={},
        product_id_column=pid_col,
        name_column="name", sku_column="sku",
        model_call=None,
    )

    post_count = db.query(ProposedAttributeValueEvent).filter(
        ProposedAttributeValueEvent.workspace_id == workspace_id,
        ProposedAttributeValueEvent.attribute_name == entry.name,
    ).count()
    res.events_created = post_count - pre_count

    after_pids = _products_with_events(db, workspace_id, entry.name)
    res.products_with_event = after_pids - before_pids
    return res


# ---------------------------------------------------------------------------
# Mode: regex_extract
# ---------------------------------------------------------------------------

def _scan_text_for_patterns(
    text: str,
    compiled: list[tuple[re.Pattern, str, float, tuple[str, ...]]],
    candidate_product_type: str | None = None,
) -> list[tuple[str, float, str]]:
    """Return list of (canonical_value, confidence, evidence_quote).
    Patterns are evaluated in declaration order; first match wins per
    canonical value (so two patterns mapping to the same canonical only
    produce one event).

    Phase A: each compiled pattern carries a tuple of `block_when_pt`
    product_types. If the candidate's existing product_type is in that
    set, the pattern is skipped (best-effort guard against
    over-extraction from bundled-component tokens, e.g., "Plush" in an
    apparel name).
    """
    out: list[tuple[str, float, str]] = []
    seen_values: set[str] = set()
    for compiled_pat, value, conf, block_when_pt in compiled:
        if value in seen_values:
            continue
        # Pattern guard: skip if candidate's product_type is in the
        # block list. When candidate_product_type is None, the guard is
        # silently ignored (best-effort -- if product_type isn't known,
        # we can't enforce the guard).
        if (block_when_pt and candidate_product_type
                and candidate_product_type in block_when_pt):
            continue
        m = compiled_pat.search(text)
        if m is None:
            continue
        out.append((value, conf, m.group(0)))
        seen_values.add(value)
    return out


def _run_regex_extract(
    *, db: Session, workspace_id: int, entry: AttributeManifestEntry,
    skip_product_ids: set[str],
) -> ModeResult:
    res = ModeResult(mode="regex_extract")
    if entry.regex_extract is None or not entry.regex_extract.patterns:
        res.notes.append("regex_extract not configured")
        return res

    compiled: list[tuple[re.Pattern, str, float, tuple[str, ...]]] = []
    for p in entry.regex_extract.patterns:
        try:
            compiled.append((
                re.compile(p.pattern), p.value, p.confidence,
                p.block_when_product_type_in,
            ))
        except re.error as e:
            res.notes.append(f"bad pattern {p.pattern!r}: {e}")
            res.errors += 1
    if not compiled:
        return res

    # Phase A: bulk-load existing product_type per candidate so
    # block_when_product_type_in pattern guards can be enforced.
    products_for_lookup = db.query(Product).filter(
        Product.workspace_id == workspace_id
    ).all()
    pt_by_db_id: dict[int, str] = {}
    has_any_block = any(
        p.block_when_product_type_in for p in entry.regex_extract.patterns
    )
    if has_any_block:
        pt_rows = (
            db.query(ProductAttribute.product_id, ProductAttribute.attribute_value)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == workspace_id,
                    ProductAttribute.attribute_id == "product_type")
            .all()
        )
        pt_by_db_id = {db_id: val for db_id, val in pt_rows}

    # Validate that pattern values are in allowed_values for closed taxonomy.
    if entry.taxonomy.kind == "closed":
        allowed = set(entry.taxonomy.allowed_values)
        for _, val, _, _ in compiled:
            if val not in allowed:
                res.notes.append(
                    f"pattern value {val!r} not in allowed_values "
                    f"for closed taxonomy {entry.name!r}"
                )

    products = db.query(Product).filter(
        Product.workspace_id == workspace_id
    ).all()

    fields = entry.regex_extract.fields
    cardinality_single = entry.taxonomy.cardinality == "single"
    max_per_object = entry.proposal.max_values_per_object
    confidence_min = entry.proposal.confidence_min

    for prod in products:
        if prod.product_id in skip_product_ids:
            continue
        res.objects_processed += 1

        # Concatenate the requested fields. We use Product.name and
        # Product.sku as the available text surfaces (no description column
        # on Product today).
        bag: list[str] = []
        for f in fields:
            v = getattr(prod, f, None)
            if v:
                bag.append(str(v))
        if not bag:
            continue
        text = " | ".join(bag)

        candidate_pt = pt_by_db_id.get(prod.id) if has_any_block else None
        matches = _scan_text_for_patterns(text, compiled, candidate_pt)
        if not matches:
            continue

        if cardinality_single:
            # Pick the highest-confidence match; tie-break on first declared.
            matches.sort(key=lambda m: -m[1])
            matches = matches[:1]
        else:
            matches = matches[:max_per_object]

        wrote_for_product = False
        for value, conf, quote in matches:
            if conf < confidence_min:
                continue
            event = ProposedAttributeValueEvent(
                workspace_id=workspace_id,
                product_id=prod.product_id,
                attribute_name=entry.name,
                proposed_value_raw=value,
                normalized_value=value,
                confidence=float(conf),
                evidence=[f"regex match {quote!r} in {fields}"],
                source="regex_extract",
            )
            db.add(event)
            res.events_created += 1
            wrote_for_product = True
        if wrote_for_product:
            res.products_with_event.add(prod.product_id)
    if res.events_created:
        db.flush()
    return res


# ---------------------------------------------------------------------------
# Mode: contextual_defaults  (Phase A)
#
# Fires after explicit modes (csv_direct, regex_extract, llm_evidence).
# For each rule whose `if_product_type_in` matches a candidate's
# existing product_type, emits a low-confidence event for the target
# attribute. The dispatcher's precedence skip-set already ensures these
# only fire on candidates with no event from prior modes -- so the
# rule semantics is "default by product_type when no other signal".
# ---------------------------------------------------------------------------

def _run_contextual_defaults(
    *, db: Session, workspace_id: int, entry: AttributeManifestEntry,
    skip_product_ids: set[str],
) -> ModeResult:
    res = ModeResult(mode="contextual_defaults")
    if entry.contextual_defaults is None or not entry.contextual_defaults.rules:
        res.notes.append("contextual_defaults not configured")
        return res

    # Bulk-load every candidate's product_type once.
    products = db.query(Product).filter(
        Product.workspace_id == workspace_id
    ).all()
    pt_rows = (
        db.query(ProductAttribute.product_id, ProductAttribute.attribute_value)
        .join(Product, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id,
                ProductAttribute.attribute_id == "product_type")
        .all()
    )
    pt_by_db_id: dict[int, str] = {db_id: val for db_id, val in pt_rows}

    confidence_min = entry.proposal.confidence_min

    for prod in products:
        if prod.product_id in skip_product_ids:
            continue
        candidate_pt = pt_by_db_id.get(prod.id)
        if not candidate_pt:
            # No product_type populated -- contextual_defaults can't
            # decide. Best-effort: skip silently.
            continue
        res.objects_processed += 1
        # First matching rule wins per (product, attribute).
        for rule in entry.contextual_defaults.rules:
            if candidate_pt not in rule.if_product_type_in:
                continue
            if rule.confidence < confidence_min:
                continue
            evidence_text = (
                f"contextual default: product_type={candidate_pt!r} "
                f"in {list(rule.if_product_type_in)} -> "
                f"{entry.name}={rule.value!r}"
            )
            if rule.rationale:
                evidence_text += f" ({rule.rationale})"
            event = ProposedAttributeValueEvent(
                workspace_id=workspace_id,
                product_id=prod.product_id,
                attribute_name=entry.name,
                proposed_value_raw=rule.value,
                normalized_value=rule.value,
                confidence=float(rule.confidence),
                evidence=[evidence_text],
                source="contextual_defaults",
            )
            db.add(event)
            res.events_created += 1
            res.products_with_event.add(prod.product_id)
            break  # one event per (product, attribute) per run
    if res.events_created:
        db.flush()
    return res


# ---------------------------------------------------------------------------
# Mode: llm_evidence
# ---------------------------------------------------------------------------

def _run_llm_evidence(
    *, db: Session, workspace_id: int, entry: AttributeManifestEntry,
    skip_product_ids: set[str], model_call: Callable[[str], dict] | None,
) -> ModeResult:
    res = ModeResult(mode="llm_evidence")
    if entry.llm_evidence is None:
        res.notes.append("llm_evidence not configured")
        return res
    if model_call is None:
        res.notes.append("model_call not provided; llm_evidence skipped")
        return res

    # Late import to avoid pulling enrichment service at module load.
    from app.services.attribute_enrichment_service import get_prompt_for_attribute

    attr_def = _build_attribute_definition_for_llm(entry)

    products = db.query(Product).filter(
        Product.workspace_id == workspace_id
    ).all()
    eligible = [p for p in products if p.product_id not in skip_product_ids]
    cap = entry.llm_evidence.max_objects_per_run
    if cap is not None and cap > 0:
        eligible = eligible[:cap]
        res.notes.append(f"capped at max_objects_per_run={cap}")

    confidence_min = entry.proposal.confidence_min
    max_per_object = entry.proposal.max_values_per_object

    for prod in eligible:
        res.objects_processed += 1
        obj_for_prompt = {
            "name": prod.name or prod.product_id,
            "sku": prod.sku or "",
        }
        prompt = get_prompt_for_attribute(attr_def, obj_for_prompt)
        try:
            raw = model_call(prompt)
        except Exception as e:
            res.errors += 1
            if res.errors <= 3:
                _log.warning("llm_evidence call failed: %s", e)
            elif res.errors == 4:
                _log.warning("llm_evidence: further errors suppressed; "
                             "see ModeResult.errors for the count")
            continue

        candidates: list[dict] = []
        if isinstance(raw, dict):
            for k in ("proposed_values", "values"):
                section = raw.get(k) or []
                if isinstance(section, list):
                    candidates.extend(d for d in section if isinstance(d, dict))
            # Some prompts respond with a top-level value/confidence/evidence.
            if not candidates and "value" in raw and raw.get("value") is not None:
                candidates.append({
                    "value": raw.get("value"),
                    "confidence": raw.get("confidence", 0.85),
                    "evidence": raw.get("evidence") or [],
                })

        # Defense-in-depth: route LLM proposals through the normalizer
        # (closed-taxonomy synonyms map "0-2 years" -> "infant" etc.).
        parsed: list[ProposedValue] = []
        for pv in candidates:
            v = pv.get("value")
            if v is None:
                continue
            if isinstance(v, list):
                raw_values = [str(x) for x in v if x]
            else:
                raw_values = [str(v)]
            conf = float(pv.get("confidence") or 0.0)
            ev_list = list(pv.get("evidence") or [])
            for raw_val in raw_values:
                if not raw_val.strip():
                    continue
                # Normalise via the closed-taxonomy synonyms (passthrough if
                # no rules configured).
                norm_results = normalize_cell(entry.name, raw_val)
                accepted_canonicals: list[str] = []
                for nr in norm_results:
                    if nr.decision == "matched" and nr.canonical_value:
                        accepted_canonicals.append(nr.canonical_value)
                    elif nr.decision == "passthrough":
                        # For closed taxonomies, the LLM may return the
                        # canonical directly. Only accept passthrough when
                        # the value is in allowed_values.
                        if (entry.taxonomy.kind == "open"
                                or raw_val.lower() in
                                {a.lower() for a in entry.taxonomy.allowed_values}):
                            accepted_canonicals.append(raw_val.strip().lower())
                    # discarded -> drop
                for canonical in accepted_canonicals:
                    if conf < confidence_min:
                        continue
                    parsed.append(ProposedValue(
                        value=canonical,
                        confidence=conf,
                        evidence=ev_list or [f"llm proposed from name {prod.name!r}"],
                    ))

        if not parsed:
            continue
        if entry.taxonomy.cardinality == "single":
            parsed.sort(key=lambda p: -p.confidence)
            parsed = parsed[:1]
        else:
            parsed = parsed[:max_per_object]

        output = EnrichmentOutput(
            attribute_name=entry.name,
            attribute_class=entry.llm_evidence.class_name,
            values=[],
            proposed_values=parsed,
            warnings=[],
            source=EnrichmentSource.TEXT,
        )
        created = record_events_from_output(
            db, workspace_id=workspace_id,
            product_id=prod.product_id, output=output,
        )
        # Override source label so the breakdown distinguishes llm_evidence.
        for ev in created:
            ev.source = "llm_evidence"
        if created:
            res.events_created += len(created)
            res.products_with_event.add(prod.product_id)
    if res.events_created:
        db.flush()
    return res


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------

def dispatch(
    *,
    db: Session,
    workspace_id: int,
    entry: AttributeManifestEntry,
    repo_root: Path,
    model_call: Callable[[str], dict] | None = None,
) -> DispatcherResult:
    """Run all configured modes for *entry* in precedence order.

    Returns a DispatcherResult describing what each mode contributed.
    """
    started = datetime.now(timezone.utc).isoformat()

    # Initial skip set: products already decided BEFORE this run starts.
    # For cardinality=single we treat any pre-existing event as "decided".
    if entry.taxonomy.cardinality == "single":
        skip = _products_with_events(db, workspace_id, entry.name)
    else:
        skip = set()

    out = DispatcherResult(
        attribute_name=entry.name,
        started_at=started,
        ended_at=started,
    )

    handlers = {
        "csv_direct": lambda: _run_csv_direct(
            db=db, workspace_id=workspace_id, entry=entry,
            repo_root=repo_root, skip_product_ids=skip,
        ),
        "regex_extract": lambda: _run_regex_extract(
            db=db, workspace_id=workspace_id, entry=entry,
            skip_product_ids=skip,
        ),
        "llm_evidence": lambda: _run_llm_evidence(
            db=db, workspace_id=workspace_id, entry=entry,
            skip_product_ids=skip, model_call=model_call,
        ),
        "contextual_defaults": lambda: _run_contextual_defaults(
            db=db, workspace_id=workspace_id, entry=entry,
            skip_product_ids=skip,
        ),
    }

    for mode in entry.precedence:
        if mode not in entry.modes:
            continue
        handler = handlers.get(mode)
        if handler is None:
            out.per_mode[mode] = ModeResult(
                mode=mode,
                notes=[f"no handler registered for mode {mode!r}"],
            )
            continue
        result = handler()
        out.per_mode[mode] = result
        out.total_events_created += result.events_created
        if entry.taxonomy.cardinality == "single":
            skip = skip | result.products_with_event

    out.total_products_decided = len(skip)
    out.ended_at = datetime.now(timezone.utc).isoformat()
    return out
