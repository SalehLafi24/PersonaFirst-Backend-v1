"""
Nested Slot schema tests.

Validates that the new nested Slot structure (audience/strategy/constraints/
controls/exclusion) parses correctly, maps to flat fields for execution,
and preserves backward compatibility with the existing flat shape.
"""
from datetime import date

from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product, ProductAttribute
from app.schemas.recommendation import SlotConfig


def make_workspace(client, name, slug):
    return client.post("/workspaces", json={"name": name, "slug": slug}).json()


def seed_product(db, workspace_id, product_id, sku, name, group_id=None,
                 attributes=None):
    p = Product(workspace_id=workspace_id, product_id=product_id, sku=sku,
                name=name, group_id=group_id)
    db.add(p)
    db.flush()
    for attr_id, attr_val in (attributes or []):
        db.add(ProductAttribute(product_id=p.id, attribute_id=attr_id,
                                attribute_value=attr_val))
    db.commit()
    return p


def seed_affinity(db, workspace_id, customer_id, attribute_id, attribute_value,
                  score):
    db.add(CustomerAttributeAffinity(
        workspace_id=workspace_id, customer_id=customer_id,
        attribute_id=attribute_id, attribute_value=attribute_value, score=score,
    ))
    db.commit()


def slot_post(client, wid, customer_id, slot):
    return client.post(
        f"/workspaces/{wid}/recommendations/slot",
        json={"customer_id": customer_id, "slot": slot},
    )


def multi_post(client, wid, customer_id, slots):
    return client.post(
        f"/workspaces/{wid}/recommendations/slots",
        json={"customer_id": customer_id, "slots": slots},
    )


# ---------------------------------------------------------------------------
# Pure schema tests (no DB needed for parsing)
# ---------------------------------------------------------------------------

def test_nested_strategy_flattens_to_algorithm_and_fallback_behavior():
    cfg = SlotConfig.model_validate({
        "slot_id": "s1",
        "top_n": 5,
        "strategy": {
            "algorithm": "affinity_first",
            "fallback_behavior": "direct",
        },
    })
    assert cfg.algorithm == "affinity_first"
    assert cfg.fallback_behavior == "direct"
    # nested structure is preserved for round-tripping
    assert cfg.strategy is not None
    assert cfg.strategy.algorithm == "affinity_first"


def test_nested_controls_flattens_to_top_n_and_diversity_mode():
    cfg = SlotConfig.model_validate({
        "slot_id": "s1",
        "algorithm": "balanced",
        "controls": {"top_n": 7, "diversity_mode": "strict"},
    })
    assert cfg.top_n == 7
    assert cfg.diversity_mode == "strict"


def test_nested_constraints_filters_flattens_to_filters():
    cfg = SlotConfig.model_validate({
        "slot_id": "s1",
        "algorithm": "balanced",
        "top_n": 5,
        "constraints": {
            "filters": [{"attribute_id": "color", "operator": "eq", "value": "red"}],
        },
    })
    assert len(cfg.filters) == 1
    assert cfg.filters[0].attribute_id == "color"
    assert cfg.filters[0].value == "red"


def test_nested_exclusion_flattens_to_flat_fields():
    cfg = SlotConfig.model_validate({
        "slot_id": "s1",
        "algorithm": "balanced",
        "top_n": 5,
        "exclusion": {
            "exclude_previous_slots": True,
            "exclusion_level": "group",
        },
    })
    assert cfg.exclude_previous_slots is True
    assert cfg.exclusion_level == "group"


def test_audience_filters_parse_but_do_not_map_to_flat_filters():
    """audience.filters is schema-only — it does NOT become flat `filters`."""
    cfg = SlotConfig.model_validate({
        "slot_id": "s1",
        "algorithm": "balanced",
        "top_n": 5,
        "audience": {
            "filters": [{"attribute_id": "region", "operator": "eq", "value": "EU"}],
        },
    })
    # audience filters are parsed and preserved on the model
    assert cfg.audience is not None
    assert len(cfg.audience.filters) == 1
    assert cfg.audience.filters[0].attribute_id == "region"
    # ...but NOT copied to the flat `filters` used for product constraints
    assert cfg.filters == []


def test_nested_overrides_flat_when_both_present():
    cfg = SlotConfig.model_validate({
        "slot_id": "s1",
        "algorithm": "balanced",
        "top_n": 5,
        "diversity_mode": "off",
        "fallback_behavior": "none",
        "strategy": {"algorithm": "behavior_first", "fallback_behavior": "direct"},
        "controls": {"top_n": 10, "diversity_mode": "strict"},
    })
    # Nested wins
    assert cfg.algorithm == "behavior_first"
    assert cfg.fallback_behavior == "direct"
    assert cfg.top_n == 10
    assert cfg.diversity_mode == "strict"


def test_flat_only_shape_unchanged():
    """Flat-only clients behave exactly as before — nested fields are None."""
    cfg = SlotConfig.model_validate({
        "slot_id": "s1",
        "algorithm": "balanced",
        "top_n": 5,
        "filters": [{"attribute_id": "color", "operator": "eq", "value": "blue"}],
        "diversity_mode": "strict",
        "fallback_behavior": "direct",
    })
    assert cfg.algorithm == "balanced"
    assert cfg.top_n == 5
    assert cfg.diversity_mode == "strict"
    assert cfg.fallback_behavior == "direct"
    assert len(cfg.filters) == 1
    # No nested blocks when not provided
    assert cfg.audience is None
    assert cfg.strategy is None
    assert cfg.constraints is None
    assert cfg.controls is None
    assert cfg.exclusion is None


def test_nested_empty_constraints_filters_clears_flat_filters():
    """constraints.filters=[] is an explicit 'no constraints' and overrides flat."""
    cfg = SlotConfig.model_validate({
        "slot_id": "s1",
        "algorithm": "balanced",
        "top_n": 5,
        "filters": [{"attribute_id": "color", "operator": "eq", "value": "red"}],
        "constraints": {"filters": []},
    })
    assert cfg.filters == []


def test_nested_partial_strategy_leaves_other_fields_at_flat():
    """Only the nested fields that are set override flat; others stay."""
    cfg = SlotConfig.model_validate({
        "slot_id": "s1",
        "algorithm": "affinity_first",
        "top_n": 5,
        "fallback_behavior": "balanced",
        "strategy": {"algorithm": "behavior_first"},  # only algorithm
    })
    assert cfg.algorithm == "behavior_first"        # overridden
    assert cfg.fallback_behavior == "balanced"      # flat preserved


def test_nested_invalid_fallback_behavior_fails_validation():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SlotConfig.model_validate({
            "slot_id": "s1",
            "algorithm": "balanced",
            "top_n": 5,
            "strategy": {"fallback_behavior": "nope"},
        })


# ---------------------------------------------------------------------------
# End-to-end tests: nested shape produces same results as flat shape
# ---------------------------------------------------------------------------

def test_nested_slot_request_runs_end_to_end(client, db):
    """Send a fully-nested slot request and verify it works through the API."""
    ws = make_workspace(client, "NESTED", "nested")
    wid = ws["id"]
    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", {
        "slot_id": "s1",
        "strategy": {"algorithm": "balanced", "fallback_behavior": "none"},
        "controls": {"top_n": 5, "diversity_mode": "off"},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["slot_id"] == "s1"
    assert data["algorithm"] == "balanced"
    assert len(data["results"]) == 1
    assert data["results"][0]["product_id"] == "prod_1"


def test_nested_constraints_filters_apply_at_runtime(client, db):
    """constraints.filters reach the engine and filter the candidate pool."""
    ws = make_workspace(client, "NESTED-CON", "nested-con")
    wid = ws["id"]
    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_red", "SKU-R", "Red Mat",
                 attributes=[("category", "yoga"), ("color", "red")])
    seed_product(db, wid, "prod_blue", "SKU-B", "Blue Mat",
                 attributes=[("category", "yoga"), ("color", "blue")])

    resp = slot_post(client, wid, "cust_1", {
        "slot_id": "s1",
        "strategy": {"algorithm": "balanced"},
        "controls": {"top_n": 5},
        "constraints": {
            "filters": [{"attribute_id": "color", "operator": "eq", "value": "red"}],
        },
    })
    data = resp.json()
    pids = [r["product_id"] for r in data["results"]]
    assert "prod_red" in pids
    assert "prod_blue" not in pids


def test_nested_exclusion_works_in_multi_slot(client, db):
    """exclusion.exclude_previous_slots via nested structure works end-to-end."""
    ws = make_workspace(client, "NESTED-EXC", "nested-exc")
    wid = ws["id"]
    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_a", "SKU-A", "A",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "B",
                 attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {
            "slot_id": "s1",
            "strategy": {"algorithm": "balanced"},
            "controls": {"top_n": 1, "diversity_mode": "off"},
        },
        {
            "slot_id": "s2",
            "strategy": {"algorithm": "balanced"},
            "controls": {"top_n": 5, "diversity_mode": "off"},
            "exclusion": {"exclude_previous_slots": True},
        },
    ])
    slots = resp.json()["slots"]
    s1_ids = {r["product_id"] for r in slots[0]["results"]}
    s2_ids = {r["product_id"] for r in slots[1]["results"]}
    assert s1_ids & s2_ids == set()
