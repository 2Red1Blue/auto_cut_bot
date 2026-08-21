"""Contract tests for preflight-materialized Story Script causality."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

import pytest
from autocut_core.contracts.story_script_causal import (
    CAUSAL_DEPENDENCY_KEYS,
    validate_story_script_causal_dependency,
)
from autocut_core.libs.script_preflight import _materialize_cross_unit_contract
from autocut_core.schema.compat import SCHEMAS, validate_task_response


def _valid_dependency(*, explains_opening_highlight: bool = False) -> dict[str, object]:
    prerequisite_ids = ["event-opening-cause"] if explains_opening_highlight else []
    return {
        "explains_opening_highlight": explains_opening_highlight,
        "required_before_fact_ids": [],
        "required_relationship_ids": [],
        "required_event_ids": prerequisite_ids,
        "required_thread_beat_ids": [],
        "causal_ancestor_episode_range": {
            "min_episode": 1,
            "max_episode": 2,
            "reason": "The prerequisite occurs before the opening highlight.",
        },
        "cross_unit_retrieval": {
            "required": explains_opening_highlight,
            "source_unit_ids": [],
            "retrieval_status": "pending" if explains_opening_highlight else "covered",
        },
    }


def _minimum_value(schema: Mapping[str, object]) -> object:
    """Build a valid value for compat's deterministic, closed JSON subset."""
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]

    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        assert isinstance(properties, Mapping)
        assert isinstance(required, list)
        result: dict[str, object] = {}
        for key in required:
            assert isinstance(key, str)
            property_schema = properties.get(key)
            assert isinstance(property_schema, Mapping)
            result[key] = _minimum_value(property_schema)
        return result
    if schema_type == "array":
        item_schema = schema.get("items")
        assert isinstance(item_schema, Mapping)
        minimum_items = schema.get("minItems", 0)
        assert isinstance(minimum_items, int)
        return [_minimum_value(item_schema) for _ in range(minimum_items)]
    if schema_type == "string":
        minimum_length = schema.get("minLength", 0)
        assert isinstance(minimum_length, int)
        return "x" * max(1, minimum_length)
    if schema_type == "integer":
        minimum = schema.get("minimum", 0)
        assert isinstance(minimum, int)
        return minimum
    if schema_type == "number":
        minimum = schema.get("minimum", 0.0)
        assert isinstance(minimum, (int, float))
        return float(minimum)
    if schema_type == "boolean":
        return False
    raise AssertionError(f"unsupported compat schema type: {schema_type!r}")


def _final_story_script_fixture() -> dict[str, object]:
    story_schema = SCHEMAS["story_script"]
    assert isinstance(story_schema, Mapping)
    fixture = _minimum_value(story_schema)
    assert isinstance(fixture, dict)
    beats = fixture.get("beats")
    assert isinstance(beats, list)
    for beat in beats:
        assert isinstance(beat, dict)
        beat["causal_dependency"] = _valid_dependency()
    return fixture


def test_accepts_exact_non_explanatory_contract() -> None:
    dependency = _valid_dependency()

    assert set(dependency) == CAUSAL_DEPENDENCY_KEYS
    assert validate_story_script_causal_dependency(dependency) == []


def test_accepts_explanatory_contract_with_prerequisite() -> None:
    assert (
        validate_story_script_causal_dependency(_valid_dependency(explains_opening_highlight=True))
        == []
    )


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (lambda value: value.pop("required_event_ids"), "missing required keys"),
        (lambda value: value.__setitem__("unexpected", "value"), "unknown keys"),
        (
            lambda value: value.__setitem__("required_event_ids", [" "]),
            "expected nonempty string ID",
        ),
        (
            lambda value: value.__setitem__("required_event_ids", ["event-a", "event-a"]),
            "duplicate ID",
        ),
        (
            lambda value: value["causal_ancestor_episode_range"].__setitem__("max_episode", 0),
            "expected integer >= 1",
        ),
        (
            lambda value: value["causal_ancestor_episode_range"].__setitem__("max_episode", 0),
            "max_episode must be >= min_episode",
        ),
        (
            lambda value: value["cross_unit_retrieval"].__setitem__(
                "retrieval_status", "unverified"
            ),
            "expected one of",
        ),
    ],
)
def test_rejects_malformed_closed_contract(
    mutator: object,
    expected: str,
) -> None:
    dependency = _valid_dependency()
    assert callable(mutator)
    mutator(dependency)

    errors = validate_story_script_causal_dependency(dependency)

    assert any(expected in error for error in errors)


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (
            lambda value: value.__setitem__("required_event_ids", ["event-cause"]),
            "non-explanatory beat must not declare",
        ),
        (
            lambda value: value["cross_unit_retrieval"].__setitem__("required", True),
            "must be false when the beat does not explain",
        ),
        (
            lambda value: value["cross_unit_retrieval"].__setitem__(
                "source_unit_ids", ["unit-001"]
            ),
            "must be empty when the beat does not explain",
        ),
        (
            lambda value: value["cross_unit_retrieval"].__setitem__("retrieval_status", "pending"),
            "must be 'covered' when the beat does not explain",
        ),
    ],
)
def test_non_explanatory_semantics_are_fail_closed(
    mutator: object,
    expected: str,
) -> None:
    dependency = _valid_dependency()
    assert callable(mutator)
    mutator(dependency)

    errors = validate_story_script_causal_dependency(dependency)

    assert any(expected in error for error in errors)


def test_explanatory_contract_requires_a_prerequisite_id() -> None:
    dependency = _valid_dependency(explains_opening_highlight=True)
    dependency["required_event_ids"] = []

    errors = validate_story_script_causal_dependency(dependency)

    assert any("requires at least one causal prerequisite ID" in error for error in errors)


def test_real_final_story_script_validator_invokes_causal_contract() -> None:
    script = _final_story_script_fixture()

    assert validate_task_response("story_script", script) == []

    malformed = deepcopy(script)
    beats = malformed["beats"]
    assert isinstance(beats, list)
    first_beat = beats[0]
    assert isinstance(first_beat, dict)
    causal = first_beat["causal_dependency"]
    assert isinstance(causal, dict)
    causal["cross_unit_retrieval"]["retrieval_status"] = "unverified"

    errors = validate_task_response("story_script", malformed)

    assert any("retrieval_status: expected one of" in error for error in errors)


def test_final_story_script_schema_owns_the_exact_causal_field() -> None:
    story_schema = SCHEMAS["story_script"]
    assert isinstance(story_schema, Mapping)
    story_properties = story_schema.get("properties")
    assert isinstance(story_properties, Mapping)
    beats_schema = story_properties.get("beats")
    assert isinstance(beats_schema, Mapping)
    beat_schema = beats_schema.get("items")
    assert isinstance(beat_schema, Mapping)
    beat_properties = beat_schema.get("properties")
    assert isinstance(beat_properties, Mapping)
    causal_schema = beat_properties.get("causal_dependency")
    assert isinstance(causal_schema, Mapping)

    causal_properties = causal_schema.get("properties")
    causal_required = causal_schema.get("required")
    assert isinstance(causal_properties, Mapping)
    assert isinstance(causal_required, list)
    assert set(causal_properties) == CAUSAL_DEPENDENCY_KEYS
    assert set(causal_required) == CAUSAL_DEPENDENCY_KEYS
    assert causal_schema.get("additionalProperties") is False

    script = _final_story_script_fixture()
    assert validate_task_response("story_script", script) == []


def test_draft_schema_remains_causal_free() -> None:
    draft_schema = SCHEMAS["story_script_draft"]
    assert isinstance(draft_schema, Mapping)
    properties = draft_schema.get("properties")
    assert isinstance(properties, Mapping)
    beats_schema = properties.get("beats")
    assert isinstance(beats_schema, Mapping)
    items = beats_schema.get("items")
    assert isinstance(items, Mapping)
    beat_properties = items.get("properties")
    assert isinstance(beat_properties, Mapping)

    assert "causal_dependency" not in beat_properties


def test_preflight_materializes_contracts_accepted_by_final_validator() -> None:
    _, beats = _materialize_cross_unit_contract(
        {"teaser_contract": {"primary_highlight_candidate_id": "candidate-opening"}},
        beats=[
            {
                "id": "beat-mainline",
                "temporal_position": "mainline",
                "retrieval_requirements": {},
                "event_ids": [],
                "must_show": [],
            },
            {
                "id": "beat-explanation",
                "temporal_position": "earlier_context",
                "retrieval_requirements": {"event_ids": ["event-cause"]},
                "event_ids": ["event-cause"],
                "must_show": [],
            },
        ],
        events={"event-cause": {"episode": 1}},
        facts={},
        relationships={},
        thread_beats={},
        candidates={"candidate-opening": {"episode": 2}},
        edit_mode="montage",
    )

    assert all(
        validate_story_script_causal_dependency(beat["causal_dependency"]) == [] for beat in beats
    )
