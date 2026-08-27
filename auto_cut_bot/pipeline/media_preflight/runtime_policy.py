"""Pure application projection for accepted PC CUDA timed speech.

The legacy local media policy supplies physical-detector and operational HTTP
limits only.  Its CPU/MPS speech identity is deliberately not CUDA authority;
the accepted ``RuntimeTimedSpeechProjection`` supplies every CUDA producer,
record reference, timing bound, and audit identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from autocut_kernel.media.calibration_record import CalibrationRecordProducerIdentity
from autocut_kernel.media.types import TimeBase, canonical_sha256, sha256_prefixed
from autocut_kernel.registry.runtime_timed_speech import RuntimeTimedSpeechProjection

from .models import LocalMediaPreflightPolicy

_SCHEMA_VERSION = "pc-cuda-runtime-timed-speech-policy-v1"
_RUNTIME_TIMED_SPEECH_ROUTE = "/v2/runtime-timed-speech-evidence"
_SPEECH_KINDS: tuple[Literal["asr", "vad"], Literal["asr", "vad"]] = ("asr", "vad")


class RuntimeMediaPreflightPolicyError(ValueError):
    """The static operational policy and CUDA authority cannot be closed."""


def _fail(detail: str) -> RuntimeMediaPreflightPolicyError:
    return RuntimeMediaPreflightPolicyError(
        f"PC CUDA runtime timed-speech policy rejected: {detail}"
    )


def _sha(value: object, field_name: str) -> str:
    try:
        return sha256_prefixed(value, field_name)
    except ValueError as error:
        raise _fail(str(error)) from error


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():  # noqa: E721
        raise _fail(f"{field_name} must be canonical non-empty text")
    return value


def _cuda_endpoint(legacy_endpoint: object) -> str:
    """Share only the configured loopback authority, never the CPU route."""
    endpoint = _text(legacy_endpoint, "endpoint_url")
    try:
        parsed = urlparse(endpoint)
        port = parsed.port
    except ValueError as error:
        raise _fail("endpoint_url port is invalid") from error
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
        raise _fail("runtime CUDA policy requires the installed CPU loopback origin")
    return f"http://127.0.0.1:{port}{_RUNTIME_TIMED_SPEECH_ROUTE}"


@dataclass(frozen=True, slots=True)
class RuntimeTimedSpeechProducerPolicy:
    """One accepted CUDA ASR/VAD identity and its exact source-clock bound."""

    producer_kind: Literal["asr", "vad"]
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
    calibration_record_sha256: str
    timing_error_bound_tick: int

    def __post_init__(self) -> None:
        expected = {
            "asr": ("SenseVoiceSmall", "sensevoice-word-timestamp"),
            "vad": ("fsmn-vad", "fsmn-vad-direct"),
        }.get(self.producer_kind)
        if expected is None or (self.model_id, self.inference_kind) != expected:
            raise _fail("runtime producer role/model/inference identity differs")
        for name in ("producer_id", "producer_version", "model_revision"):
            _text(getattr(self, name), f"runtime producer {name}")
        for name in (
            "generation_policy_sha256", "detector_sha256", "calibration_policy_sha256",
            "model_sha256", "service_sha256", "calibration_record_sha256",
        ):
            _sha(getattr(self, name), f"runtime producer {name}")
        if type(self.timing_error_bound_tick) is not int or self.timing_error_bound_tick < 1:  # noqa: E721
            raise _fail("runtime producer timing bound must be a positive exact integer")

    def to_mapping(self) -> dict[str, object]:
        return {
            "calibration_policy_sha256": self.calibration_policy_sha256,
            "calibration_record_sha256": self.calibration_record_sha256,
            "detector_sha256": self.detector_sha256,
            "generation_policy_sha256": self.generation_policy_sha256,
            "inference_kind": self.inference_kind,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "model_sha256": self.model_sha256,
            "producer_id": self.producer_id,
            "producer_kind": self.producer_kind,
            "producer_version": self.producer_version,
            "service_sha256": self.service_sha256,
            "timing_error_bound_tick": self.timing_error_bound_tick,
        }


@dataclass(frozen=True, slots=True)
class PcCudaRuntimeTimedSpeechPolicy:
    """Closed request-facing CUDA speech authority with static operation limits."""

    static_policy_sha256: str
    runtime_capability_id: str
    device: Literal["cuda"]
    runtime_measurement_identity_sha256: str
    timing_compatibility_sha256: str
    runtime_projection_compatibility_sha256: str
    build_audit_sha256: str
    runtime_projection_sha256: str
    funasr_version: str
    torch_version: str
    profile_source_sha256: str
    registry_snapshot_sha256: str
    accepted_record_sha256: str
    validation_receipt_sha256: str
    native_port_identity_sha256: str
    source_clock_id: str
    source_time_base: TimeBase
    timed_speech_policy_sha256: str
    word_gap_policy_sha256: str
    vad_merge_policy_sha256: str
    alignment_policy_sha256: str
    acceptance_policy_sha256: str
    endpoint_url: str
    provider_id: str
    provider_version: str
    timeout_seconds: int
    max_response_bytes: int
    utterance_gap_milliseconds: int
    vad_merge_gap_milliseconds: int
    producers: tuple[RuntimeTimedSpeechProducerPolicy, RuntimeTimedSpeechProducerPolicy]

    def __post_init__(self) -> None:
        if self.runtime_capability_id != "pc_cuda" or self.device != "cuda":
            raise _fail("only pc_cuda may establish CUDA runtime timed speech")
        for name in (
            "static_policy_sha256", "runtime_measurement_identity_sha256",
            "timing_compatibility_sha256", "runtime_projection_compatibility_sha256",
            "build_audit_sha256", "runtime_projection_sha256", "profile_source_sha256",
            "registry_snapshot_sha256", "accepted_record_sha256", "validation_receipt_sha256",
            "native_port_identity_sha256",
            "timed_speech_policy_sha256", "word_gap_policy_sha256",
            "vad_merge_policy_sha256", "alignment_policy_sha256", "acceptance_policy_sha256",
        ):
            _sha(getattr(self, name), name)
        if self.accepted_record_sha256 == self.validation_receipt_sha256:
            raise _fail("accepted record and validation receipt must differ")
        _text(self.source_clock_id, "source_clock_id")
        if type(self.source_time_base) is not TimeBase:  # noqa: E721
            raise _fail("source_time_base must be an exact TimeBase")
        for name in ("endpoint_url", "provider_id", "provider_version", "funasr_version", "torch_version"):
            _text(getattr(self, name), name)
        for name in (
            "timeout_seconds", "max_response_bytes", "utterance_gap_milliseconds", "vad_merge_gap_milliseconds",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0 or (name in {"timeout_seconds", "max_response_bytes"} and value == 0):  # noqa: E721
                raise _fail(f"{name} must be an allowed exact integer")
        if (
            type(self.producers) is not tuple
            or len(self.producers) != 2
            or any(type(item) is not RuntimeTimedSpeechProducerPolicy for item in self.producers)
            or tuple(item.producer_kind for item in self.producers) != _SPEECH_KINDS
        ):
            raise _fail("runtime producers must be an exact ASR/VAD tuple")

    def _base_mapping(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "static_policy_sha256": self.static_policy_sha256,
            "runtime_capability_id": self.runtime_capability_id,
            "device": self.device,
            "runtime_measurement_identity_sha256": self.runtime_measurement_identity_sha256,
            "timing_compatibility_sha256": self.timing_compatibility_sha256,
            "runtime_projection_compatibility_sha256": self.runtime_projection_compatibility_sha256,
            "runtime": {
                "funasr_version": self.funasr_version,
                "torch_version": self.torch_version,
            },
            "profile_source_sha256": self.profile_source_sha256,
            "registry_snapshot_sha256": self.registry_snapshot_sha256,
            "accepted_record_sha256": self.accepted_record_sha256,
            "validation_receipt_sha256": self.validation_receipt_sha256,
            "native_port_identity_sha256": self.native_port_identity_sha256,
            "source_clock": {
                "clock_id": self.source_clock_id,
                "time_base": {
                    "numerator": self.source_time_base.numerator,
                    "denominator": self.source_time_base.denominator,
                },
            },
            "timing": {
                "timed_speech_policy_sha256": self.timed_speech_policy_sha256,
                "word_gap_policy_sha256": self.word_gap_policy_sha256,
                "vad_merge_policy_sha256": self.vad_merge_policy_sha256,
                "alignment_policy_sha256": self.alignment_policy_sha256,
                "acceptance_policy_sha256": self.acceptance_policy_sha256,
                "utterance_gap_milliseconds": self.utterance_gap_milliseconds,
                "vad_merge_gap_milliseconds": self.vad_merge_gap_milliseconds,
            },
            "operation": {
                "endpoint_url": _cuda_endpoint(self.endpoint_url),
                "provider_id": self.provider_id,
                "provider_version": self.provider_version,
                "timeout_seconds": self.timeout_seconds,
                "max_response_bytes": self.max_response_bytes,
            },
            "producers": [item.to_mapping() for item in self.producers],
        }

    def compatibility_mapping(self) -> dict[str, object]:
        """Admission-relevant identity, intentionally excluding build provenance."""

        return self._base_mapping()

    def to_mapping(self) -> dict[str, object]:
        """Complete canonical audit/provenance closure."""

        return {
            **self._base_mapping(),
            "build_audit_sha256": self.build_audit_sha256,
            "runtime_projection_sha256": self.runtime_projection_sha256,
        }

    @property
    def compatibility_hash(self) -> str:
        return canonical_sha256(self.compatibility_mapping())

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @classmethod
    def from_static_policy_and_projection(
        cls, static_policy: LocalMediaPreflightPolicy, projection: RuntimeTimedSpeechProjection
    ) -> PcCudaRuntimeTimedSpeechPolicy:
        return project_pc_cuda_runtime_timed_speech_policy(static_policy, projection)


RuntimeTimedSpeechPolicyProjection = PcCudaRuntimeTimedSpeechPolicy


def _producer(
    identity: object,
    kind: Literal["asr", "vad"],
    record_sha256: str,
    bound_tick: int,
) -> RuntimeTimedSpeechProducerPolicy:
    if type(identity) is not CalibrationRecordProducerIdentity:  # noqa: E721
        raise _fail("runtime projection producers must be exact accepted record identities")
    if identity.role.value != kind:
        raise _fail("runtime projection producers must be ordered ASR then VAD")
    return RuntimeTimedSpeechProducerPolicy(
        kind, identity.producer_id, identity.producer_version,
        identity.generation_policy_sha256, identity.detector_sha256,
        identity.calibration_policy_sha256, identity.model_id, identity.model_revision,
        identity.model_sha256, identity.inference_kind, identity.service_sha256,
        record_sha256, bound_tick,
    )


def project_pc_cuda_runtime_timed_speech_policy(
    static_policy: LocalMediaPreflightPolicy,
    projection: RuntimeTimedSpeechProjection,
) -> PcCudaRuntimeTimedSpeechPolicy:
    """Build the CUDA-only policy from static operation limits plus accepted authority."""

    if type(static_policy) is not LocalMediaPreflightPolicy:  # noqa: E721
        raise _fail("requires an exact static LocalMediaPreflightPolicy")
    if type(projection) is not RuntimeTimedSpeechProjection:  # noqa: E721
        raise _fail("requires an exact RuntimeTimedSpeechProjection")
    if projection.runtime_capability_id != "pc_cuda" or projection.device_class != "cuda":
        raise _fail("runtime projection must be the pc_cuda CUDA authority")
    if static_policy.word_timing_capability != "required":
        raise _fail("static policy must require word timestamps")
    if static_policy.timed_speech_policy_sha256 != projection.timed_speech_policy_sha256:
        raise _fail("static timing policy differs from selected runtime projection")
    if type(projection.source_time_base) is not TimeBase:  # noqa: E721
        raise _fail("runtime projection source clock is invalid")
    if (
        type(projection.producers) is not tuple
        or len(projection.producers) != 2
        or tuple(item.role.value for item in projection.producers) != _SPEECH_KINDS
    ):
        raise _fail("runtime projection producers are not an exact ASR/VAD tuple")

    producers = tuple(
        _producer(identity, kind, record_sha256, bound_tick)
        for identity, kind, record_sha256, bound_tick in zip(
            projection.producers,
            _SPEECH_KINDS,
            (projection.asr_calibration_record_sha256, projection.vad_calibration_record_sha256),
            (projection.asr_timing_error_bound_tick, projection.vad_timing_error_bound_tick),
            strict=True,
        )
    )
    return PcCudaRuntimeTimedSpeechPolicy(
        static_policy.canonical_hash, projection.runtime_capability_id, "cuda",
        projection.runtime_measurement_identity_sha256, projection.timing_compatibility_sha256,
        projection.compatibility_hash, projection.build_audit_sha256, projection.canonical_hash,
        projection.funasr_version, projection.torch_version,
        projection.profile_source_sha256,
        projection.registry_snapshot_sha256, projection.record_sha256,
        projection.validation_receipt_sha256, projection.native_port_identity_sha256,
        projection.source_clock_id,
        projection.source_time_base, projection.timed_speech_policy_sha256,
        projection.word_gap_policy_sha256, projection.vad_merge_policy_sha256,
        projection.alignment_policy_sha256, projection.acceptance_policy_sha256,
        static_policy.timed_speech_endpoint_url, static_policy.timed_speech_provider_id,
        static_policy.timed_speech_provider_version, static_policy.timed_speech_timeout_seconds,
        static_policy.timed_speech_max_response_bytes, static_policy.utterance_gap_milliseconds,
        static_policy.vad_merge_gap_milliseconds, (producers[0], producers[1]),
    )


build_pc_cuda_runtime_timed_speech_policy = project_pc_cuda_runtime_timed_speech_policy


__all__ = [
    "PcCudaRuntimeTimedSpeechPolicy",
    "RuntimeMediaPreflightPolicyError",
    "RuntimeTimedSpeechPolicyProjection",
    "RuntimeTimedSpeechProducerPolicy",
    "build_pc_cuda_runtime_timed_speech_policy",
    "project_pc_cuda_runtime_timed_speech_policy",
]
