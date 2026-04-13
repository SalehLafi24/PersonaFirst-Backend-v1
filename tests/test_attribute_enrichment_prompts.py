"""Tests for the class-based prompt builders in attribute_enrichment_service.

These tests are pure (no DB) — they exercise prompt construction only.
The autouse setup_db fixture from conftest still runs but is irrelevant here.
"""
import json

import pytest

from app.schemas.attribute_enrichment import AttributeBehavior, AttributeDefinition
from app.services.attribute_enrichment_service import (
    _build_compatibility_prompt,
    _build_contextual_semantic_prompt,
    _build_descriptive_literal_prompt,
    _build_taxonomy_discovery_prompt,
    _normalize_obj,
    get_prompt_for_attribute,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_attr(
    *,
    class_name: str,
    name: str = "support_level",
    description: str = "The support intensity this product is functionally suited to provide.",
    allowed_values: list[str] | None = None,
    value_mode: str = "single",
    behavior: AttributeBehavior | None = None,
) -> AttributeDefinition:
    return AttributeDefinition(
        name=name,
        object_type="product",
        class_name=class_name,
        value_mode=value_mode,
        allowed_values=allowed_values,
        description=description,
        evidence_sources=["name", "description"],
        behavior=behavior or AttributeBehavior(),
    )


LIGHT_COMPRESSION_BRA = {
    "name": "Light Compression Bra",
    "description": "Soft seamless bra with light compression for everyday wear and low-impact activity.",
}

# Same product family, but the description now contains an explicit suitability
# statement ("moderate support") competing with an indirect compression clue
# ("light compression"). Used to verify the prompt sets up the explicit-priority
# rule correctly so the model resolves to "medium", not "low".
LIGHT_COMPRESSION_BRA_WITH_EXPLICIT = {
    "name": "Light Compression Bra",
    "description": "Lightweight bra with light compression and moderate support for low-impact sessions.",
}


# ---------------------------------------------------------------------------
# Compatibility builder — primary focus
# ---------------------------------------------------------------------------

class TestCompatibilityPrompt:
    def test_substitutes_attribute_name_and_description(self):
        attr = _make_attr(
            class_name="compatibility",
            allowed_values=["low", "medium", "high"],
        )
        prompt = _build_compatibility_prompt(attr, LIGHT_COMPRESSION_BRA)

        assert "Attribute name: support_level" in prompt
        assert (
            "Description: The support intensity this product is functionally suited to provide."
            in prompt
        )

    def test_renders_allowed_values_as_bullet_list(self):
        attr = _make_attr(
            class_name="compatibility",
            allowed_values=["low", "medium", "high"],
        )
        prompt = _build_compatibility_prompt(attr, LIGHT_COMPRESSION_BRA)

        assert "Allowed values:\n- low\n- medium\n- high" in prompt

    def test_handles_missing_allowed_values(self):
        attr = _make_attr(class_name="compatibility", allowed_values=None)
        prompt = _build_compatibility_prompt(attr, LIGHT_COMPRESSION_BRA)

        assert "Allowed values:\n(none provided)" in prompt

    def test_embeds_object_data_as_json(self):
        attr = _make_attr(
            class_name="compatibility",
            allowed_values=["low", "medium", "high"],
        )
        prompt = _build_compatibility_prompt(attr, LIGHT_COMPRESSION_BRA)

        # The object should appear as indented JSON inside OBJECT DATA
        expected_json = json.dumps(LIGHT_COMPRESSION_BRA, indent=2, ensure_ascii=False)
        assert expected_json in prompt

    def test_object_data_section_has_divider_header(self):
        attr = _make_attr(class_name="compatibility")
        prompt = _build_compatibility_prompt(attr, LIGHT_COMPRESSION_BRA)

        assert "--------------------------------\nOBJECT DATA\n--------------------------------" in prompt

    def test_no_unresolved_placeholder_tokens(self):
        """Regression: the old failure mode looked like placeholders weren't substituted."""
        attr = _make_attr(
            class_name="compatibility",
            allowed_values=["low", "medium", "high"],
        )
        prompt = _build_compatibility_prompt(attr, LIGHT_COMPRESSION_BRA)

        for token in ("{ATTRIBUTE_NAME}", "{ATTRIBUTE_DESCRIPTION}",
                      "{ALLOWED_VALUES}", "{OBJECT_DATA}"):
            assert token not in prompt

    def test_attribute_name_appears_only_in_attribute_context(self):
        """Single render — attribute name should appear once in the ATTRIBUTE CONTEXT block,
        not duplicated across multiple sections."""
        attr = _make_attr(
            class_name="compatibility",
            name="support_level",
            allowed_values=["low", "medium", "high"],
        )
        prompt = _build_compatibility_prompt(attr, LIGHT_COMPRESSION_BRA)

        # The literal "support_level" should appear exactly once — in the
        # "Attribute name:" line. If the prompt were rendered twice we'd see it twice.
        assert prompt.count("support_level") == 1

    def test_compatibility_logic_preserved(self):
        """The suitability framing and confidence tiers from the old builder
        must survive the format conversion."""
        attr = _make_attr(
            class_name="compatibility",
            allowed_values=["low", "medium", "high"],
        )
        prompt = _build_compatibility_prompt(attr, LIGHT_COMPRESSION_BRA)

        # Suitability framing
        assert "suitability assessment task" in prompt
        # Explicit-statement-priority rules
        assert "Explicit suitability statements have the highest priority." in prompt
        assert (
            "Indirect clues such as compression level, comfort language, activity type, "
            "or use context must not override an explicit suitability statement on their own."
            in prompt
        )
        assert (
            "If an explicit suitability statement is present and there is no direct contradiction, use it."
            in prompt
        )
        # Updated ambiguity gate
        assert (
            "If signals are ambiguous or conflicting AND there is no usable explicit suitability statement"
            in prompt
        )
        # Confidence tiers (preserved from prior SCORING GUIDANCE block)
        assert "0.80\u20131.00 = strong evidence supports the value" in prompt
        assert "0.50\u20130.79 = moderate evidence" in prompt
        assert "below 0.50 = weak or ambiguous" in prompt
        # Reasoning mode discriminator
        assert '"reasoning_mode": "suitability"' in prompt

    def test_uses_divider_based_template_format(self):
        """Compatibility builder must use the same standardised template
        as descriptive_literal and contextual_semantic."""
        attr = _make_attr(
            class_name="compatibility",
            allowed_values=["low", "medium", "high"],
        )
        prompt = _build_compatibility_prompt(attr, LIGHT_COMPRESSION_BRA)

        for header in (
            "ATTRIBUTE CONTEXT",
            "CLASS BEHAVIOR",
            "OBJECT DATA",
            "OUTPUT FORMAT (STRICT)",
            "OUTPUT RULES",
            "FINAL RULE",
        ):
            assert f"--------------------------------\n{header}\n--------------------------------" in prompt

    def test_normalizes_newlines_in_object_data(self):
        """Multiline string fields should be flattened to prevent the JSON block
        from breaking the prompt's section structure."""
        attr = _make_attr(
            class_name="compatibility",
            allowed_values=["low", "medium", "high"],
        )
        obj = {
            "name": "Light Compression Bra",
            "description": "Soft seamless bra\nwith light compression\nfor everyday wear.",
        }
        prompt = _build_compatibility_prompt(attr, obj)

        # Original embedded newlines must be replaced with spaces
        assert "Soft seamless bra with light compression for everyday wear." in prompt
        assert "Soft seamless bra\\nwith" not in prompt  # not escaped either


# ---------------------------------------------------------------------------
# Explicit-vs-indirect logic — proves the prompt is constructed to make the
# model prioritise an explicit suitability statement over indirect clues.
#
# IMPORTANT: these are prompt-construction assertions, not live LLM assertions.
# They prove the prompt sent to Claude contains the right object data and the
# explicit-priority rules. They do NOT call the API and do NOT prove the model
# actually returns "medium" — that needs a live-API test, which doesn't exist
# in this repo today.
# ---------------------------------------------------------------------------

class TestCompatibilityExplicitVsIndirect:
    def _attr(self) -> AttributeDefinition:
        return _make_attr(
            class_name="compatibility",
            name="support_level",
            description="The support intensity this product is functionally suited to provide.",
            allowed_values=["low", "medium", "high"],
        )

    def test_prompt_for_explicit_moderate_support_fixture(self):
        """When the description contains an explicit suitability statement
        ('moderate support') AND a competing indirect clue ('light compression'),
        the prompt must:
          1. embed the full description verbatim so the model sees both signals,
          2. contain the explicit-priority rules that tell the model to prefer
             the explicit statement over the indirect clue.

        Together these conditions are what should make the model return "medium".
        """
        prompt = _build_compatibility_prompt(self._attr(), LIGHT_COMPRESSION_BRA_WITH_EXPLICIT)

        # 1. Object data — both the explicit phrase and the competing indirect
        #    clue must be present in OBJECT DATA exactly as given.
        assert (
            '"description": "Lightweight bra with light compression and moderate support '
            'for low-impact sessions."'
            in prompt
        )
        assert "moderate support" in prompt
        assert "light compression" in prompt

        # 2. Allowed values exposed so "medium" is a legal answer
        assert "Allowed values:\n- low\n- medium\n- high" in prompt

        # 3. Explicit-priority rules — these are what tell the model to pick
        #    "medium" over "low" when both signals exist.
        assert "Explicit suitability statements have the highest priority." in prompt
        assert (
            "Indirect clues such as compression level, comfort language, activity type, "
            "or use context must not override an explicit suitability statement on their own."
            in prompt
        )
        assert (
            "Treat indirect clues as supporting context, not as a stronger source of truth "
            "than an explicit suitability statement."
            in prompt
        )
        assert (
            "If an explicit suitability statement is present and there is no direct contradiction, use it."
            in prompt
        )

        # 4. The ambiguity gate must NOT fire on this fixture's logic — it
        #    only fires when there's no usable explicit statement. The rule
        #    text in the prompt must reflect that condition.
        assert (
            "If signals are ambiguous or conflicting AND there is no usable explicit suitability statement"
            in prompt
        )

    def test_prompt_for_no_explicit_statement_fixture(self):
        """The original Light Compression Bra fixture contains only indirect
        clues — no explicit suitability statement. The prompt must:
          1. embed the indirect-only description verbatim,
          2. contain the no_supported_value_found fallback rule, which is
             where the model should land when no explicit statement exists
             and indirect clues alone are insufficient.
        """
        prompt = _build_compatibility_prompt(self._attr(), LIGHT_COMPRESSION_BRA)

        # 1. Object data — the indirect-only description is embedded verbatim
        assert (
            '"description": "Soft seamless bra with light compression for everyday wear '
            'and low-impact activity."'
            in prompt
        )
        # The phrase "moderate support" must NOT appear anywhere — that's the
        # whole point of this fixture being the no-explicit-statement case.
        assert "moderate support" not in prompt

        # 2. Fallback rule — model should return values=[] with this warning
        #    when no explicit statement is present and no values can be supported.
        assert (
            'Else if no values are clearly supported, return:\n'
            '    values = []\n'
            '    warnings = ["no_supported_value_found"]'
            in prompt
        )


# ---------------------------------------------------------------------------
# Light Compression Bra — full end-to-end render snapshot of the key sections
# ---------------------------------------------------------------------------

def test_light_compression_bra_full_render_via_dispatch():
    """End-to-end check: get_prompt_for_attribute routes compatibility class
    to the compatibility builder and produces the expected substituted content."""
    attr = _make_attr(
        class_name="compatibility",
        allowed_values=["low", "medium", "high"],
    )
    prompt = get_prompt_for_attribute(attr, LIGHT_COMPRESSION_BRA)

    # All four user-required positions are substituted
    assert "Attribute name: support_level" in prompt
    assert (
        "Description: The support intensity this product is functionally suited to provide."
        in prompt
    )
    assert "Allowed values:\n- low\n- medium\n- high" in prompt
    assert '"name": "Light Compression Bra"' in prompt
    assert (
        '"description": "Soft seamless bra with light compression for everyday wear and low-impact activity."'
        in prompt
    )

    # Class label in the template reflects the dispatched class
    assert "Class: compatibility" in prompt


# ---------------------------------------------------------------------------
# Other builders — sanity checks (NOT modified, just verifying dispatch + format)
# ---------------------------------------------------------------------------

class TestDispatchAndOtherBuilders:
    @pytest.mark.parametrize(
        "class_name,builder",
        [
            ("descriptive_literal", _build_descriptive_literal_prompt),
            ("contextual_semantic", _build_contextual_semantic_prompt),
            ("compatibility", _build_compatibility_prompt),
            ("taxonomy_discovery", _build_taxonomy_discovery_prompt),
        ],
    )
    def test_get_prompt_dispatches_to_correct_builder(self, class_name, builder):
        attr = _make_attr(class_name=class_name, allowed_values=["low", "medium", "high"])
        dispatched = get_prompt_for_attribute(attr, LIGHT_COMPRESSION_BRA)
        direct = builder(attr, LIGHT_COMPRESSION_BRA)
        assert dispatched == direct

    def test_descriptive_literal_uses_divider_format(self):
        attr = _make_attr(
            class_name="descriptive_literal",
            allowed_values=["low", "medium", "high"],
        )
        prompt = _build_descriptive_literal_prompt(attr, LIGHT_COMPRESSION_BRA)
        assert "Class: descriptive_literal" in prompt
        assert "Attribute name: support_level" in prompt

    def test_contextual_semantic_uses_divider_format(self):
        attr = _make_attr(
            class_name="contextual_semantic",
            allowed_values=["low", "medium", "high"],
        )
        prompt = _build_contextual_semantic_prompt(attr, LIGHT_COMPRESSION_BRA)
        assert "Class: contextual_semantic" in prompt
        assert "Attribute name: support_level" in prompt


# ---------------------------------------------------------------------------
# _normalize_obj — small focused tests for the helper used by the builders
# ---------------------------------------------------------------------------

class TestNormalizeObj:
    def test_replaces_newlines_in_top_level_strings(self):
        result = _normalize_obj({"a": "one\ntwo\nthree"})
        assert result == {"a": "one two three"}

    def test_replaces_crlf_and_cr(self):
        result = _normalize_obj({"a": "one\r\ntwo\rthree"})
        assert result == {"a": "one two three"}

    def test_recurses_into_nested_dicts(self):
        result = _normalize_obj({"outer": {"inner": "a\nb"}})
        assert result == {"outer": {"inner": "a b"}}

    def test_normalizes_strings_inside_lists(self):
        result = _normalize_obj({"tags": ["a\nb", "c\nd"]})
        assert result == {"tags": ["a b", "c d"]}

    def test_leaves_non_string_values_untouched(self):
        result = _normalize_obj({"n": 42, "b": True, "x": None})
        assert result == {"n": 42, "b": True, "x": None}
