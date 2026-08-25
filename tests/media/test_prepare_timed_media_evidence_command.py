from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import autocut_kernel.pipeline.prepare_timed_media_evidence_command as command_module
import pytest
from autocut_kernel.media import (
    AdaptiveEvidenceWindowPolicy,
    CalibrationBinding,
    CommittedVideoToAudioClockMapCertificate,
    EvidenceCompleteness,
    RootMediaEvidenceBundle,
    Stage4PredecessorError,
    TimedSpeechCapability,
    TimedSpeechGuardPolicy,
    TimedSpeechProducerRequirement,
    TimedSpeechProfileKind,
    TimedSpeechProfileRegistryEntry,
    TranscriptCompleteness,
    TranscriptSet,
    TranscriptSourceOutcome,
    derive_presentation_timeline_facts,
)
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.pipeline import (
    FinalizeTimedMediaEvidenceBatchCommand,
    FinalizeTimedMediaEvidenceBatchRequest,
    PrepareTimedMediaEvidenceCommand,
    PrepareTimedMediaEvidenceRequest,
    ProducedTimedMediaEvidence,
    TimedMediaEvidenceBatchChild,
    TimedMediaEvidenceProducerError,
)
from autocut_kernel.store import (
    ArtifactScope,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    CommittedArtifactMemberReference,
    Job,
    PersistedCommittedArtifactMember,
)
from autocut_kernel.store.models import MaterializationLimits
from test_root_evidence import HASH_A, HASH_B, HASH_C, SOURCE_HASH, _bundle
from test_timed_evidence import _bindings, _manifest_and_candidate

TEST_POLICY_JSON = json.dumps(
    {"policy_id": "fixture-policy"},
    separators=(",", ":"),
    sort_keys=True,
)
TEST_POLICY_SHA256 = canonical_sha256(json.loads(TEST_POLICY_JSON))


class _Store:
    def __init__(self) -> None:
        self.outcomes: dict[str, CommandOutcome] = {}
        self.successes: list[CommandSuccess] = []
        self.rejections: list[CommandRejection] = []
        self.blobs: dict[UUID, bytes] = {}
        self.materializations: list[BlobRef] = []
        self.closed_materializations = 0
        self.registry_members: dict[CommittedArtifactMemberReference, PersistedCommittedArtifactMember] = {}

    def read_committed_artifact_member(
        self,
        reference: CommittedArtifactMemberReference,
    ) -> PersistedCommittedArtifactMember:
        try:
            return self.registry_members[reference]
        except KeyError as error:
            raise ValueError("registry member is unavailable") from error

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        existing = self.outcomes.get(claim.idempotency_key)
        if existing is not None:
            return existing
        outcome = CommandOutcome(uuid4(), "running", is_fresh_claim=True)
        self.outcomes[claim.idempotency_key] = outcome
        return outcome

    def read_outcome(self, _: Job, idempotency_key: str) -> CommandOutcome | None:
        return self.outcomes.get(idempotency_key)

    def materialize_immutable_blob(
        self,
        _: Job,
        reference: BlobRef,
        limits: MaterializationLimits,
    ) -> _Lease:
        assert reference.byte_length <= limits.max_source_bytes
        self.materializations.append(reference)
        return _Lease(reference, self)

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
        outcome = CommandOutcome(
            success.command_slot_id,
            "succeeded",
            receipt_id=uuid4(),
            artifact_set_id=uuid4(),
        )
        self._replace_slot(outcome)
        return outcome

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome:
        self.rejections.append(rejection)
        outcome = CommandOutcome(
            rejection.command_slot_id,
            rejection.outcome,
            receipt_id=uuid4(),
            failure_code=rejection.failure_code,
            failure_detail_json=rejection.failure_detail_json,
        )
        self._replace_slot(outcome)
        return outcome

    def _replace_slot(self, outcome: CommandOutcome) -> None:
        for key, current in self.outcomes.items():
            if current.command_slot_id == outcome.command_slot_id:
                self.outcomes[key] = outcome
                return
        raise AssertionError("unknown command slot")


class _Lease:
    def __init__(self, reference: BlobRef, store: _Store) -> None:
        self.reference = reference
        self.path = Path("/private/verified/source.mp4")
        self._store = store
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._store.closed_materializations += 1


class _Producer:
    def __init__(self, bundle: RootMediaEvidenceBundle, *, fail: bool = False) -> None:
        self.bundle = bundle
        self.fail = fail
        self.calls = 0

    def prepare(
        self,
        request: PrepareTimedMediaEvidenceRequest,
        source: _Lease,
    ) -> ProducedTimedMediaEvidence:
        self.calls += 1
        assert source.reference == request.source_blob
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
        bindings = _bindings(values)
        return ProducedTimedMediaEvidence(
            TEST_POLICY_SHA256,
            bundle,
            bindings,
            TEST_POLICY_JSON,
            _provenance(request.source_provenance_sha256, bindings),
        )


class _BusyOnceProducer(_Producer):
    def prepare(
        self,
        request: PrepareTimedMediaEvidenceRequest,
        source: _Lease,
    ) -> ProducedTimedMediaEvidence:
        if self.calls == 0:
            self.calls += 1
            raise TimedMediaEvidenceProducerError(
                "TIMED_SPEECH_BUSY", "admission capacity is full", outcome="failed"
            )
        return super().prepare(request, source)


class _UnknownResultProducer(_Producer):
    def prepare(
        self,
        request: PrepareTimedMediaEvidenceRequest,
        source: _Lease,
    ) -> ProducedTimedMediaEvidence:
        del request, source
        self.calls += 1
        raise TimedMediaEvidenceProducerError(
            "TIMED_SPEECH_RESULT_UNKNOWN",
            "transport ended after request admission",
            outcome="failed",
        )


class _CancellingProducer(_Producer):
    def prepare(
        self,
        request: PrepareTimedMediaEvidenceRequest,
        source: _Lease,
    ) -> ProducedTimedMediaEvidence:
        del request, source
        self.calls += 1
        raise KeyboardInterrupt


def _request(store: _Store, *, with_candidates: bool = True) -> PrepareTimedMediaEvidenceRequest:
    manifest, semantic_pack, _ = _manifest_and_candidate()
    if not with_candidates:
        semantic_pack = replace(semantic_pack, candidate_hypotheses=())
    source_blob = BlobRef(uuid4(), SOURCE_HASH, len(b"committed source"), "video/mp4")
    store.blobs[source_blob.object_id] = b"committed source"
    template = _bundle()
    registry = _register_sentence_profile(store, template)
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
        semantic_pack=semantic_pack,
        frame_pts_index=manifest.frame_pts_index_set,
        audio_sample_boundaries=template.audio_sample_boundaries,
        frame_detector_sha256=HASH_B,
        audio_detector_sha256=HASH_B,
        adaptive_policy=AdaptiveEvidenceWindowPolicy(
            "whole-episode-test-policy-v1",
            manifest.source_time_base,
            100,
            100,
            25,
            2,
            2,
        ),
        producer_policy_sha256=TEST_POLICY_SHA256,
        materialization_limits=MaterializationLimits(
            max_source_bytes=1024,
            timed_speech_max_request_bytes=1024,
            copy_chunk_bytes=128,
            staging_quota_bytes=1024,
        ),
        timed_speech_profile_registry_entry=registry,
    )


def _register_sentence_profile(
    store: _Store,
    bundle: RootMediaEvidenceBundle,
    kind: TimedSpeechProfileKind = TimedSpeechProfileKind.SENTENCE_BOUNDARY_GUARD_V1,
    vad_calibration_record_sha256: str = HASH_C,
) -> CommittedArtifactMemberReference:
    def requirement(
        evidence: object,
        calibration_record_sha256: str = HASH_C,
    ) -> TimedSpeechProducerRequirement:
        context = evidence.context
        return TimedSpeechProducerRequirement(
            producer_id=context.producer_id,
            generation_policy_sha256=context.generation_policy_sha256,
            model_sha256=HASH_B,
            adapter_sha256=HASH_A,
            calibration_record_sha256=calibration_record_sha256,
            clock_id=context.clock_id,
            time_base=context.time_base,
        )

    entry = TimedSpeechProfileRegistryEntry(
        profile_id=kind.value,
        profile_version="1",
        kind=kind,
        capability=(
            TimedSpeechCapability.KNOWN_SPEECH_ONLY
            if kind is TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1
            else TimedSpeechCapability.COMPLETE_DIALOGUE
        ),
        transcript_requirement=requirement(bundle.transcript),
        vad_requirement=requirement(bundle.speech_activity, vad_calibration_record_sha256),
        guard_policy=TimedSpeechGuardPolicy(
            policy_sha256=HASH_A,
            source_audio_clock_id=bundle.transcript.context.clock_id,
            source_audio_time_base=bundle.transcript.context.time_base,
            word_gap_tick=0,
            vad_merge_gap_tick=0,
            pre_roll_tick=0,
            post_roll_tick=0,
        ),
        registry_contract_sha256=HASH_A,
    )
    reference = CommittedArtifactMemberReference(
        receipt_id=uuid4(),
        artifact_set_id=uuid4(),
        member_ordinal=0,
        scope=command_module.TIMED_SPEECH_PROFILE_REGISTRY_SCOPE,
        artifact_type=command_module.TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE,
        logical_id=command_module.TIMED_SPEECH_PROFILE_REGISTRY_LOGICAL_ID,
        revision=1,
        content_hash=entry.canonical_hash,
    )
    store.registry_members[reference] = PersistedCommittedArtifactMember(
        reference=reference,
        payload_json=json.dumps(entry.to_mapping(), separators=(",", ":"), sort_keys=True),
        command_slot_id=uuid4(),
    )
    return reference


def _hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _provenance(
    source_provenance_sha256: str,
    bindings: tuple[CalibrationBinding, ...],
) -> str:
    invocation = {
        "argv_sha256": HASH_A,
        "executable": "ffprobe",
        "executable_sha256": HASH_A,
        "producer_kind": "probe",
        "stderr_sha256": HASH_A,
        "stdout_sha256": HASH_A,
        "version_evidence_sha256": HASH_A,
    }
    identities = [
        {
            "calibration_policy_sha256": HASH_A,
            "calibration_record_sha256": binding.calibration_record_sha256,
            "adapter_sha256": binding.adapter_sha256,
            "detector_sha256": binding.detector_sha256,
            "producer_id": binding.producer_id,
            "producer_kind": kind,
            "producer_policy_sha256": binding.policy_sha256,
            "producer_version": binding.producer_version,
            "timing_error_bound_tick": binding.timing_error_bound_tick,
        }
        for kind, binding in zip(
            (
                "frame",
                "audio",
                "asr",
                "vad",
                "shot",
                "scene",
                "visual",
                "subtitle",
            ),
            bindings,
            strict=True,
        )
    ]
    return json.dumps(
        {
            "producer_identities": identities,
            "schema_version": "local-media-producer-provenance-v1",
            "source_provenance_sha256": source_provenance_sha256,
            "tool_invocations": [invocation],
            "tool_trace_sha256": canonical_sha256([invocation]),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


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
    assert len(store.materializations) == 1
    assert store.closed_materializations == 1
    assert len(store.successes) == 1
    assert {item.artifact_type for item in store.successes[0].artifacts} == {
        "candidate_timed_evidence_index",
        "committed_video_to_audio_clock_map_certificate",
        "presentation_timeline_probe",
        "root_media_evidence_bundle",
        "timed_speech_profile_admission",
    }
    artifacts = {item.artifact_type: json.loads(item.payload_json) for item in store.successes[0].artifacts}
    assert (
        artifacts["timed_speech_profile_admission"]["registry_member_reference"]["content_hash"]
        == request.timed_speech_profile_registry_entry.content_hash
    )
    assert (
        artifacts["committed_video_to_audio_clock_map_certificate"]["probe_sha256"]
        == canonical_sha256(artifacts["presentation_timeline_probe"])
    )
    assert artifacts["committed_video_to_audio_clock_map_certificate"]["frame_pts_index_sha256"] == request.frame_pts_index.canonical_hash
    assert artifacts["committed_video_to_audio_clock_map_certificate"]["audio_boundary_set_sha256"] == request.audio_sample_boundaries.canonical_hash


def test_empty_candidate_pack_commits_explicit_empty_index() -> None:
    store = _Store()
    request = _request(store, with_candidates=False)

    result = PrepareTimedMediaEvidenceCommand(store, _Producer(_bundle())).execute(request)

    assert result.outcome.state == "succeeded"
    assert result.candidate_count == 0
    index = next(
        item
        for item in store.successes[0].artifacts
        if item.artifact_type == "candidate_timed_evidence_index"
    )
    payload = json.loads(index.payload_json)
    assert payload["candidate_count"] == 0
    assert payload["candidate_index_state"] == "empty"
    assert payload["candidate_blobs"] == []
    assert payload["semantic_pack_sha256"] == request.semantic_pack.canonical_hash


def test_command_rejects_missing_or_vad_mismatched_authority_profile() -> None:
    no_registry_store = _Store()
    no_registry_request = replace(_request(no_registry_store), timed_speech_profile_registry_entry=None)
    no_registry_producer = _Producer(_bundle())

    no_registry = PrepareTimedMediaEvidenceCommand(
        no_registry_store, no_registry_producer
    ).execute(no_registry_request)

    assert no_registry.outcome.state == "denied"
    assert no_registry_producer.calls == 0
    assert no_registry_store.successes == []

    mismatch_store = _Store()
    mismatch_request = _request(mismatch_store)
    mismatch_request = replace(
        mismatch_request,
        timed_speech_profile_registry_entry=_register_sentence_profile(
            mismatch_store,
            _bundle(),
            vad_calibration_record_sha256=HASH_A,
        ),
    )
    mismatch = PrepareTimedMediaEvidenceCommand(
        mismatch_store, _Producer(_bundle())
    ).execute(mismatch_request)

    assert mismatch.outcome.state == "denied"
    assert mismatch_store.successes == []


def test_probe_certificate_replays_exact_indexes_and_rejects_altered_identity() -> None:
    root = _bundle()
    probe, certificate = derive_presentation_timeline_facts(
        root,
        probe_tool_identity_sha256=HASH_A,
        probe_tool_version_sha256=HASH_B,
        probe_invocation_sha256=HASH_C,
        mapping_policy_sha256=HASH_A,
        snap_error_allowance_audio_tick=1,
    )

    certificate.assert_replays_probe(probe, root)
    with pytest.raises(Stage4PredecessorError):
        replace(certificate, probe_sha256=HASH_B).assert_replays_probe(probe, root)
    with pytest.raises(Stage4PredecessorError):
        certificate.assert_replays_probe(replace(probe, probe_tool_identity_sha256=HASH_B), root)
    with pytest.raises(Stage4PredecessorError):
        CommittedVideoToAudioClockMapCertificate(
            probe_sha256=probe.canonical_hash,
            root_evidence_sha256=root.canonical_hash,
            frame_pts_index_sha256=root.frame_pts_index.canonical_hash,
            audio_boundary_set_sha256=root.audio_sample_boundaries.canonical_hash,
            mapping_policy_sha256=HASH_A,
            algorithm_version="identity-full-duration-v1",
            snap_error_allowance_audio_tick=0,
            common_presentation_interval=certificate.common_presentation_interval,
            non_overlaps=certificate.non_overlaps,
        )


def test_vad_only_nonlexical_candidate_commits_with_unknown_sentence_fact() -> None:
    store = _Store()
    request = _request(store)
    template = _bundle()
    transcript = TranscriptSet(
        "nonlexical",
        template.transcript.context,
        template.transcript.coverage,
        TranscriptSourceOutcome.NO_LEXICAL_CONTENT,
        TranscriptCompleteness(
            EvidenceCompleteness.COMPLETE,
            EvidenceCompleteness.COMPLETE,
            EvidenceCompleteness.NOT_APPLICABLE,
        ),
        (),
        (),
        (),
    )
    command = PrepareTimedMediaEvidenceCommand(
        store, _Producer(replace(template, transcript=transcript))
    )
    request = replace(
        request,
        timed_speech_profile_registry_entry=_register_sentence_profile(
            store, template, TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1
        ),
    )

    result = command.execute(request)

    assert result.outcome.state == "succeeded"
    candidate_payloads = [
        json.loads(content) for content in store.blobs.values() if b'"window_assessment"' in content
    ]
    assert candidate_payloads[0]["window_assessment"]["sentence_completeness"] == "unknown"


def test_command_retries_busy_once_but_never_retries_unknown_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(command_module.time, "sleep", waits.append)
    busy_store = _Store()
    busy = _BusyOnceProducer(_bundle())
    busy_result = PrepareTimedMediaEvidenceCommand(busy_store, busy).execute(_request(busy_store))

    assert busy_result.outcome.state == "succeeded"
    assert busy.calls == 2
    assert waits == [1]

    unknown_store = _Store()
    unknown = _UnknownResultProducer(_bundle())
    unknown_result = PrepareTimedMediaEvidenceCommand(unknown_store, unknown).execute(
        _request(unknown_store)
    )

    assert unknown_result.outcome.state == "failed"
    assert unknown_result.outcome.failure_code == "TIMED_SPEECH_RESULT_UNKNOWN"
    assert unknown.calls == 1
    assert waits == [1]
    assert len(busy_store.materializations) == 1
    assert busy_store.closed_materializations == 1
    assert unknown_store.closed_materializations == 1


def test_declared_oversize_is_rejected_before_materialization_or_producer() -> None:
    store = _Store()
    request = replace(
        _request(store),
        materialization_limits=MaterializationLimits(
            max_source_bytes=1,
            timed_speech_max_request_bytes=1,
            copy_chunk_bytes=1,
            staging_quota_bytes=1,
        ),
    )
    producer = _Producer(_bundle())

    result = PrepareTimedMediaEvidenceCommand(store, producer).execute(request)

    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == "MEDIA_SOURCE_BYTE_LIMIT_EXCEEDED"
    assert store.materializations == []
    assert producer.calls == 0


def test_service_ceiling_rejects_before_materialization_or_producer() -> None:
    store = _Store()
    request = replace(
        _request(store),
        materialization_limits=MaterializationLimits(
            max_source_bytes=1024,
            timed_speech_max_request_bytes=1,
            copy_chunk_bytes=128,
            staging_quota_bytes=1024,
        ),
    )
    producer = _Producer(_bundle())

    result = PrepareTimedMediaEvidenceCommand(store, producer).execute(request)

    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == "MEDIA_SOURCE_BYTE_LIMIT_EXCEEDED"
    assert store.materializations == []
    assert producer.calls == 0


def test_cancellation_releases_the_claim_owned_materialization() -> None:
    store = _Store()
    producer = _CancellingProducer(_bundle())

    with pytest.raises(KeyboardInterrupt):
        PrepareTimedMediaEvidenceCommand(store, producer).execute(_request(store))

    assert producer.calls == 1
    assert len(store.materializations) == 1
    assert store.closed_materializations == 1


def test_command_rejects_producer_that_replaces_committed_physical_detector() -> None:
    store = _Store()
    request = replace(_request(store), frame_detector_sha256=HASH_A)

    result = PrepareTimedMediaEvidenceCommand(store, _Producer(_bundle())).execute(request)

    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == "TIMED_MEDIA_EVIDENCE_INVALID"


def test_required_detector_gap_commits_terminal_receipt_without_artifact_set() -> None:
    store = _Store()
    request = _request(store)
    producer = _Producer(_bundle(), fail=True)

    result = PrepareTimedMediaEvidenceCommand(store, producer).execute(request)

    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == "SUBTITLE_EVIDENCE_INDETERMINATE"
    assert producer.calls == 1
    assert store.successes == []


def test_batch_receipt_is_committed_only_after_rereading_exact_child() -> None:
    store = _Store()
    request = _request(store)
    child = PrepareTimedMediaEvidenceCommand(store, _Producer(_bundle())).execute(request)
    assert child.outcome.receipt_id is not None
    assert child.outcome.artifact_set_id is not None
    batch_request = FinalizeTimedMediaEvidenceBatchRequest(
        request.job,
        "media-preflight:batch",
        request.artifact_scope,
        1,
        request.source_manifest_sha256,
        request.source_provenance_sha256,
        (
            TimedMediaEvidenceBatchChild(
                0,
                request.idempotency_key,
                child.outcome.receipt_id,
                child.outcome.artifact_set_id,
            ),
        ),
    )

    result = FinalizeTimedMediaEvidenceBatchCommand(store).execute(batch_request)

    assert result.outcome.state == "succeeded"
    assert result.artifact is not None
    assert result.artifact.artifact_type == "timed_media_evidence_batch"
