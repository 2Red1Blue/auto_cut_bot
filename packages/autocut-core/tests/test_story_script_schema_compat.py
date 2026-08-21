"""Regression coverage for the final Story Script compatibility boundary."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from autocut_core.schema.compat import (
    SCHEMAS,
    _validate_v3_finalization_contract,
    validate_schema,
    validate_task_response,
)


def _minimum_schema_value(schema: Mapping[str, object]) -> object:
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list) and alternatives:
        branch = alternatives[0]
        assert isinstance(branch, Mapping)
        return _minimum_schema_value(branch)
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    kind = schema.get("type")
    if kind == "object":
        result: dict[str, object] = {}
        properties = _properties(schema)
        required = schema.get("required")
        assert isinstance(required, list)
        for key in required:
            assert isinstance(key, str)
            property_schema = properties[key]
            assert isinstance(property_schema, Mapping)
            result[key] = _minimum_schema_value(property_schema)
        return result
    if kind == "array":
        items = schema.get("items")
        assert isinstance(items, Mapping)
        count = schema.get("minItems", 0)
        assert isinstance(count, int)
        return [_minimum_schema_value(items) for _ in range(count)]
    if kind == "string":
        return "x" * max(1, int(schema.get("minLength", 0)))
    if kind == "integer":
        return int(schema.get("minimum", 0))
    if kind == "number":
        return float(schema.get("minimum", 0))
    if kind == "boolean":
        return False
    raise AssertionError(f"unsupported schema kind: {kind!r}")


def _full_schema_value(schema: Mapping[str, object]) -> object:
    """Build a deterministic value containing every closed property."""
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list) and alternatives:
        branch = alternatives[0]
        assert isinstance(branch, Mapping)
        return _full_schema_value(branch)
    kind = schema.get("type")
    if kind == "object":
        return {
            key: _full_schema_value(property_schema)
            for key, property_schema in _properties(schema).items()
            if isinstance(property_schema, Mapping)
        }
    return _minimum_schema_value(schema)


def _complete_v3_final_script() -> dict[str, object]:
    final = SCHEMAS["story_script"]
    assert isinstance(final, Mapping)
    script = _minimum_schema_value(final)
    assert isinstance(script, dict)
    properties = _properties(final)
    for name in (
        "primary_story_thread_id_source",
        "genre_profile",
        "golden_case_ids",
        "integrated_support_thread_ids",
        "editorial_contract",
        "causal_ancestor_episode_range",
        "required_context_ids",
        "scope_policy",
    ):
        schema = properties[name]
        assert isinstance(schema, Mapping)
        script[name] = _full_schema_value(schema)
    feasibility = script["feasibility"]
    assert isinstance(feasibility, dict)
    feasibility["method"] = "functional-evidence-duration-v3-story-coherence"
    feasibility_schema = properties["feasibility"]
    assert isinstance(feasibility_schema, Mapping)
    feasibility_properties = _properties(feasibility_schema)
    for name in (
        "meets_5_minimum",
        "meets_10_preferred",
        "soft_target_seconds",
        "meets_soft_target",
        "soft_target_gap_seconds",
        "editorial_diagnostics",
    ):
        schema = feasibility_properties[name]
        assert isinstance(schema, Mapping)
        feasibility[name] = _full_schema_value(schema)
    beats = script["beats"]
    assert isinstance(beats, list)
    for beat in beats:
        assert isinstance(beat, dict)
        beat["causal_dependency"] = {
            "explains_opening_highlight": False,
            "required_before_fact_ids": [],
            "required_relationship_ids": [],
            "required_event_ids": [],
            "required_thread_beat_ids": [],
            "causal_ancestor_episode_range": {
                "min_episode": 1,
                "max_episode": 1,
                "reason": "fixture",
            },
            "cross_unit_retrieval": {
                "required": False,
                "source_unit_ids": [],
                "retrieval_status": "covered",
            },
        }
    return script


def _properties(schema: Mapping[str, object]) -> Mapping[str, object]:
    value = schema.get("properties")
    assert isinstance(value, Mapping)
    return value


def test_final_preflight_fields_are_declared_but_draft_stays_clean() -> None:
    final = SCHEMAS["story_script"]
    draft = SCHEMAS["story_script_draft"]
    assert isinstance(final, Mapping)
    assert isinstance(draft, Mapping)
    final_properties = _properties(final)
    draft_properties = _properties(draft)

    final_only = {
        "primary_story_thread_id_source",
        "genre_profile",
        "golden_case_ids",
        "integrated_support_thread_ids",
        "editorial_contract",
        "causal_ancestor_episode_range",
        "required_context_ids",
        "scope_policy",
    }
    assert final_only <= set(final_properties)
    assert not (final_only & set(draft_properties))
    assert "edit_mode" in final_properties
    assert "edit_mode_reason" in final_properties
    required = final.get("required")
    assert isinstance(required, list)
    assert not (final_only & set(required))
    assert "edit_mode" not in required
    assert "edit_mode_reason" not in required


def test_minimal_legacy_v4_final_script_remains_accepted() -> None:
    final = SCHEMAS["story_script"]
    assert isinstance(final, Mapping)
    legacy = _minimum_schema_value(final)
    assert isinstance(legacy, dict)
    beats = legacy["beats"]
    assert isinstance(beats, list)
    for beat in beats:
        assert isinstance(beat, dict)
        beat["causal_dependency"] = {
            "explains_opening_highlight": False,
            "required_before_fact_ids": [],
            "required_relationship_ids": [],
            "required_event_ids": [],
            "required_thread_beat_ids": [],
            "causal_ancestor_episode_range": {
                "min_episode": 1,
                "max_episode": 1,
                "reason": "legacy fixture",
            },
            "cross_unit_retrieval": {
                "required": False,
                "source_unit_ids": [],
                "retrieval_status": "covered",
            },
        }

    assert validate_task_response("story_script", legacy) == []


def test_preflight_scope_and_editorial_contracts_are_closed() -> None:
    final = SCHEMAS["story_script"]
    assert isinstance(final, Mapping)
    properties = _properties(final)
    scope_schema = properties["scope_policy"]
    editorial_schema = properties["editorial_contract"]
    assert isinstance(scope_schema, Mapping)
    assert isinstance(editorial_schema, Mapping)

    assert validate_schema({}, dict(scope_schema)) == []
    assert validate_schema({}, dict(editorial_schema)) == []
    assert any(
        "unknown properties" in error
        for error in validate_schema({"unexpected": True}, dict(scope_schema))
    )
    assert any(
        "unknown properties" in error
        for error in validate_schema({"unexpected": True}, dict(editorial_schema))
    )


def test_feasibility_accepts_real_v3_teaser_shape_without_relaxing_core() -> None:
    final = SCHEMAS["story_script"]
    assert isinstance(final, Mapping)
    feasibility = _properties(final)["feasibility"]
    assert isinstance(feasibility, Mapping)
    feasibility_properties = _properties(feasibility)

    assert (
        validate_schema(
            "functional-evidence-duration-v3-story-coherence",
            dict(feasibility_properties["method"]),
        )
        == []
    )
    assert validate_schema("not-a-preflight-method", dict(feasibility_properties["method"]))
    teaser = feasibility_properties["teaser_diagnostics"]
    assert isinstance(teaser, Mapping)
    valid_none_teaser = {
        "mode": "none",
        "primary_highlight_candidate_id": "",
        "candidate_duration_seconds": 0,
        "physical_obligation_duration_seconds": 0,
        "mandatory_reprise_event_ids": [],
        "maximum_repeat_seconds": 0,
        "repeat_contract_status": "feasible",
        "must_show_ids": [],
        "outside_candidate_must_show_ids": [],
        "status": "feasible",
        "failure_codes": [],
        "repair_route": "story_script",
    }
    assert validate_schema(valid_none_teaser, dict(teaser)) == []
    invalid = dict(valid_none_teaser)
    invalid.pop("repair_route")
    assert any(
        "missing required property 'repair_route'" in error
        for error in validate_schema(invalid, dict(teaser))
    )


def test_editorial_diagnostics_are_closed_and_unknown_fields_are_rejected() -> None:
    final = SCHEMAS["story_script"]
    assert isinstance(final, Mapping)
    feasibility = _properties(final)["feasibility"]
    assert isinstance(feasibility, Mapping)
    diagnostics = _properties(feasibility)["editorial_diagnostics"]
    assert isinstance(diagnostics, Mapping)
    properties = _properties(diagnostics)
    assert diagnostics.get("additionalProperties") is False
    assert "findings" in properties
    assert "policy_version" in properties
    assert any(
        "unknown properties" in error
        for error in validate_schema({"unexpected": True}, dict(diagnostics))
    )


def test_v3_finalization_requires_materialized_scope_editorial_and_diagnostics() -> None:
    final = SCHEMAS["story_script"]
    assert isinstance(final, Mapping)
    properties = _properties(final)
    feasibility = properties["feasibility"]
    assert isinstance(feasibility, Mapping)
    value = {
        "feasibility": {"method": "functional-evidence-duration-v3-story-coherence"},
        "scope_policy": {},
        "editorial_contract": {},
    }
    errors = _validate_v3_finalization_contract(value)
    assert any("scope_policy: required" in error for error in errors)
    assert any("editorial_contract: required" in error for error in errors)
    assert any("editorial_diagnostics: required" in error for error in errors)


def _v3_gate_value(empty_nested: str) -> dict[str, object]:
    nested = {
        "continuity_contract": {
            "same_primary_thread_across_opening_body_ending": True,
            "cross_segment_bridge_required": True,
            "allowed_bridge_types": [],
            "lookback_allowed_only_for": [],
            "lookback_must_return_to_mainline": True,
            "future_complete_arc_injection_forbidden": True,
            "unexplained_jump_status": "blocked",
        },
        "ending_policy": {
            "preferred_landing": "same_primary_thread_hook",
            "hook_types": [],
            "no_hook_fallback": "current_story_line_episode_tail",
            "no_hook_is_allowed": True,
            "invented_hook_forbidden": True,
            "future_arc_after_hook_forbidden": True,
        },
        "duration_extension_policy": {
            "trigger": "below_minimum_duration",
            "minimum_seconds": 0,
            "order": [],
            "after_threshold": "current_episode_tail",
            "same_primary_thread_only": True,
            "must_be_forward_chronological": True,
            "no_cross_thread_fill": True,
            "no_duplicate_or_functionless_fill": True,
            "stop_without_evidence": True,
        },
    }
    nested[empty_nested] = {}
    return {
        "feasibility": {
            "method": "functional-evidence-duration-v3-story-coherence",
            "editorial_diagnostics": {"present": True},
        },
        "scope_policy": {
            "analysis_unit_policy": "processing_only",
            "story_scope_policy": "series_global",
            "cross_unit_retrieval_allowed": True,
            "cross_unit_retrieval_required_for_montage": True,
            "unresolved_dependency_action": "blocked",
            "policy_version": "fixture-v1",
        },
        "editorial_contract": {
            "primary_story_thread_id": "thread-1",
            "secondary_thread_ids": [],
            "integrated_support_thread_ids": [],
            "mainline_type": "fixture",
            "required_bridge_beat_ids": [],
            "same_line_extension_only": True,
            "future_arc_injection_forbidden": True,
            **nested,
            "ending_hook_type": "unresolved_outcome",
            "golden_sample_reference": "fixture",
        },
    }


@pytest.mark.parametrize(
    "nested_name",
    ["continuity_contract", "ending_policy", "duration_extension_policy"],
)
def test_v3_finalization_rejects_empty_nested_editorial_contracts(nested_name: str) -> None:
    errors = _validate_v3_finalization_contract(_v3_gate_value(nested_name))

    assert any(f"editorial_contract.{nested_name}: required" in error for error in errors)


@pytest.mark.parametrize(
    "path",
    [
        ("primary_story_thread_id_source",),
        ("genre_profile",),
        ("golden_case_ids",),
        ("integrated_support_thread_ids",),
        ("causal_ancestor_episode_range",),
        ("required_context_ids",),
        ("feasibility", "meets_5_minimum"),
        ("feasibility", "meets_10_preferred"),
        ("feasibility", "soft_target_seconds"),
        ("feasibility", "meets_soft_target"),
        ("feasibility", "soft_target_gap_seconds"),
    ],
)
def test_v3_final_script_rejects_every_required_materialized_field(
    path: tuple[str, ...],
) -> None:
    script = _complete_v3_final_script()
    target = script
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target.pop(path[-1])

    errors = validate_task_response("story_script", script)

    expected = ".".join(path)
    assert any(expected in error and "required for v3" in error for error in errors)
