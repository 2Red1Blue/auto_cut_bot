from dataclasses import replace

import pytest

from auto_cut_bot.pipeline.runtime import PipelineExecutionProfile
from auto_cut_bot.pipeline.runtime.errors import PipelineRunValidationError
from tests.pipeline.runtime_profile_fixture import execution_profile, stage3_command_policy


def test_v9_roundtrips_the_exact_closed_stage3_policy() -> None:
    profile = execution_profile(stage3_policy=stage3_command_policy())
    assert profile.schema_version == "pipeline-execution-profile-v9"
    assert PipelineExecutionProfile.from_mapping(profile.to_mapping()) == profile
    assert profile.build_stage3_command_policy() == stage3_command_policy()


@pytest.mark.parametrize("field", ("stage3_command_policy", "stage2_command_policy"))
def test_v9_requires_all_semantic_policies(field: str) -> None:
    mapping = execution_profile().to_mapping()
    del mapping[field]
    with pytest.raises(PipelineRunValidationError):
        PipelineExecutionProfile.from_mapping(mapping)


def test_stage3_policy_rehash_changes_execution_profile_identity() -> None:
    original = execution_profile()
    changed = execution_profile(
        stage3_policy=replace(stage3_command_policy(), max_prompt_bytes=64_001),
    )
    assert changed.canonical_hash != original.canonical_hash


@pytest.mark.parametrize(
    ("section", "field", "value"),
    (
        ("generation", "model_id", None),
        ("draft_policy", "max_response_bytes", None),
        ("context_policy", "max_batch_context_bytes", None),
        ("feasibility_policy", "max_search_states", None),
        ("retry_policy", "max_attempts", None),
    ),
)
def test_v9_rejects_null_or_malformed_nested_stage3_policy_sections(
    section: str, field: str, value: object,
) -> None:
    mapping = execution_profile().to_mapping()
    mapping["stage3_command_policy"][section][field] = value  # type: ignore[index]
    with pytest.raises(PipelineRunValidationError):
        PipelineExecutionProfile.from_mapping(mapping)


@pytest.mark.parametrize("section", ("generation", "draft_policy", "context_policy", "feasibility_policy", "retry_policy"))
def test_v9_rejects_unknown_stage3_policy_nested_fields(section: str) -> None:
    mapping = execution_profile().to_mapping()
    mapping["stage3_command_policy"][section]["unknown"] = True  # type: ignore[index]
    with pytest.raises(PipelineRunValidationError):
        PipelineExecutionProfile.from_mapping(mapping)


@pytest.mark.parametrize(
    "change",
    (
        lambda policy: replace(policy, generation=replace(policy.generation, model_id="changed")),
        lambda policy: replace(policy, draft_policy=replace(policy.draft_policy, max_response_bytes=64_001)),
        lambda policy: replace(policy, context_policy=replace(policy.context_policy, max_source_members=101)),
        lambda policy: replace(policy, feasibility_policy=replace(policy.feasibility_policy, max_search_states=1_001)),
        lambda policy: replace(policy, retry_policy=replace(policy.retry_policy, max_attempts=1, backoff_seconds=())),
    ),
)
def test_every_stage3_policy_section_binds_execution_profile_hash(change) -> None:
    assert execution_profile(stage3_policy=change(stage3_command_policy())).canonical_hash != execution_profile().canonical_hash
