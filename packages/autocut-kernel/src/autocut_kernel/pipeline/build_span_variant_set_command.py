"""Persist one deterministic, zero-provider child of an exact Stage 4 Recipe set."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, Protocol, cast
from uuid import UUID

from ..contracts.compiler.canonical import (
    canonical_json_bytes,
    canonical_json_hash,
    load_canonical_json_bytes,
)
from ..media.types import sha256_prefixed
from ..physical_edit.candidate_dialogue_guard import (
    DialogueGuardError,
    DialogueGuardIndeterminateError,
)
from ..physical_edit.candidate_exact_span import compile_candidate_av_span_variants
from ..physical_edit.exact_span import (
    CandidatePairLimitError,
    ExactSpanValidationError,
    NoLegalSpanError,
)
from ..physical_edit.span_variant_set import (
    SpanVariantEntry,
    SpanVariantSet,
    SpanVariantSetPolicy,
    decode_span_variant_set_json,
    encode_span_variant_set_json,
)
from ..store.models import (
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
from .committed_timed_media import TimedMediaReadLimits
from .compile_production_recipe_command import (
    AuthorityResolver,
    CompileProductionRecipeError,
    CompileProductionRecipeRequest,
    PersistedProductionRecipeSet,
    ProductionRecipeCommandStore,
    ResolvedCompileProductionRecipeRequest,
    _media_parts,
    read_committed_production_recipe_set,
    resolve_compile_production_recipe_request,
)

BUILD_SPAN_VARIANT_SET_COMMAND: Final = "BuildSpanVariantSetCommand@1"
BUILD_SPAN_VARIANT_SET_STRATEGY: Final = "build-span-variant-set-v1"
SPAN_VARIANT_SET_ARTIFACT_TYPE: Final = "span_variant_set"
SPAN_VARIANT_SET_LOGICAL_ID: Final = "span_variant_set"
MAX_SPAN_VARIANT_SET_PAYLOAD_BYTES: Final = 8 * 1024 * 1024

SPAN_VARIANT_SET_COMPILATION_BLOCKED: Final = "SPAN_VARIANT_SET_COMPILATION_BLOCKED"
SPAN_VARIANT_SET_PAYLOAD_TOO_LARGE: Final = "SPAN_VARIANT_SET_PAYLOAD_TOO_LARGE"
SPAN_VARIANT_SET_INFRASTRUCTURE_FAILED: Final = "SPAN_VARIANT_SET_INFRASTRUCTURE_FAILED"

_MAX_EXACT_JSON_INTEGER: Final = 2**53 - 1


class BuildSpanVariantSetError(ValueError):
    """The child request or its exact persisted closure is invalid."""


class _BuildFailureError(BuildSpanVariantSetError):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        outcome: Literal["denied", "failed"] = "denied",
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.outcome = outcome


class SpanVariantSetCommandStore(ProductionRecipeCommandStore, Protocol):
    def commit_span_variant_set_success(
        self,
        request: BuildSpanVariantSetRequest,
        success: CommandSuccess,
        *,
        authority_profile_resolver: AuthorityResolver,
        limits: TimedMediaReadLimits,
    ) -> CommandOutcome: ...


def _succeeded_outcome_mapping(outcome: CommandOutcome) -> dict[str, object]:
    if (
        type(outcome) is not CommandOutcome  # noqa: E721
        or outcome.state != "succeeded"
        or any(
            type(value) is not UUID  # noqa: E721
            for value in (
                outcome.command_slot_id,
                outcome.job_id,
                outcome.receipt_id,
                outcome.artifact_set_id,
            )
        )
        or outcome.failure_code is not None
        or outcome.failure_detail_json is not None
    ):
        raise BuildSpanVariantSetError("span variant parent outcome must be exact and succeeded")
    return {
        "artifact_set_id": str(outcome.artifact_set_id),
        "command_slot_id": str(outcome.command_slot_id),
        "job_id": str(outcome.job_id),
        "receipt_id": str(outcome.receipt_id),
        "state": "succeeded",
    }


@dataclass(frozen=True, slots=True)
class BuildSpanVariantSetRequest:
    job: Job
    idempotency_key: str
    artifact_scope: ArtifactScope
    artifact_revision: int
    parent_request: CompileProductionRecipeRequest
    parent_outcome: CommandOutcome
    expected_parent_request_hash: str
    expected_parent_set_hash: str
    parent_report_ref: CommittedArtifactMemberReference
    parent_recipe_refs: tuple[CommittedArtifactMemberReference, ...]
    parent_admission_ref: CommittedArtifactMemberReference
    variant_policy: SpanVariantSetPolicy

    def __post_init__(self) -> None:
        if type(self.job) is not Job or self.artifact_scope != canonical_recipe_scope(self.job):  # noqa: E721
            raise BuildSpanVariantSetError("span variant command requires the canonical Job scope")
        if (
            type(self.idempotency_key) is not str  # noqa: E721
            or not self.idempotency_key
            or self.idempotency_key != self.idempotency_key.strip()
        ):
            raise BuildSpanVariantSetError("span variant idempotency key must be canonical text")
        if (
            type(self.artifact_revision) is not int  # noqa: E721
            or not 1 <= self.artifact_revision <= _MAX_EXACT_JSON_INTEGER
        ):
            raise BuildSpanVariantSetError("span variant revision must be a positive exact integer")
        if (
            type(self.parent_request) is not CompileProductionRecipeRequest  # noqa: E721
            or self.parent_request.job != self.job
            or self.parent_request.artifact_scope != self.artifact_scope
            or self.parent_request.artifact_revision != self.artifact_revision
        ):
            raise BuildSpanVariantSetError(
                "span variant request requires the complete same-Job parent request"
            )
        if self.idempotency_key == self.parent_request.idempotency_key:
            raise BuildSpanVariantSetError(
                "span variant command requires an independent idempotency key"
            )
        _succeeded_outcome_mapping(self.parent_outcome)
        try:
            sha256_prefixed(
                self.expected_parent_request_hash,
                "expected_parent_request_hash",
            )
            sha256_prefixed(
                self.expected_parent_set_hash,
                "expected_parent_set_hash",
            )
        except ValueError as error:
            raise BuildSpanVariantSetError(str(error)) from error
        if type(self.variant_policy) is not SpanVariantSetPolicy:  # noqa: E721
            raise BuildSpanVariantSetError("span variant policy must be exact")
        refs = self.parent_refs
        if (
            type(self.parent_recipe_refs) is not tuple  # noqa: E721
            or not self.parent_recipe_refs
            or any(type(item) is not CommittedArtifactMemberReference for item in refs)  # noqa: E721
        ):
            raise BuildSpanVariantSetError(
                "span variant parent references must be exact and non-empty"
            )
        outcome = self.parent_outcome
        owner = (cast(UUID, outcome.receipt_id), cast(UUID, outcome.artifact_set_id))
        if (
            tuple(item.member_ordinal for item in refs) != tuple(range(len(refs)))
            or any((item.receipt_id, item.artifact_set_id) != owner for item in refs)
            or any(
                item.scope != self.parent_request.artifact_scope
                or item.revision != self.parent_request.artifact_revision
                for item in refs
            )
            or self.parent_report_ref.artifact_type != "physical_edit_compilation_report"
            or self.parent_report_ref.logical_id != "physical_edit_compilation_report"
            or any(item.artifact_type != "recipe" for item in self.parent_recipe_refs)
            or any(
                not item.logical_id.startswith("production_recipe@")
                for item in self.parent_recipe_refs
            )
            or self.parent_admission_ref.artifact_type != "physical_edit_admission"
            or self.parent_admission_ref.logical_id != "physical_edit_admission"
        ):
            raise BuildSpanVariantSetError("span variant parent member layout or owner is invalid")

    @property
    def parent_refs(self) -> tuple[CommittedArtifactMemberReference, ...]:
        return (
            self.parent_report_ref,
            *self.parent_recipe_refs,
            self.parent_admission_ref,
        )


@dataclass(frozen=True, slots=True)
class ResolvedBuildSpanVariantSetRequest:
    request: BuildSpanVariantSetRequest
    parent: PersistedProductionRecipeSet
    resolved_parent: ResolvedCompileProductionRecipeRequest
    request_hash: str


@dataclass(frozen=True, slots=True)
class PersistedSpanVariantSet:
    record: PersistedCommittedArtifactSet
    value: SpanVariantSet

    def __post_init__(self) -> None:
        if type(self.record) is not PersistedCommittedArtifactSet:  # noqa: E721
            raise BuildSpanVariantSetError(
                "persisted span variant set requires an exact Store record"
            )
        if type(self.value) is not SpanVariantSet:  # noqa: E721
            raise BuildSpanVariantSetError("persisted span variant set requires an exact value")


@dataclass(frozen=True, slots=True)
class BuildSpanVariantSetResult:
    outcome: CommandOutcome
    committed: PersistedSpanVariantSet | None = None


def _parent_identity_mapping(request: BuildSpanVariantSetRequest) -> dict[str, object]:
    return {
        "outcome": _succeeded_outcome_mapping(request.parent_outcome),
        "request_hash": request.expected_parent_request_hash,
        "set_hash": request.expected_parent_set_hash,
        "member_refs": [item.to_mapping() for item in request.parent_refs],
    }


def _request_hash(request: BuildSpanVariantSetRequest) -> str:
    return canonical_json_hash(
        {
            "strategy_version": BUILD_SPAN_VARIANT_SET_STRATEGY,
            "provider_call_budget": 0,
            "job": {
                "job_key": request.job.job_key,
                "profile": request.job.profile,
            },
            "artifact_scope": {
                "namespace": request.artifact_scope.namespace,
                "kind": request.artifact_scope.kind,
                "key": request.artifact_scope.key,
            },
            "artifact_revision": request.artifact_revision,
            "parent": _parent_identity_mapping(request),
            "variant_policy": request.variant_policy.to_mapping(),
        }
    )


def resolve_build_span_variant_set_request(
    store: SpanVariantSetCommandStore,
    request: BuildSpanVariantSetRequest,
    *,
    authority_profile_resolver: AuthorityResolver,
    limits: TimedMediaReadLimits,
) -> ResolvedBuildSpanVariantSetRequest:
    """Validate and independently replay the complete parent before claim."""
    if type(request) is not BuildSpanVariantSetRequest:  # noqa: E721
        raise BuildSpanVariantSetError("span variant request must be exact")
    _succeeded_outcome_mapping(request.parent_outcome)
    try:
        parent = read_committed_production_recipe_set(
            store,
            request.parent_request,
            request.parent_outcome,
            authority_profile_resolver=authority_profile_resolver,
            limits=limits,
        )
        resolved_parent = resolve_compile_production_recipe_request(
            store,
            request.parent_request,
            authority_profile_resolver=authority_profile_resolver,
            limits=limits,
        )
    except CompileProductionRecipeError as error:
        raise BuildSpanVariantSetError("span variant parent Recipe closure is invalid") from error
    record = parent.record
    if (
        record.request_hash != request.expected_parent_request_hash
        or resolved_parent.request_hash != request.expected_parent_request_hash
        or record.set_hash != request.expected_parent_set_hash
        or record.references != request.parent_refs
        or record.job != request.job
        or record.job_id != request.parent_outcome.job_id
        or record.command_slot_id != request.parent_outcome.command_slot_id
        or record.receipt_id != request.parent_outcome.receipt_id
        or record.artifact_set_id != request.parent_outcome.artifact_set_id
    ):
        raise BuildSpanVariantSetError(
            "span variant request differs from the exact parent Recipe set"
        )
    return ResolvedBuildSpanVariantSetRequest(
        request,
        parent,
        resolved_parent,
        _request_hash(request),
    )


def rebuild_span_variant_set(
    resolved: ResolvedBuildSpanVariantSetRequest,
) -> SpanVariantSet:
    """Independently enumerate every retained child variant from frozen parent inputs."""
    if type(resolved) is not ResolvedBuildSpanVariantSetRequest:  # noqa: E721
        raise BuildSpanVariantSetError("span variant rebuild requires an exact resolution")
    parent = resolved.parent
    inputs = resolved.resolved_parent
    entries: list[SpanVariantEntry] = []
    try:
        for entry in parent.report.entries:
            root, plans, candidates, clock = _media_parts(inputs, entry.episode_ordinal)
            if entry.candidate_ordinal >= len(plans) or entry.candidate_ordinal >= len(candidates):
                raise BuildSpanVariantSetError(
                    "span variant parent candidate escapes exact media census"
                )
            results = compile_candidate_av_span_variants(
                entry.selected_query.request,
                root,
                candidates[entry.candidate_ordinal],
                plans[entry.candidate_ordinal],
                inputs.authority,
                clock,
                inputs.request.candidate_exact_span_policy,
                max_variants=resolved.request.variant_policy.max_variants,
            )
            if not results or results[0] != entry.selected_result:
                raise BuildSpanVariantSetError(
                    "span variant ordinal zero differs from parent selected result"
                )
            entries.append(
                SpanVariantEntry.from_results(
                    ordinal=entry.ordinal,
                    story_id=entry.story_id,
                    beat_id=entry.beat_id,
                    requirement_id=entry.requirement_id,
                    alternative_id=entry.alternative_id,
                    candidate_id=entry.candidate_id,
                    query=entry.selected_query,
                    results=results,
                )
            )
    except BuildSpanVariantSetError:
        raise
    except (
        CandidatePairLimitError,
        DialogueGuardError,
        DialogueGuardIndeterminateError,
        ExactSpanValidationError,
        NoLegalSpanError,
        CompileProductionRecipeError,
        ValueError,
    ) as error:
        raise BuildSpanVariantSetError("span variant enumeration failed closed") from error
    return SpanVariantSet(
        resolved.request.variant_policy,
        resolved.request.expected_parent_request_hash,
        resolved.request.expected_parent_set_hash,
        tuple(entries),
    )


def build_span_variant_set_artifact(
    request: BuildSpanVariantSetRequest,
    value: SpanVariantSet,
) -> ArtifactMember:
    payload = encode_span_variant_set_json(value)
    if len(payload) > MAX_SPAN_VARIANT_SET_PAYLOAD_BYTES:
        raise _BuildFailureError(
            SPAN_VARIANT_SET_PAYLOAD_TOO_LARGE,
            "span variant canonical payload exceeds 8 MiB",
        )
    return ArtifactMember(
        SPAN_VARIANT_SET_ARTIFACT_TYPE,
        SPAN_VARIANT_SET_LOGICAL_ID,
        request.artifact_revision,
        request.artifact_scope,
        canonical_payload_hash(payload.decode("utf-8", errors="strict")),
        payload.decode("utf-8", errors="strict"),
    )


def read_committed_span_variant_set(
    store: SpanVariantSetCommandStore,
    request: BuildSpanVariantSetRequest,
    outcome: CommandOutcome,
    *,
    authority_profile_resolver: AuthorityResolver,
    limits: TimedMediaReadLimits,
) -> PersistedSpanVariantSet:
    """Read the exact child and independently rebuild its immutable payload."""
    _succeeded_outcome_mapping(outcome)
    resolved = resolve_build_span_variant_set_request(
        store,
        request,
        authority_profile_resolver=authority_profile_resolver,
        limits=limits,
    )
    expected_value = rebuild_span_variant_set(resolved)
    expected_artifact = build_span_variant_set_artifact(request, expected_value)
    record = store.read_committed_artifact_set(
        request.job,
        command_slot_id=outcome.command_slot_id,
        receipt_id=cast(UUID, outcome.receipt_id),
        artifact_set_id=cast(UUID, outcome.artifact_set_id),
        expected_request_hash=resolved.request_hash,
        expected_command_name=BUILD_SPAN_VARIANT_SET_COMMAND,
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
        or record.command_name != BUILD_SPAN_VARIANT_SET_COMMAND
        or record.execution_kind != "deterministic"
        or len(record.members) != 1
    ):
        raise BuildSpanVariantSetError(
            "span variant Store record differs from exact command identity"
        )
    member = record.members[0]
    raw = member.payload_json.encode("utf-8", errors="strict")
    try:
        decoded_mapping, canonical = load_canonical_json_bytes(
            raw,
            origin="span variant set member",
        )
        stored_value = decode_span_variant_set_json(
            canonical,
            max_bytes=MAX_SPAN_VARIANT_SET_PAYLOAD_BYTES,
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise BuildSpanVariantSetError("span variant member codec is invalid") from error
    if (
        len(canonical) > MAX_SPAN_VARIANT_SET_PAYLOAD_BYTES
        or decoded_mapping != expected_value.to_mapping()
        or stored_value != expected_value
        or member.reference.member_ordinal != 0
        or member.reference.artifact_type != expected_artifact.artifact_type
        or member.reference.logical_id != expected_artifact.logical_id
        or member.reference.revision != expected_artifact.revision
        or member.reference.scope != expected_artifact.scope
        or member.reference.content_hash != expected_artifact.content_hash
        or canonical_payload_hash(member.payload_json) != expected_artifact.content_hash
        or canonical != expected_artifact.payload_json.encode("utf-8")
        or record.set_hash != artifact_set_hash((expected_artifact,))
    ):
        raise BuildSpanVariantSetError(
            "span variant committed payload differs from independent rebuild"
        )
    return PersistedSpanVariantSet(record, stored_value)


def _reject(
    store: SpanVariantSetCommandStore,
    outcome: CommandOutcome,
    failure: _BuildFailureError,
) -> CommandOutcome:
    return store.commit_command_rejection(
        CommandRejection(
            outcome.command_slot_id,
            failure.code,
            canonical_json_bytes({"reason": str(failure)}).decode("utf-8"),
            outcome=failure.outcome,
        )
    )


class BuildSpanVariantSetCommand:
    def __init__(
        self,
        store: SpanVariantSetCommandStore,
        authority_profile_resolver: AuthorityResolver,
        limits: TimedMediaReadLimits,
    ) -> None:
        self._store = store
        self._resolver = authority_profile_resolver
        self._limits = limits

    def execute(self, request: BuildSpanVariantSetRequest) -> BuildSpanVariantSetResult:
        # Parent failure/mismatch is an input error and must not consume a child slot.
        resolved = resolve_build_span_variant_set_request(
            self._store,
            request,
            authority_profile_resolver=self._resolver,
            limits=self._limits,
        )
        claimed = self._store.claim_command(
            CommandClaim(
                request.job,
                request.idempotency_key,
                BUILD_SPAN_VARIANT_SET_COMMAND,
                resolved.request_hash,
                execution_kind="deterministic",
            )
        )
        if not claimed.is_fresh_claim:
            if claimed.state == "succeeded":
                return BuildSpanVariantSetResult(
                    claimed,
                    read_committed_span_variant_set(
                        self._store,
                        request,
                        claimed,
                        authority_profile_resolver=self._resolver,
                        limits=self._limits,
                    ),
                )
            return BuildSpanVariantSetResult(claimed)
        try:
            value = rebuild_span_variant_set(resolved)
            artifact = build_span_variant_set_artifact(request, value)
        except _BuildFailureError as error:
            return BuildSpanVariantSetResult(_reject(self._store, claimed, error))
        except BuildSpanVariantSetError as error:
            failure = _BuildFailureError(SPAN_VARIANT_SET_COMPILATION_BLOCKED, str(error))
            return BuildSpanVariantSetResult(_reject(self._store, claimed, failure))
        except Exception:
            failure = _BuildFailureError(
                SPAN_VARIANT_SET_INFRASTRUCTURE_FAILED,
                "span variant compilation infrastructure failed",
                outcome="failed",
            )
            return BuildSpanVariantSetResult(_reject(self._store, claimed, failure))
        success = CommandSuccess(
            claimed.command_slot_id,
            artifact_set_hash((artifact,)),
            (artifact,),
        )
        committed = self._store.commit_span_variant_set_success(
            request,
            success,
            authority_profile_resolver=self._resolver,
            limits=self._limits,
        )
        return BuildSpanVariantSetResult(
            committed,
            read_committed_span_variant_set(
                self._store,
                request,
                committed,
                authority_profile_resolver=self._resolver,
                limits=self._limits,
            ),
        )


__all__ = (
    "BUILD_SPAN_VARIANT_SET_COMMAND",
    "BUILD_SPAN_VARIANT_SET_STRATEGY",
    "MAX_SPAN_VARIANT_SET_PAYLOAD_BYTES",
    "SPAN_VARIANT_SET_ARTIFACT_TYPE",
    "SPAN_VARIANT_SET_COMPILATION_BLOCKED",
    "SPAN_VARIANT_SET_INFRASTRUCTURE_FAILED",
    "SPAN_VARIANT_SET_LOGICAL_ID",
    "SPAN_VARIANT_SET_PAYLOAD_TOO_LARGE",
    "BuildSpanVariantSetCommand",
    "BuildSpanVariantSetError",
    "BuildSpanVariantSetRequest",
    "BuildSpanVariantSetResult",
    "PersistedSpanVariantSet",
    "ResolvedBuildSpanVariantSetRequest",
    "SpanVariantSetCommandStore",
    "build_span_variant_set_artifact",
    "read_committed_span_variant_set",
    "rebuild_span_variant_set",
    "resolve_build_span_variant_set_request",
)
