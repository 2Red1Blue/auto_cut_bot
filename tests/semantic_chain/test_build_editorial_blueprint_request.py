"""Pure Stage 3 request preparation over actual in-memory Stage 1/2 readers."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_hash, sha256_bytes
from autocut_kernel.pipeline.build_editorial_blueprint_request import (
    BuildEditorialBlueprintRequest,
    prepare_stage3_request,
)
from autocut_kernel.pipeline.compile_story_portfolio_command import CompileStoryPortfolioCommand
from autocut_kernel.pipeline.editorial_blueprint_inputs import (
    read_committed_editorial_blueprint_inputs,
)
from autocut_kernel.semantic_chain.draft_provider import decode_draft_request_payload
from autocut_kernel.semantic_chain.editorial_blueprint import EDITORIAL_BLUEPRINT_STRATEGY_VERSION
from autocut_kernel.semantic_chain.editorial_command_policy import Stage3CommandPolicy
from autocut_kernel.semantic_chain.editorial_context_models import EditorialContextPolicy
from autocut_kernel.semantic_chain.editorial_draft import (
    EditorialDraftPolicy,
    editorial_draft_response_schema,
)
from autocut_kernel.semantic_chain.editorial_feasibility import EditorialFeasibilityPolicy
from autocut_kernel.vlm.retry_policy import GenerationRetryPolicy

from tests.semantic_chain.test_compile_story_portfolio_command import command_case


def _policy(stage2_request) -> Stage3CommandPolicy:
    return Stage3CommandPolicy(
        1,
        replace(
            stage2_request.generation, prompt_version="synthetic-stage3-v1",
            prompt_template="Compile every frozen Story Blueprint without acceptance claims.",
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


def stage3_case():
    """Actual two-Story committed-reader setup for Stage 3 pure-flow consumers."""
    store, provider, stage2_request, _ = command_case(
        job_change={"selected_story_count": 2, "source_reuse_policy": "allow"},
    )
    stage2_result = CompileStoryPortfolioCommand(store, provider).execute(stage2_request)
    assert stage2_result.outcome.state == "succeeded"
    inputs = read_committed_editorial_blueprint_inputs(
        store, stage2_request=stage2_request, stage2_outcome=stage2_result.outcome,
    )
    policy = _policy(stage2_request)
    return inputs, policy.build_request(stage2_request, stage2_result.outcome, "stage3:test-request")


@pytest.fixture(scope="module")
def case():
    inputs, request = stage3_case()
    return request, inputs


def test_closed_request_policy_roundtrip_and_full_batch_provider_context(case) -> None:
    request, inputs = case
    decoded = BuildEditorialBlueprintRequest.from_mapping(request.to_mapping())
    assert decoded.to_mapping() == request.to_mapping()
    assert decoded.stage2_outcome.job_id == request.stage2_outcome.job_id
    assert request.command_policy.build_request(
        request.stage2_request, request.stage2_outcome, request.idempotency_key,
    ).to_mapping() == request.to_mapping()
    prepared = prepare_stage3_request(request, inputs)
    body = decode_draft_request_payload(prepared.provider_payload)
    prompt = body["input"][0]["content"][0]["text"]
    assert prompt.startswith(request.generation.prompt_template + "\n\n")
    assert json.loads(prompt.split("\n\n", 1)[1]) == prepared.contexts.to_mapping()
    assert len(prepared.contexts.stories) == 2
    assert body["text"]["format"]["json_schema"]["schema"] == editorial_draft_response_schema(
        request.draft_policy, target_story_ids=prepared.contexts.target_story_ids,
    )
    assert body["stream"] is body["store"] is True
    durable = json.loads(prepared.request_payload)
    assert durable["command_request"] == request.to_mapping()
    assert durable["input_binding_sha256"] == prepared.contexts.input_binding_sha256
    assert durable["context_sha256"] == prepared.contexts.canonical_hash
    assert durable["stage2_outcome"]["job_id"] == str(request.stage2_outcome.job_id)
    assert durable["stage2_outcome_sha256"] == canonical_json_hash(durable["stage2_outcome"])
    assert durable["provider_request_json"].encode() == prepared.provider_payload
    assert durable["provider_request_sha256"] == sha256_bytes(prepared.provider_payload)
    assert durable["response_schema_sha256"] == canonical_json_hash(body["text"]["format"]["json_schema"]["schema"])
    assert durable["retry_policy"] == request.retry_policy.to_mapping()
    assert durable["retry_policy_sha256"] == request.retry_policy.canonical_hash
    assert prepared.request_hash == sha256_bytes(prepared.request_payload)


@pytest.mark.parametrize("field", ("artifact_revision", "generation", "max_prompt_bytes", "draft_policy", "context_policy", "feasibility_policy", "retry_policy", "idempotency_key"))
def test_every_request_component_changes_durable_identity(case, field: str) -> None:
    request, inputs = case
    changes = {
        "artifact_revision": 2,
        "generation": replace(request.generation, prompt_template=request.generation.prompt_template + "!"),
        "max_prompt_bytes": request.max_prompt_bytes - 1,
        "draft_policy": replace(request.draft_policy, max_response_bytes=request.draft_policy.max_response_bytes + 1),
        "context_policy": replace(request.context_policy, max_source_members=request.context_policy.max_source_members + 1),
        "feasibility_policy": replace(request.feasibility_policy, max_search_states=request.feasibility_policy.max_search_states + 1),
        "retry_policy": GenerationRetryPolicy("generation-retry-v1", 1, ()),
        "idempotency_key": "stage3:another-request",
    }
    original = prepare_stage3_request(request, inputs)
    changed = prepare_stage3_request(replace(request, **{field: changes[field]}), inputs)
    assert changed.request_hash != original.request_hash
    assert changed.provider_idempotency_key_for(1) != original.provider_idempotency_key_for(1)


@pytest.mark.parametrize("field", ("job_id", "command_slot_id", "receipt_id", "artifact_set_id"))
def test_full_stage2_outcome_identity_is_rechecked(case, field: str) -> None:
    request, inputs = case
    changed_outcome = replace(request.stage2_outcome, **{field: UUID(int=999)})
    with pytest.raises(ValueError, match="exact Stage 2"):
        prepare_stage3_request(replace(request, stage2_outcome=changed_outcome), inputs)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("job_id", UUID(int=998)),
        ("request_hash", "sha256:" + "f" * 64),
        ("command_name", "ForeignCommand"),
        ("execution_kind", "deterministic"),
    ),
)
def test_stage2_persisted_record_identity_is_rechecked(case, field: str, value: object) -> None:
    request, inputs = case
    record = replace(inputs.portfolio.record, **{field: value})
    changed_inputs = replace(inputs, portfolio=replace(inputs.portfolio, record=record))
    with pytest.raises(ValueError, match="exact Stage 2"):
        prepare_stage3_request(request, changed_inputs)


def test_stage2_admission_policy_binding_is_not_replaceable_by_a_typed_value(case) -> None:
    request, inputs = case
    admission = replace(inputs.portfolio.values.admission, draft_policy_sha256="sha256:" + "f" * 64)
    values = replace(inputs.portfolio.values, admission=admission)
    changed_inputs = replace(inputs, portfolio=replace(inputs.portfolio, values=values))
    with pytest.raises(ValueError, match="policy bindings"):
        prepare_stage3_request(request, changed_inputs)


@pytest.mark.parametrize("field", ("record", "values"))
def test_mistyped_persisted_portfolio_fields_are_closed_before_dereference(case, field: str) -> None:
    request, inputs = case
    changed_inputs = replace(inputs, portfolio=replace(inputs.portfolio, **{field: {}}))
    with pytest.raises(ValueError, match="exact decoded Stage 1/2"):
        prepare_stage3_request(request, changed_inputs)


def test_budget_is_exact_whole_provider_body_with_no_clipping(case) -> None:
    request, inputs = case
    prepared = prepare_stage3_request(request, inputs)
    exact = prepare_stage3_request(replace(request, max_prompt_bytes=len(prepared.provider_payload)), inputs)
    assert exact.provider_payload == prepared.provider_payload
    with pytest.raises(ValueError, match="complete provider request"):
        prepare_stage3_request(replace(request, max_prompt_bytes=len(prepared.provider_payload) - 1), inputs)


def test_request_mapping_rejects_unknown_or_missing_nested_fields(case) -> None:
    request, _inputs = case
    for key in request.to_mapping():
        changed = request.to_mapping()
        del changed[key]
        with pytest.raises(ValueError):
            BuildEditorialBlueprintRequest.from_mapping(changed)
    for nested in ("stage2_request", "stage2_outcome", "generation", "draft_policy", "context_policy", "feasibility_policy", "retry_policy"):
        changed = request.to_mapping()
        changed[nested]["unexpected"] = True
        with pytest.raises(ValueError):
            BuildEditorialBlueprintRequest.from_mapping(changed)
