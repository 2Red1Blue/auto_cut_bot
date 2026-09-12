"""Read-side checks for immutable, unverified VLM observation reports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ..pipeline.observation_generation import (
    ObservationGenerationRequest,
    observation_artifacts,
)
from ..store.models import (
    ArtifactMember,
    BlobRef,
    CommandOutcome,
    GenerationAttempt,
    Job,
    canonical_payload_hash,
)
from ..vlm.observation_contract import ObservationReport, decode_observation_report


class ObservationArtifactStore(Protocol):
    def read_generation_attempt_for_slot(
        self, job: Job, command_slot_id: UUID
    ) -> GenerationAttempt | None: ...
    def read_immutable_blob(self, job: Job, reference: BlobRef) -> bytes: ...
    def read_committed_observation_artifacts(
        self,
        request: ObservationGenerationRequest,
        outcome: CommandOutcome,
    ) -> tuple[ArtifactMember, ...]: ...

    def read_committed_generation_attempt_chain(
        self,
        job: Job,
        *,
        command_slot_id: UUID,
        receipt_id: UUID,
        artifact_set_id: UUID,
        expected_request_hash: str,
    ) -> tuple[GenerationAttempt, ...]: ...


@dataclass(frozen=True, slots=True)
class PersistedObservationReport:
    """A source-bound model report; explicitly not verified semantic evidence."""

    request: ObservationGenerationRequest
    outcome: CommandOutcome
    attempt: GenerationAttempt
    report: ObservationReport
    artifacts: tuple[ArtifactMember, ...]

    @property
    def status(self) -> str:
        return "observation_unverified"


def read_committed_observation_report(
    store: ObservationArtifactStore,
    request: ObservationGenerationRequest,
    outcome: CommandOutcome,
) -> PersistedObservationReport:
    """Recompute from exact stored raw bytes, never trust a supplied parsed report."""
    if (
        outcome.state != "succeeded"
        or outcome.receipt_id is None
        or outcome.artifact_set_id is None
    ):
        raise ValueError("observation reader requires a succeeded immutable outcome")
    chain = store.read_committed_generation_attempt_chain(
        request.job,
        command_slot_id=outcome.command_slot_id,
        receipt_id=outcome.receipt_id,
        artifact_set_id=outcome.artifact_set_id,
        expected_request_hash=request.request_hash,
    )
    if not chain:
        raise ValueError(
            "observation reader requires a committed generation attempt chain"
        )
    attempt = chain[-1]
    if attempt.state != "committed" or attempt.raw_response is None:
        raise ValueError("observation reader requires a committed raw-response attempt")
    raw = store.read_immutable_blob(request.job, attempt.raw_response)
    report = decode_observation_report(raw, request.limits, request.alias_map)
    expected = observation_artifacts(request, attempt, report)
    actual = store.read_committed_observation_artifacts(request, outcome)
    if not _same_artifact_set(actual, expected):
        raise ValueError(
            "committed observation artifacts differ from raw reconstruction"
        )
    return PersistedObservationReport(request, outcome, attempt, report, actual)


def _same_artifact_set(
    actual: tuple[ArtifactMember, ...], expected: tuple[ArtifactMember, ...]
) -> bool:
    """Compare durable JSON members structurally, not PostgreSQL jsonb text order."""
    if len(actual) != len(expected):
        return False
    for persisted, rebuilt in zip(actual, expected, strict=True):
        if (
            persisted.artifact_type != rebuilt.artifact_type
            or persisted.logical_id != rebuilt.logical_id
            or persisted.revision != rebuilt.revision
            or persisted.scope != rebuilt.scope
            or persisted.content_hash != rebuilt.content_hash
            or canonical_payload_hash(persisted.payload_json) != canonical_payload_hash(rebuilt.payload_json)
        ):
            return False
    return True


__all__ = (
    "ObservationArtifactStore",
    "PersistedObservationReport",
    "read_committed_observation_report",
)
