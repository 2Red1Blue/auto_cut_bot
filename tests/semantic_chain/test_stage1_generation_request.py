"""Pure BuildNarrativeGraph request compiler tests; no provider dispatch."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from autocut_kernel.pipeline.build_narrative_graph_request import (
    BuildNarrativeGraphRequest,
    Stage1GenerationPolicy,
    prepare_stage1_request,
)
from autocut_kernel.semantic_chain.dependency_projection import DependencyProjectionPolicy
from autocut_kernel.semantic_chain.draft_provider import decode_draft_request_payload
from autocut_kernel.store import CommittedArtifactMemberReference, CommittedSemanticInputsRequest
from autocut_kernel.vlm.retry_policy import GenerationRetryPolicy

from tests.semantic_chain.test_coverage_compiler import COVERAGE
from tests.semantic_chain.test_stage1_draft import POLICY, _synthetic_inputs


def _request():
    inputs = _synthetic_inputs()
    source = inputs.source_manifest
    source_ref = CommittedArtifactMemberReference(
        source.receipt_id, source.artifact_set_id, 0, source.reference.scope,
        source.reference.artifact_type, source.reference.logical_id, source.reference.revision,
        source.reference.content_hash,
    )
    request = BuildNarrativeGraphRequest(
        CommittedSemanticInputsRequest(inputs.source_manifest.source_job, source_ref, inputs.vlm_semantic_pack_set),
        "stage1-generation:test", 1,
        Stage1GenerationPolicy(
            "doubao-ark-text-responses-stream", "test-model", "stage1-v1", "Build the graph.",
            "doubao-ark-text-responses-stream-v1", 1024, "0.5",
        ),
        POLICY, COVERAGE, DependencyProjectionPolicy("semantic-dependencies-v1"),
        GenerationRetryPolicy("generation-retry-v1", 2, (0,)),
    )
    return inputs, request


def test_request_round_trip_and_prepared_wire_are_exact_and_bound():
    inputs, request = _request()
    assert BuildNarrativeGraphRequest.from_mapping(request.to_mapping()) == request
    prepared = prepare_stage1_request(request, inputs)
    body = decode_draft_request_payload(prepared.provider_payload)
    assert body["model"] == request.generation.model_id and body["stream"] is body["store"] is True
    durable = json.loads(prepared.request_payload)
    assert durable["command_request"] == request.to_mapping()
    assert durable["input_binding_sha256"] == prepared.input_binding_sha256
    assert durable["retry_policy_sha256"] == request.retry_policy.canonical_hash
    assert prepared.request_hash.startswith("sha256:")
    assert prepared.provider_idempotency_key_for(1) != prepared.provider_idempotency_key_for(2)
    with pytest.raises(ValueError):
        prepared.provider_idempotency_key_for(3)


@pytest.mark.parametrize("change", ["missing", "extra", "dependency", "scope", "revision", "temperature"])
def test_closed_request_rejects_policy_and_identity_drift(change):
    inputs, request = _request()
    if change == "scope":
        with pytest.raises(ValueError):
            replace(request, inputs=replace(request.inputs, source_manifest=replace(request.inputs.source_manifest, scope=replace(request.artifact_scope, key="foreign"))))
        return
    if change == "revision":
        with pytest.raises(ValueError):
            replace(request, artifact_revision=0)
        return
    if change == "temperature":
        with pytest.raises(ValueError):
            replace(request, generation=replace(request.generation, temperature="0.50"))
        return
    wire = request.to_mapping()
    if change == "missing":
        del wire["generation"]["model_id"]
    elif change == "extra":
        wire["generation"]["caller_default"] = True
    else:
        wire["dependency_policy"] = {"strategy_version": "semantic-dependencies-v1"}
    with pytest.raises(ValueError):
        BuildNarrativeGraphRequest.from_mapping(wire)


def test_prepare_rejects_mismatched_actual_inputs_and_prompt_budget_without_clipping():
    inputs, request = _request()
    with pytest.raises(ValueError):
        prepare_stage1_request(replace(request, inputs=replace(request.inputs, vlm_semantic_pack_set=replace(request.inputs.vlm_semantic_pack_set, content_hash="sha256:" + "b" * 64))), inputs)
    with pytest.raises(ValueError):
        prepare_stage1_request(replace(request, draft_policy=replace(request.draft_policy, max_prompt_bytes=1)), inputs)


@pytest.mark.parametrize("value", ["0", "1", "2", "0.125", "1.999"])
def test_canonical_temperature_including_zero_is_supported(value):
    inputs, request = _request()
    request = replace(request, generation=replace(request.generation, temperature=value))
    assert json.loads(prepare_stage1_request(request, inputs).provider_payload)["temperature"] == float(value)
    assert Stage1GenerationPolicy.from_mapping(request.generation.to_mapping()) == request.generation


@pytest.mark.parametrize("value", ["0.0", "-0", "+0", " 0", "1e-999999999", "NaN", "Infinity", "2.1", "00", "0_1", "0." + "1" * 64, True, 0])
def test_temperature_spelling_and_size_are_checked_before_numeric_expansion(value):
    _, request = _request()
    with pytest.raises(ValueError):
        replace(request.generation, temperature=value)


@pytest.mark.parametrize("value", [(0,), "0", {"0": 0}, None, True])
def test_wire_retry_backoff_cannot_coerce_non_arrays(value):
    _, request = _request()
    wire = request.to_mapping()
    wire["retry_policy"]["backoff_seconds"] = value
    with pytest.raises(ValueError):
        BuildNarrativeGraphRequest.from_mapping(wire)


def test_changed_generation_policy_changes_durable_hash_and_attempt_keys():
    inputs, request = _request()
    first = prepare_stage1_request(request, inputs)
    changed = prepare_stage1_request(replace(request, generation=replace(request.generation, temperature="0")), inputs)
    assert first.input_binding_sha256 == changed.input_binding_sha256
    assert first.request_hash != changed.request_hash
    assert first.provider_idempotency_key_for(1) != changed.provider_idempotency_key_for(1)
