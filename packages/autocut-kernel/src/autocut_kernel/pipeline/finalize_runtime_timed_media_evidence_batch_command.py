"""Atomically finalize a bounded batch of fully reread PC-CUDA evidence.

The historical CPU finalizer is not an adapter for this command: CUDA children
have a different command name, capability admission and request hash.  The
first pass reads only compact committed identities and budgets all blobs before
the second pass parses/recomputes every child.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ..media.types import canonical_sha256
from ..registry.installed_runtime import InstalledRuntimeTimedSpeechAuthorityResolver
from ..source_manifest import SourceManifestDecodeError, decode_source_manifest
from ..store.models import (
    ArtifactMember,
    ArtifactScope,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandSuccess,
    CommittedArtifactMemberReference,
    Job,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
    canonical_payload_hash,
    canonical_recipe_scope,
)
from .committed_runtime_timed_media import (
    RuntimeTimedMediaEvidenceMetadata,
    RuntimeTimedMediaReadError,
    RuntimeTimedMediaReadStore,
    inspect_committed_runtime_timed_media_evidence,
    read_committed_runtime_timed_media_evidence,
)
from .committed_timed_media import TimedMediaReadLimits
from .prepare_runtime_timed_media_evidence_command import PrepareRuntimeTimedMediaEvidenceRequest

FINALIZE_RUNTIME_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND = (
    "FinalizeRuntimeTimedMediaEvidenceBatch@1.0.0"
)
RUNTIME_TIMED_MEDIA_EVIDENCE_BATCH_STRATEGY_VERSION = "runtime-timed-media-evidence-batch-v1"


class RuntimeTimedMediaEvidenceBatchError(ValueError):
    """A CUDA batch is not an exact, complete committed predecessor."""


@dataclass(frozen=True, slots=True)
class RuntimeTimedMediaEvidenceBatchChild:
    request: PrepareRuntimeTimedMediaEvidenceRequest
    outcome: CommandOutcome

    def __post_init__(self) -> None:
        if type(self.request) is not PrepareRuntimeTimedMediaEvidenceRequest:  # noqa: E721
            raise RuntimeTimedMediaEvidenceBatchError("runtime batch child request must be exact")
        outcome = self.outcome
        if (
            type(outcome) is not CommandOutcome  # noqa: E721
            or outcome.state != "succeeded"
            or outcome.job_id is None
            or outcome.receipt_id is None
            or outcome.artifact_set_id is None
            or outcome.failure_code is not None
            or outcome.failure_detail_json is not None
        ):
            raise RuntimeTimedMediaEvidenceBatchError(
                "runtime batch child requires an exact succeeded outcome"
            )

    def to_mapping(self) -> dict[str, object]:
        outcome = self.outcome
        assert outcome.job_id is not None
        assert outcome.receipt_id is not None
        assert outcome.artifact_set_id is not None
        return {
            "outcome": {
                "artifact_set_id": str(outcome.artifact_set_id),
                "command_slot_id": str(outcome.command_slot_id),
                "job_id": str(outcome.job_id),
                "receipt_id": str(outcome.receipt_id),
                "state": outcome.state,
            },
            "request": self.request.canonical_payload(),
        }


@dataclass(frozen=True, slots=True)
class FinalizeRuntimeTimedMediaEvidenceBatchRequest:
    job: Job
    idempotency_key: str
    artifact_scope: ArtifactScope
    artifact_revision: int
    children: tuple[RuntimeTimedMediaEvidenceBatchChild, ...]

    def __post_init__(self) -> None:
        if type(self.job) is not Job or self.artifact_scope != canonical_recipe_scope(self.job):  # noqa: E721
            raise RuntimeTimedMediaEvidenceBatchError("runtime batch Job scope is invalid")
        if (
            type(self.idempotency_key) is not str  # noqa: E721
            or not self.idempotency_key.startswith("runtime-media-finalize:")
            or self.idempotency_key != self.idempotency_key.strip()
        ):
            raise RuntimeTimedMediaEvidenceBatchError(
                "runtime batch idempotency key must use the CUDA-only prefix"
            )
        if type(self.artifact_revision) is not int or self.artifact_revision < 1:  # noqa: E721
            raise RuntimeTimedMediaEvidenceBatchError("runtime batch revision must be positive")
        children = self.children
        if type(children) is not tuple or not children:  # noqa: E721
            raise RuntimeTimedMediaEvidenceBatchError("runtime batch children must be non-empty")
        if any(type(item) is not RuntimeTimedMediaEvidenceBatchChild for item in children):  # noqa: E721
            raise RuntimeTimedMediaEvidenceBatchError("runtime batch children must be exact values")
        bases = tuple(item.request.timed_media_request for item in children)
        if any(
            base.job != self.job
            or base.artifact_scope != self.artifact_scope
            or base.artifact_revision != self.artifact_revision
            for base in bases
        ):
            raise RuntimeTimedMediaEvidenceBatchError("runtime child Job/scope/revision differs")
        if tuple(base.episode_index for base in bases) != tuple(range(len(bases))):
            raise RuntimeTimedMediaEvidenceBatchError("runtime batch must cover ordered episode indexes")
        for values, label in (
            (tuple(item.request.idempotency_key for item in children), "idempotency key"),
            (tuple(item.outcome.command_slot_id for item in children), "command slot"),
            (tuple(item.outcome.receipt_id for item in children), "Receipt"),
            (tuple(item.outcome.artifact_set_id for item in children), "ArtifactSet"),
        ):
            if len(values) != len(set(values)):
                raise RuntimeTimedMediaEvidenceBatchError(f"runtime batch duplicates child {label}")
        if len({item.outcome.job_id for item in children}) != 1:
            raise RuntimeTimedMediaEvidenceBatchError("runtime child outcomes must share one Kernel Job")
        measurement_hashes = {
            item.request.runtime_measurement_identity.canonical_sha256 for item in children
        }
        if len(measurement_hashes) != 1:
            raise RuntimeTimedMediaEvidenceBatchError(
                "one runtime batch cannot mix distinct live CUDA measurements"
            )

    def canonical_payload(
        self,
        *,
        limits: TimedMediaReadLimits,
        children: tuple[dict[str, object], ...],
    ) -> dict[str, object]:
        if type(limits) is not TimedMediaReadLimits or len(children) != len(self.children):  # noqa: E721
            raise RuntimeTimedMediaEvidenceBatchError("runtime batch payload is incomplete")
        return {
            "artifact_revision": self.artifact_revision,
            "artifact_scope": _scope_mapping(self.artifact_scope),
            "children": list(children),
            "job": {"job_key": self.job.job_key, "profile": self.job.profile},
            "reader_limits": _limits_mapping(limits),
            "strategy_version": RUNTIME_TIMED_MEDIA_EVIDENCE_BATCH_STRATEGY_VERSION,
        }


@dataclass(frozen=True, slots=True)
class FinalizeRuntimeTimedMediaEvidenceBatchResult:
    outcome: CommandOutcome
    artifact: ArtifactMember | None = None
    child_member_references: tuple[tuple[CommittedArtifactMemberReference, ...], ...] = ()

    def __post_init__(self) -> None:
        if type(self.outcome) is not CommandOutcome:  # noqa: E721
            raise RuntimeTimedMediaEvidenceBatchError("runtime batch result outcome must be exact")
        if self.artifact is not None and type(self.artifact) is not ArtifactMember:  # noqa: E721
            raise RuntimeTimedMediaEvidenceBatchError("runtime batch artifact must be exact")
        if type(self.child_member_references) is not tuple or any(  # noqa: E721
            type(item) is not tuple or len(item) != 5
            or any(type(ref) is not CommittedArtifactMemberReference for ref in item)
            for item in self.child_member_references
        ):
            raise RuntimeTimedMediaEvidenceBatchError("runtime batch must retain five exact refs per child")


class RuntimeTimedMediaEvidenceBatchStore(RuntimeTimedMediaReadStore, Protocol):
    """Exact CUDA reader Store plus the generic finalizer command transaction."""


@dataclass(frozen=True, slots=True)
class _ChildMetadata:
    job_id: UUID
    request_hash: str
    set_hash: str
    member_references: tuple[CommittedArtifactMemberReference, ...]
    blob_refs: tuple[BlobRef, ...]
    projection_hash: str


class FinalizeRuntimeTimedMediaEvidenceBatchCommand:
    def __init__(
        self,
        store: RuntimeTimedMediaEvidenceBatchStore,
        authority_resolver: InstalledRuntimeTimedSpeechAuthorityResolver,
        limits: TimedMediaReadLimits,
    ) -> None:
        if type(authority_resolver) is not InstalledRuntimeTimedSpeechAuthorityResolver:  # noqa: E721
            raise RuntimeTimedMediaEvidenceBatchError(
                "runtime batch requires the installed CUDA authority resolver"
            )
        if type(limits) is not TimedMediaReadLimits:  # noqa: E721
            raise RuntimeTimedMediaEvidenceBatchError("runtime batch requires explicit reader limits")
        self._store = store
        self._authority_resolver = authority_resolver
        self._limits = limits

    def execute(
        self, request: FinalizeRuntimeTimedMediaEvidenceBatchRequest
    ) -> FinalizeRuntimeTimedMediaEvidenceBatchResult:
        if type(request) is not FinalizeRuntimeTimedMediaEvidenceBatchRequest:  # noqa: E721
            raise RuntimeTimedMediaEvidenceBatchError("runtime batch request must be exact")
        compact, request_hash, artifact = _reread_batch(
            self._store, request, self._authority_resolver, self._limits
        )
        claimed = self._store.claim_command(
            CommandClaim(
                request.job,
                request.idempotency_key,
                FINALIZE_RUNTIME_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND,
                request_hash,
                execution_kind="deterministic",
            )
        )
        if not claimed.is_fresh_claim:
            if claimed.state == "succeeded":
                _assert_final_record(self._store, request, claimed, request_hash, artifact)
                return FinalizeRuntimeTimedMediaEvidenceBatchResult(claimed, artifact, compact)
            return FinalizeRuntimeTimedMediaEvidenceBatchResult(claimed, None, compact)
        # A Store success acknowledgement remains indeterminate if it fails; do
        # not turn a possibly committed aggregate into a denial.
        committed = self._store.commit_command_success(
            CommandSuccess(claimed.command_slot_id, artifact_set_hash((artifact,)), (artifact,))
        )
        _assert_final_record(self._store, request, committed, request_hash, artifact)
        return FinalizeRuntimeTimedMediaEvidenceBatchResult(committed, artifact, compact)


def _reread_batch(
    store: RuntimeTimedMediaEvidenceBatchStore,
    request: FinalizeRuntimeTimedMediaEvidenceBatchRequest,
    resolver: InstalledRuntimeTimedSpeechAuthorityResolver,
    limits: TimedMediaReadLimits,
) -> tuple[tuple[tuple[CommittedArtifactMemberReference, ...], ...], str, ArtifactMember]:
    if type(resolver) is not InstalledRuntimeTimedSpeechAuthorityResolver:  # noqa: E721
        raise RuntimeTimedMediaEvidenceBatchError("runtime batch resolver is not installed")
    metadata: list[_ChildMetadata] = []
    total_bytes = 0
    try:
        for child in request.children:
            inspected = inspect_committed_runtime_timed_media_evidence(
                store, child.request, child.outcome, authority_resolver=resolver, limits=limits
            )
            item = _compact_metadata(inspected)
            total_bytes += sum(ref.byte_length for ref in item.blob_refs)
            if total_bytes > limits.max_total_blob_bytes:
                raise RuntimeTimedMediaEvidenceBatchError(
                    "runtime batch evidence exceeds the cumulative byte ceiling"
                )
            metadata.append(item)
    except RuntimeTimedMediaReadError as error:
        raise RuntimeTimedMediaEvidenceBatchError("runtime child metadata cannot be reread") from error
    _assert_complete_source_coverage(store, request)
    compact = tuple(
        _read_exact_child(store, child, item, resolver, limits)
        for child, item in zip(request.children, metadata, strict=True)
    )
    payload = request.canonical_payload(
        limits=limits,
        children=tuple(
            _child_payload(child, item, refs)
            for child, item, refs in zip(request.children, metadata, compact, strict=True)
        ),
    )
    return compact, canonical_sha256(payload), _artifact(request, payload)


def _compact_metadata(metadata: RuntimeTimedMediaEvidenceMetadata) -> _ChildMetadata:
    record = metadata.record
    return _ChildMetadata(
        record.job_id,
        record.request_hash,
        record.set_hash,
        tuple(member.reference for member in record.members),
        metadata.blob_refs,
        metadata.projection.canonical_hash,
    )


def _assert_complete_source_coverage(
    store: RuntimeTimedMediaEvidenceBatchStore,
    request: FinalizeRuntimeTimedMediaEvidenceBatchRequest,
) -> None:
    first = request.children[0].request.timed_media_request
    try:
        persisted = store.read_whole_series_source_manifest(
            request.job, first.source_manifest_artifact_set_id
        )
        if (
            persisted.reference != first.source_manifest_reference
            or persisted.receipt_id != first.source_manifest_receipt_id
            or persisted.artifact_set_id != first.source_manifest_artifact_set_id
            or persisted.command_slot_id != first.source_manifest_command_slot_id
            or persisted.source_job != request.job
        ):
            raise RuntimeTimedMediaEvidenceBatchError("runtime source manifest differs from child")
        source = decode_source_manifest(persisted.payload_json, persisted.proxy_blobs)
    except (SourceManifestDecodeError, TypeError, ValueError) as error:
        if isinstance(error, RuntimeTimedMediaEvidenceBatchError):
            raise
        raise RuntimeTimedMediaEvidenceBatchError("runtime source manifest is unavailable") from error
    for child in request.children:
        item = child.request.timed_media_request
        if (
            item.source_manifest_reference != first.source_manifest_reference
            or item.source_manifest_receipt_id != first.source_manifest_receipt_id
            or item.source_manifest_artifact_set_id != first.source_manifest_artifact_set_id
            or item.source_manifest_command_slot_id != first.source_manifest_command_slot_id
            or item.source_provenance_sha256 != first.source_provenance_sha256
            or item.semantic_inputs_request != first.semantic_inputs_request
            or item.producer_policy_sha256 != first.producer_policy_sha256
            or item.materialization_limits != first.materialization_limits
            or item.adaptive_policy != first.adaptive_policy
        ):
            raise RuntimeTimedMediaEvidenceBatchError("runtime children do not share frozen selectors")
    if len(source.episodes) != len(request.children):
        raise RuntimeTimedMediaEvidenceBatchError("runtime batch does not cover every Source episode")


def _read_exact_child(
    store: RuntimeTimedMediaEvidenceBatchStore,
    child: RuntimeTimedMediaEvidenceBatchChild,
    metadata: _ChildMetadata,
    resolver: InstalledRuntimeTimedSpeechAuthorityResolver,
    limits: TimedMediaReadLimits,
) -> tuple[CommittedArtifactMemberReference, ...]:
    try:
        value = read_committed_runtime_timed_media_evidence(
            store, child.request, child.outcome, authority_resolver=resolver, limits=limits
        )
    except RuntimeTimedMediaReadError as error:
        raise RuntimeTimedMediaEvidenceBatchError("runtime child cannot be fully reread") from error
    record = value.record
    if (
        record.job_id != metadata.job_id
        or record.request_hash != metadata.request_hash
        or record.set_hash != metadata.set_hash
        or value.projection.canonical_hash != metadata.projection_hash
    ):
        raise RuntimeTimedMediaEvidenceBatchError("runtime child changed between metadata and replay")
    refs = tuple(member.reference for member in record.members)
    if refs != metadata.member_references:
        raise RuntimeTimedMediaEvidenceBatchError("runtime child reread lost a committed member")
    return refs


def read_committed_runtime_timed_media_evidence_batch(
    store: RuntimeTimedMediaEvidenceBatchStore,
    request: FinalizeRuntimeTimedMediaEvidenceBatchRequest,
    outcome: CommandOutcome,
    *,
    authority_resolver: InstalledRuntimeTimedSpeechAuthorityResolver,
    limits: TimedMediaReadLimits,
) -> FinalizeRuntimeTimedMediaEvidenceBatchResult:
    compact, request_hash, artifact = _reread_batch(store, request, authority_resolver, limits)
    _assert_final_record(store, request, outcome, request_hash, artifact)
    return FinalizeRuntimeTimedMediaEvidenceBatchResult(outcome, artifact, compact)


def _assert_final_record(
    store: RuntimeTimedMediaEvidenceBatchStore,
    request: FinalizeRuntimeTimedMediaEvidenceBatchRequest,
    outcome: CommandOutcome,
    request_hash: str,
    artifact: ArtifactMember,
) -> None:
    if (
        type(outcome) is not CommandOutcome  # noqa: E721
        or outcome.state != "succeeded"
        or outcome.job_id is None
        or outcome.receipt_id is None
        or outcome.artifact_set_id is None
        or outcome.failure_code is not None
        or outcome.failure_detail_json is not None
        or outcome.job_id != request.children[0].outcome.job_id
    ):
        raise RuntimeTimedMediaEvidenceBatchError("runtime finalizer did not produce a succeeded Receipt")
    record = store.read_committed_artifact_set(
        request.job,
        command_slot_id=outcome.command_slot_id,
        receipt_id=outcome.receipt_id,
        artifact_set_id=outcome.artifact_set_id,
        expected_request_hash=request_hash,
        expected_command_name=FINALIZE_RUNTIME_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND,
        expected_execution_kind="deterministic",
    )
    if (
        type(record) is not PersistedCommittedArtifactSet  # noqa: E721
        or record.job != request.job
        or record.job_id != outcome.job_id
        or record.command_slot_id != outcome.command_slot_id
        or record.receipt_id != outcome.receipt_id
        or record.artifact_set_id != outcome.artifact_set_id
        or record.request_hash != request_hash
        or record.command_name != FINALIZE_RUNTIME_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND
        or record.execution_kind != "deterministic"
        or len(record.members) != 1
        or record.set_hash != artifact_set_hash((artifact,))
    ):
        raise RuntimeTimedMediaEvidenceBatchError("runtime final Store record differs")
    member = record.members[0]
    if (
        member.reference.member_ordinal != 0
        or member.reference.artifact_type != artifact.artifact_type
        or member.reference.logical_id != artifact.logical_id
        or member.reference.scope != artifact.scope
        or member.reference.revision != artifact.revision
        or member.reference.content_hash != artifact.content_hash
        or member.payload_json != artifact.payload_json
    ):
        raise RuntimeTimedMediaEvidenceBatchError("runtime final Store member differs from replay")


def _child_payload(
    child: RuntimeTimedMediaEvidenceBatchChild,
    metadata: _ChildMetadata,
    refs: tuple[CommittedArtifactMemberReference, ...],
) -> dict[str, object]:
    return {
        **child.to_mapping(),
        "record": {
            "members": [item.to_mapping() for item in refs],
            "request_hash": metadata.request_hash,
            "set_hash": metadata.set_hash,
            "runtime_projection_sha256": metadata.projection_hash,
        },
    }


def _artifact(
    request: FinalizeRuntimeTimedMediaEvidenceBatchRequest,
    payload: dict[str, object],
) -> ArtifactMember:
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return ArtifactMember(
        "runtime_timed_media_evidence_batch",
        "runtime_timed_media_evidence_batch",
        request.artifact_revision,
        request.artifact_scope,
        canonical_payload_hash(payload_json),
        payload_json,
    )


def _scope_mapping(scope: ArtifactScope) -> dict[str, str]:
    return {"key": scope.key, "kind": scope.kind, "namespace": scope.namespace}


def _limits_mapping(limits: TimedMediaReadLimits) -> dict[str, object]:
    materialization = limits.materialization
    return {
        "materialization": {
            "copy_chunk_bytes": materialization.copy_chunk_bytes,
            "max_source_bytes": materialization.max_source_bytes,
            "staging_quota_bytes": materialization.staging_quota_bytes,
            "timed_speech_max_request_bytes": materialization.timed_speech_max_request_bytes,
        },
        "max_blob_bytes": limits.max_blob_bytes,
        "max_candidates": limits.max_candidates,
        "max_total_blob_bytes": limits.max_total_blob_bytes,
    }


__all__ = (
    "FINALIZE_RUNTIME_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND",
    "RUNTIME_TIMED_MEDIA_EVIDENCE_BATCH_STRATEGY_VERSION",
    "FinalizeRuntimeTimedMediaEvidenceBatchCommand",
    "FinalizeRuntimeTimedMediaEvidenceBatchRequest",
    "FinalizeRuntimeTimedMediaEvidenceBatchResult",
    "RuntimeTimedMediaEvidenceBatchChild",
    "RuntimeTimedMediaEvidenceBatchError",
    "RuntimeTimedMediaEvidenceBatchStore",
    "read_committed_runtime_timed_media_evidence_batch",
)
