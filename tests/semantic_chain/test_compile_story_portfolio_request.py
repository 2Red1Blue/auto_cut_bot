"""Pure Stage 2 request tests using synthetic media and in-memory Stage 1 IO.

Source/VLM codecs and the actual Stage 1 Command/readers run; no real database,
model or provider is contacted. Preparation itself must perform no IO.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, fields, replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.pipeline.build_narrative_graph_command import BuildNarrativeGraphCommand
from autocut_kernel.pipeline.compile_story_portfolio_request import (
    CompileStoryPortfolioRequest,
    prepare_stage2_request,
)
from autocut_kernel.pipeline.story_design_inputs import read_committed_story_design_inputs
from autocut_kernel.semantic_chain.candidate_catalog import CandidateCatalogPolicy
from autocut_kernel.semantic_chain.draft_provider import (
    MAX_DRAFT_REQUEST_BYTES,
    decode_draft_request_payload,
)
from autocut_kernel.semantic_chain.story_design_command_policy import Stage2CommandPolicy
from autocut_kernel.semantic_chain.story_design_context import story_design_input_binding
from autocut_kernel.semantic_chain.story_design_draft import story_design_draft_response_schema
from autocut_kernel.semantic_chain.story_design_models import SourceConstraints
from autocut_kernel.store.errors import BlobIntegrityError
from autocut_kernel.store.models import BlobRef, Job, artifact_set_hash, canonical_payload_hash
from autocut_kernel.store.postgres import PostgresRuntimeStore
from autocut_kernel.vlm.retry_policy import GenerationRetryPolicy

from tests.semantic_chain.test_build_narrative_graph_command import (
    MemoryNarrativeGraphStore,
    ScriptedDraftProvider,
)
from tests.semantic_chain.test_candidate_projection import _command_request, _draft_raw
from tests.semantic_chain.test_material_support import _long_material_inputs
from tests.semantic_chain.test_story_design_draft import POLICY
from tests.semantic_chain.test_story_design_models import _job_policy, _story_policy


def request_case():
    semantic = _long_material_inputs()
    stage1_request = _command_request(semantic)
    store = MemoryNarrativeGraphStore(semantic)
    provider = ScriptedDraftProvider(_draft_raw(semantic))
    result = BuildNarrativeGraphCommand(store, provider).execute(stage1_request)
    inputs = read_committed_story_design_inputs(store, stage1_request=stage1_request, stage1_outcome=result.outcome)
    policy = Stage2CommandPolicy(
        1, replace(stage1_request.generation, prompt_version="synthetic-stage2-v1",
                   prompt_template="提出完整故事候选。Do not claim acceptance or physical endpoints."),
        512_000, POLICY, CandidateCatalogPolicy("candidate-catalog-v1", "0.5", ("reveal_strength",)),
        replace(_job_policy(), source_constraints=SourceConstraints((), (), "render_source")),
        _story_policy(), GenerationRetryPolicy("generation-retry-v1", 2, (1,)),
    )
    request = policy.build_request(stage1_request, result.outcome, "stage2:test-request")
    return request, inputs, store, provider


@pytest.fixture(scope="module")
def case():
    return request_case()


def test_policy_and_request_closed_roundtrip_without_input_defaults(case):
    request, _, _, _ = case
    policy = request.command_policy
    assert Stage2CommandPolicy.from_mapping(json.loads(canonical_json_bytes(policy.to_mapping()))) == policy
    decoded = CompileStoryPortfolioRequest.from_mapping(json.loads(canonical_json_bytes(request.to_mapping())))
    assert decoded.to_mapping() == request.to_mapping()
    assert decoded.stage1_outcome.job_id == request.stage1_outcome.job_id
    assert policy.build_request(request.stage1_request, request.stage1_outcome, request.idempotency_key).to_mapping() == request.to_mapping()
    assert policy.canonical_hash == "sha256:" + hashlib.sha256(canonical_json_bytes(policy.to_mapping())).hexdigest()
    assert not hasattr(policy, "job") and not hasattr(policy, "inputs")
    assert request.job == request.stage1_request.job
    assert request.artifact_scope == request.stage1_request.artifact_scope
    with pytest.raises(TypeError):
        Stage2CommandPolicy()
    for value in (request, policy):
        with pytest.raises(FrozenInstanceError):
            value.artifact_revision = 2
        for key in value.to_mapping():
            mapping = value.to_mapping()
            del mapping[key]
            with pytest.raises(ValueError):
                type(value).from_mapping(mapping)
        with pytest.raises(ValueError):
            type(value).from_mapping({**value.to_mapping(), "accepted": True})
    mutated = policy.to_mapping()
    mutated["generation"]["prompt_template"] = "changed"
    assert policy.generation.prompt_template != "changed"


def test_complete_prompt_and_durable_request_have_independent_hash_oracles(case):
    request, inputs, _, _ = case
    prepared = prepare_stage2_request(request, inputs)
    assert prepared == prepare_stage2_request(request, inputs)
    body = decode_draft_request_payload(prepared.provider_payload)
    prompt = body["input"][0]["content"][0]["text"]
    assert prompt.startswith(request.generation.prompt_template + "\n\n")
    context = json.loads(prompt.split("\n\n", 1)[1])
    values = inputs.narrative.values.coverage
    for kind, value in (("event_card_set", values.event_cards), ("episode_digest_set", values.episode_digests),
                        ("narrative_graph", values.narrative_graph)):
        assert context[kind] == {"member_ref": values.identity(kind).to_mapping(), "payload": value.to_mapping()}
    assert context["candidate_catalog"]["payload"] == prepared.projection.catalog.to_mapping()
    assert context["source_grant"] == inputs.semantic.source_grant.to_mapping()
    assert context["stage1_members"] == [ref.to_mapping() for ref in inputs.narrative.record.references]
    assert context["policies"] == {"candidate_policy": request.candidate_policy.to_mapping(),
                                   "job_policy": request.job_policy.to_mapping(), "story_policy": request.story_policy.to_mapping()}
    assert body["text"]["format"]["json_schema"]["schema"] == story_design_draft_response_schema(request.draft_policy)
    assert body["model"] == request.generation.model_id
    assert body["stream"] is body["store"] is True
    envelope = json.loads(prepared.request_payload)
    assert envelope["command_request"] == request.to_mapping()
    assert envelope["provider_request_json"].encode() == prepared.provider_payload
    assert envelope["provider_request_sha256"] == "sha256:" + hashlib.sha256(prepared.provider_payload).hexdigest()
    assert prepared.request_hash == "sha256:" + hashlib.sha256(prepared.request_payload).hexdigest()
    assert envelope["stage1_request_sha256"] == inputs.narrative.record.request_hash
    assert envelope["response_schema_sha256"] == canonical_json_hash(story_design_draft_response_schema(request.draft_policy))
    assert envelope["retry_policy"] == request.retry_policy.to_mapping()
    assert envelope["retry_policy_sha256"] == request.retry_policy.canonical_hash
    assert prepared.input_binding_sha256 == context["input_binding_sha256"] == story_design_input_binding(
        inputs.narrative.values, prepared.projection, job_policy=request.job_policy,
        story_policy=request.story_policy, candidate_policy=request.candidate_policy,
    )
    assert b"\xe6\x8f\x90" in prepared.provider_payload


@pytest.mark.parametrize("field", ["artifact_revision", "generation", "max_prompt_bytes", "draft_policy",
                                  "candidate_policy", "job_policy", "story_policy", "retry_policy", "idempotency_key"])
def test_every_frozen_policy_component_changes_request_identity(case, field):
    request, inputs, _, _ = case
    changes = {
        "artifact_revision": 2,
        "generation": replace(request.generation, prompt_template=request.generation.prompt_template + "!"),
        "max_prompt_bytes": request.max_prompt_bytes + 1,
        "draft_policy": replace(request.draft_policy, max_response_bytes=request.draft_policy.max_response_bytes + 1),
        "candidate_policy": replace(request.candidate_policy, minimum_confidence="0.6"),
        "job_policy": replace(request.job_policy, max_search_states=request.job_policy.max_search_states + 1),
        "story_policy": replace(request.story_policy, policy_version="2.0.0"),
        "retry_policy": GenerationRetryPolicy("generation-retry-v1", 3, (1, 2)),
        "idempotency_key": "stage2:another-key",
    }
    kwargs = {field: changes[field]}
    if field == "story_policy":
        kwargs["job_policy"] = replace(request.job_policy, story_design_policy_sha256=changes[field].canonical_hash)
    original = prepare_stage2_request(request, inputs)
    changed = prepare_stage2_request(replace(request, **kwargs), inputs)
    assert original.request_hash != changed.request_hash
    assert original.provider_idempotency_key_for(1) != changed.provider_idempotency_key_for(1)


@pytest.mark.parametrize("field", ["model_id", "prompt_version", "adapter_strategy_version", "max_output_tokens", "temperature"])
def test_generation_policy_is_exact_and_hash_bound(case, field):
    request, inputs, _, _ = case
    value = {"model_id": "another-test-model", "prompt_version": "2", "adapter_strategy_version": "unregistered",
             "max_output_tokens": request.generation.max_output_tokens + 1, "temperature": "0"}[field]
    if field == "adapter_strategy_version":
        with pytest.raises(ValueError):
            replace(request.generation, **{field: value})
    else:
        changed = replace(request, generation=replace(request.generation, **{field: value}))
        assert prepare_stage2_request(changed, inputs).request_hash != prepare_stage2_request(request, inputs).request_hash


@pytest.mark.parametrize("bad", [True, 1.0, 0, -1, "1", None, 2**53])
def test_integer_policy_and_attempt_bounds_are_strict(case, bad):
    request, inputs, _, _ = case
    for field in ("artifact_revision", "max_prompt_bytes"):
        with pytest.raises(ValueError):
            replace(request, **{field: bad})
    with pytest.raises(ValueError):
        prepare_stage2_request(request, inputs).provider_idempotency_key_for(bad)


def test_provider_key_uses_distinct_stage2_namespace_and_exact_ordinal(case):
    request, inputs, _, _ = case
    prepared = prepare_stage2_request(request, inputs)
    for ordinal in (1, 2):
        assert prepared.provider_idempotency_key_for(ordinal) == canonical_json_hash({
            "command": "CompileStoryPortfolio", "job_key": request.job.job_key,
            "idempotency_key": request.idempotency_key, "request_hash": prepared.request_hash,
            "attempt_ordinal": ordinal,
        })
    assert prepared.provider_idempotency_key_for(1) != prepared.provider_idempotency_key_for(2)
    with pytest.raises(ValueError):
        prepared.provider_idempotency_key_for(3)


def test_budget_applies_to_whole_provider_body_with_no_clipping(case):
    request, inputs, _, _ = case
    prepared = prepare_stage2_request(request, inputs)
    size = len(prepared.provider_payload)
    exact = prepare_stage2_request(replace(request, max_prompt_bytes=size), inputs)
    assert exact.provider_payload == prepared.provider_payload
    with pytest.raises(ValueError, match="complete provider request"):
        prepare_stage2_request(replace(request, max_prompt_bytes=size - 1), inputs)
    with pytest.raises(ValueError):
        replace(request, max_prompt_bytes=MAX_DRAFT_REQUEST_BYTES + 1)
    large = replace(request, generation=replace(request.generation, prompt_template="界" * request.max_prompt_bytes))
    with pytest.raises(ValueError, match="complete provider request"):
        prepare_stage2_request(large, inputs)


@pytest.mark.parametrize("field", ["job_id", "command_slot_id", "receipt_id", "artifact_set_id"])
def test_outcome_full_identity_is_not_dataclass_equality(case, field):
    request, inputs, _, _ = case
    changed = replace(request.stage1_outcome, **{field: UUID(int=999)})
    if field == "job_id":
        assert changed == request.stage1_outcome  # Existing Store equality explicitly ignores job_id.
    with pytest.raises(ValueError, match="exact Stage 1 outcome"):
        prepare_stage2_request(replace(request, stage1_outcome=changed), inputs)
    for bad in (None, str(UUID(int=999)), True, 3.0):
        with pytest.raises(ValueError, match="exact succeeded"):
            replace(request, stage1_outcome=replace(request.stage1_outcome, **{field: bad}))


@pytest.mark.parametrize("changes", [{"state": "running"}, {"state": "denied"}, {"state": "failed"},
                                     {"failure_code": "DENIED"}, {"failure_detail_json": "{}"}, {"is_fresh_claim": 1}])
def test_unsuccessful_or_inconsistent_outcome_is_not_prepared(case, changes):
    request = case[0]
    with pytest.raises(ValueError, match="exact succeeded"):
        replace(request, stage1_outcome=replace(request.stage1_outcome, **changes))


def test_freshness_and_json_mapping_order_do_not_change_durable_identity(case):
    request, inputs, _, _ = case
    replay = replace(request, stage1_outcome=replace(request.stage1_outcome, is_fresh_claim=not request.stage1_outcome.is_fresh_claim))
    assert prepare_stage2_request(replay, inputs).request_payload == prepare_stage2_request(request, inputs).request_payload
    mapping = dict(reversed(tuple(request.to_mapping().items())))
    assert prepare_stage2_request(CompileStoryPortfolioRequest.from_mapping(mapping), inputs).request_payload == prepare_stage2_request(request, inputs).request_payload


@pytest.mark.parametrize("field", ["job", "job_id", "request_hash", "command_name", "execution_kind"])
def test_record_identity_or_command_substitution_cannot_be_prepared(case, field):
    request, inputs, _, _ = case
    record = inputs.narrative.record
    value = {"job": Job("foreign-job", request.job.profile), "job_id": UUID(int=999),
             "request_hash": "sha256:" + "f" * 64, "command_name": "Other@2.1.3", "execution_kind": "deterministic"}[field]
    changed = replace(inputs, narrative=replace(inputs.narrative, record=replace(record, **{field: value})))
    with pytest.raises(ValueError, match="exact Stage 1 outcome"):
        prepare_stage2_request(request, changed)


def test_stage1_policy_and_exact_semantic_inputs_cannot_be_substituted(case):
    request, inputs, _, _ = case
    stage1 = replace(request.stage1_request, generation=replace(request.stage1_request.generation, prompt_version="other"))
    with pytest.raises(ValueError, match="exact Stage 1 outcome"):
        prepare_stage2_request(replace(request, stage1_request=stage1), inputs)
    semantic = replace(inputs.semantic, vlm_semantic_pack_set=replace(inputs.semantic.vlm_semantic_pack_set, content_hash="sha256:" + "f" * 64))
    with pytest.raises(ValueError, match="exactly bind request"):
        prepare_stage2_request(request, replace(inputs, semantic=semantic))
    with pytest.raises(ValueError):
        prepare_stage2_request({}, inputs)
    with pytest.raises(ValueError):
        prepare_stage2_request(request, {})


def test_nested_policy_outcome_and_request_extra_fields_are_closed(case):
    request = case[0]
    for key in ("stage1_outcome", "generation", "draft_policy", "candidate_policy", "job_policy", "story_policy", "retry_policy"):
        mapping = request.to_mapping()
        mapping[key]["unexpected"] = "not allowed"
        with pytest.raises(ValueError):
            CompileStoryPortfolioRequest.from_mapping(mapping)
    mapping = request.to_mapping()
    mapping["stage1_outcome"]["job_id"] = UUID(int=999)
    with pytest.raises(ValueError):
        CompileStoryPortfolioRequest.from_mapping(mapping)
    for bad in ((1,), "1", None, {"0": 1}):
        mapping = request.to_mapping()
        mapping["retry_policy"]["backoff_seconds"] = bad
        with pytest.raises(ValueError):
            CompileStoryPortfolioRequest.from_mapping(mapping)
    for field in fields(Stage2CommandPolicy):
        if field.name not in {"artifact_revision", "max_prompt_bytes"}:
            with pytest.raises(ValueError):
                replace(request.command_policy, **{field.name: {}})


def test_preparation_does_not_read_write_dispatch_or_execute_stage1(case, monkeypatch):
    request, inputs, store, provider = case

    def forbidden(*args, **kwargs):
        raise AssertionError("pure preparation performed IO")

    for name in ("read_committed_semantic_inputs", "claim_command", "put_immutable_blob", "read_immutable_blob", "commit_generation_success"):
        monkeypatch.setattr(store, name, forbidden)
    monkeypatch.setattr(provider, "dispatch", forbidden)
    monkeypatch.setattr(BuildNarrativeGraphCommand, "execute", forbidden)
    assert prepare_stage2_request(request, inputs).projection.catalog.candidates


def test_durable_envelope_is_consumable_by_actual_store_retry_reader_without_database(case):
    request, inputs, _, _ = case
    prepared = prepare_stage2_request(request, inputs)
    blob = BlobRef(UUID(int=888), prepared.request_hash, len(prepared.request_payload), "application/json")
    previous = replace(inputs.narrative.attempts[0], request_payload=blob,
                       retry_policy_hash=request.retry_policy.canonical_hash,
                       max_attempts=request.retry_policy.max_attempts)
    cursor = SimpleNamespace(execute=Mock(), fetchone=Mock(return_value=(prepared.request_payload,)))
    assert PostgresRuntimeStore._generation_retry_backoff_seconds(cursor, previous) == 1
    cursor.execute.assert_called_once_with(
        "SELECT content_bytes FROM storage.blob_objects WHERE object_id = %s", (blob.object_id,),
    )
    for missing in ("retry_policy", "retry_policy_sha256"):
        envelope = json.loads(prepared.request_payload)
        del envelope[missing]
        cursor.fetchone.return_value = (canonical_json_bytes(envelope),)
        with pytest.raises(BlobIntegrityError):
            PostgresRuntimeStore._generation_retry_backoff_seconds(cursor, previous)


@pytest.mark.parametrize("field", ["artifact_type", "logical_id", "revision"])
def test_rehashed_record_references_must_match_actual_stage1_members(case, field):
    request, inputs, _, _ = case
    record = inputs.narrative.record
    first, *rest = record.members
    value = 2 if field == "revision" else "foreign_member"
    first = replace(first, reference=replace(first.reference, **{field: value}))
    artifacts = tuple(replace(artifact, **{field: value}) if index == 0 else artifact
                      for index, artifact in enumerate(record.artifacts))
    changed_record = replace(record, members=(first, *rest), set_hash=artifact_set_hash(artifacts))
    changed = replace(inputs, narrative=replace(inputs.narrative, record=changed_record))
    with pytest.raises(ValueError, match="exact Stage 1 outcome"):
        prepare_stage2_request(request, changed)


def test_job_policy_hash_and_nested_unsafe_retry_integer_are_rejected(case):
    request = case[0]
    with pytest.raises(ValueError, match="does not bind"):
        replace(request, job_policy=replace(request.job_policy, story_design_policy_sha256="sha256:" + "e" * 64))
    mapping = request.to_mapping()
    mapping["retry_policy"]["backoff_seconds"] = [2**53]
    with pytest.raises(ValueError):
        CompileStoryPortfolioRequest.from_mapping(mapping)


def test_matching_foreign_record_and_outcome_job_ids_cannot_replace_source_job(case):
    request, inputs, _, _ = case
    foreign = UUID(int=999)
    request = replace(request, stage1_outcome=replace(request.stage1_outcome, job_id=foreign))
    inputs = replace(inputs, narrative=replace(inputs.narrative, record=replace(inputs.narrative.record, job_id=foreign)))
    with pytest.raises(ValueError, match="exact Stage 1 outcome"):
        prepare_stage2_request(request, inputs)


def test_rehashed_record_payload_cannot_reuse_stale_decoded_stage1_values(case):
    request, inputs, _, _ = case
    record = inputs.narrative.record
    first, *rest = record.members
    content_hash = canonical_payload_hash("{}")
    first = replace(first, payload_json="{}", reference=replace(first.reference, content_hash=content_hash))
    artifacts = (replace(record.artifacts[0], payload_json="{}", content_hash=content_hash), *record.artifacts[1:])
    record = replace(record, members=(first, *rest), set_hash=artifact_set_hash(artifacts))
    with pytest.raises(ValueError, match="exact Stage 1 outcome"):
        prepare_stage2_request(request, replace(inputs, narrative=replace(inputs.narrative, record=record)))
