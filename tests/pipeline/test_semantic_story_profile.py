from __future__ import annotations

from copy import deepcopy

import pytest

from auto_cut_bot.pipeline.runtime.errors import PipelineRunValidationError
from auto_cut_bot.pipeline.runtime.models import PipelineExecutionProfile
from auto_cut_bot.pipeline.runtime.semantic_authority import (
    load_installed_semantic_run_authority,
)
from tests.pipeline.runtime_profile_fixture import (
    execution_profile,
    stage1_command_policy,
    stage2_command_policy,
    stage3_command_policy,
)


def _profile() -> PipelineExecutionProfile:
    semantic = load_installed_semantic_run_authority()
    return PipelineExecutionProfile.from_semantic_story_policies(
        semantic.vlm_policy,
        retry_policy=semantic.retry_policy,
        stage1_policy=stage1_command_policy(),
        stage2_policy=stage2_command_policy(),
        stage3_policy=stage3_command_policy(),
    )


def test_v11_semantic_story_profile_roundtrips_without_physical_fields() -> None:
    profile = _profile()
    restored = PipelineExecutionProfile.from_mapping(profile.to_mapping())

    assert restored == profile
    assert restored.is_semantic_story
    assert not restored.is_semantic_only
    assert not restored.has_media_preflight_policy
    assert restored.schema_version == "pipeline-execution-profile-v11"
    assert restored.prompt_version == "vlm-semantic-pack-v23-context-assisted-candidate-core"
    assert restored.to_doubao_policy() == load_installed_semantic_run_authority().vlm_policy
    assert restored.build_stage1_command_policy() == stage1_command_policy()
    assert restored.build_stage2_command_policy() == stage2_command_policy()
    assert restored.build_stage3_command_policy() == stage3_command_policy()
    assert not {
        "media_preflight_policy",
        "media_preflight_policy_hash",
        "materialization_limits",
        "evidence_read_limits",
    }.intersection(restored.to_mapping())


@pytest.mark.parametrize(
    "mutation",
    ("missing_stage", "media_field", "v3_prompt", "semantic_only_schema"),
)
def test_v11_rejects_incomplete_or_mixed_authority(mutation: str) -> None:
    mapping = deepcopy(_profile().to_mapping())
    if mutation == "missing_stage":
        mapping.pop("stage2_command_policy")
    elif mutation == "media_field":
        mapping["media_preflight_policy"] = {}
    elif mutation == "v3_prompt":
        mapping["prompt_version"] = "vlm-semantic-pack-v3"
    else:
        mapping["schema_version"] = "pipeline-execution-profile-v10"

    with pytest.raises(PipelineRunValidationError):
        PipelineExecutionProfile.from_mapping(mapping)


def test_v11_rejects_a_complete_but_legacy_v3_vlm_authority() -> None:
    legacy = execution_profile()

    with pytest.raises(
        PipelineRunValidationError,
        match="registered V23 VLM authority",
    ):
        PipelineExecutionProfile.from_semantic_story_policies(
            legacy.to_doubao_policy(),
            retry_policy=legacy.to_generation_retry_policy(),
            stage1_policy=stage1_command_policy(),
            stage2_policy=stage2_command_policy(),
            stage3_policy=stage3_command_policy(),
        )
