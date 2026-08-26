"""Durable Stage 1 generation, independent evaluation and exact-set replay.

Only this boundary adds KC-IN-001 after actual Store reads. Providers return raw
draft bytes; they never submit Graphs or Admissions. Replays read the committed
eight-member set, not freshly compiled substitutes for persisted references.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash, sha256_bytes
from ..semantic_chain.coverage_admission import CoverageAdmission
from ..semantic_chain.coverage_compiler import compile_stage1_coverage
from ..semantic_chain.dependency_proof import build_dependency_proof
from ..semantic_chain.draft_provider import DraftDispatchRequest, DraftProviderPort
from ..semantic_chain.member_refs import SemanticMemberIdentity
from ..semantic_chain.stage1_checks import Stage1Check
from ..semantic_chain.stage1_draft import decode_stage1_draft
from ..semantic_chain.stage1_evaluation import evaluate_stage1_business_members
from ..semantic_chain.stage1_result import Stage1Values, decode_stage1_members
from ..store.models import (
    ArtifactMember,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    CommittedSemanticInputs,
    CommittedSemanticInputsRequest,
    GenerationAttempt,
    Job,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
)
from ..vlm.provider_port import (
    ProviderCompleted,
    ProviderFailed,
    ProviderIndeterminate,
    ProviderPending,
    ProviderReconcileQuery,
)
from .build_narrative_graph_request import (
    BuildNarrativeGraphRequest,
    PreparedStage1Request,
    prepare_stage1_request,
)
from .generate_vlm_evidence_command import GenerationStore

COMMAND_NAME = "BuildNarrativeGraphCommand"


class NarrativeGraphStore(GenerationStore, Protocol):
    def read_committed_semantic_inputs(self, request: CommittedSemanticInputsRequest) -> CommittedSemanticInputs: ...

    def read_committed_artifact_set(
        self, job: Job, *, command_slot_id: UUID, receipt_id: UUID, artifact_set_id: UUID,
        expected_request_hash: str, expected_command_name: str, expected_execution_kind: str,
    ) -> PersistedCommittedArtifactSet: ...

    def read_committed_generation_attempt_chain(
        self, job: Job, *, command_slot_id: UUID, receipt_id: UUID, artifact_set_id: UUID,
        expected_request_hash: str,
    ) -> tuple[GenerationAttempt, ...]: ...


@dataclass(frozen=True, slots=True)
class PersistedNarrativeGraphSet:
    record: PersistedCommittedArtifactSet
    values: Stage1Values
    attempts: tuple[GenerationAttempt, ...]


@dataclass(frozen=True, slots=True)
class BuildNarrativeGraphResult:
    outcome: CommandOutcome
    attempt: GenerationAttempt | None = None
    committed: PersistedNarrativeGraphSet | None = None


def _assert_attempt(
    prepared: PreparedStage1Request, outcome: CommandOutcome, attempt: GenerationAttempt,
) -> None:
    request = prepared.request
    if (
        outcome.job_id is None or attempt.job_id != outcome.job_id
        or attempt.command_slot_id != outcome.command_slot_id
        or attempt.request_hash != prepared.request_hash
        or attempt.provider_id != request.generation.provider_id
        or attempt.provider_idempotency_key != prepared.provider_idempotency_key_for(attempt.attempt_ordinal)
        or attempt.request_payload.content_hash != sha256_bytes(prepared.request_payload)
        or attempt.request_payload.byte_length != len(prepared.request_payload)
        or attempt.request_payload.media_type != "application/json"
        or attempt.retry_policy_hash != request.retry_policy.canonical_hash
        or attempt.max_attempts != request.retry_policy.max_attempts
        or attempt.retry_backoff_seconds != (
            0 if attempt.attempt_ordinal == 1
            else request.retry_policy.backoff_after(attempt.attempt_ordinal - 1)
        )
    ):
        raise ValueError("Stage 1 Attempt differs from its exact Job/Command/request/policy")


def _read_request_bytes(store: NarrativeGraphStore, prepared: PreparedStage1Request, attempt: GenerationAttempt) -> None:
    if store.read_immutable_blob(prepared.request.job, attempt.request_payload) != prepared.request_payload:
        raise ValueError("Stage 1 durable request differs from frozen semantic input/policy")


def read_committed_narrative_graph(
    store: NarrativeGraphStore, request: BuildNarrativeGraphRequest, outcome: CommandOutcome,
) -> PersistedNarrativeGraphSet:
    """Exact replay and downstream read; never infer a missing member hash/ID."""
    if (type(outcome) is not CommandOutcome or outcome.state != "succeeded"  # noqa: E721
            or outcome.receipt_id is None or outcome.artifact_set_id is None):
        raise ValueError("Stage 1 reader requires an exact succeeded Receipt/ArtifactSet")
    inputs = store.read_committed_semantic_inputs(request.inputs)
    prepared = prepare_stage1_request(request, inputs)
    record = store.read_committed_artifact_set(
        request.job, command_slot_id=outcome.command_slot_id, receipt_id=outcome.receipt_id,
        artifact_set_id=outcome.artifact_set_id, expected_request_hash=prepared.request_hash,
        expected_command_name=COMMAND_NAME, expected_execution_kind="generation",
    )
    if record.job_id != outcome.job_id:
        raise ValueError("Stage 1 outcome Job differs from committed result")
    chain = store.read_committed_generation_attempt_chain(
        request.job, command_slot_id=outcome.command_slot_id, receipt_id=outcome.receipt_id,
        artifact_set_id=outcome.artifact_set_id, expected_request_hash=prepared.request_hash,
    )
    if not chain or chain[-1].state != "committed":
        raise ValueError("Stage 1 has no committed generation audit")
    for attempt in chain:
        _assert_attempt(prepared, outcome, attempt)
        _read_request_bytes(store, prepared, attempt)
    final = chain[-1]
    if (final.receipt_id != outcome.receipt_id or final.artifact_set_id != outcome.artifact_set_id
            or final.raw_response is None or final.raw_response.media_type != "application/json"):
        raise ValueError("Stage 1 raw response is not bound to this committed result")
    raw = store.read_immutable_blob(request.job, final.raw_response)
    values = decode_stage1_members(record.artifacts, scope=request.artifact_scope)
    draft = decode_stage1_draft(raw, inputs=inputs, policy=request.draft_policy)
    checks = evaluate_stage1_business_members(
        inputs, raw, members=record.artifacts[:7], draft_policy=request.draft_policy,
        coverage_policy=request.coverage_policy, dependency_policy=request.dependency_policy,
    )
    expected_checks = tuple(sorted((*checks, Stage1Check("KC-IN-001", "pass", ())), key=lambda item: item.rule_id))
    admission = values.admission
    if (
        admission.rule_results != expected_checks or admission.next_action != "continue"
        or admission.input_binding_sha256 != prepared.input_binding_sha256
        or admission.raw_draft_sha256 != sha256_bytes(raw)
        or admission.canonical_draft_sha256 != draft.canonical_hash
        or admission.draft_policy_sha256 != request.draft_policy.canonical_hash
        or admission.coverage_policy_sha256 != request.coverage_policy.canonical_hash
        or admission.dependency_policy_sha256 != request.dependency_policy.canonical_hash
        or any(member.revision != request.artifact_revision for member in record.artifacts)
    ):
        raise ValueError("Stage 1 committed Admission differs from independent audited evaluation")
    return PersistedNarrativeGraphSet(record, values, chain)


class BuildNarrativeGraphCommand:
    def __init__(self, store: NarrativeGraphStore, provider: DraftProviderPort) -> None:
        self._store, self._provider = store, provider

    def execute(self, request: BuildNarrativeGraphRequest) -> BuildNarrativeGraphResult:
        inputs = self._store.read_committed_semantic_inputs(request.inputs)
        prepared = prepare_stage1_request(request, inputs)
        outcome = self._store.claim_command(CommandClaim(
            request.job, request.idempotency_key, COMMAND_NAME, prepared.request_hash,
            execution_kind="generation",
        ))
        if outcome.state in ("failed", "denied"):
            return BuildNarrativeGraphResult(outcome)
        if outcome.state == "succeeded":
            return self._replay(request, outcome)
        if self._provider.strategy_version != request.generation.adapter_strategy_version:
            raise ValueError("Stage 1 provider strategy differs from its frozen request")
        attempt = self._store.read_generation_attempt_for_slot(request.job, outcome.command_slot_id)
        if attempt is None:
            blob = self._store.put_immutable_blob(
                request.job, content=prepared.request_payload, content_hash=sha256_bytes(prepared.request_payload),
                media_type="application/json",
            )
            attempt = self._store.reserve_generation_attempt(
                outcome.command_slot_id, prepared.request_hash,
                provider_id=request.generation.provider_id,
                provider_idempotency_key=prepared.provider_idempotency_key_for(1),
                request_payload=blob, retry_policy_hash=request.retry_policy.canonical_hash,
                max_attempts=request.retry_policy.max_attempts,
            )
        _assert_attempt(prepared, outcome, attempt)
        _read_request_bytes(self._store, prepared, attempt)
        if attempt.state == "committed":
            # A concurrent writer may have committed since claim returned.
            return self._replay(request, CommandOutcome(
                outcome.command_slot_id, "succeeded", receipt_id=attempt.receipt_id,
                artifact_set_id=attempt.artifact_set_id, job_id=attempt.job_id,
            ))
        if attempt.state == "failed":
            return self._recover(prepared, outcome, attempt)
        if attempt.state in ("responded", "reconciled"):
            return self._compile(prepared, inputs, outcome, attempt)
        if attempt.state == "reserved":
            leased = self._store.dispatch_generation_attempt(attempt.attempt_id, expected_version=attempt.version)
            if leased is None:
                return BuildNarrativeGraphResult(outcome, attempt)
            active_attempt = leased

            def save_request_id(provider_request_id: str) -> None:
                nonlocal active_attempt
                active_attempt = self._store.record_generation_provider_request_id(
                    active_attempt.attempt_id, expected_version=active_attempt.version,
                    provider_request_id=provider_request_id, dispatch_lease_token=self._lease(active_attempt),
                )

            try:
                result = self._provider.dispatch(DraftDispatchRequest(
                    request.generation.provider_id, request.generation.model_id,
                    active_attempt.provider_idempotency_key, prepared.provider_payload,
                    sha256_bytes(prepared.provider_payload), save_request_id,
                ))
            except Exception:
                return self._unknown(outcome, active_attempt)
            return self._handle(prepared, inputs, outcome, active_attempt, result)
        if attempt.state in ("dispatched", "indeterminate"):
            leased = self._store.acquire_generation_reconcile_lease(attempt.attempt_id, expected_version=attempt.version)
            if leased is None:
                return BuildNarrativeGraphResult(outcome, attempt)
            attempt = leased
            try:
                result = self._provider.reconcile(ProviderReconcileQuery(
                    attempt.provider_id, request.generation.model_id,
                    attempt.provider_idempotency_key, attempt.provider_request_id,
                ))
            except Exception:
                return self._unknown(outcome, attempt)
            return self._handle(prepared, inputs, outcome, attempt, result)
        raise ValueError("Stage 1 encountered an unregistered generation state")

    @staticmethod
    def _lease(attempt: GenerationAttempt) -> str:
        if attempt.dispatch_lease_token is None:
            raise ValueError("Stage 1 provider operation requires a durable lease")
        return attempt.dispatch_lease_token

    def _unknown(self, outcome: CommandOutcome, attempt: GenerationAttempt, request_id: str | None = None) -> BuildNarrativeGraphResult:
        updated = self._store.mark_generation_indeterminate(
            attempt.attempt_id, expected_version=attempt.version,
            dispatch_lease_token=self._lease(attempt), provider_request_id=request_id or attempt.provider_request_id,
        )
        return BuildNarrativeGraphResult(outcome, updated)

    def _handle(
        self, prepared: PreparedStage1Request, inputs: CommittedSemanticInputs,
        outcome: CommandOutcome, attempt: GenerationAttempt, result: object,
    ) -> BuildNarrativeGraphResult:
        if isinstance(result, ProviderCompleted):
            blob = self._store.put_immutable_blob(
                prepared.request.job, content=result.raw_response,
                content_hash=sha256_bytes(result.raw_response), media_type="application/json",
            )
            record = (self._store.record_generation_response if attempt.state == "dispatched"
                      else self._store.reconcile_generation_response)
            attempt = record(attempt.attempt_id, expected_version=attempt.version,
                             raw_response=blob, dispatch_lease_token=self._lease(attempt),
                             provider_request_id=result.provider_request_id)
            return self._compile(prepared, inputs, outcome, attempt)
        if isinstance(result, ProviderFailed):
            failed = self._store.fail_generation_attempt(
                attempt.attempt_id, expected_version=attempt.version,
                failure_code=result.failure_code, failure_detail_json=result.failure_detail_json,
                failure_disposition=result.disposition.value, provider_request_id=result.provider_request_id,
                dispatch_lease_token=self._lease(attempt),
            )
            return self._recover(prepared, outcome, failed)
        if isinstance(result, (ProviderPending, ProviderIndeterminate)):
            return self._unknown(outcome, attempt, result.provider_request_id)
        return self._unknown(outcome, attempt)

    def _compile(
        self, prepared: PreparedStage1Request, inputs: CommittedSemanticInputs,
        outcome: CommandOutcome, attempt: GenerationAttempt,
    ) -> BuildNarrativeGraphResult:
        request = prepared.request
        _assert_attempt(prepared, outcome, attempt)
        _read_request_bytes(self._store, prepared, attempt)
        if attempt.raw_response is None or attempt.raw_response.media_type != "application/json":
            raise ValueError("Stage 1 compilation requires its durable raw response")
        raw = self._store.read_immutable_blob(request.job, attempt.raw_response)
        try:
            draft = decode_stage1_draft(raw, inputs=inputs, policy=request.draft_policy)
            compilation = compile_stage1_coverage(
                inputs, raw, draft_policy=request.draft_policy, coverage_policy=request.coverage_policy,
                scope=request.artifact_scope, revision=request.artifact_revision,
            )
            proof = build_dependency_proof(
                inputs, graph_member=compilation.narrative.narrative_graph,
                event_card_member=compilation.narrative.event_cards, ledger_member=compilation.coverage_ledger,
                policy=request.dependency_policy, revision=request.artifact_revision,
            )
            business = (*compilation.members, proof)
            checks = evaluate_stage1_business_members(
                inputs, raw, members=business, draft_policy=request.draft_policy,
                coverage_policy=request.coverage_policy, dependency_policy=request.dependency_policy,
            )
            admission = CoverageAdmission(
                canonical_json_hash({"kind": "coverage_admission", "input_binding_sha256": draft.input_binding_sha256,
                                     "canonical_draft_sha256": draft.canonical_hash}),
                draft.input_binding_sha256, sha256_bytes(raw), draft.canonical_hash,
                request.draft_policy.canonical_hash, request.coverage_policy.canonical_hash,
                request.dependency_policy.canonical_hash, request.coverage_policy.coverage_mode, "stage1-kc-v1",
                tuple(SemanticMemberIdentity.from_artifact_member(member) for member in business),
                (*checks, Stage1Check("KC-IN-001", "pass", ())),
            )
        except ValueError as error:
            return self._deny(prepared, outcome, attempt, "STAGE1_DRAFT_OR_COMPILATION_REJECTED", {"reason": str(error)})
        if admission.next_action != "continue":
            return self._deny(prepared, outcome, attempt, "STAGE1_COVERAGE_REJECTED", admission.to_mapping())
        payload = canonical_json_bytes(admission.to_mapping())
        artifacts = (*business, ArtifactMember(
            "coverage_admission", "coverage_admission", request.artifact_revision,
            request.artifact_scope, canonical_json_hash(admission.to_mapping()), payload.decode("utf-8"),
        ))
        decode_stage1_members(artifacts, scope=request.artifact_scope)
        committed = self._store.commit_generation_success(
            attempt.attempt_id, expected_version=attempt.version,
            success=CommandSuccess(outcome.command_slot_id, artifact_set_hash(artifacts), artifacts),
        )
        return self._replay(request, CommandOutcome(
            outcome.command_slot_id, "succeeded", receipt_id=committed.receipt_id,
            artifact_set_id=committed.artifact_set_id, job_id=committed.job_id,
        ))

    def _deny(self, prepared: PreparedStage1Request, outcome: CommandOutcome, attempt: GenerationAttempt,
              code: str, detail: dict[str, object]) -> BuildNarrativeGraphResult:
        failed = self._store.fail_generation_attempt(
            attempt.attempt_id, expected_version=attempt.version, failure_code=code,
            failure_detail_json=canonical_json_bytes(detail).decode("utf-8"), failure_disposition="repairable",
        )
        return self._terminal(prepared, outcome, failed, code, "denied")

    def _recover(self, prepared: PreparedStage1Request, outcome: CommandOutcome,
                 attempt: GenerationAttempt) -> BuildNarrativeGraphResult:
        if attempt.failure_disposition == "retryable" and attempt.attempt_ordinal < attempt.max_attempts:
            successor = self._store.reserve_next_generation_attempt(
                attempt.attempt_id, expected_version=attempt.version,
                provider_idempotency_key=prepared.provider_idempotency_key_for(attempt.attempt_ordinal + 1),
            )
            _assert_attempt(prepared, outcome, successor)
            return BuildNarrativeGraphResult(outcome, successor)
        code = "RETRY_BUDGET_EXHAUSTED" if attempt.failure_disposition == "retryable" else attempt.failure_code
        if code is None:
            raise ValueError("failed Stage 1 Attempt lacks its causal reason")
        # Preserve denial after a crash between the durable failure transition
        # and the Receipt transaction. Re-entry must not relabel the same cause.
        state: Literal["failed", "denied"] = (
            "denied" if code in {"STAGE1_DRAFT_OR_COMPILATION_REJECTED", "STAGE1_COVERAGE_REJECTED"}
            else "failed"
        )
        return self._terminal(prepared, outcome, attempt, code, state)

    def _terminal(self, prepared: PreparedStage1Request, outcome: CommandOutcome,
                  attempt: GenerationAttempt, code: str, state: Literal["failed", "denied"]) -> BuildNarrativeGraphResult:
        chain = self._store.read_generation_attempt_chain(prepared.request.job, outcome.command_slot_id)
        if not chain or chain[-1].attempt_id != attempt.attempt_id:
            raise ValueError("Stage 1 rejection lost its exact final Attempt")
        for item in chain:
            _assert_attempt(prepared, outcome, item)
            if item.state != "failed":
                raise ValueError("Stage 1 terminal rejection contains a nonfailed Attempt")
        detail = json.dumps({"terminal_reason": code, "attempts": [
            {"attempt_id": str(item.attempt_id), "attempt_ordinal": item.attempt_ordinal,
             "provider_request_id": item.provider_request_id, "failure_code": item.failure_code,
             "failure_disposition": item.failure_disposition,
             "failure_detail": json.loads(item.failure_detail_json or "{}")}
            for item in chain
        ]}, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
        result = self._store.commit_generation_rejection(
            attempt.attempt_id, expected_version=attempt.version,
            rejection=CommandRejection(outcome.command_slot_id, code, detail, outcome=state),
        )
        return BuildNarrativeGraphResult(result, attempt)

    def _replay(self, request: BuildNarrativeGraphRequest, outcome: CommandOutcome) -> BuildNarrativeGraphResult:
        committed = read_committed_narrative_graph(self._store, request, outcome)
        return BuildNarrativeGraphResult(outcome, committed.attempts[-1], committed)
