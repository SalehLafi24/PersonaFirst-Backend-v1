# PersonaFirst Backend — Comprehensive Platform Analysis

> Informational document. No code was modified in producing this analysis.
> Generated 2026-04-24.

---

## 1. Executive Summary

PersonaFirst is a **multi-tenant personalization / recommendation backend** built on FastAPI + SQLAlchemy 2.0 + PostgreSQL. Its core job is: given a customer's purchase history in a workspace, enrich the product catalog with typed attributes (via the Anthropic Claude API), derive customer affinities, learn co-occurrence and co-purchase relationships, and return ranked product recommendations through a flexible **slot-based API**.

Distinctive features:

1. **Multi-source attribute enrichment** — text + (planned) visual enrichment via Claude, merged with class-aware conflict resolution.
2. **Typed-attribute scoring** — attributes carry `targeting_mode`, `class_name`, and behavior flags that route them to different parts of the scoring algorithm (affinity, compatibility signal, filter, descriptive-only).
3. **Review-first taxonomy evolution** — proposed values and new attributes land in append-only event tables, roll up into reviewable aggregates, and require manual approval before entering the active taxonomy.
4. **Signal-aware ranking** — a computed `customer_signal_strength` tunes diversity, scan depth, and score thresholds per request.
5. **Slot-based recommendation API** — clients request multiple independent "slots" (e.g., "new for you", "because you bought X") with per-slot filters, fallback strategies, and cross-slot exclusion.

---

## 2. Project Structure

### Repository layout

```
personafirst-backend/
├── alembic/                          Database migrations
│   ├── env.py
│   └── versions/                     001..010 migration scripts
├── app/
│   ├── main.py                       FastAPI app bootstrap
│   ├── core/
│   │   ├── config.py                 Settings (pydantic-settings)
│   │   └── database.py               Engine, SessionLocal, get_db, Base
│   ├── models/                       13 SQLAlchemy models
│   ├── schemas/                      12 Pydantic schema modules
│   ├── services/                     22 service modules (business logic)
│   └── api/routes/                   9 route modules
├── scripts/                          16 standalone runner scripts
├── seed_data/                        Seed JSON/CSV for starter workspace
├── tests/                            41 test files + conftest + fixtures
├── products_enriched.json            Enrichment run output (demo scale)
├── products_enriched_real.json       Enrichment run output (full catalog)
├── products_normalized.json          Post-normalization catalog snapshot
├── requirements.txt
├── package.json                      Only declares @anthropic-ai/sdk (unused by Python)
├── alembic.ini
├── pytest.ini
├── install.ps1
├── .env                              DATABASE_URL only
└── CLAUDE.MD                         Project rules for the assistant
```

### Python dependencies ([requirements.txt](requirements.txt))

- `fastapi==0.115.6`
- `uvicorn[standard]==0.34.0`
- `sqlalchemy==2.0.36`
- `alembic==1.14.0`
- `psycopg2-binary==2.9.11`
- `pydantic>=2.10.3`, `pydantic-settings>=2.7.0`
- `pytest==8.3.4`, `pytest-asyncio==0.25.0`
- `httpx==0.28.1`, `email-validator==2.2.0`
- `anthropic>=0.40.0`

### Generated data files at repo root

| File | Approx size | Contents |
|---|---|---|
| `products_enriched.json` | ~50 KB | Enrichment output: `product_id`, `name`, `clean_description`, `functional_categories`, per-attribute `proposed_values[{value, confidence, evidence[]}]`. |
| `products_enriched_real.json` | ~129 KB | Same shape, larger catalog (real run). |
| `products_normalized.json` | ~71 KB | Normalized/canonicalized attribute values after the proposed-value pipeline. |

### `.env` shape

Single declared key: `DATABASE_URL=postgresql://postgres:<password>@localhost:5432/personafirst`. `ANTHROPIC_API_KEY` is read by [app/services/model_client.py](app/services/model_client.py) directly from the environment.

---

## 3. Core Infrastructure — [app/core](app/core)

### [app/core/database.py](app/core/database.py)

```python
engine       = create_engine(settings.database_url)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase): ...

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

- SQLAlchemy 2.0 declarative `Base`.
- Sync engine; PostgreSQL via `psycopg2-binary`.
- `get_db()` is the FastAPI dependency used throughout the route layer.

### [app/core/config.py](app/core/config.py)

```python
class Settings(BaseSettings):
    app_name: str = "PersonaFirst"
    debug: bool = False
    database_url: str = "postgresql://postgres:postgres@localhost:5432/personafirst"
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
```

Minimal — values come from `.env` via `pydantic-settings`.

### [app/main.py](app/main.py)

- Instantiates FastAPI with `title=settings.app_name`.
- `Base.metadata.create_all(bind=engine)` runs at import time (tables auto-create on startup even without migrations).
- Registers routers: `health`, `workspaces`, `users`, `affinities`, `purchases`, `recommendations`, `relationships`, `behavioral_relationships`, `signal_strength`.

### Multi-tenancy enforcement

There is **no middleware or dependency that enforces workspace isolation**. Every row in a tenant-scoped model carries `workspace_id` as a `ForeignKey("workspaces.id")`, and every service/route filters on it explicitly. Rule from [CLAUDE.MD](CLAUDE.MD): *"Always use workspace_id for multi-tenancy."*

User/Workspace are the only non-tenant-scoped models; they connect via the junction table `workspace_users`.

---

## 4. Data Model — [app/models](app/models)

### ER overview

```
workspaces ──┬─ workspace_users ─ users
             │
             ├─ products ──── product_attributes
             │          └──── product_behavior_relationships (src, tgt)
             │
             ├─ customer_purchases     (denormalized product_id, group_id)
             ├─ customer_attribute_affinities
             ├─ attribute_value_relationships
             ├─ attribute_allowed_values            (workspace-scoped taxonomy)
             ├─ proposed_attribute_value_events / _aggregates
             ├─ proposed_attribute_events / _aggregates
             └─ audit_logs
```

### Tenant / identity

- [Workspace](app/models/workspace.py) — `id`, `name`, `slug` (unique), `created_at`.
- [User](app/models/user.py) — `id`, `email` (unique), `name`, `created_at`. Not workspace-scoped.
- [WorkspaceUser](app/models/workspace_user.py) — `workspace_id`, `user_id`, `role` (default `"member"`); unique `(workspace_id, user_id)`.

### Catalog

- [Product](app/models/product.py)
  - `id` (PK), `workspace_id`, `product_id` (external string), `sku`, `name`.
  - `group_id` — nullable, used for cross-sell groups and group-level repurchase suppression.
  - `repurchase_behavior` — `one_time | repurchasable | seasonal`.
  - `repurchase_window_days` — int; drives suppression window.
  - `recommendation_role` — default `"same_use_case"`; `"complementary"` bypasses functional suppression (e.g., layering, accessories).
  - Unique `(workspace_id, product_id)`.
  - Child model **ProductAttribute**: `product_id` FK, `attribute_id`, `attribute_value` — one row per (product, attribute, value). This is the normalized home of the enriched catalog.

### Behavior / history

- [CustomerPurchase](app/models/customer_purchase.py)
  - `workspace_id`, `customer_id` (string, denormalized), `product_db_id` FK → `products.id`, `product_id` (denormalized external string), `group_id` (denormalized).
  - `order_date`, `quantity`, `revenue` (nullable).
  - Indexes on `(workspace_id, customer_id)`, `(workspace_id, customer_id, product_db_id)`, `(workspace_id, customer_id, group_id)`.
  - Denormalization is deliberate: enables analytics and repurchase-window checks without joining `products`.

### Learned signals

- [CustomerAttributeAffinity](app/models/customer_attribute_affinity.py)
  - `workspace_id`, `customer_id`, `attribute_id`, `attribute_value`, `score` (float).
  - Unique `(workspace_id, customer_id, attribute_id, attribute_value)`; index on `(workspace_id, customer_id)`.
  - Produced by [affinity_service.generate_affinities_from_purchases](app/services/affinity_service.py).

- [AttributeValueRelationship](app/models/attribute_value_relationship.py)
  - Directional complementary pair: `(source_attribute_id, source_value) → (target_attribute_id, target_value)`.
  - `confidence`, `lift`, `pair_count`, `strength`, `status` (`suggested | approved | rejected | archived`).
  - Produced by [relationship_engine_service](app/services/relationship_engine_service.py).

- [ProductBehaviorRelationship](app/models/product_behavior_relationship.py)
  - Directional co-purchase edge: `source_product_db_id → target_product_db_id`.
  - `strength = customer_overlap_count / source_customer_count`.
  - Unique `(workspace_id, source_product_db_id, target_product_db_id)`; index on `(workspace_id, source_product_db_id)`.
  - Produced by [behavior_engine_service.run_behavior_engine](app/services/behavior_engine_service.py) — full-refresh semantics.

### Taxonomy evolution (review pipeline)

- [AttributeAllowedValue](app/models/attribute_allowed_value.py) — workspace-scoped taxonomy override; unique `(workspace_id, attribute_name, value)`; `is_active` soft-delete flag. When no rows exist, callers fall back to [seed_data/attribute_definitions.json](seed_data/attribute_definitions.json).

- [ProposedAttributeValueEvent / ProposedAttributeValueAggregate](app/models/proposed_attribute_value.py) — two-level design:
  - **Event** (append-only): `product_id`, `attribute_name`, `proposed_value_raw`, `normalized_value`, `confidence`, `evidence (JSON)`, `source`.
  - **Aggregate**: `cluster_key` (= normalized value for flat taxonomy), `proposal_count`, `distinct_product_count`, `avg_confidence`, `max_confidence`, `status` (`pending | approved | rejected | merged`), `merge_reason` (`normalized_duplicate | synonym_to_existing | flattened_child | noise`), `review_note`.
  - **No auto-promotion** — only manual review transitions `pending → approved`.

- [ProposedAttributeEvent / ProposedAttributeAggregate](app/models/proposed_attribute.py) — same two-level pattern, but for *new attribute dimensions* (not values within a known dimension). Fields include `canonical_attribute_name`, `suggested_values`, `suggested_class_name`, `suggested_targeting_mode`.

### Audit

- [AuditLog](app/models/audit_log.py) — schema present (`workspace_id`, `user_id`, `action`, `resource_type`, `resource_id`, `created_at`) but not populated anywhere in the codebase.

---

## 5. Schemas — [app/schemas](app/schemas)

Pydantic v2 models; most use `ConfigDict(from_attributes=True)` to map from ORM.

### CRUD / simple

- [workspace.py](app/schemas/workspace.py), [user.py](app/schemas/user.py), [workspace_user.py](app/schemas/workspace_user.py) — plain `*Create` / `*Read`.
- [purchase.py](app/schemas/purchase.py) — `PurchaseCreate` validates `quantity ≥ 1`; `PurchaseRead` adds `workspace_id`, `group_id`, `created_at`.
- [customer_attribute_affinity.py](app/schemas/customer_attribute_affinity.py) — input uses `attribute_name`/`value_label`; response echoes ORM field names.
- [attribute_value_relationship.py](app/schemas/attribute_value_relationship.py), [product_behavior_relationship.py](app/schemas/product_behavior_relationship.py) — read models for the learned edges.
- [affinity_generate.py](app/schemas/affinity_generate.py) — response of the affinity-generation endpoint.
- [signal_strength.py](app/schemas/signal_strength.py) — `SignalStrengthComponents` (`purchase_depth`, `attribute_richness`, `behavioral_graph`), `SignalStrengthRead`, `BatchSignalStrengthRead`, `AudienceSignalRead`.

### Recommendation slot system — [recommendation.py](app/schemas/recommendation.py)

**Request side**

- `SlotFilter` — `attribute_id`, `operator ∈ {eq, in}`, `value`.
- `SlotConfig` — flat fields (`slot_id`, `algorithm`, `top_n`, `filters`, `fallback_mode`, `exclude_previous_slots`, `exclusion_level`, `diversity_enabled`, `diversity_mode`, `fallback_behavior`) plus optional nested round-trip config blocks (`audience`, `strategy`, `constraints`, `controls`, `exclusion`).
- `SlotRequest` / `MultiSlotRequest` — customer + one or many slots.

**Response side**

- `MatchedAttribute` — `attribute_id`, `value`, `score`, `weight`, `targeting_mode`.
- `RelationshipMatch`, `BehavioralMatch` — evidence rows.
- `RecommendationRead` — the full per-product row:
  - Identity: `product_id`, `sku`, `name`, `group_id`.
  - Matches: `matched_attributes[]`, `relationship_matches[]`, `behavioral_matches[]`.
  - Component scores: `direct_score`, `relationship_score`, `popularity_score`, `behavioral_score`.
  - Contribution buckets: `affinity_contribution`, `compatibility_positive_contribution`, `compatibility_negative_contribution`, `contextual_negative_contribution`, `low_signal_penalty`.
  - Final: `recommendation_score`.
  - Labels: `recommendation_source` (e.g. `"direct+behavioral"`, `"popular"`), free-text `explanation`.
  - Signal metadata: `product_signal_strength`, `customer_signal_strength`, `match_confidence`, `signal_summary`.
- `SlotResponse` — `slot_id`, `algorithm`, `fallback_applied`, `results[]`.
- `MultiSlotResponse` — `customer_id`, `slots[]`.

### Enrichment — [attribute_enrichment.py](app/schemas/attribute_enrichment.py)

- `TargetingMode` enum: `categorical_affinity | compatibility_signal | categorical_filter | descriptive_metadata`.
- `EnrichmentSource` enum: `text | visual | ...`.
- `AttributeBehavior` — flags: `taxonomy_sensitive`, `ordered_values`, `can_propose_values`, `multi_value_allowed`, `prefer_conservative_inference`, `value_order` (ordered list), `negative_scoring_enabled`.
- `AttributeDefinition` — `name`, `object_type` (`product|customer`), `class_name`, `value_mode` (`single|multi|boolean`), `allowed_values`, `description`, `evidence_sources`, `behavior`, `targeting_mode` (defaults by class).
- `EnrichedValue` — `value`, `confidence`, `evidence[]`, `reasoning_mode`, `source`, `contributing_sources`.
- `ProposedValue` — `value`, `confidence`, `evidence`.
- `EnrichmentOutput` — `attribute_name`, `attribute_class`, `values[]`, `proposed_values[]`, `warnings`, `source`.

- [attribute_discovery.py](app/schemas/attribute_discovery.py) — request/response types for the new-dimension discovery pipeline.

---

## 6. Services — [app/services](app/services)

Every service filters on `workspace_id`. Routes stay thin per the [CLAUDE.MD](CLAUDE.MD) rule.

### CRUD services

- [workspace_service.py](app/services/workspace_service.py), [user_service.py](app/services/user_service.py), [workspace_user_service.py](app/services/workspace_user_service.py), [purchase_service.py](app/services/purchase_service.py), [relationship_service.py](app/services/relationship_service.py) — straightforward create/get/list/update; no domain logic.

### [affinity_service.py](app/services/affinity_service.py)

- `bulk_create_affinities(db, workspace_id, items)` — bulk insert of `AffinityCreate`.
- `list_affinities(db, workspace_id, min_score?, sort_by_score_desc?)`.
- `generate_affinities_from_purchases(db, workspace_id, customer_id?)`:
  1. Group purchases by customer.
  2. Count `(attribute_id, value)` occurrences across each customer's purchases, **weighted by `quantity`**.
  3. Normalize per-customer: `score = count / max_count_for_customer` → `[0, 1]`.
  4. Upsert into `customer_attribute_affinities`.
  5. Returns `{customers_processed, affinities_upserted}`.

### [relationship_engine_service.py](app/services/relationship_engine_service.py)

`run_relationship_engine(db, workspace_id, min_confidence=0.1, min_lift=1.0, min_pair_count=2)`:

1. Build per-customer sets of `(attribute_id, value)` from affinities.
2. Compute **support** (distinct customers per item) and **co-occurrence counts**.
3. For each ordered pair (A, B):
   - `confidence(A→B) = co_occur(A,B) / support(A)`
   - `lift(A→B) = confidence / global_rate(B)`
4. Filter by `min_confidence`, `min_lift`, `min_pair_count`.
5. Insert as `AttributeValueRelationship(status="suggested")`; idempotent (existing rows unchanged).

### [behavior_engine_service.py](app/services/behavior_engine_service.py)

`run_behavior_engine(db, workspace_id)`:

1. Customer → set of distinct `product_db_id` purchased.
2. For every ordered pair (A, B): count customers who bought both.
3. `strength = overlap / source_customer_count`.
4. **Full refresh** — deletes all rows for workspace, then bulk-inserts fresh.

### [signal_strength_service.py](app/services/signal_strength_service.py)

Customer signal strength as the average of three components, each normalized via `log1p` + workspace min-max:

```
purchase_depth     = 0.7 * norm(purchase_count)       + 0.3 * norm(unique_products)
attribute_richness = 0.7 * norm(affinity_count)       + 0.3 * norm(distinct_attribute_types)
behavioral_graph   = 0.5 * norm(outgoing_edge_count)  + 0.5 * norm(avg_edge_strength)

customer_signal_strength = (purchase_depth + attribute_richness + behavioral_graph) / 3
```

### [audience_signal_service.py](app/services/audience_signal_service.py)

Batch-level: aggregates `customer_signal_strength` across a set of customers — mean, min, max, distribution buckets.

### [multi_source_signal_service.py](app/services/multi_source_signal_service.py)

Post-scoring enrichment of the recommendation list with confidence metadata.

- **Product signal strength** (weighted combination of enrichment-quality signals):
  ```
  PRODUCT_SIGNAL_WEIGHTS = {
      "coverage":         0.35,   # fraction of attributes actually enriched
      "avg_confidence":   0.35,   # mean confidence across enriched attrs
      "agreement":        0.30,   # text vs visual agreement
      "conflict_penalty": 0.40,   # negative weight for conflicts
  }
  ```

- **Match confidence** (overall recommendation trust):
  ```
  MATCH_CONFIDENCE_WEIGHTS = {
      "customer_signal":  0.30,
      "product_signal":   0.30,
      "attribute_match":  0.20,   # # of matched attributes
      "compatibility":    0.20,   # # of compatibility-signal matches
      "conflict_penalty": 0.30,
  }
  ```

### [attribute_taxonomy_service.py](app/services/attribute_taxonomy_service.py)

Read/write of `AttributeAllowedValue`. When a workspace has no override rows for an attribute, callers use static definitions from `seed_data/attribute_definitions.json`.

### Enrichment pipeline

- [model_client.py](app/services/model_client.py)
  - `call_model_json(prompt, model="claude-sonnet-4-5", max_tokens=2048, temperature=0.0) -> dict`
  - Reads `ANTHROPIC_API_KEY` from env.
  - Sends the prompt as a single user message with a JSON-only system instruction.
  - Tolerant of ```` ```json ```` fences in the response.
  - Raises `ApiKeyMissingError`, `ModelCallError`, `ModelResponseError` (distinct for callers).

- [attribute_enrichment_service.py](app/services/attribute_enrichment_service.py)
  - `enrich_attribute(db, workspace_id, attribute, obj, ...)` — single-attribute enrichment.
  - Four class-specific prompt builders, each injects `allowed_values` (with fallback to static), translates the `AttributeBehavior` flags into natural-language constraints, and mandates evidence quotes:
    - `_build_descriptive_literal_prompt` — strict extraction, zero inference.
    - `_build_contextual_semantic_prompt` — reasoning (occasion, activity, environment).
    - `_build_compatibility_prompt` — suitability/fit reasoning with ordered scales.
    - `_build_taxonomy_discovery_prompt` — allows proposing new values.
  - Output schema: `EnrichmentOutput`.

- [visual_attribute_enrichment_service.py](app/services/visual_attribute_enrichment_service.py) — skeleton for image-based enrichment; the merge service is already wired to consume its output.

- [attribute_merge_service.py](app/services/attribute_merge_service.py)
  - `merge_enrichment_outputs(attribute, text_output, visual_output, confidence_gap_threshold=0.15)`:
    1. Single source → pass through.
    2. Both agree → combine evidence, boost confidence via noisy-or.
    3. Conflict on single/boolean modes:
       - Apply class-aware source weights: `text = {descriptive:0.9, contextual:1.0, compat:1.1}`, `visual = {descriptive:1.0, contextual:0.85, compat:0.8}`.
       - If weighted gap ≥ threshold: pick winner.
       - Else: emit no value with `cross_source_conflict` warning.
    4. Multi-value: union disjoint values, keep agreements merged.

- [proposed_value_normalizer.py](app/services/proposed_value_normalizer.py), [proposed_attribute_value_service.py](app/services/proposed_attribute_value_service.py), [proposed_attribute_normalizer.py](app/services/proposed_attribute_normalizer.py), [proposed_attribute_service.py](app/services/proposed_attribute_service.py), [attribute_discovery_service.py](app/services/attribute_discovery_service.py) — implement the raw-event → aggregate → manual-review → promotion pipeline described in §4. Normalizers cluster synonymous raw proposals (e.g. casing, whitespace, simple stem variants) into a `cluster_key` used as the aggregate row's identity.

### [recommendation_service.py](app/services/recommendation_service.py) — the core

This is the largest service (~1300 lines) and the heart of the platform. Sections 7 and 8 document it in detail.

---

## 7. Recommendation Algorithm — In Detail

### 7.1 Public entry point

`get_recommendations(db, workspace_id, customer_id, algorithm, slot_config=None, weights=..., disable_purchase_suppression_for_eval=False, ...) → (list[RecommendationRead], fallback_applied: bool)`

### 7.2 Algorithm presets

`ALGORITHM_PRESETS` selects default weights and tie-break field ordering:

| Preset | direct | relationship | popularity | behavioral | Tie-break priority |
|---|---|---|---|---|---|
| `balanced`        | 1.0 | 0.7 | 0.0 | 0.5 | direct → relationship → behavioral → product_id |
| `behavior_first`  | 0.3 | 0.3 | 0.0 | 1.0 | behavioral → relationship → direct → product_id |
| `affinity_first`  | 1.0 | 0.3 | 0.0 | 0.2 | direct → relationship → behavioral → product_id |
| `relationship_only` | 0.0 | 1.0 | 0.0 | 0.0 | relationship → direct → behavioral → product_id |
| `behavioral_only` | 0.0 | 0.0 | 0.0 | 1.0 | behavioral → relationship → direct → product_id |

Query-string weight overrides on the route win over presets.

### 7.3 Attribute weights

`get_attribute_weight(attr_id)`:

- **CORE_ATTRS** (e.g., category, activity, occasion, support_level, fit_type): `1.0`.
- **DESCRIPTIVE_ATTRS** (color, material, visual-only): `0.2`.
- **Others**: `0.5`.

### 7.4 Suppression

Applied before ranking. Products with `recommendation_role="complementary"` bypass functional suppression entirely.

1. **Product suppression** — customer has already purchased this `product_db_id`.
2. **Group suppression** — any product in the same `group_id` purchased within that product's `repurchase_window_days` of its most recent `order_date`. `repurchase_behavior` controls whether the window applies (`one_time` → always suppress, `repurchasable` → suppress only inside the window, `seasonal` → similar).
3. **Functional suppression** — a non-complementary product sharing `type + activity` with an already-purchased product.

Suppressed products are still **scored** (used by the similarity-halo fallback) but are excluded from the ranked output unless `disable_purchase_suppression_for_eval=True`.

### 7.5 Targeting-mode routing

Each attribute has a `targeting_mode` (default: `categorical_affinity`). The mode decides which scoring bucket the attribute contributes to:

| Mode | Behavior |
|---|---|
| `categorical_affinity`  | Soft preference → accumulates into `direct_score` (and `affinity_contribution`). |
| `compatibility_signal`  | Suitability/fit. Positive match × weight × **1.5** → `compatibility_positive_contribution`. Mismatch (when `negative_scoring_enabled`) → `compatibility_negative_contribution`. |
| `categorical_filter`    | Hard gate when `filter_context` is provided; no score contribution. |
| `descriptive_metadata`  | No scoring; surfaced for display only. |

### 7.6 Scoring pipeline (per product)

1. **Direct-affinity pass** — for each product attribute that matches a customer affinity:
   - `categorical_affinity`: `direct_score += affinity_score × attr_weight`.
   - `compatibility_signal`: `compatibility_positive_contribution += affinity_score × attr_weight × 1.5`.

2. **Negative-compatibility pass** (only if `attribute_behaviors.negative_scoring_enabled`):
   - For each `compatibility_signal` attribute the customer has any affinity for, where the product carries an explicit value and no match:
     - If the attribute is **complementary** (e.g., `layering_role`): treat the different value as a *positive* match (the product plays a complementary role, which is desirable).
     - Elif `ordered_values` is present: compute `severity = distance / max_distance`; `penalty = customer_signal_max × attr_weight × 1.5 × severity`.
     - Else: flat penalty.
   - Accumulates into `compatibility_negative_contribution`.

3. **Contextual semantic pass** — for `occasion`, `activity`, `environment`:
   - If customer has affinity and product carries an explicit non-matching value:
   - `contextual_negative_contribution += customer_signal_max × attr_weight × 0.3`.

4. **Relationship pass** — for each attribute the product carries:
   - For each approved `AttributeValueRelationship` whose *source* is that attribute:
     - If customer has affinity for that source: `relationship_score += affinity_score × relationship.strength`.

5. **Popularity pass** — workspace-wide purchase sum for the product.

6. **Behavioral pass** — sum of `strength` on edges `(purchased_product → candidate)` from `ProductBehaviorRelationship`.

7. **Low-signal penalty** — if product signal strength is available:
   - `low_signal_penalty = customer_signal_strength × 0.1 × (1 - product_signal_strength)`.
   - Demotes low-enrichment products more aggressively when the customer has strong preferences.

8. **Final combination**:

```
recommendation_score =
      (direct_score
       + compatibility_positive_contribution
       - compatibility_negative_contribution
       - contextual_negative_contribution
       - low_signal_penalty
      ) * direct_weight
    + relationship_score   * relationship_weight
    + popularity_score     * popularity_weight
    + behavioral_score     * behavioral_weight
```

Default weights (when no preset and no query override): `direct=1.0, relationship=1.0, popularity=0.0, behavioral=0.0` — preserves pre-V6 behavior.

### 7.7 Similarity-halo fallback

If suppression removed a very strong match, boost remaining candidates that share its key characteristics.

Trigger:

```
top_suppressed_score ≥ 0.5
AND top_suppressed_score > top_unsuppressed_score × 1.25
```

Procedure:

1. Mine top-2 suppressed products for these "halo" attributes and weights:
   `category=1.0, occasion=0.9, activity=0.8, support_level=0.7, fit_type=0.5, fit=0.3`.
2. For each unsuppressed candidate:
   - `boost = (overlap_weight / num_halo_attrs) × 0.5 (HALO_BASE_WEIGHT) × suppressed_confidence`.
   - Cap: `boost ≤ base_score × 0.75 (HALO_MAX_RATIO)`.
3. Add `boost` to `recommendation_score`.

`suppressed_confidence` resolves to `match_confidence → product_signal_strength → 0.5` (defensive default).

### 7.8 Diversity shaping

Soft post-ranking pass that preserves raw scores.

```
DIVERSITY_SCAN_WINDOW    = 5
DIVERSITY_CLOSENESS_RATIO = 0.85
MAX_REPEAT_PENALTY        = 0.45   # cap
REPEAT_PENALTY_STEP       = 0.15
```

Procedure:

1. Rank #1 is locked.
2. For each subsequent slot, look at the next `DIVERSITY_SCAN_WINDOW` candidates.
3. For each within the window:
   - If `raw_score ≥ window_top × DIVERSITY_CLOSENESS_RATIO` (close enough to the leader):
     - Apply category-repeat penalty: `penalty = 1 − min(repeats × 0.15, 0.45)`; `adjusted_score = raw_score × penalty`.
   - Otherwise: out of contention for reshuffling; `adjusted_score = raw_score`.
4. Pick the max `adjusted_score` (stable tiebreak prefers earlier position).
5. Increment that category's repeat count and continue.

Net effect: the score leader always wins; dense category runs get broken up among near-tied candidates.

### 7.9 Slot selection, filters, thresholds

Applied per slot, in order:

1. **Hard filters** — `SlotFilter(attribute_id, operator ∈ {eq, in}, value)` AND-combined; non-matching products removed before scoring.
2. **Score threshold** — `recommendation_score < min_score_threshold` discarded.
3. **Scan-depth cap** — stop considering candidates after `max_scan_depth` inspections.
4. **Group diversity** — at most `max_per_group` results per `group_id`.
5. **Cross-slot exclusion** — products (or groups, by `exclusion_level`) selected in previous slots are removed from this slot's pool.

All three of `min_score_threshold`, `max_scan_depth`, `max_per_group` are **adaptive** to customer signal strength:

| Customer signal | min_score_threshold | max_scan_depth | max_per_group |
|---|---|---|---|
| low    | 0.2 | 100 | 1 |
| medium | 0.4 |  50 | 2 |
| high   | 0.6 |  20 | unlimited |

Intuition: a high-signal customer has strong preferences, so the engine can afford to be stricter and stop scanning earlier; a low-signal customer benefits from broader exploration.

### 7.10 Fallback behavior

When the slot's primary algorithm signal is effectively absent (e.g., `behavioral_score ≤ 0` for `behavior_first`), the slot's `fallback_behavior` controls the response:

| `fallback_behavior` | Action |
|---|---|
| `none`     | Return empty results. |
| `direct`   | Re-rank using `direct_score` only. |
| `balanced` | Re-rank using the `balanced` preset's mixed weights. |

`fallback_applied` is reported in `SlotResponse` so clients know substitution happened.

### 7.11 Evaluation mode

Flag: `disable_purchase_suppression_for_eval` (query param, wired by commit `9943364`).

When `True`, purchase-based suppression (sections 1–3 of §7.4) is disabled. Everything else still runs. This lets evaluation scripts score relevance on the raw catalog without purchase-history artifacts distorting benchmarks.

### 7.12 Starter mode

Commit `b7f3d96` ("Starter mode complete") refers to the bundle that is now always on: compatibility attributes, mismatch penalties, signal-confidence metadata, and low-signal correction. "Starter mode" isn't a runtime toggle — it's the baseline configuration shipped after V6/V7 with conservative compatibility defaults and the low-signal penalty active.

---

## 8. End-to-End Data Flow

```
 ┌──────────────────────────────────────────────────────────────────────┐
 │ Phase 1 — Ingest                                                     │
 │   CustomerPurchase rows inserted (POST /purchases)                   │
 │   Product rows populated from seed / feed                            │
 └──────────────────────────────────────────────────────────────────────┘
                           │
                           ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │ Phase 2 — Enrichment                                                 │
 │   scripts/run_real_text_enrichment.py                                │
 │      ↳ attribute_enrichment_service (Claude text)                    │
 │   scripts/run_visual_*  (planned)                                    │
 │      ↳ visual_attribute_enrichment_service                           │
 │   attribute_merge_service → EnrichmentOutput per attribute           │
 │      ↳ ProposedAttributeValueEvents inserted                         │
 │   proposed_value_normalizer → Aggregates (pending review)            │
 │   Manual approve/reject (run_*_review.py)                            │
 │   Approved values → AttributeAllowedValue + ProductAttribute rows    │
 └──────────────────────────────────────────────────────────────────────┘
                           │
                           ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │ Phase 3 — Learned signals                                            │
 │   affinity_service.generate_affinities_from_purchases                │
 │      ↳ CustomerAttributeAffinity                                     │
 │   relationship_engine_service.run_relationship_engine                │
 │      ↳ AttributeValueRelationship (status=suggested)                 │
 │   behavior_engine_service.run_behavior_engine                        │
 │      ↳ ProductBehaviorRelationship  (full refresh)                   │
 └──────────────────────────────────────────────────────────────────────┘
                           │
                           ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │ Phase 4 — Recommendation                                             │
 │   POST /workspaces/{id}/recommendations/slots                        │
 │   signal_strength_service → customer_signal_strength                 │
 │   recommendation_service.get_recommendations (see §7)                │
 │   multi_source_signal_service (optional) → match_confidence          │
 │   SlotResponse[] → client                                            │
 └──────────────────────────────────────────────────────────────────────┘
```

Where `workspace_id`, `customer_id`, and `targeting_mode` flow:

- `workspace_id` — path parameter on every route → explicit filter in every service query → every ORM row.
- `customer_id` — body field on recommendation/signal requests; used as the primary partition for affinity, behavioral, and signal-strength computations.
- `targeting_mode` — attached to the attribute definition; read at scoring time to route contributions to the correct bucket (see §7.5). Also echoed in `MatchedAttribute` for explainability.

---

## 9. API Routes — [app/api/routes](app/api/routes)

All `workspace_id` paths are int (DB PK).

### [health.py](app/api/routes/health.py)

- `GET /health` → `{status: "ok"}`.

### [workspaces.py](app/api/routes/workspaces.py)

- `POST   /workspaces` — create.
- `GET    /workspaces` — list.
- `GET    /workspaces/{workspace_id}` — get.
- `PUT    /workspaces/{workspace_id}` — update.

### [users.py](app/api/routes/users.py)

- `POST /users`, `GET /users/{user_id}`, `GET /users`.
- `POST   /workspaces/{workspace_id}/members` — add `{user_id, role}`.
- `GET    /workspaces/{workspace_id}/members`.
- `DELETE /workspaces/{workspace_id}/members/{user_id}`.

### [purchases.py](app/api/routes/purchases.py)

- `POST /workspaces/{workspace_id}/purchases` — bulk create (`list[PurchaseCreate]`).

### [affinities.py](app/api/routes/affinities.py)

- `POST /workspaces/{workspace_id}/affinities` — bulk create.
- `GET  /workspaces/{workspace_id}/affinities?min_score=&sort_by_score_desc=`.
- `POST /workspaces/{workspace_id}/affinities/generate` — runs `generate_affinities_from_purchases`.

### [relationships.py](app/api/routes/relationships.py)

- `GET   /workspaces/{workspace_id}/relationships`.
- `PATCH /workspaces/{workspace_id}/relationships/{relationship_id}` — approve/reject (sets `status`).

### [behavioral_relationships.py](app/api/routes/behavioral_relationships.py)

- `GET /workspaces/{workspace_id}/behavioral-relationships`.

### [recommendations.py](app/api/routes/recommendations.py)

- **Legacy:** `GET /workspaces/{workspace_id}/recommendations/{customer_id}`
  - Query: `min_score?`, `top_n?`, `direct_weight?`, `relationship_weight?`, `popularity_weight?`, `behavioral_weight?`.
  - Returns `list[RecommendationRead]`.
- **Single slot:** `POST /workspaces/{workspace_id}/recommendations/slot`
  - Body: `SlotRequest`. Returns `SlotResponse`.
- **Multi-slot:** `POST /workspaces/{workspace_id}/recommendations/slots`
  - Body: `MultiSlotRequest`. Returns `MultiSlotResponse`.

Slot endpoint logic:
1. Compute `customer_signal_strength` once.
2. For each slot: resolve adaptive `max_per_group`, `max_scan_depth`, `min_score_threshold` (see §7.9).
3. Call `recommendation_service.get_recommendations(...)` with slot-specific filters, weights, and previously-selected product/group exclusion sets.
4. Accumulate selected IDs for cross-slot exclusion on the next slot.

### [signal_strength.py](app/api/routes/signal_strength.py)

- `POST /workspaces/{workspace_id}/signal/customer` — body `{customer_ids: list[str]}` → `BatchSignalStrengthRead`.
- `GET  /workspaces/{workspace_id}/signal/audience?customer_ids=` → `AudienceSignalRead`.

---

## 10. Alembic Migrations — [alembic/versions](alembic/versions)

| # | File | Purpose |
|---|---|---|
| 001 | [001_initial_tables.py](alembic/versions/001_initial_tables.py) | `workspaces`, `users`, `workspace_users`, `products`, `product_attributes`, `customer_purchases`, `customer_attribute_affinities`, `attribute_value_relationships`, `audit_logs`. |
| 002 | [002_v2_additions.py](alembic/versions/002_v2_additions.py) | Additional v2 product/attribute fields. |
| 003 | [003_add_product_db_id_to_purchases.py](alembic/versions/003_add_product_db_id_to_purchases.py) | Add `product_db_id` FK on `customer_purchases` (enables joins while keeping denormalized `product_id`). |
| 004 | [004_add_recommendation_role.py](alembic/versions/004_add_recommendation_role.py) | `products.recommendation_role` (`same_use_case` / `complementary`). |
| 005 | [005_add_strength_to_relationships.py](alembic/versions/005_add_strength_to_relationships.py) | `attribute_value_relationships.strength` (normalized weight). |
| 006 | [006_add_product_behavior_relationships.py](alembic/versions/006_add_product_behavior_relationships.py) | New `product_behavior_relationships` table. |
| 007 | [007_add_proposed_attribute_values.py](alembic/versions/007_add_proposed_attribute_values.py) | Event + aggregate tables for proposed values. |
| 008 | [008_add_attribute_allowed_values.py](alembic/versions/008_add_attribute_allowed_values.py) | Workspace-scoped `attribute_allowed_values` overrides. |
| 009 | [009_add_merge_reason_review_note.py](alembic/versions/009_add_merge_reason_review_note.py) | `merge_reason`, `review_note` on proposed-value aggregate (audit for hierarchy merges). |
| 010 | [010_add_proposed_attribute_tables.py](alembic/versions/010_add_proposed_attribute_tables.py) | Event + aggregate tables for proposing **new attributes** (separate from values). |

Alembic config: [alembic.ini](alembic.ini), env at [alembic/env.py](alembic/env.py). Note: [app/main.py](app/main.py) *also* calls `Base.metadata.create_all` at startup — if models drift from migrations, tables get created without the migration-tracked schema.

---

## 11. Scripts — [scripts](scripts)

Each script is a self-contained CLI runner that uses `.env` for `DATABASE_URL` and `ANTHROPIC_API_KEY`.

| Script | Purpose |
|---|---|
| [seed_starter_dataset.py](scripts/seed_starter_dataset.py) | Loads `seed_data/*.json` + `attribute_definitions.json` into a starter workspace. |
| [run_text_enrichment.py](scripts/run_text_enrichment.py) | Demo text enrichment run; writes `products_enriched.json`. |
| [run_real_text_enrichment.py](scripts/run_real_text_enrichment.py) | Full-catalog enrichment; writes `products_enriched_real.json`. |
| [run_enrichment_demo.py](scripts/run_enrichment_demo.py) | End-to-end enrichment demo (prompts + merge + normalizer). |
| [run_attribute_discovery_pipeline.py](scripts/run_attribute_discovery_pipeline.py) | Proposes **new attribute dimensions** for the taxonomy. |
| [run_proposed_value_pipeline_demo.py](scripts/run_proposed_value_pipeline_demo.py) | Proposes new **values** inside existing attributes. |
| [run_activity_type_discovery.py](scripts/run_activity_type_discovery.py) | Activity-specific value discovery. |
| [run_activity_type_review.py](scripts/run_activity_type_review.py) | CLI review (approve/reject/merge) for activity proposals. |
| [run_mom_stage_discovery.py](scripts/run_mom_stage_discovery.py) | mom_stage discovery (pregnancy/postpartum). |
| [run_mom_stage_review.py](scripts/run_mom_stage_review.py) | CLI review for mom_stage proposals. |
| [run_approve_discovered_attributes.py](scripts/run_approve_discovered_attributes.py) | Bulk-approve proposed attributes into the taxonomy. |
| [run_recommendations_c001.py](scripts/run_recommendations_c001.py) | Generate recommendations for customer `c001`. |
| [run_recommendations_c004_eval.py](scripts/run_recommendations_c004_eval.py) | Runs `c004` with evaluation-mode suppression disabled. |
| [run_multi_customer_eval.py](scripts/run_multi_customer_eval.py) | Batch recommendation + scoring breakdown across customers. |
| [run_multi_customer_eval_expanded.py](scripts/run_multi_customer_eval_expanded.py) | Extended eval, more metrics. |
| [run_scoring_breakdown_demo.py](scripts/run_scoring_breakdown_demo.py) | Dumps per-component score contributions for debugging. |

---

## 12. Seed Data — [seed_data](seed_data)

| File | Shape | Purpose |
|---|---|---|
| [attribute_definitions.json](seed_data/attribute_definitions.json) | JSON array (~296 lines) | Canonical taxonomy: 25+ attributes across `object_type ∈ {product, customer}`, each with `class_name`, `value_mode`, `allowed_values`, `behavior` flags, `targeting_mode`. Static fallback when no workspace override exists in `attribute_allowed_values`. |
| [customers.json](seed_data/customers.json) | JSON array | Starter customer list. |
| [products.json](seed_data/products.json) | JSON array | Starter product catalog: `product_id`, `sku`, `name`, `description`, `category`. |
| [purchases.json](seed_data/purchases.json) | JSON array | Starter purchase history: `customer_id`, `product_id`, `order_date`, `quantity`, `revenue`. |
| [recommendation_scenarios.json](seed_data/recommendation_scenarios.json) | JSON object | Scenario dictionary `customer_id → expected_top_products` for evaluation. |
| [apparel_feed.csv](seed_data/apparel_feed.csv) | CSV | Real apparel product feed used by enrichment scripts. |
| [ground_truth_product_attributes.csv](seed_data/ground_truth_product_attributes.csv) | CSV | Hand-labeled attribute ground truth for evaluating enrichment accuracy. |

---

## 13. Tests — [tests](tests)

41 test files. Highlights:

### Recommendations — algorithm evolution

- [test_recommendations.py](tests/test_recommendations.py) — baseline.
- [test_recommendations_v2.py](tests/test_recommendations_v2.py) through [test_recommendations_v7.py](tests/test_recommendations_v7.py) plus [test_recommendations_v6r.py](tests/test_recommendations_v6r.py) — one file per algorithm generation. Each pins the contract for its era.
- [test_recommendations_negative_compatibility.py](tests/test_recommendations_negative_compatibility.py) — mismatch-penalty behavior.
- [test_refill.py](tests/test_refill.py) — repurchase-window suppression.

### Slot system

- [test_slot_recommendations.py](tests/test_slot_recommendations.py), [test_multi_slot.py](tests/test_multi_slot.py), [test_slot_filters.py](tests/test_slot_filters.py), [test_slot_nested_schema.py](tests/test_slot_nested_schema.py), [test_slot_diversity.py](tests/test_slot_diversity.py), [test_slot_fallback.py](tests/test_slot_fallback.py), [test_fallback_behavior.py](tests/test_fallback_behavior.py), [test_cross_slot_exclusion.py](tests/test_cross_slot_exclusion.py), [test_exclusion_level.py](tests/test_exclusion_level.py).

### Adaptive slot tuning

- [test_adaptive_diversity.py](tests/test_adaptive_diversity.py), [test_scan_depth.py](tests/test_scan_depth.py), [test_min_score_threshold.py](tests/test_min_score_threshold.py), [test_product_uniqueness.py](tests/test_product_uniqueness.py).

### Engines & pipelines

- [test_behavior_engine.py](tests/test_behavior_engine.py), [test_relationship_engine.py](tests/test_relationship_engine.py), [test_signal_strength.py](tests/test_signal_strength.py), [test_multi_source_signal.py](tests/test_multi_source_signal.py), [test_audience_signal.py](tests/test_audience_signal.py).

### Enrichment / taxonomy

- [test_attribute_enrichment_prompts.py](tests/test_attribute_enrichment_prompts.py), [test_attribute_merge_service.py](tests/test_attribute_merge_service.py), [test_attribute_discovery.py](tests/test_attribute_discovery.py), [test_attribute_taxonomy.py](tests/test_attribute_taxonomy.py), [test_visual_attribute_enrichment_service.py](tests/test_visual_attribute_enrichment_service.py).

### CRUD / integration

- [test_workspaces.py](tests/test_workspaces.py), [test_workspace_members.py](tests/test_workspace_members.py), [test_users.py](tests/test_users.py), [test_purchases.py](tests/test_purchases.py), [test_affinities.py](tests/test_affinities.py), [test_affinity_generate.py](tests/test_affinity_generate.py), [test_relationships.py](tests/test_relationships.py), [test_health.py](tests/test_health.py).

Shared fixtures: [tests/conftest.py](tests/conftest.py), [tests/fixtures/](tests/fixtures).

---

## 14. Observations (no action taken)

Listed for visibility only — per the CLAUDE.MD rule, none of these were changed.

1. **Visual enrichment** ([visual_attribute_enrichment_service.py](app/services/visual_attribute_enrichment_service.py)) is present but not wired into a shipping script — merge logic already handles its output.
2. **AuditLog** ([app/models/audit_log.py](app/models/audit_log.py)) — schema exists, never written to.
3. **Workspace roles** — [WorkspaceUser](app/models/workspace_user.py) has a `role` column but no RBAC enforcement at the route layer; any member is effectively admin.
4. **Debug logging in recommendation_service** — extensive `logger.warning()` calls flagged "DEBUG"; noisy in production.
5. **Startup `create_all` + Alembic** — [app/main.py](app/main.py) calls `Base.metadata.create_all(bind=engine)` at import, which can shadow migration state in environments that don't run `alembic upgrade`.
6. **Enum-like string columns** — `repurchase_behavior` and `recommendation_role` are free-text columns without CHECK constraints or SQLAlchemy enums.
7. **Conftest DB dialect** — tests appear to use SQLite; Postgres-only SQL (if any creeps in) wouldn't be caught.
8. **V2–V7 recommendation test files** — valuable as a regression ladder, but likely consolidation candidates once V7 stabilizes.
9. **Full-refresh behavior engine** — [run_behavior_engine](app/services/behavior_engine_service.py) deletes then reinserts all edges for the workspace; no incremental update path.
10. **Hierarchy support** — `merge_reason` field is in place (`flattened_child` etc.) but no parent/child attribute hierarchy is actually stored yet.
11. **Similarity-halo confidence default** — falls back to `0.5` when neither `match_confidence` nor `product_signal_strength` is available (defensive, but masks missing-data cases).

---

## 15. Recent Git History (context)

| Commit | Summary |
|---|---|
| `543fecd` | Checkpoint: wire real Anthropic enrichment + normalization pipeline. |
| `9943364` | Add evaluation mode to disable purchase-based suppression. |
| `b7f3d96` | Starter mode complete: compatibility, mismatch penalties, signal confidence, low-signal correction. |
| `c8f5984` | feat: add attribute enrichment service. |
| `fa05dd8` | Wire `targeting_mode` into recommendation scoring engine. |

Current branch: `partial-test`. Main branch: `main`.

---

*End of analysis.*
