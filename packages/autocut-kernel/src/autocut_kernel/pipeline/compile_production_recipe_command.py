"""Persist and exactly reread one deterministic Stage 4 production Recipe set."""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Final, Protocol, cast
from uuid import UUID

from ..contracts.compiler.canonical import (
    canonical_json_bytes,
    canonical_json_hash,
    load_canonical_json_bytes,
)
from ..media.root_evidence import RootMediaEvidenceBundle
from ..media.timed_evidence import CandidateEvidenceWindowPlan, CandidateTimedEvidenceSet
from ..media.types import canonical_sha256
from ..physical_edit.candidate_dialogue_guard import (
    DialogueGuardError,
    DialogueGuardIndeterminateError,
)
from ..physical_edit.candidate_exact_span import (
    CandidateExactSpanPolicy,
    CandidateExactSpanResult,
    compile_candidate_av_span,
)
from ..physical_edit.candidate_timed_speech_authority import (
    CandidateTimedSpeechAuthority,
    project_candidate_timed_speech_authority_from_registry_entry,
    project_candidate_timed_speech_authority_from_runtime_projection,
)
from ..physical_edit.editorial_exact_span import (
    EditorialExactSpanError,
    EditorialExactSpanIndeterminateError,
    EditorialExactSpanPolicy,
    EditorialExactSpanQuery,
    derive_editorial_exact_span_query,
)
from ..physical_edit.exact_span import (
    CandidatePairLimitError,
    ExactSpanValidationError,
    NoLegalSpanError,
)
from ..physical_edit.presentation_map import ReplayedPresentationMap
from ..registry.installed_runtime import (
    InstalledLocalRunProfileResolver,
    InstalledRuntimeTimedSpeechAuthorityResolver,
)
from ..semantic_chain.candidate_catalog import Candidate
from ..semantic_chain.editorial_material_search import MaterialSearchChoice
from ..semantic_chain.editorial_models import SpanIntent
from ..semantic_chain.editorial_timing import verify_editorial_timing
from ..store.models import (
    PRODUCTION_RECIPE_COMMAND_NAME,
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    CommittedArtifactMemberReference,
    Job,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
    canonical_payload_hash,
    canonical_recipe_scope,
)
from .build_editorial_blueprint_request import BuildEditorialBlueprintRequest
from .committed_runtime_timed_media import (
    PersistedRuntimeTimedMediaEvidence,
    read_committed_runtime_timed_media_evidence,
)
from .committed_timed_media import (
    PersistedTimedMediaEvidence,
    TimedMediaReadLimits,
    read_committed_timed_media_evidence,
)
from .editorial_timed_media_inputs import (
    CommittedEditorialTimedMediaInputs,
    EditorialTimedAlternativeBinding,
    EditorialTimedCandidateBinding,
    EditorialTimedMediaStore,
    read_committed_editorial_timed_media_inputs,
)
from .finalize_runtime_timed_media_evidence_batch_command import (
    FinalizeRuntimeTimedMediaEvidenceBatchRequest,
)
from .finalize_timed_media_evidence_batch_command import FinalizeTimedMediaEvidenceBatchRequest
from .prepare_timed_media_evidence_command import PrepareTimedMediaEvidenceRequest
from .production_recipe import (
    PRODUCTION_RECIPE_PRODUCER_ID,
    ProductionBeat,
    ProductionRecipe,
    ProductionSpan,
    ProductionStory,
)
from .production_recipe_admission import (
    PHYSICAL_EDIT_REPLAY_EVALUATOR_STRATEGY_VERSION,
    PHYSICAL_EDIT_RULE_IDS,
    PhysicalEditAdmission,
    PhysicalEditBackend,
    PhysicalEditChoiceIdentity,
    PhysicalEditCompilationAttempt,
    PhysicalEditCompilationEntry,
    PhysicalEditCompilationReport,
    PhysicalEditRecipeSubject,
    PhysicalEditReplayEvidence,
    PhysicalEditReplayFact,
    VerifiedPhysicalEditAdmission,
    build_physical_edit_admission,
    verify_physical_edit_admission,
)

COMPILE_PRODUCTION_RECIPE_COMMAND: Final = PRODUCTION_RECIPE_COMMAND_NAME
PRODUCTION_RECIPE_COMMAND_STRATEGY: Final = "compile-production-recipe-v1"
SPAN_RESOLUTION_STRATEGY: Final = "preferred-then-fallback-v1"

STAGE4_NO_LEGAL_SPAN: Final = "STAGE4_NO_LEGAL_SPAN"
STAGE4_PHYSICAL_EVIDENCE_INDETERMINATE: Final = "STAGE4_PHYSICAL_EVIDENCE_INDETERMINATE"
STAGE4_DIALOGUE_EVIDENCE_INDETERMINATE: Final = "STAGE4_DIALOGUE_EVIDENCE_INDETERMINATE"
STAGE4_COMPILATION_BLOCKED: Final = "STAGE4_COMPILATION_BLOCKED"
STAGE4_OUTPUT_TIMING_INDETERMINATE: Final = "STAGE4_OUTPUT_TIMING_INDETERMINATE"
STAGE4_COMPILATION_INFRASTRUCTURE_FAILED: Final = "STAGE4_COMPILATION_INFRASTRUCTURE_FAILED"

_MAX_EXACT_JSON_INTEGER = 2**53 - 1
_JSONB_READ_FIXED_ALLOWANCE_BYTES: Final = 4_096
_REPORT_TYPE = "physical_edit_compilation_report"
_ADMISSION_TYPE = "physical_edit_admission"
_RECIPE_PREFIX = "production_recipe@"
_PRODUCTION_RECIPE_COMMIT_KEY: Final = secrets.token_bytes(32)


class CompileProductionRecipeError(ValueError):
    """The request or committed Stage 4 closure is not exact."""


class _CompilationFailureError(CompileProductionRecipeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True, slots=True)
class _VerifiedProductionRecipeCommit:
    """Process-local capability binding Store bytes to a verified Stage 4 closure."""

    success: CommandSuccess
    admission: VerifiedPhysicalEditAdmission
    _verification_mac: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.success) is not CommandSuccess:  # noqa: E721
            raise CompileProductionRecipeError("Stage 4 commit capability has invalid success")
        if type(self.admission) is not VerifiedPhysicalEditAdmission:  # noqa: E721
            raise CompileProductionRecipeError("Stage 4 commit capability has invalid Admission")
        if type(self._verification_mac) is not bytes or len(self._verification_mac) != 32:  # noqa: E721
            raise CompileProductionRecipeError("Stage 4 commit capability signature is invalid")


class ProductionRecipeCommandStore(EditorialTimedMediaStore, Protocol):
    def claim_command(self, claim: CommandClaim) -> CommandOutcome: ...

    def commit_production_recipe_success(
        self,
        verified: object,
    ) -> CommandOutcome: ...

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome: ...


@dataclass(frozen=True, slots=True)
class ProductionRecipeCompilationLimits:
    max_compilation_entries: int
    max_member_payload_bytes: int
    max_total_payload_bytes: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or not 1 <= value <= _MAX_EXACT_JSON_INTEGER  # noqa: E721
            for value in (
                self.max_compilation_entries,
                self.max_member_payload_bytes,
                self.max_total_payload_bytes,
            )
        ):
            raise CompileProductionRecipeError(
                "Stage 4 compilation limits must be positive exact integers"
            )
        if self.max_member_payload_bytes > self.max_total_payload_bytes:
            raise CompileProductionRecipeError(
                "Stage 4 member payload ceiling exceeds total payload ceiling"
            )

    def to_mapping(self) -> dict[str, int]:
        return {
            "max_compilation_entries": self.max_compilation_entries,
            "max_member_payload_bytes": self.max_member_payload_bytes,
            "max_total_payload_bytes": self.max_total_payload_bytes,
        }


MediaBatchRequest = (
    FinalizeTimedMediaEvidenceBatchRequest | FinalizeRuntimeTimedMediaEvidenceBatchRequest
)
AuthorityResolver = InstalledLocalRunProfileResolver | InstalledRuntimeTimedSpeechAuthorityResolver
PersistedMedia = PersistedTimedMediaEvidence | PersistedRuntimeTimedMediaEvidence


@dataclass(frozen=True, slots=True)
class CompileProductionRecipeRequest:
    job: Job
    idempotency_key: str
    artifact_scope: ArtifactScope
    artifact_revision: int
    stage3_request: BuildEditorialBlueprintRequest
    stage3_outcome: CommandOutcome
    media_batch_request: MediaBatchRequest
    media_batch_outcome: CommandOutcome
    editorial_exact_span_policy: EditorialExactSpanPolicy
    candidate_exact_span_policy: CandidateExactSpanPolicy
    compilation_limits: ProductionRecipeCompilationLimits

    def __post_init__(self) -> None:
        if type(self.job) is not Job or self.artifact_scope != canonical_recipe_scope(self.job):  # noqa: E721
            raise CompileProductionRecipeError("Stage 4 command requires the canonical Job scope")
        if (
            type(self.idempotency_key) is not str  # noqa: E721
            or not self.idempotency_key
            or self.idempotency_key != self.idempotency_key.strip()
        ):
            raise CompileProductionRecipeError("Stage 4 idempotency key must be canonical text")
        if (
            type(self.artifact_revision) is not int
            or not 1 <= self.artifact_revision <= _MAX_EXACT_JSON_INTEGER
        ):  # noqa: E721
            raise CompileProductionRecipeError("Stage 4 revision must be a positive exact integer")
        if (
            type(self.stage3_request) is not BuildEditorialBlueprintRequest
            or self.stage3_request.job != self.job
        ):  # noqa: E721
            raise CompileProductionRecipeError(
                "Stage 4 requires the exact same-Job Stage 3 request"
            )
        if type(self.media_batch_request) not in (  # noqa: E721
            FinalizeTimedMediaEvidenceBatchRequest,
            FinalizeRuntimeTimedMediaEvidenceBatchRequest,
        ):
            raise CompileProductionRecipeError("Stage 4 media batch request is unsupported")
        if type(self.editorial_exact_span_policy) is not EditorialExactSpanPolicy:  # noqa: E721
            raise CompileProductionRecipeError("Stage 4 editorial span policy must be exact")
        if type(self.candidate_exact_span_policy) is not CandidateExactSpanPolicy:  # noqa: E721
            raise CompileProductionRecipeError("Stage 4 candidate span policy must be exact")
        if type(self.compilation_limits) is not ProductionRecipeCompilationLimits:  # noqa: E721
            raise CompileProductionRecipeError("Stage 4 compilation limits must be exact")


@dataclass(frozen=True, slots=True)
class ResolvedCompileProductionRecipeRequest:
    request: CompileProductionRecipeRequest
    joined: CommittedEditorialTimedMediaInputs
    media: tuple[PersistedMedia, ...]
    authority: CandidateTimedSpeechAuthority
    backend_discriminator: str
    media_batch_member_ref: CommittedArtifactMemberReference
    request_hash: str


@dataclass(frozen=True, slots=True)
class PersistedProductionRecipeSet:
    record: PersistedCommittedArtifactSet
    report: PhysicalEditCompilationReport
    recipes: tuple[ProductionRecipe, ...]
    admission: VerifiedPhysicalEditAdmission

    def __post_init__(self) -> None:
        if type(self.record) is not PersistedCommittedArtifactSet:  # noqa: E721
            raise CompileProductionRecipeError(
                "persisted Stage 4 value requires an exact Store record"
            )
        if type(self.report) is not PhysicalEditCompilationReport:  # noqa: E721
            raise CompileProductionRecipeError("persisted Stage 4 value requires an exact report")
        if not self.recipes or any(type(item) is not ProductionRecipe for item in self.recipes):  # noqa: E721
            raise CompileProductionRecipeError("persisted Stage 4 value requires non-empty Recipes")
        if (
            type(self.admission) is not VerifiedPhysicalEditAdmission
            or not self.admission.render_authorized
        ):  # noqa: E721
            raise CompileProductionRecipeError(
                "persisted Stage 4 value requires valid physical Admission"
            )


@dataclass(frozen=True, slots=True)
class CompileProductionRecipeResult:
    outcome: CommandOutcome
    committed: PersistedProductionRecipeSet | None = None


def _outcome_mapping(outcome: CommandOutcome) -> dict[str, object]:
    if (
        type(outcome) is not CommandOutcome  # noqa: E721
        or outcome.state != "succeeded"
        or any(
            type(value) is not UUID
            for value in (  # noqa: E721
                outcome.command_slot_id,
                outcome.job_id,
                outcome.receipt_id,
                outcome.artifact_set_id,
            )
        )
        or outcome.failure_code is not None
        or outcome.failure_detail_json is not None
    ):
        raise CompileProductionRecipeError(
            "Stage 4 predecessor outcome must be exact and succeeded"
        )
    return {
        "artifact_set_id": str(outcome.artifact_set_id),
        "command_slot_id": str(outcome.command_slot_id),
        "job_id": str(outcome.job_id),
        "receipt_id": str(outcome.receipt_id),
        "state": "succeeded",
    }


def _limits_mapping(limits: TimedMediaReadLimits) -> dict[str, object]:
    materialization = limits.materialization
    return {
        "max_blob_bytes": limits.max_blob_bytes,
        "max_total_blob_bytes": limits.max_total_blob_bytes,
        "max_candidates": limits.max_candidates,
        "materialization": {
            "copy_chunk_bytes": materialization.copy_chunk_bytes,
            "max_source_bytes": materialization.max_source_bytes,
            "staging_quota_bytes": materialization.staging_quota_bytes,
            "timed_speech_max_request_bytes": materialization.timed_speech_max_request_bytes,
        },
    }


def _batch_member_reference(
    joined: CommittedEditorialTimedMediaInputs,
) -> CommittedArtifactMemberReference:
    artifact = joined.media_batch.artifact
    outcome = joined.media_batch.outcome
    if artifact is None or outcome.receipt_id is None or outcome.artifact_set_id is None:
        raise CompileProductionRecipeError(
            "Stage 4 requires the exact committed media batch member"
        )
    return CommittedArtifactMemberReference(
        outcome.receipt_id,
        outcome.artifact_set_id,
        0,
        artifact.scope,
        artifact.artifact_type,
        artifact.logical_id,
        artifact.revision,
        artifact.content_hash,
    )


def _base_request(child: object) -> PrepareTimedMediaEvidenceRequest:
    request = getattr(child, "request", None)
    result = getattr(request, "timed_media_request", request)
    if type(result) is not PrepareTimedMediaEvidenceRequest:  # noqa: E721
        raise CompileProductionRecipeError("Stage 4 media child has an unsupported request grammar")
    return result


def _base_request_for_episode(
    resolved: ResolvedCompileProductionRecipeRequest, episode: int
) -> PrepareTimedMediaEvidenceRequest:
    children = resolved.request.media_batch_request.children
    if type(episode) is not int or not 0 <= episode < len(children):  # noqa: E721
        raise _CompilationFailureError(
            STAGE4_PHYSICAL_EVIDENCE_INDETERMINATE,
            "candidate media child request is absent",
        )
    return _base_request(children[episode])


def _read_all_media(
    store: ProductionRecipeCommandStore,
    request: CompileProductionRecipeRequest,
    resolver: AuthorityResolver,
    limits: TimedMediaReadLimits,
) -> tuple[tuple[PersistedMedia, ...], CandidateTimedSpeechAuthority, str]:
    values: list[PersistedMedia] = []
    if type(request.media_batch_request) is FinalizeTimedMediaEvidenceBatchRequest:  # noqa: E721
        if type(resolver) is not InstalledLocalRunProfileResolver:  # noqa: E721
            raise CompileProductionRecipeError(
                "CPU Stage 4 batch requires the installed CPU resolver"
            )
        for child in request.media_batch_request.children:
            values.append(
                read_committed_timed_media_evidence(
                    store,
                    child.request,
                    child.outcome,
                    authority_profile_resolver=resolver,
                    limits=limits,
                )
            )
        authorities = tuple(
            project_candidate_timed_speech_authority_from_registry_entry(
                cast(PersistedTimedMediaEvidence, item).profile.entry
            )
            for item in values
        )
        backend = "installed_cpu_profile"
    else:
        if type(resolver) is not InstalledRuntimeTimedSpeechAuthorityResolver:  # noqa: E721
            raise CompileProductionRecipeError(
                "CUDA Stage 4 batch requires the installed CUDA resolver"
            )
        runtime_request = cast(
            FinalizeRuntimeTimedMediaEvidenceBatchRequest, request.media_batch_request
        )
        for child in runtime_request.children:
            values.append(
                read_committed_runtime_timed_media_evidence(
                    store,
                    child.request,
                    child.outcome,
                    authority_resolver=resolver,
                    limits=limits,
                )
            )
        authorities = tuple(
            project_candidate_timed_speech_authority_from_runtime_projection(
                cast(PersistedRuntimeTimedMediaEvidence, item).projection,
                resolver.selector.timing_policies,
            )
            for item in values
        )
        backend = "runtime_cuda_capability"
    if not authorities or len({item.canonical_hash for item in authorities}) != 1:
        raise CompileProductionRecipeError(
            "Stage 4 media batch does not have one homogeneous authority"
        )
    return tuple(values), authorities[0], backend


def resolve_compile_production_recipe_request(
    store: ProductionRecipeCommandStore,
    request: CompileProductionRecipeRequest,
    *,
    authority_profile_resolver: AuthorityResolver,
    limits: TimedMediaReadLimits,
) -> ResolvedCompileProductionRecipeRequest:
    """Reread the complete exact Stage 3/media closure before command claim."""
    if type(request) is not CompileProductionRecipeRequest:  # noqa: E721
        raise CompileProductionRecipeError("Stage 4 request must be exact")
    if type(limits) is not TimedMediaReadLimits:  # noqa: E721
        raise CompileProductionRecipeError("Stage 4 requires explicit committed-read limits")
    _outcome_mapping(request.stage3_outcome)
    _outcome_mapping(request.media_batch_outcome)
    joined = read_committed_editorial_timed_media_inputs(
        store,
        stage3_request=request.stage3_request,
        stage3_outcome=request.stage3_outcome,
        media_batch_request=request.media_batch_request,
        media_batch_outcome=request.media_batch_outcome,
        authority_profile_resolver=authority_profile_resolver,
        limits=limits,
    )
    media, authority, backend = _read_all_media(store, request, authority_profile_resolver, limits)
    batch_ref = _batch_member_reference(joined)
    payload = {
        "strategy_version": PRODUCTION_RECIPE_COMMAND_STRATEGY,
        "span_resolution_strategy": SPAN_RESOLUTION_STRATEGY,
        "job": {"job_key": request.job.job_key, "profile": request.job.profile},
        "artifact_scope": {
            "namespace": request.artifact_scope.namespace,
            "kind": request.artifact_scope.kind,
            "key": request.artifact_scope.key,
        },
        "artifact_revision": request.artifact_revision,
        "stage3": {
            "outcome": _outcome_mapping(request.stage3_outcome),
            "request_hash": joined.editorial.record.request_hash,
            "member_refs": [item.to_mapping() for item in joined.editorial.record.references],
        },
        "media_batch": {
            "outcome": _outcome_mapping(request.media_batch_outcome),
            "member_ref": batch_ref.to_mapping(),
            "child_member_refs": [
                [item.to_mapping() for item in row]
                for row in joined.media_batch.child_member_references
            ],
        },
        "backend_discriminator": backend,
        "authority": authority.to_mapping(),
        "authority_sha256": authority.original_authority_sha256,
        "editorial_exact_span_policy": request.editorial_exact_span_policy.to_mapping(),
        "candidate_exact_span_policy": request.candidate_exact_span_policy.to_mapping(),
        "committed_read_limits": _limits_mapping(limits),
        "compilation_limits": request.compilation_limits.to_mapping(),
    }
    return ResolvedCompileProductionRecipeRequest(
        request, joined, media, authority, backend, batch_ref, canonical_json_hash(payload)
    )


def _candidate_for_binding(
    resolved: ResolvedCompileProductionRecipeRequest, candidate_id: str
) -> Candidate:
    matches = tuple(
        item
        for item in resolved.joined.predecessors.portfolio.values.business.candidate_catalog.candidates
        if item.candidate_id == candidate_id
    )
    if len(matches) != 1:
        raise _CompilationFailureError(
            STAGE4_PHYSICAL_EVIDENCE_INDETERMINATE, "Catalog candidate is not unique"
        )
    return matches[0]


def _media_parts(
    resolved: ResolvedCompileProductionRecipeRequest,
    episode: int,
) -> tuple[
    RootMediaEvidenceBundle,
    tuple[CandidateEvidenceWindowPlan, ...],
    tuple[CandidateTimedEvidenceSet, ...],
    ReplayedPresentationMap,
]:
    if not 0 <= episode < len(resolved.media):
        raise _CompilationFailureError(
            STAGE4_PHYSICAL_EVIDENCE_INDETERMINATE, "candidate media child is absent"
        )
    persisted = resolved.media[episode]
    if type(persisted) is PersistedTimedMediaEvidence:  # noqa: E721
        root = persisted.produced.root_bundle
        calibrations = persisted.produced.calibration_bindings
        probe = persisted.request.presentation_timeline_probe
        certificate = persisted.certificate
    else:
        runtime = cast(PersistedRuntimeTimedMediaEvidence, persisted)
        root = runtime.produced.evidence.root_bundle
        calibrations = runtime.produced.evidence.calibration_bindings
        probe = runtime.request.presentation_timeline_probe
        certificate = runtime.certificate
    matches = tuple(
        item
        for item in calibrations
        if item.producer_id == root.audio_sample_boundaries.context.producer_id
    )
    if len(matches) != 1:
        raise _CompilationFailureError(
            STAGE4_PHYSICAL_EVIDENCE_INDETERMINATE, "audio snap calibration is not unique"
        )
    source_manifest_sha256 = _base_request_for_episode(resolved, episode).source_manifest_sha256
    try:
        clock = ReplayedPresentationMap(
            root, probe, certificate, source_manifest_sha256, matches[0]
        )
    except ValueError as error:
        raise _CompilationFailureError(
            STAGE4_PHYSICAL_EVIDENCE_INDETERMINATE,
            "presentation clock cannot be reconstructed",
        ) from error
    return root, persisted.plans, persisted.candidates, clock


def _choice_rows(
    resolved: ResolvedCompileProductionRecipeRequest,
) -> tuple[
    tuple[MaterialSearchChoice, EditorialTimedAlternativeBinding, EditorialTimedCandidateBinding],
    ...,
]:
    result: list[
        tuple[
            MaterialSearchChoice, EditorialTimedAlternativeBinding, EditorialTimedCandidateBinding
        ]
    ] = []
    choices = resolved.joined.editorial.values.admission.feasibility.material_search.choices
    if not choices:
        raise _CompilationFailureError(
            STAGE4_OUTPUT_TIMING_INDETERMINATE, "Stage 3 contains no admitted material choices"
        )
    for choice in choices:
        matches = tuple(
            row
            for row in resolved.joined.alternatives
            if (
                row.story_id,
                row.requirement.evidence_requirement_id,
                row.alternative.alternative_id,
            )
            == (choice.story_id, choice.requirement_id, choice.alternative_key)
        )
        if len(matches) != 1:
            raise _CompilationFailureError(
                STAGE4_OUTPUT_TIMING_INDETERMINATE,
                "admitted choice has no unique Blueprint binding",
            )
        row = matches[0]
        for key in choice.candidate_keys:
            selected = tuple(
                item for item in row.candidates if item.candidate_ref.canonical_hash == key
            )
            if len(selected) != 1:
                raise _CompilationFailureError(
                    STAGE4_PHYSICAL_EVIDENCE_INDETERMINATE,
                    "admitted candidate has no unique timed-media binding",
                )
            result.append((choice, row, selected[0]))
    if len(result) > resolved.request.compilation_limits.max_compilation_entries:
        raise _CompilationFailureError(
            STAGE4_COMPILATION_BLOCKED, "compilation entry ceiling was exceeded"
        )
    return tuple(result)


def _compile_entries(
    resolved: ResolvedCompileProductionRecipeRequest,
) -> tuple[PhysicalEditCompilationEntry, ...]:
    entries: list[PhysicalEditCompilationEntry] = []
    for ordinal, (choice, row, selected) in enumerate(_choice_rows(resolved)):
        candidate = _candidate_for_binding(resolved, selected.candidate_ref.object_id)
        base = _base_request_for_episode(resolved, selected.episode_index)
        pack = base.semantic_pack
        root, plans, candidates, clock = _media_parts(resolved, selected.episode_index)
        if not (
            0 <= selected.candidate_ordinal < len(pack.candidate_hypotheses)
            and selected.candidate_ordinal < len(plans)
            and selected.candidate_ordinal < len(candidates)
        ):
            raise _CompilationFailureError(
                STAGE4_PHYSICAL_EVIDENCE_INDETERMINATE,
                "candidate ordinal escapes exact media/VLM census",
            )
        raw = pack.candidate_hypotheses[selected.candidate_ordinal]
        plan = plans[selected.candidate_ordinal]
        timed = candidates[selected.candidate_ordinal]
        attempts: list[PhysicalEditCompilationAttempt] = []
        intents = cast(
            tuple[SpanIntent, ...],
            (
                row.beat.span_policy.preferred,
                *(
                    item
                    for item in row.beat.span_policy.fallback_order
                    if item != row.beat.span_policy.preferred
                ),
            ),
        )
        selected_pair: tuple[EditorialExactSpanQuery, CandidateExactSpanResult] | None = None
        for intent in intents:
            try:
                query = derive_editorial_exact_span_query(
                    admitted_choice=choice,
                    beat=row.beat,
                    requirement=row.requirement,
                    alternative=row.alternative,
                    selected_candidate_ref=selected.candidate_ref,
                    candidate=candidate,
                    semantic_pack=pack,
                    raw_candidate=raw,
                    timed_evidence=timed,
                    span_intent=intent,
                    policy=resolved.request.editorial_exact_span_policy,
                )
            except EditorialExactSpanIndeterminateError as error:
                raise _CompilationFailureError(
                    STAGE4_PHYSICAL_EVIDENCE_INDETERMINATE, str(error)
                ) from error
            except EditorialExactSpanError as error:
                raise _CompilationFailureError(
                    STAGE4_PHYSICAL_EVIDENCE_INDETERMINATE, str(error)
                ) from error
            try:
                result = compile_candidate_av_span(
                    query.request,
                    root,
                    timed,
                    plan,
                    resolved.authority,
                    clock,
                    resolved.request.candidate_exact_span_policy,
                )
            except NoLegalSpanError:
                attempts.append(
                    PhysicalEditCompilationAttempt(
                        intent, "no_legal_span", STAGE4_NO_LEGAL_SPAN, query.canonical_hash, None
                    )
                )
                continue
            except CandidatePairLimitError as error:
                raise _CompilationFailureError(STAGE4_COMPILATION_BLOCKED, str(error)) from error
            except (DialogueGuardIndeterminateError,) as error:
                raise _CompilationFailureError(
                    STAGE4_DIALOGUE_EVIDENCE_INDETERMINATE, str(error)
                ) from error
            except (DialogueGuardError, ExactSpanValidationError) as error:
                raise _CompilationFailureError(
                    STAGE4_PHYSICAL_EVIDENCE_INDETERMINATE, str(error)
                ) from error
            attempts.append(
                PhysicalEditCompilationAttempt(
                    intent,
                    "selected",
                    "STAGE4_SPAN_SELECTED",
                    query.canonical_hash,
                    result.canonical_hash,
                )
            )
            selected_pair = (query, result)
            break
        if selected_pair is None:
            raise _CompilationFailureError(
                STAGE4_NO_LEGAL_SPAN, "no declared span intent has a legal A/V relation"
            )
        query, result = selected_pair
        entries.append(
            PhysicalEditCompilationEntry(
                ordinal,
                choice.story_id,
                row.beat.beat_id,
                row.requirement.evidence_requirement_id,
                row.alternative.alternative_id,
                candidate.candidate_id,
                selected.episode_index,
                selected.candidate_ordinal,
                tuple(attempts),
                query,
                result,
            )
        )
    return tuple(entries)


def _compile_values(
    resolved: ResolvedCompileProductionRecipeRequest,
) -> tuple[PhysicalEditCompilationReport, tuple[ProductionRecipe, ...]]:
    entries = _compile_entries(resolved)
    request = resolved.request
    report = PhysicalEditCompilationReport(
        resolved.request_hash,
        resolved.joined.editorial.record.references,
        resolved.media_batch_member_ref,
        resolved.joined.media_batch.child_member_references,
        cast(PhysicalEditBackend, resolved.backend_discriminator),
        resolved.authority.original_authority_sha256,
        request.editorial_exact_span_policy.canonical_hash,
        request.candidate_exact_span_policy.canonical_hash,
        entries,
    )
    source_manifest_ref = (
        request.stage3_request.stage2_request.stage1_request.inputs.source_manifest
    )
    recipes: list[ProductionRecipe] = []
    for blueprint in resolved.joined.editorial.values.business.projection.blueprints:
        beats: list[ProductionBeat] = []
        durations: list[Fraction] = []
        for beat in blueprint.beats:
            selected_entries = tuple(
                item
                for item in entries
                if item.story_id == blueprint.story_id and item.beat_id == beat.beat_id
            )
            if not selected_entries:
                raise _CompilationFailureError(
                    STAGE4_OUTPUT_TIMING_INDETERMINATE, "a Blueprint Beat has no physical span"
                )
            spans = tuple(
                ProductionSpan.from_exact_span(
                    ordinal=index,
                    source_blob=_base_request_for_episode(
                        resolved, item.episode_ordinal
                    ).source_blob,
                    source_manifest_ref=source_manifest_ref,
                    query=item.selected_query,
                    result=item.selected_result,
                )
                for index, item in enumerate(selected_entries)
            )
            durations.append(
                sum(
                    (
                        Fraction(
                            (
                                item.selected_result.video_range.end_pts
                                - item.selected_result.video_range.start_pts
                            )
                            * item.selected_result.boundary_proof.video_time_base.numerator,
                            item.selected_result.boundary_proof.video_time_base.denominator,
                        )
                        for item in selected_entries
                    ),
                    start=Fraction(0),
                )
            )
            beats.append(
                ProductionBeat(
                    beat.ordinal, beat.beat_id, canonical_sha256(beat.to_mapping()), spans
                )
            )
        try:
            verify_editorial_timing(
                tuple(item.duration_seconds for item in blueprint.beats),
                blueprint.story_duration_seconds,
                blueprint.ordering_constraints,
                tuple(durations),
            )
        except ValueError as error:
            raise _CompilationFailureError(
                STAGE4_OUTPUT_TIMING_INDETERMINATE,
                "compiled A/V durations do not close the Blueprint timing constraints",
            ) from error
        recipes.append(
            ProductionRecipe(
                PRODUCTION_RECIPE_PRODUCER_ID,
                resolved.authority.profile_kind.value,
                resolved.authority.original_authority_sha256,
                ProductionStory(0, blueprint.story_id, blueprint.canonical_hash, tuple(beats)),
            )
        )
    if tuple(item.story.story_id for item in recipes) != tuple(
        item.story_id for item in resolved.joined.editorial.values.business.projection.blueprints
    ):
        raise _CompilationFailureError(
            STAGE4_OUTPUT_TIMING_INDETERMINATE, "Recipe Story census is incomplete"
        )
    return report, tuple(recipes)


def _subjects(
    request: CompileProductionRecipeRequest, recipes: tuple[ProductionRecipe, ...]
) -> tuple[PhysicalEditRecipeSubject, ...]:
    return tuple(
        PhysicalEditRecipeSubject(
            ordinal,
            recipe.story.story_id,
            "recipe",
            _RECIPE_PREFIX + recipe.story.story_id,
            request.artifact_revision,
            request.artifact_scope,
            recipe.canonical_hash,
        )
        for ordinal, recipe in enumerate(recipes)
    )


def _frozen_choice_order(
    resolved: ResolvedCompileProductionRecipeRequest,
) -> tuple[PhysicalEditChoiceIdentity, ...]:
    """Project the exact Stage 3 choice order without consulting a report."""
    return tuple(
        PhysicalEditChoiceIdentity(
            ordinal,
            choice.story_id,
            row.beat.beat_id,
            row.requirement.evidence_requirement_id,
            row.alternative.alternative_id,
            selected.candidate_ref.object_id,
            selected.episode_index,
            selected.candidate_ordinal,
        )
        for ordinal, (choice, row, selected) in enumerate(_choice_rows(resolved))
    )


def _rule_censuses(
    report: PhysicalEditCompilationReport,
    subjects: tuple[PhysicalEditRecipeSubject, ...],
) -> tuple[tuple[int, str], ...]:
    entries = report.entries
    input_rows = {
        "input_binding_sha256": report.input_binding_sha256,
        "stage3_member_refs": [item.to_mapping() for item in report.stage3_member_refs],
        "media_batch_member_ref": report.media_batch_member_ref.to_mapping(),
        "timed_media_child_member_refs": [
            [item.to_mapping() for item in row] for row in report.timed_media_child_member_refs
        ],
        "backend_discriminator": report.backend_discriminator,
        "authority_sha256": report.authority_sha256,
        "editorial_exact_policy_sha256": report.editorial_exact_policy_sha256,
        "candidate_exact_policy_sha256": report.candidate_exact_policy_sha256,
    }
    rows = (
        [
            {
                "ordinal": item.ordinal,
                "story_id": item.story_id,
                "beat_id": item.beat_id,
                "requirement_id": item.requirement_id,
                "alternative_id": item.alternative_id,
                "candidate_id": item.candidate_id,
                "episode_ordinal": item.episode_ordinal,
                "candidate_ordinal": item.candidate_ordinal,
            }
            for item in entries
        ],
        [
            {
                "ordinal": item.ordinal,
                "query": item.selected_query.to_mapping(),
                "query_sha256": item.selected_query.canonical_hash,
            }
            for item in entries
        ],
        [
            {
                "ordinal": item.ordinal,
                "result": item.selected_result.to_mapping(),
                "result_sha256": item.selected_result.canonical_hash,
            }
            for item in entries
        ],
        [
            {
                "ordinal": item.ordinal,
                "guard": item.selected_result.dialogue_guard.to_mapping(),
                "guard_sha256": item.selected_result.dialogue_guard.canonical_hash,
            }
            for item in entries
        ],
        [
            {
                "ordinal": item.ordinal,
                "boundary_proof": item.selected_result.boundary_proof.to_mapping(),
                "boundary_proof_sha256": item.selected_result.boundary_proof.canonical_hash,
            }
            for item in entries
        ],
        [
            {
                "ordinal": item.ordinal,
                "story_id": item.story_id,
                "beat_id": item.beat_id,
                "video_range": {
                    "start_pts": item.selected_result.video_range.start_pts,
                    "end_pts": item.selected_result.video_range.end_pts,
                },
                "audio_range": {
                    "start_pts": item.selected_result.audio_range.start_pts,
                    "end_pts": item.selected_result.audio_range.end_pts,
                },
            }
            for item in entries
        ],
        [item.to_mapping() for item in subjects],
    )
    return (
        (
            len(report.stage3_member_refs)
            + 1
            + sum(map(len, report.timed_media_child_member_refs)),
            canonical_sha256(input_rows),
        ),
        *((len(row), canonical_sha256(row)) for row in rows),
    )


def _replay_evidence(
    report: PhysicalEditCompilationReport,
    subjects: tuple[PhysicalEditRecipeSubject, ...],
) -> PhysicalEditReplayEvidence:
    return PhysicalEditReplayEvidence(
        tuple(
            PhysicalEditReplayFact(rule_id, count, value_hash)
            for rule_id, (count, value_hash) in zip(
                PHYSICAL_EDIT_RULE_IDS, _rule_censuses(report, subjects), strict=True
            )
        ),
        PHYSICAL_EDIT_REPLAY_EVALUATOR_STRATEGY_VERSION,
    )


def _compile_and_admit(
    resolved: ResolvedCompileProductionRecipeRequest,
) -> tuple[
    PhysicalEditCompilationReport,
    tuple[ProductionRecipe, ...],
    PhysicalEditAdmission,
    VerifiedPhysicalEditAdmission,
]:
    report, recipes = _compile_values(resolved)
    subjects = _subjects(resolved.request, recipes)
    replay_report, replay_recipes = _compile_values(resolved)
    replay_subjects = _subjects(resolved.request, replay_recipes)
    replay_evidence = _replay_evidence(replay_report, replay_subjects)
    admission = build_physical_edit_admission(report, subjects, replay_evidence)
    if admission.validation_status != "valid" or admission.next_action != "render":
        raise _CompilationFailureError(
            STAGE4_COMPILATION_INFRASTRUCTURE_FAILED,
            "independent physical Admission rejected compilation",
        )
    verified = verify_physical_edit_admission(
        admission,
        report=report,
        recipe_subjects=subjects,
        expected_job_scope=resolved.request.artifact_scope,
        expected_input_binding_sha256=resolved.request_hash,
        expected_authority_sha256=resolved.authority.original_authority_sha256,
        expected_editorial_exact_policy_sha256=(
            resolved.request.editorial_exact_span_policy.canonical_hash
        ),
        expected_candidate_exact_policy_sha256=(
            resolved.request.candidate_exact_span_policy.canonical_hash
        ),
        expected_stage3_member_refs=resolved.joined.editorial.record.references,
        expected_media_batch_member_ref=resolved.media_batch_member_ref,
        expected_timed_media_child_member_refs=(
            resolved.joined.media_batch.child_member_references
        ),
        frozen_choice_order=_frozen_choice_order(resolved),
        replay_evidence=replay_evidence,
    )
    return report, recipes, admission, verified


def _member(
    artifact_type: str,
    logical_id: str,
    request: CompileProductionRecipeRequest,
    value: PhysicalEditCompilationReport | ProductionRecipe | PhysicalEditAdmission,
) -> ArtifactMember:
    mapping = value.to_mapping()
    payload = canonical_json_bytes(mapping)
    return ArtifactMember(
        artifact_type,
        logical_id,
        request.artifact_revision,
        request.artifact_scope,
        canonical_payload_hash(payload.decode("utf-8")),
        payload.decode("utf-8"),
    )


def _artifacts(
    resolved: ResolvedCompileProductionRecipeRequest,
    report: PhysicalEditCompilationReport,
    recipes: tuple[ProductionRecipe, ...],
    admission: PhysicalEditAdmission,
) -> tuple[ArtifactMember, ...]:
    request = resolved.request
    artifacts = (
        _member(_REPORT_TYPE, _REPORT_TYPE, request, report),
        *(
            _member("recipe", _RECIPE_PREFIX + recipe.story.story_id, request, recipe)
            for recipe in recipes
        ),
        _member(_ADMISSION_TYPE, _ADMISSION_TYPE, request, admission),
    )
    total = sum(len(item.payload_json.encode("utf-8")) for item in artifacts)
    if (
        any(
            len(item.payload_json.encode("utf-8"))
            > request.compilation_limits.max_member_payload_bytes
            for item in artifacts
        )
        or total > request.compilation_limits.max_total_payload_bytes
    ):
        raise _CompilationFailureError(
            STAGE4_COMPILATION_BLOCKED, "Stage 4 canonical payload byte ceiling was exceeded"
        )
    return artifacts


def _validate_verified_production_recipe_success(
    success: CommandSuccess,
    verified: VerifiedPhysicalEditAdmission,
) -> None:
    """Prove that the exact Store members are the independently admitted closure."""

    subjects = verified.recipe_subjects
    artifacts = success.artifacts
    if (
        not verified.render_authorized
        or len(artifacts) != len(subjects) + 2
        or success.set_hash != artifact_set_hash(artifacts)
    ):
        raise CompileProductionRecipeError(
            "Stage 4 commit does not contain the verified report, Recipes, and Admission"
        )
    report = artifacts[0]
    admission = artifacts[-1]
    if (
        report.artifact_type != _REPORT_TYPE
        or report.logical_id != _REPORT_TYPE
        or report.scope != verified.expected_job_scope
        or report.content_hash != verified.report.canonical_hash
        or canonical_payload_hash(report.payload_json) != report.content_hash
        or admission.artifact_type != _ADMISSION_TYPE
        or admission.logical_id != _ADMISSION_TYPE
        or admission.scope != verified.expected_job_scope
        or admission.content_hash != verified.admission.canonical_hash
        or canonical_payload_hash(admission.payload_json) != admission.content_hash
    ):
        raise CompileProductionRecipeError(
            "Stage 4 report or Admission differs from the verified closure"
        )
    for artifact, subject in zip(artifacts[1:-1], subjects, strict=True):
        if (
            artifact.artifact_type != subject.artifact_type
            or artifact.logical_id != subject.logical_id
            or artifact.revision != subject.revision
            or artifact.scope != subject.scope
            or artifact.content_hash != subject.content_hash
            or canonical_payload_hash(artifact.payload_json) != artifact.content_hash
        ):
            raise CompileProductionRecipeError(
                "Stage 4 Recipe member differs from the verified closure"
            )


def _verified_production_recipe_commit_payload(
    value: _VerifiedProductionRecipeCommit,
) -> bytes:
    verified = value.admission
    return canonical_json_bytes(
        {
            "command_slot_id": str(value.success.command_slot_id),
            "set_hash": value.success.set_hash,
            "verification_binding_sha256": verified.verification_binding_sha256,
            "report_sha256": verified.report.canonical_hash,
            "admission_sha256": verified.admission.canonical_hash,
            "recipe_subjects": [item.to_mapping() for item in verified.recipe_subjects],
        }
    )


def _issue_verified_production_recipe_commit(
    success: CommandSuccess,
    verified: VerifiedPhysicalEditAdmission,
) -> _VerifiedProductionRecipeCommit:
    """Issue the only capability accepted by the Stage 4 persistence owner."""

    _validate_verified_production_recipe_success(success, verified)
    unsigned = _VerifiedProductionRecipeCommit(success, verified, b"\x00" * 32)
    return replace(
        unsigned,
        _verification_mac=hmac.digest(
            _PRODUCTION_RECIPE_COMMIT_KEY,
            _verified_production_recipe_commit_payload(unsigned),
            "sha256",
        ),
    )


def _open_verified_production_recipe_commit(  # pyright: ignore[reportUnusedFunction]
    value: object,
) -> CommandSuccess:
    """Open one untampered command-owned Stage 4 commit capability."""

    if type(value) is not _VerifiedProductionRecipeCommit:  # noqa: E721
        raise CompileProductionRecipeError(
            "Stage 4 persistence requires a verified commit capability"
        )
    verified = value
    expected = hmac.digest(
        _PRODUCTION_RECIPE_COMMIT_KEY,
        _verified_production_recipe_commit_payload(verified),
        "sha256",
    )
    if not hmac.compare_digest(verified._verification_mac, expected):  # pyright: ignore[reportPrivateUsage]
        raise CompileProductionRecipeError("Stage 4 commit capability was modified")
    _validate_verified_production_recipe_success(verified.success, verified.admission)
    return verified.success


def _reject(
    store: ProductionRecipeCommandStore, outcome: CommandOutcome, failure: _CompilationFailureError
) -> CommandOutcome:
    detail = canonical_json_bytes({"reason": str(failure)}).decode("utf-8")
    state = "failed" if failure.code == STAGE4_COMPILATION_INFRASTRUCTURE_FAILED else "denied"
    return store.commit_command_rejection(
        CommandRejection(outcome.command_slot_id, failure.code, detail, outcome=state)
    )


class CompileProductionRecipeCommand:
    def __init__(
        self,
        store: ProductionRecipeCommandStore,
        authority_profile_resolver: AuthorityResolver,
        limits: TimedMediaReadLimits,
    ) -> None:
        if type(authority_profile_resolver) not in (  # noqa: E721
            InstalledLocalRunProfileResolver,
            InstalledRuntimeTimedSpeechAuthorityResolver,
        ):
            raise CompileProductionRecipeError(
                "Stage 4 command requires an installed authority resolver"
            )
        if type(limits) is not TimedMediaReadLimits:  # noqa: E721
            raise CompileProductionRecipeError("Stage 4 command requires exact read limits")
        self._store = store
        self._resolver = authority_profile_resolver
        self._limits = limits

    def execute(self, request: CompileProductionRecipeRequest) -> CompileProductionRecipeResult:
        resolved = resolve_compile_production_recipe_request(
            self._store,
            request,
            authority_profile_resolver=self._resolver,
            limits=self._limits,
        )
        claimed = self._store.claim_command(
            CommandClaim(
                request.job,
                request.idempotency_key,
                COMPILE_PRODUCTION_RECIPE_COMMAND,
                resolved.request_hash,
                execution_kind="deterministic",
            )
        )
        if not claimed.is_fresh_claim:
            if claimed.state == "succeeded":
                return CompileProductionRecipeResult(
                    claimed,
                    read_committed_production_recipe_set(
                        self._store,
                        request,
                        claimed,
                        authority_profile_resolver=self._resolver,
                        limits=self._limits,
                    ),
                )
            return CompileProductionRecipeResult(claimed)
        try:
            report, recipes, admission, verified = _compile_and_admit(resolved)
            artifacts = _artifacts(resolved, report, recipes, admission)
        except _CompilationFailureError as error:
            return CompileProductionRecipeResult(_reject(self._store, claimed, error))
        except Exception:
            failure = _CompilationFailureError(
                STAGE4_COMPILATION_INFRASTRUCTURE_FAILED,
                "Stage 4 compilation infrastructure failed",
            )
            return CompileProductionRecipeResult(_reject(self._store, claimed, failure))
        success = CommandSuccess(
            claimed.command_slot_id,
            artifact_set_hash(artifacts),
            artifacts,
        )
        committed = self._store.commit_production_recipe_success(
            _issue_verified_production_recipe_commit(success, verified)
        )
        return CompileProductionRecipeResult(
            committed,
            read_committed_production_recipe_set(
                self._store,
                request,
                committed,
                authority_profile_resolver=self._resolver,
                limits=self._limits,
            ),
        )


def read_committed_production_recipe_set(
    store: ProductionRecipeCommandStore,
    request: CompileProductionRecipeRequest,
    outcome: CommandOutcome,
    *,
    authority_profile_resolver: AuthorityResolver,
    limits: TimedMediaReadLimits,
) -> PersistedProductionRecipeSet:
    """Reread exact set identity, then independently recompile and admit it."""
    _outcome_mapping(outcome)
    resolved = resolve_compile_production_recipe_request(
        store,
        request,
        authority_profile_resolver=authority_profile_resolver,
        limits=limits,
    )
    record = store.read_committed_artifact_set(
        request.job,
        command_slot_id=outcome.command_slot_id,
        receipt_id=cast(UUID, outcome.receipt_id),
        artifact_set_id=cast(UUID, outcome.artifact_set_id),
        expected_request_hash=resolved.request_hash,
        expected_command_name=COMPILE_PRODUCTION_RECIPE_COMMAND,
        expected_execution_kind="deterministic",
    )
    if (
        type(record) is not PersistedCommittedArtifactSet  # noqa: E721
        or record.job != request.job
        or record.job_id != outcome.job_id
        or record.command_slot_id != outcome.command_slot_id
        or record.receipt_id != outcome.receipt_id
        or record.artifact_set_id != outcome.artifact_set_id
        or record.request_hash != resolved.request_hash
        or record.command_name != COMPILE_PRODUCTION_RECIPE_COMMAND
        or record.execution_kind != "deterministic"
        or len(record.members) < 3
    ):
        raise CompileProductionRecipeError(
            "Stage 4 Store record differs from exact command identity"
        )
    if len(record.members) > request.compilation_limits.max_compilation_entries + 2:
        raise CompileProductionRecipeError(
            "Stage 4 committed member count exceeds its frozen compilation bound"
        )
    raw_bytes = tuple(
        member.payload_json.encode("utf-8", errors="strict") for member in record.members
    )
    jsonb_member_read_limit = min(
        _MAX_EXACT_JSON_INTEGER,
        request.compilation_limits.max_member_payload_bytes * 2 + _JSONB_READ_FIXED_ALLOWANCE_BYTES,
    )
    jsonb_total_read_limit = min(
        _MAX_EXACT_JSON_INTEGER,
        request.compilation_limits.max_total_payload_bytes * 2
        + _JSONB_READ_FIXED_ALLOWANCE_BYTES * len(raw_bytes),
    )
    if (
        any(len(raw) > jsonb_member_read_limit for raw in raw_bytes)
        or sum(map(len, raw_bytes)) > jsonb_total_read_limit
    ):
        raise CompileProductionRecipeError(
            "Stage 4 committed JSONB transport exceeds its bounded read allowance"
        )
    try:
        decoded = tuple(
            load_canonical_json_bytes(raw, origin="Stage 4 member") for raw in raw_bytes
        )
        canonical_bytes = tuple(item[1] for item in decoded)
        if (
            any(
                len(payload) > request.compilation_limits.max_member_payload_bytes
                for payload in canonical_bytes
            )
            or sum(map(len, canonical_bytes)) > request.compilation_limits.max_total_payload_bytes
        ):
            raise CompileProductionRecipeError(
                "Stage 4 committed canonical payload exceeds its frozen byte ceiling"
            )
        stored_report = PhysicalEditCompilationReport.from_mapping(decoded[0][0])
        stored_recipes = tuple(ProductionRecipe.from_mapping(item[0]) for item in decoded[1:-1])
        stored_admission = PhysicalEditAdmission.from_mapping(decoded[-1][0])
    except (TypeError, ValueError) as error:
        raise CompileProductionRecipeError("Stage 4 committed member codec is invalid") from error
    report, recipes, admission, verified = _compile_and_admit(resolved)
    expected = _artifacts(resolved, report, recipes, admission)
    if (
        len(record.members) != len(expected)
        or tuple(member.reference.member_ordinal for member in record.members)
        != tuple(range(len(expected)))
        or tuple(member.reference.artifact_type for member in record.members)
        != (_REPORT_TYPE, *("recipe" for _ in recipes), _ADMISSION_TYPE)
        or tuple(member.reference.logical_id for member in record.members)
        != (
            _REPORT_TYPE,
            *(_RECIPE_PREFIX + item.story.story_id for item in recipes),
            _ADMISSION_TYPE,
        )
        or any(
            member.reference.scope != request.artifact_scope
            or member.reference.revision != request.artifact_revision
            or member.reference.content_hash != artifact.content_hash
            or payload != artifact.payload_json.encode("utf-8")
            for member, artifact, payload in zip(
                record.members,
                expected,
                canonical_bytes,
                strict=True,
            )
        )
        or record.set_hash != artifact_set_hash(expected)
        or stored_report != report
        or stored_recipes != recipes
        or stored_admission != admission
    ):
        raise CompileProductionRecipeError(
            "Stage 4 committed layout, payload, or set hash differs from replay"
        )
    stored_verified = verify_physical_edit_admission(
        stored_admission,
        report=report,
        recipe_subjects=_subjects(request, recipes),
        expected_job_scope=request.artifact_scope,
        expected_input_binding_sha256=resolved.request_hash,
        expected_authority_sha256=resolved.authority.original_authority_sha256,
        expected_editorial_exact_policy_sha256=(request.editorial_exact_span_policy.canonical_hash),
        expected_candidate_exact_policy_sha256=(request.candidate_exact_span_policy.canonical_hash),
        expected_stage3_member_refs=resolved.joined.editorial.record.references,
        expected_media_batch_member_ref=resolved.media_batch_member_ref,
        expected_timed_media_child_member_refs=(
            resolved.joined.media_batch.child_member_references
        ),
        frozen_choice_order=_frozen_choice_order(resolved),
        replay_evidence=verified.replay_evidence,
    )
    return PersistedProductionRecipeSet(record, report, recipes, stored_verified)


__all__ = (
    "COMPILE_PRODUCTION_RECIPE_COMMAND",
    "PRODUCTION_RECIPE_COMMAND_STRATEGY",
    "SPAN_RESOLUTION_STRATEGY",
    "STAGE4_COMPILATION_BLOCKED",
    "STAGE4_COMPILATION_INFRASTRUCTURE_FAILED",
    "STAGE4_DIALOGUE_EVIDENCE_INDETERMINATE",
    "STAGE4_NO_LEGAL_SPAN",
    "STAGE4_OUTPUT_TIMING_INDETERMINATE",
    "STAGE4_PHYSICAL_EVIDENCE_INDETERMINATE",
    "CompileProductionRecipeCommand",
    "CompileProductionRecipeError",
    "CompileProductionRecipeRequest",
    "CompileProductionRecipeResult",
    "PersistedProductionRecipeSet",
    "ProductionRecipeCommandStore",
    "ProductionRecipeCompilationLimits",
    "ResolvedCompileProductionRecipeRequest",
    "read_committed_production_recipe_set",
    "resolve_compile_production_recipe_request",
)
