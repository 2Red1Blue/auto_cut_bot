"""Compose a whole episode from exact immutable child evidence closures.

This is intentionally separate from the legacy same-Job batch finalizer.  The
caller supplies each selected child by full Job/slot/Receipt/ArtifactSet
identity; this command never discovers a "latest" child or changes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from ..store.models import (
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandOutcome,
    CommandSuccess,
    Job,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
    canonical_recipe_scope,
)
from .evidence_index import EvidenceIndex, EvidenceIndexEntry
from .prepare_timed_media_evidence_command import PREPARE_TIMED_MEDIA_EVIDENCE_COMMAND

COMPOSE_WHOLE_EPISODE_EVIDENCE_COMMAND = "ComposeWholeEpisodeEvidence@2.1.3"
WHOLE_EPISODE_EVIDENCE_STRATEGY_VERSION = "whole-episode-evidence-compose-v1"


class WholeEpisodeEvidenceError(ValueError):
    """The requested whole-episode aggregate is not an exact closed selection."""


class WholeEpisodeEvidenceStore(Protocol):
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

    def claim_command(self, claim: CommandClaim) -> CommandOutcome: ...

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome: ...


@dataclass(frozen=True, slots=True)
class ComposeWholeEpisodeEvidenceRequest:
    """One append-only aggregate for a destination Job and complete target set."""

    job: Job
    idempotency_key: str
    artifact_scope: ArtifactScope
    artifact_revision: int
    evidence_index: EvidenceIndex

    def __post_init__(self) -> None:
        if type(self.job) is not Job:  # noqa: E721
            raise WholeEpisodeEvidenceError("composition requires an exact destination Job")
        if type(self.idempotency_key) is not str or not self.idempotency_key.strip():  # noqa: E721
            raise WholeEpisodeEvidenceError("composition idempotency key must be non-empty")
        if self.artifact_scope != canonical_recipe_scope(self.job):
            raise WholeEpisodeEvidenceError("aggregate must use its destination Job recipe scope")
        if type(self.artifact_revision) is not int or self.artifact_revision < 1:  # noqa: E721
            raise WholeEpisodeEvidenceError("aggregate revision must be a positive integer")
        if type(self.evidence_index) is not EvidenceIndex:  # noqa: E721
            raise WholeEpisodeEvidenceError("composition requires an exact complete evidence index")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "artifact_revision": self.artifact_revision,
            "artifact_scope": {
                "namespace": self.artifact_scope.namespace,
                "kind": self.artifact_scope.kind,
                "key": self.artifact_scope.key,
            },
            "destination_job": {"job_key": self.job.job_key, "profile": self.job.profile},
            "evidence_index": self.evidence_index.to_mapping(),
            "strategy_version": WHOLE_EPISODE_EVIDENCE_STRATEGY_VERSION,
        }

    @property
    def request_hash(self) -> str:
        return canonical_json_hash(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ComposeWholeEpisodeEvidenceResult:
    outcome: CommandOutcome
    artifact: ArtifactMember | None


def _reread_exact_child(
    store: WholeEpisodeEvidenceStore, entry: EvidenceIndexEntry,
) -> None:
    """Prove the caller's child handle still names that succeeded five-member set."""
    record = store.read_committed_artifact_set(
        entry.origin_job,
        command_slot_id=entry.command_slot_id,
        receipt_id=entry.receipt_id,
        artifact_set_id=entry.artifact_set_id,
        expected_request_hash=entry.request_hash,
        expected_command_name=PREPARE_TIMED_MEDIA_EVIDENCE_COMMAND,
        expected_execution_kind="deterministic",
    )
    if type(record) is not PersistedCommittedArtifactSet:  # noqa: E721
        raise WholeEpisodeEvidenceError("Store did not return an exact committed child set")
    if (
        record.job != entry.origin_job
        or record.command_slot_id != entry.command_slot_id
        or record.receipt_id != entry.receipt_id
        or record.artifact_set_id != entry.artifact_set_id
        or record.request_hash != entry.request_hash
        or record.command_name != PREPARE_TIMED_MEDIA_EVIDENCE_COMMAND
        or record.execution_kind != "deterministic"
        or record.set_hash != entry.set_hash
        or record.references != entry.members
    ):
        raise WholeEpisodeEvidenceError("persisted child differs from the selected immutable closure")


def _aggregate_artifact(request: ComposeWholeEpisodeEvidenceRequest) -> ArtifactMember:
    payload = request.canonical_payload()
    return ArtifactMember(
        "whole_episode_evidence_aggregate",
        "whole_episode_evidence_aggregate",
        request.artifact_revision,
        request.artifact_scope,
        canonical_json_hash(payload),
        canonical_json_bytes(payload).decode("utf-8"),
    )


def _reread_aggregate(
    store: WholeEpisodeEvidenceStore,
    request: ComposeWholeEpisodeEvidenceRequest,
    outcome: CommandOutcome,
    artifact: ArtifactMember,
) -> None:
    if outcome.state != "succeeded" or outcome.receipt_id is None or outcome.artifact_set_id is None:
        return
    record = store.read_committed_artifact_set(
        request.job,
        command_slot_id=outcome.command_slot_id,
        receipt_id=outcome.receipt_id,
        artifact_set_id=outcome.artifact_set_id,
        expected_request_hash=request.request_hash,
        expected_command_name=COMPOSE_WHOLE_EPISODE_EVIDENCE_COMMAND,
        expected_execution_kind="deterministic",
    )
    if (
        type(record) is not PersistedCommittedArtifactSet  # noqa: E721
        or record.artifacts != (artifact,)
        or record.job != request.job
        or record.command_slot_id != outcome.command_slot_id
    ):
        raise WholeEpisodeEvidenceError("composition replay does not match its immutable aggregate")


class ComposeWholeEpisodeEvidenceCommand:
    """Atomically publish an aggregate after independently rereading every child."""

    def __init__(self, store: WholeEpisodeEvidenceStore) -> None:
        self._store = store

    def execute(
        self, request: ComposeWholeEpisodeEvidenceRequest,
    ) -> ComposeWholeEpisodeEvidenceResult:
        if type(request) is not ComposeWholeEpisodeEvidenceRequest:  # noqa: E721
            raise WholeEpisodeEvidenceError("composition request must be exact")
        for entry in request.evidence_index.entries:
            _reread_exact_child(self._store, entry)
        artifact = _aggregate_artifact(request)
        outcome = self._store.claim_command(
            CommandClaim(
                request.job,
                request.idempotency_key,
                COMPOSE_WHOLE_EPISODE_EVIDENCE_COMMAND,
                request.request_hash,
                execution_kind="deterministic",
            )
        )
        if not outcome.is_fresh_claim:
            _reread_aggregate(self._store, request, outcome, artifact)
            return ComposeWholeEpisodeEvidenceResult(outcome, artifact if outcome.state == "succeeded" else None)
        committed = self._store.commit_command_success(
            CommandSuccess(outcome.command_slot_id, artifact_set_hash((artifact,)), (artifact,))
        )
        _reread_aggregate(self._store, request, committed, artifact)
        return ComposeWholeEpisodeEvidenceResult(committed, artifact)


__all__ = (
    "COMPOSE_WHOLE_EPISODE_EVIDENCE_COMMAND",
    "WHOLE_EPISODE_EVIDENCE_STRATEGY_VERSION",
    "ComposeWholeEpisodeEvidenceCommand",
    "ComposeWholeEpisodeEvidenceRequest",
    "ComposeWholeEpisodeEvidenceResult",
    "WholeEpisodeEvidenceError",
    "WholeEpisodeEvidenceStore",
)
