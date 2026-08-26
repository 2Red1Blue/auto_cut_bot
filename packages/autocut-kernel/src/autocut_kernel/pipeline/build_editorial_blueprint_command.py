"""Audited all-or-nothing Stage 3 Editorial Blueprint generation Command."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from ..semantic_chain.draft_provider import DraftProviderPort
from ..semantic_chain.editorial_admission import (
    SS_BATCH_RULE_IDS,
    SS_EVALUATION_STRATEGY,
    EditorialCheck,
    SemanticFeasibilityAdmission,
)
from ..semantic_chain.editorial_blueprint import project_editorial_blueprints
from ..semantic_chain.editorial_draft import decode_editorial_draft
from ..semantic_chain.editorial_evaluation import (
    EditorialBusinessEvaluation,
    evaluate_editorial_business_members,
)
from ..semantic_chain.editorial_members import compose_editorial_business_members
from ..semantic_chain.editorial_result import EditorialValues, decode_editorial_members
from ..semantic_chain.member_refs import SemanticMemberIdentity
from ..store.models import (
    ArtifactMember,
    CommandOutcome,
    CommandSuccess,
    GenerationAttempt,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
)
from .build_editorial_blueprint_request import (
    BuildEditorialBlueprintRequest,
    PreparedStage3Request,
    prepare_stage3_request,
)
from .build_narrative_graph_command import NarrativeGraphStore
from .draft_generation_lifecycle import (
    DraftExecutionPlan,
    DraftGenerationLifecycle,
    read_committed_draft_audit,
    read_draft_response_bytes,
)
from .editorial_blueprint_inputs import (
    CommittedEditorialBlueprintInputs,
    read_committed_editorial_blueprint_inputs,
)

COMMAND_NAME = "BuildEditorialBlueprintCommand"
_DENIAL_CODES = frozenset({
    "STAGE3_DRAFT_OR_COMPILATION_REJECTED",
    "STAGE3_FEASIBILITY_REJECTED",
    "STAGE3_ADMISSION_REJECTED",
})


@dataclass(frozen=True, slots=True)
class PersistedEditorialBlueprintSet:
    record: PersistedCommittedArtifactSet
    values: EditorialValues
    attempts: tuple[GenerationAttempt, ...]


@dataclass(frozen=True, slots=True)
class BuildEditorialBlueprintResult:
    outcome: CommandOutcome
    attempt: GenerationAttempt | None = None
    committed: PersistedEditorialBlueprintSet | None = None


def _plan(prepared: PreparedStage3Request) -> DraftExecutionPlan:
    request = prepared.request
    return DraftExecutionPlan(
        command_name=COMMAND_NAME,
        provider_key_namespace="BuildEditorialBlueprint",
        job=request.job,
        idempotency_key=request.idempotency_key,
        request_hash=prepared.request_hash,
        request_payload=prepared.request_payload,
        provider_payload=prepared.provider_payload,
        provider_id=request.generation.provider_id,
        model_id=request.generation.model_id,
        adapter_strategy_version=request.generation.adapter_strategy_version,
        retry_policy=request.retry_policy,
        denial_codes=_DENIAL_CODES,
    )


def _inputs(
    store: NarrativeGraphStore, request: BuildEditorialBlueprintRequest,
) -> CommittedEditorialBlueprintInputs:
    return read_committed_editorial_blueprint_inputs(
        store, stage2_request=request.stage2_request, stage2_outcome=request.stage2_outcome,
    )


def _command_checks(prepared: PreparedStage3Request) -> tuple[EditorialCheck, ...]:
    if (
        len(prepared.provider_payload) > prepared.request.max_prompt_bytes
        or len(prepared.contexts.prompt_payload)
        > prepared.request.context_policy.max_batch_context_bytes
    ):
        raise ValueError("actual Stage 3 provider request exceeds its frozen byte budget")
    return (
        EditorialCheck("SS-IN-001", "pass", ()),
        EditorialCheck("SS-IN-002", "pass", ()),
        EditorialCheck("SS-CTX-BYTES-001", "pass", ()),
    )


def _admission(
    prepared: PreparedStage3Request, raw: bytes, evaluation: EditorialBusinessEvaluation,
) -> SemanticFeasibilityAdmission:
    business = evaluation.expected_business.members
    checks = (*evaluation.batch_checks, *_command_checks(prepared))
    if (
        len(checks) != len(SS_BATCH_RULE_IDS)
        or {check.rule_id for check in checks} != set(SS_BATCH_RULE_IDS)
    ):
        raise ValueError("Stage 3 evaluation lacks the exact closed batch checks")
    ordered_checks = tuple(next(check for check in checks if check.rule_id == rule_id)
                           for rule_id in SS_BATCH_RULE_IDS)
    return SemanticFeasibilityAdmission(
        prepared.input_binding_sha256,
        sha256_bytes(raw),
        evaluation.canonical_draft_sha256,
        prepared.request.command_policy.canonical_hash,
        prepared.request.stage2_request.command_policy.canonical_hash,
        evaluation.feasibility,
        tuple(SemanticMemberIdentity.from_artifact_member(member) for member in business),
        evaluation.story_checks,
        ordered_checks,
        SS_EVALUATION_STRATEGY,
    )


def _evaluate(
    prepared: PreparedStage3Request, inputs: CommittedEditorialBlueprintInputs,
    raw: bytes, business: tuple[ArtifactMember, ...],
) -> tuple[SemanticFeasibilityAdmission, EditorialBusinessEvaluation]:
    request = prepared.request
    evaluation = evaluate_editorial_business_members(
        inputs.semantic, inputs.narrative.values, inputs.portfolio.values, raw,
        members=business, command_policy=request.command_policy,
        stage2_policy=request.stage2_request.command_policy,
    )
    if evaluation.expected_business.members != business:
        raise ValueError("Stage 3 business members differ from independent evaluation")
    return _admission(prepared, raw, evaluation), evaluation


def read_committed_editorial_blueprints(
    store: NarrativeGraphStore, request: BuildEditorialBlueprintRequest, outcome: CommandOutcome,
) -> PersistedEditorialBlueprintSet:
    """Replay exact 3N+1 output and independently recompute its Admission."""
    if (
        type(outcome) is not CommandOutcome  # noqa: E721
        or outcome.state != "succeeded"
        or outcome.job_id is None
        or outcome.receipt_id is None
        or outcome.artifact_set_id is None
        or outcome.failure_code is not None
        or outcome.failure_detail_json is not None
    ):
        raise ValueError("Stage 3 reader requires an exact succeeded outcome")
    inputs = _inputs(store, request)
    prepared = prepare_stage3_request(request, inputs)
    plan = _plan(prepared)
    record = store.read_committed_artifact_set(
        request.job, command_slot_id=outcome.command_slot_id, receipt_id=outcome.receipt_id,
        artifact_set_id=outcome.artifact_set_id, expected_request_hash=prepared.request_hash,
        expected_command_name=COMMAND_NAME, expected_execution_kind="generation",
    )
    if type(record) is not PersistedCommittedArtifactSet:  # noqa: E721
        raise ValueError("Stage 3 reader requires an exact committed artifact Set")
    if (
        record.job != request.job
        or record.job_id != outcome.job_id
        or record.command_slot_id != outcome.command_slot_id
        or record.receipt_id != outcome.receipt_id
        or record.artifact_set_id != outcome.artifact_set_id
        or record.request_hash != prepared.request_hash
        or record.command_name != COMMAND_NAME
        or record.execution_kind != "generation"
    ):
        raise ValueError("Stage 3 committed result differs from the exact requested identity")
    chain, raw = read_committed_draft_audit(store, plan, outcome)
    business = record.artifacts[:-1]
    admission, evaluation = _evaluate(prepared, inputs, raw, business)
    values = decode_editorial_members(record.artifacts, contexts=evaluation.contexts)
    if (
        values.admission != admission
        or admission.next_action != "continue"
        or any(member.revision != request.artifact_revision for member in record.artifacts)
    ):
        raise ValueError("Stage 3 committed Admission differs from independent audited evaluation")
    return PersistedEditorialBlueprintSet(record, values, chain)


class BuildEditorialBlueprintCommand:
    """One full-batch Stage 3 generation; never commits partial Story outputs."""

    def __init__(self, store: NarrativeGraphStore, provider: DraftProviderPort) -> None:
        self._store = store
        self._generation = DraftGenerationLifecycle(store, provider)

    def execute(self, request: BuildEditorialBlueprintRequest) -> BuildEditorialBlueprintResult:
        inputs = _inputs(self._store, request)
        prepared = prepare_stage3_request(request, inputs)
        state = self._generation.execute(_plan(prepared))
        if state.outcome.state in ("failed", "denied"):
            return BuildEditorialBlueprintResult(state.outcome, state.attempt)
        if state.outcome.state == "succeeded":
            return self._replay(request, state.outcome)
        if state.attempt is None or state.attempt.state not in ("responded", "reconciled"):
            return BuildEditorialBlueprintResult(state.outcome, state.attempt)
        return self._compile(prepared, inputs, state.outcome, state.attempt)

    def _compile(
        self, prepared: PreparedStage3Request, inputs: CommittedEditorialBlueprintInputs,
        outcome: CommandOutcome, attempt: GenerationAttempt,
    ) -> BuildEditorialBlueprintResult:
        request = prepared.request
        # Audit defects are durable-transport integrity failures, not semantic
        # model content.  Never convert a tampered/misbound Blob into a new
        # causal denial Receipt; replay readers must see the same hard error.
        raw = read_draft_response_bytes(self._store, _plan(prepared), outcome, attempt)
        try:
            draft = decode_editorial_draft(
                raw, expected_input_binding_sha256=prepared.input_binding_sha256,
                expected_target_story_ids=prepared.contexts.target_story_ids,
                policy=request.draft_policy,
            )
            projection = project_editorial_blueprints(
                inputs.narrative.values, inputs.portfolio.values, draft,
                expected_input_binding_sha256=prepared.input_binding_sha256,
                strategy_version=request.blueprint_strategy_version,
            )
            business = compose_editorial_business_members(prepared.contexts, projection).members
            admission, _evaluation = _evaluate(prepared, inputs, raw, business)
        except ValueError as error:
            return self._deny(
                prepared, outcome, attempt, "STAGE3_DRAFT_OR_COMPILATION_REJECTED", {"reason": str(error)},
            )
        if admission.feasibility.status != "feasible":
            return self._deny(
                prepared, outcome, attempt, "STAGE3_FEASIBILITY_REJECTED", admission.to_mapping(),
            )
        if admission.next_action != "continue":
            return self._deny(
                prepared, outcome, attempt, "STAGE3_ADMISSION_REJECTED", admission.to_mapping(),
            )
        artifacts = (*business, ArtifactMember(
            "semantic_feasibility_admission", "semantic_feasibility_admission",
            request.artifact_revision, request.artifact_scope, admission.canonical_hash,
            canonical_json_bytes(admission.to_mapping()).decode("utf-8"),
        ))
        decode_editorial_members(artifacts, contexts=prepared.contexts)
        committed = self._store.commit_generation_success(
            attempt.attempt_id, expected_version=attempt.version,
            success=CommandSuccess(outcome.command_slot_id, artifact_set_hash(artifacts), artifacts),
        )
        return self._replay(request, CommandOutcome(
            outcome.command_slot_id, "succeeded", receipt_id=committed.receipt_id,
            artifact_set_id=committed.artifact_set_id, job_id=committed.job_id,
        ))

    def _deny(
        self, prepared: PreparedStage3Request, outcome: CommandOutcome,
        attempt: GenerationAttempt, code: str, detail: dict[str, object],
    ) -> BuildEditorialBlueprintResult:
        state = self._generation.reject(_plan(prepared), outcome, attempt, code, detail)
        return BuildEditorialBlueprintResult(state.outcome, state.attempt)

    def _replay(
        self, request: BuildEditorialBlueprintRequest, outcome: CommandOutcome,
    ) -> BuildEditorialBlueprintResult:
        committed = read_committed_editorial_blueprints(self._store, request, outcome)
        return BuildEditorialBlueprintResult(outcome, committed.attempts[-1], committed)
