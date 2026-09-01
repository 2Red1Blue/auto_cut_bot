"""Commit and exactly reread one deterministic V23 candidate decision set.

The semantic-input dependency is the existing complete committed V4 aggregate.
This command deliberately does not claim selected-only child support: caller
hashes select within that aggregate, while the Store reread remains authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from ..media import (
    V23CandidateDecisionSet,
    V23CandidateWindowCompilePolicy,
    compile_v23_candidate_decision_set,
    decode_v23_candidate_decision_set_json,
    verify_v23_candidate_decision_set,
)
from ..media.root_evidence import FramePtsIndexSet
from ..media.types import MediaValidationError, sha256_prefixed
from ..source_manifest import (
    DecodedSourceManifest,
    SourceManifestDecodeError,
    decode_source_manifest,
)
from ..store.models import (
    VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4,
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    CommittedArtifactMemberReference,
    CommittedSemanticInputs,
    CommittedSemanticInputsRequest,
    CommittedVlmSemanticInput,
    Job,
    PersistedCommittedArtifactSet,
    PersistedVlmSemanticPackV4,
    artifact_set_hash,
    canonical_payload_hash,
    canonical_recipe_scope,
)
from ..vlm.window import WindowManifest

COMPILE_V23_CANDIDATE_DECISION_SET_COMMAND = "CompileV23CandidateDecisionSet@1"
V23_CANDIDATE_DECISION_SET_COMMAND_STRATEGY = "compile-v23-candidate-decision-set-v1"
V23_CANDIDATE_DECISION_SET_INVALID = "V23_CANDIDATE_DECISION_SET_INVALID"
V23_CANDIDATE_DECISION_SET_INFRASTRUCTURE_FAILED = (
    "V23_CANDIDATE_DECISION_SET_INFRASTRUCTURE_FAILED"
)
_MAX_EXACT_JSON_INTEGER = 2**53 - 1
_JSONB_READ_FIXED_ALLOWANCE_BYTES = 4_096


class CompileV23CandidateDecisionSetError(ValueError):
    """The request or persisted closure is not the exact committed V4 aggregate."""


class _DeterministicV23DenialError(CompileV23CandidateDecisionSetError):
    """A repeat with the exact same committed inputs cannot change this result."""


class V23CandidateDecisionSetStore(Protocol):
    def read_committed_semantic_inputs(
        self, request: CommittedSemanticInputsRequest
    ) -> CommittedSemanticInputs: ...

    def claim_command(self, claim: CommandClaim) -> CommandOutcome: ...

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome: ...

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome: ...

    def read_committed_artifact_set(
        self,
        job: Job,
        *,
        command_slot_id: UUID,
        receipt_id: UUID,
        artifact_set_id: UUID,
        expected_request_hash: str,
        expected_command_name: str,
        expected_execution_kind: str,
    ) -> PersistedCommittedArtifactSet: ...


def _sha(value: object, field_name: str) -> str:
    try:
        return sha256_prefixed(value, field_name)
    except MediaValidationError as error:
        raise CompileV23CandidateDecisionSetError(str(error)) from error


def _scope_mapping(scope: ArtifactScope) -> dict[str, str]:
    return {"key": scope.key, "kind": scope.kind, "namespace": scope.namespace}


def _job_mapping(job: Job) -> dict[str, str]:
    return {"job_key": job.job_key, "profile": job.profile}


def _semantic_request_mapping(
    request: CommittedSemanticInputsRequest,
) -> dict[str, object]:
    return {
        "job": _job_mapping(request.job),
        "source_manifest": request.source_manifest.to_mapping(),
        "vlm_semantic_pack_set": request.vlm_semantic_pack_set.to_mapping(),
    }


@dataclass(frozen=True, slots=True)
class CompileV23CandidateDecisionSetRequest:
    """Exact selectors and output policy for one complete committed V4 aggregate."""

    job: Job
    idempotency_key: str
    artifact_scope: ArtifactScope
    artifact_revision: int
    semantic_inputs_request: CommittedSemanticInputsRequest
    episode_index: int
    window_manifest_sha256: str
    semantic_pack_sha256: str
    vlm_request_identity_sha256: str
    compile_policy: V23CandidateWindowCompilePolicy
    max_payload_bytes: int

    def __post_init__(self) -> None:
        if type(self.job) is not Job:  # noqa: E721
            raise CompileV23CandidateDecisionSetError("V23 command requires an exact Job")
        if (
            type(self.idempotency_key) is not str  # noqa: E721
            or not self.idempotency_key
            or self.idempotency_key != self.idempotency_key.strip()
        ):
            raise CompileV23CandidateDecisionSetError(
                "V23 command idempotency_key must be canonical nonempty text"
            )
        if (
            type(self.artifact_scope) is not ArtifactScope  # noqa: E721
            or self.artifact_scope != canonical_recipe_scope(self.job)
        ):
            raise CompileV23CandidateDecisionSetError(
                "V23 command requires the canonical Job artifact scope"
            )
        if (
            type(self.artifact_revision) is not int  # noqa: E721
            or not 1 <= self.artifact_revision <= _MAX_EXACT_JSON_INTEGER
        ):
            raise CompileV23CandidateDecisionSetError(
                "V23 command artifact_revision must be a positive exact JSON integer"
            )
        if (
            type(self.semantic_inputs_request) is not CommittedSemanticInputsRequest  # noqa: E721
            or self.semantic_inputs_request.job != self.job
        ):
            raise CompileV23CandidateDecisionSetError(
                "V23 command requires an exact same-Job committed semantic aggregate request"
            )
        if (
            type(self.episode_index) is not int  # noqa: E721
            or not 0 <= self.episode_index <= _MAX_EXACT_JSON_INTEGER
        ):
            raise CompileV23CandidateDecisionSetError(
                "V23 command episode_index must be a non-negative exact JSON integer"
            )
        for name in (
            "window_manifest_sha256",
            "semantic_pack_sha256",
            "vlm_request_identity_sha256",
        ):
            _sha(getattr(self, name), f"V23 selector {name}")
        if type(self.compile_policy) is not V23CandidateWindowCompilePolicy:  # noqa: E721
            raise CompileV23CandidateDecisionSetError("V23 command compile_policy must be exact")
        if (
            type(self.max_payload_bytes) is not int  # noqa: E721
            or not 1 <= self.max_payload_bytes <= _MAX_EXACT_JSON_INTEGER
        ):
            raise CompileV23CandidateDecisionSetError(
                "V23 command max_payload_bytes must be a positive exact JSON integer"
            )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "artifact_revision": self.artifact_revision,
            "artifact_scope": _scope_mapping(self.artifact_scope),
            "compile_policy": self.compile_policy.to_mapping(),
            "compile_policy_sha256": self.compile_policy.canonical_hash,
            "job": _job_mapping(self.job),
            "max_payload_bytes": self.max_payload_bytes,
            "selectors": {
                "episode_index": self.episode_index,
                "semantic_pack_sha256": self.semantic_pack_sha256,
                "vlm_request_identity_sha256": self.vlm_request_identity_sha256,
                "window_manifest_sha256": self.window_manifest_sha256,
            },
            "semantic_inputs_request": _semantic_request_mapping(self.semantic_inputs_request),
            "strategy_version": V23_CANDIDATE_DECISION_SET_COMMAND_STRATEGY,
        }

    @property
    def request_hash(self) -> str:
        return canonical_json_hash(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ResolvedCompileV23CandidateDecisionSetRequest:
    """Authoritative dependencies selected from the reread committed aggregate."""

    request: CompileV23CandidateDecisionSetRequest
    semantic_inputs: CommittedSemanticInputs
    semantic_input: CommittedVlmSemanticInput
    source_manifest: DecodedSourceManifest
    window_manifest: WindowManifest
    frame_pts_index: FramePtsIndexSet


@dataclass(frozen=True, slots=True)
class PersistedV23CandidateDecisionSet:
    record: PersistedCommittedArtifactSet
    value: V23CandidateDecisionSet


@dataclass(frozen=True, slots=True)
class CompileV23CandidateDecisionSetResult:
    outcome: CommandOutcome
    committed: PersistedV23CandidateDecisionSet | None = None


def _source_reference_matches(
    persisted: CommittedSemanticInputs,
    expected: CommittedArtifactMemberReference,
) -> bool:
    source = persisted.source_manifest
    reference = source.reference
    return (
        source.receipt_id == expected.receipt_id
        and source.artifact_set_id == expected.artifact_set_id
        and expected.member_ordinal == 0
        and reference.scope == expected.scope
        and reference.artifact_type == expected.artifact_type
        and reference.logical_id == expected.logical_id
        and reference.revision == expected.revision
        and reference.content_hash == expected.content_hash
    )


def resolve_compile_v23_candidate_decision_set_request(
    store: V23CandidateDecisionSetStore,
    request: CompileV23CandidateDecisionSetRequest,
) -> ResolvedCompileV23CandidateDecisionSetRequest:
    """Resolve one selector from the exact committed V4 aggregate, without claiming."""

    if type(request) is not CompileV23CandidateDecisionSetRequest:  # noqa: E721
        raise CompileV23CandidateDecisionSetError("V23 command request must be exact")
    semantic = store.read_committed_semantic_inputs(request.semantic_inputs_request)
    if type(semantic) is not CommittedSemanticInputs:  # noqa: E721
        raise CompileV23CandidateDecisionSetError(
            "Store did not return an exact committed semantic aggregate"
        )
    if semantic.vlm_batch_strategy_version != VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4:
        raise CompileV23CandidateDecisionSetError(
            "V23 command requires the exact committed V4 aggregate"
        )
    if any(
        type(item.semantic_pack) is not PersistedVlmSemanticPackV4  # noqa: E721
        for item in semantic.inputs
    ):
        raise CompileV23CandidateDecisionSetError(
            "V23 command requires exact PersistedVlmSemanticPackV4 aggregate children"
        )
    if (
        semantic.vlm_semantic_pack_set != request.semantic_inputs_request.vlm_semantic_pack_set
        or not _source_reference_matches(semantic, request.semantic_inputs_request.source_manifest)
        or semantic.source_manifest.source_job != request.semantic_inputs_request.job
    ):
        raise CompileV23CandidateDecisionSetError(
            "committed V4 aggregate differs from its full requested references"
        )
    try:
        decoded = decode_source_manifest(
            semantic.source_manifest.payload_json,
            semantic.source_manifest.proxy_blobs,
        )
    except (SourceManifestDecodeError, TypeError, ValueError) as error:
        raise CompileV23CandidateDecisionSetError(
            "committed V4 aggregate SourceManifest cannot be decoded exactly"
        ) from error
    if semantic.source_grant != decoded.census:
        raise CompileV23CandidateDecisionSetError(
            "committed V4 aggregate Source grant differs from its SourceManifest"
        )
    expected_episode_indices = tuple(range(len(decoded.episodes)))
    actual_episode_indices = tuple(item.source_window.episode_index for item in semantic.inputs)
    if (
        len(semantic.source_manifest.proxy_blobs) != len(decoded.episodes)
        or actual_episode_indices != expected_episode_indices
        or any(
            item.source_window.window_manifest_sha256
            != decoded.episodes[episode_index].manifest.canonical_hash
            for episode_index, item in enumerate(semantic.inputs)
        )
    ):
        raise CompileV23CandidateDecisionSetError(
            "V23 command requires one ordered committed V4 input for every Source episode"
        )
    matches = tuple(
        item
        for item in semantic.inputs
        if item.source_window.episode_index == request.episode_index
        and item.source_window.window_manifest_sha256 == request.window_manifest_sha256
    )
    if len(matches) != 1:
        raise CompileV23CandidateDecisionSetError(
            "V23 selectors must resolve exactly one committed V4 aggregate input"
        )
    item = matches[0]
    assert type(item.semantic_pack) is PersistedVlmSemanticPackV4
    episode = decoded.episodes[request.episode_index]
    manifest = episode.manifest
    frame_index = manifest.frame_pts_index_set
    persisted_pack = item.semantic_pack
    pack = persisted_pack.semantic_pack
    child = persisted_pack.source_child
    source_window = item.source_window
    source = semantic.source_manifest

    selectors_match = (
        request.window_manifest_sha256
        == manifest.canonical_hash
        == pack.window_manifest_sha256
        == item.request_identity.window_manifest_sha256
        == child.window_manifest_sha256
        and request.semantic_pack_sha256
        == pack.canonical_hash
        == persisted_pack.reference.content_hash
        and request.vlm_request_identity_sha256
        == item.request_identity.canonical_hash
        == pack.request_identity_sha256
        == child.request_identity_sha256
    )
    if not selectors_match:
        raise CompileV23CandidateDecisionSetError(
            "V23 caller selector differs from the authoritative committed V4 closure"
        )
    if (
        child.episode_index != request.episode_index
        or child.source_job != request.semantic_inputs_request.job
        or child.source_manifest_sha256 != source.reference.content_hash
        or child.source_provenance_sha256 != source.canonical_hash
        or child.window_manifest_set_sha256 != episode.manifest_set.canonical_hash
        or source_window.stream_index != manifest.stream_index
        or source_window.core_start_pts != manifest.core_range.start_pts
        or source_window.core_end_pts != manifest.core_range.end_pts
        or source_window.source_id != manifest.source_id
        or source_window.source_sha256 != manifest.source_sha256
        or source_window.source_clock_id != manifest.source_clock_id
        or source_window.window_manifest_set_sha256 != episode.manifest_set.canonical_hash
        or source_window.proxy_blob != semantic.source_manifest.proxy_blobs[request.episode_index]
        or item.request_identity.window_manifest_set_sha256 != episode.manifest_set.canonical_hash
        or item.request_identity.source_id != manifest.source_id
        or item.request_identity.source_sha256 != manifest.source_sha256
        or item.request_identity.source_clock_id != manifest.source_clock_id
    ):
        raise CompileV23CandidateDecisionSetError(
            "committed V4 pack/request/source/window closure is not exact"
        )
    return ResolvedCompileV23CandidateDecisionSetRequest(
        request, semantic, item, decoded, manifest, frame_index
    )


def _logical_id(request: CompileV23CandidateDecisionSetRequest) -> str:
    semantic_identity = canonical_json_hash(
        {
            "compile_policy_sha256": request.compile_policy.canonical_hash,
            "semantic_pack_sha256": request.semantic_pack_sha256,
            "vlm_request_identity_sha256": request.vlm_request_identity_sha256,
            "window_manifest_sha256": request.window_manifest_sha256,
        }
    )
    return f"v23_candidate_decision_set_{semantic_identity[7:]}"


def _artifact(
    resolved: ResolvedCompileV23CandidateDecisionSetRequest,
) -> tuple[ArtifactMember, V23CandidateDecisionSet]:
    request = resolved.request
    persisted = resolved.semantic_input.semantic_pack
    assert type(persisted) is PersistedVlmSemanticPackV4  # resolved above
    try:
        value = compile_v23_candidate_decision_set(
            persisted.semantic_pack,
            resolved.window_manifest,
            resolved.frame_pts_index,
            request.compile_policy,
        )
    except MediaValidationError as error:
        raise _DeterministicV23DenialError(str(error)) from error
    raw = canonical_json_bytes(value.to_mapping())
    if len(raw) > request.max_payload_bytes:
        raise _DeterministicV23DenialError(
            "V23 candidate decision set exceeds the frozen payload byte cap"
        )
    payload_json = raw.decode("utf-8")
    if canonical_payload_hash(payload_json) != value.canonical_hash:
        raise RuntimeError("V23 candidate decision set canonical payload hash differs")
    return (
        ArtifactMember(
            "v23_candidate_decision_set",
            _logical_id(request),
            request.artifact_revision,
            request.artifact_scope,
            value.canonical_hash,
            payload_json,
        ),
        value,
    )


def _reject(
    store: V23CandidateDecisionSetStore,
    claimed: CommandOutcome,
    code: str,
    detail: str,
    *,
    outcome: Literal["denied", "failed"] = "denied",
) -> CommandOutcome:
    failure_detail = canonical_json_bytes({"code": code, "detail": detail}).decode("utf-8")
    return store.commit_command_rejection(
        CommandRejection(
            claimed.command_slot_id,
            code,
            failure_detail,
            outcome=outcome,
        )
    )


class CompileV23CandidateDecisionSetCommand:
    """Compile only after dependency reread and a fresh deterministic claim."""

    def __init__(self, store: V23CandidateDecisionSetStore) -> None:
        self._store = store

    def execute(
        self, request: CompileV23CandidateDecisionSetRequest
    ) -> CompileV23CandidateDecisionSetResult:
        # Re-resolve before every claim, including replay. A terminal Receipt
        # never authorizes consumption after its committed dependencies stop
        # satisfying the exact V4 closure.
        resolved = resolve_compile_v23_candidate_decision_set_request(self._store, request)
        claimed = self._store.claim_command(
            CommandClaim(
                request.job,
                request.idempotency_key,
                COMPILE_V23_CANDIDATE_DECISION_SET_COMMAND,
                request.request_hash,
                execution_kind="deterministic",
            )
        )
        if not claimed.is_fresh_claim:
            return CompileV23CandidateDecisionSetResult(claimed)
        try:
            artifact, _value = _artifact(resolved)
            success = CommandSuccess(
                claimed.command_slot_id,
                artifact_set_hash((artifact,)),
                (artifact,),
            )
        except _DeterministicV23DenialError as error:
            outcome = _reject(
                self._store,
                claimed,
                V23_CANDIDATE_DECISION_SET_INVALID,
                str(error),
            )
            return CompileV23CandidateDecisionSetResult(outcome)
        except Exception:
            outcome = _reject(
                self._store,
                claimed,
                V23_CANDIDATE_DECISION_SET_INFRASTRUCTURE_FAILED,
                "V23 candidate decision set infrastructure failed",
                outcome="failed",
            )
            return CompileV23CandidateDecisionSetResult(outcome)
        # Ambiguous success commit exceptions must propagate.  They are not a
        # new causal rejection and same-key reconciliation owns the outcome.
        committed = self._store.commit_command_success(success)
        persisted = read_committed_v23_candidate_decision_set(self._store, request, committed)
        return CompileV23CandidateDecisionSetResult(committed, persisted)


def read_committed_v23_candidate_decision_set(
    store: V23CandidateDecisionSetStore,
    request: CompileV23CandidateDecisionSetRequest,
    outcome: CommandOutcome,
) -> PersistedV23CandidateDecisionSet:
    """Reread the exact member and independently verify all V4 dependencies."""

    if (
        type(request) is not CompileV23CandidateDecisionSetRequest  # noqa: E721
        or type(outcome) is not CommandOutcome  # noqa: E721
        or outcome.state != "succeeded"
        or type(outcome.is_fresh_claim) is not bool  # noqa: E721
        or any(
            type(value) is not UUID  # noqa: E721
            for value in (
                outcome.job_id,
                outcome.command_slot_id,
                outcome.receipt_id,
                outcome.artifact_set_id,
            )
        )
        or outcome.failure_code is not None
        or outcome.failure_detail_json is not None
    ):
        raise CompileV23CandidateDecisionSetError(
            "V23 exact reader requires a succeeded Job/slot/Receipt/Set identity"
        )
    resolved = resolve_compile_v23_candidate_decision_set_request(store, request)
    assert outcome.receipt_id is not None
    assert outcome.artifact_set_id is not None
    record = store.read_committed_artifact_set(
        request.job,
        command_slot_id=outcome.command_slot_id,
        receipt_id=outcome.receipt_id,
        artifact_set_id=outcome.artifact_set_id,
        expected_request_hash=request.request_hash,
        expected_command_name=COMPILE_V23_CANDIDATE_DECISION_SET_COMMAND,
        expected_execution_kind="deterministic",
    )
    if (
        type(record) is not PersistedCommittedArtifactSet  # noqa: E721
        or record.job != request.job
        or record.job_id != outcome.job_id
        or record.command_slot_id != outcome.command_slot_id
        or record.receipt_id != outcome.receipt_id
        or record.artifact_set_id != outcome.artifact_set_id
        or record.request_hash != request.request_hash
        or record.command_name != COMPILE_V23_CANDIDATE_DECISION_SET_COMMAND
        or record.execution_kind != "deterministic"
        or len(record.members) != 1
    ):
        raise CompileV23CandidateDecisionSetError(
            "V23 committed Store record differs from the exact requested identity"
        )
    member = record.members[0]
    reference = member.reference
    payload_json = member.payload_json
    try:
        actual_payload_hash = canonical_payload_hash(payload_json)
    except ValueError as error:
        raise CompileV23CandidateDecisionSetError(
            "V23 committed member payload hash cannot be verified"
        ) from error
    if (
        reference.member_ordinal != 0
        or reference.artifact_type != "v23_candidate_decision_set"
        or reference.logical_id != _logical_id(request)
        or reference.scope != request.artifact_scope
        or reference.revision != request.artifact_revision
        or reference.content_hash != actual_payload_hash
    ):
        raise CompileV23CandidateDecisionSetError(
            "V23 committed member identity, order, scope, revision, or hash differs"
        )
    raw = payload_json.encode("utf-8", errors="strict")
    # The closed V23 codec rejects floats/exponent notation and emits integers
    # as decimal text. PostgreSQL jsonb can therefore only expand this payload
    # through separator whitespace: at most one byte per comma/colon, hence
    # less than 2x canonical bytes. The fixed allowance keeps tiny caps usable.
    jsonb_read_limit = min(
        _MAX_EXACT_JSON_INTEGER,
        request.max_payload_bytes * 2 + _JSONB_READ_FIXED_ALLOWANCE_BYTES,
    )
    try:
        value = decode_v23_candidate_decision_set_json(
            raw,
            max_bytes=jsonb_read_limit,
        )
    except MediaValidationError as error:
        raise CompileV23CandidateDecisionSetError(
            "V23 committed member is not bounded strict DecisionSet JSON"
        ) from error
    canonical_payload = canonical_json_bytes(value.to_mapping())
    if len(canonical_payload) > request.max_payload_bytes:
        raise CompileV23CandidateDecisionSetError(
            "V23 committed canonical payload exceeds the frozen byte cap"
        )
    if value.canonical_hash != reference.content_hash or record.set_hash != artifact_set_hash(
        (
            ArtifactMember(
                reference.artifact_type,
                reference.logical_id,
                reference.revision,
                reference.scope,
                reference.content_hash,
                payload_json,
            ),
        )
    ):
        raise CompileV23CandidateDecisionSetError(
            "V23 committed semantic payload or ArtifactSet hash differs"
        )
    persisted_pack = resolved.semantic_input.semantic_pack
    assert type(persisted_pack) is PersistedVlmSemanticPackV4
    verified = verify_v23_candidate_decision_set(
        value,
        persisted_pack.semantic_pack,
        resolved.window_manifest,
        resolved.frame_pts_index,
        request.compile_policy,
    )
    return PersistedV23CandidateDecisionSet(record, verified)


__all__ = (
    "COMPILE_V23_CANDIDATE_DECISION_SET_COMMAND",
    "V23_CANDIDATE_DECISION_SET_COMMAND_STRATEGY",
    "V23_CANDIDATE_DECISION_SET_INFRASTRUCTURE_FAILED",
    "V23_CANDIDATE_DECISION_SET_INVALID",
    "CompileV23CandidateDecisionSetCommand",
    "CompileV23CandidateDecisionSetError",
    "CompileV23CandidateDecisionSetRequest",
    "CompileV23CandidateDecisionSetResult",
    "PersistedV23CandidateDecisionSet",
    "ResolvedCompileV23CandidateDecisionSetRequest",
    "V23CandidateDecisionSetStore",
    "read_committed_v23_candidate_decision_set",
    "resolve_compile_v23_candidate_decision_set_request",
)
