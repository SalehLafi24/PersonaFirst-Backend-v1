"""CSV column-to-attribute mapping layer.

Routes raw CSV cells through the existing PersonaFirst proposal pipeline.
NEVER writes ProductAttribute directly. NEVER bypasses
ProposedAttributeValueEvent / ProposedAttributeValueAggregate.

Two routing modes (declared on the import-side, not the engine):

  direct_or_normalized_value:
      Deterministically normalise the cell against the active
      AttributeAllowedValue set for the target attribute. Always emit
      a ProposedAttributeValueEvent (not a ProductAttribute row); a
      match raises confidence, a non-match becomes a discovery event.

  evidence_for_enrichment:
      Pass the cell text into the existing enrichment prompt
      (`attribute_enrichment_service.get_prompt_for_attribute` +
      `model_client.call_model_json`). The LLM's `proposed_values`
      flow through `record_events_from_output` to
      ProposedAttributeValueEvent.

Both modes converge on `ProposedAttributeValueEvent`. Aggregates are
refreshed once at the end of the batch via `refresh_aggregates`.
Approval / promotion to ProductAttribute is a SEPARATE downstream step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from sqlalchemy.orm import Session

from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product
from app.models.proposed_attribute_value import (
    ProposedAttributeValueAggregate,
    ProposedAttributeValueEvent,
)
from app.schemas.attribute_enrichment import (
    AttributeDefinition,
    EnrichmentOutput,
    EnrichmentSource,
    ProposedValue,
)
from app.services.attribute_enrichment_service import get_prompt_for_attribute
from app.services.attribute_normalizer_service import (
    NormalizationResult, normalize_cell,
)
from app.services.proposed_attribute_value_service import (
    record_events_from_output,
    refresh_aggregates,
    promotion_readiness,
)
from app.services.proposed_value_normalizer import normalize_proposed_value


MAPPING_MODE_DIRECT = "direct_or_normalized_value"
MAPPING_MODE_EVIDENCE = "evidence_for_enrichment"
VALID_MODES: frozenset[str] = frozenset({MAPPING_MODE_DIRECT, MAPPING_MODE_EVIDENCE})


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MappingRule:
    """One CSV column -> one target attribute, with a routing mode."""
    source_column: str
    target_attribute: str
    mode: str

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(
                f"unknown mapping mode {self.mode!r}; "
                f"must be one of {sorted(VALID_MODES)}"
            )


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------

@dataclass
class ImportResult:
    products_created: int = 0
    products_seen: int = 0
    raw_values_captured: int = 0
    direct_events_created: int = 0
    evidence_events_created: int = 0
    enrichment_calls: int = 0
    enrichment_errors: int = 0
    aggregates_total_after: int = 0
    aggregates_ready_after: int = 0
    per_attribute_event_counts: dict[str, int] = field(default_factory=dict)
    per_mode_event_counts: dict[str, int] = field(default_factory=dict)
    skipped_unknown_columns: dict[str, int] = field(default_factory=dict)
    # Normalizer counters (direct mode only).
    normalizer_matched_canonical_counts: dict[str, int] = field(default_factory=dict)
    normalizer_discarded_count: int = 0
    normalizer_passthrough_events: int = 0


# --------------------------------------------------------------------------
# Direct (normalized) mode
# --------------------------------------------------------------------------

def _split_multi_value(raw: str) -> list[str]:
    """Conservative split for pipe / comma / semicolon-delimited cells."""
    if raw is None:
        return []
    parts: list[str] = [str(raw)]
    for sep in ("|", ";", ","):
        next_parts: list[str] = []
        for p in parts:
            next_parts.extend(p.split(sep))
        parts = next_parts
    return [p.strip() for p in parts if p and p.strip()]


@dataclass
class _DirectRouteOutcome:
    events_written: int = 0
    matched_canonical_counts: dict[str, int] = field(default_factory=dict)
    discarded_count: int = 0
    passthrough_events: int = 0


def _emit_event(
    *, db: Session, workspace_id: int, product_id: str,
    target_attribute: str, normalized_value: str, raw_display: str,
    confidence: float, evidence_text: str,
) -> None:
    db.add(ProposedAttributeValueEvent(
        workspace_id=workspace_id,
        product_id=product_id,
        attribute_name=target_attribute,
        proposed_value_raw=raw_display,
        normalized_value=normalized_value,
        confidence=confidence,
        evidence=[evidence_text],
        source=EnrichmentSource.TEXT.value,
    ))


def _direct_route_cell(
    *, db: Session, workspace_id: int, product_id: str,
    target_attribute: str, raw_cell: str,
    active_allowed_lower: set[str],
) -> _DirectRouteOutcome:
    """Route a single cell through direct_or_normalized_value mode.

    Pass the raw cell to `attribute_normalizer_service.normalize_cell`
    first. Per-result behaviour:

        matched      emit a 0.98-confidence event with normalized_value =
                     canonical and the raw token preserved in evidence.
        discarded    NO event written; the normalizer already wrote a
                     structured discard log entry.
        passthrough  preserve prior behaviour: normalize against active
                     allowed values, emit a 0.98-conf event on match or a
                     0.85-conf discovery event otherwise.

    Returns counts for the validation report; callers update aggregate
    counters from the outcome.
    """
    out = _DirectRouteOutcome()
    if raw_cell is None or str(raw_cell).strip() == "":
        return out

    results = normalize_cell(target_attribute, str(raw_cell))
    for r in results:
        if r.decision == "matched":
            assert r.canonical_value is not None
            _emit_event(
                db=db, workspace_id=workspace_id, product_id=product_id,
                target_attribute=target_attribute,
                normalized_value=r.canonical_value,
                raw_display=r.canonical_value,
                confidence=0.98,
                evidence_text=(
                    f"csv direct cell {r.raw!r} -> canonical "
                    f"{r.canonical_value!r} (rule={r.rule_id})"
                ),
            )
            out.events_written += 1
            out.matched_canonical_counts[r.canonical_value] = (
                out.matched_canonical_counts.get(r.canonical_value, 0) + 1
            )
        elif r.decision == "discarded":
            out.discarded_count += 1
            # No event. Discard already logged by the normalizer.
        elif r.decision == "passthrough":
            if r.proposed_value is not None:
                # The normalizer applied cleaning (e.g. "Sebamed Inc" ->
                # "sebamed"). Emit ONE event with the cleaned form as
                # both normalized_value and proposed_value_raw; preserve
                # the original raw token in evidence.
                normalized = r.proposed_value
                is_match = normalized in active_allowed_lower
                _emit_event(
                    db=db, workspace_id=workspace_id, product_id=product_id,
                    target_attribute=target_attribute,
                    normalized_value=normalized,
                    raw_display=normalized,
                    confidence=0.98 if is_match else 0.85,
                    evidence_text=(
                        f"csv direct cell {r.raw!r} -> cleaned to "
                        f"{normalized!r} (rule={r.rule_id})"
                    ),
                )
                out.events_written += 1
                out.passthrough_events += 1
            else:
                # No cleaning applied. Fall back: split the passthrough
                # token on multi-value separators, match against active AAV.
                for tok in _split_multi_value(r.raw):
                    normalized = normalize_proposed_value(tok)
                    if not normalized:
                        continue
                    is_match = normalized in active_allowed_lower
                    _emit_event(
                        db=db, workspace_id=workspace_id, product_id=product_id,
                        target_attribute=target_attribute,
                        normalized_value=normalized,
                        raw_display=tok,
                        confidence=0.98 if is_match else 0.85,
                        evidence_text=(
                            f"csv direct cell {tok!r}"
                            + (" (matches active allowed value)"
                               if is_match else " (discovery)")
                        ),
                    )
                    out.events_written += 1
                    out.passthrough_events += 1
    if out.events_written:
        db.flush()
    return out


# --------------------------------------------------------------------------
# Evidence-for-enrichment mode
# --------------------------------------------------------------------------

def _evidence_route_product(
    *, db: Session, workspace_id: int, product_id: str,
    target_attribute: str, attribute_def: AttributeDefinition,
    obj_for_prompt: dict, model_call,
) -> tuple[int, bool]:
    """Build the existing prompt for `target_attribute`, call the model,
    write proposed values via `record_events_from_output`. Returns
    (event_count, error_occurred)."""
    prompt = get_prompt_for_attribute(attribute_def, obj_for_prompt)
    try:
        raw = model_call(prompt)
    except Exception:  # propagate as soft error so batch can continue
        return (0, True)

    # In the mapping-import layer EVERY LLM-returned item flows as a
    # proposed value -- including items the LLM put in `values` (i.e.
    # matches against existing allowed_values). This enforces the user's
    # invariant: "Claude is NOT assigning values directly". A matched
    # value still needs a reviewer's approval before it can become a
    # ProductAttribute row. (Outside this layer, an enrichment caller
    # could honour `values` as direct hits -- but here, we route them
    # through the proposal pipeline by design.)
    candidate_dicts: list[dict] = []
    if isinstance(raw, dict):
        for k in ("proposed_values", "values"):
            section = raw.get(k) or []
            if isinstance(section, list):
                candidate_dicts.extend(d for d in section if isinstance(d, dict))
    elif isinstance(raw, list):
        candidate_dicts.extend(d for d in raw if isinstance(d, dict))
    else:
        return (0, True)

    parsed = []
    for pv in candidate_dicts:
        try:
            value = str(pv.get("value", "")).strip()
            if not value:
                continue
            # `values` items often omit confidence; default to 0.95
            # (slightly below an explicit allow-list match) so the
            # readiness gate still applies.
            confidence = pv.get("confidence")
            if confidence is None:
                confidence = 0.95
            parsed.append(ProposedValue(
                value=value,
                confidence=float(confidence),
                evidence=list(pv.get("evidence") or []),
            ))
        except (TypeError, ValueError):
            continue

    output = EnrichmentOutput(
        attribute_name=target_attribute,
        attribute_class=attribute_def.class_name,
        values=[],  # discarded by design (see comment above)
        proposed_values=parsed,
        warnings=[],
        source=EnrichmentSource.TEXT,
    )
    created = record_events_from_output(
        db, workspace_id=workspace_id, product_id=product_id, output=output,
    )
    return (len(created), False)


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def _ensure_product(
    *, db: Session, workspace_id: int, external_id: str,
    name: str | None, sku: str | None,
) -> tuple[Product, bool]:
    existing = db.query(Product).filter(
        Product.workspace_id == workspace_id,
        Product.product_id == external_id,
    ).first()
    if existing is not None:
        return existing, False
    p = Product(
        workspace_id=workspace_id,
        product_id=external_id,
        sku=sku or external_id,
        name=name or external_id,
    )
    db.add(p)
    db.flush()
    return p, True


def _load_active_allowed_lower(
    db: Session, workspace_id: int, attribute: str,
) -> set[str]:
    return {
        v.lower() for (v,) in db.query(AttributeAllowedValue.value).filter(
            AttributeAllowedValue.workspace_id == workspace_id,
            AttributeAllowedValue.attribute_name == attribute,
            AttributeAllowedValue.is_active == True,
        ).all()
    }


def import_csv_with_mapping(
    *,
    db: Session,
    workspace_id: int,
    rows: Iterable[Mapping[str, object]],
    mapping_rules: list[MappingRule],
    attribute_definitions: Mapping[str, AttributeDefinition],
    product_id_column: str,
    name_column: str | None = None,
    sku_column: str | None = None,
    model_call=None,
) -> ImportResult:
    """Run a CSV-shape iterable through the mapping layer.

    Args:
        db: SQLAlchemy session.
        workspace_id: target workspace.
        rows: iterable of dicts (one per product row).
        mapping_rules: routing rules.
        attribute_definitions: attribute_name -> AttributeDefinition.
            Required for any target attribute used in
            evidence_for_enrichment mode (defines the prompt + behaviour).
        product_id_column: which CSV column carries the external product id.
        name_column: optional CSV column for Product.name.
        sku_column: optional CSV column for Product.sku.
        model_call: callable(prompt: str) -> dict. Required only if any
            mapping rule uses evidence_for_enrichment. Inject
            `app.services.model_client.call_model_json` from the caller
            -- this module deliberately does not import it so unit tests
            and dry-runs can pass a stub.
    """
    # Validate mapping_rules.
    for r in mapping_rules:
        if r.mode == MAPPING_MODE_EVIDENCE:
            if r.target_attribute not in attribute_definitions:
                raise ValueError(
                    f"evidence_for_enrichment rule maps to "
                    f"{r.target_attribute!r} but no AttributeDefinition "
                    f"was provided for it"
                )
            if model_call is None:
                raise ValueError(
                    "evidence_for_enrichment requires a model_call "
                    "callable (typically model_client.call_model_json)"
                )

    # Pre-compute active allowed-values per target attribute used in direct mode.
    direct_targets = {r.target_attribute for r in mapping_rules
                      if r.mode == MAPPING_MODE_DIRECT}
    allowed_lookup: dict[str, set[str]] = {
        attr: _load_active_allowed_lower(db, workspace_id, attr)
        for attr in direct_targets
    }

    result = ImportResult()

    for row in rows:
        result.products_seen += 1
        external_id = str(row.get(product_id_column) or "").strip()
        if not external_id:
            continue
        name = str(row.get(name_column) or "").strip() if name_column else ""
        sku = str(row.get(sku_column) or "").strip() if sku_column else ""
        prod, created = _ensure_product(
            db=db, workspace_id=workspace_id,
            external_id=external_id, name=name or None, sku=sku or None,
        )
        if created:
            result.products_created += 1

        # Group evidence-mode columns by target_attribute so each product
        # gets ONE call per target attribute -- not one per column.
        evidence_text_by_attr: dict[str, list[str]] = {}

        for rule in mapping_rules:
            raw = row.get(rule.source_column)
            if raw is None or str(raw).strip() == "":
                if rule.source_column not in row:
                    result.skipped_unknown_columns[rule.source_column] = (
                        result.skipped_unknown_columns.get(rule.source_column, 0) + 1
                    )
                continue
            result.raw_values_captured += 1

            if rule.mode == MAPPING_MODE_DIRECT:
                outcome = _direct_route_cell(
                    db=db, workspace_id=workspace_id,
                    product_id=external_id,
                    target_attribute=rule.target_attribute,
                    raw_cell=str(raw),
                    active_allowed_lower=allowed_lookup.get(rule.target_attribute, set()),
                )
                n = outcome.events_written
                result.direct_events_created += n
                result.per_attribute_event_counts[rule.target_attribute] = (
                    result.per_attribute_event_counts.get(rule.target_attribute, 0) + n
                )
                result.per_mode_event_counts[MAPPING_MODE_DIRECT] = (
                    result.per_mode_event_counts.get(MAPPING_MODE_DIRECT, 0) + n
                )
                result.normalizer_discarded_count += outcome.discarded_count
                result.normalizer_passthrough_events += outcome.passthrough_events
                for cv, c in outcome.matched_canonical_counts.items():
                    result.normalizer_matched_canonical_counts[cv] = (
                        result.normalizer_matched_canonical_counts.get(cv, 0) + c
                    )
            elif rule.mode == MAPPING_MODE_EVIDENCE:
                evidence_text_by_attr.setdefault(rule.target_attribute, []).append(
                    f"{rule.source_column}: {str(raw).strip()}"
                )

        # Run evidence-mode enrichment once per (product, target attribute).
        for target_attr, lines in evidence_text_by_attr.items():
            attr_def = attribute_definitions[target_attr]
            obj_for_prompt = {
                "name": name or external_id,
                "evidence_text": "\n".join(lines),
            }
            n, err = _evidence_route_product(
                db=db, workspace_id=workspace_id, product_id=external_id,
                target_attribute=target_attr, attribute_def=attr_def,
                obj_for_prompt=obj_for_prompt, model_call=model_call,
            )
            result.enrichment_calls += 1
            if err:
                result.enrichment_errors += 1
            result.evidence_events_created += n
            result.per_attribute_event_counts[target_attr] = (
                result.per_attribute_event_counts.get(target_attr, 0) + n
            )
            result.per_mode_event_counts[MAPPING_MODE_EVIDENCE] = (
                result.per_mode_event_counts.get(MAPPING_MODE_EVIDENCE, 0) + n
            )

    db.commit()

    # Refresh aggregates once for each touched attribute.
    touched_attrs: set[str] = set()
    touched_attrs.update(direct_targets)
    touched_attrs.update(
        r.target_attribute for r in mapping_rules
        if r.mode == MAPPING_MODE_EVIDENCE
    )
    for attr in touched_attrs:
        refresh_aggregates(db, workspace_id=workspace_id, attribute_name=attr)
    db.commit()

    # Final aggregate stats.
    aggs = db.query(ProposedAttributeValueAggregate).filter(
        ProposedAttributeValueAggregate.workspace_id == workspace_id,
        ProposedAttributeValueAggregate.attribute_name.in_(touched_attrs),
    ).all()
    result.aggregates_total_after = len(aggs)
    result.aggregates_ready_after = sum(
        1 for a in aggs if promotion_readiness(a).ready
    )

    return result
