"""Typed boundary for jointly returned, independently produced ASR and VAD evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from autocut_kernel.media import SpeechActivitySet, TimeBase, TranscriptSet
from autocut_kernel.media.types import canonical_sha256, sha256_prefixed

from .models import LocalMediaEvidenceError, LocalMediaPolicyError, validate_timed_speech_endpoint


@dataclass(frozen=True, slots=True)
class TimedSpeechExpectedProducer:
    producer_kind: Literal["asr", "vad"]
    producer_id: str
    producer_version: str
    generation_policy_sha256: str
    detector_sha256: str
    calibration_policy_sha256: str
    calibration_record_sha256: str
    timing_error_bound_tick: int
    model_id: str
    model_revision: str
    model_sha256: str
    service_sha256: str
    inference_kind: Literal["sensevoice-word-timestamp", "fsmn-vad-direct"]

    def __post_init__(self) -> None:
        if self.producer_kind not in {"asr", "vad"}:
            raise LocalMediaPolicyError("invalid speech producer")
        for name in (
            "generation_policy_sha256",
            "detector_sha256",
            "calibration_policy_sha256",
            "calibration_record_sha256",
            "model_sha256",
            "service_sha256",
        ):
            sha256_prefixed(getattr(self, name), name)
        if self.timing_error_bound_tick <= 0:
            raise LocalMediaPolicyError("timing error bound must be positive")
        expected_inference = (
            "sensevoice-word-timestamp" if self.producer_kind == "asr" else "fsmn-vad-direct"
        )
        if self.inference_kind != expected_inference:
            raise LocalMediaPolicyError("speech producer inference kind is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class TimedSpeechEvidenceRequest:
    source_path: Path
    source_id: str
    source_sha256: str
    clock_id: str
    time_base: TimeBase
    origin_tick: int
    duration_tick: int
    requested_in_tick: int
    requested_out_tick: int
    endpoint_url: str
    provider_id: str
    provider_version: str
    funasr_version: str
    torch_version: str
    device: Literal["cpu", "mps"]
    word_timing_capability: Literal["required", "sentence_only"]
    policy_sha256: str
    profile_calibration_sha256: str
    expected_producers: tuple[TimedSpeechExpectedProducer, TimedSpeechExpectedProducer]
    timeout_seconds: int
    max_response_bytes: int
    utterance_gap_milliseconds: int
    vad_merge_gap_milliseconds: int

    def __post_init__(self) -> None:
        if not self.source_path.is_absolute():
            raise LocalMediaPolicyError("source_path must be absolute")
        validate_timed_speech_endpoint(self.endpoint_url)
        for value, name in (
            (self.source_sha256, "source_sha256"),
            (self.policy_sha256, "policy_sha256"),
            (self.profile_calibration_sha256, "profile_calibration_sha256"),
        ):
            sha256_prefixed(value, name)
        if (
            self.requested_in_tick != self.origin_tick
            or self.requested_out_tick != self.origin_tick + self.duration_tick
        ):
            raise LocalMediaPolicyError("timed speech v1 supports only complete source range")
        if (
            self.duration_tick <= 0
            or min(
                self.timeout_seconds,
                self.max_response_bytes,
                self.utterance_gap_milliseconds,
                self.vad_merge_gap_milliseconds,
            )
            <= 0
        ):
            raise LocalMediaPolicyError("timed speech bounds must be positive")
        if tuple(x.producer_kind for x in self.expected_producers) != ("asr", "vad"):
            raise LocalMediaPolicyError("producers must be asr then vad")

    def to_mapping(self) -> dict[str, object]:
        word = "complete" if self.word_timing_capability == "required" else "not_applicable"
        return {
            "schema_version": "timed-speech-evidence-request-v1",
            "source": {"source_id": self.source_id, "source_sha256": self.source_sha256},
            "container": {"media_type": "video/mp4", "safe_suffix": ".mp4"},
            "audio_clock": {
                "clock_id": self.clock_id,
                "time_base": {
                    "numerator": self.time_base.numerator,
                    "denominator": self.time_base.denominator,
                },
                "origin_tick": self.origin_tick,
                "duration_tick": self.duration_tick,
            },
            "requested_range": {
                "in_tick": self.requested_in_tick,
                "out_tick": self.requested_out_tick,
            },
            "profile": {
                "provider_id": self.provider_id,
                "provider_version": self.provider_version,
                "funasr_version": self.funasr_version,
                "torch_version": self.torch_version,
                "device": self.device,
                "word_timing_capability": self.word_timing_capability,
                "profile_calibration_sha256": self.profile_calibration_sha256,
            },
            "expected_producers": [x.to_mapping() for x in self.expected_producers],
            "timed_speech_policy_sha256": self.policy_sha256,
            "response_limits": {"max_response_bytes": self.max_response_bytes},
            "timing_policy": {
                "utterance_gap_milliseconds": self.utterance_gap_milliseconds,
                "vad_merge_gap_milliseconds": self.vad_merge_gap_milliseconds,
            },
            "transcript_capability": {
                "profile": "sensevoice_timed_transcript_v1",
                "segment": "complete",
                "sentence": "complete",
                "word": word,
                "word_timing": self.word_timing_capability,
            },
        }

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class TimedSpeechProducerIdentity:
    producer_kind: Literal["asr", "vad"]
    provider_id: str
    provider_version: str
    funasr_version: str
    torch_version: str
    device: str
    model_id: str
    model_revision: str
    model_sha256: str
    producer_id: str
    producer_version: str
    generation_policy_sha256: str
    detector_sha256: str
    calibration_policy_sha256: str
    calibration_record_sha256: str
    service_sha256: str
    inference_kind: Literal["sensevoice-word-timestamp", "fsmn-vad-direct"]

    def __post_init__(self) -> None:
        expected = "sensevoice-word-timestamp" if self.producer_kind == "asr" else "fsmn-vad-direct"
        if self.producer_kind not in {"asr", "vad"} or self.inference_kind != expected:
            raise LocalMediaEvidenceError("measured speech producer kind is invalid")
        for name in (
            "model_sha256",
            "generation_policy_sha256",
            "detector_sha256",
            "calibration_policy_sha256",
            "calibration_record_sha256",
            "service_sha256",
        ):
            sha256_prefixed(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class TimedSpeechTimingErrorBound:
    producer_kind: Literal["asr", "vad"]
    early_tick: int
    late_tick: int
    time_base: TimeBase

    def __post_init__(self) -> None:
        if self.producer_kind not in {"asr", "vad"}:
            raise LocalMediaEvidenceError("invalid timing-bound producer")
        if type(self.early_tick) is not int or type(self.late_tick) is not int:
            raise LocalMediaEvidenceError("timing bounds must be integers")
        if self.early_tick <= 0 or self.late_tick <= 0:
            raise LocalMediaEvidenceError("timing bounds must be positive")


@dataclass(frozen=True, slots=True)
class TimedSpeechInvocationTrace:
    endpoint_url: str
    request_sha256: str
    response_sha256: str
    producer_identity_sha256: str
    service_sha256: str


@dataclass(frozen=True, slots=True)
class TimedSpeechEvidence:
    transcript: TranscriptSet
    speech_activity: SpeechActivitySet
    producer_identities: tuple[TimedSpeechProducerIdentity, TimedSpeechProducerIdentity]
    timing_error_bounds: tuple[TimedSpeechTimingErrorBound, TimedSpeechTimingErrorBound]
    invocation_trace: TimedSpeechInvocationTrace

    def __post_init__(self) -> None:
        if tuple(x.producer_kind for x in self.producer_identities) != ("asr", "vad"):
            raise LocalMediaEvidenceError("identities must be asr then vad")
        if tuple(x.producer_kind for x in self.timing_error_bounds) != ("asr", "vad"):
            raise LocalMediaEvidenceError("timing bounds must be asr then vad")


class TimedSpeechEvidencePort(Protocol):
    def produce(self, request: TimedSpeechEvidenceRequest) -> TimedSpeechEvidence: ...
