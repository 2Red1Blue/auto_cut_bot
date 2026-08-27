"""Versioned PC-CUDA timed-speech request closure.

This module is deliberately parallel to ``speech_port.py``.  It does not
widen the historical CPU/MPS request grammar or reuse its calibration profile.
The runtime policy is a complete immutable projection of one accepted
``pc_cuda`` capability and is echoed exactly by the service response.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from autocut_kernel.media.types import TimeBase, canonical_sha256, sha256_prefixed

from .models import LocalMediaPolicyError
from .runtime_policy import PcCudaRuntimeTimedSpeechPolicy
from .speech_port import SENSEVOICE_WORD_GUARD_PROFILE, TimedSpeechExpectedProducer

RUNTIME_TIMED_SPEECH_ROUTE = "/v2/runtime-timed-speech-evidence"
RUNTIME_TIMED_SPEECH_REQUEST_SCHEMA = "runtime-timed-speech-evidence-request-v2"
RUNTIME_TIMED_SPEECH_RESPONSE_SCHEMA = "runtime-timed-speech-evidence-response-v2"


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():  # noqa: E721
        raise LocalMediaPolicyError(f"{field_name} must be canonical non-empty text")
    return value


def _runtime_endpoint(legacy_endpoint: object) -> str:
    """Derive the dedicated v2 loopback route without accepting caller routing."""
    endpoint = _text(legacy_endpoint, "runtime timed speech endpoint")
    try:
        parsed = urlparse(endpoint)
        port = parsed.port
    except ValueError as error:
        raise LocalMediaPolicyError("runtime timed speech endpoint port is invalid") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1 <= port <= 65_535
        or parsed.path != "/v1/timed-speech-evidence"
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise LocalMediaPolicyError("runtime timed speech requires the installed loopback endpoint")
    return f"http://127.0.0.1:{port}{RUNTIME_TIMED_SPEECH_ROUTE}"


@dataclass(frozen=True, slots=True)
class RuntimeTimedSpeechEvidenceRequest:
    """One closed full-source ASR/VAD call backed by accepted PC CUDA authority."""

    source_path: Path
    source_id: str
    source_sha256: str
    kernel_max_source_bytes: int
    service_max_request_bytes: int
    effective_max_source_bytes: int
    clock_id: str
    time_base: TimeBase
    origin_tick: int
    duration_tick: int
    requested_in_tick: int
    requested_out_tick: int
    runtime_policy: PcCudaRuntimeTimedSpeechPolicy

    def __post_init__(self) -> None:
        if not self.source_path.is_absolute():
            raise LocalMediaPolicyError("runtime source_path must be absolute")
        for value, name in (
            (self.kernel_max_source_bytes, "kernel_max_source_bytes"),
            (self.service_max_request_bytes, "service_max_request_bytes"),
            (self.effective_max_source_bytes, "effective_max_source_bytes"),
        ):
            if type(value) is not int or value <= 0:  # noqa: E721
                raise LocalMediaPolicyError(f"runtime {name} must be a positive integer")
        if self.effective_max_source_bytes != min(
            self.kernel_max_source_bytes, self.service_max_request_bytes
        ):
            raise LocalMediaPolicyError("runtime effective source-byte limit does not close")
        try:
            sha256_prefixed(self.source_sha256, "runtime source_sha256")
        except ValueError as error:
            raise LocalMediaPolicyError("runtime source_sha256 is invalid") from error
        _text(self.source_id, "runtime source_id")
        _text(self.clock_id, "runtime clock_id")
        if type(self.time_base) is not TimeBase:  # noqa: E721
            raise LocalMediaPolicyError("runtime time_base must be an exact TimeBase")
        if type(self.runtime_policy) is not PcCudaRuntimeTimedSpeechPolicy:  # noqa: E721
            raise LocalMediaPolicyError("runtime request requires exact PC CUDA authority")
        _runtime_endpoint(self.runtime_policy.endpoint_url)
        if (
            self.runtime_policy.device != "cuda"
            or self.runtime_policy.runtime_capability_id != "pc_cuda"
            or self.runtime_policy.source_clock_id != self.clock_id
            or self.runtime_policy.source_time_base != self.time_base
        ):
            raise LocalMediaPolicyError("runtime CUDA authority does not match source clock")
        if (
            self.requested_in_tick != self.origin_tick
            or self.requested_out_tick != self.origin_tick + self.duration_tick
            or self.duration_tick <= 0
        ):
            raise LocalMediaPolicyError("runtime timed speech supports only the complete source range")

    @property
    def endpoint_url(self) -> str:
        return _runtime_endpoint(self.runtime_policy.endpoint_url)

    @property
    def provider_id(self) -> str:
        return self.runtime_policy.provider_id

    @property
    def provider_version(self) -> str:
        return self.runtime_policy.provider_version

    @property
    def funasr_version(self) -> str:
        return self.runtime_policy.funasr_version

    @property
    def torch_version(self) -> str:
        return self.runtime_policy.torch_version

    @property
    def device(self) -> Literal["cuda"]:
        return "cuda"

    @property
    def word_timing_capability(self) -> Literal["required"]:
        return "required"

    @property
    def policy_sha256(self) -> str:
        return self.runtime_policy.timed_speech_policy_sha256

    @property
    def timeout_seconds(self) -> int:
        return self.runtime_policy.timeout_seconds

    @property
    def max_response_bytes(self) -> int:
        return self.runtime_policy.max_response_bytes

    @property
    def utterance_gap_milliseconds(self) -> int:
        return self.runtime_policy.utterance_gap_milliseconds

    @property
    def vad_merge_gap_milliseconds(self) -> int:
        return self.runtime_policy.vad_merge_gap_milliseconds

    @property
    def expected_producers(
        self,
    ) -> tuple[TimedSpeechExpectedProducer, TimedSpeechExpectedProducer]:
        producers = tuple(
            TimedSpeechExpectedProducer(
                item.producer_kind,
                item.producer_id,
                item.producer_version,
                item.generation_policy_sha256,
                item.detector_sha256,
                item.calibration_policy_sha256,
                item.calibration_record_sha256,
                item.timing_error_bound_tick,
                item.model_id,
                item.model_revision,
                item.model_sha256,
                item.service_sha256,
                item.inference_kind,  # type: ignore[arg-type]
            )
            for item in self.runtime_policy.producers
        )
        return (producers[0], producers[1])

    @property
    def response_schema_version(self) -> str:
        return RUNTIME_TIMED_SPEECH_RESPONSE_SCHEMA

    @property
    def response_extra_fields(self) -> frozenset[str]:
        return frozenset({"runtime_authority"})

    def validate_response_authority(self, value: object) -> None:
        if value != self.runtime_policy.to_mapping():
            raise LocalMediaPolicyError("runtime response authority differs from request closure")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": RUNTIME_TIMED_SPEECH_REQUEST_SCHEMA,
            "source": {"source_id": self.source_id, "source_sha256": self.source_sha256},
            "source_byte_limits": {
                "kernel_max_source_bytes": self.kernel_max_source_bytes,
                "service_max_request_bytes": self.service_max_request_bytes,
                "effective_max_source_bytes": self.effective_max_source_bytes,
            },
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
            "runtime_authority": self.runtime_policy.to_mapping(),
            "timed_speech_policy_sha256": self.policy_sha256,
            "response_limits": {"max_response_bytes": self.max_response_bytes},
            "timing_policy": {
                "utterance_gap_milliseconds": self.utterance_gap_milliseconds,
                "vad_merge_gap_milliseconds": self.vad_merge_gap_milliseconds,
            },
            "transcript_capability": {
                "profile": SENSEVOICE_WORD_GUARD_PROFILE,
                "segment": "complete",
                "segment_semantics": "utterance_gap_protected_range",
                "sentence": "not_applicable",
                "word": "complete",
                "word_timing": "required",
            },
        }

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.to_mapping())


__all__ = [
    "RUNTIME_TIMED_SPEECH_REQUEST_SCHEMA",
    "RUNTIME_TIMED_SPEECH_RESPONSE_SCHEMA",
    "RUNTIME_TIMED_SPEECH_ROUTE",
    "RuntimeTimedSpeechEvidenceRequest",
]
