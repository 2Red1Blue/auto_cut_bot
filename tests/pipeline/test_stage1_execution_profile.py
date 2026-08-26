"""Frozen runtime policy tests with synthetic values; no DB/provider acceptance."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest
from autocut_kernel.semantic_chain.stage1_command_policy import Stage1CommandPolicy

from auto_cut_bot.pipeline.runtime import (
    PipelineCommand,
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineRunSnapshot,
    PipelineRunValidationError,
    PipelineStageContext,
    postgres,
)
from tests.pipeline.runtime_profile_fixture import execution_profile, stage1_command_policy

STAGES = ("source_prep", "vlm", "stage1_narrative", "media_preflight")
RUN_ID = "pipeline_run_" + "1" * 32


def _historical_v5():
    wire = execution_profile().to_mapping()
    wire["schema_version"] = "pipeline-execution-profile-v5"
    del wire["stage1_command_policy"]
    # Historical v1 adapter bytes remain history, not a silent v2 upgrade.
    wire["adapter_strategy_version"] = "doubao-ark-files-responses-stream-v1"
    wire["request_parameters"]["adapter_strategy_version"] = wire["adapter_strategy_version"]
    return wire, PipelineExecutionProfile.from_mapping(wire)


def test_v6_round_trips_complete_immutable_stage1_policy_without_defaults():
    policy = stage1_command_policy()
    profile = execution_profile(stage1_policy=policy)
    assert profile.schema_version == "pipeline-execution-profile-v6"
    assert profile.to_mapping()["stage1_command_policy"] == policy.to_mapping()
    assert profile.build_stage1_command_policy() == policy
    assert type(profile.build_stage1_command_policy()) is Stage1CommandPolicy
    assert PipelineExecutionProfile.from_mapping(profile.to_mapping()) == profile
    assert profile.stage1_command_policy_json == json.dumps(
        policy.to_mapping(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    with pytest.raises(FrozenInstanceError):
        profile.stage1_command_policy_json = "{}"
    copy = profile.to_mapping()
    copy["stage1_command_policy"]["generation"]["model_id"] = "changed-caller-copy"
    assert profile.build_stage1_command_policy() == policy


@pytest.mark.parametrize(
    "path,value",
    [
        (("artifact_revision",), 2),
        (("generation", "model_id"), "different-model"),
        (("generation", "prompt_version"), "synthetic-prompt-v2"),
        (("generation", "prompt_template"), "Different prompt source."),
        (("generation", "max_output_tokens"), 2048),
        (("generation", "temperature"), "0.75"),
        (("draft_policy", "max_response_bytes"), 65_000),
        (("draft_policy", "max_prompt_bytes"), 65_000),
        (("draft_policy", "max_input_windows"), 5),
        (("draft_policy", "max_input_objects"), 33),
        (("draft_policy", "max_beats"), 5),
        (("draft_policy", "max_obligations"), 5),
        (("draft_policy", "max_story_threads"), 5),
        (("draft_policy", "max_merge_proposals"), 5),
        (("draft_policy", "max_references_per_item"), 9),
        (("draft_policy", "max_text_characters"), 257),
        (("draft_policy", "max_total_text_characters"), 2049),
        (("coverage_policy", "minimum_confidence"), "0.9"),
        (("retry_policy", "backoff_seconds"), [3, 8]),
    ],
)
def test_every_variable_stage1_policy_input_changes_execution_identity(path, value):
    original = execution_profile()
    wire = original.to_mapping()
    target = wire["stage1_command_policy"]
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value
    changed = PipelineExecutionProfile.from_mapping(wire)
    assert changed.canonical_hash != original.canonical_hash
    assert changed.build_stage1_command_policy().canonical_hash != (
        original.build_stage1_command_policy().canonical_hash
    )
    assert changed.to_mapping()["stage1_command_policy"] == wire["stage1_command_policy"]
    with pytest.raises(PipelineRunValidationError, match="hash does not bind"):
        postgres._execution_profile(wire, original.canonical_hash)


@pytest.mark.parametrize(
    "section", [None, "generation", "draft_policy", "coverage_policy", "dependency_policy", "retry_policy"]
)
def test_stage1_policy_rejects_unknown_fields_at_every_object_boundary(section):
    wire = execution_profile().to_mapping()
    target = wire["stage1_command_policy"]
    if section is not None:
        target = target[section]
    target["implicit_default"] = True
    with pytest.raises(PipelineRunValidationError, match="stage1_command_policy_json"):
        PipelineExecutionProfile.from_mapping(wire)


@pytest.mark.parametrize("field", [
    "artifact_revision", "generation", "draft_policy", "coverage_policy", "dependency_policy", "retry_policy",
])
def test_stage1_policy_requires_all_sections(field):
    wire = execution_profile().to_mapping()
    del wire["stage1_command_policy"][field]
    with pytest.raises(PipelineRunValidationError, match="stage1_command_policy_json"):
        PipelineExecutionProfile.from_mapping(wire)


@pytest.mark.parametrize("invalid", [None, {}, [], "{}", True])
def test_v6_requires_complete_object_policy(invalid):
    wire = execution_profile().to_mapping()
    wire["stage1_command_policy"] = invalid
    with pytest.raises(PipelineRunValidationError):
        PipelineExecutionProfile.from_mapping(wire)


def test_v6_cannot_omit_policy_or_supply_noncanonical_embedded_json():
    profile = execution_profile()
    wire = profile.to_mapping()
    del wire["stage1_command_policy"]
    with pytest.raises(PipelineRunValidationError, match="missing fields"):
        PipelineExecutionProfile.from_mapping(wire)
    for raw in ("{}", "[]", "null", '{"artifact_revision":1,"artifact_revision":2}',
                " " + profile.stage1_command_policy_json):
        with pytest.raises(PipelineRunValidationError):
            replace(profile, stage1_command_policy_json=raw)


def test_from_policies_requires_explicit_typed_stage1_policy():
    profile = execution_profile()
    arguments = {
        "retry_policy": profile.to_generation_retry_policy(),
        "materialization_limits": profile.to_materialization_limits(),
    }
    with pytest.raises(TypeError, match="stage1_policy"):
        PipelineExecutionProfile.from_policies(
            profile.to_doubao_policy(), profile.to_media_preflight_policy(), **arguments,
        )
    with pytest.raises(PipelineRunValidationError, match="exact Stage1CommandPolicy"):
        PipelineExecutionProfile.from_policies(
            profile.to_doubao_policy(), profile.to_media_preflight_policy(),
            **arguments, stage1_policy=stage1_command_policy().to_mapping(),
        )


def test_historical_v5_is_exact_read_only_without_stage1_or_adapter_upgrade():
    wire, historical = _historical_v5()
    assert historical.to_mapping() == wire
    assert historical.stage1_command_policy_json is None
    assert not historical.has_media_preflight_policy
    assert postgres._execution_profile(wire, historical.canonical_hash) == historical
    with pytest.raises(PipelineRunValidationError, match="profile v6"):
        historical.build_stage1_command_policy()
    with pytest.raises(PipelineRunValidationError, match="read-only"):
        historical.to_doubao_policy()
    with pytest.raises(PipelineRunValidationError, match="persisted mappings"):
        replace(execution_profile(), schema_version="pipeline-execution-profile-v5")
    wire["stage1_command_policy"] = stage1_command_policy().to_mapping()
    with pytest.raises(PipelineRunValidationError, match="unsupported fields"):
        PipelineExecutionProfile.from_mapping(wire)


@pytest.mark.parametrize("stage", ["vlm", "stage1_narrative", "media_preflight"])
@pytest.mark.parametrize("status", ["pending", "indeterminate"])
def test_historical_v5_cannot_form_execute_or_reconcile_context(stage, status):
    _, historical = _historical_v5()
    with pytest.raises(PipelineRunValidationError, match="profile v6"):
        PipelineStageContext(
            RUN_ID, PipelineRunRequest("test", source_reference="synthetic-source"),
            PipelineCommand("command-history", stage, status), historical,
        )


def test_partial_four_stage_plan_still_cannot_claim_whole_run_success():
    rows = [(stage, "succeeded") for stage in STAGES]
    assert postgres._PIPELINE_SUCCESS_TERMINAL_STAGE is None
    assert postgres._terminal_run_state(rows) == "failed"
    assert postgres._terminal_run_state(rows[:3] + [("media_preflight", "pending")]) == "running"
    request = PipelineRunRequest("test", source_reference="synthetic-source")
    snapshot = PipelineRunSnapshot(
        RUN_ID, request, request.request_hash, "failed",
        tuple(PipelineCommand(f"command-{index}", stage, "succeeded", uuid4())
              for index, stage in enumerate(STAGES)),
        1, execution_profile(),
    )
    assert snapshot.status == "failed"
    assert all(command.status == "succeeded" for command in snapshot.commands)
