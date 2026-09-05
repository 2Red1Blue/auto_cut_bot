"""Durable derivation and restart readback use no provider object or invocation."""

import hashlib
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from autocut_kernel.pipeline.reprocess_vlm_evidence_command import (
    ReprocessVlmEvidenceCommand, ReprocessVlmEvidenceRequest, rebuild_reprocessed_vlm_evidence,
    read_reprocessed_vlm_evidence,
)
from autocut_kernel.store import CommandClaim, CommandRejection, CommandSuccess, PostgresRuntimeStore
from autocut_kernel.store.models import artifact_set_hash
from autocut_kernel.vlm.normalized_contracts import VLM_PARSER_NORMALIZED_V4, parser_contract_sha256_for
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


@pytest.mark.parametrize("field", ["parent_request_hash", "parent_request_payload_sha256", "parent_raw_response_sha256"])
def test_parent_hash_mismatch_cannot_be_admitted(prepared, field):
    request, _, _ = _failed_parent(prepared)
    wrong = replace(request, **{field: "sha256:" + "0" * 64})
    with pytest.raises(ValueError):
        rebuild_reprocessed_vlm_evidence(prepared.store, wrong)


def test_forged_parent_attempt_and_generic_success_writer_are_rejected(prepared):
    request, _, _ = _failed_parent(prepared)
    with pytest.raises(ValueError):
        rebuild_reprocessed_vlm_evidence(prepared.store, replace(request, parent_attempt_id=uuid4()))
    evidence = rebuild_reprocessed_vlm_evidence(prepared.store, request)
    from autocut_kernel.pipeline.reprocess_vlm_evidence_command import REPROCESS_VLM_COMMAND

    claimed = prepared.store.claim_command(CommandClaim(request.job, request.idempotency_key, REPROCESS_VLM_COMMAND,
                                                         request.request_hash, execution_kind="deterministic"))
    with pytest.raises(ValueError):
        prepared.store.commit_command_success(CommandSuccess(claimed.command_slot_id,
            artifact_set_hash((evidence.artifact,)), (evidence.artifact,)))
