from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from uuid import UUID, uuid4

from autocut_kernel.media import (
    SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE,
    SHADOW_CALIBRATION_RAW_RESPONSE_SCHEMA,
    CalibrationAnchor,
    CalibrationAnchorMatch,
    CalibrationMeasurementSummary,
    CalibrationObservation,
    CalibrationProducer,
    ProducerCalibrationMeasurement,
    ShadowCalibrationAsrObservation,
    ShadowCalibrationAudioClock,
    ShadowCalibrationContainer,
    ShadowCalibrationInvocation,
    ShadowCalibrationPolicies,
    ShadowCalibrationProducerIdentity,
    ShadowCalibrationProjection,
    ShadowCalibrationRawBlob,
    ShadowCalibrationRawContext,
    ShadowCalibrationRequestMapping,
    ShadowCalibrationSource,
    ShadowCalibrationSourceByteLimits,
    ShadowCalibrationTranscriptCapability,
    ShadowCalibrationWordGapSegment,
    TickRange,
    TimeBase,
)
from autocut_kernel.pipeline import (
    MeasureShadowCalibrationCommand,
    MeasureShadowCalibrationRequest,
    ShadowCalibrationCorpusMember,
    ShadowCalibrationInputs,
    ShadowCalibrationPortResult,
)
from autocut_kernel.store import (
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
)


def _sha(number: int) -> str:
    return f"sha256:{number:064x}"


TIME_BASE = TimeBase(1, 1_000)
CLOCK = ShadowCalibrationAudioClock("shadow-source-audio-clock", TIME_BASE, 0, 1_000)
SOURCE = ShadowCalibrationSource(
    "corpus-0001",
    _sha(1),
    _sha(10),
    "0f02e85b-d8c6-4d1b-a86b-3dc0f64d2f34",
    _sha(1),
    4_096,
    "video/mp4",
)
SOURCE_LIMITS = ShadowCalibrationSourceByteLimits(8_192, 4_096, 4_096)
CONTAINER = ShadowCalibrationContainer("video/mp4", ".mp4")
POLICIES = ShadowCalibrationPolicies(_sha(2), _sha(3), _sha(4), 100, 50)
CAPABILITY = ShadowCalibrationTranscriptCapability(
    "sensevoice_word_guard_v1",
    "complete",
    "utterance_gap_protected_range",
    "not_applicable",
    "complete",
    "required",
)
ASR_IDENTITY = ShadowCalibrationProducerIdentity(
    CalibrationProducer.ASR,
    "sensevoice-shadow",
    "1.0.0",
    _sha(5),
    _sha(6),
    _sha(7),
    "iic/SenseVoiceSmall",
    "main",
    _sha(8),
    "sensevoice-word-timestamp",
    _sha(9),
)
VAD_IDENTITY = ShadowCalibrationProducerIdentity(
    CalibrationProducer.VAD,
    "fsmn-shadow",
    "1.0.0",
    _sha(5),
    _sha(6),
    _sha(7),
    "iic/fsmn-vad",
    "main",
    _sha(11),
    "fsmn-vad-direct",
    _sha(9),
)
NATIVE_IDENTITY = _sha(12)


def _inputs() -> ShadowCalibrationInputs:
    return ShadowCalibrationInputs(
        _sha(20),
        _sha(21),
        _sha(22),
        NATIVE_IDENTITY,
        POLICIES.word_gap_policy_sha256,
        POLICIES.vad_merge_policy_sha256,
        _sha(23),
        _sha(24),
        ASR_IDENTITY.producer_id,
        VAD_IDENTITY.producer_id,
        CLOCK.clock_id,
        TIME_BASE,
    )


def _anchors() -> tuple[tuple[CalibrationAnchor, ...], tuple[CalibrationAnchor, ...]]:
    return (
        (
            CalibrationAnchor(
                "asr-anchor-0",
                CalibrationProducer.ASR,
                ASR_IDENTITY.producer_id,
                CLOCK.clock_id,
                TIME_BASE,
                TickRange(101, 220),
            ),
            CalibrationAnchor(
                "asr-anchor-1",
                CalibrationProducer.ASR,
                ASR_IDENTITY.producer_id,
                CLOCK.clock_id,
                TIME_BASE,
                TickRange(400, 561),
            ),
        ),
        (
            CalibrationAnchor(
                "vad-anchor-0",
                CalibrationProducer.VAD,
                VAD_IDENTITY.producer_id,
                CLOCK.clock_id,
                TIME_BASE,
                TickRange(81, 600),
            ),
        ),
    )


def _context(source: ShadowCalibrationSource = SOURCE) -> ShadowCalibrationRawContext:
    asr, vad = _anchors()
    return ShadowCalibrationRawContext(
        source,
        SOURCE_LIMITS,
        CONTAINER,
        CLOCK,
        POLICIES,
        NATIVE_IDENTITY,
        CAPABILITY,
        ASR_IDENTITY,
        VAD_IDENTITY,
        asr,
        vad,
    )


def _invocation(source: ShadowCalibrationSource = SOURCE) -> ShadowCalibrationInvocation:
    mapping = ShadowCalibrationRequestMapping(
        source,
        SOURCE_LIMITS,
        CONTAINER,
        CLOCK,
        CLOCK.full_range,
        NATIVE_IDENTITY,
        32_768,
        CAPABILITY,
        POLICIES.timed_speech_policy_sha256,
        POLICIES.word_gap_policy_sha256,
        POLICIES.vad_merge_policy_sha256,
        POLICIES.word_gap_ms,
        POLICIES.vad_merge_gap_ms,
        (ASR_IDENTITY, VAD_IDENTITY),
    )
    return ShadowCalibrationInvocation(
        source.corpus_member_reference_sha256, mapping.sha256, mapping, mapping.sha256
    )


def _request() -> MeasureShadowCalibrationRequest:
    return MeasureShadowCalibrationRequest(
        _inputs(),
        (
            ShadowCalibrationCorpusMember(
                SOURCE.corpus_member_reference_sha256, _sha(25), _context(), _invocation()
            ),
        ),
    )


SECOND_SOURCE = ShadowCalibrationSource(
    "corpus-0002",
    _sha(26),
    _sha(27),
    "73ad6766-3a13-49c3-9186-b7ccca505053",
    _sha(26),
    4_096,
    "video/mp4",
)


def _two_member_request() -> MeasureShadowCalibrationRequest:
    second_context = _context(SECOND_SOURCE)
    second_invocation = _invocation(SECOND_SOURCE)
    return MeasureShadowCalibrationRequest(
        _inputs(),
        (
            ShadowCalibrationCorpusMember(
                SOURCE.corpus_member_reference_sha256, _sha(25), _context(), _invocation()
            ),
            ShadowCalibrationCorpusMember(
                SECOND_SOURCE.corpus_member_reference_sha256,
                _sha(28),
                second_context,
                second_invocation,
            ),
        ),
    )


def _observation(
    identifier: str, producer: CalibrationProducer, producer_id: str, kind: str, interval: TickRange
) -> CalibrationObservation:
    return CalibrationObservation(
        identifier, producer, producer_id, kind, CLOCK.clock_id, TIME_BASE, interval
    )


def _measurement(
    producer: CalibrationProducer,
    producer_id: str,
    kind: str,
    anchors: tuple[CalibrationAnchor, ...],
    observations: tuple[CalibrationObservation, ...],
) -> ProducerCalibrationMeasurement:
    matches = tuple(
        CalibrationAnchorMatch(anchor, observation)
        for anchor, observation in zip(anchors, observations, strict=True)
    )
    return ProducerCalibrationMeasurement(
        producer,
        producer_id,
        kind,
        CLOCK.clock_id,
        TIME_BASE,
        matches,
        max(match.absolute_tick for match in matches),
    )


def _projection(invocation: ShadowCalibrationInvocation | None = None) -> ShadowCalibrationProjection:
    request_invocation = invocation or _invocation()
    asr_anchors, vad_anchors = _anchors()
    asr = (
        ShadowCalibrationAsrObservation(
            _observation(
                "asr-word-00000000",
                CalibrationProducer.ASR,
                ASR_IDENTITY.producer_id,
                "sensevoice-word-timestamp",
                TickRange(100, 220),
            ),
            "a",
        ),
        ShadowCalibrationAsrObservation(
            _observation(
                "asr-word-00000001",
                CalibrationProducer.ASR,
                ASR_IDENTITY.producer_id,
                "sensevoice-word-timestamp",
                TickRange(400, 560),
            ),
            "b",
        ),
    )
    vad = (
        _observation(
            "vad-segment-00000000",
            CalibrationProducer.VAD,
            VAD_IDENTITY.producer_id,
            "fsmn-vad-direct",
            TickRange(80, 600),
        ),
    )
    return ShadowCalibrationProjection(
        NATIVE_IDENTITY,
        request_invocation.request_identity_sha256,
        asr,
        (
            ShadowCalibrationWordGapSegment("asr-segment-00000000", "a", TickRange(100, 220)),
            ShadowCalibrationWordGapSegment("asr-segment-00000001", "b", TickRange(400, 560)),
        ),
        vad,
        CalibrationMeasurementSummary(
            _measurement(
                CalibrationProducer.ASR,
                ASR_IDENTITY.producer_id,
                "sensevoice-word-timestamp",
                asr_anchors,
                tuple(item.observation for item in asr),
            ),
            _measurement(
                CalibrationProducer.VAD,
                VAD_IDENTITY.producer_id,
                "fsmn-vad-direct",
                vad_anchors,
                vad,
            ),
        ),
    )


def _raw(invocation: ShadowCalibrationInvocation | None = None) -> bytes:
    request_invocation = invocation or _invocation()
    response = {
        "schema_version": SHADOW_CALIBRATION_RAW_RESPONSE_SCHEMA,
        "request_identity_sha256": request_invocation.request_identity_sha256,
        "source": request_invocation.request_mapping.source.to_response_mapping(),
        "audio_clock": CLOCK.to_mapping(),
        "requested_range": {"in_tick": 0, "out_tick": 1_000},
        "timed_speech_policy_sha256": POLICIES.timed_speech_policy_sha256,
        "word_gap_policy_sha256": POLICIES.word_gap_policy_sha256,
        "vad_merge_policy_sha256": POLICIES.vad_merge_policy_sha256,
        "native_profile_identity_sha256": NATIVE_IDENTITY,
        "producer_identities": [ASR_IDENTITY.to_mapping(), VAD_IDENTITY.to_mapping()],
        "asr_native_output": [
            {"text": "ab", "words": ["a", "b"], "timestamp": [[100, 220], [400, 560]]}
        ],
        "vad_native_output": [{"value": [[80, 260], [300, 600]]}],
    }
    return json.dumps(response, sort_keys=True, separators=(",", ":")).encode()


def _blob(
    raw: bytes | None = None, invocation: ShadowCalibrationInvocation | None = None
) -> ShadowCalibrationRawBlob:
    material = _raw(invocation) if raw is None else raw
    return ShadowCalibrationRawBlob(
        material,
        SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE,
        len(material),
        "sha256:" + hashlib.sha256(material).hexdigest(),
    )


def _result(
    *,
    invocation: ShadowCalibrationInvocation | None = None,
    raw_blob: ShadowCalibrationRawBlob | None = None,
    projection: ShadowCalibrationProjection | None = None,
) -> ShadowCalibrationPortResult:
    request_invocation = invocation or _invocation()
    return ShadowCalibrationPortResult(
        request_invocation,
        raw_blob or _blob(invocation=request_invocation),
        projection or _projection(request_invocation),
    )


@dataclass
class _Store:
    slot_id: UUID = field(default_factory=uuid4)
    terminal: CommandOutcome | None = None
    claims: list[CommandClaim] = field(default_factory=list)
    successes: list[CommandSuccess] = field(default_factory=list)
    rejections: list[CommandRejection] = field(default_factory=list)
    blobs: list[BlobRef] = field(default_factory=list)

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        self.claims.append(claim)
        if self.terminal is not None:
            return self.terminal
        return CommandOutcome(self.slot_id, "running", is_fresh_claim=True)

    def put_immutable_blob(
        self, job: Job, *, content: bytes, content_hash: str, media_type: str
    ) -> BlobRef:
        blob = BlobRef(uuid4(), content_hash, len(content), media_type)
        self.blobs.append(blob)
        return blob

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        self.successes.append(success)
        self.terminal = CommandOutcome(success.command_slot_id, "succeeded")
        return self.terminal

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome:
        self.rejections.append(rejection)
        self.terminal = CommandOutcome(rejection.command_slot_id, rejection.outcome)
        return self.terminal


@dataclass
class _Port:
    result: ShadowCalibrationPortResult
    calls: int = 0

    def measure(
        self, request: MeasureShadowCalibrationRequest, member: ShadowCalibrationCorpusMember
    ) -> ShadowCalibrationPortResult:
        self.calls += 1
        return self.result


@dataclass
class _MemberPort:
    results: dict[str, ShadowCalibrationPortResult]
    calls: int = 0

    def measure(
        self, request: MeasureShadowCalibrationRequest, member: ShadowCalibrationCorpusMember
    ) -> ShadowCalibrationPortResult:
        self.calls += 1
        return self.results[member.corpus_member_reference_sha256]


@dataclass
class _AmbiguousBlobWriteStore(_Store):
    def put_immutable_blob(
        self, job: Job, *, content: bytes, content_hash: str, media_type: str
    ) -> BlobRef:
        super().put_immutable_blob(
            job, content=content, content_hash=content_hash, media_type=media_type
        )
        raise TimeoutError("blob write outcome is ambiguous")


@dataclass
class _LaterBlobFailureStore(_Store):
    write_attempts: int = 0

    def put_immutable_blob(
        self, job: Job, *, content: bytes, content_hash: str, media_type: str
    ) -> BlobRef:
        self.write_attempts += 1
        if self.write_attempts == 2:
            raise ConnectionError("later blob write failed")
        return super().put_immutable_blob(
            job, content=content, content_hash=content_hash, media_type=media_type
        )


def _execute(result: ShadowCalibrationPortResult) -> tuple[CommandOutcome, _Store, _Port]:
    request, store, port = _request(), _Store(), _Port(result)
    return MeasureShadowCalibrationCommand(store, port).execute(request), store, port


def test_decoder_verified_raw_evidence_commits_exactly_two_ordered_non_authority_artifacts() -> (
    None
):
    request, store, port = _request(), _Store(), _Port(_result())

    outcome = MeasureShadowCalibrationCommand(store, port).execute(request)

    assert outcome.state == "succeeded"
    assert port.calls == 1
    assert len(store.blobs) == 1
    success = store.successes[0]
    assert [item.artifact_type for item in success.artifacts] == [
        "calibration_measurement_manifest",
        "calibration_measurement_results",
    ]
    assert [item.logical_id for item in success.artifacts] == [
        "measurement-manifest",
        "measurement-results",
    ]
    assert all(
        item.revision == 1 and item.scope.namespace != "autocut_authority"
        for item in success.artifacts
    )
    manifest, results = (json.loads(item.payload_json) for item in success.artifacts)
    assert (
        manifest["native_invocations"][0]["native_invocation"]["request_mapping_sha256"]
        == _invocation().request_mapping_sha256
    )
    assert (
        manifest["native_invocations"][0]["native_response_blob"]["content_hash"]
        == store.blobs[0].content_hash
    )
    projection = results["members"][0]["projection"]
    assert projection["asr_observations"][0]["text"] == "a"
    assert projection["word_gap_segments"] == [
        {
            "observed_range": {"in_tick": 100, "out_tick": 220},
            "segment_id": "asr-segment-00000000",
            "text": "a",
        },
        {
            "observed_range": {"in_tick": 400, "out_tick": 560},
            "segment_id": "asr-segment-00000001",
            "text": "b",
        },
    ]


def test_altered_port_projection_is_denied_before_any_blob_or_artifact_set() -> None:
    projection = _projection()
    altered = replace(
        projection,
        word_gap_segments=(
            ShadowCalibrationWordGapSegment("asr-segment-00000000", "ab", TickRange(100, 560)),
        ),
    )

    outcome, store, _ = _execute(_result(projection=altered))

    assert outcome.state == "denied"
    assert store.rejections[0].failure_code == "SHADOW_CALIBRATION_INVALID"
    assert not store.blobs and not store.successes


def test_altered_raw_envelope_is_denied_before_any_blob_or_artifact_set() -> None:
    payload = json.loads(_raw())
    payload["asr_native_output"][0]["timestamp"][0] = [101, 220]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    outcome, store, _ = _execute(_result(raw_blob=_blob(raw)))

    assert outcome.state == "denied"
    assert not store.blobs and not store.successes


def test_altered_port_invocation_is_denied_before_any_blob_or_artifact_set() -> None:
    invocation = _invocation()
    other_mapping = replace(invocation.request_mapping, max_response_bytes=32_769)
    altered = ShadowCalibrationInvocation(
        SOURCE.corpus_member_reference_sha256,
        other_mapping.sha256,
        other_mapping,
        other_mapping.sha256,
    )

    outcome, store, _ = _execute(_result(invocation=altered))

    assert outcome.state == "denied"
    assert not store.blobs and not store.successes


def test_old_generic_raw_response_is_not_accepted_as_calibration_evidence() -> None:
    generic = b'{"native":"response"}'

    outcome, store, _ = _execute(_result(raw_blob=_blob(generic)))

    assert outcome.state == "denied"
    assert store.rejections[0].failure_code == "SHADOW_CALIBRATION_INVALID"
    assert not store.blobs and not store.successes


def test_replay_returns_terminal_outcome_without_second_port_call_or_artifact_set() -> None:
    request, store, port = _request(), _Store(), _Port(_result())
    command = MeasureShadowCalibrationCommand(store, port)

    first = command.execute(request)
    replay = command.execute(request)

    assert first == replay
    assert port.calls == 1
    assert len(store.successes) == 1


def test_ambiguous_blob_write_propagates_without_a_terminal_outcome() -> None:
    request, store, port = _request(), _AmbiguousBlobWriteStore(), _Port(_result())

    try:
        MeasureShadowCalibrationCommand(store, port).execute(request)
    except TimeoutError as error:
        assert str(error) == "blob write outcome is ambiguous"
    else:
        raise AssertionError("ambiguous Store write must propagate")

    assert port.calls == 1
    assert len(store.blobs) == 1
    assert not store.successes and not store.rejections
    assert store.terminal is None


def test_two_member_measurement_keeps_local_observation_ids_and_aggregates_by_member() -> None:
    request = _two_member_request()
    first, second = (member.native_invocation for member in request.corpus_members)
    store = _Store()
    port = _MemberPort(
        {
            first.corpus_member_reference_sha256: _result(invocation=first),
            second.corpus_member_reference_sha256: _result(invocation=second),
        }
    )

    outcome = MeasureShadowCalibrationCommand(store, port).execute(request)

    assert outcome.state == "succeeded"
    assert port.calls == 2
    assert len(store.blobs) == 2
    results = json.loads(store.successes[0].artifacts[1].payload_json)
    assert [
        member["projection"]["asr_observations"][0]["observation"]["observation_id"]
        for member in results["members"]
    ] == ["asr-word-00000000", "asr-word-00000000"]
    assert [
        member["projection"]["vad_observations"][0]["observation_id"]
        for member in results["members"]
    ] == ["vad-segment-00000000", "vad-segment-00000000"]
    aggregate = results["per_producer_measurements"]
    for producer, anchor_count in (("asr", 4), ("vad", 2)):
        assert aggregate[producer]["aggregation"] == "member-bound-calibration-statistics-v1"
        assert aggregate[producer]["corpus_member_count"] == 2
        assert aggregate[producer]["corpus_member_references"] == [
            SOURCE.corpus_member_reference_sha256,
            SECOND_SOURCE.corpus_member_reference_sha256,
        ]
        assert aggregate[producer]["eligible_anchor_count"] == anchor_count
        assert aggregate[producer]["matched_anchor_count"] == anchor_count
        assert aggregate[producer]["invalid_or_indeterminate_member_count"] == 0
        assert "matches" not in aggregate[producer]


def test_later_blob_failure_leaves_no_artifact_set_and_only_unreferenced_recovery_blob() -> None:
    request = _two_member_request()
    first, second = (member.native_invocation for member in request.corpus_members)
    store = _LaterBlobFailureStore()
    port = _MemberPort(
        {
            first.corpus_member_reference_sha256: _result(invocation=first),
            second.corpus_member_reference_sha256: _result(invocation=second),
        }
    )

    try:
        MeasureShadowCalibrationCommand(store, port).execute(request)
    except ConnectionError as error:
        assert str(error) == "later blob write failed"
    else:
        raise AssertionError("Store failure must propagate")

    assert port.calls == 2
    assert len(store.blobs) == 1
    assert not store.successes and not store.rejections
    assert store.terminal is None
