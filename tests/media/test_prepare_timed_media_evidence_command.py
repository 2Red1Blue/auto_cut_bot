from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import autocut_kernel.pipeline.prepare_timed_media_evidence_command as command_module
import pytest
from autocut_kernel.media import (
    AdaptiveEvidenceWindowPolicy,
    CalibrationBinding,
    EvidenceCompleteness,
    MediaKind,
    PresentationProbeExecution,
    PresentationSegmentContinuity,
    PresentationTimelineProbe,
    PresentationTrack,
    PresentationTrackSegment,
    RationalPresentationInterval,
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
from autocut_kernel.media.stage4_predecessor import _compile_presentation_map
from autocut_kernel.media.types import TickRange, TimeBase, canonical_sha256
from autocut_kernel.pipeline import (
    FinalizeTimedMediaEvidenceBatchCommand,
    FinalizeTimedMediaEvidenceBatchRequest,
    PrepareTimedMediaEvidenceCommand,
    PrepareTimedMediaEvidenceRequest,
    ProducedTimedMediaEvidence,
    TimedMediaEvidenceBatchChild,
    TimedMediaEvidenceProducerError,
)
from autocut_kernel.registry import BootstrappedTimedSpeechProfile
from autocut_kernel.registry.timed_speech import (
    DEFAULT_TIMED_SPEECH_AUTHORITY_SNAPSHOT,
    TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE,
    TIMED_SPEECH_PROFILE_REGISTRY_SCOPE,
    StoreAnchoredTimedSpeechProfileResolver,
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
    PersistedWholeSeriesSourceManifest,
    WholeSeriesSourceManifestReference,
)
from autocut_kernel.store.models import MaterializationLimits, canonical_payload_hash
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
        self.bootstrapped_reference: CommittedArtifactMemberReference | None = None
        self.bootstrapped_entry: TimedSpeechProfileRegistryEntry | None = None
        self.source_manifest: PersistedWholeSeriesSourceManifest | None = None
        self.source_decoded: object | None = None

    def read_whole_series_source_manifest(
        self, _: Job, artifact_set_id: UUID
    ) -> PersistedWholeSeriesSourceManifest:
        if self.source_manifest is None or self.source_manifest.artifact_set_id != artifact_set_id:
            raise ValueError("source manifest is unavailable")
        return self.source_manifest

    def read_committed_artifact_member(
        self,
        reference: CommittedArtifactMemberReference,
    ) -> PersistedCommittedArtifactMember:
        try:
            return self.registry_members[reference]
        except KeyError as error:
            raise ValueError("registry member is unavailable") from error

    def read_bootstrapped_timed_speech_profile(
        self, snapshot: object
    ) -> BootstrappedTimedSpeechProfile:
        if self.bootstrapped_reference is None or self.bootstrapped_entry is None:
            raise ValueError("authority anchor is unavailable")
        return BootstrappedTimedSpeechProfile(
            snapshot, self.bootstrapped_reference, self.bootstrapped_entry  # type: ignore[arg-type]
        )

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


def _decode_test_source_manifest(payload_json: str, _: tuple[object, ...]) -> object:
    try:
        return _TEST_SOURCE_MANIFESTS[payload_json]
    except KeyError as error:
        raise AssertionError("unexpected test source manifest payload") from error


_TEST_SOURCE_MANIFESTS: dict[str, object] = {}
command_module.decode_source_manifest = _decode_test_source_manifest  # type: ignore[assignment]


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
        transcript = replace(
            self.bundle.transcript,
            segments=tuple(
                replace(segment, sentence_ids=()) for segment in self.bundle.transcript.segments
            ),
            sentences=(),
            completeness=TranscriptCompleteness(
                EvidenceCompleteness.COMPLETE,
                EvidenceCompleteness.COMPLETE,
                EvidenceCompleteness.NOT_APPLICABLE,
            ),
        )
        bundle = replace(
            self.bundle,
            transcript=transcript,
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
        bindings = tuple(
            replace(item, calibration_record_sha256=HASH_A, detector_sha256=HASH_C)
            if item.producer_id == bundle.speech_activity.context.producer_id
            else item
            for item in _bindings(values)
        )
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
    _register_sentence_profile(store, template)
    job = Job("real-run-001", "shadow")
    payload_json = "{}"
    reference = WholeSeriesSourceManifestReference(
        ArtifactScope("pipeline", "job", job.job_key),
        "whole_series_source_manifest",
        1,
        canonical_payload_hash(payload_json),
    )
    receipt_id, artifact_set_id, command_slot_id = uuid4(), uuid4(), uuid4()
    store.source_manifest = PersistedWholeSeriesSourceManifest(
        reference,
        payload_json,
        (source_blob,),
        uuid4(),
        receipt_id,
        artifact_set_id,
        command_slot_id,
        job,
    )
    store.source_decoded = SimpleNamespace(
        episodes=(
            SimpleNamespace(
                proxy_blob=source_blob,
                manifest=manifest,
                media_probe=SimpleNamespace(
                    audio_sample_boundaries=template.audio_sample_boundaries,
                    frame_detector_sha256=HASH_B,
                    audio_detector_sha256=HASH_B,
                    presentation_timeline_probe=_presentation_facts(template, source_blob, manifest),
                ),
            ),
        )
    )
    _TEST_SOURCE_MANIFESTS[payload_json] = store.source_decoded
    return PrepareTimedMediaEvidenceRequest(
        job=job,
        idempotency_key="media-preflight:episode:0",
        episode_index=0,
        artifact_scope=ArtifactScope("pipeline", "job", "real-run-001"),
        artifact_revision=1,
        source_blob=source_blob,
        source_manifest_reference=reference,
        source_manifest_receipt_id=receipt_id,
        source_manifest_artifact_set_id=artifact_set_id,
        source_manifest_command_slot_id=command_slot_id,
        source_provenance_sha256=canonical_sha256(
            _persisted_source_manifest_provenance_mapping(store.source_manifest)
        ),
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
    )


def _persisted_source_manifest_provenance_mapping(
    persisted: PersistedWholeSeriesSourceManifest,
) -> dict[str, object]:
    reference = persisted.reference
    return {
        "artifact_reference": {
            "artifact_type": reference.artifact_type,
            "content_hash": reference.content_hash,
            "logical_id": reference.logical_id,
            "revision": reference.revision,
            "scope": {
                "key": reference.scope.key,
                "kind": reference.scope.kind,
                "namespace": reference.scope.namespace,
            },
        },
        "artifact_set_id": str(persisted.artifact_set_id),
        "command_slot_id": str(persisted.command_slot_id),
        "kernel_job_id": str(persisted.job_id),
        "receipt_id": str(persisted.receipt_id),
        "source_job": {
            "job_key": persisted.source_job.job_key,
            "profile": persisted.source_job.profile,
        },
    }


def _command(store: _Store, producer: object) -> PrepareTimedMediaEvidenceCommand:
    return PrepareTimedMediaEvidenceCommand(
        store,
        producer,  # type: ignore[arg-type]
        StoreAnchoredTimedSpeechProfileResolver(DEFAULT_TIMED_SPEECH_AUTHORITY_SNAPSHOT),
    )


def _register_sentence_profile(
    store: _Store,
    bundle: RootMediaEvidenceBundle,
    kind: TimedSpeechProfileKind = TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1,
    vad_calibration_record_sha256: str = HASH_A,
) -> CommittedArtifactMemberReference:
    def requirement(
        evidence: object,
        calibration_record_sha256: str | None = None,
    ) -> TimedSpeechProducerRequirement:
        context = evidence.context
        return TimedSpeechProducerRequirement(
            producer_id=context.producer_id,
            generation_policy_sha256=context.generation_policy_sha256,
            model_sha256=HASH_B if evidence is bundle.transcript else HASH_C,
            adapter_sha256=HASH_A,
            calibration_record_sha256=(
                HASH_C
                if evidence is bundle.transcript and calibration_record_sha256 is None
                else (HASH_A if calibration_record_sha256 is None else calibration_record_sha256)
            ),
            clock_id=context.clock_id,
            time_base=context.time_base,
            producer_kind="asr" if evidence is bundle.transcript else "vad",
            inference_kind=(
                "sensevoice-word-timestamp"
                if evidence is bundle.transcript
                else "fsmn-vad-direct"
            ),
        )

    entry = TimedSpeechProfileRegistryEntry(
        profile_id=TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1.value,
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
        scope=TIMED_SPEECH_PROFILE_REGISTRY_SCOPE,
        artifact_type=TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE,
        logical_id="timed-speech/sensevoice_word_guard_v1/1",
        revision=1,
        content_hash=entry.canonical_hash,
    )
    store.registry_members[reference] = PersistedCommittedArtifactMember(
        reference=reference,
        payload_json=json.dumps(entry.to_mapping(), separators=(",", ":"), sort_keys=True),
        command_slot_id=uuid4(),
    )
    store.bootstrapped_reference = reference
    store.bootstrapped_entry = entry
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
    command = _command(store, producer)

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
        == store.bootstrapped_reference.content_hash
    )
    assert (
        artifacts["committed_video_to_audio_clock_map_certificate"]["facts_sha256"]
        == canonical_sha256(artifacts["presentation_timeline_probe"])
    )
    assert artifacts["committed_video_to_audio_clock_map_certificate"]["frame_pts_index_sha256"] == request.frame_pts_index.canonical_hash
    assert artifacts["committed_video_to_audio_clock_map_certificate"]["audio_boundary_set_sha256"] == request.audio_sample_boundaries.canonical_hash


def test_empty_candidate_pack_commits_explicit_empty_index() -> None:
    store = _Store()
    request = _request(store, with_candidates=False)

    result = _command(store, _Producer(_bundle())).execute(request)

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
    no_registry_request = _request(no_registry_store)
    no_registry_store.bootstrapped_reference = None
    no_registry_store.bootstrapped_entry = None
    no_registry_producer = _Producer(_bundle())

    no_registry = _command(no_registry_store, no_registry_producer).execute(no_registry_request)

    assert no_registry.outcome.state == "denied"
    assert no_registry_producer.calls == 0
    assert no_registry_store.successes == []

    mismatch_store = _Store()
    mismatch_request = _request(mismatch_store)
    _register_sentence_profile(mismatch_store, _bundle(), vad_calibration_record_sha256=HASH_B)
    mismatch = _command(mismatch_store, _Producer(_bundle())).execute(mismatch_request)

    assert mismatch.outcome.state == "denied"
    assert mismatch_store.successes == []


def test_probe_certificate_replays_exact_indexes_and_rejects_altered_identity() -> None:
    root = _bundle()
    manifest, _, _ = _manifest_and_candidate()
    source_blob = BlobRef(uuid4(), SOURCE_HASH, len(b"committed source"), "video/mp4")
    probe = _presentation_facts(root, source_blob, manifest)
    calibration = CalibrationBinding(
        HASH_A,
        HASH_B,
        HASH_C,
        root.audio_sample_boundaries.context.producer_id,
        "fixture-v1",
        root.audio_sample_boundaries.context.time_base,
        1,
        True,
        HASH_A,
    )
    probe, certificate = derive_presentation_timeline_facts(
        root,
        probe=probe,
        source_manifest_sha256=HASH_B,
        audio_snap_calibration=calibration,
    )

    certificate.assert_replays_probe(
        probe, root, source_manifest_sha256=HASH_B, calibration_binding=calibration
    )
    with pytest.raises(Stage4PredecessorError):
        replace(certificate, facts_sha256=HASH_B).assert_replays_probe(
            probe, root, source_manifest_sha256=HASH_B, calibration_binding=calibration
        )
    with pytest.raises(Stage4PredecessorError):
        certificate.assert_replays_probe(
            replace(probe, facts_compiler_contract_sha256=HASH_B),
            root,
            source_manifest_sha256=HASH_B,
            calibration_binding=calibration,
        )
    with pytest.raises(Stage4PredecessorError):
        certificate.assert_replays_probe(
            replace(
                probe,
                video=replace(
                    probe.video,
                    segments=(
                        replace(probe.video.segments[0], decoded_boundary_sequence_sha256=HASH_B),
                    ),
                ),
            ),
            root,
            source_manifest_sha256=HASH_B,
            calibration_binding=calibration,
        )
    with pytest.raises(Stage4PredecessorError):
        replace(certificate, algorithm="identity-full-duration-v1")


def test_preflight_rejects_missing_source_presentation_facts() -> None:
    store = _Store()
    request = _request(store)
    assert store.source_decoded is not None
    store.source_decoded.episodes[0].media_probe.presentation_timeline_probe = None

    with pytest.raises(command_module.TimedMediaEvidenceCommandError, match="presentation"):
        _command(store, _Producer(_bundle())).execute(request)
    with pytest.raises(TypeError, match="presentation_timeline_probe"):
        replace(_request(store), presentation_timeline_probe=None)


def test_preflight_denies_mismatched_source_member_or_absent_episode_before_work() -> None:
    store = _Store()
    request = _request(store)
    producer = _Producer(_bundle())

    with pytest.raises(command_module.TimedMediaEvidenceCommandError, match="immutable handle"):
        _command(store, producer).execute(
            replace(request, source_manifest_receipt_id=uuid4())
        )
    with pytest.raises(command_module.TimedMediaEvidenceCommandError, match="episode is absent"):
        _command(store, producer).execute(replace(request, episode_index=1))

    assert producer.calls == 0
    assert store.materializations == []


def test_preflight_rejects_forged_source_provenance_before_claim_or_work() -> None:
    store = _Store()
    request = _request(store)
    producer = _Producer(_bundle())

    with pytest.raises(command_module.TimedMediaEvidenceCommandError, match="provenance"):
        _command(store, producer).execute(
            replace(request, source_provenance_sha256="sha256:" + "f" * 64)
        )

    assert store.outcomes == {}
    assert store.materializations == []
    assert store.successes == []
    assert producer.calls == 0


def test_command_requires_an_explicit_store_anchored_resolver() -> None:
    with pytest.raises(TypeError, match="authority_profile_resolver"):
        PrepareTimedMediaEvidenceCommand(_Store(), _Producer(_bundle()))


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
    command = _command(store, _Producer(replace(template, transcript=transcript)))
    _register_sentence_profile(store, template, TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1)

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
    busy_result = _command(busy_store, busy).execute(_request(busy_store))

    assert busy_result.outcome.state == "succeeded"
    assert busy.calls == 2
    assert waits == [1]

    unknown_store = _Store()
    unknown = _UnknownResultProducer(_bundle())
    unknown_result = _command(unknown_store, unknown).execute(
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

    result = _command(store, producer).execute(request)

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

    result = _command(store, producer).execute(request)

    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == "MEDIA_SOURCE_BYTE_LIMIT_EXCEEDED"
    assert store.materializations == []
    assert producer.calls == 0


def _presentation_facts(
    bundle: RootMediaEvidenceBundle,
    source_blob: BlobRef,
    manifest: object,
) -> PresentationTimelineProbe:
    def track(media_kind: MediaKind, index_hash: str, context: object, stream_index: int) -> PresentationTrack:
        time_base = context.time_base
        origin, end = context.origin_tick, context.end_tick
        return PresentationTrack(
            media_kind=media_kind,
            stream_index=stream_index,
            clock_id=context.clock_id,
            time_base=time_base,
            origin_tick=origin,
            end_tick=end,
            coverage_outcome=EvidenceCompleteness.COMPLETE,
            endpoint_proof="decoded_start_and_end",
            index_sha256=index_hash,
            segments=(
                PresentationTrackSegment(
                    TickRange(origin, end),
                    RationalPresentationInterval.from_fractions(
                        Fraction(origin * time_base.numerator, time_base.denominator),
                        Fraction(end * time_base.numerator, time_base.denominator),
                    ),
                    HASH_A,
                    PresentationSegmentContinuity.CONTINUOUS_DECODED,
                ),
            ),
        )

    return PresentationTimelineProbe(
        schema_version="presentation-map-facts-v2",
        source_id=bundle.source_id,
        source_sha256=source_blob.content_hash,
        source_blob_content_hash=source_blob.content_hash,
        source_blob_byte_length=source_blob.byte_length,
        source_blob_media_type=source_blob.media_type,
        facts_compiler_id="fixture-source-prep-v2",
        facts_compiler_contract_sha256=HASH_A,
        probe_execution=PresentationProbeExecution(
            "ffprobe-decoded-presentation-v2", HASH_A, HASH_B, HASH_C, HASH_A, source_blob.content_hash
        ),
        video=track(
            MediaKind.VIDEO,
            bundle.frame_pts_index.canonical_hash,
            bundle.frame_pts_index.context,
            0,
        ),
        audio=track(
            MediaKind.AUDIO,
            bundle.audio_sample_boundaries.canonical_hash,
            bundle.audio_sample_boundaries.context,
            1,
        ),
        frame_pts_index_set_sha256=bundle.frame_pts_index.canonical_hash,
        audio_sample_boundary_set_sha256=bundle.audio_sample_boundaries.canonical_hash,
        source_proxy_timeline_map_sha256=manifest.timeline_map.canonical_hash,
        window_manifest_sha256=manifest.canonical_hash,
    )


def _vector_track(
    media_kind: MediaKind,
    time_base: TimeBase,
    origin_tick: int,
    end_tick: int,
    segments: tuple[tuple[int, int, PresentationSegmentContinuity], ...],
) -> PresentationTrack:
    return PresentationTrack(
        media_kind=media_kind,
        stream_index=0 if media_kind is MediaKind.VIDEO else 1,
        clock_id=f"vector-{media_kind.value}",
        time_base=time_base,
        origin_tick=origin_tick,
        end_tick=end_tick,
        coverage_outcome=EvidenceCompleteness.COMPLETE,
        endpoint_proof="decoded_start_and_end",
        index_sha256=HASH_A if media_kind is MediaKind.VIDEO else HASH_B,
        segments=tuple(
            PresentationTrackSegment(
                TickRange(start, end),
                RationalPresentationInterval.from_fractions(
                    Fraction(start * time_base.numerator, time_base.denominator),
                    Fraction(end * time_base.numerator, time_base.denominator),
                ),
                HASH_C,
                continuity,
            )
            for start, end, continuity in segments
        ),
    )


def _vector_facts(video: PresentationTrack, audio: PresentationTrack) -> PresentationTimelineProbe:
    return PresentationTimelineProbe(
        "presentation-map-facts-v2",
        "vector-source",
        SOURCE_HASH,
        SOURCE_HASH,
        1,
        "video/mp4",
        "vector-source-prep-v2",
        HASH_A,
        PresentationProbeExecution(
            "ffprobe-decoded-presentation-v2", HASH_A, HASH_B, HASH_C, HASH_A, SOURCE_HASH
        ),
        video,
        audio,
        video.index_sha256,
        audio.index_sha256,
    )
def test_cancellation_releases_the_claim_owned_materialization() -> None:
    store = _Store()
    producer = _CancellingProducer(_bundle())

    with pytest.raises(KeyboardInterrupt):
        _command(store, producer).execute(_request(store))

    assert producer.calls == 1
    assert len(store.materializations) == 1
    assert store.closed_materializations == 1


def test_command_rejects_producer_that_replaces_committed_physical_detector() -> None:
    store = _Store()
    request = replace(_request(store), frame_detector_sha256=HASH_A)

    producer = _Producer(_bundle())
    with pytest.raises(command_module.TimedMediaEvidenceCommandError, match="source episode"):
        _command(store, producer).execute(request)
    assert producer.calls == 0


def test_required_detector_gap_commits_terminal_receipt_without_artifact_set() -> None:
    store = _Store()
    request = _request(store)
    producer = _Producer(_bundle(), fail=True)

    result = _command(store, producer).execute(request)

    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == "SUBTITLE_EVIDENCE_INDETERMINATE"
    assert producer.calls == 1
    assert store.successes == []


def test_batch_receipt_is_committed_only_after_rereading_exact_child() -> None:
    store = _Store()
    request = _request(store)
    child = _command(store, _Producer(_bundle())).execute(request)
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


def test_presentation_map_preserves_unequal_nonzero_source_pts() -> None:
    facts = _vector_facts(
        _vector_track(
            MediaKind.VIDEO,
            TimeBase(1, 90_000),
            90,
            270,
            ((90, 270, PresentationSegmentContinuity.CONTINUOUS_DECODED),),
        ),
        _vector_track(
            MediaKind.AUDIO,
            TimeBase(1, 48_000),
            48,
            144,
            ((48, 144, PresentationSegmentContinuity.CONTINUOUS_DECODED),),
        ),
    )

    segments, common, tails = _compile_presentation_map(facts)

    assert segments[0].video_tick_range == TickRange(90, 270)
    assert segments[0].audio_tick_range == TickRange(48, 144)
    assert common == (RationalPresentationInterval.from_fractions(Fraction(1, 1000), Fraction(3, 1000)),)
    assert tails == ()


def test_presentation_map_records_exact_tails_without_stretching() -> None:
    facts = _vector_facts(
        _vector_track(
            MediaKind.VIDEO,
            TimeBase(1, 90_000),
            0,
            270,
            ((0, 270, PresentationSegmentContinuity.CONTINUOUS_DECODED),),
        ),
        _vector_track(
            MediaKind.AUDIO,
            TimeBase(1, 48_000),
            48,
            96,
            ((48, 96, PresentationSegmentContinuity.CONTINUOUS_DECODED),),
        ),
    )

    segments, common, tails = _compile_presentation_map(facts)

    assert len(segments) == 1
    assert common == (RationalPresentationInterval.from_fractions(Fraction(1, 1000), Fraction(2, 1000)),)
    assert [item.to_mapping() for item in tails] == [
        {
            "media": "video",
            "position": "leading",
            "presentation_interval": RationalPresentationInterval.from_fractions(
                Fraction(0), Fraction(1, 1000)
            ).to_mapping(),
        },
        {
            "media": "video",
            "position": "trailing",
            "presentation_interval": RationalPresentationInterval.from_fractions(
                Fraction(2, 1000), Fraction(3, 1000)
            ).to_mapping(),
        },
    ]


def test_presentation_map_keeps_declared_gaps_and_rejects_unproved_discontinuity() -> None:
    video = _vector_track(
        MediaKind.VIDEO,
        TimeBase(1, 90_000),
        0,
        270,
        (
            (0, 90, PresentationSegmentContinuity.CONTINUOUS_DECODED),
            (90, 180, PresentationSegmentContinuity.DECLARED_GAP),
            (180, 270, PresentationSegmentContinuity.CONTINUOUS_DECODED),
        ),
    )
    audio = _vector_track(
        MediaKind.AUDIO,
        TimeBase(1, 48_000),
        0,
        144,
        (
            (0, 48, PresentationSegmentContinuity.CONTINUOUS_DECODED),
            (48, 96, PresentationSegmentContinuity.DECLARED_GAP),
            (96, 144, PresentationSegmentContinuity.CONTINUOUS_DECODED),
        ),
    )

    segments, common, tails = _compile_presentation_map(_vector_facts(video, audio))

    assert len(segments) == len(common) == 2
    assert tails == ()
    with pytest.raises(Stage4PredecessorError, match="undeclared source discontinuity"):
        _vector_track(
            MediaKind.VIDEO,
            TimeBase(1, 90_000),
            0,
            270,
            (
                (0, 90, PresentationSegmentContinuity.CONTINUOUS_DECODED),
                (180, 270, PresentationSegmentContinuity.CONTINUOUS_DECODED),
            ),
        )
