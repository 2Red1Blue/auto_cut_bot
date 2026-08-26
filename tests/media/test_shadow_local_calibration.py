"""Pure synthetic local-calibration content; no source execution or acceptance."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest
from autocut_kernel.media.calibration import CalibrationAnchor, CalibrationProducer
from autocut_kernel.media.local_audio_window import LocalAudioWindowSpec
from autocut_kernel.media.local_speech_window import LocalSpeechWindowPolicy
from autocut_kernel.media.shadow_calibration_raw import (
    ShadowCalibrationPolicies,
    ShadowCalibrationProducerIdentity,
    ShadowCalibrationSource,
)
from autocut_kernel.media.shadow_local_calibration import (
    SHADOW_LOCAL_ALIGNMENT_POLICY_SHA256,
    ShadowLocalCalibrationCase,
    ShadowLocalCalibrationError,
    build_shadow_local_request,
    decode_shadow_local_calibration_case,
)
from autocut_kernel.media.types import TickRange, TimeBase


def digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def local_case() -> ShadowLocalCalibrationCase:
    """Hand-authored independent local gold, not a model-generated oracle."""
    source = ShadowCalibrationSource(
        "源一", digest("source"), digest("corpus-member"),
        "12345678-1234-5678-1234-567812345678", digest("source"), 1024, "video/mp4",
    )
    spec = LocalAudioWindowSpec(
        source.source_id, source.source_sha256, 2, "original-audio-clock", TimeBase(1, 48_000),
        TickRange(-48_000, 96_000), TickRange(-480, 960), 96_000, 2,
        digest("audio-boundaries"), digest("decoder"), 100_000, 100, 100_000, 100_000,
    )
    policy = LocalSpeechWindowPolicy(
        digest("complete-shadow-service-profile"), "asr", digest("asr-generation"),
        "vad", digest("vad-generation"), 4, 2,
    )
    identities = tuple(
        ShadowCalibrationProducerIdentity(
            producer, producer.value, "native-v1", digest(producer.value + "-generation"),
            digest(producer.value + "-detector"), digest("calibration-policy"),
            producer.value + "-model", "model-revision", digest(producer.value + "-model"),
            "sensevoice-word-timestamp" if producer is CalibrationProducer.ASR else "fsmn-vad-direct",
            digest("service-source"),
        )
        for producer in (CalibrationProducer.ASR, CalibrationProducer.VAD)
    )
    asr = tuple(
        CalibrationAnchor(f"word-{i}", CalibrationProducer.ASR, "asr", spec.clock_id, spec.time_base, interval)
        for i, interval in enumerate((TickRange(-432, -336), TickRange(-96, 0)))
    )
    vad = tuple(
        CalibrationAnchor(f"speech-{i}", CalibrationProducer.VAD, "vad", spec.clock_id, spec.time_base, interval)
        for i, interval in enumerate((TickRange(-480, -192), TickRange(96, 240)))
    )
    return ShadowLocalCalibrationCase(
        source, digest("source-provenance"), spec, policy, digest("nested-native-port-identity"),
        ShadowCalibrationPolicies(digest("timed-policy"), digest("word-policy"), digest("vad-policy"), 4, 2),
        (identities[0], identities[1]), SHADOW_LOCAL_ALIGNMENT_POLICY_SHA256, asr, vad,
    )


def test_closed_case_roundtrip_preserves_unicode_negative_origin_and_native_layout() -> None:
    case = local_case()
    raw = case.to_bytes()
    assert decode_shadow_local_calibration_case(raw, max_bytes=len(raw)) == case
    assert ShadowLocalCalibrationCase.from_mapping(case.to_mapping()) == case
    assert case.canonical_hash == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert case.source.source_id == "源一"
    assert case.extraction.requested_range == TickRange(-480, 960)
    assert case.extraction.sample_rate == 96_000  # Not inverse of 1/48000 clock.
    assert case.extraction.channels == 2
    assert case.policy.service_profile_sha256 != case.native_profile_identity_sha256
    request = build_shadow_local_request(case, max_response_bytes=20_000)
    assert request.binding_sha256 == case.canonical_hash
    assert request.extraction == case.extraction
    assert request.policy == case.policy
    assert "accepted" not in raw.decode() and "receipt" not in raw.decode()


def test_case_and_mapping_are_not_mutable_aliases() -> None:
    case = local_case()
    mapping = case.to_mapping()
    cast(dict[str, object], mapping["source"])["source_id"] = "foreign"
    cast(list[object], mapping["asr_anchors"]).clear()
    assert case == local_case()
    with pytest.raises(FrozenInstanceError):
        setattr(case, "source_provenance_sha256", digest("foreign"))


@pytest.mark.parametrize("field", tuple(local_case().to_mapping()))
@pytest.mark.parametrize("operation", ("missing", "null"))
def test_top_level_all_fields_are_required(field: str, operation: str) -> None:
    mapping = local_case().to_mapping()
    if operation == "missing":
        del mapping[field]
    else:
        mapping[field] = None
    with pytest.raises(ShadowLocalCalibrationError):
        ShadowLocalCalibrationCase.from_mapping(mapping)


@pytest.mark.parametrize("path", (
    (), ("source",), ("extraction",), ("policy",), ("policies",),
    ("producer_identities", 0), ("producer_identities", 1),
    ("asr_anchors", 0), ("vad_anchors", 0), ("asr_anchors", 0, "expected_range"),
    ("asr_anchors", 0, "time_base"), ("extraction", "time_base"),
    ("extraction", "source_range"), ("extraction", "requested_range"),
))
def test_nested_objects_are_closed(path: tuple[str | int, ...]) -> None:
    mapping = local_case().to_mapping()
    current: object = mapping
    for key in path:
        current = cast(list[object], current)[key] if type(key) is int else cast(dict[str, object], current)[key]
    cast(dict[str, object], current)["accepted_bound_tick"] = 1
    with pytest.raises(ShadowLocalCalibrationError):
        ShadowLocalCalibrationCase.from_mapping(mapping)


@pytest.mark.parametrize("path", (
    ("source", "blob_byte_length"), ("extraction", "sample_rate"),
    ("extraction", "channels"), ("extraction", "max_pcm_bytes"),
    ("policy", "utterance_gap_milliseconds"), ("policies", "word_gap_ms"),
    ("asr_anchors", 0, "expected_range", "start_pts"),
))
@pytest.mark.parametrize("value", (True, 1.0, "1", None))
def test_wire_numbers_do_not_coerce(path: tuple[str | int, ...], value: object) -> None:
    mapping = local_case().to_mapping()
    current: object = mapping
    for key in path[:-1]:
        current = cast(list[object], current)[key] if type(key) is int else cast(dict[str, object], current)[key]
    cast(dict[str, object], current)[cast(str, path[-1])] = value
    with pytest.raises(ShadowLocalCalibrationError):
        ShadowLocalCalibrationCase.from_mapping(mapping)


@pytest.mark.parametrize("change", (
    "source_id", "source_hash", "size", "service_hash", "word_gap", "vad_gap",
    "producer_order", "producer_count", "producer_mutable", "producer_id", "generation", "service",
    "anchors_mutable", "duplicate", "reordered", "overlap", "outside", "anchor_producer",
    "anchor_clock", "anchor_timebase", "alignment", "provenance", "native", "surrogate",
))
def test_direct_case_rejects_identity_and_anchor_drift(change: str) -> None:
    case = local_case()
    asr, vad = case.producer_identities
    with pytest.raises(ValueError):
        if change == "source_id":
            replace(case, source=replace(case.source, source_id="foreign"))
        elif change == "source_hash":
            replace(case, extraction=replace(case.extraction, source_sha256=digest("foreign")))
        elif change == "size":
            replace(case, source=replace(case.source, blob_byte_length=100_001))
        elif change == "service_hash":
            replace(case, native_profile_identity_sha256=case.policy.service_profile_sha256)
        elif change == "word_gap":
            replace(case, policies=replace(case.policies, word_gap_ms=5))
        elif change == "vad_gap":
            replace(case, policies=replace(case.policies, vad_merge_gap_ms=3))
        elif change == "producer_order":
            replace(case, producer_identities=(vad, asr))
        elif change == "producer_count":
            replace(case, producer_identities=cast(tuple[ShadowCalibrationProducerIdentity, ShadowCalibrationProducerIdentity], (asr,)))
        elif change == "producer_mutable":
            replace(case, producer_identities=cast(tuple[ShadowCalibrationProducerIdentity, ShadowCalibrationProducerIdentity], [asr, vad]))
        elif change == "producer_id":
            replace(case, producer_identities=(replace(asr, producer_id="foreign"), vad))
        elif change == "generation":
            replace(case, producer_identities=(replace(asr, generation_policy_sha256=digest("foreign")), vad))
        elif change == "service":
            replace(case, producer_identities=(asr, replace(vad, service_sha256=digest("foreign"))))
        elif change == "anchors_mutable":
            replace(case, asr_anchors=cast(tuple[CalibrationAnchor, ...], list(case.asr_anchors)))
        elif change == "duplicate":
            replace(case, asr_anchors=(case.asr_anchors[0], replace(case.asr_anchors[1], anchor_id=case.asr_anchors[0].anchor_id)))
        elif change == "reordered":
            replace(case, asr_anchors=tuple(reversed(case.asr_anchors)))
        elif change == "overlap":
            replace(case, asr_anchors=(case.asr_anchors[0], replace(case.asr_anchors[1], expected_range=TickRange(-400, 0))))
        elif change == "outside":
            replace(case, asr_anchors=(replace(case.asr_anchors[0], expected_range=TickRange(-500, -336)),))
        elif change == "anchor_producer":
            replace(case, asr_anchors=(replace(case.asr_anchors[0], producer=CalibrationProducer.VAD),))
        elif change == "anchor_clock":
            replace(case, asr_anchors=(replace(case.asr_anchors[0], clock_id="foreign"),))
        elif change == "anchor_timebase":
            replace(case, asr_anchors=(replace(case.asr_anchors[0], time_base=TimeBase(1, 96_000)),))
        elif change == "alignment":
            replace(case, alignment_policy_sha256=digest("foreign"))
        elif change == "provenance":
            replace(case, source_provenance_sha256="sha256:invalid")
        elif change == "native":
            replace(case, native_profile_identity_sha256="sha256:invalid")
        else:
            replace(case, producer_identities=(replace(asr, model_id="\ud800"), vad))


@pytest.mark.parametrize("field", ("source_provenance_sha256", "native_profile_identity_sha256"))
def test_provenance_is_independent_of_source_bytes_but_changes_case_and_request(field: str) -> None:
    case = local_case()
    updated = replace(case, **{field: digest("foreign")})
    assert updated.source == case.source
    assert updated.canonical_hash != case.canonical_hash
    assert build_shadow_local_request(updated, max_response_bytes=100_000) != build_shadow_local_request(case, max_response_bytes=100_000)


@pytest.mark.parametrize("limit", (0, -1, True, 1.5, "100000", None))
def test_limits_are_explicit_positive_integers(limit: object) -> None:
    case = local_case()
    with pytest.raises(ValueError):
        decode_shadow_local_calibration_case(case.to_bytes(), max_bytes=cast(int, limit))
    with pytest.raises(ValueError):
        build_shadow_local_request(case, max_response_bytes=cast(int, limit))


def test_case_parser_checks_budget_before_json_and_rejects_duplicates() -> None:
    case = local_case()
    raw = case.to_bytes()
    with pytest.raises(ShadowLocalCalibrationError):
        decode_shadow_local_calibration_case(raw, max_bytes=len(raw) - 1)
    duplicate = b'{"schema_version":"shadow-local-calibration-case-v1",' + raw[1:]
    assert json.loads(duplicate) == case.to_mapping()
    with pytest.raises(ShadowLocalCalibrationError):
        decode_shadow_local_calibration_case(duplicate, max_bytes=len(duplicate))


@pytest.mark.parametrize("raw", (b"", b"{}", b"[]", b"null", b"{", b"\xff", b"{\"x\":NaN}", b"{\"x\":1e999}"))
def test_invalid_case_json(raw: bytes) -> None:
    with pytest.raises(ShadowLocalCalibrationError):
        decode_shadow_local_calibration_case(raw, max_bytes=100_000)


def test_empty_independent_anchor_sets_remain_explicit() -> None:
    case = replace(local_case(), asr_anchors=(), vad_anchors=())
    assert decode_shadow_local_calibration_case(case.to_bytes(), max_bytes=100_000) == case
