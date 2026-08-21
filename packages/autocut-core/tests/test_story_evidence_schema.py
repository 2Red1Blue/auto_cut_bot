"""Contract tests for the isolated Story Evidence schema leaf."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from autocut_core.schema.compat import (
    _STORY_EVIDENCE_SCHEMAS,
    SCHEMAS,
    STORY_SCRIPT_BEAT_SCHEMA,
    validate_schema,
)
from autocut_core.schema.story_evidence import (
    Schema,
    StoryEvidenceSchemaComponents,
    build_story_evidence_schemas,
)


def _closed_empty_object() -> Schema:
    return {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def _components() -> StoryEvidenceSchemaComponents:
    scalar = _closed_empty_object()
    causal_dependency = STORY_SCRIPT_BEAT_SCHEMA["properties"]["causal_dependency"]
    assert isinstance(causal_dependency, dict)
    return StoryEvidenceSchemaComponents(
        teaser_contract=scalar,
        window_analysis=scalar,
        event_card=scalar,
        highlight_hook_candidate=scalar,
        character=scalar,
        relationship=scalar,
        fact=scalar,
        story_thread=scalar,
        thread_beat=scalar,
        open_question=scalar,
        causal_dependency=causal_dependency,
    )


def _cross_unit_context() -> dict[str, object]:
    return {
        "beat_id": "beat-1-hook",
        "opening_candidate_id": "candidate-1",
        "required_context_ids": [],
        "required_event_ids": [],
        "ancestor_episode_range": None,
        "source_episode_ids": [],
        "source_unit_ids": [],
        "covered_event_ids": [],
        "missing_event_ids": [],
        "retrieval_status": "not_required",
        "cross_unit_required": False,
        "reason": "the opening does not depend on an earlier unit",
    }


def test_leaf_has_no_project_import_boundary() -> None:
    source = Path(__file__).parents[1] / "autocut_core/schema/story_evidence.py"
    text = source.read_text(encoding="utf-8")
    assert "from autocut_core" not in text
    assert "import autocut_core" not in text
    assert "importlib" not in text


def test_factory_exposes_only_typed_component_inputs() -> None:
    names = {field.name for field in fields(StoryEvidenceSchemaComponents)}
    assert names == {
        "teaser_contract",
        "window_analysis",
        "event_card",
        "highlight_hook_candidate",
        "character",
        "relationship",
        "fact",
        "story_thread",
        "thread_beat",
        "open_question",
        "causal_dependency",
    }


def test_factory_is_closed_and_accepts_valid_cross_unit_context() -> None:
    schemas = build_story_evidence_schemas(_components())
    packet_properties = schemas.packet["properties"]
    assert isinstance(packet_properties, dict)
    assert schemas.packet["additionalProperties"] is False
    beat_schema = packet_properties["beat_evidence"]
    assert isinstance(beat_schema, dict)
    beat_items = beat_schema["items"]
    assert isinstance(beat_items, dict)
    cross_unit_schema = beat_items["properties"]
    assert isinstance(cross_unit_schema, dict)
    context_schema = cross_unit_schema["cross_unit_context"]
    assert isinstance(context_schema, dict)
    assert validate_schema(_cross_unit_context(), context_schema) == []


def test_factory_rejects_unknown_cross_unit_and_malformed_causal_fields() -> None:
    schemas = build_story_evidence_schemas(_components())
    packet_properties = schemas.packet["properties"]
    assert isinstance(packet_properties, dict)
    beat_schema = packet_properties["beat_evidence"]
    assert isinstance(beat_schema, dict)
    beat_items = beat_schema["items"]
    assert isinstance(beat_items, dict)
    beat_properties = beat_items["properties"]
    assert isinstance(beat_properties, dict)

    malformed_context = _cross_unit_context() | {"untracked_default": True}
    context_schema = beat_properties["cross_unit_context"]
    assert isinstance(context_schema, dict)
    context_errors = validate_schema(malformed_context, context_schema)
    assert any("unknown properties" in error for error in context_errors)

    causal_schema = beat_properties["causal_dependency"]
    assert isinstance(causal_schema, dict)
    malformed_causal = {
        "explains_opening_highlight": "not-a-boolean",
        "required_before_fact_ids": [],
        "required_relationship_ids": [],
        "required_event_ids": [],
        "required_thread_beat_ids": [],
        "causal_ancestor_episode_range": {
            "min_episode": 1,
            "max_episode": 1,
            "reason": "no dependency",
        },
        "cross_unit_retrieval": {
            "required": False,
            "source_unit_ids": [],
            "retrieval_status": "covered",
        },
    }
    causal_errors = validate_schema(malformed_causal, causal_schema)
    assert any("expected boolean" in error for error in causal_errors)


def test_compat_registry_uses_the_leaf_packet_and_index() -> None:
    schemas = build_story_evidence_schemas(_components())
    assert SCHEMAS["story_evidence_packet"] is _STORY_EVIDENCE_SCHEMAS.packet
    assert SCHEMAS["story_evidence_index"] is _STORY_EVIDENCE_SCHEMAS.index
    assert SCHEMAS["story_evidence_packet"]["properties"]["method"] == {
        "type": "string",
        "const": "structured-thread-beat-recall-v4",
    }
    assert (
        schemas.packet["properties"]["method"]
        == SCHEMAS["story_evidence_packet"]["properties"]["method"]
    )
    assert SCHEMAS["story_evidence_index"]["additionalProperties"] is False
