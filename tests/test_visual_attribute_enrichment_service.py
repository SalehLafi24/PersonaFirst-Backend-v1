"""Tests for the visual attribute enrichment service.

These tests are pure (no DB) — they exercise prompt construction and output
shaping only. The autouse setup_db fixture from conftest still runs but is
irrelevant here.
"""

import pytest

from app.schemas.attribute_enrichment import (
    AttributeBehavior,
    AttributeDefinition,
    EnrichmentSource,
)
from app.services.visual_attribute_enrichment_service import (
    VisualInput,
    _build_compatibility_visual_prompt,
    _build_contextual_semantic_visual_prompt,
    _build_descriptive_literal_visual_prompt,
    build_visual_enrichment_output,
    generate_visual_enrichment,
    get_visual_prompt_for_attribute,
)


def _make_attr(
    *,
    class_name: str,
    name: str = "color",
    value_mode: str = "single",
    allowed_values: list[str] | None = None,
) -> AttributeDefinition:
    return AttributeDefinition(
        name=name,
        object_type="product",
        class_name=class_name,
        value_mode=value_mode,
        allowed_values=allowed_values,
        description=f"{name} for visual tests.",
        evidence_sources=["image"],
        behavior=AttributeBehavior(),
    )


DUMMY_VISUAL = VisualInput(image_ref="https://example.com/test.jpg")


class TestDescriptiveLiteralVisualPrompt:
    def test_prompt_is_a_strict_visual_extraction_task(self):
        attr = _make_attr(
            class_name="descriptive_literal",
            allowed_values=["red", "blue", "green"],
        )
        prompt = _build_descriptive_literal_visual_prompt(attr, DUMMY_VISUAL)
        assert "strict visual extraction task" in prompt.lower()
        assert "Class: descriptive_literal" in prompt
        assert "visual_explicit" in prompt
        assert "Attribute name: color" in prompt

    def test_allowed_values_are_listed(self):
        attr = _make_attr(
            class_name="descriptive_literal",
            allowed_values=["red", "blue"],
        )
        prompt = _build_descriptive_literal_visual_prompt(attr, DUMMY_VISUAL)
        assert "- red" in prompt
        assert "- blue" in prompt

    def test_image_ref_is_embedded(self):
        attr = _make_attr(class_name="descriptive_literal")
        prompt = _build_descriptive_literal_visual_prompt(attr, DUMMY_VISUAL)
        assert DUMMY_VISUAL.image_ref in prompt
        assert "VISUAL INPUT" in prompt


class TestContextualSemanticVisualPrompt:
    def test_prompt_allows_scene_inference(self):
        attr = _make_attr(
            class_name="contextual_semantic",
            name="occasion",
            allowed_values=["casual", "formal", "athletic"],
        )
        prompt = _build_contextual_semantic_visual_prompt(attr, DUMMY_VISUAL)
        assert "visual_inferred" in prompt
        assert "scene" in prompt.lower()
        assert "Class: contextual_semantic" in prompt

    def test_prompt_lists_allowed_values(self):
        attr = _make_attr(
            class_name="contextual_semantic",
            name="occasion",
            allowed_values=["casual", "formal"],
        )
        prompt = _build_contextual_semantic_visual_prompt(attr, DUMMY_VISUAL)
        assert "- casual" in prompt
        assert "- formal" in prompt


class TestCompatibilityVisualPrompt:
    def test_prompt_asks_for_visual_suitability(self):
        attr = _make_attr(
            class_name="compatibility",
            name="support_level",
            allowed_values=["low", "medium", "high"],
        )
        prompt = _build_compatibility_visual_prompt(attr, DUMMY_VISUAL)
        assert "visual suitability" in prompt.lower()
        assert "visual_suitability" in prompt
        assert "Class: compatibility" in prompt


class TestDispatch:
    def test_taxonomy_discovery_is_not_supported(self):
        attr = _make_attr(class_name="taxonomy_discovery", name="style_family")
        with pytest.raises(NotImplementedError):
            get_visual_prompt_for_attribute(attr, DUMMY_VISUAL)

    def test_dispatch_by_class_descriptive_literal(self):
        attr = _make_attr(class_name="descriptive_literal")
        prompt = get_visual_prompt_for_attribute(attr, DUMMY_VISUAL)
        assert "Class: descriptive_literal" in prompt

    def test_dispatch_by_class_contextual_semantic(self):
        attr = _make_attr(class_name="contextual_semantic")
        prompt = get_visual_prompt_for_attribute(attr, DUMMY_VISUAL)
        assert "Class: contextual_semantic" in prompt

    def test_dispatch_by_class_compatibility(self):
        attr = _make_attr(class_name="compatibility")
        prompt = get_visual_prompt_for_attribute(attr, DUMMY_VISUAL)
        assert "Class: compatibility" in prompt


class TestVisualInputFormatting:
    def test_metadata_and_alt_text_are_embedded(self):
        attr = _make_attr(class_name="descriptive_literal")
        visual = VisualInput(
            image_ref="https://example.com/x.jpg",
            alt_text="A red t-shirt on a white background",
            metadata={"resolution": "1024x1024"},
        )
        prompt = _build_descriptive_literal_visual_prompt(attr, visual)
        assert "A red t-shirt on a white background" in prompt
        assert "1024x1024" in prompt


class TestBuildVisualEnrichmentOutput:
    def test_stamps_source_visual_on_every_value(self):
        attr = _make_attr(class_name="descriptive_literal")
        raw = {
            "attribute_name": "color",
            "attribute_class": "descriptive_literal",
            "values": [
                {
                    "value": "red",
                    "confidence": 0.97,
                    "evidence": ["dominant red fabric across garment body"],
                    "reasoning_mode": "visual_explicit",
                }
            ],
            "proposed_values": [],
            "warnings": [],
        }
        output = build_visual_enrichment_output(attr, raw)
        assert output.source == EnrichmentSource.VISUAL
        assert len(output.values) == 1
        assert output.values[0].source == EnrichmentSource.VISUAL
        assert output.values[0].value == "red"
        assert output.values[0].confidence == pytest.approx(0.97)
        assert output.values[0].reasoning_mode == "visual_explicit"

    def test_handles_missing_optional_fields(self):
        attr = _make_attr(class_name="descriptive_literal")
        raw = {"values": [], "warnings": ["no_supported_value_found"]}
        output = build_visual_enrichment_output(attr, raw)
        assert output.attribute_name == "color"
        assert output.attribute_class == "descriptive_literal"
        assert output.warnings == ["no_supported_value_found"]
        assert output.values == []
        assert output.source == EnrichmentSource.VISUAL


class TestGenerateVisualEnrichment:
    def test_wires_prompt_to_analyzer_and_returns_tagged_output(self):
        attr = _make_attr(
            class_name="descriptive_literal",
            allowed_values=["red", "blue"],
        )
        captured: dict = {}

        def fake_analyzer(prompt: str, visual: VisualInput) -> dict:
            captured["prompt"] = prompt
            captured["visual"] = visual
            return {
                "values": [
                    {
                        "value": "red",
                        "confidence": 0.95,
                        "evidence": ["visible red fabric covering most of the garment"],
                        "reasoning_mode": "visual_explicit",
                    }
                ],
            }

        output = generate_visual_enrichment(
            attr, DUMMY_VISUAL, analyzer=fake_analyzer
        )
        assert "visual_explicit" in captured["prompt"]
        assert captured["visual"] is DUMMY_VISUAL
        assert output.source == EnrichmentSource.VISUAL
        assert output.values[0].value == "red"
        assert output.values[0].source == EnrichmentSource.VISUAL
