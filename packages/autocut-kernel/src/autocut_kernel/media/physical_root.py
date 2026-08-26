"""Isolated physical source-root evidence contract.

The six source-global physical evidence sets — frame PTS index, shot/scene
boundaries, audio sample boundaries, visual validity and subtitle cues — are
held here without any Transcript or VAD. This value proves physical coverage,
clock agreement and frame membership only. It grants no Source, probe or
certificate ownership, no calibration admission, and never fabricates global
silence. The later window Command resolves Source/VLM/candidate ownership and
binds this value by its own hash.
"""

from __future__ import annotations

from dataclasses import dataclass

from .root_evidence import (
    AudioSampleBoundarySet,
    CanonicalEvidence,
    CoverageOutcome,
    FramePtsIndexSet,
    SceneBoundarySet,
    ShotBoundarySet,
    SubtitleCueSet,
    VisualValiditySet,
)
from .types import MediaValidationError, sha256_prefixed


def _require_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise MediaValidationError(f"{field_name} must be a non-empty string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise MediaValidationError(f"{field_name} must be valid UTF-8") from error
    return value


@dataclass(frozen=True, slots=True)
class PhysicalRootMediaEvidence(CanonicalEvidence):
    """Six source-global physical sets; distinct from the eight-set v1 bundle."""

    physical_root_id: str
    source_id: str
    source_sha256: str
    source_manifest_sha256: str
    root_input_manifest_sha256: str
    frame_pts_index: FramePtsIndexSet
    shot_boundaries: ShotBoundarySet
    scene_boundaries: SceneBoundarySet
    audio_sample_boundaries: AudioSampleBoundarySet
    visual_validity: VisualValiditySet
    subtitle_cues: SubtitleCueSet

    def __post_init__(self) -> None:
        _require_text(self.physical_root_id, "physical_root.physical_root_id")
        _require_text(self.source_id, "physical_root.source_id")
        sha256_prefixed(self.source_sha256, "physical_root.source_sha256")
        sha256_prefixed(self.source_manifest_sha256, "physical_root.source_manifest_sha256")
        sha256_prefixed(self.root_input_manifest_sha256, "physical_root.root_input_manifest_sha256")
        typed_sets = (
            (self.frame_pts_index, FramePtsIndexSet, "frame_pts_index"),
            (self.shot_boundaries, ShotBoundarySet, "shot_boundaries"),
            (self.scene_boundaries, SceneBoundarySet, "scene_boundaries"),
            (self.audio_sample_boundaries, AudioSampleBoundarySet, "audio_sample_boundaries"),
            (self.visual_validity, VisualValiditySet, "visual_validity"),
            (self.subtitle_cues, SubtitleCueSet, "subtitle_cues"),
        )
        for value, expected_type, field_name in typed_sets:
            if type(value) is not expected_type:  # noqa: E721
                raise MediaValidationError(
                    f"physical_root.{field_name} has an invalid evidence type"
                )
        for value, _expected_type, field_name in typed_sets:
            if (
                value.context.source_id != self.source_id
                or value.context.source_sha256 != self.source_sha256
            ):
                raise MediaValidationError(
                    f"physical_root.{field_name} does not bind the root source identity"
                )
            if value.coverage.outcome is not CoverageOutcome.COMPLETE:
                raise MediaValidationError(
                    f"physical_root.{field_name} requires complete source-bound coverage"
                )
        video_sets = (
            self.frame_pts_index,
            self.shot_boundaries,
            self.scene_boundaries,
            self.visual_validity,
            self.subtitle_cues,
        )
        video_contexts = tuple(item.context for item in video_sets)
        for context in video_contexts[1:]:
            if (
                context.clock_id,
                context.time_base,
                context.origin_tick,
                context.duration_tick,
            ) != (
                video_contexts[0].clock_id,
                video_contexts[0].time_base,
                video_contexts[0].origin_tick,
                video_contexts[0].duration_tick,
            ):
                raise MediaValidationError(
                    "physical_root video evidence sets must share one source clock"
                )
        # The audio set keeps its own native source clock; unequal A/V tails do
        # not imply a shared presentation map and are therefore never stretched.
        exact_frame_set_hash = self.frame_pts_index.canonical_hash
        for boundary_name, boundary_set in (
            ("shot", self.shot_boundaries),
            ("scene", self.scene_boundaries),
        ):
            if boundary_set.frame_pts_index_set_sha256 != exact_frame_set_hash:
                raise MediaValidationError(
                    f"{boundary_name} boundaries must bind the exact frame PTS index set hash"
                )
            for point in boundary_set.points:
                if not self.frame_pts_index.pts_index.contains(point.tick):
                    raise MediaValidationError(
                        f"{boundary_name} boundary must be a member of the exact frame PTS index"
                    )
