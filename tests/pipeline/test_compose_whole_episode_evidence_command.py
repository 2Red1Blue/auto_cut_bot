"""Whole-episode composition only accepts exact succeeded child closures."""

from __future__ import annotations

from uuid import UUID

from autocut_kernel.pipeline.compose_whole_episode_evidence_command import (
    COMPOSE_WHOLE_EPISODE_EVIDENCE_COMMAND,
    ComposeWholeEpisodeEvidenceCommand,
    ComposeWholeEpisodeEvidenceRequest,
)
from autocut_kernel.pipeline.evidence_index import (
    EvidenceIndex,
    EvidenceIndexEntry,
    EvidenceRequirement,
)
from autocut_kernel.pipeline.prepare_timed_media_evidence_command import (
    PREPARE_TIMED_MEDIA_EVIDENCE_COMMAND,
)
from autocut_kernel.store.models import (
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandOutcome,
    CommandSuccess,
    CommittedArtifactMemberReference,
    Job,
    PersistedCommittedArtifactMember,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
    canonical_payload_hash,
    canonical_recipe_scope,
)


def _hash(digit: str) -> str:
    return "sha256:" + digit * 64


def _requirement() -> EvidenceRequirement:
    return EvidenceRequirement(
        0,
        *(_hash(digit) for digit in "123456789a"),
        "exact-timed-evidence-v1",
    )


def _child_record(job: Job) -> tuple[PersistedCommittedArtifactSet, EvidenceIndexEntry]:
    slot, receipt, artifact_set = (UUID(int=value) for value in (11, 12, 13))
    scope = canonical_recipe_scope(job)
    layout = (
        ("root_media_evidence_bundle", "root_media_evidence"),
        ("candidate_timed_evidence_index", "candidate_timed_evidence"),
        ("timed_speech_profile_admission", "timed_speech_profile_admission"),
        ("presentation_timeline_probe", "presentation_timeline_probe"),
        ("committed_video_to_audio_clock_map_certificate", "video_to_audio_clock_map"),
    )
    artifacts = tuple(
        ArtifactMember(
            artifact_type,
            f"{prefix}_episode_0000",
            1,
            scope,
            canonical_payload_hash(f'{{"ordinal":{ordinal}}}'),
            f'{{"ordinal":{ordinal}}}',
        )
        for ordinal, (artifact_type, prefix) in enumerate(layout)
    )
    request_hash = _hash("c")
    record = PersistedCommittedArtifactSet(
        job,
        UUID(int=10),
        slot,
        receipt,
        artifact_set,
        request_hash,
        PREPARE_TIMED_MEDIA_EVIDENCE_COMMAND,
        "deterministic",
        artifact_set_hash(artifacts),
        tuple(
            PersistedCommittedArtifactMember(
                CommittedArtifactMemberReference(
                    receipt,
                    artifact_set,
                    ordinal,
                    member.scope,
                    member.artifact_type,
                    member.logical_id,
                    member.revision,
                    member.content_hash,
                ),
                member.payload_json,
                slot,
            )
            for ordinal, member in enumerate(artifacts)
        ),
    )
    entry = EvidenceIndexEntry(
        _requirement(), job, slot, receipt, artifact_set, request_hash, record.set_hash, record.references
    )
    return record, entry


class Store:
    def __init__(self, child: PersistedCommittedArtifactSet) -> None:
        self.records = {(child.job, child.command_slot_id): child}
        self.claim: CommandClaim | None = None

    def read_committed_artifact_set(self, job: Job, **expected: object) -> PersistedCommittedArtifactSet:
        record = self.records[(job, expected["command_slot_id"])]
        assert record.receipt_id == expected["receipt_id"]
        assert record.artifact_set_id == expected["artifact_set_id"]
        assert record.request_hash == expected["expected_request_hash"]
        assert record.command_name == expected["expected_command_name"]
        assert record.execution_kind == expected["expected_execution_kind"]
        return record

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        self.claim = claim
        return CommandOutcome(UUID(int=21), "running", is_fresh_claim=True, job_id=UUID(int=20))

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        assert self.claim is not None
        receipt, artifact_set = UUID(int=22), UUID(int=23)
        record = PersistedCommittedArtifactSet(
            self.claim.job,
            UUID(int=20),
            success.command_slot_id,
            receipt,
            artifact_set,
            self.claim.request_hash,
            self.claim.command_name,
            "deterministic",
            success.set_hash,
            tuple(
                PersistedCommittedArtifactMember(
                    CommittedArtifactMemberReference(
                        receipt,
                        artifact_set,
                        ordinal,
                        artifact.scope,
                        artifact.artifact_type,
                        artifact.logical_id,
                        artifact.revision,
                        artifact.content_hash,
                    ),
                    artifact.payload_json,
                    success.command_slot_id,
                )
                for ordinal, artifact in enumerate(success.artifacts)
            ),
        )
        self.records[(record.job, record.command_slot_id)] = record
        return CommandOutcome(
            success.command_slot_id,
            "succeeded",
            receipt_id=receipt,
            artifact_set_id=artifact_set,
            job_id=UUID(int=20),
        )


def test_cross_job_child_is_reread_by_exact_closure_before_one_aggregate_is_committed() -> None:
    child_job = Job("episode-00-attempt-a", "production")
    child, entry = _child_record(child_job)
    destination = Job("whole-drama-output", "production")
    request = ComposeWholeEpisodeEvidenceRequest(
        destination,
        "compose-whole-drama-1",
        canonical_recipe_scope(destination),
        1,
        EvidenceIndex((entry,)),
    )

    result = ComposeWholeEpisodeEvidenceCommand(Store(child)).execute(request)

    assert result.outcome.state == "succeeded"
    assert result.artifact is not None
    assert result.artifact.scope == canonical_recipe_scope(destination)
    assert result.artifact.artifact_type == "whole_episode_evidence_aggregate"
    assert COMPOSE_WHOLE_EPISODE_EVIDENCE_COMMAND == "ComposeWholeEpisodeEvidence@2.1.3"


def test_destination_scope_cannot_be_borrowed_from_a_child_job() -> None:
    child, entry = _child_record(Job("episode-00-attempt-a", "production"))
    del child
    destination = Job("whole-drama-output", "production")

    try:
        ComposeWholeEpisodeEvidenceRequest(
            destination,
            "compose-whole-drama-1",
            ArtifactScope("pipeline", "job", entry.origin_job.job_key),
            1,
            EvidenceIndex((entry,)),
        )
    except ValueError as error:
        assert "destination Job recipe scope" in str(error)
    else:
        raise AssertionError("foreign child scope was accepted")
