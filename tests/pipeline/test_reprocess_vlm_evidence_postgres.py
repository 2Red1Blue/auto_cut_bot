"""Durable derivation and restart readback use no provider object or invocation."""

# Imported pytest fixture names intentionally match test parameters.
# ruff: noqa: F811

import hashlib
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from autocut_kernel.pipeline.reprocess_vlm_evidence_command import (
    ReprocessVlmEvidenceCommand,
    ReprocessVlmEvidenceRequest,
    rebuild_reprocessed_vlm_evidence,
)
from autocut_kernel.store import (
    CommandClaim,
    CommandRejection,
    CommandSuccess,
    PostgresRuntimeStore,
)
from autocut_kernel.store.errors import (
    CommandStateError,
    SemanticInputUnavailableError,
    StoreValidationError,
)
from autocut_kernel.store.models import artifact_set_hash
from autocut_kernel.vlm.normalized_contracts import (
    VLM_PARSER_NORMALIZED_V4,
    parser_contract_sha256_for,
)

from tests.pipeline.test_vlm_v4_store_postgres import DSN, prepared, psycopg  # noqa: F401
from tests.vlm.test_semantic_pack_v4 import _wire

pytestmark = pytest.mark.skipif(not DSN, reason="requires explicit disposable PostgreSQL")


def _blob(store, job, raw):
    return store.put_immutable_blob(job, content=raw, content_hash="sha256:" + hashlib.sha256(raw).hexdigest(),
                                    media_type="application/json")


def _failed_parent(prepared, *, unknown_ref=False):
    store, original = prepared.store, prepared.request
    outcome = store.claim_command(CommandClaim(original.job, original.idempotency_key, "GenerateVlmEvidenceCommand",
                                                original.request_hash, execution_kind="generation"))
    attempt = store.reserve_generation_attempt(
        outcome.command_slot_id, original.request_hash, provider_id=original.provider_id,
        provider_idempotency_key=original.provider_idempotency_key,
        request_payload=_blob(store, original.job, original.request_payload),
        retry_policy_hash=original.retry_policy.canonical_hash, max_attempts=1,
    )
    attempt = store.dispatch_generation_attempt(attempt.attempt_id, expected_version=attempt.version)
    wire = _wire()
    wire["candidate_hypotheses"][0]["tags"] = ["reveal", "dialogue"]
    if unknown_ref:
        wire["events"][0]["fact_refs"] = ["missing"]
    raw = json.dumps(wire).encode()
    attempt = store.record_generation_response(
        attempt.attempt_id, expected_version=attempt.version, raw_response=_blob(store, original.job, raw),
        dispatch_lease_token=attempt.dispatch_lease_token, provider_request_id="already-paid-fixture",
    )
    detail = {"reason_code": "NONCANONICAL_ENUM_SET", "parser_message": "NONCANONICAL_ENUM_SET: tags order"}
    attempt = store.fail_generation_attempt(attempt.attempt_id, expected_version=attempt.version,
                                            failure_code="NONCANONICAL_ENUM_SET", failure_detail_json=json.dumps(detail),
                                            failure_disposition="retryable")
    terminal = store.commit_generation_rejection(
        attempt.attempt_id, expected_version=attempt.version,
        rejection=CommandRejection(outcome.command_slot_id, "RETRY_BUDGET_EXHAUSTED",
                                   json.dumps({"attempts": [{"attempt_id": str(attempt.attempt_id)}]}), outcome="failed"),
    )
    request = ReprocessVlmEvidenceRequest(
        original.job, outcome.command_slot_id, terminal.receipt_id, attempt.attempt_id,
        original.request_hash, attempt.request_payload.content_hash, attempt.raw_response.content_hash,
        prepared.source_reference.artifact_set_id, original.episode_index, original.artifact_revision,
        parser_contract_sha256_for(VLM_PARSER_NORMALIZED_V4),
    )
    return request, attempt, terminal


def test_reprocess_is_durable_idempotent_and_keeps_original_failure(prepared):
    request, parent, terminal = _failed_parent(prepared)
    result = ReprocessVlmEvidenceCommand(prepared.store).execute(request)
    assert result.outcome.state == "succeeded"
    assert result.evidence.semantic_pack.raw_response_sha256 == request.parent_raw_response_sha256
    assert result.evidence.normalization.transformations[0].path == "$.candidate_hypotheses[0].tags"
    restarted = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    replay = ReprocessVlmEvidenceCommand(restarted).execute(request)
    assert replay.outcome.receipt_id == result.outcome.receipt_id
    assert replay.evidence == result.evidence
    assert restarted.read_generation_attempt(parent.attempt_id) == parent
    original = restarted.read_terminal_command_receipt(
        request.job, command_slot_id=request.parent_command_slot_id, receipt_id=terminal.receipt_id,
        expected_request_hash=request.parent_request_hash, expected_command_name="GenerateVlmEvidenceCommand",
        expected_execution_kind="generation", max_failure_detail_bytes=10000,
    )
    assert original.outcome == "failed"


def test_remaining_reference_error_commits_new_denial_without_generation(prepared):
    request, parent, _ = _failed_parent(prepared, unknown_ref=True)
    result = ReprocessVlmEvidenceCommand(prepared.store).execute(request)
    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == "UNKNOWN_REFERENCE"
    assert prepared.store.read_generation_attempt(parent.attempt_id) == parent


def test_v1_replay_and_explicit_v2_projection_use_distinct_durable_receipts(prepared):
    from autocut_kernel.pipeline.reprocess_vlm_evidence_command import project_reprocessed_semantic_input
    from autocut_kernel.store.models import canonical_payload_hash

    request, parent, _ = _failed_parent(prepared)
    v1 = replace(request, projection_version=1)
    old = ReprocessVlmEvidenceCommand(prepared.store).execute(v1)
    old_mapping = json.loads(old.evidence.artifact.payload_json)
    assert old_mapping["schema_version"] == "reprocessed-vlm-evidence-v1"
    assert not {"request_identity", "parse_policy", "proxy_blob"} & old_mapping.keys()
    restarted = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    restored_v1 = ReprocessVlmEvidenceRequest.from_mapping(old_mapping["request"])
    replay = ReprocessVlmEvidenceCommand(restarted).execute(restored_v1)
    assert replay.outcome.receipt_id == old.outcome.receipt_id and replay.evidence.artifacts == old.evidence.artifacts
    assert project_reprocessed_semantic_input(restarted, restored_v1, replay.outcome).request_identity == old.evidence.request_identity

    new = ReprocessVlmEvidenceCommand(restarted).execute(request)
    assert new.outcome.receipt_id != old.outcome.receipt_id
    assert new.outcome.command_slot_id != old.outcome.command_slot_id
    assert new.outcome.artifact_set_id != old.outcome.artifact_set_id
    payload = json.loads(new.evidence.artifact.payload_json)
    frozen = json.loads(prepared.request.request_payload)
    assert payload["schema_version"] == "reprocessed-vlm-evidence-v2"
    assert payload["request_identity"] == old.evidence.request_identity.to_mapping()
    assert payload["parse_policy"] == frozen["parse_policy"]
    assert payload["proxy_blob"] == frozen["proxy_blob"]
    assert new.evidence.semantic_pack == old.evidence.semantic_pack
    projected = project_reprocessed_semantic_input(restarted, request, new.outcome)
    assert projected.request_identity == old.evidence.request_identity
    for field in ("request_identity", "parse_policy", "proxy_blob"):
        tampered = json.dumps({**payload, field: {}}, sort_keys=True, separators=(",", ":"))
        child = projected.semantic_pack.source_child
        with pytest.raises(StoreValidationError):
            replace(child, payload_json=tampered,
                    reference=replace(child.reference, content_hash=canonical_payload_hash(tampered)))
    assert restarted.read_generation_attempt(parent.attempt_id) == parent
    assert ReprocessVlmEvidenceCommand(restarted).execute(restored_v1).evidence.artifacts == old.evidence.artifacts


@pytest.mark.parametrize("field", ["parent_request_hash", "parent_request_payload_sha256", "parent_raw_response_sha256"])
def test_parent_hash_mismatch_cannot_be_admitted(prepared, field):
    request, _, _ = _failed_parent(prepared)
    wrong = replace(request, **{field: "sha256:" + "0" * 64})
    with pytest.raises((ValueError, StoreValidationError, SemanticInputUnavailableError)):
        rebuild_reprocessed_vlm_evidence(prepared.store, wrong)
    with pytest.raises((ValueError, StoreValidationError, SemanticInputUnavailableError)):
        ReprocessVlmEvidenceCommand(prepared.store).execute(wrong)
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM runtime.command_slots WHERE idempotency_key = %s", (wrong.idempotency_key,))
        assert cursor.fetchone()[0] == 0


def test_forged_parent_attempt_and_generic_success_writer_are_rejected(prepared):
    request, _, _ = _failed_parent(prepared)
    with pytest.raises(StoreValidationError):
        rebuild_reprocessed_vlm_evidence(prepared.store, replace(request, parent_attempt_id=uuid4()))
    wrong_owner = replace(request, parent_attempt_id=uuid4())
    with pytest.raises(StoreValidationError):
        ReprocessVlmEvidenceCommand(prepared.store).execute(wrong_owner)
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM runtime.command_slots WHERE idempotency_key = %s", (wrong_owner.idempotency_key,))
        assert cursor.fetchone()[0] == 0
    evidence = rebuild_reprocessed_vlm_evidence(prepared.store, request)
    from autocut_kernel.pipeline.reprocess_vlm_evidence_command import REPROCESS_VLM_COMMAND

    claimed = prepared.store.claim_command(CommandClaim(request.job, request.idempotency_key, REPROCESS_VLM_COMMAND,
                                                         request.request_hash, execution_kind="deterministic"))
    with pytest.raises(CommandStateError):
        prepared.store.commit_command_success(CommandSuccess(claimed.command_slot_id,
            artifact_set_hash((evidence.artifact,)), (evidence.artifact,)))


def test_complete_derived_batch_reenters_normal_stage1_reader_after_restart(prepared):
    from autocut_kernel.pipeline.reprocess_vlm_batch_command import (
        DERIVED_VLM_BATCH_STRATEGY,
        FinalizeDerivedVlmBatchCommand,
        FinalizeDerivedVlmBatchRequest,
        VlmBatchEvidenceSelection,
        rebuild_derived_vlm_batch,
    )
    from autocut_kernel.semantic_chain.core_observations import semantic_pack
    from autocut_kernel.semantic_chain.stage1_draft import stage1_draft_prompt_inputs
    from autocut_kernel.store.models import (
        CommittedArtifactMemberReference,
        CommittedSemanticInputsRequest,
        PersistedReprocessedVlmChild,
    )

    from tests.semantic_chain.test_stage1_draft import POLICY

    request, parent, _ = _failed_parent(prepared)
    derived = ReprocessVlmEvidenceCommand(prepared.store).execute(request)
    artifact = derived.evidence.artifact
    reference = CommittedArtifactMemberReference(derived.outcome.receipt_id, derived.outcome.artifact_set_id, 0,
        artifact.scope, artifact.artifact_type, artifact.logical_id, artifact.revision, artifact.content_hash)
    batch = FinalizeDerivedVlmBatchRequest(request.job, prepared.source_reference,
        (VlmBatchEvidenceSelection(0, request.idempotency_key, reference),))
    outcome = FinalizeDerivedVlmBatchCommand(prepared.store).execute(batch)
    assert outcome.state == "succeeded"
    aggregate, *_ = rebuild_derived_vlm_batch(prepared.store, batch)
    aggregate_ref = CommittedArtifactMemberReference(outcome.receipt_id, outcome.artifact_set_id, 0,
        aggregate.scope, aggregate.artifact_type, aggregate.logical_id, aggregate.revision, aggregate.content_hash)
    restarted = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    inputs = restarted.read_committed_semantic_inputs(CommittedSemanticInputsRequest(request.job, prepared.source_reference, aggregate_ref))
    assert inputs.vlm_batch_strategy_version == DERIVED_VLM_BATCH_STRATEGY
    assert type(inputs.inputs[0].semantic_pack.source_child) is PersistedReprocessedVlmChild
    assert semantic_pack(inputs.inputs[0]) == derived.evidence.semantic_pack
    assert stage1_draft_prompt_inputs(inputs, policy=POLICY)["input_binding_sha256"]
    assert restarted.read_generation_attempt(parent.attempt_id) == parent
    assert FinalizeDerivedVlmBatchCommand(restarted).execute(batch).receipt_id == outcome.receipt_id


def test_new_normalized_generation_reader_replays_derived_metadata(prepared):
    from autocut_kernel.pipeline import GenerateVlmEvidenceCommand

    from tests.pipeline.test_vlm_v4_store_postgres import FixtureProvider, NoProvider

    request = replace(prepared.request, parser_strategy_version=VLM_PARSER_NORMALIZED_V4,
                      parser_contract_sha256=parser_contract_sha256_for(VLM_PARSER_NORMALIZED_V4))
    wire = _wire()
    wire["candidate_hypotheses"][0]["tags"] = ["reveal", "dialogue"]
    provider = FixtureProvider(json.dumps(wire).encode())
    generated = GenerateVlmEvidenceCommand(prepared.store, provider).execute(request)
    assert generated.outcome.state == "succeeded" and provider.calls == 1
    inspected = prepared.store.read_committed_v4_semantic_child_inspection(request.job, request.idempotency_key)
    assert inspected.semantic_input.semantic_pack.semantic_pack == generated.semantic_pack
    response = json.loads(inspected.semantic_input.response_payload_json)
    assert response["normalization"]["transformations"][0]["path"] == "$.candidate_hypotheses[0].tags"
    assert GenerateVlmEvidenceCommand(prepared.store, NoProvider()).execute(request).outcome.receipt_id == generated.outcome.receipt_id
