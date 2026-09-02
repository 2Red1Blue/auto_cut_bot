"""Finalize one exact, fully reread batch of timed-media evidence.

The batch is deliberately a small durable aggregate.  It never retains the
per-episode root/plan/candidate DTOs used during replay, and it makes no
admission claim beyond proving every child was reread before the aggregate
Receipt was written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ..media.types import canonical_sha256
from ..registry.installed_runtime import InstalledLocalRunProfileResolver
from ..registry.timed_speech import AuthorityRegistrySnapshot
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
from .committed_timed_media import (
    TimedMediaReadError,
    TimedMediaReadLimits,
    TimedMediaReadStore,
    inspect_committed_timed_media_evidence,
    read_committed_timed_media_evidence,
)
from .prepare_timed_media_evidence_command import PrepareTimedMediaEvidenceRequest

FINALIZE_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND = "FinalizeTimedMediaEvidenceBatch@2.1.3"
TIMED_MEDIA_EVIDENCE_BATCH_STRATEGY_VERSION = "timed-media-evidence-batch-v1"


class TimedMediaEvidenceBatchError(ValueError):
    """A proposed batch is not an exact, complete committed predecessor."""


@dataclass(frozen=True, slots=True)
class TimedMediaEvidenceBatchChild:
    """One actual Prepare request and its exact succeeded Store outcome."""

    request: PrepareTimedMediaEvidenceRequest
    outcome: CommandOutcome

    def __post_init__(self) -> None:
        if type(self.request) is not PrepareTimedMediaEvidenceRequest:  # noqa: E721
            raise TimedMediaEvidenceBatchError("batch child request must be exact")
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
            raise TimedMediaEvidenceBatchError("batch child requires an exact succeeded outcome")

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
class FinalizeTimedMediaEvidenceBatchRequest:
    job: Job
    idempotency_key: str
    artifact_scope: ArtifactScope
    artifact_revision: int
    children: tuple[TimedMediaEvidenceBatchChild, ...]

    def __post_init__(self) -> None:
        if type(self.job) is not Job or self.artifact_scope != canonical_recipe_scope(self.job):  # noqa: E721
            raise TimedMediaEvidenceBatchError("batch Job scope is invalid")
        if (
            type(self.idempotency_key) is not str  # noqa: E721
            or not self.idempotency_key.strip()
            or self.idempotency_key != self.idempotency_key.strip()
        ):
            raise TimedMediaEvidenceBatchError("batch idempotency_key must be canonical")
        if type(self.artifact_revision) is not int or self.artifact_revision < 1:  # noqa: E721
            raise TimedMediaEvidenceBatchError("batch artifact_revision must be positive")
        children = self.children
        if type(children) is not tuple or not children:  # noqa: E721
            raise TimedMediaEvidenceBatchError("batch children must be a non-empty tuple")
        if any(type(item) is not TimedMediaEvidenceBatchChild for item in children):  # noqa: E721
            raise TimedMediaEvidenceBatchError("batch children must be exact typed values")
        if any(
            item.request.artifact_scope != canonical_recipe_scope(item.request.job)
            or item.request.artifact_revision != self.artifact_revision
            for item in children
        ):
            raise TimedMediaEvidenceBatchError("batch child scope or revision differs")
        if tuple(item.request.episode_index for item in children) != tuple(range(len(children))):
            raise TimedMediaEvidenceBatchError("batch children must cover ordered episode indexes")
        for value, label in (
            (tuple(item.request.idempotency_key for item in children), "idempotency key"),
            (tuple(item.outcome.command_slot_id for item in children), "command slot"),
            (tuple(item.outcome.receipt_id for item in children), "Receipt"),
            (tuple(item.outcome.artifact_set_id for item in children), "ArtifactSet"),
        ):
            if len(value) != len(set(value)):
                raise TimedMediaEvidenceBatchError(f"batch contains duplicate child {label}")
        if len({item.request.evidence_job for item in children}) != 1:
            raise TimedMediaEvidenceBatchError("batch children must share one immutable evidence Job")

    def canonical_payload(
        self,
        *,
        snapshot: AuthorityRegistrySnapshot,
        limits: TimedMediaReadLimits,
        children: tuple[dict[str, object], ...],
    ) -> dict[str, object]:
        if type(snapshot) is not AuthorityRegistrySnapshot:  # noqa: E721
            raise TimedMediaEvidenceBatchError("batch requires an exact authority snapshot")
        if type(limits) is not TimedMediaReadLimits:  # noqa: E721
            raise TimedMediaEvidenceBatchError("batch requires exact reader limits")
        if type(children) is not tuple or len(children) != len(self.children):  # noqa: E721
            raise TimedMediaEvidenceBatchError("batch child identity payload is incomplete")
        return {
            "artifact_revision": self.artifact_revision,
            "artifact_scope": _scope_mapping(self.artifact_scope),
            "authority_snapshot": {
                "enabled_profile": {
                    "profile_id": snapshot.enabled_profile.profile_id,
                    "profile_version": snapshot.enabled_profile.profile_version,
                },
                "registry_set_sha256": snapshot.registry_set_sha256,
            },
            "children": list(children),
            "job": {"job_key": self.job.job_key, "profile": self.job.profile},
            "reader_limits": _limits_mapping(limits),
            "strategy_version": TIMED_MEDIA_EVIDENCE_BATCH_STRATEGY_VERSION,
        }


@dataclass(frozen=True, slots=True)
class FinalizeTimedMediaEvidenceBatchResult:
    outcome: CommandOutcome
    artifact: ArtifactMember | None = None
    child_member_references: tuple[tuple[CommittedArtifactMemberReference, ...], ...] = ()

    def __post_init__(self) -> None:
        if type(self.outcome) is not CommandOutcome:  # noqa: E721
            raise TimedMediaEvidenceBatchError("batch result outcome must be exact")
        if self.artifact is not None and type(self.artifact) is not ArtifactMember:  # noqa: E721
            raise TimedMediaEvidenceBatchError("batch result artifact must be exact")
        refs = self.child_member_references
        if type(refs) is not tuple or any(type(item) is not tuple for item in refs):  # noqa: E721
            raise TimedMediaEvidenceBatchError("batch result references must be immutable")
        if any(
            len(item) != 5 or any(type(ref) is not CommittedArtifactMemberReference for ref in item)  # noqa: E721
            for item in refs
        ):
            raise TimedMediaEvidenceBatchError("batch result must retain exactly five references per child")


class TimedMediaEvidenceBatchStore(TimedMediaReadStore, Protocol):
    """The exact reader Store plus the finalizer's generic command path."""


@dataclass(frozen=True, slots=True)
class _ChildMetadata:
    """Small first-pass identity; never retain child payload JSON across the batch."""

    job_id: UUID
    request_hash: str
    set_hash: str
    member_references: tuple[CommittedArtifactMemberReference, ...]
    blob_refs: tuple[BlobRef, ...]

    def __post_init__(self) -> None:
        if (
            type(self.job_id) is not UUID  # noqa: E721
            or type(self.request_hash) is not str  # noqa: E721
            or type(self.set_hash) is not str  # noqa: E721
            or type(self.member_references) is not tuple  # noqa: E721
            or type(self.blob_refs) is not tuple  # noqa: E721
            or len(self.member_references) != 5
            or any(type(item) is not CommittedArtifactMemberReference for item in self.member_references)  # noqa: E721
            or any(type(item) is not BlobRef for item in self.blob_refs)  # noqa: E721
        ):
            raise TimedMediaEvidenceBatchError("batch metadata must retain only exact compact identities")


class FinalizeTimedMediaEvidenceBatchCommand:
    """Atomically finalize only every bounded, independently reread episode."""

    def __init__(
        self,
        store: TimedMediaEvidenceBatchStore,
        authority_profile_resolver: InstalledLocalRunProfileResolver,
        limits: TimedMediaReadLimits,
    ) -> None:
        if type(authority_profile_resolver) is not InstalledLocalRunProfileResolver:  # noqa: E721
            raise TimedMediaEvidenceBatchError("batch requires the installed profile resolver")
        if type(limits) is not TimedMediaReadLimits:  # noqa: E721
            raise TimedMediaEvidenceBatchError("batch requires explicit timed-media reader limits")
        self._store = store
        self._authority_profile_resolver = authority_profile_resolver
        self._limits = limits

    def execute(
        self,
        request: FinalizeTimedMediaEvidenceBatchRequest,
    ) -> FinalizeTimedMediaEvidenceBatchResult:
        if type(request) is not FinalizeTimedMediaEvidenceBatchRequest:  # noqa: E721
            raise TimedMediaEvidenceBatchError("batch request must be exact")
        compact, request_hash, artifact = _reread_batch(
            self._store, request, self._authority_profile_resolver, self._limits
        )
        claimed = self._store.claim_command(
            CommandClaim(
                request.job,
                request.idempotency_key,
                FINALIZE_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND,
                request_hash,
                execution_kind="deterministic",
            )
        )
        if not claimed.is_fresh_claim:
            if claimed.state == "succeeded":
                _assert_final_record(self._store, request, claimed, request_hash, artifact)
                return FinalizeTimedMediaEvidenceBatchResult(claimed, artifact, compact)
            return FinalizeTimedMediaEvidenceBatchResult(claimed, None, compact)
        committed = self._store.commit_command_success(
            CommandSuccess(claimed.command_slot_id, artifact_set_hash((artifact,)), (artifact,)))
        _assert_final_record(self._store, request, committed, request_hash, artifact)
        return FinalizeTimedMediaEvidenceBatchResult(committed, artifact, compact)


def _reread_batch(
    store: TimedMediaEvidenceBatchStore,
    request: FinalizeTimedMediaEvidenceBatchRequest,
    resolver: InstalledLocalRunProfileResolver,
    limits: TimedMediaReadLimits,
) -> tuple[
    tuple[tuple[CommittedArtifactMemberReference, ...], ...],
    str,
    ArtifactMember,
]:
    if type(resolver) is not InstalledLocalRunProfileResolver:  # noqa: E721
        raise TimedMediaEvidenceBatchError("batch requires the installed profile resolver")
    if type(limits) is not TimedMediaReadLimits:  # noqa: E721
        raise TimedMediaEvidenceBatchError("batch requires explicit timed-media reader limits")
    try:
        metadata: list[_ChildMetadata] = []
        total_bytes = 0
        for child in request.children:
            item = _inspect_child(store, child, resolver, limits)
            total_bytes += sum(ref.byte_length for ref in item.blob_refs)
            if total_bytes > limits.max_total_blob_bytes:
                raise TimedMediaEvidenceBatchError(
                    "batch evidence BlobRefs exceed the cumulative byte ceiling"
                )
            metadata.append(item)
    except TimedMediaReadError as error:
        raise TimedMediaEvidenceBatchError("batch child metadata cannot be reread") from error
    _assert_complete_source_coverage(store, request)
    compact = tuple(
        _read_exact_child(store, child, item, resolver, limits)
        for child, item in zip(request.children, metadata, strict=True)
    )
    child_payloads = tuple(
        _child_payload(child, item, refs)
        for child, item, refs in zip(request.children, metadata, compact, strict=True)
    )
    payload = request.canonical_payload(
        snapshot=resolver.snapshot,
        limits=limits,
        children=child_payloads,
    )
    return compact, canonical_sha256(payload), _artifact(request, payload)


def _assert_complete_source_coverage(
    store: TimedMediaEvidenceBatchStore,
    request: FinalizeTimedMediaEvidenceBatchRequest,
) -> None:
    first = request.children[0].request
    try:
        persisted = store.read_whole_series_source_manifest(
            first.evidence_job, first.source_manifest_artifact_set_id
        )
        if (
            persisted.reference != first.source_manifest_reference
            or persisted.receipt_id != first.source_manifest_receipt_id
            or persisted.artifact_set_id != first.source_manifest_artifact_set_id
            or persisted.command_slot_id != first.source_manifest_command_slot_id
            or persisted.source_job != first.evidence_job
        ):
            raise TimedMediaEvidenceBatchError("batch Source manifest differs from child identity")
        source = decode_source_manifest(persisted.payload_json, persisted.proxy_blobs)
    except (SourceManifestDecodeError, ValueError, TypeError) as error:
        if isinstance(error, TimedMediaEvidenceBatchError):
            raise
        raise TimedMediaEvidenceBatchError("batch Source manifest is unavailable or invalid") from error
    for child in request.children:
        item = child.request
        if (
            item.source_manifest_reference != first.source_manifest_reference
            or item.evidence_job != first.evidence_job
            or item.source_manifest_receipt_id != first.source_manifest_receipt_id
            or item.source_manifest_artifact_set_id != first.source_manifest_artifact_set_id
            or item.source_manifest_command_slot_id != first.source_manifest_command_slot_id
            or item.source_provenance_sha256 != first.source_provenance_sha256
            or item.semantic_inputs_request != first.semantic_inputs_request
            or item.producer_policy_sha256 != first.producer_policy_sha256
            or item.materialization_limits != first.materialization_limits
            or item.adaptive_policy.strategy_version != first.adaptive_policy.strategy_version
            or item.adaptive_policy.max_expansion_count != first.adaptive_policy.max_expansion_count
        ):
            raise TimedMediaEvidenceBatchError("batch children do not share frozen source and policy selectors")
    if len(source.episodes) != len(request.children):
        raise TimedMediaEvidenceBatchError("batch does not cover every committed Source episode")


def _inspect_child(
    store: TimedMediaEvidenceBatchStore,
    child: TimedMediaEvidenceBatchChild,
    resolver: InstalledLocalRunProfileResolver,
    limits: TimedMediaReadLimits,
) -> _ChildMetadata:
    metadata = inspect_committed_timed_media_evidence(
        store,
        child.request,
        child.outcome,
        authority_profile_resolver=resolver,
        limits=limits,
    )
    record = metadata.record
    result = _ChildMetadata(
        record.job_id,
        record.request_hash,
        record.set_hash,
        tuple(member.reference for member in record.members),
        metadata.blob_refs,
    )
    del metadata, record
    return result


def _read_exact_child(
    store: TimedMediaEvidenceBatchStore,
    child: TimedMediaEvidenceBatchChild,
    metadata: _ChildMetadata,
    resolver: InstalledLocalRunProfileResolver,
    limits: TimedMediaReadLimits,
) -> tuple[CommittedArtifactMemberReference, ...]:
    try:
        value = read_committed_timed_media_evidence(
            store,
            child.request,
            child.outcome,
            authority_profile_resolver=resolver,
            limits=limits,
        )
    except TimedMediaReadError as error:
        raise TimedMediaEvidenceBatchError("batch child cannot be fully reread") from error
    if (
        value.record.job_id != metadata.job_id
        or value.record.request_hash != metadata.request_hash
        or value.record.set_hash != metadata.set_hash
    ):
        raise TimedMediaEvidenceBatchError("batch child record changed between metadata and full replay")
    refs = tuple(member.reference for member in value.record.members)
    if refs != metadata.member_references:
        raise TimedMediaEvidenceBatchError("batch child reread lost a timed-media member")
    return refs


def read_committed_timed_media_evidence_batch(
    store: TimedMediaEvidenceBatchStore,
    request: FinalizeTimedMediaEvidenceBatchRequest,
    outcome: CommandOutcome,
    *,
    authority_profile_resolver: InstalledLocalRunProfileResolver,
    limits: TimedMediaReadLimits,
) -> FinalizeTimedMediaEvidenceBatchResult:
    """Reread the exact aggregate and every child without claiming or writing."""
    if type(request) is not FinalizeTimedMediaEvidenceBatchRequest:  # noqa: E721
        raise TimedMediaEvidenceBatchError("batch request must be exact")
    compact, request_hash, artifact = _reread_batch(
        store, request, authority_profile_resolver, limits
    )
    _assert_final_record(store, request, outcome, request_hash, artifact)
    return FinalizeTimedMediaEvidenceBatchResult(outcome, artifact, compact)


def _assert_final_record(
    store: TimedMediaEvidenceBatchStore,
    request: FinalizeTimedMediaEvidenceBatchRequest,
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
        or type(outcome.command_slot_id) is not UUID  # noqa: E721
        or type(outcome.job_id) is not UUID  # noqa: E721
        or type(outcome.receipt_id) is not UUID  # noqa: E721
        or type(outcome.artifact_set_id) is not UUID  # noqa: E721
        or outcome.failure_code is not None
        or outcome.failure_detail_json is not None
    ):
        raise TimedMediaEvidenceBatchError("batch finalizer did not produce a succeeded Receipt")
    record = store.read_committed_artifact_set(
        request.job,
        command_slot_id=outcome.command_slot_id,
        receipt_id=outcome.receipt_id,
        artifact_set_id=outcome.artifact_set_id,
        expected_request_hash=request_hash,
        expected_command_name=FINALIZE_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND,
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
        or record.command_name != FINALIZE_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND
        or record.execution_kind != "deterministic"
        or len(record.members) != 1
    ):
        raise TimedMediaEvidenceBatchError(
            "batch succeeded Receipt/final Store record differs from exact finalizer identity"
        )
    member = record.members[0]
    if (
        member.reference.member_ordinal != 0
        or member.reference.artifact_type != artifact.artifact_type
        or member.reference.logical_id != artifact.logical_id
        or member.reference.scope != artifact.scope
        or member.reference.revision != artifact.revision
        or member.reference.content_hash != artifact.content_hash
        or member.payload_json != artifact.payload_json
        or record.set_hash != artifact_set_hash((artifact,))
    ):
        raise TimedMediaEvidenceBatchError("batch final Store member differs from reread payload")


def _child_payload(
    child: TimedMediaEvidenceBatchChild,
    record: _ChildMetadata,
    refs: tuple[CommittedArtifactMemberReference, ...],
) -> dict[str, object]:
    return {
        **child.to_mapping(),
        "record": {
            "members": [item.to_mapping() for item in refs],
            "request_hash": record.request_hash,
            "set_hash": record.set_hash,
        },
    }


def _artifact(
    request: FinalizeTimedMediaEvidenceBatchRequest,
    payload: dict[str, object],
) -> ArtifactMember:
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return ArtifactMember(
        "timed_media_evidence_batch",
        "timed_media_evidence_batch",
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
    "FINALIZE_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND",
    "TIMED_MEDIA_EVIDENCE_BATCH_STRATEGY_VERSION",
    "FinalizeTimedMediaEvidenceBatchCommand",
    "FinalizeTimedMediaEvidenceBatchRequest",
    "FinalizeTimedMediaEvidenceBatchResult",
    "TimedMediaEvidenceBatchChild",
    "TimedMediaEvidenceBatchError",
    "TimedMediaEvidenceBatchStore",
    "read_committed_timed_media_evidence_batch",
)
