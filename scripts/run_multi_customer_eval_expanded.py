"""Expanded multi-customer evaluation with recall-expansion catalog.

Seeds the original EVAL_ catalog + 16 new PX21..PX36 products.  Each new
product carries only a `category` attribute at the catalog level; the other
four attributes (activity_type, workout_intensity, environment, layering_role)
are produced by MODEL_OUTPUTS following the same fixture-based convention
used in tests/fixtures/workout_travel_test_data.py, then materialized as
ProductAttribute rows so the live scoring engine picks them up.

Runs the 4-customer evaluation twice — once on the original 12-product
catalog (BEFORE) and once on the expanded 28-product catalog (AFTER) — and
prints a side-by-side comparison.  Rolls back all DB writes at the end.

Extraction conventions for MODEL_OUTPUTS:
- activity_type / workout_intensity: allowed_values == [] in the taxonomy →
  all emitted values go in `proposed_values` (no reasoning_mode).
- environment / layering_role: allowed_values populated → emitted values go
  in `values` with reasoning_mode ("inferred" for contextual_semantic,
  "suitability" for compatibility).
- Conservative: confidence >= 0.8 AND at least one evidence span.
- warnings=["no_supported_value_found"] when the description gives no signal.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.database import SessionLocal
from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.product import Product, ProductAttribute
from app.models.workspace import Workspace
from app.schemas.attribute_enrichment import AttributeDefinition
from app.services.recommendation_service import get_recommendations

WORKSPACE_SLUG = "personafirst-starter"
SEED_DIR = Path(__file__).resolve().parent.parent / "seed_data"
TOP_N = 5
CONF_THRESHOLD = 0.8


# -------- Original evaluation catalog (same 12 products as run_multi_customer_eval.py) --------

ORIGINAL_PRODUCTS = [
    {"product_id": "EVAL_HIIT_BRA", "sku": "E-01", "name": "HIIT High-Impact Bra",
     "attrs": {"category": "bras", "activity_type": "hiit", "workout_intensity": "high",
               "environment": "indoor", "layering_role": "base"}},
    {"product_id": "EVAL_HIIT_MID", "sku": "E-02", "name": "HIIT Training Hoodie",
     "attrs": {"category": "tops", "activity_type": "hiit", "workout_intensity": "high",
               "environment": "indoor", "layering_role": "mid"}},
    {"product_id": "EVAL_RUN_BASE", "sku": "E-03", "name": "Sprint Base Tee",
     "attrs": {"category": "tops", "activity_type": "running", "workout_intensity": "high",
               "environment": "indoor", "layering_role": "base"}},
    {"product_id": "EVAL_RUN_MID_IN", "sku": "E-04", "name": "Treadmill Mid Layer",
     "attrs": {"category": "tops", "activity_type": "running", "workout_intensity": "moderate",
               "environment": "indoor", "layering_role": "mid"}},
    {"product_id": "EVAL_RUN_OUTER", "sku": "E-05", "name": "Running Wind Shell",
     "attrs": {"category": "jackets", "activity_type": "running", "workout_intensity": "high",
               "environment": "outdoor", "layering_role": "outer"}},
    {"product_id": "EVAL_YOGA_BASE", "sku": "E-06", "name": "Flow Yoga Leggings",
     "attrs": {"category": "leggings", "activity_type": "yoga", "workout_intensity": "low",
               "environment": "indoor", "layering_role": "base"}},
    {"product_id": "EVAL_YOGA_MID", "sku": "E-07", "name": "Yin Yoga Wrap",
     "attrs": {"category": "tops", "activity_type": "yoga", "workout_intensity": "low",
               "environment": "indoor", "layering_role": "mid"}},
    {"product_id": "EVAL_HIKE_BASE", "sku": "E-08", "name": "Trail Merino Tee",
     "attrs": {"category": "tops", "activity_type": "hiking", "workout_intensity": "moderate",
               "environment": "outdoor", "layering_role": "base"}},
    {"product_id": "EVAL_HIKE_MID", "sku": "E-09", "name": "Trail Fleece Mid",
     "attrs": {"category": "tops", "activity_type": "hiking", "workout_intensity": "moderate",
               "environment": "outdoor", "layering_role": "mid"}},
    {"product_id": "EVAL_HIKE_OUTER", "sku": "E-10", "name": "Hiker Shell Jacket",
     "attrs": {"category": "jackets", "activity_type": "hiking", "workout_intensity": "moderate",
               "environment": "outdoor", "layering_role": "outer"}},
    {"product_id": "EVAL_LOUNGE_SET", "sku": "E-11", "name": "Recovery Lounge Set",
     "attrs": {"category": "sets", "activity_type": "lounge", "workout_intensity": "low",
               "environment": "indoor", "layering_role": "base"}},
    {"product_id": "EVAL_PILATES", "sku": "E-12", "name": "Pilates Core Top",
     "attrs": {"category": "tops", "activity_type": "pilates", "workout_intensity": "low",
               "environment": "studio", "layering_role": "base"}},
]


# -------- 16 new products (category only at catalog level) --------

NEW_PRODUCTS = [
    {"product_id": "PX21", "name": "Studio Yoga Tank",
     "description": "Soft tank designed for yoga flow, stretching, and low-impact studio practice. Breathable fabric and unrestricted movement for indoor sessions.",
     "attributes": {"category": "tops"}},
    {"product_id": "PX22", "name": "Pilates Support Bra",
     "description": "Light-support bra for pilates and controlled studio movement. Comfortable for low-intensity training and indoor classes.",
     "attributes": {"category": "bras"}},
    {"product_id": "PX23", "name": "Recovery Wrap Cardigan",
     "description": "Lightweight cardigan for post-class recovery and gentle movement. Ideal as a mid layer for indoor studio and lounge transitions.",
     "attributes": {"category": "tops"}},
    {"product_id": "PX24", "name": "Mobility Studio Leggings",
     "description": "Flexible leggings built for yoga, mobility drills, and low-impact indoor training. Soft compression for studio comfort.",
     "attributes": {"category": "leggings"}},
    {"product_id": "PX25", "name": "Trail Hiking Vest",
     "description": "Lightweight vest for hiking and outdoor trail movement. Works as a mid layer over a base layer in cool weather.",
     "attributes": {"category": "tops"}},
    {"product_id": "PX26", "name": "Outdoor Trek Shorts",
     "description": "Durable shorts for hiking and rugged outdoor walks. Quick-dry fabric and freedom of movement for moderate-effort trail use.",
     "attributes": {"category": "shorts"}},
    {"product_id": "PX27", "name": "Mountain Fleece Midlayer",
     "description": "Warm fleece designed as a mid layer for hiking and outdoor exploration. Built for moderate activity in changing temperatures.",
     "attributes": {"category": "tops"}},
    {"product_id": "PX28", "name": "Storm Trail Shell",
     "description": "Protective outer shell for hiking in wind and rain. Outdoor-focused design with weather resistance and layering versatility.",
     "attributes": {"category": "jackets"}},
    {"product_id": "PX29", "name": "Everyday Running Tee",
     "description": "Sweat-wicking tee for running and steady aerobic sessions. Lightweight construction for indoor treadmill or outdoor road runs.",
     "attributes": {"category": "tops"}},
    {"product_id": "PX30", "name": "Run Warm Midlayer",
     "description": "Breathable mid layer for running in cool conditions. Built for moderate running effort and easy layering over a base top.",
     "attributes": {"category": "tops"}},
    {"product_id": "PX31", "name": "All-Weather Run Shell",
     "description": "Packable outer shell designed for running in wind and light rain. Suitable for outdoor runs and variable conditions.",
     "attributes": {"category": "jackets"}},
    {"product_id": "PX32", "name": "Recovery Joggers",
     "description": "Soft joggers for post-run recovery, rest days, and light movement. Comfortable for low-intensity wear indoors or casually outside.",
     "attributes": {"category": "pants"}},
    {"product_id": "PX33", "name": "Studio-to-Street Jacket",
     "description": "Versatile jacket designed for indoor studio warmups and everyday outdoor errands. Easy layering from gym to street.",
     "attributes": {"category": "jackets"}},
    {"product_id": "PX34", "name": "Travel Yoga Pants",
     "description": "Packable yoga pants for studio sessions and travel days. Wrinkle-resistant fabric with comfort for low-intensity movement on the go.",
     "attributes": {"category": "leggings"}},
    {"product_id": "PX35", "name": "Outdoor Training Hoodie",
     "description": "Training hoodie for bootcamp workouts and outdoor conditioning. Mid-layer warmth for moderate-to-high effort sessions.",
     "attributes": {"category": "tops"}},
    {"product_id": "PX36", "name": "Packable Midlayer Zip",
     "description": "Light mid layer that packs small for travel and transitional weather. Useful for layering during outdoor walks or active commutes.",
     "attributes": {"category": "tops"}},
]


# -------- MODEL_OUTPUTS: same shape as workout_travel_test_data.py --------
# Convention:
#   ("PID", "attribute_name") -> {
#       "attribute_name", "attribute_class",
#       "values":          [{value, confidence, evidence, reasoning_mode}]   # for allowed_values-defined taxonomies
#       "proposed_values": [{value, confidence, evidence}]                    # for empty-taxonomy attrs
#       "warnings":        [...]
#   }

MODEL_OUTPUTS = {
    # ---------------- PX21 — Studio Yoga Tank ----------------
    ("PX21", "activity_type"): {
        "attribute_name": "activity_type", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "yoga", "confidence": 0.96,
             "evidence": ["\"designed for yoga flow, stretching\""]},
        ],
        "warnings": [],
    },
    ("PX21", "workout_intensity"): {
        "attribute_name": "workout_intensity", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "low", "confidence": 0.94,
             "evidence": ["\"low-impact studio practice\""]},
        ],
        "warnings": [],
    },
    ("PX21", "environment"): {
        "attribute_name": "environment", "attribute_class": "contextual_semantic",
        "values": [
            {"value": "studio", "confidence": 0.94,
             "evidence": ["\"low-impact studio practice\""], "reasoning_mode": "inferred"},
            {"value": "indoor", "confidence": 0.92,
             "evidence": ["\"for indoor sessions\""], "reasoning_mode": "inferred"},
        ],
        "proposed_values": [], "warnings": [],
    },
    ("PX21", "layering_role"): {
        "attribute_name": "layering_role", "attribute_class": "compatibility",
        "values": [], "proposed_values": [], "warnings": ["no_supported_value_found"],
    },

    # ---------------- PX22 — Pilates Support Bra ----------------
    ("PX22", "activity_type"): {
        "attribute_name": "activity_type", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "pilates", "confidence": 0.96,
             "evidence": ["\"for pilates and controlled studio movement\""]},
        ],
        "warnings": [],
    },
    ("PX22", "workout_intensity"): {
        "attribute_name": "workout_intensity", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "low", "confidence": 0.94,
             "evidence": ["\"Comfortable for low-intensity training\""]},
        ],
        "warnings": [],
    },
    ("PX22", "environment"): {
        "attribute_name": "environment", "attribute_class": "contextual_semantic",
        "values": [
            {"value": "studio", "confidence": 0.93,
             "evidence": ["\"controlled studio movement\""], "reasoning_mode": "inferred"},
            {"value": "indoor", "confidence": 0.91,
             "evidence": ["\"indoor classes\""], "reasoning_mode": "inferred"},
        ],
        "proposed_values": [], "warnings": [],
    },
    ("PX22", "layering_role"): {
        "attribute_name": "layering_role", "attribute_class": "compatibility",
        "values": [], "proposed_values": [], "warnings": ["no_supported_value_found"],
    },

    # ---------------- PX23 — Recovery Wrap Cardigan ----------------
    ("PX23", "activity_type"): {
        "attribute_name": "activity_type", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "lounge", "confidence": 0.88,
             "evidence": ["\"lounge transitions\""]},
        ],
        "warnings": [],
    },
    ("PX23", "workout_intensity"): {
        "attribute_name": "workout_intensity", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "low", "confidence": 0.91,
             "evidence": ["\"post-class recovery and gentle movement\""]},
        ],
        "warnings": [],
    },
    ("PX23", "environment"): {
        "attribute_name": "environment", "attribute_class": "contextual_semantic",
        "values": [
            {"value": "studio", "confidence": 0.90,
             "evidence": ["\"indoor studio and lounge transitions\""], "reasoning_mode": "inferred"},
            {"value": "indoor", "confidence": 0.92,
             "evidence": ["\"for indoor studio\""], "reasoning_mode": "inferred"},
        ],
        "proposed_values": [], "warnings": [],
    },
    ("PX23", "layering_role"): {
        "attribute_name": "layering_role", "attribute_class": "compatibility",
        "values": [
            {"value": "mid", "confidence": 0.96,
             "evidence": ["\"Ideal as a mid layer\""], "reasoning_mode": "suitability"},
        ],
        "proposed_values": [], "warnings": [],
    },

    # ---------------- PX24 — Mobility Studio Leggings ----------------
    ("PX24", "activity_type"): {
        "attribute_name": "activity_type", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "yoga", "confidence": 0.95,
             "evidence": ["\"built for yoga, mobility drills\""]},
        ],
        "warnings": [],
    },
    ("PX24", "workout_intensity"): {
        "attribute_name": "workout_intensity", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "low", "confidence": 0.93,
             "evidence": ["\"low-impact indoor training\""]},
        ],
        "warnings": [],
    },
    ("PX24", "environment"): {
        "attribute_name": "environment", "attribute_class": "contextual_semantic",
        "values": [
            {"value": "studio", "confidence": 0.90,
             "evidence": ["\"studio comfort\""], "reasoning_mode": "inferred"},
            {"value": "indoor", "confidence": 0.92,
             "evidence": ["\"low-impact indoor training\""], "reasoning_mode": "inferred"},
        ],
        "proposed_values": [], "warnings": [],
    },
    ("PX24", "layering_role"): {
        "attribute_name": "layering_role", "attribute_class": "compatibility",
        "values": [], "proposed_values": [], "warnings": ["no_supported_value_found"],
    },

    # ---------------- PX25 — Trail Hiking Vest ----------------
    ("PX25", "activity_type"): {
        "attribute_name": "activity_type", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "hiking", "confidence": 0.96,
             "evidence": ["\"for hiking and outdoor trail movement\""]},
        ],
        "warnings": [],
    },
    ("PX25", "workout_intensity"): {
        "attribute_name": "workout_intensity", "attribute_class": "contextual_semantic",
        "values": [], "proposed_values": [], "warnings": ["no_supported_value_found"],
    },
    ("PX25", "environment"): {
        "attribute_name": "environment", "attribute_class": "contextual_semantic",
        "values": [
            {"value": "outdoor", "confidence": 0.96,
             "evidence": ["\"outdoor trail movement\""], "reasoning_mode": "inferred"},
        ],
        "proposed_values": [], "warnings": [],
    },
    ("PX25", "layering_role"): {
        "attribute_name": "layering_role", "attribute_class": "compatibility",
        "values": [
            {"value": "mid", "confidence": 0.96,
             "evidence": ["\"Works as a mid layer over a base layer\""], "reasoning_mode": "suitability"},
        ],
        "proposed_values": [], "warnings": [],
    },

    # ---------------- PX26 — Outdoor Trek Shorts ----------------
    ("PX26", "activity_type"): {
        "attribute_name": "activity_type", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "hiking", "confidence": 0.96,
             "evidence": ["\"for hiking and rugged outdoor walks\""]},
        ],
        "warnings": [],
    },
    ("PX26", "workout_intensity"): {
        "attribute_name": "workout_intensity", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "moderate", "confidence": 0.93,
             "evidence": ["\"moderate-effort trail use\""]},
        ],
        "warnings": [],
    },
    ("PX26", "environment"): {
        "attribute_name": "environment", "attribute_class": "contextual_semantic",
        "values": [
            {"value": "outdoor", "confidence": 0.96,
             "evidence": ["\"rugged outdoor walks\""], "reasoning_mode": "inferred"},
        ],
        "proposed_values": [], "warnings": [],
    },
    ("PX26", "layering_role"): {
        "attribute_name": "layering_role", "attribute_class": "compatibility",
        "values": [], "proposed_values": [], "warnings": ["no_supported_value_found"],
    },

    # ---------------- PX27 — Mountain Fleece Midlayer ----------------
    ("PX27", "activity_type"): {
        "attribute_name": "activity_type", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "hiking", "confidence": 0.95,
             "evidence": ["\"for hiking and outdoor exploration\""]},
        ],
        "warnings": [],
    },
    ("PX27", "workout_intensity"): {
        "attribute_name": "workout_intensity", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "moderate", "confidence": 0.92,
             "evidence": ["\"Built for moderate activity\""]},
        ],
        "warnings": [],
    },
    ("PX27", "environment"): {
        "attribute_name": "environment", "attribute_class": "contextual_semantic",
        "values": [
            {"value": "outdoor", "confidence": 0.95,
             "evidence": ["\"outdoor exploration\""], "reasoning_mode": "inferred"},
        ],
        "proposed_values": [], "warnings": [],
    },
    ("PX27", "layering_role"): {
        "attribute_name": "layering_role", "attribute_class": "compatibility",
        "values": [
            {"value": "mid", "confidence": 0.97,
             "evidence": ["\"designed as a mid layer\""], "reasoning_mode": "suitability"},
        ],
        "proposed_values": [], "warnings": [],
    },

    # ---------------- PX28 — Storm Trail Shell ----------------
    ("PX28", "activity_type"): {
        "attribute_name": "activity_type", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "hiking", "confidence": 0.95,
             "evidence": ["\"for hiking in wind and rain\""]},
        ],
        "warnings": [],
    },
    ("PX28", "workout_intensity"): {
        "attribute_name": "workout_intensity", "attribute_class": "contextual_semantic",
        "values": [], "proposed_values": [], "warnings": ["no_supported_value_found"],
    },
    ("PX28", "environment"): {
        "attribute_name": "environment", "attribute_class": "contextual_semantic",
        "values": [
            {"value": "outdoor", "confidence": 0.97,
             "evidence": ["\"Outdoor-focused design\""], "reasoning_mode": "inferred"},
        ],
        "proposed_values": [], "warnings": [],
    },
    ("PX28", "layering_role"): {
        "attribute_name": "layering_role", "attribute_class": "compatibility",
        "values": [
            {"value": "outer", "confidence": 0.97,
             "evidence": ["\"Protective outer shell\""], "reasoning_mode": "suitability"},
        ],
        "proposed_values": [], "warnings": [],
    },

    # ---------------- PX29 — Everyday Running Tee ----------------
    ("PX29", "activity_type"): {
        "attribute_name": "activity_type", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "running", "confidence": 0.96,
             "evidence": ["\"for running and steady aerobic sessions\""]},
        ],
        "warnings": [],
    },
    ("PX29", "workout_intensity"): {
        "attribute_name": "workout_intensity", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "moderate", "confidence": 0.85,
             "evidence": ["\"steady aerobic sessions\""]},
        ],
        "warnings": [],
    },
    ("PX29", "environment"): {
        "attribute_name": "environment", "attribute_class": "contextual_semantic",
        "values": [
            {"value": "indoor", "confidence": 0.92,
             "evidence": ["\"indoor treadmill\""], "reasoning_mode": "inferred"},
            {"value": "outdoor", "confidence": 0.92,
             "evidence": ["\"outdoor road runs\""], "reasoning_mode": "inferred"},
        ],
        "proposed_values": [], "warnings": [],
    },
    ("PX29", "layering_role"): {
        "attribute_name": "layering_role", "attribute_class": "compatibility",
        "values": [], "proposed_values": [], "warnings": ["no_supported_value_found"],
    },

    # ---------------- PX30 — Run Warm Midlayer ----------------
    ("PX30", "activity_type"): {
        "attribute_name": "activity_type", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "running", "confidence": 0.96,
             "evidence": ["\"for running in cool conditions\""]},
        ],
        "warnings": [],
    },
    ("PX30", "workout_intensity"): {
        "attribute_name": "workout_intensity", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "moderate", "confidence": 0.93,
             "evidence": ["\"Built for moderate running effort\""]},
        ],
        "warnings": [],
    },
    ("PX30", "environment"): {
        "attribute_name": "environment", "attribute_class": "contextual_semantic",
        "values": [], "proposed_values": [], "warnings": ["no_supported_value_found"],
    },
    ("PX30", "layering_role"): {
        "attribute_name": "layering_role", "attribute_class": "compatibility",
        "values": [
            {"value": "mid", "confidence": 0.96,
             "evidence": ["\"Breathable mid layer\""], "reasoning_mode": "suitability"},
        ],
        "proposed_values": [], "warnings": [],
    },

    # ---------------- PX31 — All-Weather Run Shell ----------------
    ("PX31", "activity_type"): {
        "attribute_name": "activity_type", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "running", "confidence": 0.95,
             "evidence": ["\"designed for running in wind and light rain\""]},
        ],
        "warnings": [],
    },
    ("PX31", "workout_intensity"): {
        "attribute_name": "workout_intensity", "attribute_class": "contextual_semantic",
        "values": [], "proposed_values": [], "warnings": ["no_supported_value_found"],
    },
    ("PX31", "environment"): {
        "attribute_name": "environment", "attribute_class": "contextual_semantic",
        "values": [
            {"value": "outdoor", "confidence": 0.94,
             "evidence": ["\"Suitable for outdoor runs\""], "reasoning_mode": "inferred"},
        ],
        "proposed_values": [], "warnings": [],
    },
    ("PX31", "layering_role"): {
        "attribute_name": "layering_role", "attribute_class": "compatibility",
        "values": [
            {"value": "outer", "confidence": 0.96,
             "evidence": ["\"Packable outer shell\""], "reasoning_mode": "suitability"},
        ],
        "proposed_values": [], "warnings": [],
    },

    # ---------------- PX32 — Recovery Joggers ----------------
    ("PX32", "activity_type"): {
        "attribute_name": "activity_type", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "lounge", "confidence": 0.88,
             "evidence": ["\"rest days, and light movement\""]},
        ],
        "warnings": [],
    },
    ("PX32", "workout_intensity"): {
        "attribute_name": "workout_intensity", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "low", "confidence": 0.93,
             "evidence": ["\"low-intensity wear\""]},
        ],
        "warnings": [],
    },
    ("PX32", "environment"): {
        "attribute_name": "environment", "attribute_class": "contextual_semantic",
        "values": [
            {"value": "indoor", "confidence": 0.90,
             "evidence": ["\"low-intensity wear indoors\""], "reasoning_mode": "inferred"},
            {"value": "outdoor", "confidence": 0.85,
             "evidence": ["\"casually outside\""], "reasoning_mode": "inferred"},
        ],
        "proposed_values": [], "warnings": [],
    },
    ("PX32", "layering_role"): {
        "attribute_name": "layering_role", "attribute_class": "compatibility",
        "values": [], "proposed_values": [], "warnings": ["no_supported_value_found"],
    },

    # ---------------- PX33 — Studio-to-Street Jacket ----------------
    ("PX33", "activity_type"): {
        "attribute_name": "activity_type", "attribute_class": "contextual_semantic",
        "values": [], "proposed_values": [], "warnings": ["no_supported_value_found"],
    },
    ("PX33", "workout_intensity"): {
        "attribute_name": "workout_intensity", "attribute_class": "contextual_semantic",
        "values": [], "proposed_values": [], "warnings": ["no_supported_value_found"],
    },
    ("PX33", "environment"): {
        "attribute_name": "environment", "attribute_class": "contextual_semantic",
        "values": [
            {"value": "studio", "confidence": 0.88,
             "evidence": ["\"indoor studio warmups\""], "reasoning_mode": "inferred"},
            {"value": "indoor", "confidence": 0.90,
             "evidence": ["\"indoor studio warmups\""], "reasoning_mode": "inferred"},
            {"value": "outdoor", "confidence": 0.86,
             "evidence": ["\"everyday outdoor errands\""], "reasoning_mode": "inferred"},
        ],
        "proposed_values": [], "warnings": [],
    },
    ("PX33", "layering_role"): {
        "attribute_name": "layering_role", "attribute_class": "compatibility",
        "values": [
            {"value": "outer", "confidence": 0.84,
             "evidence": ["\"Versatile jacket\"", "\"Easy layering from gym to street\""],
             "reasoning_mode": "suitability"},
        ],
        "proposed_values": [], "warnings": [],
    },

    # ---------------- PX34 — Travel Yoga Pants ----------------
    ("PX34", "activity_type"): {
        "attribute_name": "activity_type", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "yoga", "confidence": 0.96,
             "evidence": ["\"yoga pants for studio sessions\""]},
        ],
        "warnings": [],
    },
    ("PX34", "workout_intensity"): {
        "attribute_name": "workout_intensity", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "low", "confidence": 0.93,
             "evidence": ["\"low-intensity movement\""]},
        ],
        "warnings": [],
    },
    ("PX34", "environment"): {
        "attribute_name": "environment", "attribute_class": "contextual_semantic",
        "values": [
            {"value": "studio", "confidence": 0.94,
             "evidence": ["\"studio sessions\""], "reasoning_mode": "inferred"},
        ],
        "proposed_values": [], "warnings": [],
    },
    ("PX34", "layering_role"): {
        "attribute_name": "layering_role", "attribute_class": "compatibility",
        "values": [], "proposed_values": [], "warnings": ["no_supported_value_found"],
    },

    # ---------------- PX35 — Outdoor Training Hoodie ----------------
    ("PX35", "activity_type"): {
        "attribute_name": "activity_type", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "training", "confidence": 0.93,
             "evidence": ["\"Training hoodie for bootcamp workouts\""]},
        ],
        "warnings": [],
    },
    ("PX35", "workout_intensity"): {
        "attribute_name": "workout_intensity", "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "moderate", "confidence": 0.90,
             "evidence": ["\"moderate-to-high effort sessions\""]},
            {"value": "high", "confidence": 0.90,
             "evidence": ["\"moderate-to-high effort sessions\""]},
        ],
        "warnings": [],
    },
    ("PX35", "environment"): {
        "attribute_name": "environment", "attribute_class": "contextual_semantic",
        "values": [
            {"value": "outdoor", "confidence": 0.94,
             "evidence": ["\"outdoor conditioning\""], "reasoning_mode": "inferred"},
        ],
        "proposed_values": [], "warnings": [],
    },
    ("PX35", "layering_role"): {
        "attribute_name": "layering_role", "attribute_class": "compatibility",
        "values": [
            {"value": "mid", "confidence": 0.94,
             "evidence": ["\"Mid-layer warmth\""], "reasoning_mode": "suitability"},
        ],
        "proposed_values": [], "warnings": [],
    },

    # ---------------- PX36 — Packable Midlayer Zip ----------------
    ("PX36", "activity_type"): {
        "attribute_name": "activity_type", "attribute_class": "contextual_semantic",
        "values": [], "proposed_values": [], "warnings": ["no_supported_value_found"],
    },
    ("PX36", "workout_intensity"): {
        "attribute_name": "workout_intensity", "attribute_class": "contextual_semantic",
        "values": [], "proposed_values": [], "warnings": ["no_supported_value_found"],
    },
    ("PX36", "environment"): {
        "attribute_name": "environment", "attribute_class": "contextual_semantic",
        "values": [
            {"value": "outdoor", "confidence": 0.90,
             "evidence": ["\"outdoor walks\""], "reasoning_mode": "inferred"},
        ],
        "proposed_values": [], "warnings": [],
    },
    ("PX36", "layering_role"): {
        "attribute_name": "layering_role", "attribute_class": "compatibility",
        "values": [
            {"value": "mid", "confidence": 0.95,
             "evidence": ["\"Light mid layer\""], "reasoning_mode": "suitability"},
        ],
        "proposed_values": [], "warnings": [],
    },
}


CUSTOMERS = {
    "C_EVAL_1": [
        ("activity_type", "hiit", 0.9), ("activity_type", "running", 0.8),
        ("workout_intensity", "high", 0.85),
        ("environment", "indoor", 0.8),
        ("layering_role", "base", 0.75),
    ],
    "C_EVAL_2": [
        ("activity_type", "yoga", 0.9),
        ("workout_intensity", "low", 0.85),
        ("environment", "indoor", 0.8),
        ("layering_role", "base", 0.75),
    ],
    "C_EVAL_3": [
        ("activity_type", "hiking", 0.9),
        ("workout_intensity", "moderate", 0.85),
        ("environment", "outdoor", 0.8),
        ("layering_role", "mid", 0.75),
    ],
    "C_EVAL_4": [
        ("activity_type", "running", 0.9),
        ("workout_intensity", "moderate", 0.85),
        ("environment", "indoor", 0.8), ("environment", "outdoor", 0.8),
        ("layering_role", "base", 0.75), ("layering_role", "mid", 0.75),
    ],
}


def _materialize_model_outputs_as_attrs(product: Product, pid: str, db) -> list[tuple[str, str]]:
    """Convert MODEL_OUTPUTS for this product into ProductAttribute rows.

    Emits one ProductAttribute per (attribute, value) where the value passed
    the confidence threshold and had evidence.  Matches the scoring engine's
    expectations — it reads ProductAttribute, not enrichment objects.

    Returns the list of (attribute_id, attribute_value) pairs emitted, for
    logging purposes.
    """
    emitted = []
    for (px_id, attr_name), output in MODEL_OUTPUTS.items():
        if px_id != pid:
            continue
        # `values` entries (populated when allowed_values defines the taxonomy)
        for v in output.get("values", []):
            if v.get("confidence", 0) >= CONF_THRESHOLD and v.get("evidence"):
                db.add(ProductAttribute(
                    product_id=product.id, attribute_id=attr_name,
                    attribute_value=v["value"],
                ))
                emitted.append((attr_name, v["value"]))
        # `proposed_values` (populated when allowed_values == [] in the taxonomy)
        for v in output.get("proposed_values", []):
            if v.get("confidence", 0) >= CONF_THRESHOLD and v.get("evidence"):
                db.add(ProductAttribute(
                    product_id=product.id, attribute_id=attr_name,
                    attribute_value=v["value"],
                ))
                emitted.append((attr_name, v["value"]))
    return emitted


def _seed_products(db, ws_id: int, products: list[dict], model_outputs: bool) -> dict[str, Product]:
    """Seed a list of products with their catalog `attrs` / `attributes`, plus
    (for PX products) materialized MODEL_OUTPUTS.
    """
    created: dict[str, Product] = {}
    for p in products:
        prod = Product(
            workspace_id=ws_id, product_id=p["product_id"],
            sku=p.get("sku") or p["product_id"], name=p["name"], group_id=None,
        )
        db.add(prod)
        db.flush()
        created[p["product_id"]] = prod

        catalog_attrs = p.get("attrs") or p.get("attributes") or {}
        for attr_id, attr_val in catalog_attrs.items():
            db.add(ProductAttribute(
                product_id=prod.id, attribute_id=attr_id, attribute_value=attr_val,
            ))
        if model_outputs:
            _materialize_model_outputs_as_attrs(prod, p["product_id"], db)
    db.flush()
    return created


def _seed_affinities(db, ws_id: int, cust_id: str, affinities):
    db.query(CustomerAttributeAffinity).filter(
        CustomerAttributeAffinity.workspace_id == ws_id,
        CustomerAttributeAffinity.customer_id == cust_id,
    ).delete(synchronize_session=False)
    for attr_id, attr_val, score in affinities:
        db.add(CustomerAttributeAffinity(
            workspace_id=ws_id, customer_id=cust_id,
            attribute_id=attr_id, attribute_value=attr_val, score=score,
        ))
    db.flush()


def _run_scoring(db, ws_id: int, cust_id: str, top_n: int, targeting_modes, behaviors):
    results, _ = get_recommendations(
        db, workspace_id=ws_id, customer_id=cust_id, top_n=top_n,
        attribute_targeting_modes=targeting_modes, attribute_behaviors=behaviors,
    )
    return results


def _print_top(label, results, top_n):
    print(f"  {label}")
    print(f"    {'rank':<4} {'product_id':<20} {'final':>9} {'aff':>9} "
          f"{'compat+':>9} {'compat-':>9} {'ctx-':>9}")
    if not results:
        print("    (none)")
        return
    for i, rec in enumerate(results[:top_n], 1):
        print(f"    #{i:<3} {rec.product_id:<20} "
              f"{rec.recommendation_score:>+9.4f} "
              f"{rec.affinity_contribution:>+9.4f} "
              f"{rec.compatibility_positive_contribution:>+9.4f} "
              f"{rec.compatibility_negative_contribution:>+9.4f} "
              f"{rec.contextual_negative_contribution:>+9.4f}")


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WORKSPACE_SLUG).first()
        if ws is None:
            raise SystemExit(f"Workspace {WORKSPACE_SLUG!r} not found.")

        # Seed original 12 EVAL_ products with all attrs (no enrichment needed).
        _seed_products(db, ws.id, ORIGINAL_PRODUCTS, model_outputs=False)
        before_pids = {p["product_id"] for p in ORIGINAL_PRODUCTS}

        # Load attribute defs for targeting modes / behaviors.
        defs = json.loads(SEED_DIR.joinpath("attribute_definitions.json").read_text("utf-8"))
        attr_defs = [AttributeDefinition(**d) for d in defs]
        targeting_modes = {d.name: d.targeting_mode.value for d in attr_defs}
        behaviors = {d.name: d.behavior for d in attr_defs}

        # -------- BEFORE: score over just the 12 originals --------
        print("=" * 100)
        print("BEFORE: 12-product evaluation catalog")
        print("=" * 100)
        before_results: dict[str, list] = {}
        for cust_id, affinities in CUSTOMERS.items():
            _seed_affinities(db, ws.id, cust_id, affinities)
            # Ask for all candidates so we see the full scored pool.
            before_results[cust_id] = _run_scoring(
                db, ws.id, cust_id, top_n=100,
                targeting_modes=targeting_modes, behaviors=behaviors,
            )

        # -------- Seed the 16 new PX products (with materialized MODEL_OUTPUTS) --------
        new_prods_created = _seed_products(db, ws.id, NEW_PRODUCTS, model_outputs=True)

        # Print which attributes got materialized per new product.
        print()
        print("=" * 100)
        print("MODEL_OUTPUTS materialized for 16 new products")
        print("=" * 100)
        for p in NEW_PRODUCTS:
            pid = p["product_id"]
            attrs = []
            for (px_id, attr_name), output in MODEL_OUTPUTS.items():
                if px_id != pid:
                    continue
                kept = [
                    v["value"]
                    for v in (output.get("values", []) + output.get("proposed_values", []))
                    if v.get("confidence", 0) >= CONF_THRESHOLD and v.get("evidence")
                ]
                if kept:
                    attrs.append(f"{attr_name}={'|'.join(kept)}")
                elif output.get("warnings"):
                    attrs.append(f"{attr_name}=<none>")
            print(f"  {pid:5s} {p['name']:30s}  {', '.join(attrs)}")

        # -------- AFTER: score over the expanded 28-product catalog --------
        print()
        print("=" * 100)
        print("AFTER: 28-product evaluation catalog (12 original + 16 new)")
        print("=" * 100)
        after_results: dict[str, list] = {}
        for cust_id, affinities in CUSTOMERS.items():
            _seed_affinities(db, ws.id, cust_id, affinities)
            after_results[cust_id] = _run_scoring(
                db, ws.id, cust_id, top_n=100,
                targeting_modes=targeting_modes, behaviors=behaviors,
            )

        # -------- Top-N per customer (AFTER) --------
        print()
        for cust_id, affinities in CUSTOMERS.items():
            print("-" * 100)
            print(f"Customer {cust_id}")
            affs = ", ".join(f"{a[0]}={a[1]}({a[2]:.2f})" for a in affinities)
            print(f"  affinities: {affs}")
            print()
            _print_top(f"TOP {TOP_N} AFTER (expanded catalog)",
                       after_results[cust_id], TOP_N)
            print()

        # -------- Before vs After counts --------
        print("=" * 100)
        print("Recall comparison — scored candidates vs surfaced results")
        print("=" * 100)
        print(f"  {'customer':<10} "
              f"{'scored(before)':>16} {'scored(after)':>15} {'delta_scored':>14}  "
              f"{'top5(before)':>14} {'top5(after)':>13} {'delta_top5':>12}")
        for cust_id in CUSTOMERS:
            b = before_results[cust_id]
            a = after_results[cust_id]
            b_top5_pids = {r.product_id for r in b[:TOP_N]}
            a_top5_pids = {r.product_id for r in a[:TOP_N]}
            print(f"  {cust_id:<10} "
                  f"{len(b):>16d} {len(a):>15d} {len(a) - len(b):>+14d}  "
                  f"{len(b_top5_pids):>14d} {len(a_top5_pids):>13d} "
                  f"{len(a_top5_pids) - len(b_top5_pids):>+12d}")

        # Did any original top-5 get displaced? (precision-proxy check)
        print()
        print("Top-5 churn (precision proxy): who entered / left each customer's top 5")
        for cust_id in CUSTOMERS:
            b_pids = [r.product_id for r in before_results[cust_id][:TOP_N]]
            a_pids = [r.product_id for r in after_results[cust_id][:TOP_N]]
            added = [p for p in a_pids if p not in b_pids]
            removed = [p for p in b_pids if p not in a_pids]
            print(f"  {cust_id}:")
            print(f"    before : {b_pids}")
            print(f"    after  : {a_pids}")
            print(f"    added  : {added}")
            print(f"    removed: {removed}")

    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
