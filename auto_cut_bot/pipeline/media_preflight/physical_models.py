"""Speech-free physical media prelude policy/request/result.

These values bound only the six physical producers (frame/audio/shot/scene/
visual/subtitle) and hold no ASR/VAD model, adapter, endpoint, profile, or
candidate-expansion policy. A ``PhysicalMediaRequest`` never requires speech
adapter/model calibration, so a physical prelude can run with no speech
endpoint configured. It grants no Source, probe or certificate ownership and no
accepted speech profile; the later physical prelude Command binds this value by
its own hash.
"""

from __future__ import annotations

from dataclasses import dataclass

from autocut_kernel.media import (
    AudioSampleBoundarySet,
    CalibrationBinding,
    FramePtsIndexSet,
    SceneBoundarySet,
    ShotBoundarySet,
    SubtitleCueSet,
    VisualValiditySet,
)
from autocut_kernel.media.types import canonical_sha256, sha256_prefixed

from .models import (
    LocalMediaEvidenceError,
    LocalMediaPolicyError,
    LocalMediaSourceError,
    ProducerCalibrationIdentity,
    ProducerIdentity,
    ProducerKind,
    ToolTrace,
)

_PHYSICAL_KINDS: tuple[ProducerKind, ...] = (
    "frame",
    "audio",
    "shot",
    "scene",
    "visual",
    "subtitle",
)

_PHYSICAL_KIND_SET = frozenset(_PHYSICAL_KINDS)


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


def _ppm(value: object, name: str) -> int:
    value = _non_negative(value, name)
    if not 0 <= value <= 1_000_000:
        raise LocalMediaPolicyError(f"{name} must be a ppm value in [0, 1000000]")
    return value


def _sha(value: object, name: str) -> str:
    try:
        return sha256_prefixed(value, name)
    except ValueError as error:
        raise LocalMediaPolicyError(str(error)) from error


@dataclass(frozen=True, slots=True)
class PhysicalMediaPolicy:
    """Closed physical detector policy; no speech, adapters or expansion knobs."""

    policy_id: str
    policy_version: str
    analysis_fps_numerator: int
    analysis_fps_denominator: int
    analysis_width: int
    analysis_height: int
    max_analysis_frames: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    probe_timeout_seconds: int
    analysis_timeout_seconds: int
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
            "analysis_fps_numerator",
            "analysis_fps_denominator",
            "analysis_width",
            "analysis_height",
            "max_analysis_frames",
            "max_stdout_bytes",
            "max_stderr_bytes",
            "probe_timeout_seconds",
            "analysis_timeout_seconds",
            "subtitle_edge_delta_min",
            "subtitle_min_consecutive_samples",
        ):
            _positive(getattr(self, name), f"policy.{name}")
        if self.analysis_width < 2:
            raise LocalMediaPolicyError("policy.analysis_width must be at least two pixels")
        if (
            type(self.black_luma_max) is not int  # noqa: E721
            or type(self.white_luma_min) is not int  # noqa: E721
            or not 0 <= self.black_luma_max < self.white_luma_min <= 255
        ):
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
        if type(self.calibrations) is not tuple or any(  # noqa: E721
            type(item) is not ProducerCalibrationIdentity for item in self.calibrations
        ):
            raise LocalMediaPolicyError("physical calibrations must be exact typed values")
        if tuple(item.producer_kind for item in self.calibrations) != _PHYSICAL_KINDS:
            raise LocalMediaPolicyError(
                "physical calibrations must contain frame/audio/shot/scene/visual/subtitle in order"
            )

    def calibration(self, kind: ProducerKind) -> ProducerCalibrationIdentity:
        return next(item for item in self.calibrations if item.producer_kind == kind)

    def producer_policy_sha256(self, kind: ProducerKind) -> str:
        if kind not in _PHYSICAL_KIND_SET:
            raise LocalMediaPolicyError(f"{kind} is not a physical producer kind")
        return self.calibration(kind).generation_policy_sha256

    def to_mapping(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "analysis_fps_numerator": self.analysis_fps_numerator,
            "analysis_fps_denominator": self.analysis_fps_denominator,
            "analysis_width": self.analysis_width,
            "analysis_height": self.analysis_height,
            "max_analysis_frames": self.max_analysis_frames,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "probe_timeout_seconds": self.probe_timeout_seconds,
            "analysis_timeout_seconds": self.analysis_timeout_seconds,
            "black_luma_max": self.black_luma_max,
            "white_luma_min": self.white_luma_min,
            "frozen_change_ppm_max": self.frozen_change_ppm_max,
            "transition_change_ppm_min": self.transition_change_ppm_min,
            "shot_change_ppm_min": self.shot_change_ppm_min,
            "scene_change_ppm_min": self.scene_change_ppm_min,
            "subtitle_edge_delta_min": self.subtitle_edge_delta_min,
            "subtitle_edge_fraction_ppm_min": self.subtitle_edge_fraction_ppm_min,
            "subtitle_min_consecutive_samples": self.subtitle_min_consecutive_samples,
            "calibrations": [item.to_mapping() for item in self.calibrations],
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class PhysicalMediaRequest:
    """Materialized source plus only the installed physical native identity.

    No timed speech adapter/hash, endpoint or model identity is required; a
    physically-only prelude therefore needs no speech configuration.
    """

    source_path: str
    episode_id: str
    source_id: str
    source_sha256: str
    source_provenance_sha256: str
    source_manifest_sha256: str
    root_input_manifest_sha256: str
    physical_root_id: str
    frame_pts_index: FramePtsIndexSet
    audio_sample_boundaries: AudioSampleBoundarySet
    frame_detector_sha256: str
    audio_detector_sha256: str
    policy: PhysicalMediaPolicy

    def __post_init__(self) -> None:
        if type(self.source_path) is not str or not self.source_path.strip():  # noqa: E721
            raise LocalMediaSourceError("source_path must be an absolute private materialization")
        for name in ("episode_id", "source_id", "physical_root_id"):
            if type(getattr(self, name)) is not str or not getattr(self, name).strip():  # noqa: E721
                raise LocalMediaSourceError(f"{name} must be non-empty text")
        for name in (
            "source_sha256",
            "source_provenance_sha256",
            "source_manifest_sha256",
            "root_input_manifest_sha256",
            "frame_detector_sha256",
            "audio_detector_sha256",
        ):
            try:
                sha256_prefixed(getattr(self, name), name)
            except ValueError as error:
                raise LocalMediaSourceError(str(error)) from error
        if type(self.frame_pts_index) is not FramePtsIndexSet:  # noqa: E721
            raise LocalMediaSourceError("frame_pts_index must be a FramePtsIndexSet")
        if type(self.audio_sample_boundaries) is not AudioSampleBoundarySet:  # noqa: E721
            raise LocalMediaSourceError("audio_sample_boundaries must be an AudioSampleBoundarySet")
        if type(self.policy) is not PhysicalMediaPolicy:  # noqa: E721
            raise LocalMediaSourceError("physical policy must be a PhysicalMediaPolicy")
        for evidence in (self.frame_pts_index, self.audio_sample_boundaries):
            if (
                evidence.context.source_id != self.source_id
                or evidence.context.source_sha256 != self.source_sha256
            ):
                raise LocalMediaSourceError("physical endpoint evidence source identity mismatch")


@dataclass(frozen=True, slots=True)
class PhysicalMediaResult:
    """Six physical evidence sets and producer facts; no speech, no bundle."""

    frame_pts_index: FramePtsIndexSet
    shot_boundaries: ShotBoundarySet
    scene_boundaries: SceneBoundarySet
    audio_sample_boundaries: AudioSampleBoundarySet
    visual_validity: VisualValiditySet
    subtitle_cues: SubtitleCueSet
    tool_trace: ToolTrace
    producer_identities: tuple[ProducerIdentity, ...]
    calibration_bindings: tuple[CalibrationBinding, ...]
    source_provenance_sha256: str

    def __post_init__(self) -> None:
        for value, expected, name in (
            (self.frame_pts_index, FramePtsIndexSet, "frame_pts_index"),
            (self.shot_boundaries, ShotBoundarySet, "shot_boundaries"),
            (self.scene_boundaries, SceneBoundarySet, "scene_boundaries"),
            (self.audio_sample_boundaries, AudioSampleBoundarySet, "audio_sample_boundaries"),
            (self.visual_validity, VisualValiditySet, "visual_validity"),
            (self.subtitle_cues, SubtitleCueSet, "subtitle_cues"),
            (self.tool_trace, ToolTrace, "tool_trace"),
        ):
            if type(value) is not expected:  # noqa: E721
                raise LocalMediaEvidenceError(f"{name} is not an exact physical evidence value")
        if type(self.producer_identities) is not tuple or any(  # noqa: E721
            type(item) is not ProducerIdentity for item in self.producer_identities
        ):
            raise LocalMediaEvidenceError("physical producer identities must be exact typed values")
        if tuple(item.producer_kind for item in self.producer_identities) != _PHYSICAL_KINDS:
            raise LocalMediaEvidenceError(
                "physical producer identities must be frame/audio/shot/scene/visual/subtitle"
            )
        if (
            type(self.calibration_bindings) is not tuple  # noqa: E721
            or len(self.calibration_bindings) != len(_PHYSICAL_KINDS)
            or any(type(item) is not CalibrationBinding for item in self.calibration_bindings)
        ):
            raise LocalMediaEvidenceError("physical calibration bindings are not closed")
        try:
            sha256_prefixed(self.source_provenance_sha256, "source_provenance_sha256")
        except ValueError as error:
            raise LocalMediaEvidenceError(str(error)) from error

    def provenance_mapping(self) -> dict[str, object]:
        return {
            "producer_identities": [item.to_mapping() for item in self.producer_identities],
            "schema_version": "local-physical-producer-provenance-v1",
            "source_provenance_sha256": self.source_provenance_sha256,
            "tool_invocations": self.tool_trace.to_mapping(),
            "tool_trace_sha256": self.tool_trace.canonical_hash,
        }

    @property
    def tool_trace_sha256(self) -> str:
        return self.tool_trace.canonical_hash


__all__ = [
    "PhysicalMediaPolicy",
    "PhysicalMediaRequest",
    "PhysicalMediaResult",
    "_PHYSICAL_KINDS",
    "_text",
    "_positive",
    "_non_negative",
    "_ppm",
    "_sha",
]
