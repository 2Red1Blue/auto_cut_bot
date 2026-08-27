"""Closed request, policy, provenance, and result types for local media preflight."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import urlparse

from autocut_kernel.media import (
    AdaptiveEvidenceWindowPolicy,
    AudioSampleBoundarySet,
    CalibrationBinding,
    FramePtsIndexSet,
    RootMediaEvidenceBundle,
    TimeBase,
)
from autocut_kernel.media.types import canonical_sha256, sha256_prefixed

if TYPE_CHECKING:
    from .runtime_policy import PcCudaRuntimeTimedSpeechPolicy

ProducerKind = Literal["frame", "audio", "asr", "vad", "shot", "scene", "visual", "subtitle"]
_PRODUCER_KINDS: tuple[ProducerKind, ...] = (
    "frame",
    "audio",
    "asr",
    "vad",
    "shot",
    "scene",
    "visual",
    "subtitle",
)


class LocalMediaPreflightError(RuntimeError):
    """A stable, fail-closed local producer refusal."""

    code = "LOCAL_MEDIA_PREFLIGHT_FAILED"

    def __init__(self, detail: str, *, code: str | None = None) -> None:
        super().__init__(detail)
        self.code = code or type(self).code


class LocalMediaPolicyError(LocalMediaPreflightError):
    code = "LOCAL_MEDIA_POLICY_INVALID"


class LocalMediaSourceError(LocalMediaPreflightError):
    code = "LOCAL_MEDIA_SOURCE_MISMATCH"


class LocalMediaToolError(LocalMediaPreflightError):
    code = "LOCAL_MEDIA_TOOL_FAILED"


class LocalMediaEvidenceError(LocalMediaPreflightError):
    code = "LOCAL_MEDIA_EVIDENCE_INDETERMINATE"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise LocalMediaPolicyError(f"{name} must be non-empty text")
    return value


def _positive(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:  # noqa: E721
        raise LocalMediaPolicyError(f"{name} must be a positive integer")
    return value


def _non_negative(value: object, name: str) -> int:
    if type(value) is not int or value < 0:  # noqa: E721
        raise LocalMediaPolicyError(f"{name} must be a non-negative integer")
    return value


def _ppm(value: object, name: str, *, allow_zero: bool = True) -> int:
    if type(value) is not int or not (0 if allow_zero else 1) <= value <= 1_000_000:  # noqa: E721
        raise LocalMediaPolicyError(f"{name} must be an integer ppm value")
    return value


def validate_timed_speech_endpoint(value: object) -> str:
    endpoint = _text(value, "timed_speech_endpoint_url")
    try:
        parsed = urlparse(endpoint)
        port = parsed.port
    except ValueError as error:
        raise LocalMediaPolicyError("timed speech endpoint port is invalid") from error
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
        raise LocalMediaPolicyError("timed speech endpoint must be exact loopback HTTP path")
    return endpoint


@dataclass(frozen=True, slots=True)
class ProducerCalibrationIdentity:
    """Frozen external calibration and detector identity for one producer."""

    producer_kind: ProducerKind
    producer_id: str
    producer_version: str
    generation_policy_sha256: str
    detector_sha256: str
    calibration_policy_sha256: str
    calibration_record_sha256: str
    timing_error_bound_microseconds: int

    def __post_init__(self) -> None:
        if self.producer_kind not in _PRODUCER_KINDS:
            raise LocalMediaPolicyError("calibration producer_kind is not registered")
        _text(self.producer_id, "calibration.producer_id")
        _text(self.producer_version, "calibration.producer_version")
        for name in (
            "generation_policy_sha256",
            "detector_sha256",
            "calibration_policy_sha256",
            "calibration_record_sha256",
        ):
            try:
                sha256_prefixed(getattr(self, name), f"calibration.{name}")
            except ValueError as error:
                raise LocalMediaPolicyError(str(error)) from error
        _positive(
            self.timing_error_bound_microseconds,
            "calibration.timing_error_bound_microseconds",
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "calibration_policy_sha256": self.calibration_policy_sha256,
            "calibration_record_sha256": self.calibration_record_sha256,
            "detector_sha256": self.detector_sha256,
            "generation_policy_sha256": self.generation_policy_sha256,
            "producer_id": self.producer_id,
            "producer_kind": self.producer_kind,
            "producer_version": self.producer_version,
            "timing_error_bound_microseconds": self.timing_error_bound_microseconds,
        }


@dataclass(frozen=True, slots=True)
class LocalMediaPreflightPolicy:
    """Closed detector policy. Every threshold is explicit and calibration-bound."""

    policy_id: str
    policy_version: str
    timed_speech_endpoint_url: str
    timed_speech_provider_id: str
    timed_speech_provider_version: str
    timed_speech_service_sha256: str
    funasr_version: str
    torch_version: str
    speech_device: str
    word_timing_capability: Literal["required", "sentence_only"]
    asr_model_id: str
    asr_model_revision: str
    asr_model_sha256: str
    vad_model_id: str
    vad_model_revision: str
    vad_model_sha256: str
    timed_speech_policy_sha256: str
    timed_speech_calibration_sha256: str
    initial_left_expansion_milliseconds: int
    initial_right_expansion_milliseconds: int
    expansion_step_milliseconds: int
    max_expansion_count: int
    boundary_touch_margin_milliseconds: int
    analysis_fps_numerator: int
    analysis_fps_denominator: int
    analysis_width: int
    analysis_height: int
    max_analysis_frames: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    probe_timeout_seconds: int
    analysis_timeout_seconds: int
    timed_speech_timeout_seconds: int
    timed_speech_max_response_bytes: int
    utterance_gap_milliseconds: int
    vad_merge_gap_milliseconds: int
    black_luma_max: int
    white_luma_min: int
    frozen_change_ppm_max: int
    transition_change_ppm_min: int
    shot_change_ppm_min: int
    scene_change_ppm_min: int
    subtitle_edge_delta_min: int
    subtitle_edge_fraction_ppm_min: int
    subtitle_min_consecutive_samples: int
    calibrations: tuple[ProducerCalibrationIdentity, ...]

    def __post_init__(self) -> None:
        _text(self.policy_id, "policy_id")
        _text(self.policy_version, "policy_version")
        for name in (
            "timed_speech_provider_id",
            "timed_speech_provider_version",
            "funasr_version",
            "torch_version",
            "speech_device",
            "asr_model_id",
            "asr_model_revision",
            "vad_model_id",
            "vad_model_revision",
        ):
            _text(getattr(self, name), name)
        validate_timed_speech_endpoint(self.timed_speech_endpoint_url)
        if self.speech_device not in {"cpu", "mps"}:
            raise LocalMediaPolicyError("speech_device must be cpu or mps")
        if self.word_timing_capability not in {"required", "sentence_only"}:
            raise LocalMediaPolicyError("word_timing_capability is unsupported")
        for name in (
            "asr_model_sha256",
            "vad_model_sha256",
            "timed_speech_policy_sha256",
            "timed_speech_calibration_sha256",
            "timed_speech_service_sha256",
        ):
            try:
                sha256_prefixed(getattr(self, name), name)
            except ValueError as error:
                raise LocalMediaPolicyError(str(error)) from error
        for name in (
            "analysis_fps_numerator",
            "analysis_fps_denominator",
            "analysis_width",
            "analysis_height",
            "max_analysis_frames",
            "max_stdout_bytes",
            "max_stderr_bytes",
            "probe_timeout_seconds",
            "analysis_timeout_seconds",
            "timed_speech_timeout_seconds",
            "timed_speech_max_response_bytes",
            "utterance_gap_milliseconds",
            "vad_merge_gap_milliseconds",
            "initial_left_expansion_milliseconds",
            "initial_right_expansion_milliseconds",
            "expansion_step_milliseconds",
            "boundary_touch_margin_milliseconds",
            "subtitle_edge_delta_min",
            "subtitle_min_consecutive_samples",
        ):
            _positive(getattr(self, name), f"policy.{name}")
        _non_negative(self.max_expansion_count, "policy.max_expansion_count")
        if not 0 <= self.black_luma_max < self.white_luma_min <= 255:
            raise LocalMediaPolicyError("black/white luma thresholds are inconsistent")
        if not 1 <= self.subtitle_edge_delta_min <= 255:
            raise LocalMediaPolicyError("subtitle_edge_delta_min must be in [1, 255]")
        for name in (
            "frozen_change_ppm_max",
            "transition_change_ppm_min",
            "shot_change_ppm_min",
            "scene_change_ppm_min",
            "subtitle_edge_fraction_ppm_min",
        ):
            _ppm(getattr(self, name), f"policy.{name}")
        if not (
            self.frozen_change_ppm_max
            < self.shot_change_ppm_min
            <= self.scene_change_ppm_min
            <= self.transition_change_ppm_min
        ):
            raise LocalMediaPolicyError("visual/shot/scene change thresholds are inconsistent")
        required_rawvideo_bytes = (
            self.analysis_width * self.analysis_height * self.max_analysis_frames
        )
        if self.max_stdout_bytes < required_rawvideo_bytes:
            raise LocalMediaPolicyError(
                "max_stdout_bytes must bound the complete configured rawvideo analysis"
            )
        calibrations = tuple(self.calibrations)
        if tuple(item.producer_kind for item in calibrations) != _PRODUCER_KINDS:
            raise LocalMediaPolicyError(
                "calibrations must contain frame/audio/asr/vad/shot/scene/visual/subtitle in canonical order"
            )
        object.__setattr__(self, "calibrations", calibrations)

    @classmethod
    def from_calibrated_values(cls, **values: object) -> LocalMediaPreflightPolicy:
        """The public construction entry; no deployment threshold is supplied implicitly."""

        try:
            return cls(**values)  # type: ignore[arg-type]
        except TypeError as error:
            raise LocalMediaPolicyError("closed policy fields are missing or unknown") from error

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> LocalMediaPreflightPolicy:
        """Decode the environment-injected JSON object with an exact closed schema."""

        expected = {
            "policy_id",
            "policy_version",
            "timed_speech_endpoint_url",
            "timed_speech_provider_id",
            "timed_speech_provider_version",
            "timed_speech_service_sha256",
            "funasr_version",
            "torch_version",
            "speech_device",
            "word_timing_capability",
            "asr_model_id",
            "asr_model_revision",
            "asr_model_sha256",
            "vad_model_id",
            "vad_model_revision",
            "vad_model_sha256",
            "timed_speech_policy_sha256",
            "timed_speech_calibration_sha256",
            "initial_left_expansion_milliseconds",
            "initial_right_expansion_milliseconds",
            "expansion_step_milliseconds",
            "max_expansion_count",
            "boundary_touch_margin_milliseconds",
            "analysis_fps_numerator",
            "analysis_fps_denominator",
            "analysis_width",
            "analysis_height",
            "max_analysis_frames",
            "max_stdout_bytes",
            "max_stderr_bytes",
            "probe_timeout_seconds",
            "analysis_timeout_seconds",
            "timed_speech_timeout_seconds",
            "timed_speech_max_response_bytes",
            "utterance_gap_milliseconds",
            "vad_merge_gap_milliseconds",
            "black_luma_max",
            "white_luma_min",
            "frozen_change_ppm_max",
            "transition_change_ppm_min",
            "shot_change_ppm_min",
            "scene_change_ppm_min",
            "subtitle_edge_delta_min",
            "subtitle_edge_fraction_ppm_min",
            "subtitle_min_consecutive_samples",
            "calibrations",
        }
        if set(value) != expected:
            raise LocalMediaPolicyError("media preflight policy schema is not closed")
        raw_calibrations = value["calibrations"]
        if not isinstance(raw_calibrations, list):
            raise LocalMediaPolicyError("policy.calibrations must be an array")
        calibration_fields = {
            "producer_kind",
            "producer_id",
            "producer_version",
            "generation_policy_sha256",
            "detector_sha256",
            "calibration_policy_sha256",
            "calibration_record_sha256",
            "timing_error_bound_microseconds",
        }
        calibrations: list[ProducerCalibrationIdentity] = []
        for raw_object in cast(list[object], raw_calibrations):
            if not isinstance(raw_object, Mapping):
                raise LocalMediaPolicyError("policy calibration schema is not closed")
            raw = cast(Mapping[str, object], raw_object)
            if set(raw) != calibration_fields:
                raise LocalMediaPolicyError("policy calibration schema is not closed")
            kind = raw["producer_kind"]
            if type(kind) is not str or kind not in _PRODUCER_KINDS:  # noqa: E721
                raise LocalMediaPolicyError("policy calibration kind is invalid")
            calibrations.append(
                ProducerCalibrationIdentity(
                    producer_kind=kind,
                    producer_id=raw["producer_id"],  # type: ignore[arg-type]
                    producer_version=raw["producer_version"],  # type: ignore[arg-type]
                    generation_policy_sha256=raw["generation_policy_sha256"],  # type: ignore[arg-type]
                    detector_sha256=raw["detector_sha256"],  # type: ignore[arg-type]
                    calibration_policy_sha256=raw["calibration_policy_sha256"],  # type: ignore[arg-type]
                    calibration_record_sha256=raw["calibration_record_sha256"],  # type: ignore[arg-type]
                    timing_error_bound_microseconds=raw["timing_error_bound_microseconds"],  # type: ignore[arg-type]
                )
            )
        values_copy = dict(value)
        values_copy["calibrations"] = tuple(calibrations)
        return cls.from_calibrated_values(**values_copy)

    def calibration(self, kind: ProducerKind) -> ProducerCalibrationIdentity:
        return next(item for item in self.calibrations if item.producer_kind == kind)

    def producer_policy_sha256(self, kind: ProducerKind) -> str:
        calibration = self.calibration(kind)
        return calibration.generation_policy_sha256

    def timed_speech_detector_sha256(self, kind: Literal["asr", "vad"]) -> str:
        return canonical_sha256(
            {
                "device": self.speech_device,
                "funasr_version": self.funasr_version,
                "model_id": self.asr_model_id if kind == "asr" else self.vad_model_id,
                "model_revision": self.asr_model_revision
                if kind == "asr"
                else self.vad_model_revision,
                "model_sha256": self.asr_model_sha256 if kind == "asr" else self.vad_model_sha256,
                "producer_kind": kind,
                "provider_id": self.timed_speech_provider_id,
                "provider_version": self.timed_speech_provider_version,
                "service_sha256": self.timed_speech_service_sha256,
                "timed_speech_policy_sha256": self.timed_speech_policy_sha256,
                "torch_version": self.torch_version,
                "word_timing_capability": self.word_timing_capability,
            }
        )

    @staticmethod
    def _milliseconds_to_tick(milliseconds: int, time_base: TimeBase) -> int:
        ticks = milliseconds * time_base.denominator
        divisor = 1000 * time_base.numerator
        return (ticks + divisor - 1) // divisor

    def adaptive_window_policy(self, time_base: TimeBase) -> AdaptiveEvidenceWindowPolicy:
        """Convert explicit wall-clock values outward into one exact video clock."""

        if type(time_base) is not TimeBase:  # noqa: E721
            raise LocalMediaPolicyError("adaptive window time_base must be a TimeBase")
        return AdaptiveEvidenceWindowPolicy(
            strategy_version=self.policy_version,
            time_base=time_base,
            initial_left_expansion_pts=self._milliseconds_to_tick(
                self.initial_left_expansion_milliseconds, time_base
            ),
            initial_right_expansion_pts=self._milliseconds_to_tick(
                self.initial_right_expansion_milliseconds, time_base
            ),
            expansion_step_pts=self._milliseconds_to_tick(
                self.expansion_step_milliseconds, time_base
            ),
            max_expansion_count=self.max_expansion_count,
            boundary_touch_margin_pts=self._milliseconds_to_tick(
                self.boundary_touch_margin_milliseconds, time_base
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "analysis_fps_denominator": self.analysis_fps_denominator,
            "analysis_fps_numerator": self.analysis_fps_numerator,
            "analysis_height": self.analysis_height,
            "analysis_timeout_seconds": self.analysis_timeout_seconds,
            "analysis_width": self.analysis_width,
            "black_luma_max": self.black_luma_max,
            "boundary_touch_margin_milliseconds": self.boundary_touch_margin_milliseconds,
            "calibrations": [item.to_mapping() for item in self.calibrations],
            "frozen_change_ppm_max": self.frozen_change_ppm_max,
            "expansion_step_milliseconds": self.expansion_step_milliseconds,
            "initial_left_expansion_milliseconds": self.initial_left_expansion_milliseconds,
            "initial_right_expansion_milliseconds": self.initial_right_expansion_milliseconds,
            "max_analysis_frames": self.max_analysis_frames,
            "max_expansion_count": self.max_expansion_count,
            "max_stderr_bytes": self.max_stderr_bytes,
            "max_stdout_bytes": self.max_stdout_bytes,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "probe_timeout_seconds": self.probe_timeout_seconds,
            "scene_change_ppm_min": self.scene_change_ppm_min,
            "shot_change_ppm_min": self.shot_change_ppm_min,
            "subtitle_edge_delta_min": self.subtitle_edge_delta_min,
            "subtitle_edge_fraction_ppm_min": self.subtitle_edge_fraction_ppm_min,
            "subtitle_min_consecutive_samples": self.subtitle_min_consecutive_samples,
            "transition_change_ppm_min": self.transition_change_ppm_min,
            "timed_speech_endpoint_url": self.timed_speech_endpoint_url,
            "timed_speech_provider_id": self.timed_speech_provider_id,
            "timed_speech_provider_version": self.timed_speech_provider_version,
            "timed_speech_service_sha256": self.timed_speech_service_sha256,
            "funasr_version": self.funasr_version,
            "torch_version": self.torch_version,
            "speech_device": self.speech_device,
            "word_timing_capability": self.word_timing_capability,
            "asr_model_id": self.asr_model_id,
            "asr_model_revision": self.asr_model_revision,
            "asr_model_sha256": self.asr_model_sha256,
            "vad_model_id": self.vad_model_id,
            "vad_model_revision": self.vad_model_revision,
            "vad_model_sha256": self.vad_model_sha256,
            "timed_speech_policy_sha256": self.timed_speech_policy_sha256,
            "timed_speech_calibration_sha256": self.timed_speech_calibration_sha256,
            "timed_speech_timeout_seconds": self.timed_speech_timeout_seconds,
            "timed_speech_max_response_bytes": self.timed_speech_max_response_bytes,
            "utterance_gap_milliseconds": self.utterance_gap_milliseconds,
            "vad_merge_gap_milliseconds": self.vad_merge_gap_milliseconds,
            "white_luma_min": self.white_luma_min,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    # Unreachable compatibility surface for the removed local producers. The
    # closed policy mapping deliberately contains none of these legacy fields.
    whisper_model_path = Path("/removed/whisper.pt")
    whisper_model_name = "removed"
    whisper_language = "removed"
    whisper_timeout_seconds = 1
    vad_noise_db = -100
    vad_min_silence_milliseconds = 1


@dataclass(frozen=True, slots=True)
class LocalMediaPreflightRequest:
    """Materialized source/endpoints plus the explicitly installed native identity.

    The adapter identity is supplied by controlled runtime composition, never
    derived from the service hash or inferred from an untrusted response.
    """

    source_path: Path
    episode_id: str
    source_id: str
    source_sha256: str
    source_provenance_sha256: str
    source_manifest_sha256: str
    root_input_manifest_sha256: str
    frame_pts_index: FramePtsIndexSet
    audio_sample_boundaries: AudioSampleBoundarySet
    frame_detector_sha256: str
    audio_detector_sha256: str
    policy: LocalMediaPreflightPolicy
    timed_speech_adapter_sha256: str

    def __post_init__(self) -> None:
        if not self.source_path.is_absolute():
            raise LocalMediaSourceError("source_path must be an absolute private materialization")
        for name in ("episode_id", "source_id"):
            if type(getattr(self, name)) is not str or not getattr(self, name).strip():
                raise LocalMediaSourceError(f"{name} must be non-empty text")
        for name in (
            "source_sha256",
            "source_provenance_sha256",
            "source_manifest_sha256",
            "root_input_manifest_sha256",
            "frame_detector_sha256",
            "audio_detector_sha256",
            "timed_speech_adapter_sha256",
        ):
            try:
                sha256_prefixed(getattr(self, name), name)
            except ValueError as error:
                raise LocalMediaSourceError(str(error)) from error
        if (
            type(self.timed_speech_adapter_sha256) is not str
            or self.timed_speech_adapter_sha256 == "sha256:" + "0" * 64
        ):
            raise LocalMediaSourceError("timed_speech_adapter_sha256 must be a nonzero identity")
        for evidence in (self.frame_pts_index, self.audio_sample_boundaries):
            if (
                evidence.context.source_id != self.source_id
                or evidence.context.source_sha256 != self.source_sha256
            ):
                raise LocalMediaSourceError("physical endpoint evidence source identity mismatch")


@dataclass(frozen=True, slots=True)
class RuntimeMediaPreflightRequest:
    """Closed PC-CUDA whole-source request over an existing physical request.

    ``local_request`` remains the authority for the physical detector inputs.
    Its historical CPU/MPS speech profile is intentionally not CUDA authority:
    the accepted runtime policy supplies every ASR/VAD identity instead.
    """

    local_request: LocalMediaPreflightRequest
    runtime_policy: PcCudaRuntimeTimedSpeechPolicy

    def __post_init__(self) -> None:
        # Delayed to keep the policy projection's import of this module acyclic.
        from .runtime_policy import PcCudaRuntimeTimedSpeechPolicy

        if type(self.local_request) is not LocalMediaPreflightRequest:  # noqa: E721
            raise LocalMediaSourceError("runtime media request requires an exact local physical request")
        if type(self.runtime_policy) is not PcCudaRuntimeTimedSpeechPolicy:  # noqa: E721
            raise LocalMediaPolicyError("runtime media request requires exact PC CUDA authority")
        audio = self.local_request.audio_sample_boundaries.context
        if (
            self.local_request.timed_speech_adapter_sha256
            != self.runtime_policy.native_port_identity_sha256
        ):
            raise LocalMediaSourceError(
                "runtime media adapter identity differs from accepted CUDA native port"
            )
        if (
            self.runtime_policy.runtime_capability_id != "pc_cuda"
            or self.runtime_policy.device != "cuda"
            or self.runtime_policy.source_clock_id != audio.clock_id
            or self.runtime_policy.source_time_base != audio.time_base
        ):
            raise LocalMediaPolicyError("runtime CUDA authority does not match source audio clock")


@dataclass(frozen=True, slots=True)
class ToolInvocationTrace:
    producer_kind: str
    executable: str
    executable_sha256: str
    version_evidence_sha256: str
    argv_sha256: str
    stdout_sha256: str
    stderr_sha256: str

    def to_mapping(self) -> dict[str, str]:
        return {
            "argv_sha256": self.argv_sha256,
            "executable": self.executable,
            "executable_sha256": self.executable_sha256,
            "producer_kind": self.producer_kind,
            "stderr_sha256": self.stderr_sha256,
            "stdout_sha256": self.stdout_sha256,
            "version_evidence_sha256": self.version_evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class ToolTrace:
    invocations: tuple[ToolInvocationTrace, ...]

    def __post_init__(self) -> None:
        if not self.invocations:
            raise LocalMediaEvidenceError("tool trace must contain invocations")

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def to_mapping(self) -> list[dict[str, str]]:
        return [item.to_mapping() for item in self.invocations]


@dataclass(frozen=True, slots=True)
class ProducerIdentity:
    producer_kind: ProducerKind
    producer_id: str
    producer_version: str
    producer_policy_sha256: str
    detector_sha256: str
    calibration_policy_sha256: str
    calibration_record_sha256: str
    timing_error_bound_tick: int
    adapter_sha256: str | None

    def __post_init__(self) -> None:
        if self.adapter_sha256 is None:
            if self.producer_kind in {"asr", "vad"}:
                raise LocalMediaEvidenceError("timed speech producer adapter identity is required")
            return
        try:
            sha256_prefixed(self.adapter_sha256, "adapter_sha256")
        except ValueError as error:
            raise LocalMediaEvidenceError(str(error)) from error
        if type(self.adapter_sha256) is not str or self.adapter_sha256 == "sha256:" + "0" * 64:
            raise LocalMediaEvidenceError("adapter_sha256 must be a nonzero identity")

    def to_mapping(self) -> dict[str, object]:
        return {
            "adapter_sha256": self.adapter_sha256,
            "calibration_policy_sha256": self.calibration_policy_sha256,
            "calibration_record_sha256": self.calibration_record_sha256,
            "detector_sha256": self.detector_sha256,
            "producer_id": self.producer_id,
            "producer_kind": self.producer_kind,
            "producer_policy_sha256": self.producer_policy_sha256,
            "producer_version": self.producer_version,
            "timing_error_bound_tick": self.timing_error_bound_tick,
        }


@dataclass(frozen=True, slots=True)
class LocalMediaPreflightResult:
    evidence: RootMediaEvidenceBundle
    tool_trace: ToolTrace
    producer_identities: tuple[ProducerIdentity, ...]
    calibration_bindings: tuple[CalibrationBinding, ...]
    source_provenance_sha256: str

    def __post_init__(self) -> None:
        if tuple(item.producer_kind for item in self.producer_identities) != _PRODUCER_KINDS:
            raise LocalMediaEvidenceError("producer identities are not closed and canonical")
        if len(self.calibration_bindings) != len(_PRODUCER_KINDS):
            raise LocalMediaEvidenceError("calibration bindings are not closed")
        try:
            sha256_prefixed(self.source_provenance_sha256, "source_provenance_sha256")
        except ValueError as error:
            raise LocalMediaEvidenceError(str(error)) from error

    def provenance_mapping(self) -> dict[str, object]:
        return {
            "producer_identities": [item.to_mapping() for item in self.producer_identities],
            "schema_version": "local-media-producer-provenance-v1",
            "source_provenance_sha256": self.source_provenance_sha256,
            "tool_invocations": self.tool_trace.to_mapping(),
            "tool_trace_sha256": self.tool_trace.canonical_hash,
        }

    @property
    def provenance_sha256(self) -> str:
        return canonical_sha256(self.provenance_mapping())


@dataclass(frozen=True, slots=True)
class RuntimeMediaPreflightResult:
    """Complete physical plus accepted-PC-CUDA timed-speech evidence closure."""

    evidence: RootMediaEvidenceBundle
    tool_trace: ToolTrace
    producer_identities: tuple[ProducerIdentity, ...]
    calibration_bindings: tuple[CalibrationBinding, ...]
    source_provenance_sha256: str
    runtime_policy: PcCudaRuntimeTimedSpeechPolicy

    def __post_init__(self) -> None:
        from .runtime_policy import PcCudaRuntimeTimedSpeechPolicy

        if tuple(item.producer_kind for item in self.producer_identities) != _PRODUCER_KINDS:
            raise LocalMediaEvidenceError("runtime producer identities are not closed and canonical")
        if len(self.calibration_bindings) != len(_PRODUCER_KINDS):
            raise LocalMediaEvidenceError("runtime calibration bindings are not closed")
        try:
            sha256_prefixed(self.source_provenance_sha256, "source_provenance_sha256")
        except ValueError as error:
            raise LocalMediaEvidenceError(str(error)) from error
        if type(self.runtime_policy) is not PcCudaRuntimeTimedSpeechPolicy:  # noqa: E721
            raise LocalMediaEvidenceError("runtime result requires exact PC CUDA authority")

        identities = self.producer_identities[2:4]
        bindings = self.calibration_bindings[2:4]
        for identity, binding, expected in zip(
            identities, bindings, self.runtime_policy.producers, strict=True
        ):
            if (
                identity.adapter_sha256 != self.runtime_policy.native_port_identity_sha256
                or binding.adapter_sha256 != self.runtime_policy.native_port_identity_sha256
                or identity.producer_kind != expected.producer_kind
                or identity.producer_id != expected.producer_id
                or identity.producer_version != expected.producer_version
                or identity.producer_policy_sha256 != expected.generation_policy_sha256
                or identity.detector_sha256 != expected.detector_sha256
                or identity.calibration_policy_sha256 != expected.calibration_policy_sha256
                or identity.calibration_record_sha256 != expected.calibration_record_sha256
                or identity.timing_error_bound_tick != expected.timing_error_bound_tick
                or binding.policy_sha256 != expected.generation_policy_sha256
                or binding.detector_sha256 != expected.detector_sha256
                or binding.calibration_record_sha256 != expected.calibration_record_sha256
                or binding.producer_id != expected.producer_id
                or binding.producer_version != expected.producer_version
                or binding.timing_error_bound_tick != expected.timing_error_bound_tick
            ):
                raise LocalMediaEvidenceError(
                    "runtime ASR/VAD calibration, bound, or native adapter identity drift"
                )

    def provenance_mapping(self) -> dict[str, object]:
        return {
            "producer_identities": [item.to_mapping() for item in self.producer_identities],
            "runtime_timed_speech_authority": self.runtime_policy.to_mapping(),
            "schema_version": "runtime-cuda-media-producer-provenance-v2",
            "source_provenance_sha256": self.source_provenance_sha256,
            "tool_invocations": self.tool_trace.to_mapping(),
            "tool_trace_sha256": self.tool_trace.canonical_hash,
        }

    @property
    def provenance_sha256(self) -> str:
        return canonical_sha256(self.provenance_mapping())


__all__ = [
    "LocalMediaEvidenceError",
    "LocalMediaPolicyError",
    "LocalMediaPreflightError",
    "LocalMediaPreflightPolicy",
    "LocalMediaPreflightRequest",
    "LocalMediaPreflightResult",
    "LocalMediaSourceError",
    "LocalMediaToolError",
    "ProducerCalibrationIdentity",
    "ProducerIdentity",
    "ProducerKind",
    "RuntimeMediaPreflightRequest",
    "RuntimeMediaPreflightResult",
    "ToolInvocationTrace",
    "ToolTrace",
]
