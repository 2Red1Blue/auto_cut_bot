from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import UUID, uuid4

from autocut_kernel.media import AdaptiveEvidenceWindowPolicy, RootMediaEvidenceBundle
from autocut_kernel.pipeline import (
    PrepareTimedMediaEvidenceCommand,
    PrepareTimedMediaEvidenceRequest,
    ProducedTimedMediaEvidence,
    TimedMediaEvidenceProducerError,
)
from autocut_kernel.store import (
    ArtifactScope,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
)
from autocut_kernel.vlm import VlmObservationSet
from test_root_evidence import HASH_A, HASH_B, SOURCE_HASH, _bundle
from test_timed_evidence import _bindings, _manifest_and_observation


class _Store:
    def __init__(self) -> None:
        self.outcome: CommandOutcome | None = None
        self.successes: list[CommandSuccess] = []
        self.rejections: list[CommandRejection] = []
        self.blobs: dict[UUID, bytes] = {}

    def claim_command(self, _: CommandClaim) -> CommandOutcome:
        if self.outcome is not None:
            return self.outcome
        self.outcome = CommandOutcome(uuid4(), "running", is_fresh_claim=True)
        return self.outcome

    def read_immutable_blob(self, _: Job, reference: BlobRef) -> bytes:
        return self.blobs[reference.object_id]

    def put_immutable_blob(
        self,
        _: Job,
        *,
        content: bytes,
        content_hash: str,
        media_type: str,
    ) -> BlobRef:
        assert _hash(content) == content_hash
        reference = BlobRef(uuid4(), content_hash, len(content), media_type)
        self.blobs[reference.object_id] = content
        return reference

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        self.successes.append(success)
        self.outcome = CommandOutcome(
            success.command_slot_id,
            "succeeded",
            receipt_id=uuid4(),
            artifact_set_id=uuid4(),
        )
        return self.outcome

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome:
        self.rejections.append(rejection)
        self.outcome = CommandOutcome(
            rejection.command_slot_id,
            rejection.outcome,
            receipt_id=uuid4(),
            failure_code=rejection.failure_code,
            failure_detail_json=rejection.failure_detail_json,
        )
        return self.outcome


class _Producer:
    def __init__(self, bundle: RootMediaEvidenceBundle, *, fail: bool = False) -> None:
        self.bundle = bundle
        self.fail = fail
        self.calls = 0

    def prepare(
        self,
        request: PrepareTimedMediaEvidenceRequest,
        source_bytes: bytes,
    ) -> ProducedTimedMediaEvidence:
        self.calls += 1
        assert source_bytes == b"committed source"
        if self.fail:
            raise TimedMediaEvidenceProducerError(
                "SUBTITLE_EVIDENCE_INDETERMINATE",
                "burned-in subtitle coverage did not close",
            )
        bundle = replace(
            self.bundle,
            source_manifest_sha256=request.source_manifest_sha256,
            root_input_manifest_sha256=request.root_input_manifest_sha256,
        )
        values = (
            bundle.transcript,
            bundle.speech_activity,
            bundle.audio_sample_boundaries,
            bundle.frame_pts_index,
            bundle.shot_boundaries,
            bundle.scene_boundaries,
            bundle.visual_validity,
            bundle.subtitle_cues,
        )
        return ProducedTimedMediaEvidence(HASH_A, bundle, _bindings(values))


def _request(store: _Store) -> PrepareTimedMediaEvidenceRequest:
    manifest, observation = _manifest_and_observation()
    observation_set = VlmObservationSet(
        observation.request_identity_sha256,
        manifest.canonical_hash,
        HASH_B,
        (observation,),
    )
    source_blob = BlobRef(uuid4(), SOURCE_HASH, len(b"committed source"), "video/mp4")
    store.blobs[source_blob.object_id] = b"committed source"
    template = _bundle()
    return PrepareTimedMediaEvidenceRequest(
        job=Job("real-run-001", "shadow"),
        idempotency_key="media-preflight:episode:0",
        episode_index=0,
        artifact_scope=ArtifactScope("pipeline", "job", "real-run-001"),
        artifact_revision=1,
        source_blob=source_blob,
        source_manifest_sha256=HASH_B,
        source_provenance_sha256=HASH_A,
        window_manifest=manifest,
        observation_set=observation_set,
        frame_pts_index=manifest.frame_pts_index_set,
        audio_sample_boundaries=template.audio_sample_boundaries,
        adaptive_policy=AdaptiveEvidenceWindowPolicy(
            "whole-episode-test-policy-v1",
            manifest.source_time_base,
            100,
            100,
            25,
            2,
            2,
        ),
        producer_policy_sha256=HASH_A,
    )


def _hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def test_command_commits_conjunctive_evidence_once_and_replay_skips_producer() -> None:
    store = _Store()
    request = _request(store)
    producer = _Producer(_bundle())
    command = PrepareTimedMediaEvidenceCommand(store, producer)

    first = command.execute(request)
    replay = command.execute(request)

    assert first.outcome.state == "succeeded"
    assert first.candidate_count == 1
    assert replay.outcome.state == "succeeded"
    assert producer.calls == 1
    assert len(store.successes) == 1
    assert {item.artifact_type for item in store.successes[0].artifacts} == {
        "candidate_timed_evidence_index",
        "root_media_evidence_bundle",
    }


def test_required_detector_gap_commits_terminal_receipt_without_artifact_set() -> None:
    store = _Store()
    request = _request(store)
    producer = _Producer(_bundle(), fail=True)

    result = PrepareTimedMediaEvidenceCommand(store, producer).execute(request)

    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == "SUBTITLE_EVIDENCE_INDETERMINATE"
    assert producer.calls == 1
    assert store.successes == []
