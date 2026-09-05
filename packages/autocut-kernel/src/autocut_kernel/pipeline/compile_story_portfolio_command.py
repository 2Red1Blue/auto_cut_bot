"""Audited Stage 2 generation, atomic five-member commit and exact replay.

The provider proposes narrative choices, never physical endpoints or Admission.
Only actual predecessor/audit reads authorize the two input checks. Installed
Runtime profile selection remains the composition root's responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from ..semantic_chain.draft_provider import DraftProviderPort
from ..semantic_chain.member_refs import SemanticMemberIdentity
from ..semantic_chain.portfolio_admission import PortfolioAdmission, Stage2Check
from ..semantic_chain.story_design_boundary import decode_bound_story_draft, uses_compact_story_design
from ..semantic_chain.story_design_compiler import compile_story_design
from ..semantic_chain.story_design_evaluation import evaluate_story_design_business_members
from ..semantic_chain.story_design_members import decode_story_design_business_members
from ..semantic_chain.story_design_result import StoryDesignValues, decode_story_design_members
from ..store.models import (
    ArtifactMember,
    CommandOutcome,
    CommandSuccess,
    GenerationAttempt,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
)
from .build_narrative_graph_command import NarrativeGraphStore
from .compile_story_portfolio_request import (
    CompileStoryPortfolioRequest,
    PreparedStage2Request,
    prepare_stage2_request,
)
from .draft_generation_lifecycle import (
    DraftExecutionPlan,
    DraftGenerationLifecycle,
    read_committed_draft_audit,
    read_draft_response_bytes,
)
from .story_design_diagnostics import story_design_failure_detail
from .story_design_inputs import CommittedStoryDesignInputs, read_committed_story_design_inputs

COMMAND_NAME = "CompileStoryPortfolioCommand"
_DENIAL_CODES = frozenset({
    "STAGE2_DRAFT_OR_COMPILATION_REJECTED", "STAGE2_PORTFOLIO_INFEASIBLE",
    "STAGE2_MATERIAL_INDETERMINATE", "STAGE2_ADMISSION_REJECTED",
})


@dataclass(frozen=True, slots=True)
class PersistedStoryPortfolioSet:
    record: PersistedCommittedArtifactSet
    values: StoryDesignValues
    attempts: tuple[GenerationAttempt, ...]


@dataclass(frozen=True, slots=True)
class CompileStoryPortfolioResult:
    outcome: CommandOutcome
    attempt: GenerationAttempt | None = None
    committed: PersistedStoryPortfolioSet | None = None


def _plan(prepared: PreparedStage2Request) -> DraftExecutionPlan:
    request = prepared.request
    return DraftExecutionPlan(
        command_name=COMMAND_NAME, provider_key_namespace="CompileStoryPortfolio",
        job=request.job, idempotency_key=request.idempotency_key,
        request_hash=prepared.request_hash, request_payload=prepared.request_payload,
        provider_payload=prepared.provider_payload, provider_id=request.generation.provider_id,
        model_id=request.generation.model_id, adapter_strategy_version=request.generation.adapter_strategy_version,
        retry_policy=request.retry_policy, denial_codes=_DENIAL_CODES,
    )


def _inputs(store: NarrativeGraphStore, request: CompileStoryPortfolioRequest) -> CommittedStoryDesignInputs:
    return read_committed_story_design_inputs(
        store, stage1_request=request.stage1_request, stage1_outcome=request.stage1_outcome,
    )


def _evaluate(
    prepared: PreparedStage2Request, inputs: CommittedStoryDesignInputs,
    raw: bytes, business: tuple[ArtifactMember, ...],
) -> PortfolioAdmission:
    """Called only after this Command/reader has performed input and audit reads."""
    request = prepared.request
    draft = decode_bound_story_draft(
        inputs.semantic, inputs.narrative.values, prepared.projection, raw,
        job_policy=request.job_policy, story_policy=request.story_policy,
        candidate_policy=request.candidate_policy, draft_policy=request.draft_policy,
        prompt_version=request.generation.prompt_version,
    )
    values = decode_story_design_business_members(business, scope=request.artifact_scope)
    checks = evaluate_story_design_business_members(
        inputs.semantic, inputs.narrative.values, raw, members=business,
        candidate_policy=request.candidate_policy, job_policy=request.job_policy,
        story_policy=request.story_policy, draft_policy=request.draft_policy,
        prompt_version=request.generation.prompt_version,
    )
    return PortfolioAdmission(
        prepared.input_binding_sha256, sha256_bytes(raw), draft.canonical_hash,
        request.draft_policy.canonical_hash, request.candidate_policy.canonical_hash,
        request.story_policy.canonical_hash, request.job_policy.canonical_hash,
        "stage2-sd-compact-v2" if uses_compact_story_design(request.generation.prompt_version) else "stage2-sd-v1",
        tuple(SemanticMemberIdentity.from_artifact_member(member) for member in business),
        values.portfolio.target_story_ids,
        (*checks, Stage2Check("SD-IN-001", "pass", ()), Stage2Check("SD-IN-002", "pass", ())),
    )


def read_committed_story_portfolio(
    store: NarrativeGraphStore, request: CompileStoryPortfolioRequest, outcome: CommandOutcome,
) -> PersistedStoryPortfolioSet:
    """Read stored identities and independently recompute decisions, not outputs."""
    if (type(outcome) is not CommandOutcome or outcome.state != "succeeded"  # noqa: E721
            or outcome.job_id is None or outcome.receipt_id is None or outcome.artifact_set_id is None
            or outcome.failure_code is not None or outcome.failure_detail_json is not None):
        raise ValueError("Stage 2 reader requires an exact succeeded Job/Receipt/ArtifactSet")
    inputs = _inputs(store, request)
    prepared = prepare_stage2_request(request, inputs)
    record = store.read_committed_artifact_set(
        request.job, command_slot_id=outcome.command_slot_id, receipt_id=outcome.receipt_id,
        artifact_set_id=outcome.artifact_set_id, expected_request_hash=prepared.request_hash,
        expected_command_name=COMMAND_NAME, expected_execution_kind="generation",
    )
    if (record.job != request.job or record.job_id != outcome.job_id
            or record.command_slot_id != outcome.command_slot_id or record.receipt_id != outcome.receipt_id
            or record.artifact_set_id != outcome.artifact_set_id or record.request_hash != prepared.request_hash
            or record.command_name != COMMAND_NAME or record.execution_kind != "generation"):
        raise ValueError("Stage 2 committed result differs from the exact requested identity")
    chain, raw = read_committed_draft_audit(store, _plan(prepared), outcome)
    values = decode_story_design_members(record.artifacts, scope=request.artifact_scope)
    expected = _evaluate(prepared, inputs, raw, record.artifacts[:4])
    if (values.admission != expected or expected.next_action != "continue"
            or any(member.revision != request.artifact_revision for member in record.artifacts)):
        raise ValueError("Stage 2 committed Admission differs from independent audited evaluation")
    return PersistedStoryPortfolioSet(record, values, chain)


class CompileStoryPortfolioCommand:
    def __init__(self, store: NarrativeGraphStore, provider: DraftProviderPort) -> None:
        self._store = store
        self._generation = DraftGenerationLifecycle(store, provider)

    def execute(self, request: CompileStoryPortfolioRequest) -> CompileStoryPortfolioResult:
        inputs = _inputs(self._store, request)
        prepared = prepare_stage2_request(request, inputs)
        state = self._generation.execute(_plan(prepared))
        if state.outcome.state == "succeeded":
            return self._replay(request, state.outcome)
        if state.attempt is None or state.attempt.state not in ("responded", "reconciled"):
            return CompileStoryPortfolioResult(state.outcome, state.attempt)
        return self._compile(prepared, inputs, state.outcome, state.attempt)

    def _compile(
        self, prepared: PreparedStage2Request, inputs: CommittedStoryDesignInputs,
        outcome: CommandOutcome, attempt: GenerationAttempt,
    ) -> CompileStoryPortfolioResult:
        request = prepared.request
        raw = read_draft_response_bytes(self._store, _plan(prepared), outcome, attempt)
        try:
            compilation = compile_story_design(
                inputs.semantic, inputs.narrative.values, raw,
                scope=request.artifact_scope, revision=request.artifact_revision,
                candidate_policy=request.candidate_policy, job_policy=request.job_policy,
                story_policy=request.story_policy, draft_policy=request.draft_policy,
                prompt_version=request.generation.prompt_version,
            )
        except ValueError as error:
            return self._deny(prepared, outcome, attempt, "STAGE2_DRAFT_OR_COMPILATION_REJECTED", story_design_failure_detail(
                error, phase="compilation", raw=raw, attempt_id=attempt.attempt_id,
            ))
        if compilation.search.status != "feasible":
            code = ("STAGE2_PORTFOLIO_INFEASIBLE" if compilation.search.status == "infeasible"
                    else "STAGE2_MATERIAL_INDETERMINATE")
            return self._deny(prepared, outcome, attempt, code, {
                "search_status": compilation.search.status,
                "visited_states": compilation.search.visited_states,
                "proposal_set": compilation.proposal_set.to_mapping(),
            })
        business = compilation.business_members
        try:
            admission = _evaluate(prepared, inputs, raw, business)
        except ValueError as error:
            return self._deny(prepared, outcome, attempt, "STAGE2_DRAFT_OR_COMPILATION_REJECTED", story_design_failure_detail(
                error, phase="independent_evaluation", raw=raw, attempt_id=attempt.attempt_id,
            ))
        if admission.next_action != "continue":
            return self._deny(prepared, outcome, attempt, "STAGE2_ADMISSION_REJECTED", admission.to_mapping())
        artifacts = (*business, ArtifactMember(
            "portfolio_admission", "portfolio_admission", request.artifact_revision, request.artifact_scope,
            admission.canonical_hash, canonical_json_bytes(admission.to_mapping()).decode("utf-8"),
        ))
        decode_story_design_members(artifacts, scope=request.artifact_scope)
        committed = self._store.commit_generation_success(
            attempt.attempt_id, expected_version=attempt.version,
            success=CommandSuccess(outcome.command_slot_id, artifact_set_hash(artifacts), artifacts),
        )
        return self._replay(request, CommandOutcome(
            outcome.command_slot_id, "succeeded", receipt_id=committed.receipt_id,
            artifact_set_id=committed.artifact_set_id, job_id=committed.job_id,
        ))

    def _deny(
        self, prepared: PreparedStage2Request, outcome: CommandOutcome,
        attempt: GenerationAttempt, code: str, detail: dict[str, object],
    ) -> CompileStoryPortfolioResult:
        state = self._generation.reject(_plan(prepared), outcome, attempt, code, detail)
        return CompileStoryPortfolioResult(state.outcome, state.attempt)

    def _replay(self, request: CompileStoryPortfolioRequest, outcome: CommandOutcome) -> CompileStoryPortfolioResult:
        committed = read_committed_story_portfolio(self._store, request, outcome)
        return CompileStoryPortfolioResult(outcome, committed.attempts[-1], committed)
