from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.media import (
    SHADOW_CALIBRATION_ANCHOR_SET_SCHEMA,
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
    ShadowCalibrationContainer,
    ShadowCalibrationInvocation,
    ShadowCalibrationPolicies,
    ShadowCalibrationProducerIdentity,
    ShadowCalibrationProjection,
    ShadowCalibrationRawBlob,
    ShadowCalibrationRawContext,
    ShadowCalibrationRawEvidenceError,
    ShadowCalibrationRequestMapping,
    ShadowCalibrationSource,
    ShadowCalibrationSourceByteLimits,
    ShadowCalibrationTranscriptCapability,
    ShadowCalibrationWordGapSegment,
    TickRange,
    TimeBase,
    decode_shadow_calibration_invocation,
    decode_shadow_calibration_raw_context,
    decode_shadow_calibration_raw_response,
    derive_shadow_calibration_raw_response,
    shadow_calibration_anchor_reference_sha256,
    shadow_calibration_context_mapping,
    shadow_calibration_invocation_mapping,
    shadow_calibration_projection_mapping,
)


def _sha(number: int) -> str:
    return f"sha256:{number:064x}"


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
CLOCK = ShadowCalibrationAudioClock("audio-30k", TimeBase(1_001, 30_000), 300, 300)
POLICIES = ShadowCalibrationPolicies(_sha(2), _sha(3), _sha(4), 100, 50)
TRANSCRIPT_CAPABILITY = ShadowCalibrationTranscriptCapability(
    "sensevoice_word_guard_v1",
    "complete",
    "utterance_gap_protected_range",
    "not_applicable",
    "complete",
    "required",
)
ASR_IDENTITY = ShadowCalibrationProducerIdentity(
    CalibrationProducer.ASR,
    "sensevoice-asr",
    "1.0.0",
    _sha(5),
    _sha(6),
    _sha(7),
    "iic/SenseVoiceSmall",
    "main",
    _sha(9),
    "sensevoice-word-timestamp",
    _sha(11),
)
VAD_IDENTITY = ShadowCalibrationProducerIdentity(
    CalibrationProducer.VAD,
    "fsmn-vad",
    "1.0.0",
    _sha(5),
    _sha(6),
    _sha(7),
    "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    "main",
    _sha(12),
    "fsmn-vad-direct",
    _sha(11),
)
NATIVE_IDENTITY = _sha(13)


def _request_mapping() -> ShadowCalibrationRequestMapping:
    return ShadowCalibrationRequestMapping(
        SOURCE,
        SOURCE_LIMITS,
        CONTAINER,
        CLOCK,
        TickRange(300, 600),
        NATIVE_IDENTITY,
        32_768,
        TRANSCRIPT_CAPABILITY,
        POLICIES.timed_speech_policy_sha256,
        POLICIES.word_gap_policy_sha256,
        POLICIES.vad_merge_policy_sha256,
        POLICIES.word_gap_ms,
        POLICIES.vad_merge_gap_ms,
        (ASR_IDENTITY, VAD_IDENTITY),
    )


def _context(
    *, asr_anchors: tuple[CalibrationAnchor, ...] | None = None
) -> ShadowCalibrationRawContext:
    return ShadowCalibrationRawContext(
        SOURCE,
        SOURCE_LIMITS,
        CONTAINER,
        CLOCK,
        POLICIES,
        NATIVE_IDENTITY,
        TRANSCRIPT_CAPABILITY,
        ASR_IDENTITY,
        VAD_IDENTITY,
        asr_anchors
        or (
            CalibrationAnchor(
                "asr-anchor-0",
                CalibrationProducer.ASR,
                "sensevoice-asr",
                "audio-30k",
                TimeBase(1_001, 30_000),
                TickRange(303, 307),
            ),
            CalibrationAnchor(
                "asr-anchor-1",
                CalibrationProducer.ASR,
                "sensevoice-asr",
                "audio-30k",
                TimeBase(1_001, 30_000),
                TickRange(311, 317),
            ),
        ),
        (
            CalibrationAnchor(
                "vad-anchor-0",
                CalibrationProducer.VAD,
                "fsmn-vad",
                "audio-30k",
                TimeBase(1_001, 30_000),
                TickRange(302, 317),
            ),
        ),
    )


def _invocation() -> ShadowCalibrationInvocation:
    request_mapping = _request_mapping()
    return ShadowCalibrationInvocation(
        SOURCE.corpus_member_reference_sha256,
        request_mapping.sha256,
        request_mapping,
        request_mapping.sha256,
    )


def _response() -> dict[str, object]:
    invocation = _invocation()
    return {
        "schema_version": SHADOW_CALIBRATION_RAW_RESPONSE_SCHEMA,
        "request_identity_sha256": invocation.request_identity_sha256,
        "source": SOURCE.to_response_mapping(),
        "audio_clock": CLOCK.to_mapping(),
        "requested_range": {"in_tick": 300, "out_tick": 600},
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
        "audio-30k",
        TimeBase(1_001, 30_000),
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
                "audio-30k",
                TimeBase(1_001, 30_000),
                TickRange(302, 307),
            ),
            "a",
        ),
        ShadowCalibrationAsrObservation(
            CalibrationObservation(
                "asr-word-00000001",
                CalibrationProducer.ASR,
                "sensevoice-asr",
                "sensevoice-word-timestamp",
                "audio-30k",
                TimeBase(1_001, 30_000),
                TickRange(311, 317),
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
            "audio-30k",
            TimeBase(1_001, 30_000),
            TickRange(302, 318),
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
        NATIVE_IDENTITY,
        _invocation().request_identity_sha256,
        asr,
        (
            ShadowCalibrationWordGapSegment("asr-segment-00000000", "a", TickRange(302, 307)),
            ShadowCalibrationWordGapSegment("asr-segment-00000001", "b", TickRange(311, 317)),
        ),
        vad,
        summary,
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

    assert decoded.projection.asr_observations[0].observation.observed_range == TickRange(302, 307)
    assert decoded.projection.vad_observations == (
        CalibrationObservation(
            "vad-segment-00000000",
            CalibrationProducer.VAD,
            "fsmn-vad",
            "fsmn-vad-direct",
            "audio-30k",
            TimeBase(1_001, 30_000),
            TickRange(302, 318),
        ),
    )
    assert decoded.projection.word_gap_segments == decoded.word_gap_segments
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


def test_rejects_structured_identity_blob_provenance_and_word_gap_projection_drift() -> None:
    blob, invocation, context, projection = _material()
    response = _response()
    identities = response["producer_identities"]
    assert isinstance(identities, list)
    identities[0] = {**ASR_IDENTITY.to_mapping(), "producer_version": "substituted"}
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        decode_shadow_calibration_raw_response(
            _blob(json.dumps(response, separators=(",", ":")).encode()),
            invocation,
            context,
            projection,
        )
    response = _response()
    identities = response["producer_identities"]
    assert isinstance(identities, list)
    identities[0] = {**ASR_IDENTITY.to_mapping(), "timing_error_bound_tick": 1}
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        decode_shadow_calibration_raw_response(
            _blob(json.dumps(response, separators=(",", ":")).encode()),
            invocation,
            context,
            projection,
        )
    substituted_source = replace(SOURCE, blob_id="218b198e-6b87-4770-b0fa-91770b93b169")
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        decode_shadow_calibration_raw_response(
            blob, invocation, replace(context, source=substituted_source), projection
        )
    injected_segments = (
        replace(projection.word_gap_segments[0], text="injected"),
        projection.word_gap_segments[1],
    )
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        decode_shadow_calibration_raw_response(
            blob,
            invocation,
            context,
            replace(projection, word_gap_segments=injected_segments),
        )


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
                "audio-30k",
                TimeBase(1_001, 30_000),
                TickRange(302, 307),
            ),
            CalibrationAnchor(
                "asr-anchor-1",
                CalibrationProducer.ASR,
                "sensevoice-asr",
                "audio-30k",
                TimeBase(1_001, 30_000),
                TickRange(311, 317),
            ),
        )
    )
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        decode_shadow_calibration_raw_response(blob, invocation, zero_context, projection)


def test_independent_derivation_needs_no_claim_or_provider() -> None:
    blob, invocation, context, claimed = _material()
    derived = derive_shadow_calibration_raw_response(blob, invocation, context)

    assert derived.projection == claimed
    assert derived == decode_shadow_calibration_raw_response(blob, invocation, context, claimed)
    assert shadow_calibration_projection_mapping(derived.projection) == shadow_calibration_projection_mapping(claimed)
    forged = replace(claimed, reported_native_identity_sha256=_sha(99))
    with pytest.raises(ShadowCalibrationRawEvidenceError, match="claimed projection"):
        decode_shadow_calibration_raw_response(blob, invocation, context, forged)


def test_persisted_context_and_invocation_round_trip_exact_typed_values() -> None:
    _, invocation, context, _ = _material()
    context_mapping = shadow_calibration_context_mapping(context)
    invocation_mapping = shadow_calibration_invocation_mapping(invocation)

    decoded_context = decode_shadow_calibration_raw_context(context_mapping)
    assert decoded_context == context
    assert decode_shadow_calibration_raw_context(canonical_json_bytes(context_mapping)) == context
    assert decode_shadow_calibration_invocation(invocation_mapping, context=decoded_context) == invocation
    assert decode_shadow_calibration_invocation(
        canonical_json_bytes(invocation_mapping), context=decoded_context
    ) == invocation
    assert shadow_calibration_context_mapping(decoded_context) == context_mapping


def test_public_encoders_match_existing_measurement_persistence_shapes() -> None:
    from autocut_kernel.pipeline.measure_shadow_calibration_command import (
        _invocation_mapping,
        _projection_mapping,
        _raw_context_mapping,
    )

    _, invocation, context, projection = _material()
    assert shadow_calibration_invocation_mapping(invocation) == _invocation_mapping(invocation)
    assert shadow_calibration_context_mapping(context) == _raw_context_mapping(context)
    assert shadow_calibration_projection_mapping(projection) == _projection_mapping(projection)


def _mutate_mapping(
    mapping: dict[str, object], path: tuple[str | int, ...], value: object
) -> None:
    current: object = mapping
    for key in path[:-1]:
        if isinstance(key, int):
            assert isinstance(current, list)
            current = current[key]
        else:
            assert isinstance(current, dict)
            current = current[key]
    last = path[-1]
    if isinstance(last, int):
        assert isinstance(current, list)
        current[last] = value
    else:
        assert isinstance(current, dict)
        current[last] = value


@pytest.mark.parametrize(
    "path,value",
    [
        (("unknown",), "injected"),
        (("source", "unknown"), "injected"),
        (("source", "blob_id"), "not-a-uuid"),
        (("source", "blob_id"), "0F02E85B-D8C6-4D1B-A86B-3DC0F64D2F34"),
        (("source", "blob_sha256"), _sha(99)),
        (("source", "source_sha256"), _sha(0)),
        (("source", "blob_byte_length"), 0),
        (("source", "blob_byte_length"), True),
        (("source", "blob_byte_length"), 1.5),
        (("audio_clock", "origin_tick"), -1),
        (("audio_clock", "origin_tick"), False),
        (("audio_clock", "duration_tick"), 0),
        (("audio_clock", "time_base", "numerator"), True),
        (("audio_clock", "time_base", "denominator"), 0),
        (("policies", "word_gap_ms"), True),
        (("policies", "word_gap_ms"), -1),
        (("policies", "word_gap_ms"), 0.5),
        (("asr_identity", "producer_kind"), "vad"),
        (("asr_identity", "model_sha256"), _sha(0)),
        (("asr_identity", "inference_kind"), "fsmn-vad-direct"),
        (("asr_anchors", 0, "producer_id"), "substituted"),
        (("asr_anchors", 0, "clock_id"), "substituted"),
        (("asr_anchors", 0, "expected_range", "in_tick"), -1),
        (("asr_anchors", 0, "expected_range", "out_tick"), 1_000),
        (("asr_anchors", 0, "anchor_id"), "asr-anchor-1"),
        (("asr_anchors", 0, "expected_range", "in_tick"), True),
        (("vad_anchors",), []),
    ],
)
def test_persisted_context_rejects_malformed_nested_fields(
    path: tuple[str | int, ...], value: object
) -> None:
    mapping = shadow_calibration_context_mapping(_context())
    _mutate_mapping(mapping, path, value)
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        decode_shadow_calibration_raw_context(mapping)


def test_persisted_decoders_reject_every_missing_top_level_field() -> None:
    context_mapping = shadow_calibration_context_mapping(_context())
    for missing in context_mapping:
        incomplete = {key: value for key, value in context_mapping.items() if key != missing}
        with pytest.raises(ShadowCalibrationRawEvidenceError, match="closed"):
            decode_shadow_calibration_raw_context(incomplete)
    invocation_mapping = shadow_calibration_invocation_mapping(_invocation())
    for missing in invocation_mapping:
        incomplete = {key: value for key, value in invocation_mapping.items() if key != missing}
        with pytest.raises(ShadowCalibrationRawEvidenceError, match="closed"):
            decode_shadow_calibration_invocation(incomplete, context=_context())


@pytest.mark.parametrize(
    "path,value",
    [
        (("unknown",), "injected"),
        (("corpus_member_reference_sha256",), _sha(99)),
        (("request_identity_sha256",), _sha(99)),
        (("request_mapping_sha256",), _sha(0)),
        (("request_mapping", "schema_version"), "unknown"),
        (("request_mapping", "source", "source_sha256"), _sha(99)),
        (("request_mapping", "source", "blob_id"), SOURCE.blob_id),
        (("request_mapping", "native_profile_identity_sha256"), _sha(99)),
        (("request_mapping", "word_gap_policy_sha256"), _sha(99)),
        (("request_mapping", "source_byte_limits", "kernel_max_source_bytes"), 16_384),
        (("request_mapping", "container", "safe_suffix"), ".mkv"),
        (("request_mapping", "audio_clock", "clock_id"), "substituted"),
        (("request_mapping", "requested_range", "in_tick"), 301),
        (("request_mapping", "expected_producers", 0, "model_sha256"), _sha(99)),
        (("request_mapping", "response_limits", "max_response_bytes"), True),
        (("request_mapping", "response_limits", "max_response_bytes"), 0),
        (("request_mapping", "response_limits", "max_response_bytes"), 1.5),
        (("request_mapping", "timing_policy", "vad_merge_gap_milliseconds"), 51),
        (("request_mapping", "timing_policy", "utterance_gap_milliseconds"), False),
        (("request_mapping", "transcript_capability", "word_timing"), "optional"),
    ],
)
def test_persisted_invocation_rejects_nested_drift_and_invalid_identity(
    path: tuple[str | int, ...], value: object
) -> None:
    mapping = shadow_calibration_invocation_mapping(_invocation())
    _mutate_mapping(mapping, path, value)
    with pytest.raises(ShadowCalibrationRawEvidenceError):
        decode_shadow_calibration_invocation(mapping, context=_context())


@pytest.mark.parametrize("kind", ["context", "invocation"])
@pytest.mark.parametrize("bad_bytes", [b'{"x":1,"x":2}', b'{"x":1.5}', b'{"x":NaN}', b"\xff"])
def test_persisted_byte_entry_rejects_duplicate_float_nonfinite_and_invalid_utf8(
    kind: str, bad_bytes: bytes
) -> None:
    with pytest.raises(ShadowCalibrationRawEvidenceError, match="strict UTF-8"):
        if kind == "context":
            decode_shadow_calibration_raw_context(bad_bytes)
        else:
            decode_shadow_calibration_invocation(bad_bytes, context=_context())


@pytest.mark.parametrize("kind", ["context", "invocation"])
def test_persisted_byte_entry_rejects_noncanonical_encoding(kind: str) -> None:
    context = _context()
    mapping = (
        shadow_calibration_context_mapping(context)
        if kind == "context"
        else shadow_calibration_invocation_mapping(_invocation())
    )
    raw = canonical_json_bytes(mapping)
    for noncanonical in (raw + b"\n", b" " + raw, json.dumps(mapping).encode()):
        with pytest.raises(ShadowCalibrationRawEvidenceError, match="exact canonical encoding"):
            if kind == "context":
                decode_shadow_calibration_raw_context(noncanonical)
            else:
                decode_shadow_calibration_invocation(noncanonical, context=context)


def test_anchor_reference_hash_binds_exact_ordered_anchor_document() -> None:
    context = _context()
    mapping = shadow_calibration_context_mapping(context)
    anchor_document = {
        "schema_version": SHADOW_CALIBRATION_ANCHOR_SET_SCHEMA,
        "asr_anchors": mapping["asr_anchors"],
        "vad_anchors": mapping["vad_anchors"],
    }
    expected_hash = canonical_json_hash(anchor_document)
    assert shadow_calibration_anchor_reference_sha256(context) == expected_hash
    decoded = decode_shadow_calibration_raw_context(canonical_json_bytes(mapping))
    assert shadow_calibration_anchor_reference_sha256(decoded) == expected_hash

    changed_anchor = replace(context.asr_anchors[0], expected_range=TickRange(303, 308))
    changed_context = replace(context, asr_anchors=(changed_anchor, context.asr_anchors[1]))
    assert shadow_calibration_anchor_reference_sha256(changed_context) != expected_hash
    renamed_anchor = replace(context.asr_anchors[0], anchor_id="different-anchor")
    assert shadow_calibration_anchor_reference_sha256(
        replace(context, asr_anchors=(renamed_anchor, context.asr_anchors[1]))
    ) != expected_hash

    asr_anchors = mapping["asr_anchors"]
    assert isinstance(asr_anchors, list)
    reversed_anchors = list(reversed(asr_anchors))
    assert canonical_json_hash({**anchor_document, "asr_anchors": reversed_anchors}) != expected_hash
    with pytest.raises(ShadowCalibrationRawEvidenceError, match="ordered"):
        decode_shadow_calibration_raw_context({**mapping, "asr_anchors": reversed_anchors})
