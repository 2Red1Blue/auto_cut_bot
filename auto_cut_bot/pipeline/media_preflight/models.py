"""Closed request, policy, provenance, and result types for local media preflight."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from autocut_kernel.media import (
    AdaptiveEvidenceWindowPolicy,
    AudioSampleBoundarySet,
    CalibrationBinding,
    FramePtsIndexSet,
    RootMediaEvidenceBundle,
    TimeBase,
)
from autocut_kernel.media.types import canonical_sha256, sha256_prefixed

ProducerKind = Literal[
    "frame", "audio", "asr", "vad", "shot", "scene", "visual", "subtitle"
]
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
    whisper_model_name: str
    whisper_model_path: Path
    whisper_model_sha256: str
    whisper_language: str
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
    whisper_timeout_seconds: int
    vad_noise_db: int
    vad_min_silence_milliseconds: int
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
        _text(self.whisper_model_name, "whisper_model_name")
        _text(self.whisper_language, "whisper_language")
        if not self.whisper_model_path.is_absolute():
            raise LocalMediaPolicyError("whisper_model_path must be absolute")
        try:
            sha256_prefixed(self.whisper_model_sha256, "whisper_model_sha256")
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
            "whisper_timeout_seconds",
            "vad_min_silence_milliseconds",
            "initial_left_expansion_milliseconds",
            "initial_right_expansion_milliseconds",
            "expansion_step_milliseconds",
            "boundary_touch_margin_milliseconds",
            "subtitle_edge_delta_min",
            "subtitle_min_consecutive_samples",
        ):
            _positive(getattr(self, name), f"policy.{name}")
        _non_negative(self.max_expansion_count, "policy.max_expansion_count")
        if not -100 <= self.vad_noise_db <= 0:
            raise LocalMediaPolicyError("policy.vad_noise_db must be in [-100, 0]")
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
            "whisper_model_name",
            "whisper_model_path",
            "whisper_model_sha256",
            "whisper_language",
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
            "whisper_timeout_seconds",
            "vad_noise_db",
            "vad_min_silence_milliseconds",
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
                    timing_error_bound_microseconds=raw[
                        "timing_error_bound_microseconds"
                    ],  # type: ignore[arg-type]
                )
            )
        values_copy = dict(value)
        model_path = values_copy["whisper_model_path"]
        if type(model_path) is not str:  # noqa: E721
            raise LocalMediaPolicyError("whisper_model_path must be text")
        values_copy["whisper_model_path"] = Path(model_path)
        values_copy["calibrations"] = tuple(calibrations)
        return cls.from_calibrated_values(**values_copy)

    def calibration(self, kind: ProducerKind) -> ProducerCalibrationIdentity:
        return next(item for item in self.calibrations if item.producer_kind == kind)

    def producer_policy_sha256(self, kind: ProducerKind) -> str:
        calibration = self.calibration(kind)
        return calibration.generation_policy_sha256

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
            "vad_min_silence_milliseconds": self.vad_min_silence_milliseconds,
            "vad_noise_db": self.vad_noise_db,
            "whisper_language": self.whisper_language,
            "whisper_model_name": self.whisper_model_name,
            "whisper_model_path": str(self.whisper_model_path),
            "whisper_model_sha256": self.whisper_model_sha256,
            "whisper_timeout_seconds": self.whisper_timeout_seconds,
            "white_luma_min": self.white_luma_min,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class LocalMediaPreflightRequest:
    """An immutable materialized source plus already committed physical endpoints."""

    source_path: Path
    episode_id: str
    source_id: str
    source_sha256: str
    source_provenance_sha256: str
    source_manifest_sha256: str
    root_input_manifest_sha256: str
    frame_pts_index: FramePtsIndexSet
    audio_sample_boundaries: AudioSampleBoundarySet
    policy: LocalMediaPreflightPolicy

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
        ):
            try:
                sha256_prefixed(getattr(self, name), name)
            except ValueError as error:
                raise LocalMediaSourceError(str(error)) from error
        for evidence in (self.frame_pts_index, self.audio_sample_boundaries):
            if (
                evidence.context.source_id != self.source_id
                or evidence.context.source_sha256 != self.source_sha256
            ):
                raise LocalMediaSourceError("physical endpoint evidence source identity mismatch")


@dataclass(frozen=True, slots=True)
class ToolInvocationTrace:
    producer_kind: str
    executable: str
    executable_sha256: str
    version_evidence_sha256: str
    argv_sha256: str
    stdout_sha256: str
    stderr_sha256: str


@dataclass(frozen=True, slots=True)
class ToolTrace:
    invocations: tuple[ToolInvocationTrace, ...]

    def __post_init__(self) -> None:
        if not self.invocations:
            raise LocalMediaEvidenceError("tool trace must contain invocations")

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(
            [
                {
                    "argv_sha256": item.argv_sha256,
                    "executable": item.executable,
                    "executable_sha256": item.executable_sha256,
                    "producer_kind": item.producer_kind,
                    "stderr_sha256": item.stderr_sha256,
                    "stdout_sha256": item.stdout_sha256,
                    "version_evidence_sha256": item.version_evidence_sha256,
                }
                for item in self.invocations
            ]
        )


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
    "ToolInvocationTrace",
    "ToolTrace",
]
