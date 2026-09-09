from __future__ import annotations

from copy import deepcopy

import pytest
from autocut_kernel.store.models import MaterializationLimits

from auto_cut_bot.pipeline.runtime.errors import PipelineRunValidationError
from auto_cut_bot.pipeline.runtime.models import (
    EvidenceReadLimits,
    PipelineCommand,
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineStageContext,
)
from auto_cut_bot.pipeline.runtime.semantic_authority import (
    load_installed_semantic_run_authority,
)
from tests.pipeline.runtime_profile_fixture import (
    execution_profile,
    media_preflight_policy,
    stage1_command_policy,
    stage2_command_policy,
    stage3_command_policy,
)

_RUN_ID = "pipeline_run_" + "c" * 32


def _materialization_limits() -> MaterializationLimits:
    return MaterializationLimits(
        max_source_bytes=8 * 1024 * 1024,
        timed_speech_max_request_bytes=8 * 1024 * 1024,
        copy_chunk_bytes=64 * 1024,
        staging_quota_bytes=16 * 1024 * 1024,
    )


def _profile() -> PipelineExecutionProfile:
    semantic = load_installed_semantic_run_authority()
    return PipelineExecutionProfile.from_semantic_story_media_policies(
        semantic.vlm_policy,
        media_preflight_policy(),
        retry_policy=semantic.retry_policy,
        materialization_limits=_materialization_limits(),
        stage1_policy=semantic.stage1_command_policy,
        stage2_policy=semantic.stage2_command_policy,
        stage3_policy=semantic.stage3_command_policy,
        evidence_read_limits=EvidenceReadLimits(100_000, 500_000),
    )


def test_v12_is_the_closed_union_of_semantic_story_and_media_policies() -> None:
    profile = _profile()
    mapping = profile.to_mapping()
    restored = PipelineExecutionProfile.from_mapping(mapping)

    assert restored == profile
    assert restored.canonical_hash == profile.canonical_hash
    assert restored.schema_version == "pipeline-execution-profile-v12"
    assert restored.is_semantic_story
    assert restored.is_semantic_story_media
    assert not restored.is_semantic_only
    assert restored.has_media_preflight_policy
    assert restored.has_executable_plan
    assert restored.to_doubao_policy() == load_installed_semantic_run_authority().vlm_policy
    assert restored.to_media_preflight_policy() == media_preflight_policy()
    assert restored.to_materialization_limits() == _materialization_limits()
    assert restored.to_evidence_read_limits() == EvidenceReadLimits(100_000, 500_000)
    assert (
        restored.build_stage1_command_policy()
        == load_installed_semantic_run_authority().stage1_command_policy
    )
    assert (
        restored.build_stage2_command_policy()
        == load_installed_semantic_run_authority().stage2_command_policy
    )
    assert (
        restored.build_stage3_command_policy()
        == load_installed_semantic_run_authority().stage3_command_policy
    )
    assert set(mapping) == {
        "adapter_strategy_version",
        "evidence_read_limits",
        "generation_retry_policy",
        "kernel_parser_strategy_version",
        "kind",
        "materialization_limits",
        "media_preflight_policy",
        "media_preflight_policy_hash",
        "model_id",
        "parse_policy",
        "parser_contract_sha256",
        "prompt_version",
        "provider_id",
        "request_parameters",
        "response_schema",
        "schema_version",
        "stage1_command_policy",
        "stage2_command_policy",
        "stage3_command_policy",
        "vlm_stage_strategy_version",
    }
    assert not any("stage4" in field for field in mapping)


@pytest.mark.parametrize(
    "field",
    (
        "media_preflight_policy",
        "media_preflight_policy_hash",
        "materialization_limits",
        "stage1_command_policy",
        "stage2_command_policy",
        "stage3_command_policy",
        "evidence_read_limits",
    ),
)
def test_v12_requires_every_union_member(field: str) -> None:
    mapping = deepcopy(_profile().to_mapping())
    del mapping[field]

    with pytest.raises(PipelineRunValidationError, match="missing fields"):
        PipelineExecutionProfile.from_mapping(mapping)


def test_v12_rejects_stage4_authority_or_legacy_vlm_authority() -> None:
    mapping = _profile().to_mapping()
    mapping["stage4_authority_profile"] = {"authority": "separate-command-only"}
    with pytest.raises(PipelineRunValidationError, match="unsupported fields"):
        PipelineExecutionProfile.from_mapping(mapping)

    legacy = execution_profile()
    with pytest.raises(PipelineRunValidationError, match="registered V23 VLM authority"):
        PipelineExecutionProfile.from_semantic_story_media_policies(
            legacy.to_doubao_policy(),
            legacy.to_media_preflight_policy(),
            retry_policy=legacy.to_generation_retry_policy(),
            materialization_limits=legacy.to_materialization_limits(),
            stage1_policy=stage1_command_policy(),
            stage2_policy=stage2_command_policy(),
            stage3_policy=stage3_command_policy(),
            evidence_read_limits=legacy.to_evidence_read_limits(),
        )


def test_v12_allows_its_stages_without_migrating_v9_v10_or_v11_hashes() -> None:
    semantic = load_installed_semantic_run_authority()
    old_profiles = (
        execution_profile(),
        PipelineExecutionProfile.from_semantic_policies(
            semantic.vlm_policy,
            retry_policy=semantic.retry_policy,
        ),
        PipelineExecutionProfile.from_semantic_story_policies(
            semantic.vlm_policy,
            retry_policy=semantic.retry_policy,
            stage1_policy=semantic.stage1_command_policy,
            stage2_policy=semantic.stage2_command_policy,
            stage3_policy=semantic.stage3_command_policy,
        ),
    )
    old_snapshots = tuple(
        (profile.to_mapping(), profile.canonical_hash) for profile in old_profiles
    )

    profile = _profile()
    request = PipelineRunRequest("test", source_reference="synthetic-source")
    for stage in (
        "vlm",
        "stage1_narrative",
        "stage2_portfolio",
        "stage3_blueprint",
        "media_preflight",
        "stage4_recipe",
    ):
        context = PipelineStageContext(
            _RUN_ID,
            request,
            PipelineCommand(f"{stage}-command", stage, "pending"),
            profile,
        )
        assert context.execution_profile_hash == profile.canonical_hash

    for old, (mapping, canonical_hash) in zip(old_profiles, old_snapshots, strict=True):
        assert old.to_mapping() == mapping
        assert old.canonical_hash == canonical_hash
        assert PipelineExecutionProfile.from_mapping(mapping).canonical_hash == canonical_hash
