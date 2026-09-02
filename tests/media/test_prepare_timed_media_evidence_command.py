from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
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
    PTSIndex,
    RationalPresentationInterval,
    RootMediaEvidenceBundle,
    SpeechActivitySegment,
    SpeechActivitySet,
    SpeechSourceOutcome,
    Stage4PredecessorError,
    TimedSpeechCapability,
    TimedSpeechGuardPolicy,
    TimedSpeechProducerRequirement,
    TimedSpeechProfileKind,
    TimedSpeechProfileRegistryEntry,
    TranscriptCompleteness,
    TranscriptSegment,
    TranscriptSet,
    TranscriptSourceOutcome,
    TranscriptWord,
    derive_presentation_timeline_facts,
)
from autocut_kernel.media.ffprobe_port import ProbeResult
from autocut_kernel.media.stage4_predecessor import _compile_presentation_map
from autocut_kernel.media.types import (
    TickRange,
    TimeBase,
    ToolEvidence,
    VideoStreamEvidence,
    canonical_sha256,
)
from autocut_kernel.pipeline import (
    PrepareTimedMediaEvidenceCommand,
    PrepareTimedMediaEvidenceRequest,
    ProducedTimedMediaEvidence,
    TimedMediaEvidenceProducerError,
)
from autocut_kernel.registry import (
    AuthorityRegistrySnapshot,
    BootstrappedTimedSpeechProfile,
    TimedSpeechProfileKey,
)
from autocut_kernel.registry.timed_speech import (
    TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE,
    TIMED_SPEECH_PROFILE_REGISTRY_SCOPE,
    StoreAnchoredTimedSpeechProfileResolver,
)
from autocut_kernel.source_manifest import (
    DecodedBlobRef,
    DecodedMediaProbe,
    DecodedSeriesSource,
    DecodedSourceEpisode,
    DecodedSourceManifest,
    SourceOperationGrant,
    SourceOperationPolicy,
    decode_source_manifest,
    identity_frame_index,
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
from autocut_kernel.store.models import (
    CommittedSemanticInputs,
    CommittedSemanticInputsRequest,
    CommittedVlmSemanticInput,
    MaterializationLimits,
    PersistedVlmGenerationChild,
    PersistedVlmSemanticPack,
    SourceWindowIdentity,
    VlmRequestRecordReference,
    VlmSemanticPackReference,
    canonical_payload_hash,
)
from autocut_kernel.vlm import (
    ProxyTimelineMap,
    VlmRequestIdentity,
    WindowFrameSample,
    WindowManifestSet,
    WindowProxyBlobRef,
    parse_vlm_response,
)

from tests.media.test_root_evidence import HASH_A, HASH_B, HASH_C, SOURCE_HASH, _bundle
from tests.media.test_timed_evidence import _bindings, _manifest_and_candidate
from tests.vlm.test_parser import _context as _vlm_context
from tests.vlm.test_parser import _payload as _vlm_payload
from tests.vlm.test_parser import _raw as _vlm_raw

AUTHORITY_SNAPSHOT = AuthorityRegistrySnapshot(
    "sha256:" + "a" * 64,
    TimedSpeechProfileKey("sensevoice_word_guard_v1", "1"),
)

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
        self.semantic_inputs: CommittedSemanticInputs | None = None
        self.semantic_reads = 0
        self.claims: list[CommandClaim] = []

    def read_committed_semantic_inputs(self, request: CommittedSemanticInputsRequest) -> CommittedSemanticInputs:
        self.semantic_reads += 1
        value = self.semantic_inputs
        if value is None or request.job != value.source_manifest.source_job or request.vlm_semantic_pack_set != value.vlm_semantic_pack_set:
            raise ValueError("committed semantic input is unavailable")
        source = value.source_manifest
        ref = source.reference
        expected = CommittedArtifactMemberReference(
            source.receipt_id, source.artifact_set_id, 0, ref.scope,
            ref.artifact_type, ref.logical_id, ref.revision, ref.content_hash,
        )
        if request.source_manifest != expected:
            raise ValueError("committed semantic Source differs")
        return value

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
        self.claims.append(claim)
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
        root_template = _rebind_root_bundle(
            self.bundle, request.frame_pts_index, request.audio_sample_boundaries
        )
        transcript = replace(
            root_template.transcript,
            segments=tuple(
                replace(segment, sentence_ids=()) for segment in root_template.transcript.segments
            ),
            sentences=(),
            completeness=TranscriptCompleteness(
                EvidenceCompleteness.COMPLETE,
                EvidenceCompleteness.COMPLETE,
                EvidenceCompleteness.NOT_APPLICABLE,
            ),
        )
        bundle = replace(
            root_template,
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
        by_producer = {item.producer_id: item for item in bindings}
        bindings = tuple(by_producer[item.context.producer_id] for item in (
            bundle.frame_pts_index, bundle.audio_sample_boundaries, bundle.transcript,
            bundle.speech_activity, bundle.shot_boundaries, bundle.scene_boundaries,
            bundle.visual_validity, bundle.subtitle_cues,
        ))
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


class _BusyThenSuccessProducer(_Producer):
    def __init__(self, root: RootMediaEvidenceBundle, failures: int) -> None:
        super().__init__(root)
        self._failures = failures

    def prepare(
        self,
        request: PrepareTimedMediaEvidenceRequest,
        source: _Lease,
    ) -> ProducedTimedMediaEvidence:
        if self.calls < self._failures:
            self.calls += 1
            raise TimedMediaEvidenceProducerError(
                "TIMED_SPEECH_BUSY", "admission capacity is full", outcome="failed"
            )
        return super().prepare(request, source)


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
    manifest, semantic_pack, persisted_payload_json, canonical_template = _canonical_v2_source_manifest(
        manifest,
        semantic_pack,
        template,
        source_blob,
    )
    _register_sentence_profile(store, canonical_template)
    job = Job("real-run-001", "shadow")
    reference = WholeSeriesSourceManifestReference(
        ArtifactScope("pipeline", "job", job.job_key),
        "whole_series_source_manifest",
        1,
        canonical_payload_hash(persisted_payload_json),
    )
    receipt_id, artifact_set_id, command_slot_id = uuid4(), uuid4(), uuid4()
    store.source_manifest = PersistedWholeSeriesSourceManifest(
        reference,
        persisted_payload_json,
        (source_blob,),
        uuid4(),
        receipt_id,
        artifact_set_id,
        command_slot_id,
        job,
    )
    semantic_inputs_request, semantic_pack = _register_semantic_inputs(store, with_candidates=with_candidates)
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
        semantic_inputs_request=semantic_inputs_request,
        window_manifest=manifest,
        semantic_pack=semantic_pack,
        frame_pts_index=manifest.frame_pts_index_set,
        audio_sample_boundaries=canonical_template.audio_sample_boundaries,
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


def _register_semantic_inputs(store: _Store, *, with_candidates: bool):
    """Typed fake Store rows with genuinely parsed VLM; no SQL/provider authority."""
    source = store.source_manifest
    assert source is not None
    decoded = decode_source_manifest(source.payload_json, source.proxy_blobs)
    episode = decoded.episodes[0]
    manifest, manifest_set = episode.manifest, episode.manifest_set
    _, _, parse_policy, template = _vlm_context()
    request_content = b'{"fixture":"VLM request"}'
    request_blob = BlobRef(uuid4(), _hash(request_content), len(request_content), "application/json")
    identity = VlmRequestIdentity.from_manifest(
        manifest, manifest_set, prompt_template_sha256=template.prompt_template_sha256,
        prompt_version=template.prompt_version, response_schema_sha256=template.response_schema_sha256,
        model_id=template.model_id, provider_id=template.provider_id,
        request_parameters_sha256=template.request_parameters_sha256,
        request_payload_sha256=request_blob.content_hash, parse_policy=parse_policy,
    )
    payload = _vlm_payload(manifest)

    def retarget(value):
        if type(value) is dict:
            if "supporting_frame_ids" in value:
                value["supporting_frame_ids"] = [manifest.frame_samples[2].frame_id]
            for nested in value.values():
                retarget(nested)
        elif type(value) is list:
            for nested in value:
                retarget(nested)

    retarget(payload)
    if not with_candidates:
        payload["candidate_hypotheses"] = []
    raw = _vlm_raw(payload)
    pack = parse_vlm_response(raw, manifest=manifest, manifest_set=manifest_set,
                              request_identity=identity, policy=parse_policy)
    raw_blob = BlobRef(uuid4(), _hash(raw), len(raw), "application/json")
    store.blobs[request_blob.object_id], store.blobs[raw_blob.object_id] = request_content, raw
    scope = source.reference.scope
    attempt, slot, receipt, artifact_set = uuid4(), uuid4(), uuid4(), uuid4()
    request_payload = {
        "attempt_id": str(attempt), "episode_index": 0, "idempotency_key": "fixture-vlm-child",
        "provider_idempotency_key": "fixture-vlm-provider", "proxy_blob": command_module._blob_mapping(source.proxy_blobs[0]),
        "request_hash": HASH_A, "request_identity": identity.to_mapping(),
        "request_identity_sha256": identity.canonical_hash,
        "request_payload_blob": command_module._blob_mapping(request_blob),
        "source_manifest_sha256": source.reference.content_hash,
        "source_provenance_sha256": source.canonical_hash,
        "window_manifest_set_sha256": manifest_set.canonical_hash,
        "window_manifest_sha256": manifest.canonical_hash,
    }
    request_json = json.dumps(request_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    child = PersistedVlmGenerationChild(
        VlmRequestRecordReference(scope, f"vlm_request_{manifest.canonical_hash[7:31]}", 1, canonical_payload_hash(request_json)),
        request_json, source.source_job, source.job_id, slot, "fixture-vlm-child", HASH_A,
        attempt, "fixture-vlm-provider", request_blob, receipt, artifact_set, 0,
        manifest.canonical_hash, manifest_set.canonical_hash, source.reference.content_hash,
        source.canonical_hash, identity.canonical_hash,
    )
    pack_json = json.dumps(pack.to_mapping(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    persisted = PersistedVlmSemanticPack(
        VlmSemanticPackReference(scope, f"semantic_pack_{manifest.canonical_hash[7:39]}", 1, canonical_payload_hash(pack_json)),
        pack_json, pack, child,
    )
    response = CommittedArtifactMemberReference(receipt, artifact_set, 1, scope,
        "vlm_response_record", f"vlm_response_{manifest.canonical_hash[7:31]}", 1, HASH_B)
    window = SourceWindowIdentity(0, manifest.stream_index, manifest.core_range.start_pts,
        manifest.core_range.end_pts, manifest.canonical_hash, manifest.source_id,
        manifest.source_sha256, manifest.source_clock_id, manifest_set.canonical_hash, source.proxy_blobs[0])
    aggregate = CommittedArtifactMemberReference(uuid4(), uuid4(), 0, scope,
        "vlm_semantic_pack_set", "vlm_semantic_pack_set", 1, HASH_C)
    store.semantic_inputs = CommittedSemanticInputs(source, decoded.census, aggregate,
        child.request_policy, (CommittedVlmSemanticInput(window, identity, persisted, response, raw_blob),))
    ref = source.reference
    selector = CommittedSemanticInputsRequest(source.source_job, CommittedArtifactMemberReference(
        source.receipt_id, source.artifact_set_id, 0, ref.scope, ref.artifact_type,
        ref.logical_id, ref.revision, ref.content_hash), aggregate)
    return selector, pack


def _canonical_v2_source_manifest(
    manifest: object,
    semantic_pack: object,
    template: RootMediaEvidenceBundle,
    source_blob: BlobRef,
) -> tuple[object, object, str, RootMediaEvidenceBundle]:
    """Build a persisted V2 source payload accepted by the production decoder."""
    assert hasattr(manifest, "source_time_base")
    source = DecodedSeriesSource(
        "episode.mp4",
        template.source_id,
        source_blob.content_hash,
        source_blob.byte_length,
    )
    video_range = TickRange(0, 100)
    probe = ProbeResult(
        VideoStreamEvidence(0, "h264", 96, 64, manifest.source_time_base),
        PTSIndex((0, 25, 50, 75)),
        ToolEvidence("ffprobe", "fixture-v2", HASH_A),
    )
    media_probe = DecodedMediaProbe(
        source,
        probe,
        video_range,
        _canonical_audio_boundaries(template),
        HASH_B,
        HASH_B,
    )
    frame_index = identity_frame_index(media_probe)
    canonical_manifest = replace(
        manifest,
        source_clock_id=frame_index.context.clock_id,
        source_range=video_range,
        core_range=video_range,
        frame_pts_index_set=frame_index,
        proxy_blob_ref=WindowProxyBlobRef(
            str(source_blob.object_id),
            source_blob.content_hash,
            source_blob.byte_length,
            source_blob.media_type,
        ),
        timeline_map=ProxyTimelineMap.translation(
            time_base=manifest.source_time_base,
            proxy_range=TickRange(0, video_range.duration_pts),
            source_start_pts=video_range.start_pts,
        ),
        frame_samples=tuple(
            WindowFrameSample(tick, tick, HASH_C) for tick in probe.pts_index.ticks
        ),
    )
    manifest_set = WindowManifestSet(
        canonical_manifest.source_id,
        canonical_manifest.source_clock_id,
        canonical_manifest.source_sha256,
        canonical_manifest.stream_index,
        canonical_manifest.source_time_base,
        canonical_manifest.source_range,
        (canonical_manifest,),
    )
    presentation = _canonical_presentation_facts(
        _rebind_root_bundle(template, frame_index, media_probe.audio_sample_boundaries),
        source_blob,
        canonical_manifest,
        probe.pts_index.ticks,
    )
    media_probe = replace(
        media_probe,
        presentation_timeline_probe=presentation,
        presentation_video_frame_boundaries=((0, 25), (25, 50), (50, 75), (75, 100)),
        presentation_audio_frame_boundaries=((0, 50), (50, 100)),
    )
    canonical_pack = _pack_for_manifest(semantic_pack, canonical_manifest.canonical_hash)
    decoded = DecodedSourceManifest(
        SourceOperationGrant(
            SourceOperationPolicy("fixture-authority", "fixture-series", 1, ("semantic_analysis", "render_source")),
            "all_or_nothing",
            (source,),
        ),
        (
            DecodedSourceEpisode(
                media_probe,
                DecodedBlobRef(
                    source_blob.object_id,
                    source_blob.content_hash,
                    source_blob.byte_length,
                    source_blob.media_type,
                ),
                canonical_manifest,
                manifest_set,
            ),
        ),
    )
    canonical_template = _rebind_root_bundle(
        template, frame_index, media_probe.audio_sample_boundaries
    )
    return (
        canonical_manifest,
        canonical_pack,
        json.dumps(decoded.to_mapping(), separators=(",", ":"), sort_keys=True),
        canonical_template,
    )


def _pack_for_manifest(semantic_pack: object, manifest_sha256: str) -> object:
    def with_manifest_hash(item: object) -> object:
        return replace(
            item,
            support=replace(item.support, core_owner_window_manifest_sha256=manifest_sha256),
        )

    return replace(
        semantic_pack,
        window_manifest_sha256=manifest_sha256,
        entities=tuple(with_manifest_hash(item) for item in semantic_pack.entities),
        facts=tuple(with_manifest_hash(item) for item in semantic_pack.facts),
        events=tuple(with_manifest_hash(item) for item in semantic_pack.events),
        candidate_hypotheses=tuple(
            with_manifest_hash(item) for item in semantic_pack.candidate_hypotheses
        ),
    )


def _canonical_presentation_facts(
    bundle: RootMediaEvidenceBundle,
    source_blob: BlobRef,
    manifest: object,
    video_pts: tuple[int, ...],
) -> PresentationTimelineProbe:
    def track(
        media_kind: MediaKind,
        index_hash: str,
        context: object,
        stream_index: int,
        boundaries: tuple[tuple[int, int], ...],
    ) -> PresentationTrack:
        time_base = context.time_base
        origin, end = context.origin_tick, context.end_tick
        return PresentationTrack(
            media_kind=media_kind,
            stream_index=stream_index,
            clock_id=(f"video-stream-{stream_index}" if media_kind is MediaKind.VIDEO else f"audio-stream-{stream_index}"),
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
                    canonical_sha256(
                        {
                            "boundaries": [
                                {"end_tick": boundary_end, "start_tick": start}
                                for start, boundary_end in boundaries
                            ],
                            "kind": "decoded-continuous-run-v2",
                        }
                    ),
                    PresentationSegmentContinuity.CONTINUOUS_DECODED,
                ),
            ),
        )

    video_boundaries = tuple(
        (start, end) for start, end in zip(video_pts, (*video_pts[1:], 100), strict=True)
    )
    audio_boundaries = ((0, 50), (50, 100))
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
            manifest.frame_pts_index_set.canonical_hash,
            manifest.frame_pts_index_set.context,
            0,
            video_boundaries,
        ),
        audio=track(
            MediaKind.AUDIO,
            bundle.audio_sample_boundaries.canonical_hash,
            bundle.audio_sample_boundaries.context,
            1,
            audio_boundaries,
        ),
        frame_pts_index_set_sha256=manifest.frame_pts_index_set.canonical_hash,
        audio_sample_boundary_set_sha256=bundle.audio_sample_boundaries.canonical_hash,
        source_proxy_timeline_map_sha256=manifest.timeline_map.canonical_hash,
        window_manifest_sha256=manifest.canonical_hash,
    )


def _canonical_audio_boundaries(bundle: RootMediaEvidenceBundle) -> object:
    context = replace(bundle.audio_sample_boundaries.context, clock_id="audio-stream-1")
    return replace(
        bundle.audio_sample_boundaries,
        context=context,
        coverage=replace(bundle.audio_sample_boundaries.coverage, clock_id=context.clock_id),
        points=tuple(
            replace(point, clock_id=context.clock_id)
            for point in bundle.audio_sample_boundaries.points
        ),
    )


def _rebind_root_bundle(
    bundle: RootMediaEvidenceBundle,
    frame_index: object,
    audio_boundaries: object,
) -> RootMediaEvidenceBundle:
    video_context = frame_index.context
    audio_context = audio_boundaries.context

    def video_record(record: object) -> object:
        return replace(record, clock_id=video_context.clock_id)

    def audio_record(record: object) -> object:
        return replace(record, clock_id=audio_context.clock_id)

    shots = replace(
        bundle.shot_boundaries,
        context=replace(bundle.shot_boundaries.context, clock_id=video_context.clock_id),
        coverage=replace(bundle.shot_boundaries.coverage, clock_id=video_context.clock_id),
        frame_pts_index_set_sha256=frame_index.canonical_hash,
        points=tuple(video_record(point) for point in bundle.shot_boundaries.points),
    )
    scenes = replace(
        bundle.scene_boundaries,
        context=replace(bundle.scene_boundaries.context, clock_id=video_context.clock_id),
        coverage=replace(bundle.scene_boundaries.coverage, clock_id=video_context.clock_id),
        frame_pts_index_set_sha256=frame_index.canonical_hash,
        points=tuple(video_record(point) for point in bundle.scene_boundaries.points),
    )
    transcript = replace(
        bundle.transcript,
        context=replace(bundle.transcript.context, clock_id=audio_context.clock_id),
        coverage=replace(bundle.transcript.coverage, clock_id=audio_context.clock_id),
        segments=tuple(audio_record(item) for item in bundle.transcript.segments),
        words=tuple(audio_record(item) for item in bundle.transcript.words),
        sentences=tuple(audio_record(item) for item in bundle.transcript.sentences),
    )
    speech = replace(
        bundle.speech_activity,
        context=replace(bundle.speech_activity.context, clock_id=audio_context.clock_id),
        coverage=replace(bundle.speech_activity.coverage, clock_id=audio_context.clock_id),
        segments=tuple(audio_record(item) for item in bundle.speech_activity.segments),
    )
    visual = replace(
        bundle.visual_validity,
        context=replace(bundle.visual_validity.context, clock_id=video_context.clock_id),
        coverage=replace(bundle.visual_validity.coverage, clock_id=video_context.clock_id),
        intervals=tuple(video_record(item) for item in bundle.visual_validity.intervals),
    )
    subtitles = replace(
        bundle.subtitle_cues,
        context=replace(bundle.subtitle_cues.context, clock_id=video_context.clock_id),
        coverage=replace(bundle.subtitle_cues.coverage, clock_id=video_context.clock_id),
        cues=tuple(video_record(item) for item in bundle.subtitle_cues.cues),
    )
    return replace(
        bundle,
        frame_pts_index=frame_index,
        shot_boundaries=shots,
        scene_boundaries=scenes,
        audio_sample_boundaries=audio_boundaries,
        transcript=transcript,
        speech_activity=speech,
        visual_validity=visual,
        subtitle_cues=subtitles,
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
        StoreAnchoredTimedSpeechProfileResolver(AUTHORITY_SNAPSHOT),
    )


def _register_sentence_profile(
    store: _Store,
    bundle: RootMediaEvidenceBundle,
    kind: TimedSpeechProfileKind = TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1,
    vad_calibration_record_sha256: str = HASH_A,
) -> CommittedArtifactMemberReference:
    def requirement(
        evidence: object,
        *,
        producer_kind: str,
        calibration_record_sha256: str | None = None,
    ) -> TimedSpeechProducerRequirement:
        context = evidence.context
        is_asr = producer_kind == "asr"
        return TimedSpeechProducerRequirement(
            producer_id=context.producer_id,
            generation_policy_sha256=context.generation_policy_sha256,
            model_sha256=HASH_B if is_asr else HASH_C,
            adapter_sha256=HASH_A,
            calibration_record_sha256=(
                HASH_C
                if is_asr and calibration_record_sha256 is None
                else (HASH_A if calibration_record_sha256 is None else calibration_record_sha256)
            ),
            clock_id=context.clock_id,
            time_base=context.time_base,
            producer_kind=producer_kind,
            inference_kind=(
                "sensevoice-word-timestamp"
                if is_asr
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
        transcript_requirement=requirement(bundle.transcript, producer_kind="asr"),
        vad_requirement=requirement(
            bundle.speech_activity,
            producer_kind="vad",
            calibration_record_sha256=vad_calibration_record_sha256,
        ),
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
    root_payload = json.loads(store.successes[0].artifacts[0].payload_json)
    from autocut_kernel.media.timed_evidence_codec import decode_calibration_binding
    bindings = tuple(decode_calibration_binding(item) for item in root_payload["calibration_bindings"])
    assert len(bindings) == 8
    assert all(item.active for item in bindings)
    admission = json.loads(store.successes[0].artifacts[2].payload_json)
    root = json.loads(store.blobs[UUID(root_payload["blob"]["object_id"])])
    by_producer = {item.producer_id: item for item in bindings}
    assert admission["transcript_calibration_sha256"] == by_producer[root["transcript"]["context"]["producer_id"]].canonical_hash
    assert admission["vad_calibration_sha256"] == by_producer[root["speech_activity"]["context"]["producer_id"]].canonical_hash


@pytest.mark.parametrize("with_candidates", (False, True))
@pytest.mark.parametrize("mutation", ("clock", "missing", "duplicate", "policy", "producer"))
def test_complete_calibration_closure_is_required_even_without_candidates(with_candidates, mutation):
    class ChangedBindingProducer(_Producer):
        def prepare(self, request, source):
            produced = super().prepare(request, source)
            bindings = list(produced.calibration_bindings)
            index = next(i for i, item in enumerate(bindings)
                         if item.producer_id == produced.root_bundle.shot_boundaries.context.producer_id)
            if mutation == "clock":
                bindings[index] = replace(bindings[index], time_base=TimeBase(1, 999))
            elif mutation == "missing":
                del bindings[index]
            elif mutation == "duplicate":
                bindings.append(bindings[index])
            elif mutation == "policy":
                bindings[index] = replace(bindings[index], policy_sha256=HASH_C)
            else:
                bindings[index] = replace(bindings[index], producer_id="foreign-shot")
            return replace(produced, calibration_bindings=tuple(bindings))

    store = _Store()
    request = _request(store, with_candidates=with_candidates)
    outcome = _command(store, ChangedBindingProducer(_bundle())).execute(request).outcome
    assert outcome.state == "denied"
    assert "calibration" in outcome.failure_detail_json
    assert store.successes == [] and store.closed_materializations == 1


@pytest.mark.parametrize("with_candidates", (False, True))
@pytest.mark.parametrize("field,value", [
    ("timing_error_bound_tick", value)
    for value in (True, False, 1.0, 0, -1, "1", None, [], {}, float("nan"), float("inf"))
] + [
    (field, value)
    for field in ("producer_id", "producer_version")
    for value in (None, True, 1, 1.0, "", " \t", "\ud800", [], {})
])
def test_provenance_identity_leaves_reject_before_equality_comparison(with_candidates, field, value):
    store = _Store()
    request = _request(store, with_candidates=with_candidates)
    resolved = command_module.resolve_committed_timed_media_request(store, request)
    produced = _Producer(_bundle()).prepare(resolved, _Lease(request.source_blob, store))
    provenance = json.loads(produced.producer_provenance_json)
    provenance["producer_identities"][0][field] = value
    raw = json.dumps(provenance, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    with pytest.raises(command_module.TimedMediaEvidenceCommandError, match="producer (timing bound|identity)"):
        replace(produced, producer_provenance_json=raw)


@pytest.mark.parametrize("value", (True, 1.0))
@pytest.mark.parametrize("with_candidates", (False, True))
def test_invalid_provenance_bound_commits_denial_not_evidence(value, with_candidates):
    class InvalidProvenanceProducer(_Producer):
        def prepare(self, request, source):
            produced = super().prepare(request, source)
            provenance = json.loads(produced.producer_provenance_json)
            provenance["producer_identities"][0]["timing_error_bound_tick"] = value
            return replace(produced, producer_provenance_json=json.dumps(
                provenance, ensure_ascii=False, separators=(",", ":"), sort_keys=True))

    store = _Store()
    request = _request(store, with_candidates=with_candidates)
    result = _command(store, InvalidProvenanceProducer(_bundle())).execute(request)
    assert result.outcome.state == "denied"
    assert "exact positive integer" in result.outcome.failure_detail_json
    assert not store.successes and store.closed_materializations == 1


def test_provenance_producer_text_preserves_valid_unicode_without_normalization():
    store = _Store()
    request = _request(store, with_candidates=False)
    resolved = command_module.resolve_committed_timed_media_request(store, request)
    produced = _Producer(_bundle()).prepare(resolved, _Lease(request.source_blob, store))
    version = " 版本一😀 "
    provenance = json.loads(produced.producer_provenance_json)
    provenance["producer_identities"][0]["producer_version"] = version
    bindings = (replace(produced.calibration_bindings[0], producer_version=version),
                *produced.calibration_bindings[1:])
    updated = replace(produced, calibration_bindings=bindings,
        producer_provenance_json=json.dumps(provenance, ensure_ascii=False,
                                           separators=(",", ":"), sort_keys=True))
    command_module.validate_produced_timed_media_evidence(resolved, updated)
    assert json.loads(updated.producer_provenance_json)["producer_identities"][0]["producer_version"] == version


def test_provenance_requires_explicit_adapter_and_allows_null_only_for_physical_producers():
    store = _Store()
    request = _request(store, with_candidates=False)
    resolved = command_module.resolve_committed_timed_media_request(store, request)
    produced = _Producer(_bundle()).prepare(resolved, _Lease(request.source_blob, store))
    provenance = json.loads(produced.producer_provenance_json)
    provenance["producer_identities"][0]["adapter_sha256"] = None
    accepted = replace(
        produced,
        calibration_bindings=(
            replace(produced.calibration_bindings[0], adapter_sha256=None),
            *produced.calibration_bindings[1:],
        ),
        producer_provenance_json=json.dumps(
            provenance, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ),
    )
    command_module.validate_produced_timed_media_evidence(resolved, accepted)

    provenance["producer_identities"][0]["adapter_sha256"] = "not-a-hash"
    with pytest.raises(command_module.TimedMediaEvidenceCommandError, match="adapter hash"):
        replace(
            produced,
            producer_provenance_json=json.dumps(
                provenance, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ),
        )

    provenance = json.loads(produced.producer_provenance_json)
    provenance["producer_identities"][2]["adapter_sha256"] = None
    with pytest.raises(command_module.TimedMediaEvidenceCommandError, match="speech producer adapter"):
        replace(
            produced,
            producer_provenance_json=json.dumps(
                provenance, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ),
        )


def test_exact_semantic_selector_is_required_and_fully_hash_bound():
    store = _Store()
    request = _request(store)
    selector = request.semantic_inputs_request
    assert request.canonical_payload()["semantic_inputs_request"] == {
        "job": {"job_key": request.job.job_key, "profile": request.job.profile},
        "source_manifest": selector.source_manifest.to_mapping(),
        "vlm_semantic_pack_set": selector.vlm_semantic_pack_set.to_mapping(),
    }
    with pytest.raises(command_module.TimedMediaEvidenceCommandError, match="must be exact"):
        replace(request, semantic_inputs_request=None)
    with pytest.raises(command_module.TimedMediaEvidenceCommandError, match="exact Source member"):
        replace(request, semantic_inputs_request=replace(selector, job=Job("foreign", "shadow")))
    for field, value in (("receipt_id", uuid4()), ("artifact_set_id", uuid4()),
                         ("revision", 2), ("content_hash", HASH_B)):
        changed = replace(request, semantic_inputs_request=replace(selector,
            vlm_semantic_pack_set=replace(selector.vlm_semantic_pack_set, **{field: value})))
        assert changed.request_hash != request.request_hash
        producer = _Producer(_bundle())
        with pytest.raises(command_module.TimedMediaEvidenceCommandError):
            _command(store, producer).execute(changed)
        assert producer.calls == 0
    assert store.claims == [] and store.materializations == []


@pytest.mark.parametrize("mutation", ("summary", "remove_candidates"))
def test_caller_semantic_pack_cannot_replace_the_committed_pack(mutation):
    store = _Store()
    request = _request(store)
    original = request.semantic_pack
    forged = (replace(original, candidate_hypotheses=()) if mutation == "remove_candidates"
              else replace(original, window_summary=replace(original.window_summary, summary="forged")))
    producer = _Producer(_bundle())
    with pytest.raises(command_module.TimedMediaEvidenceCommandError, match="committed VLM pack"):
        _command(store, producer).execute(replace(request, semantic_pack=forged))
    assert store.claims == [] and store.materializations == [] and producer.calls == 0


@pytest.mark.parametrize("field,value", [
    ("episode_index", 1), ("stream_index", 1), ("core_start_pts", 1),
    ("core_end_pts", 99), ("source_id", "foreign"), ("source_sha256", HASH_B),
    ("source_clock_id", "foreign-clock"), ("window_manifest_set_sha256", HASH_B),
])
def test_committed_window_must_join_exact_source_episode_before_work(field, value):
    store = _Store()
    request = _request(store)
    inputs = store.semantic_inputs
    selected = inputs.inputs[0]
    store.semantic_inputs = replace(inputs, inputs=(replace(selected,
        source_window=replace(selected.source_window, **{field: value})),))
    producer = _Producer(_bundle())
    with pytest.raises(command_module.TimedMediaEvidenceCommandError, match="committed VLM pack"):
        _command(store, producer).execute(request)
    assert store.claims == [] and store.materializations == [] and producer.calls == 0


@pytest.mark.parametrize("field,value", [
    ("receipt_id", UUID(int=900)), ("artifact_set_id", UUID(int=901)),
    ("member_ordinal", 2), ("logical_id", "foreign-response"),
    ("artifact_type", "vlm_request_record"), ("revision", 2),
])
def test_committed_response_reference_must_be_the_matching_child(field, value):
    store = _Store()
    request = _request(store)
    inputs = store.semantic_inputs
    selected = inputs.inputs[0]
    store.semantic_inputs = replace(inputs, inputs=(replace(selected,
        response_record=replace(selected.response_record, **{field: value})),))
    with pytest.raises(command_module.TimedMediaEvidenceCommandError, match="committed VLM pack"):
        _command(store, _Producer(_bundle())).execute(request)
    assert store.claims == [] and store.materializations == []


def test_shared_request_hash_is_the_actual_claim_hash_and_replay_rereads_inputs():
    store = _Store()
    request = _request(store)
    resolved = command_module.resolve_committed_timed_media_request(store, request)
    expected = command_module.timed_media_request_hash(resolved, AUTHORITY_SNAPSHOT)
    assert expected == canonical_sha256({
        "authority_registry_set_sha256": AUTHORITY_SNAPSHOT.registry_set_sha256,
        "authority_profile_key": AUTHORITY_SNAPSHOT.enabled_profile.value,
        "request": resolved.canonical_payload(),
    })
    assert expected != request.request_hash
    producer = _Producer(_bundle())
    command = _command(store, producer)
    assert command.execute(request).outcome.state == "succeeded"
    assert store.claims[-1].request_hash == expected
    before = store.semantic_reads
    assert command.execute(request).outcome.state == "succeeded"
    assert store.semantic_reads == before + 1 and producer.calls == 1
    store.semantic_inputs = None
    with pytest.raises(command_module.TimedMediaEvidenceCommandError):
        command.execute(request)
    assert producer.calls == 1


@pytest.mark.parametrize("purpose", ("semantic_analysis", "render_source"))
def test_missing_source_operation_rejects_before_claim_or_work(purpose):
    store = _Store()
    request = _request(store)
    source = store.source_manifest
    decoded = decode_source_manifest(source.payload_json, source.proxy_blobs)
    policy = replace(decoded.census.policy, authorized_purposes=(purpose,))
    decoded = replace(decoded, census=replace(decoded.census, policy=policy))
    raw = json.dumps(decoded.to_mapping(), separators=(",", ":"), sort_keys=True)
    source = replace(source, payload_json=raw,
                     reference=replace(source.reference, content_hash=canonical_payload_hash(raw)))
    store.source_manifest = source
    selector, pack = _register_semantic_inputs(store, with_candidates=True)
    request = replace(request, source_manifest_reference=source.reference,
        source_provenance_sha256=source.canonical_hash, semantic_inputs_request=selector,
        semantic_pack=pack)
    producer = _Producer(_bundle())
    with pytest.raises(command_module.TimedMediaEvidenceCommandError):
        _command(store, producer).execute(request)
    assert store.claims == [] and store.materializations == [] and producer.calls == 0


@pytest.mark.parametrize("with_candidates", (False, True))
def test_public_closure_helpers_replay_persisted_plans_and_complete_bindings(with_candidates):
    store = _Store()
    request = _request(store, with_candidates=with_candidates)
    resolved = command_module.resolve_committed_timed_media_request(store, request)
    produced = _Producer(_bundle()).prepare(resolved, _Lease(request.source_blob, store))
    command_module.validate_produced_timed_media_evidence(resolved, produced)
    plans, candidates = command_module.close_timed_media_candidates(resolved, produced)
    assert _command(store, _Producer(_bundle())).execute(request).outcome.state == "succeeded"
    root, index = (json.loads(item.payload_json) for item in store.successes[0].artifacts[:2])
    assert root["calibration_bindings"] == [item.to_mapping() for item in produced.calibration_bindings]
    plan_payload = json.loads(store.blobs[UUID(index["plan_blob"]["object_id"])])
    assert plan_payload["plans"] == [item.to_mapping() for item in plans]
    assert index["candidate_set_sha256"] == [item.canonical_hash for item in candidates]


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


def test_preflight_real_decoder_rejects_v1_source_manifest_before_claim_or_work() -> None:
    store = _Store()
    request = _request(store)
    assert store.source_manifest is not None
    persisted_payload = json.loads(store.source_manifest.payload_json)
    persisted_probe = persisted_payload["episodes"][0]["media_probe"]
    persisted_probe.pop("presentation_timeline_probe")
    persisted_probe.pop("decoded_video_frame_boundaries")
    persisted_probe.pop("decoded_audio_frame_boundaries")
    payload_json = json.dumps(persisted_payload, separators=(",", ":"), sort_keys=True)
    reference = replace(
        store.source_manifest.reference,
        content_hash=canonical_payload_hash(payload_json),
    )
    store.source_manifest = replace(
        store.source_manifest,
        reference=reference,
        payload_json=payload_json,
    )
    request = replace(
        request,
        source_manifest_reference=reference,
        semantic_inputs_request=replace(request.semantic_inputs_request,
            source_manifest=replace(request.semantic_inputs_request.source_manifest,
                                    content_hash=reference.content_hash)),
        source_provenance_sha256=canonical_sha256(
            _persisted_source_manifest_provenance_mapping(store.source_manifest)
        ),
    )
    producer = _Producer(_bundle())

    with pytest.raises(command_module.TimedMediaEvidenceCommandError, match="invalid for preflight"):
        _command(store, producer).execute(request)

    assert store.outcomes == {}
    assert store.materializations == []
    assert producer.calls == 0
    with pytest.raises(TypeError, match="presentation_timeline_probe"):
        replace(_request(store), presentation_timeline_probe=None)


def test_preflight_denies_mismatched_source_member_or_absent_episode_before_work() -> None:
    store = _Store()
    request = _request(store)
    producer = _Producer(_bundle())

    with pytest.raises(command_module.TimedMediaEvidenceCommandError, match="exact Source member"):
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


def test_vad_only_nonlexical_candidate_preserves_explicit_sentence_not_applicable() -> None:
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
    nonlexical_template = _rebind_root_bundle(
        replace(template, transcript=transcript),
        request.frame_pts_index,
        request.audio_sample_boundaries,
    )
    command = _command(store, _Producer(replace(template, transcript=transcript)))
    _register_sentence_profile(
        store,
        nonlexical_template,
        TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1,
    )

    result = command.execute(request)

    assert result.outcome.state == "succeeded"
    candidate_payloads = [
        json.loads(content) for content in store.blobs.values() if b'"window_assessment"' in content
    ]
    assert candidate_payloads[0]["window_assessment"]["sentence_completeness"] == "not_applicable"


def test_assessment_uses_absolute_audio_time_for_offset_boundary_touch() -> None:
    template = _bundle()
    audio_context = replace(
        template.audio_sample_boundaries.context,
        origin_tick=10,
        duration_tick=100,
    )
    transcript_context = replace(template.transcript.context, origin_tick=10, duration_tick=100)
    speech_context = replace(template.speech_activity.context, origin_tick=10, duration_tick=100)
    audio = replace(
        template.audio_sample_boundaries,
        context=audio_context,
        coverage=replace(template.audio_sample_boundaries.coverage, in_tick=10, out_tick=110),
        points=tuple(
            replace(point, tick=tick)
            for point, tick in zip(template.audio_sample_boundaries.points, (10, 60, 110), strict=True)
        ),
    )
    word = TranscriptWord(
        "offset-word",
        template.source_id,
        template.source_sha256,
        transcript_context.clock_id,
        transcript_context.time_base,
        13,
        15,
        "crosses video start",
    )
    transcript = TranscriptSet(
        "offset-transcript",
        transcript_context,
        replace(template.transcript.coverage, in_tick=10, out_tick=110),
        TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE,
        TranscriptCompleteness(
            EvidenceCompleteness.COMPLETE,
            EvidenceCompleteness.COMPLETE,
            EvidenceCompleteness.NOT_APPLICABLE,
        ),
        (
            TranscriptSegment(
                "offset-segment",
                template.source_id,
                template.source_sha256,
                transcript_context.clock_id,
                transcript_context.time_base,
                13,
                15,
                (),
                "crosses video start",
            ),
        ),
        (word,),
        (),
    )
    speech = SpeechActivitySet(
        "offset-vad",
        speech_context,
        replace(template.speech_activity.coverage, in_tick=10, out_tick=110),
        SpeechSourceOutcome.SPEECH_DETECTED,
        (
            SpeechActivitySegment(
                "offset-speech",
                template.source_id,
                template.source_sha256,
                speech_context.clock_id,
                speech_context.time_base,
                13,
                15,
                900_000,
            ),
        ),
    )
    root = replace(template, audio_sample_boundaries=audio, transcript=transcript, speech_activity=speech)
    manifest, semantic_pack, candidate = _manifest_and_candidate()
    window = command_module.plan_candidate_evidence_window(
        candidate,
        semantic_pack,
        manifest,
        manifest.frame_pts_index_set,
        command_module.AdaptiveEvidenceWindowPolicy(
            "offset-assessment-v1", manifest.source_time_base, 0, 0, 1, 2, 2
        ),
    ).final_window

    assessment = command_module._assess_window(window, root, margin_tick=2)

    assert assessment.transcript_left_boundary_touch is True
    assert assessment.speech_left_boundary_touch is True
    assert assessment.left_truncated is True
    assert assessment.sentence_completeness.value == "not_applicable"


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


def test_selected_recompute_uses_its_frozen_transient_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(command_module.time, "sleep", waits.append)
    store = _Store()
    producer = _BusyThenSuccessProducer(_bundle(), failures=3)
    request = replace(_request(store), transient_retry_budget=3)

    result = _command(store, producer).execute(request)

    assert result.outcome.state == "succeeded"
    assert producer.calls == 4
    assert waits == [1, 1, 1]
    assert request.canonical_payload()["transient_retry_budget"] == 3

    zero_store = _Store()
    zero = _BusyThenSuccessProducer(_bundle(), failures=1)
    zero_request = replace(_request(zero_store), transient_retry_budget=0)
    zero_result = _command(zero_store, zero).execute(zero_request)
    assert zero_result.outcome.state == "failed"
    assert zero.calls == 1
    assert zero_request.canonical_payload()["transient_retry_budget"] == 0


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
