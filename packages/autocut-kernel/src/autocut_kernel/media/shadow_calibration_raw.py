"""Fail-closed decoder for first-run shadow calibration native evidence.

This module deliberately accepts only the calibration-only FunASR envelope.  It
does not know about HTTP, Store objects, or the normal timed-speech response.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import cast

from .calibration import (
    CalibrationAnchor,
    CalibrationAnchorMatch,
    CalibrationMeasurementSummary,
    CalibrationObservation,
    CalibrationProducer,
    CalibrationRecordError,
    ProducerCalibrationMeasurement,
)
from .types import TickRange, TimeBase, canonical_sha256, sha256_prefixed

SHADOW_CALIBRATION_RAW_RESPONSE_SCHEMA = "shadow-calibration-funasr-raw-response-v1"
SHADOW_CALIBRATION_RAW_REQUEST_SCHEMA = "shadow-calibration-funasr-raw-request-v1"
SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE = "application/vnd.autocut.funasr-native-response+json"
SHADOW_CALIBRATION_RAW_EVIDENCE_INVALID = "SHADOW_CALIBRATION_RAW_EVIDENCE_INVALID"


class ShadowCalibrationRawEvidenceError(ValueError):
    """Terminal denial: raw calibration material cannot establish authority."""

    code = SHADOW_CALIBRATION_RAW_EVIDENCE_INVALID


def _invalid(detail: str) -> ShadowCalibrationRawEvidenceError:
    return ShadowCalibrationRawEvidenceError(f"{SHADOW_CALIBRATION_RAW_EVIDENCE_INVALID}: {detail}")


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise _invalid(f"{field_name} must be non-empty text")
    return value


def _integer(value: object, field_name: str) -> int:
    if type(value) is not int:  # noqa: E721
        raise _invalid(f"{field_name} must be an integer")
    return value


def _sha(value: object, field_name: str) -> str:
    try:
        digest = sha256_prefixed(value, field_name)
    except ValueError as error:
        raise _invalid(str(error)) from error
    if digest == "sha256:" + "0" * 64:
        raise _invalid(f"{field_name} must not be zero")
    return digest


def _exact_object(value: object, fields: frozenset[str], field_name: str) -> dict[str, object]:
    if type(value) is not dict or frozenset(cast(dict[str, object], value)) != fields:  # noqa: E721
        raise _invalid(f"{field_name} schema is not closed")
    return cast(dict[str, object], value)


def _time_base(value: object, field_name: str) -> TimeBase:
    mapping = _exact_object(value, frozenset({"numerator", "denominator"}), field_name)
    try:
        return TimeBase(
            _integer(mapping["numerator"], f"{field_name}.numerator"),
            _integer(mapping["denominator"], f"{field_name}.denominator"),
        )
    except ValueError as error:
        raise _invalid(str(error)) from error


def _tick_range(value: object, field_name: str) -> TickRange:
    mapping = _exact_object(value, frozenset({"in_tick", "out_tick"}), field_name)
    try:
        return TickRange(
            _integer(mapping["in_tick"], f"{field_name}.in_tick"),
            _integer(mapping["out_tick"], f"{field_name}.out_tick"),
        )
    except ValueError as error:
        raise _invalid(str(error)) from error


def _strict_json_object(raw: bytes) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _invalid("JSON contains a duplicate key")
            result[key] = value
        return result

    try:
        value: object = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"JSON constant {constant!r} is forbidden")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        if isinstance(error, ShadowCalibrationRawEvidenceError):
            raise
        raise _invalid("blob must be strict UTF-8 JSON") from error
    if type(value) is not dict:  # noqa: E721
        raise _invalid("blob root must be an object")
    return cast(dict[str, object], value)


@dataclass(frozen=True, slots=True)
class ShadowCalibrationRawBlob:
    """Immutable bytes as already hash-bound by the caller's blob reference."""

    raw: bytes
    media_type: str
    byte_length: int
    content_sha256: str

    def __post_init__(self) -> None:
        if type(self.raw) is not bytes:  # noqa: E721
            raise _invalid("blob.raw must be bytes")
        if self.media_type != SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE:
            raise _invalid("blob media type is not the calibration raw-response media type")
        if _integer(self.byte_length, "blob.byte_length") != len(self.raw):
            raise _invalid("blob byte length does not match bytes")
        if (
            _sha(self.content_sha256, "blob.content_sha256")
            != "sha256:" + hashlib.sha256(self.raw).hexdigest()
        ):
            raise _invalid("blob content hash does not match bytes")


@dataclass(frozen=True, slots=True)
class ShadowCalibrationSource:
    source_id: str
    source_sha256: str
    corpus_member_reference_sha256: str
    blob_id: str
    blob_sha256: str
    blob_byte_length: int
    blob_media_type: str

    def __post_init__(self) -> None:
        _text(self.source_id, "source.source_id")
        _sha(self.source_sha256, "source.source_sha256")
        _sha(self.corpus_member_reference_sha256, "source.corpus_member_reference_sha256")
        try:
            parsed = uuid.UUID(self.blob_id)
        except (AttributeError, ValueError) as error:
            raise _invalid("source.blob_id must be a canonical UUID") from error
        if str(parsed) != self.blob_id:
            raise _invalid("source.blob_id must be a canonical UUID")
        if _sha(self.blob_sha256, "source.blob_sha256") != self.source_sha256:
            raise _invalid("source blob hash must equal source bytes hash")
        if _integer(self.blob_byte_length, "source.blob_byte_length") <= 0:
            raise _invalid("source.blob_byte_length must be positive")
        _text(self.blob_media_type, "source.blob_media_type")

    def to_mapping(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "corpus_member_reference_sha256": self.corpus_member_reference_sha256,
            "blob_id": self.blob_id,
            "blob_sha256": self.blob_sha256,
            "blob_byte_length": self.blob_byte_length,
            "blob_media_type": self.blob_media_type,
        }

    def to_response_mapping(self) -> dict[str, object]:
        return {"source_id": self.source_id, "source_sha256": self.source_sha256}


@dataclass(frozen=True, slots=True)
class ShadowCalibrationSourceByteLimits:
    kernel_max_source_bytes: int
    service_max_request_bytes: int
    effective_max_source_bytes: int

    def __post_init__(self) -> None:
        values = (
            self.kernel_max_source_bytes,
            self.service_max_request_bytes,
            self.effective_max_source_bytes,
        )
        if any(_integer(value, "source byte limit") <= 0 for value in values):
            raise _invalid("source byte limits must be positive")
        if self.effective_max_source_bytes != min(
            self.kernel_max_source_bytes, self.service_max_request_bytes
        ):
            raise _invalid("effective source byte limit is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "kernel_max_source_bytes": self.kernel_max_source_bytes,
            "service_max_request_bytes": self.service_max_request_bytes,
            "effective_max_source_bytes": self.effective_max_source_bytes,
        }


@dataclass(frozen=True, slots=True)
class ShadowCalibrationContainer:
    media_type: str
    safe_suffix: str

    def __post_init__(self) -> None:
        _text(self.media_type, "container.media_type")
        if (
            type(self.safe_suffix) is not str
            or not self.safe_suffix.startswith(".")
            or "/" in self.safe_suffix
        ):  # noqa: E721
            raise _invalid("container.safe_suffix is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {"media_type": self.media_type, "safe_suffix": self.safe_suffix}


@dataclass(frozen=True, slots=True)
class ShadowCalibrationTranscriptCapability:
    profile: str
    segment: str
    segment_semantics: str
    sentence: str
    word: str
    word_timing: str

    def __post_init__(self) -> None:
        if (
            self.profile != "sensevoice_word_guard_v1"
            or self.segment != "complete"
            or self.segment_semantics != "utterance_gap_protected_range"
            or self.sentence != "not_applicable"
            or self.word != "complete"
            or self.word_timing != "required"
        ):
            raise _invalid(
                "transcript capability is not the locked SenseVoice word-timing capability"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "segment": self.segment,
            "segment_semantics": self.segment_semantics,
            "sentence": self.sentence,
            "word": self.word,
            "word_timing": self.word_timing,
        }


@dataclass(frozen=True, slots=True)
class ShadowCalibrationAudioClock:
    clock_id: str
    time_base: TimeBase
    origin_tick: int
    duration_tick: int

    def __post_init__(self) -> None:
        _text(self.clock_id, "audio_clock.clock_id")
        if type(self.time_base) is not TimeBase:  # noqa: E721
            raise _invalid("audio_clock.time_base must be an exact TimeBase")
        if _integer(self.origin_tick, "audio_clock.origin_tick") < 0:
            raise _invalid("audio_clock.origin_tick must be non-negative")
        if _integer(self.duration_tick, "audio_clock.duration_tick") <= 0:
            raise _invalid("audio_clock.duration_tick must be positive")

    @property
    def full_range(self) -> TickRange:
        return TickRange(self.origin_tick, self.origin_tick + self.duration_tick)

    def to_mapping(self) -> dict[str, object]:
        return {
            "clock_id": self.clock_id,
            "time_base": {
                "numerator": self.time_base.numerator,
                "denominator": self.time_base.denominator,
            },
            "origin_tick": self.origin_tick,
            "duration_tick": self.duration_tick,
        }


@dataclass(frozen=True, slots=True)
class ShadowCalibrationPolicies:
    timed_speech_policy_sha256: str
    word_gap_policy_sha256: str
    vad_merge_policy_sha256: str
    word_gap_ms: int
    vad_merge_gap_ms: int

    def __post_init__(self) -> None:
        _sha(self.timed_speech_policy_sha256, "policies.timed_speech_policy_sha256")
        _sha(self.word_gap_policy_sha256, "policies.word_gap_policy_sha256")
        _sha(self.vad_merge_policy_sha256, "policies.vad_merge_policy_sha256")
        if _integer(self.word_gap_ms, "policies.word_gap_ms") < 0:
            raise _invalid("policies.word_gap_ms must be non-negative")
        if _integer(self.vad_merge_gap_ms, "policies.vad_merge_gap_ms") < 0:
            raise _invalid("policies.vad_merge_gap_ms must be non-negative")


@dataclass(frozen=True, slots=True)
class ShadowCalibrationProducerIdentity:
    """Closed measured identity of one direct native producer."""

    producer: CalibrationProducer
    producer_id: str
    producer_version: str
    generation_policy_sha256: str
    detector_sha256: str
    calibration_policy_sha256: str
    model_id: str
    model_revision: str
    model_sha256: str
    inference_kind: str
    service_sha256: str

    def __post_init__(self) -> None:
        if type(self.producer) is not CalibrationProducer:  # noqa: E721
            raise _invalid("producer identity producer is invalid")
        _text(self.producer_id, "producer_identity.producer_id")
        _text(self.producer_version, "producer_identity.producer_version")
        for field_name in (
            "generation_policy_sha256",
            "detector_sha256",
            "calibration_policy_sha256",
            "model_sha256",
            "service_sha256",
        ):
            _sha(getattr(self, field_name), f"producer_identity.{field_name}")
        _text(self.model_id, "producer_identity.model_id")
        _text(self.model_revision, "producer_identity.model_revision")
        expected_kind = (
            "sensevoice-word-timestamp"
            if self.producer is CalibrationProducer.ASR
            else "fsmn-vad-direct"
        )
        if self.inference_kind != expected_kind:
            raise _invalid("producer identity inference kind is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "producer_kind": self.producer.value,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "generation_policy_sha256": self.generation_policy_sha256,
            "detector_sha256": self.detector_sha256,
            "calibration_policy_sha256": self.calibration_policy_sha256,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "model_sha256": self.model_sha256,
            "inference_kind": self.inference_kind,
            "service_sha256": self.service_sha256,
        }


@dataclass(frozen=True, slots=True)
class ShadowCalibrationRequestMapping:
    """Canonical, non-secret invocation material that derives the request hash."""

    source: ShadowCalibrationSource
    source_byte_limits: ShadowCalibrationSourceByteLimits
    container: ShadowCalibrationContainer
    audio_clock: ShadowCalibrationAudioClock
    requested_range: TickRange
    native_profile_identity_sha256: str
    max_response_bytes: int
    transcript_capability: ShadowCalibrationTranscriptCapability
    timed_speech_policy_sha256: str
    word_gap_policy_sha256: str
    vad_merge_policy_sha256: str
    word_gap_ms: int
    vad_merge_gap_ms: int
    producer_identities: tuple[ShadowCalibrationProducerIdentity, ShadowCalibrationProducerIdentity]

    def __post_init__(self) -> None:
        if (
            type(self.source) is not ShadowCalibrationSource
            or type(self.source_byte_limits) is not ShadowCalibrationSourceByteLimits
            or type(self.container) is not ShadowCalibrationContainer
            or type(self.audio_clock) is not ShadowCalibrationAudioClock
            or type(self.transcript_capability) is not ShadowCalibrationTranscriptCapability
        ):  # noqa: E721
            raise _invalid("request mapping source and audio clock must be exact")
        if (
            type(self.requested_range) is not TickRange
            or self.requested_range != self.audio_clock.full_range
        ):  # noqa: E721
            raise _invalid("request mapping must request the complete audio range")
        for field_name in (
            "native_profile_identity_sha256",
            "timed_speech_policy_sha256",
            "word_gap_policy_sha256",
            "vad_merge_policy_sha256",
        ):
            _sha(getattr(self, field_name), f"request_mapping.{field_name}")
        if _integer(self.max_response_bytes, "request_mapping.max_response_bytes") <= 0:
            raise _invalid("request mapping response limit must be positive")
        if (
            _integer(self.word_gap_ms, "request_mapping.word_gap_ms") < 0
            or _integer(self.vad_merge_gap_ms, "request_mapping.vad_merge_gap_ms") < 0
        ):
            raise _invalid("request mapping timing policy is invalid")
        if (
            type(self.producer_identities) is not tuple  # noqa: E721
            or len(self.producer_identities) != 2
            or any(
                type(identity) is not ShadowCalibrationProducerIdentity
                for identity in self.producer_identities
            )  # noqa: E721
            or tuple(identity.producer for identity in self.producer_identities)
            != (CalibrationProducer.ASR, CalibrationProducer.VAD)
        ):
            raise _invalid("request mapping producers must be ordered ASR then VAD")
        if self.producer_identities[0].producer_id == self.producer_identities[1].producer_id:
            raise _invalid("request mapping producer IDs must differ")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": SHADOW_CALIBRATION_RAW_REQUEST_SCHEMA,
            "source": self.source.to_response_mapping(),
            "source_byte_limits": self.source_byte_limits.to_mapping(),
            "container": self.container.to_mapping(),
            "audio_clock": self.audio_clock.to_mapping(),
            "requested_range": {
                "in_tick": self.requested_range.start_pts,
                "out_tick": self.requested_range.end_pts,
            },
            "expected_producers": [identity.to_mapping() for identity in self.producer_identities],
            "timed_speech_policy_sha256": self.timed_speech_policy_sha256,
            "word_gap_policy_sha256": self.word_gap_policy_sha256,
            "vad_merge_policy_sha256": self.vad_merge_policy_sha256,
            "native_profile_identity_sha256": self.native_profile_identity_sha256,
            "response_limits": {"max_response_bytes": self.max_response_bytes},
            "timing_policy": {
                "utterance_gap_milliseconds": self.word_gap_ms,
                "vad_merge_gap_milliseconds": self.vad_merge_gap_ms,
            },
            "transcript_capability": self.transcript_capability.to_mapping(),
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ShadowCalibrationInvocation:
    corpus_member_reference_sha256: str
    request_identity_sha256: str
    request_mapping: ShadowCalibrationRequestMapping
    request_mapping_sha256: str

    def __post_init__(self) -> None:
        _sha(self.corpus_member_reference_sha256, "invocation.corpus_member_reference_sha256")
        _sha(self.request_identity_sha256, "invocation.request_identity_sha256")
        if type(self.request_mapping) is not ShadowCalibrationRequestMapping:  # noqa: E721
            raise _invalid("invocation.request_mapping must be exact")
        if (
            _sha(self.request_mapping_sha256, "invocation.request_mapping_sha256")
            != self.request_mapping.sha256
        ):
            raise _invalid("invocation request mapping hash does not match canonical mapping")
        if self.request_identity_sha256 != self.request_mapping_sha256:
            raise _invalid("invocation request identity does not match canonical mapping")


@dataclass(frozen=True, slots=True)
class ShadowCalibrationWordGapSegment:
    segment_id: str
    text: str
    observed_range: TickRange

    def __post_init__(self) -> None:
        _text(self.segment_id, "word-gap segment ID")
        _text(self.text, "word-gap segment text")
        if type(self.observed_range) is not TickRange:  # noqa: E721
            raise _invalid("word-gap segment range must be an exact TickRange")


@dataclass(frozen=True, slots=True)
class ShadowCalibrationAsrObservation:
    observation: CalibrationObservation
    text: str

    def __post_init__(self) -> None:
        if type(self.observation) is not CalibrationObservation:  # noqa: E721
            raise _invalid("ASR observation must contain an exact CalibrationObservation")
        _text(self.text, "ASR observation text")


@dataclass(frozen=True, slots=True)
class ShadowCalibrationProjection:
    reported_native_identity_sha256: str
    native_request_identity_sha256: str
    asr_observations: tuple[ShadowCalibrationAsrObservation, ...]
    word_gap_segments: tuple[ShadowCalibrationWordGapSegment, ...]
    vad_observations: tuple[CalibrationObservation, ...]
    summary: CalibrationMeasurementSummary

    def __post_init__(self) -> None:
        _sha(self.reported_native_identity_sha256, "projection.reported_native_identity_sha256")
        _sha(self.native_request_identity_sha256, "projection.native_request_identity_sha256")
        if type(self.asr_observations) is not tuple or not self.asr_observations:  # noqa: E721
            raise _invalid("projection ASR observations must be non-empty")
        if type(self.vad_observations) is not tuple or not self.vad_observations:  # noqa: E721
            raise _invalid("projection VAD observations must be non-empty")
        if type(self.word_gap_segments) is not tuple or not self.word_gap_segments:  # noqa: E721
            raise _invalid("projection word-gap segments must be non-empty")
        if any(type(item) is not ShadowCalibrationAsrObservation for item in self.asr_observations):  # noqa: E721
            raise _invalid("projection ASR observation is invalid")
        if any(type(item) is not CalibrationObservation for item in self.vad_observations):  # noqa: E721
            raise _invalid("projection VAD observation is invalid")
        if any(
            type(item) is not ShadowCalibrationWordGapSegment for item in self.word_gap_segments
        ):  # noqa: E721
            raise _invalid("projection word-gap segment is invalid")
        if type(self.summary) is not CalibrationMeasurementSummary:  # noqa: E721
            raise _invalid("projection summary is invalid")


@dataclass(frozen=True, slots=True)
class ShadowCalibrationRawContext:
    source: ShadowCalibrationSource
    source_byte_limits: ShadowCalibrationSourceByteLimits
    container: ShadowCalibrationContainer
    audio_clock: ShadowCalibrationAudioClock
    policies: ShadowCalibrationPolicies
    native_profile_identity_sha256: str
    transcript_capability: ShadowCalibrationTranscriptCapability
    asr_identity: ShadowCalibrationProducerIdentity
    vad_identity: ShadowCalibrationProducerIdentity
    asr_anchors: tuple[CalibrationAnchor, ...]
    vad_anchors: tuple[CalibrationAnchor, ...]

    def __post_init__(self) -> None:
        if (
            type(self.source) is not ShadowCalibrationSource
            or type(self.source_byte_limits) is not ShadowCalibrationSourceByteLimits
            or type(self.container) is not ShadowCalibrationContainer
            or type(self.audio_clock) is not ShadowCalibrationAudioClock
        ):  # noqa: E721
            raise _invalid("context source and audio clock must be exact")
        if type(self.policies) is not ShadowCalibrationPolicies:  # noqa: E721
            raise _invalid("context policies must be exact")
        _sha(self.native_profile_identity_sha256, "context.native_profile_identity_sha256")
        if type(self.transcript_capability) is not ShadowCalibrationTranscriptCapability:  # noqa: E721
            raise _invalid("context transcript capability must be exact")
        if (
            type(self.asr_identity) is not ShadowCalibrationProducerIdentity
            or type(self.vad_identity) is not ShadowCalibrationProducerIdentity
        ):  # noqa: E721
            raise _invalid("context producer identities must be exact")
        if (
            self.asr_identity.producer is not CalibrationProducer.ASR
            or self.vad_identity.producer is not CalibrationProducer.VAD
        ):
            raise _invalid("context producer identities are unordered")
        if self.asr_identity.producer_id == self.vad_identity.producer_id:
            raise _invalid("context producer IDs must differ")
        for anchors, producer, producer_id, label in (
            (self.asr_anchors, CalibrationProducer.ASR, self.asr_identity.producer_id, "ASR"),
            (self.vad_anchors, CalibrationProducer.VAD, self.vad_identity.producer_id, "VAD"),
        ):
            if type(anchors) is not tuple or not anchors:  # noqa: E721
                raise _invalid(f"context {label} anchors must be non-empty")
            if any(type(anchor) is not CalibrationAnchor for anchor in anchors):  # noqa: E721
                raise _invalid(f"context {label} anchor is invalid")
            if any(
                anchor.producer is not producer
                or anchor.producer_id != producer_id
                or anchor.clock_id != self.audio_clock.clock_id
                or anchor.time_base != self.audio_clock.time_base
                for anchor in anchors
            ):
                raise _invalid(f"context {label} anchors drift from locked producer clock")
            identifiers = tuple(anchor.anchor_id for anchor in anchors)
            if len(identifiers) != len(set(identifiers)):
                raise _invalid(f"context {label} anchors duplicate IDs")

    @property
    def producer_identities(
        self,
    ) -> tuple[ShadowCalibrationProducerIdentity, ShadowCalibrationProducerIdentity]:
        return (self.asr_identity, self.vad_identity)


@dataclass(frozen=True, slots=True)
class DecodedShadowCalibrationRawResponse:
    projection: ShadowCalibrationProjection
    word_gap_segments: tuple[ShadowCalibrationWordGapSegment, ...]


def _decode_source(value: object) -> dict[str, object]:
    mapping = _exact_object(value, frozenset({"source_id", "source_sha256"}), "response.source")
    return {
        "source_id": _text(mapping["source_id"], "response.source.source_id"),
        "source_sha256": _sha(mapping["source_sha256"], "response.source.source_sha256"),
    }


def _decode_clock(value: object) -> ShadowCalibrationAudioClock:
    mapping = _exact_object(
        value,
        frozenset({"clock_id", "time_base", "origin_tick", "duration_tick"}),
        "response.audio_clock",
    )
    return ShadowCalibrationAudioClock(
        _text(mapping["clock_id"], "response.audio_clock.clock_id"),
        _time_base(mapping["time_base"], "response.audio_clock.time_base"),
        _integer(mapping["origin_tick"], "response.audio_clock.origin_tick"),
        _integer(mapping["duration_tick"], "response.audio_clock.duration_tick"),
    )


def _decode_identity(
    value: object, expected_producer: CalibrationProducer
) -> ShadowCalibrationProducerIdentity:
    mapping = _exact_object(
        value,
        frozenset(
            {
                "producer_kind",
                "producer_id",
                "producer_version",
                "generation_policy_sha256",
                "detector_sha256",
                "calibration_policy_sha256",
                "model_id",
                "model_revision",
                "model_sha256",
                "inference_kind",
                "service_sha256",
            }
        ),
        "response.producer_identity",
    )
    try:
        producer = CalibrationProducer(
            _text(mapping["producer_kind"], "response.producer_identity.producer_kind")
        )
    except ValueError as error:
        raise _invalid("response producer is invalid") from error
    if producer is not expected_producer:
        raise _invalid("response producer identity order is invalid")
    return ShadowCalibrationProducerIdentity(
        producer,
        _text(mapping["producer_id"], "response.producer_identity.producer_id"),
        _text(mapping["producer_version"], "response.producer_identity.producer_version"),
        _sha(
            mapping["generation_policy_sha256"],
            "response.producer_identity.generation_policy_sha256",
        ),
        _sha(mapping["detector_sha256"], "response.producer_identity.detector_sha256"),
        _sha(
            mapping["calibration_policy_sha256"],
            "response.producer_identity.calibration_policy_sha256",
        ),
        _text(mapping["model_id"], "response.producer_identity.model_id"),
        _text(mapping["model_revision"], "response.producer_identity.model_revision"),
        _sha(mapping["model_sha256"], "response.producer_identity.model_sha256"),
        _text(mapping["inference_kind"], "response.producer_identity.inference_kind"),
        _sha(mapping["service_sha256"], "response.producer_identity.service_sha256"),
    )


def _ticks_from_ms(
    start_ms: int, end_ms: int, clock: ShadowCalibrationAudioClock, requested_range: TickRange
) -> TickRange:
    if start_ms < 0 or start_ms >= end_ms:
        raise _invalid("native millisecond interval is invalid")
    scale_denominator = 1_000 * clock.time_base.numerator
    start_tick = requested_range.start_pts + (
        start_ms * clock.time_base.denominator // scale_denominator
    )
    end_tick = requested_range.start_pts + (
        (end_ms * clock.time_base.denominator + scale_denominator - 1) // scale_denominator
    )
    try:
        converted = TickRange(start_tick, end_tick)
    except ValueError as error:
        raise _invalid("native millisecond interval has no positive tick range") from error
    if not requested_range.contains(converted):
        raise _invalid("native millisecond interval lies outside the full requested range")
    return converted


def _native_pairs(
    value: object, field_name: str, *, reject_overlap: bool
) -> tuple[tuple[int, int], ...]:
    if type(value) is not list or not value:  # noqa: E721
        raise _invalid(f"{field_name} must be a non-empty array")
    pairs: list[tuple[int, int]] = []
    for position, raw_pair in enumerate(cast(list[object], value)):
        if type(raw_pair) is not list or len(cast(list[object], raw_pair)) != 2:  # noqa: E721
            raise _invalid(f"{field_name}[{position}] must be a two-integer pair")
        pair = cast(list[object], raw_pair)
        start, end = (
            _integer(pair[0], f"{field_name}[{position}][0]"),
            _integer(pair[1], f"{field_name}[{position}][1]"),
        )
        if start < 0 or start >= end or (pairs and pairs[-1][0] > start):
            raise _invalid(f"{field_name} must be ordered positive-length pairs")
        if reject_overlap and pairs and pairs[-1][1] > start:
            raise _invalid(f"{field_name} must not overlap")
        pairs.append((start, end))
    return tuple(pairs)


def _decode_asr(
    value: object,
    context: ShadowCalibrationRawContext,
    requested_range: TickRange,
) -> tuple[
    tuple[ShadowCalibrationAsrObservation, ...], tuple[ShadowCalibrationWordGapSegment, ...]
]:
    if type(value) is not list or len(cast(list[object], value)) != 1:  # noqa: E721
        raise _invalid("response.asr_native_output must contain exactly one result")
    item = _exact_object(
        cast(list[object], value)[0],
        frozenset({"text", "words", "timestamp"}),
        "response.asr_native_output[0]",
    )
    _text(item["text"], "response.asr_native_output[0].text")
    if type(item["words"]) is not list or not cast(list[object], item["words"]):  # noqa: E721
        raise _invalid("response ASR words must be non-empty")
    words = tuple(_text(word, "response ASR word") for word in cast(list[object], item["words"]))
    pairs = _native_pairs(
        item["timestamp"], "response.asr_native_output[0].timestamp", reject_overlap=True
    )
    if len(words) != len(pairs):
        raise _invalid("response ASR words and timestamps must have equal length")
    observations = tuple(
        ShadowCalibrationAsrObservation(
            CalibrationObservation(
                f"asr-word-{position:08d}",
                CalibrationProducer.ASR,
                context.asr_identity.producer_id,
                "sensevoice-word-timestamp",
                context.audio_clock.clock_id,
                context.audio_clock.time_base,
                _ticks_from_ms(start, end, context.audio_clock, requested_range),
            ),
            word,
        )
        for position, ((start, end), word) in enumerate(zip(pairs, words, strict=True))
    )
    ranges: list[tuple[int, int]] = []
    segment_start = 0
    for position in range(1, len(pairs)):
        if pairs[position][0] - pairs[position - 1][1] > context.policies.word_gap_ms:
            ranges.append((segment_start, position))
            segment_start = position
    ranges.append((segment_start, len(observations)))
    segments = tuple(
        ShadowCalibrationWordGapSegment(
            f"asr-segment-{position:08d}",
            "".join(item.text for item in observations[start:end]),
            TickRange(
                observations[start].observation.observed_range.start_pts,
                observations[end - 1].observation.observed_range.end_pts,
            ),
        )
        for position, (start, end) in enumerate(ranges)
    )
    return observations, segments


def _decode_vad(
    value: object, context: ShadowCalibrationRawContext, requested_range: TickRange
) -> tuple[CalibrationObservation, ...]:
    if type(value) is not list or len(cast(list[object], value)) != 1:  # noqa: E721
        raise _invalid("response.vad_native_output must contain exactly one result")
    item = _exact_object(
        cast(list[object], value)[0], frozenset({"value"}), "response.vad_native_output[0]"
    )
    pairs = _native_pairs(
        item["value"], "response.vad_native_output[0].value", reject_overlap=False
    )
    merged: list[tuple[int, int]] = []
    for start, end in pairs:
        if merged and start - merged[-1][1] <= context.policies.vad_merge_gap_ms:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(
        CalibrationObservation(
            f"vad-segment-{position:08d}",
            CalibrationProducer.VAD,
            context.vad_identity.producer_id,
            "fsmn-vad-direct",
            context.audio_clock.clock_id,
            context.audio_clock.time_base,
            _ticks_from_ms(start, end, context.audio_clock, requested_range),
        )
        for position, (start, end) in enumerate(merged)
    )


def _measurement(
    anchors: tuple[CalibrationAnchor, ...],
    observations: tuple[CalibrationObservation, ...],
    producer: CalibrationProducer,
    producer_id: str,
    context: ShadowCalibrationRawContext,
) -> ProducerCalibrationMeasurement:
    if len(anchors) != len(observations):
        raise _invalid("alignment must be complete ordered one-to-one")
    matches = tuple(
        CalibrationAnchorMatch(anchor, observation)
        for anchor, observation in zip(anchors, observations, strict=True)
    )
    try:
        return ProducerCalibrationMeasurement(
            producer,
            producer_id,
            "sensevoice-word-timestamp"
            if producer is CalibrationProducer.ASR
            else "fsmn-vad-direct",
            context.audio_clock.clock_id,
            context.audio_clock.time_base,
            matches,
            max(match.absolute_tick for match in matches),
        )
    except CalibrationRecordError as error:
        raise _invalid(str(error)) from error


def decode_shadow_calibration_raw_response(
    blob: ShadowCalibrationRawBlob,
    invocation: ShadowCalibrationInvocation,
    context: ShadowCalibrationRawContext,
    claimed_projection: ShadowCalibrationProjection,
) -> DecodedShadowCalibrationRawResponse:
    """Strictly derive and verify one complete calibration result projection."""
    if type(blob) is not ShadowCalibrationRawBlob:  # noqa: E721
        raise _invalid("blob must be an exact ShadowCalibrationRawBlob")
    if (
        type(invocation) is not ShadowCalibrationInvocation
        or type(context) is not ShadowCalibrationRawContext
    ):  # noqa: E721
        raise _invalid("invocation and context must be exact typed values")
    if type(claimed_projection) is not ShadowCalibrationProjection:  # noqa: E721
        raise _invalid("claimed projection must be exact")
    response = _strict_json_object(blob.raw)
    required_fields = frozenset(
        {
            "schema_version",
            "request_identity_sha256",
            "source",
            "audio_clock",
            "requested_range",
            "timed_speech_policy_sha256",
            "word_gap_policy_sha256",
            "vad_merge_policy_sha256",
            "native_profile_identity_sha256",
            "producer_identities",
            "asr_native_output",
            "vad_native_output",
        }
    )
    response = _exact_object(response, required_fields, "response")
    if response["schema_version"] != SHADOW_CALIBRATION_RAW_RESPONSE_SCHEMA:
        raise _invalid("response schema version is not the calibration raw envelope")
    if (
        _sha(response["request_identity_sha256"], "response.request_identity_sha256")
        != invocation.request_identity_sha256
    ):
        raise _invalid("response request identity drift")
    if (
        _decode_source(response["source"]) != context.source.to_response_mapping()
        or invocation.request_mapping.source != context.source
        or invocation.corpus_member_reference_sha256
        != context.source.corpus_member_reference_sha256
        or invocation.request_mapping.source_byte_limits != context.source_byte_limits
        or invocation.request_mapping.container != context.container
    ):
        raise _invalid("response source drift")
    clock = _decode_clock(response["audio_clock"])
    if (
        clock != context.audio_clock
        or invocation.request_mapping.audio_clock != context.audio_clock
    ):
        raise _invalid("response clock drift")
    if (
        invocation.request_mapping.native_profile_identity_sha256
        != context.native_profile_identity_sha256
        or invocation.request_mapping.transcript_capability != context.transcript_capability
        or invocation.request_mapping.word_gap_ms != context.policies.word_gap_ms
        or invocation.request_mapping.vad_merge_gap_ms != context.policies.vad_merge_gap_ms
    ):
        raise _invalid("response capability or timing-policy drift")
    requested_range = _tick_range(response["requested_range"], "response.requested_range")
    if (
        requested_range != context.audio_clock.full_range
        or requested_range != invocation.request_mapping.requested_range
    ):
        raise _invalid("response range must be the complete locked range")
    for field_name in (
        "timed_speech_policy_sha256",
        "word_gap_policy_sha256",
        "vad_merge_policy_sha256",
    ):
        if _sha(response[field_name], f"response.{field_name}") != getattr(
            context.policies, field_name
        ) or _sha(response[field_name], f"response.{field_name}") != getattr(
            invocation.request_mapping, field_name
        ):
            raise _invalid(f"response {field_name} drift")
    if (
        _sha(response["native_profile_identity_sha256"], "response.native_profile_identity_sha256")
        != context.native_profile_identity_sha256
    ):
        raise _invalid("response native profile identity drift")
    raw_identities = response["producer_identities"]
    if type(raw_identities) is not list or len(cast(list[object], raw_identities)) != 2:  # noqa: E721
        raise _invalid("response producer identities must contain ASR then VAD")
    decoded_identities = (
        _decode_identity(cast(list[object], raw_identities)[0], CalibrationProducer.ASR),
        _decode_identity(cast(list[object], raw_identities)[1], CalibrationProducer.VAD),
    )
    if (
        decoded_identities != context.producer_identities
        or decoded_identities != invocation.request_mapping.producer_identities
    ):
        raise _invalid("response producer identity drift")
    asr_observations, word_gap_segments = _decode_asr(
        response["asr_native_output"], context, requested_range
    )
    vad_observations = _decode_vad(response["vad_native_output"], context, requested_range)
    asr_measurement = _measurement(
        context.asr_anchors,
        tuple(item.observation for item in asr_observations),
        CalibrationProducer.ASR,
        context.asr_identity.producer_id,
        context,
    )
    vad_measurement = _measurement(
        context.vad_anchors,
        vad_observations,
        CalibrationProducer.VAD,
        context.vad_identity.producer_id,
        context,
    )
    try:
        projection = ShadowCalibrationProjection(
            context.native_profile_identity_sha256,
            invocation.request_identity_sha256,
            asr_observations,
            word_gap_segments,
            vad_observations,
            CalibrationMeasurementSummary(asr_measurement, vad_measurement),
        )
    except CalibrationRecordError as error:
        raise _invalid(str(error)) from error
    if projection != claimed_projection:
        raise _invalid("claimed projection does not exactly equal the raw-derived projection")
    return DecodedShadowCalibrationRawResponse(projection, word_gap_segments)
