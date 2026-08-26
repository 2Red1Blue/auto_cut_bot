"""Closed input-free Stage 3 policy tests; no Store or provider dispatch."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.semantic_chain.editorial_blueprint import EDITORIAL_BLUEPRINT_STRATEGY_VERSION
from autocut_kernel.semantic_chain.editorial_command_policy import Stage3CommandPolicy
from autocut_kernel.semantic_chain.editorial_context_models import EditorialContextPolicy
from autocut_kernel.semantic_chain.editorial_draft import EditorialDraftPolicy
from autocut_kernel.semantic_chain.editorial_feasibility import EditorialFeasibilityPolicy
from autocut_kernel.semantic_chain.stage1_command_policy import Stage1GenerationPolicy
from autocut_kernel.vlm.retry_policy import GenerationRetryPolicy


def _policy() -> Stage3CommandPolicy:
    return Stage3CommandPolicy(
        1,
        Stage1GenerationPolicy(
            "doubao-ark-text-responses-stream", "test-stage3-model", "stage3-v1",
            "Compile every frozen Story Blueprint.",
            "doubao-ark-text-responses-stream-v1", 1024, "0.5",
        ),
        2_000_000,
        EditorialDraftPolicy(
            "bytes", 256_000, 24, 4, 8, 16, 8, 32, 8, 64, 16, 256, 16, 32, 5_000, 100_000,
        ),
        EditorialContextPolicy("unpartitioned-batch-v1", "bytes", 2_000_000, 2_000_000, 100),
        EditorialFeasibilityPolicy("editorial-material-feasibility-v1", 1_000),
        GenerationRetryPolicy("generation-retry-v1", 2, (0,)),
        EDITORIAL_BLUEPRINT_STRATEGY_VERSION,
    )


def test_policy_is_closed_immutable_and_has_no_request_or_job_authority() -> None:
    policy = _policy()
    assert Stage3CommandPolicy.from_mapping(json.loads(canonical_json_bytes(policy.to_mapping()))) == policy
    assert policy.canonical_hash == canonical_json_hash(policy.to_mapping())
    assert not hasattr(policy, "job") and not hasattr(policy, "stage2_request")
    with pytest.raises(TypeError):
        Stage3CommandPolicy()
    with pytest.raises(FrozenInstanceError):
        policy.max_prompt_bytes = 1
    for key in policy.to_mapping():
        missing = policy.to_mapping()
        del missing[key]
        with pytest.raises(ValueError):
            Stage3CommandPolicy.from_mapping(missing)
    with pytest.raises(ValueError):
        Stage3CommandPolicy.from_mapping({**policy.to_mapping(), "caller_default": True})


@pytest.mark.parametrize(
    "field,value",
    (
        ("artifact_revision", 0),
        ("artifact_revision", True),
        ("max_prompt_bytes", 0),
        ("max_prompt_bytes", True),
        ("blueprint_strategy_version", "partitioned-v1"),
        ("generation", {}),
        ("draft_policy", {}),
        ("context_policy", {}),
        ("feasibility_policy", {}),
        ("retry_policy", {}),
    ),
)
def test_policy_rejects_unregistered_or_untyped_components(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(_policy(), **{field: value})


def test_every_policy_component_is_hash_bound() -> None:
    policy = _policy()
    changes = (
        replace(policy, artifact_revision=2),
        replace(policy, generation=replace(policy.generation, temperature="0")),
        replace(policy, max_prompt_bytes=policy.max_prompt_bytes - 1),
        replace(policy, draft_policy=replace(policy.draft_policy, max_response_bytes=policy.draft_policy.max_response_bytes + 1)),
        replace(policy, context_policy=replace(policy.context_policy, max_source_members=policy.context_policy.max_source_members + 1)),
        replace(policy, feasibility_policy=replace(policy.feasibility_policy, max_search_states=policy.feasibility_policy.max_search_states + 1)),
        replace(policy, retry_policy=GenerationRetryPolicy("generation-retry-v1", 1, ())),
    )
    assert all(changed.canonical_hash != policy.canonical_hash for changed in changes)
