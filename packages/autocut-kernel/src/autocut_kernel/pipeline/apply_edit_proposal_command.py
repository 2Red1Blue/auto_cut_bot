"""Apply one closed ``EditProposal/v1`` as a new verified Recipe revision.

This command deliberately has no renderer, HTTP, or UI dependency.  Its only
mutation is a Store commit guarded by a process-local capability; all physical
span selection is reread from the committed ``SpanVariantSet`` closure.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, field, replace
from typing import Final, Protocol, cast
from uuid import UUID

from ..contracts.compiler.canonical import (
    canonical_json_bytes,
    canonical_json_hash,
    load_canonical_json_bytes,
)
from ..physical_edit.span_variant_set import SpanVariant, SpanVariantEntry
from ..store.errors import StaleHeadError
from ..store.models import (
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
    canonical_payload_hash,
    canonical_recipe_scope,
)
from .build_span_variant_set_command import (
    BuildSpanVariantSetRequest,
    PersistedSpanVariantSet,
    SpanVariantSetCommandStore,
    read_committed_span_variant_set,
)
from .committed_timed_media import TimedMediaReadLimits
from .compile_production_recipe_command import (
    AuthorityResolver,
    CompileProductionRecipeError,
    CompileProductionRecipeRequest,
    PersistedProductionRecipeSet,
    ResolvedCompileProductionRecipeRequest,
    _frozen_choice_order,
    _recipes_from_entries,
    _replay_evidence,
    read_committed_production_recipe_set,
    resolve_compile_production_recipe_request,
)
from .edit_proposal import EditProposal, SelectVariantOperation
from .production_recipe import ProductionRecipe
from .production_recipe_admission import (
    PhysicalEditAdmission,
    PhysicalEditCompilationAttempt,
    PhysicalEditCompilationEntry,
    PhysicalEditCompilationReport,
    PhysicalEditRecipeSubject,
    VerifiedPhysicalEditAdmission,
    build_physical_edit_admission,
    verify_physical_edit_admission,
)

APPLY_EDIT_PROPOSAL_COMMAND: Final = "ApplyEditProposalCommand@1"
APPLY_EDIT_PROPOSAL_STRATEGY: Final = "apply-edit-proposal-v1"
EDITED_RECIPE_COMPILATION_BLOCKED: Final = "EDITED_RECIPE_COMPILATION_BLOCKED"
EDITED_RECIPE_INFRASTRUCTURE_FAILED: Final = "EDITED_RECIPE_INFRASTRUCTURE_FAILED"
EDITED_RECIPE_STALE_PARENT: Final = "EDITED_RECIPE_STALE_PARENT"

_REPORT_TYPE: Final = "physical_edit_compilation_report"
_ADMISSION_TYPE: Final = "physical_edit_admission"
_RECIPE_PREFIX: Final = "production_recipe@"
_MAX_REVISION: Final = 2**53 - 1
_EDITED_RECIPE_COMMIT_KEY: Final = secrets.token_bytes(32)


class ApplyEditProposalError(ValueError):
    """The proposal or its committed predecessors are not an exact closure."""


class _ApplyFailureError(ApplyEditProposalError):
    def __init__(self, code: str, detail: str, *, outcome: str = "denied") -> None:
        super().__init__(detail)
        self.code = code
        self.outcome = outcome


class ApplyEditProposalStore(SpanVariantSetCommandStore, Protocol):
    def commit_apply_edit_proposal_success(
        self,
        request: ApplyEditProposalRequest,
        issued_capability: object,
        *,
        authority_profile_resolver: AuthorityResolver,
        limits: TimedMediaReadLimits,
    ) -> CommandOutcome: ...


@dataclass(frozen=True, slots=True)
class ApplyEditProposalRequest:
    """All immutable parents necessary to apply one proposal exactly once."""

    job: Job
    proposal: EditProposal
    parent_request: CompileProductionRecipeRequest
    parent_outcome: CommandOutcome
    span_variant_request: BuildSpanVariantSetRequest
    span_variant_outcome: CommandOutcome

    def __post_init__(self) -> None:
        if type(self.job) is not Job or self.job != self.parent_request.job:  # noqa: E721
            raise ApplyEditProposalError("edited Recipe request requires the exact same-Job parent")
        if type(self.proposal) is not EditProposal:  # noqa: E721
            raise ApplyEditProposalError("edited Recipe request requires an exact EditProposal")
        if type(self.parent_request) is not CompileProductionRecipeRequest:  # noqa: E721
            raise ApplyEditProposalError("edited Recipe request requires an exact parent request")
        if type(self.span_variant_request) is not BuildSpanVariantSetRequest:  # noqa: E721
            raise ApplyEditProposalError("edited Recipe request requires an exact variant request")
        if self.parent_request.artifact_scope != canonical_recipe_scope(self.job):
            raise ApplyEditProposalError("edited Recipe parent scope must be canonical")
        if self.proposal.base_recipe_ref.scope != self.parent_request.artifact_scope:
            raise ApplyEditProposalError("proposal base Recipe scope differs from parent")
        if self.proposal.base_revision != self.parent_request.artifact_revision:
            raise ApplyEditProposalError("proposal base revision differs from parent")
        if self.proposal.base_revision >= _MAX_REVISION:
            raise ApplyEditProposalError("proposal base revision cannot advance safely")
        if (
            self.span_variant_request.parent_request != self.parent_request
            or self.span_variant_request.parent_outcome != self.parent_outcome
            or self.span_variant_request.job != self.job
        ):
            raise ApplyEditProposalError("variant request differs from exact parent Recipe closure")
        if self.proposal.idempotency_key in {
            self.parent_request.idempotency_key,
            self.span_variant_request.idempotency_key,
        }:
            raise ApplyEditProposalError("edited Recipe requires an independent idempotency key")

    @property
    def artifact_scope(self) -> ArtifactScope:
        return self.parent_request.artifact_scope

    @property
    def artifact_revision(self) -> int:
        return self.proposal.base_revision + 1


@dataclass(frozen=True, slots=True)
class ResolvedApplyEditProposalRequest:
    request: ApplyEditProposalRequest
    parent: PersistedProductionRecipeSet
    resolved_parent: ResolvedCompileProductionRecipeRequest
    variants: PersistedSpanVariantSet
    request_hash: str


@dataclass(frozen=True, slots=True)
class ApplyEditProposalResult:
    outcome: CommandOutcome
    committed: PersistedProductionRecipeSet | None = None


@dataclass(frozen=True, slots=True)
class RebuiltAppliedEditArtifacts:
    """The complete, independently admitted closure Store must persist verbatim."""

    report: PhysicalEditCompilationReport
    recipes: tuple[ProductionRecipe, ...]
    admission: PhysicalEditAdmission
    verified: VerifiedPhysicalEditAdmission
    artifacts: tuple[ArtifactMember, ...]


@dataclass(frozen=True, slots=True)
class _VerifiedEditedRecipeCommit:
    request: ApplyEditProposalRequest
    resolved: ResolvedApplyEditProposalRequest
    success: CommandSuccess
    admission: VerifiedPhysicalEditAdmission
    _verification_mac: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.request) is not ApplyEditProposalRequest  # noqa: E721
            or type(self.resolved) is not ResolvedApplyEditProposalRequest  # noqa: E721
            or self.resolved.request is not self.request
            or type(self.success) is not CommandSuccess  # noqa: E721
            or type(self.admission) is not VerifiedPhysicalEditAdmission  # noqa: E721
        ):
            raise ApplyEditProposalError("edited Recipe commit capability is invalid")
        if type(self._verification_mac) is not bytes or len(self._verification_mac) != 32:  # noqa: E721
            raise ApplyEditProposalError("edited Recipe commit signature is invalid")


def _succeeded(outcome: CommandOutcome, label: str) -> dict[str, object]:
    if (
        type(outcome) is not CommandOutcome  # noqa: E721
        or outcome.state != "succeeded"
        or any(type(item) is not UUID for item in (outcome.command_slot_id, outcome.job_id, outcome.receipt_id, outcome.artifact_set_id))  # noqa: E721
        or outcome.failure_code is not None
        or outcome.failure_detail_json is not None
    ):
        raise ApplyEditProposalError(f"{label} outcome must be exact and succeeded")
    return {
        "command_slot_id": str(outcome.command_slot_id),
        "job_id": str(outcome.job_id),
        "receipt_id": str(outcome.receipt_id),
        "artifact_set_id": str(outcome.artifact_set_id),
        "state": "succeeded",
    }


def _request_hash(request: ApplyEditProposalRequest, parent: PersistedProductionRecipeSet, variants: PersistedSpanVariantSet) -> str:
    return canonical_json_hash(
        {
            "strategy_version": APPLY_EDIT_PROPOSAL_STRATEGY,
            "provider_call_budget": 0,
            "job": {"job_key": request.job.job_key, "profile": request.job.profile},
            "artifact_scope": {
                "namespace": request.artifact_scope.namespace,
                "kind": request.artifact_scope.kind,
                "key": request.artifact_scope.key,
            },
            "base": {
                "outcome": _succeeded(request.parent_outcome, "parent"),
                "request_hash": parent.record.request_hash,
                "set_hash": parent.record.set_hash,
                "members": [item.to_mapping() for item in parent.record.references],
            },
            "variants": {
                "outcome": _succeeded(request.span_variant_outcome, "variant"),
                "request_hash": variants.record.request_hash,
                "set_hash": variants.record.set_hash,
                "member": variants.record.references[0].to_mapping(),
            },
            "proposal_sha256": request.proposal.canonical_hash,
            "artifact_revision": request.artifact_revision,
        }
    )


def resolve_apply_edit_proposal_request(
    store: ApplyEditProposalStore,
    request: ApplyEditProposalRequest,
    *,
    authority_profile_resolver: AuthorityResolver,
    limits: TimedMediaReadLimits,
) -> ResolvedApplyEditProposalRequest:
    """Reread every parent before claiming an edited-recipe command slot."""
    if type(request) is not ApplyEditProposalRequest:  # noqa: E721
        raise ApplyEditProposalError("edited Recipe request must be exact")
    _succeeded(request.parent_outcome, "parent")
    _succeeded(request.span_variant_outcome, "variant")
    try:
        parent = read_committed_production_recipe_set(
            store, request.parent_request, request.parent_outcome,
            authority_profile_resolver=authority_profile_resolver, limits=limits,
        )
        resolved_parent = resolve_compile_production_recipe_request(
            store, request.parent_request,
            authority_profile_resolver=authority_profile_resolver, limits=limits,
        )
        variants = read_committed_span_variant_set(
            store, request.span_variant_request, request.span_variant_outcome,
            authority_profile_resolver=authority_profile_resolver, limits=limits,
        )
    except (CompileProductionRecipeError, ValueError) as error:
        raise ApplyEditProposalError("edited Recipe predecessor closure is invalid") from error
    if (
        parent.record.set_hash != request.proposal.base_recipe_set_hash
        or request.proposal.base_recipe_ref not in parent.record.references
        or request.proposal.base_recipe_ref.artifact_type != "recipe"
        or request.proposal.base_recipe_ref.revision != request.proposal.base_revision
        or variants.value.parent_request_sha256 != parent.record.request_hash
        or variants.value.parent_artifact_set_sha256 != parent.record.set_hash
        or variants.record.references != (request.proposal.edit_ops[0].span_variant_set_ref,)
        or variants.record.job != request.job
    ):
        raise ApplyEditProposalError("proposal differs from exact parent or variant committed set")
    return ResolvedApplyEditProposalRequest(request, parent, resolved_parent, variants, _request_hash(request, parent, variants))


def _variant_for_operation(
    parent_entry: PhysicalEditCompilationEntry,
    variant_entry: SpanVariantEntry,
    operation: SelectVariantOperation,
) -> SpanVariant:
    if (
        (variant_entry.ordinal, variant_entry.story_id, variant_entry.beat_id, variant_entry.requirement_id, variant_entry.alternative_id, variant_entry.candidate_id)
        != (parent_entry.ordinal, parent_entry.story_id, parent_entry.beat_id, parent_entry.requirement_id, parent_entry.alternative_id, parent_entry.candidate_id)
        or variant_entry.exact_span_query_sha256 != parent_entry.selected_query.canonical_hash
    ):
        raise ApplyEditProposalError("selected variant does not map to the same report entry/query")
    matches = tuple(item for item in variant_entry.variants if item.variant_id == operation.variant_id)
    if len(matches) != 1:
        raise ApplyEditProposalError("selected variant is absent from committed variant set")
    return matches[0]


def _edited_entries(resolved: ResolvedApplyEditProposalRequest) -> tuple[PhysicalEditCompilationEntry, ...]:
    parent_entries = resolved.parent.report.entries
    variant_entries = resolved.variants.value.entries
    if len(parent_entries) != len(variant_entries):
        raise ApplyEditProposalError("variant set entry census differs from parent report")
    operation_by_entry: dict[int, SelectVariantOperation] = {}
    for operation in resolved.request.proposal.edit_ops:
        candidates = [index for index, entry in enumerate(variant_entries) if any(item.variant_id == operation.variant_id for item in entry.variants)]
        if len(candidates) != 1 or candidates[0] in operation_by_entry:
            raise ApplyEditProposalError("select_variant must resolve one distinct report entry")
        operation_by_entry[candidates[0]] = operation
    entries: list[PhysicalEditCompilationEntry] = []
    for index, parent_entry in enumerate(parent_entries):
        operation = operation_by_entry.get(index)
        if operation is None:
            entries.append(parent_entry)
            continue
        variant = _variant_for_operation(parent_entry, variant_entries[index], operation)
        selected_attempt = parent_entry.attempts[-1]
        updated_attempt = PhysicalEditCompilationAttempt(
            selected_attempt.span_intent,
            "selected",
            selected_attempt.code,
            parent_entry.selected_query.canonical_hash,
            variant.exact_span_result.canonical_hash,
        )
        entries.append(replace(parent_entry, attempts=(*parent_entry.attempts[:-1], updated_attempt), selected_result=variant.exact_span_result))
    return tuple(entries)


def _subjects(request: ApplyEditProposalRequest, recipes: tuple[ProductionRecipe, ...]) -> tuple[PhysicalEditRecipeSubject, ...]:
    return tuple(
        PhysicalEditRecipeSubject(
            ordinal, recipe.story.story_id, "recipe", _RECIPE_PREFIX + recipe.story.story_id,
            request.artifact_revision, request.artifact_scope, recipe.canonical_hash,
        )
        for ordinal, recipe in enumerate(recipes)
    )


def _compile_and_admit(
    resolved: ResolvedApplyEditProposalRequest,
) -> tuple[PhysicalEditCompilationReport, tuple[ProductionRecipe, ...], PhysicalEditAdmission, VerifiedPhysicalEditAdmission]:
    entries = _edited_entries(resolved)
    source = resolved.resolved_parent
    report = PhysicalEditCompilationReport(
        resolved.request_hash, source.joined.editorial.record.references,
        source.media_batch_member_ref, source.joined.media_batch.child_member_references,
        source.backend_discriminator, source.authority.original_authority_sha256,
        source.request.editorial_exact_span_policy.canonical_hash,
        source.request.candidate_exact_span_policy.canonical_hash, entries,
    )
    recipes = _recipes_from_entries(source, entries)
    subjects = _subjects(resolved.request, recipes)
    replay_entries = _edited_entries(resolved)
    replay_report = PhysicalEditCompilationReport(
        resolved.request_hash, source.joined.editorial.record.references,
        source.media_batch_member_ref, source.joined.media_batch.child_member_references,
        source.backend_discriminator, source.authority.original_authority_sha256,
        source.request.editorial_exact_span_policy.canonical_hash,
        source.request.candidate_exact_span_policy.canonical_hash, replay_entries,
    )
    replay_evidence = _replay_evidence(replay_report, _subjects(resolved.request, _recipes_from_entries(source, replay_entries)))
    admission = build_physical_edit_admission(report, subjects, replay_evidence)
    if admission.validation_status != "valid" or admission.next_action != "render":
        raise _ApplyFailureError(EDITED_RECIPE_INFRASTRUCTURE_FAILED, "independent physical Admission rejected edited Recipe", outcome="failed")
    verified = verify_physical_edit_admission(
        admission, report=report, recipe_subjects=subjects,
        expected_job_scope=resolved.request.artifact_scope,
        expected_input_binding_sha256=resolved.request_hash,
        expected_authority_sha256=source.authority.original_authority_sha256,
        expected_editorial_exact_policy_sha256=source.request.editorial_exact_span_policy.canonical_hash,
        expected_candidate_exact_policy_sha256=source.request.candidate_exact_span_policy.canonical_hash,
        expected_stage3_member_refs=source.joined.editorial.record.references,
        expected_media_batch_member_ref=source.media_batch_member_ref,
        expected_timed_media_child_member_refs=source.joined.media_batch.child_member_references,
        frozen_choice_order=_frozen_choice_order(source), replay_evidence=replay_evidence,
    )
    return report, recipes, admission, verified


def _member(artifact_type: str, logical_id: str, request: ApplyEditProposalRequest, value: object) -> ArtifactMember:
    payload = canonical_json_bytes(cast(object, value).to_mapping())  # type: ignore[attr-defined]
    return ArtifactMember(artifact_type, logical_id, request.artifact_revision, request.artifact_scope, canonical_payload_hash(payload.decode("utf-8")), payload.decode("utf-8"))


def _artifacts(resolved: ResolvedApplyEditProposalRequest, report: PhysicalEditCompilationReport, recipes: tuple[ProductionRecipe, ...], admission: PhysicalEditAdmission) -> tuple[ArtifactMember, ...]:
    artifacts = (
        _member(_REPORT_TYPE, _REPORT_TYPE, resolved.request, report),
        *(_member("recipe", _RECIPE_PREFIX + item.story.story_id, resolved.request, item) for item in recipes),
        _member(_ADMISSION_TYPE, _ADMISSION_TYPE, resolved.request, admission),
    )
    limits = resolved.resolved_parent.request.compilation_limits
    if any(len(item.payload_json.encode("utf-8")) > limits.max_member_payload_bytes for item in artifacts) or sum(len(item.payload_json.encode("utf-8")) for item in artifacts) > limits.max_total_payload_bytes:
        raise _ApplyFailureError(EDITED_RECIPE_COMPILATION_BLOCKED, "edited Recipe payload exceeds frozen compilation limits")
    return artifacts


def rebuild_applied_edit_artifacts(
    resolved: ResolvedApplyEditProposalRequest,
) -> RebuiltAppliedEditArtifacts:
    """Rebuild the edited report, Recipes and Admission from committed parents."""
    if type(resolved) is not ResolvedApplyEditProposalRequest:  # noqa: E721
        raise ApplyEditProposalError("edited Recipe rebuild requires an exact resolution")
    report, recipes, admission, verified = _compile_and_admit(resolved)
    return RebuiltAppliedEditArtifacts(
        report, recipes, admission, verified, _artifacts(resolved, report, recipes, admission)
    )


def _capability_payload(value: _VerifiedEditedRecipeCommit) -> bytes:
    return canonical_json_bytes({
        "request_sha256": value.resolved.request_hash,
        "proposal_sha256": value.request.proposal.canonical_hash,
        "command_slot_id": str(value.success.command_slot_id), "set_hash": value.success.set_hash,
        "verification_binding_sha256": value.admission.verification_binding_sha256,
        "report_sha256": value.admission.report.canonical_hash,
        "admission_sha256": value.admission.admission.canonical_hash,
        "recipe_subjects": [item.to_mapping() for item in value.admission.recipe_subjects],
    })


def _validate_verified_edited_recipe_success(
    request: ApplyEditProposalRequest,
    success: CommandSuccess,
    verified: VerifiedPhysicalEditAdmission,
) -> None:
    artifacts = success.artifacts
    if (
        not verified.render_authorized
        or len(artifacts) != len(verified.recipe_subjects) + 2
        or success.set_hash != artifact_set_hash(artifacts)
    ):
        raise ApplyEditProposalError("edited Recipe success is not an admitted artifact closure")
    if (
        artifacts[0].artifact_type != _REPORT_TYPE
        or artifacts[0].logical_id != _REPORT_TYPE
        or artifacts[0].revision != request.artifact_revision
        or artifacts[0].scope != request.artifact_scope
        or artifacts[0].content_hash != verified.report.canonical_hash
        or artifacts[-1].artifact_type != _ADMISSION_TYPE
        or artifacts[-1].logical_id != _ADMISSION_TYPE
        or artifacts[-1].revision != request.artifact_revision
        or artifacts[-1].scope != request.artifact_scope
        or artifacts[-1].content_hash != verified.admission.canonical_hash
    ):
        raise ApplyEditProposalError("edited Recipe report or Admission differs from verified closure")
    for artifact, subject in zip(artifacts[1:-1], verified.recipe_subjects, strict=True):
        if (
            artifact.artifact_type != subject.artifact_type
            or artifact.logical_id != subject.logical_id
            or artifact.revision != subject.revision
            or artifact.scope != subject.scope
            or artifact.content_hash != subject.content_hash
            or canonical_payload_hash(artifact.payload_json) != artifact.content_hash
        ):
            raise ApplyEditProposalError("edited Recipe member differs from verified closure")


def _issue_verified_edited_recipe_commit(
    request: ApplyEditProposalRequest,
    resolved: ResolvedApplyEditProposalRequest,
    success: CommandSuccess,
    verified: VerifiedPhysicalEditAdmission,
) -> _VerifiedEditedRecipeCommit:
    _validate_verified_edited_recipe_success(request, success, verified)
    unsigned = _VerifiedEditedRecipeCommit(request, resolved, success, verified, b"\x00" * 32)
    return replace(unsigned, _verification_mac=hmac.digest(_EDITED_RECIPE_COMMIT_KEY, _capability_payload(unsigned), "sha256"))


def _open_verified_edited_recipe_commit(
    value: object,
) -> CommandSuccess:
    """Open the only Store-accepted edited Recipe success capability."""
    if type(value) is not _VerifiedEditedRecipeCommit:  # noqa: E721
        raise ApplyEditProposalError("edited Recipe persistence requires a verified commit capability")
    expected = hmac.digest(_EDITED_RECIPE_COMMIT_KEY, _capability_payload(value), "sha256")
    if not hmac.compare_digest(value._verification_mac, expected):  # pyright: ignore[reportPrivateUsage]
        raise ApplyEditProposalError("edited Recipe commit capability was modified")
    _validate_verified_edited_recipe_success(value.request, value.success, value.admission)
    return value.success


def _reject(
    store: ApplyEditProposalStore,
    outcome: CommandOutcome,
    failure: _ApplyFailureError,
) -> CommandOutcome:
    return store.commit_command_rejection(CommandRejection(outcome.command_slot_id, failure.code, canonical_json_bytes({"reason": str(failure)}).decode("utf-8"), outcome=cast(object, failure.outcome)))


class ApplyEditProposalCommand:
    def __init__(self, store: ApplyEditProposalStore, authority_profile_resolver: AuthorityResolver, limits: TimedMediaReadLimits) -> None:
        self._store = store
        self._resolver = authority_profile_resolver
        self._limits = limits

    def execute(self, request: ApplyEditProposalRequest) -> ApplyEditProposalResult:
        resolved = resolve_apply_edit_proposal_request(self._store, request, authority_profile_resolver=self._resolver, limits=self._limits)
        claimed = self._store.claim_command(CommandClaim(request.job, request.proposal.idempotency_key, APPLY_EDIT_PROPOSAL_COMMAND, resolved.request_hash, execution_kind="deterministic"))
        if not claimed.is_fresh_claim:
            return ApplyEditProposalResult(claimed, read_committed_edited_production_recipe_set(self._store, request, claimed, authority_profile_resolver=self._resolver, limits=self._limits) if claimed.state == "succeeded" else None)
        try:
            rebuilt = rebuild_applied_edit_artifacts(resolved)
        except _ApplyFailureError as error:
            return ApplyEditProposalResult(_reject(self._store, claimed, error))
        except Exception:
            return ApplyEditProposalResult(_reject(self._store, claimed, _ApplyFailureError(EDITED_RECIPE_INFRASTRUCTURE_FAILED, "edited Recipe compilation infrastructure failed", outcome="failed")))
        success = CommandSuccess(
            claimed.command_slot_id, artifact_set_hash(rebuilt.artifacts), rebuilt.artifacts
        )
        try:
            committed = self._store.commit_apply_edit_proposal_success(
                request,
                _issue_verified_edited_recipe_commit(request, resolved, success, rebuilt.verified),
                authority_profile_resolver=self._resolver,
                limits=self._limits,
            )
        except StaleHeadError as error:
            return ApplyEditProposalResult(
                _reject(
                    self._store,
                    claimed,
                    _ApplyFailureError(EDITED_RECIPE_STALE_PARENT, str(error)),
                )
            )
        if committed.state != "succeeded":
            return ApplyEditProposalResult(committed)
        return ApplyEditProposalResult(
            committed,
            read_committed_edited_production_recipe_set(
                self._store,
                request,
                committed,
                authority_profile_resolver=self._resolver,
                limits=self._limits,
            ),
        )


def read_committed_edited_production_recipe_set(store: ApplyEditProposalStore, request: ApplyEditProposalRequest, outcome: CommandOutcome, *, authority_profile_resolver: AuthorityResolver, limits: TimedMediaReadLimits) -> PersistedProductionRecipeSet:
    """Reread a committed edit and compare it to a fresh closed reconstruction."""
    _succeeded(outcome, "edited Recipe")
    resolved = resolve_apply_edit_proposal_request(store, request, authority_profile_resolver=authority_profile_resolver, limits=limits)
    record = store.read_committed_artifact_set(request.job, command_slot_id=outcome.command_slot_id, receipt_id=cast(UUID, outcome.receipt_id), artifact_set_id=cast(UUID, outcome.artifact_set_id), expected_request_hash=resolved.request_hash, expected_command_name=APPLY_EDIT_PROPOSAL_COMMAND, expected_execution_kind="deterministic")
    if type(record) is not PersistedCommittedArtifactSet or record.job != request.job or record.job_id != outcome.job_id or record.command_slot_id != outcome.command_slot_id or record.receipt_id != outcome.receipt_id or record.artifact_set_id != outcome.artifact_set_id or record.request_hash != resolved.request_hash or record.command_name != APPLY_EDIT_PROPOSAL_COMMAND or record.execution_kind != "deterministic":  # noqa: E501
        raise ApplyEditProposalError("edited Recipe Store record differs from exact command identity")
    rebuilt = rebuild_applied_edit_artifacts(resolved)
    report, recipes, admission, expected = (
        rebuilt.report,
        rebuilt.recipes,
        rebuilt.admission,
        rebuilt.artifacts,
    )
    try:
        decoded = tuple(load_canonical_json_bytes(item.payload_json.encode("utf-8"), origin="edited Recipe member") for item in record.members)
        stored_report = PhysicalEditCompilationReport.from_mapping(decoded[0][0])
        stored_recipes = tuple(ProductionRecipe.from_mapping(item[0]) for item in decoded[1:-1])
        stored_admission = PhysicalEditAdmission.from_mapping(decoded[-1][0])
    except (IndexError, TypeError, ValueError, UnicodeError) as error:
        raise ApplyEditProposalError("edited Recipe member codec is invalid") from error
    if (
        len(record.members) != len(expected)
        or tuple(item.reference.member_ordinal for item in record.members) != tuple(range(len(expected)))
        or tuple(item.reference.artifact_type for item in record.members) != (_REPORT_TYPE, *("recipe" for _ in recipes), _ADMISSION_TYPE)
        or any(item.reference.scope != request.artifact_scope or item.reference.revision != request.artifact_revision or item.reference.content_hash != artifact.content_hash or canonical != artifact.payload_json.encode("utf-8") for item, artifact, (_mapping, canonical) in zip(record.members, expected, decoded, strict=True))
        or record.set_hash != artifact_set_hash(expected)
        or stored_report != report or stored_recipes != recipes or stored_admission != admission
    ):
        raise ApplyEditProposalError("edited Recipe committed layout, payload, or set hash differs from replay")
    verified = verify_physical_edit_admission(stored_admission, report=report, recipe_subjects=_subjects(request, recipes), expected_job_scope=request.artifact_scope, expected_input_binding_sha256=resolved.request_hash, expected_authority_sha256=resolved.resolved_parent.authority.original_authority_sha256, expected_editorial_exact_policy_sha256=resolved.resolved_parent.request.editorial_exact_span_policy.canonical_hash, expected_candidate_exact_policy_sha256=resolved.resolved_parent.request.candidate_exact_span_policy.canonical_hash, expected_stage3_member_refs=resolved.resolved_parent.joined.editorial.record.references, expected_media_batch_member_ref=resolved.resolved_parent.media_batch_member_ref, expected_timed_media_child_member_refs=resolved.resolved_parent.joined.media_batch.child_member_references, frozen_choice_order=_frozen_choice_order(resolved.resolved_parent), replay_evidence=_replay_evidence(report, _subjects(request, recipes)))
    return PersistedProductionRecipeSet(record, report, recipes, verified)


__all__ = (
    "APPLY_EDIT_PROPOSAL_COMMAND", "APPLY_EDIT_PROPOSAL_STRATEGY",
    "EDITED_RECIPE_COMPILATION_BLOCKED", "EDITED_RECIPE_INFRASTRUCTURE_FAILED",
    "EDITED_RECIPE_STALE_PARENT",
    "ApplyEditProposalCommand", "ApplyEditProposalError", "ApplyEditProposalRequest",
    "ApplyEditProposalResult", "ApplyEditProposalStore", "ResolvedApplyEditProposalRequest",
    "RebuiltAppliedEditArtifacts", "read_committed_edited_production_recipe_set",
    "rebuild_applied_edit_artifacts", "resolve_apply_edit_proposal_request",
)
