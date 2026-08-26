"""Durable Stage 1 generation, independent evaluation and exact-set replay.

Only this boundary adds KC-IN-001 after actual Store reads. Providers return raw
draft bytes; they never submit Graphs or Admissions. Replays read the committed
eight-member set, not freshly compiled substitutes for persisted references.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash, sha256_bytes
from ..semantic_chain.coverage_admission import CoverageAdmission
from ..semantic_chain.coverage_compiler import compile_stage1_coverage
from ..semantic_chain.dependency_proof import build_dependency_proof
from ..semantic_chain.draft_provider import DraftProviderPort
from ..semantic_chain.member_refs import SemanticMemberIdentity
from ..semantic_chain.stage1_checks import Stage1Check
from ..semantic_chain.stage1_draft import decode_stage1_draft
from ..semantic_chain.stage1_evaluation import evaluate_stage1_business_members
from ..semantic_chain.stage1_result import Stage1Values, decode_stage1_members
from ..store.models import (
    ArtifactMember,
    CommandOutcome,
    CommandSuccess,
    CommittedSemanticInputs,
    CommittedSemanticInputsRequest,
    GenerationAttempt,
    Job,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
)
from .build_narrative_graph_request import (
    BuildNarrativeGraphRequest,
    PreparedStage1Request,
    prepare_stage1_request,
)
from .draft_generation_lifecycle import (
    CommittedDraftAuditStore,
    DraftExecutionPlan,
    DraftGenerationLifecycle,
    read_committed_draft_audit,
    read_draft_response_bytes,
)

COMMAND_NAME = "BuildNarrativeGraphCommand"


class NarrativeGraphStore(CommittedDraftAuditStore, Protocol):
    def read_committed_semantic_inputs(self, request: CommittedSemanticInputsRequest) -> CommittedSemanticInputs: ...

    def read_committed_artifact_set(
        self, job: Job, *, command_slot_id: UUID, receipt_id: UUID, artifact_set_id: UUID,
        expected_request_hash: str, expected_command_name: str, expected_execution_kind: str,
    ) -> PersistedCommittedArtifactSet: ...

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


def _execution_plan(prepared: PreparedStage1Request) -> DraftExecutionPlan:
    request = prepared.request
    return DraftExecutionPlan(
        COMMAND_NAME,
        "BuildNarrativeGraph",
        request.job,
        request.idempotency_key,
        prepared.request_hash,
        prepared.request_payload,
        prepared.provider_payload,
        request.generation.provider_id,
        request.generation.model_id,
        request.generation.adapter_strategy_version,
        request.retry_policy,
        frozenset({"STAGE1_DRAFT_OR_COMPILATION_REJECTED", "STAGE1_COVERAGE_REJECTED"}),
    )


def read_committed_narrative_graph(
    store: NarrativeGraphStore, request: BuildNarrativeGraphRequest, outcome: CommandOutcome,
) -> PersistedNarrativeGraphSet:
    """Exact replay and downstream read; never infer a missing member hash/ID."""
    if (type(outcome) is not CommandOutcome or outcome.state != "succeeded"  # noqa: E721
            or outcome.receipt_id is None or outcome.artifact_set_id is None):
        raise ValueError("Stage 1 reader requires an exact succeeded Receipt/ArtifactSet")
    inputs = store.read_committed_semantic_inputs(request.inputs)
    prepared = prepare_stage1_request(request, inputs)
    plan = _execution_plan(prepared)
    record = store.read_committed_artifact_set(
        request.job, command_slot_id=outcome.command_slot_id, receipt_id=outcome.receipt_id,
        artifact_set_id=outcome.artifact_set_id, expected_request_hash=prepared.request_hash,
        expected_command_name=COMMAND_NAME, expected_execution_kind="generation",
    )
    if record.job_id != outcome.job_id:
        raise ValueError("Stage 1 outcome Job differs from committed result")
    chain, raw = read_committed_draft_audit(store, plan, outcome)
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
        self._store = store
        self._lifecycle = DraftGenerationLifecycle(store, provider)

    def execute(self, request: BuildNarrativeGraphRequest) -> BuildNarrativeGraphResult:
        inputs = self._store.read_committed_semantic_inputs(request.inputs)
        prepared = prepare_stage1_request(request, inputs)
        state = self._lifecycle.execute(_execution_plan(prepared))
        if state.outcome.state in ("failed", "denied"):
            return BuildNarrativeGraphResult(state.outcome, state.attempt)
        if state.outcome.state == "succeeded":
            return self._replay(request, state.outcome)
        if state.attempt is None:
            return BuildNarrativeGraphResult(state.outcome)
        if state.attempt.state in ("responded", "reconciled"):
            return self._compile(prepared, inputs, state.outcome, state.attempt)
        return BuildNarrativeGraphResult(state.outcome, state.attempt)

    def _compile(
        self, prepared: PreparedStage1Request, inputs: CommittedSemanticInputs,
        outcome: CommandOutcome, attempt: GenerationAttempt,
    ) -> BuildNarrativeGraphResult:
        request = prepared.request
        plan = _execution_plan(prepared)
        raw = read_draft_response_bytes(self._store, plan, outcome, attempt)
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
        state = self._lifecycle.reject(_execution_plan(prepared), outcome, attempt, code, detail)
        return BuildNarrativeGraphResult(state.outcome, state.attempt)

    def _replay(self, request: BuildNarrativeGraphRequest, outcome: CommandOutcome) -> BuildNarrativeGraphResult:
        committed = read_committed_narrative_graph(self._store, request, outcome)
        return BuildNarrativeGraphResult(outcome, committed.attempts[-1], committed)
