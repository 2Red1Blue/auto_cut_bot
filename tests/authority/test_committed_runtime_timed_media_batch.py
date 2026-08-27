"""CUDA batch finalizer replays every runtime child before one atomic Receipt."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from autocut_kernel.pipeline.finalize_runtime_timed_media_evidence_batch_command import (
    FINALIZE_RUNTIME_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND,
    FinalizeRuntimeTimedMediaEvidenceBatchCommand,
    FinalizeRuntimeTimedMediaEvidenceBatchRequest,
    RuntimeTimedMediaEvidenceBatchChild,
)
from autocut_kernel.store.models import (
    CommandOutcome,
    CommandSuccess,
    CommittedArtifactMemberReference,
    PersistedCommittedArtifactMember,
    PersistedCommittedArtifactSet,
)

from tests.authority.test_committed_runtime_timed_media import (
    _limits,
    _ReaderStore,
    runtime_reader_case,
)


class _BatchStore(_ReaderStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.final_record: PersistedCommittedArtifactSet | None = None

    def read_committed_artifact_set(self, job, **expected):  # type: ignore[no-untyped-def]
        if expected["expected_command_name"] == FINALIZE_RUNTIME_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND:
            assert self.final_record is not None
            return self.final_record
        return super().read_committed_artifact_set(job, **expected)

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        result = super().commit_command_success(success)
        if len(success.artifacts) == 5:
            return result
        assert self.record is not None
        receipt_id, artifact_set_id = uuid4(), uuid4()
        outcome = CommandOutcome(
            success.command_slot_id,
            "succeeded",
            receipt_id=receipt_id,
            artifact_set_id=artifact_set_id,
            job_id=self.record.job_id,
        )
        self._replace_slot(outcome)
        claim = self.claims[-1]
        members = tuple(
            PersistedCommittedArtifactMember(
                CommittedArtifactMemberReference(
                    receipt_id, artifact_set_id, ordinal, artifact.scope, artifact.artifact_type,
                    artifact.logical_id, artifact.revision, artifact.content_hash,
                ),
                artifact.payload_json,
                success.command_slot_id,
            )
            for ordinal, artifact in enumerate(success.artifacts)
        )
        self.final_record = PersistedCommittedArtifactSet(
            claim.job,
            self.record.job_id,
            success.command_slot_id,
            receipt_id,
            artifact_set_id,
            claim.request_hash,
            claim.command_name,
            claim.execution_kind,
            success.set_hash,
            members,
        )
        return outcome


def test_runtime_batch_has_distinct_command_and_replays_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Build the child with the same live CUDA resolver, then retain its Store
    # state in a finalizer-capable subclass.  The test targets command identity
    # and two-pass replay; multi-episode census coverage is covered by the CPU
    # analogue and is explicitly checked by the runtime finalizer itself.
    source_store, request, outcome, resolver = runtime_reader_case(tmp_path, monkeypatch)
    store = _BatchStore(tmp_path)
    store.__dict__.update(source_store.__dict__)
    batch = FinalizeRuntimeTimedMediaEvidenceBatchRequest(
        request.job,
        "runtime-media-finalize:fixture",
        request.timed_media_request.artifact_scope,
        request.timed_media_request.artifact_revision,
        (RuntimeTimedMediaEvidenceBatchChild(request, outcome),),
    )

    result = FinalizeRuntimeTimedMediaEvidenceBatchCommand(
        store, resolver, _limits(request)
    ).execute(batch)

    assert result.outcome.state == "succeeded"
    assert result.artifact is not None
    assert len(result.child_member_references) == 1
    assert store.final_record is not None
    assert store.final_record.command_name == FINALIZE_RUNTIME_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND
    assert len(store.final_record.members) == 1
