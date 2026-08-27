"""Frozen Stage 2 execution-profile closure tests; no Store or provider calls."""

from __future__ import annotations

from dataclasses import replace

import pytest
from autocut_kernel.semantic_chain.story_design_command_policy import Stage2CommandPolicy

from auto_cut_bot.pipeline.runtime import (
    PipelineCommand,
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineRunValidationError,
    PipelineStageContext,
)
from tests.pipeline.runtime_profile_fixture import execution_profile, stage2_command_policy

RUN_ID = "pipeline_run_" + "2" * 32


def test_v9_round_trips_exact_typed_stage2_policy_and_binds_its_identity() -> None:
    policy = stage2_command_policy()
    profile = execution_profile(stage2_policy=policy)

    assert profile.schema_version == "pipeline-execution-profile-v9"
    assert profile.to_mapping()["stage2_command_policy"] == policy.to_mapping()
    assert profile.build_stage2_command_policy() == policy
    assert type(profile.build_stage2_command_policy()) is Stage2CommandPolicy
    assert PipelineExecutionProfile.from_mapping(profile.to_mapping()) == profile

    copied = profile.to_mapping()
    copied["stage2_command_policy"]["generation"]["model_id"] = "caller-copy"
    assert profile.build_stage2_command_policy() == policy


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("artifact_revision",), 2),
        (("generation", "model_id"), "another-stage2-model"),
        (("generation", "temperature"), "0.75"),
        (("max_prompt_bytes",), 63_999),
        (("draft_policy", "max_proposals"), 7),
        (("candidate_policy", "minimum_confidence"), "0.6"),
        (("job_policy", "max_search_states"), 99),
        (("retry_policy", "backoff_seconds"), [2]),
    ],
)
def test_each_stage2_policy_component_changes_profile_identity(path, value) -> None:
    original = execution_profile()
    mapping = original.to_mapping()
    target = mapping["stage2_command_policy"]
    for field_name in path[:-1]:
        target = target[field_name]
    target[path[-1]] = value

    changed = PipelineExecutionProfile.from_mapping(mapping)
    assert changed.canonical_hash != original.canonical_hash
    assert changed.build_stage2_command_policy().canonical_hash != (
        original.build_stage2_command_policy().canonical_hash
    )


@pytest.mark.parametrize(
    "section",
    [
        None,
        "generation",
        "draft_policy",
        "candidate_policy",
        "job_policy",
        "story_policy",
        "retry_policy",
    ],
)
def test_stage2_policy_rejects_unknown_fields_at_all_object_boundaries(section: str | None) -> None:
    mapping = execution_profile().to_mapping()
    target = mapping["stage2_command_policy"]
    if section is not None:
        target = target[section]
    target["implicit_default"] = True
    with pytest.raises(PipelineRunValidationError, match="stage2_command_policy_json"):
        PipelineExecutionProfile.from_mapping(mapping)


@pytest.mark.parametrize(
    "field",
    [
        "artifact_revision",
        "generation",
        "max_prompt_bytes",
        "draft_policy",
        "candidate_policy",
        "job_policy",
        "story_policy",
        "retry_policy",
    ],
)
def test_stage2_policy_requires_all_closed_sections(field: str) -> None:
    mapping = execution_profile().to_mapping()
    del mapping["stage2_command_policy"][field]
    with pytest.raises(PipelineRunValidationError, match="stage2_command_policy_json"):
        PipelineExecutionProfile.from_mapping(mapping)


@pytest.mark.parametrize("raw", [None, {}, [], "{}", True])
def test_v9_requires_typed_closed_stage2_policy(raw: object) -> None:
    mapping = execution_profile().to_mapping()
    mapping["stage2_command_policy"] = raw
    with pytest.raises(PipelineRunValidationError):
        PipelineExecutionProfile.from_mapping(mapping)


def test_stage2_context_requires_v9_and_never_backfills_historical_policy() -> None:
    current = execution_profile()
    mapping = current.to_mapping()
    mapping["schema_version"] = "pipeline-execution-profile-v6"
    del mapping["evidence_read_limits"]
    del mapping["stage2_command_policy"]
    del mapping["stage3_command_policy"]
    historical = PipelineExecutionProfile.from_mapping(mapping)
    request = PipelineRunRequest("test", source_reference="synthetic-source")

    with pytest.raises(PipelineRunValidationError, match="profile v7"):
        historical.build_stage2_command_policy()
    with pytest.raises(PipelineRunValidationError, match="profile v9"):
        PipelineStageContext(
            RUN_ID, PipelineRunRequest("test", source_reference="synthetic-source"),
            PipelineCommand("historical-context", "stage1_narrative", "pending"), historical,
        )
    with pytest.raises(PipelineRunValidationError, match="profile v9"):
        PipelineStageContext(
            RUN_ID,
            request,
            PipelineCommand("stage2-command", "stage2_portfolio", "pending"),
            historical,
        )

    context = PipelineStageContext(
        RUN_ID,
        request,
        PipelineCommand("stage2-command", "stage2_portfolio", "pending"),
        current,
    )
    assert context.execution_profile.build_stage2_command_policy() == stage2_command_policy()


def test_profile_rejects_noncanonical_stage2_embedded_json() -> None:
    profile = execution_profile()
    with pytest.raises(PipelineRunValidationError):
        replace(profile, stage2_command_policy_json="{}")
    with pytest.raises(PipelineRunValidationError):
        replace(profile, stage2_command_policy_json=" " + profile.stage2_command_policy_json)


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 6, 7, 8])
def test_every_historical_version_round_trips_but_cannot_execute(version: int) -> None:
    mapping = execution_profile().to_mapping()
    mapping["schema_version"] = f"pipeline-execution-profile-v{version}"
    del mapping["evidence_read_limits"]
    if version < 8:
        del mapping["stage3_command_policy"]
    if version < 7:
        del mapping["stage2_command_policy"]
    if version < 6:
        del mapping["stage1_command_policy"]
    if version < 5:
        del mapping["materialization_limits"]
    if version < 3:
        del mapping["media_preflight_policy"]
        del mapping["media_preflight_policy_hash"]
    if version < 2:
        del mapping["generation_retry_policy"]
    if version < 4:
        mapping["parse_policy"] = {
            "max_observations": 64, "max_response_bytes": 64000,
            "max_summary_characters": 512, "max_total_summary_characters": 8192,
            "minimum_confidence": "0.80",
        }
    historical = PipelineExecutionProfile.from_mapping(mapping)
    assert historical.to_mapping() == mapping
    historical_hash = historical.canonical_hash
    with pytest.raises(PipelineRunValidationError, match="read-only"):
        historical.to_doubao_policy()
    if version < 7:
        with pytest.raises(PipelineRunValidationError, match="profile v7"):
            historical.build_stage2_command_policy()
    else:
        assert historical.build_stage2_command_policy() == stage2_command_policy()
    if version < 8:
        assert historical.stage3_command_policy_json is None
        with pytest.raises(PipelineRunValidationError, match="profile v8"):
            historical.build_stage3_command_policy()
    else:
        assert historical.build_stage3_command_policy() == execution_profile().build_stage3_command_policy()
    for stage in ("vlm", "stage1_narrative", "stage2_portfolio", "stage3_blueprint", "media_preflight"):
        expected_error = (
            "VLM execution requires a persisted current execution profile"
            if stage == "vlm" else "physical/story stages require execution profile v9"
        )
        with pytest.raises(PipelineRunValidationError, match=expected_error):
            PipelineStageContext(
                RUN_ID, PipelineRunRequest("test", source_reference="synthetic-source"),
                PipelineCommand("historical-context", stage, "pending"), historical,
            )
    assert historical.to_mapping() == mapping
    assert historical.canonical_hash == historical_hash
