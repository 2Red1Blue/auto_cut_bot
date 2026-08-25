from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from autocut_kernel.media import (
    SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE,
    SHADOW_CALIBRATION_RAW_RESPONSE_SCHEMA,
    CalibrationAnchor,
    CalibrationAnchorMatch,
    CalibrationMeasurementSummary,
    CalibrationObservation,
    CalibrationProducer,
    DecodedShadowCalibrationRawResponse,
    ProducerCalibrationMeasurement,
    ShadowCalibrationAsrObservation,
    ShadowCalibrationAudioClock,
    ShadowCalibrationInvocation,
    ShadowCalibrationPolicies,
    ShadowCalibrationProducerIdentity,
    ShadowCalibrationProjection,
    ShadowCalibrationRawBlob,
    ShadowCalibrationRawContext,
    ShadowCalibrationRawEvidenceError,
    ShadowCalibrationRequestMapping,
    ShadowCalibrationSource,
    TickRange,
    TimeBase,
    decode_shadow_calibration_raw_response,
)
from autocut_kernel.media.types import canonical_sha256


def _sha(number: int) -> str:
    return f"sha256:{number:064x}"


SOURCE = ShadowCalibrationSource("corpus-0001", _sha(1))
CLOCK = ShadowCalibrationAudioClock("audio-48k", TimeBase(1, 1_000), 0, 5_000)
POLICIES = ShadowCalibrationPolicies(_sha(2), _sha(3), _sha(4), 100, 50)
ASR_IDENTITY = ShadowCalibrationProducerIdentity(
    CalibrationProducer.ASR,
    "sensevoice-asr",
    "funasr-http-v1",
    "SenseVoiceSmall",
    "sensevoice-word-timestamp",
    _sha(5),
    _sha(6),
    _sha(7),
    "cpu",
    _sha(8),
)
VAD_IDENTITY = ShadowCalibrationProducerIdentity(
    CalibrationProducer.VAD,
    "fsmn-vad",
    "funasr-http-v1",
    "FSMN-VAD",
    "fsmn-vad-direct",
    _sha(5),
    _sha(6),
    _sha(7),
    "cpu",
    _sha(9),
)
NATIVE_IDENTITY = canonical_sha256([ASR_IDENTITY.to_mapping(), VAD_IDENTITY.to_mapping()])


def _request_mapping() -> ShadowCalibrationRequestMapping:
    return ShadowCalibrationRequestMapping(
        SOURCE,
        CLOCK,
        TickRange(0, 5_000),
        POLICIES.timed_speech_policy_sha256,
        POLICIES.word_gap_policy_sha256,
        POLICIES.vad_merge_policy_sha256,
        "required",
        (ASR_IDENTITY, VAD_IDENTITY),
    )


def _context(
    *, asr_anchors: tuple[CalibrationAnchor, ...] | None = None
) -> ShadowCalibrationRawContext:
    return ShadowCalibrationRawContext(
        SOURCE,
        CLOCK,
        POLICIES,
        NATIVE_IDENTITY,
        ASR_IDENTITY,
        VAD_IDENTITY,
        asr_anchors
        or (
            CalibrationAnchor(
                "asr-anchor-0",
                CalibrationProducer.ASR,
                "sensevoice-asr",
                "audio-48k",
                TimeBase(1, 1_000),
                TickRange(101, 220),
            ),
            CalibrationAnchor(
                "asr-anchor-1",
                CalibrationProducer.ASR,
                "sensevoice-asr",
                "audio-48k",
                TimeBase(1, 1_000),
                TickRange(400, 560),
            ),
        ),
        (
            CalibrationAnchor(
                "vad-anchor-0",
                CalibrationProducer.VAD,
                "fsmn-vad",
                "audio-48k",
                TimeBase(1, 1_000),
                TickRange(80, 599),
            ),
        ),
    )


def _invocation() -> ShadowCalibrationInvocation:
    request_mapping = _request_mapping()
    return ShadowCalibrationInvocation(
        _sha(10), request_mapping.sha256, request_mapping, request_mapping.sha256
    )


def _response() -> dict[str, object]:
    invocation = _invocation()
    return {
        "schema_version": SHADOW_CALIBRATION_RAW_RESPONSE_SCHEMA,
        "request_identity_sha256": invocation.request_identity_sha256,
        "source": SOURCE.to_mapping(),
        "audio_clock": CLOCK.to_mapping(),
        "requested_range": {"in_tick": 0, "out_tick": 5_000},
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


def _blob(
    raw: bytes, *, media_type: str = SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE
) -> ShadowCalibrationRawBlob:
    return ShadowCalibrationRawBlob(
        raw, media_type, len(raw), "sha256:" + hashlib.sha256(raw).hexdigest()
    )


def _measurement(
    producer: CalibrationProducer,
    producer_id: str,
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
        "sensevoice-word-timestamp" if producer is CalibrationProducer.ASR else "fsmn-vad-direct",
        "audio-48k",
        TimeBase(1, 1_000),
        matches,
        max(match.absolute_tick for match in matches),
    )


def _projection(context: ShadowCalibrationRawContext) -> ShadowCalibrationProjection:
    asr = (
        ShadowCalibrationAsrObservation(
            CalibrationObservation(
                "asr-word-00000000",
                CalibrationProducer.ASR,
                "sensevoice-asr",
                "sensevoice-word-timestamp",
                "audio-48k",
                TimeBase(1, 1_000),
                TickRange(100, 220),
            ),
            "a",
        ),
        ShadowCalibrationAsrObservation(
            CalibrationObservation(
                "asr-word-00000001",
                CalibrationProducer.ASR,
                "sensevoice-asr",
                "sensevoice-word-timestamp",
                "audio-48k",
                TimeBase(1, 1_000),
                TickRange(400, 560),
            ),
            "b",
        ),
    )
    vad = (
        CalibrationObservation(
            "vad-segment-00000000",
            CalibrationProducer.VAD,
            "fsmn-vad",
            "fsmn-vad-direct",
            "audio-48k",
            TimeBase(1, 1_000),
            TickRange(80, 600),
        ),
    )
    summary = CalibrationMeasurementSummary(
        _measurement(
            CalibrationProducer.ASR,
            "sensevoice-asr",
            context.asr_anchors,
            tuple(item.observation for item in asr),
        ),
        _measurement(CalibrationProducer.VAD, "fsmn-vad", context.vad_anchors, vad),
    )
    return ShadowCalibrationProjection(
        NATIVE_IDENTITY, _invocation().request_identity_sha256, asr, vad, summary
    )


def _material() -> tuple[
    ShadowCalibrationRawBlob,
    ShadowCalibrationInvocation,
    ShadowCalibrationRawContext,
    ShadowCalibrationProjection,
]:
    context = _context()
    raw = json.dumps(_response(), separators=(",", ":"), sort_keys=True).encode()
    return _blob(raw), _invocation(), context, _projection(context)


def _decode() -> DecodedShadowCalibrationRawResponse:
    return decode_shadow_calibration_raw_response(*_material())


def test_raw_response_derives_exact_projection_segments_matches_and_positive_summary() -> None:
    decoded = _decode()

    assert decoded.projection.asr_observations[0].observation.observed_range == TickRange(100, 220)
    assert decoded.projection.vad_observations == (
        CalibrationObservation(
            "vad-segment-00000000",
            CalibrationProducer.VAD,
            "fsmn-vad",
            "fsmn-vad-direct",
            "audio-48k",
            TimeBase(1, 1_000),
            TickRange(80, 600),
        ),
    )
    assert decoded.word_gap_segments[0].text == "a"
    assert decoded.word_gap_segments[1].text == "b"
    assert decoded.projection.summary.asr.accepted_bound_tick == 1
    assert decoded.projection.summary.vad.accepted_bound_tick == 1


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":"x","schema_version":"y"}',
        json.dumps(
            {"schema_version": SHADOW_CALIBRATION_RAW_RESPONSE_SCHEMA, "unknown": True}
        ).encode(),
        b"\xff",
        json.dumps({"schema_version": "timed-speech-evidence-response-v1"}).encode(),
    ],
)
def test_rejects_duplicate_unknown_non_utf8_and_normal_response_schemas(raw: bytes) -> None:
    _, invocation, context, projection = _material()
    with pytest.raises(
        ShadowCalibrationRawEvidenceError, match="SHADOW_CALIBRATION_RAW_EVIDENCE_INVALID"
    ):
        decode_shadow_calibration_raw_response(_blob(raw), invocation, context, projection)


def test_raw_blob_rejects_wrong_media_type_and_content_hash() -> None:
    raw = b"{}"
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        _blob(raw, media_type="application/json")
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        ShadowCalibrationRawBlob(
            raw, SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE, len(raw), _sha(99)
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", {"source_id": "other", "source_sha256": _sha(1)}),
        ("requested_range", {"in_tick": 1, "out_tick": 5_000}),
        ("timed_speech_policy_sha256", _sha(99)),
        ("native_profile_identity_sha256", _sha(99)),
    ],
)
def test_rejects_response_identity_clock_range_and_policy_drift(field: str, value: object) -> None:
    _, invocation, context, projection = _material()
    response = _response()
    response[field] = value
    raw = json.dumps(response, separators=(",", ":"), sort_keys=True).encode()
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        decode_shadow_calibration_raw_response(_blob(raw), invocation, context, projection)


@pytest.mark.parametrize("timestamps", [[[100.0, 220], [400, 560]], [[100, 300], [250, 560]], []])
def test_rejects_noninteger_overlapping_or_missing_asr_timestamps(timestamps: object) -> None:
    _, invocation, context, projection = _material()
    response = _response()
    response["asr_native_output"] = [{"text": "ab", "words": ["a", "b"], "timestamp": timestamps}]
    raw = json.dumps(response, separators=(",", ":"), sort_keys=True).encode()
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        decode_shadow_calibration_raw_response(_blob(raw), invocation, context, projection)


def test_rejects_unordered_vad_and_a_changed_merge_policy() -> None:
    _, invocation, context, projection = _material()
    response = _response()
    response["vad_native_output"] = [{"value": [[300, 600], [80, 260]]}]
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        decode_shadow_calibration_raw_response(
            _blob(json.dumps(response).encode()), invocation, context, projection
        )
    changed = replace(context, policies=replace(POLICIES, vad_merge_gap_ms=39))
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        decode_shadow_calibration_raw_response(*(_material()[:2] + (changed, projection)))


def test_rejects_injected_projection_and_partial_or_zero_bound_alignment() -> None:
    blob, invocation, context, projection = _material()
    bad_asr = replace(projection.asr_observations[0], text="injected")
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        decode_shadow_calibration_raw_response(
            blob,
            invocation,
            context,
            replace(projection, asr_observations=(bad_asr, projection.asr_observations[1])),
        )
    changed_id = replace(
        projection.asr_observations[0],
        observation=replace(
            projection.asr_observations[0].observation, observation_id="injected-id"
        ),
    )
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        decode_shadow_calibration_raw_response(
            blob,
            invocation,
            context,
            replace(projection, asr_observations=(changed_id, projection.asr_observations[1])),
        )
    partial_context = _context(asr_anchors=(_context().asr_anchors[0],))
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        decode_shadow_calibration_raw_response(blob, invocation, partial_context, projection)
    zero_context = _context(
        asr_anchors=(
            CalibrationAnchor(
                "asr-anchor-0",
                CalibrationProducer.ASR,
                "sensevoice-asr",
                "audio-48k",
                TimeBase(1, 1_000),
                TickRange(100, 220),
            ),
            CalibrationAnchor(
                "asr-anchor-1",
                CalibrationProducer.ASR,
                "sensevoice-asr",
                "audio-48k",
                TimeBase(1, 1_000),
                TickRange(400, 560),
            ),
        )
    )
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        decode_shadow_calibration_raw_response(blob, invocation, zero_context, projection)
