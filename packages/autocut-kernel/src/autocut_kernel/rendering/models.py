"""Closed immutable values for the one-source, one-span render MVP."""

from __future__ import annotations

from dataclasses import dataclass

from ..media.types import TimeBase, canonical_sha256, require_pts, sha256_prefixed


@dataclass(frozen=True, slots=True)
class Recipe:
    """A validated current ``LocalMediaCommand`` one-source recipe.

    ``start_pts`` and ``end_pts`` retain the source-native integer clock.  No
    seconds representation is admitted at this boundary.
    """

    source_sha256: str
    source_byte_size: int
    time_base: TimeBase
    start_pts: int
    end_pts: int
    fixture_id: str
    fixture_mode: str
    canonical_hash: str

    def __post_init__(self) -> None:
        sha256_prefixed(self.source_sha256, "recipe.source.sha256")
        if require_pts(self.source_byte_size, "recipe.source.byte_size") <= 0:
            raise ValueError("recipe.source.byte_size must be positive")
        start = require_pts(self.start_pts, "recipe.span.start_pts")
        end = require_pts(self.end_pts, "recipe.span.end_pts")
        if start >= end:
            raise ValueError("recipe span must satisfy start_pts < end_pts")
        if not self.fixture_id:
            raise ValueError("recipe fixture_id must be a non-empty string")
        if self.fixture_mode != "fixture_ground_truth_v1":
            raise ValueError("recipe fixture_mode is unsupported")
        sha256_prefixed(self.canonical_hash, "recipe.canonical_hash")

    @property
    def duration_pts(self) -> int:
        """Return the exact selected source duration in PTS ticks."""
        return self.end_pts - self.start_pts


@dataclass(frozen=True, slots=True)
class RenderProfile:
    """Fixed video-only H.264 MP4 settings for the reference renderer."""

    profile_id: str
    codec: str = "libx264"
    pixel_format: str = "yuv420p"
    preset: str = "medium"
    crf: int = 18
    keyframe_interval: int = 48
    output_timescale: int = 90_000

    def __post_init__(self) -> None:
        if not self.profile_id:
            raise ValueError("render profile_id must be non-empty")
        if self.codec != "libx264":
            raise ValueError("MVP render profile must use libx264")
        if self.pixel_format != "yuv420p":
            raise ValueError("MVP render profile must use yuv420p")
        if not self.preset:
            raise ValueError("render preset must be non-empty")
        if require_pts(self.crf, "render.crf") < 0:
            raise ValueError("render.crf must be non-negative")
        if require_pts(self.keyframe_interval, "render.keyframe_interval") <= 0:
            raise ValueError("render.keyframe_interval must be positive")
        if require_pts(self.output_timescale, "render.output_timescale") <= 0:
            raise ValueError("render.output_timescale must be positive")

    @property
    def canonical_hash(self) -> str:
        """Return the stable identity of the complete fixed encoder profile."""
        return canonical_sha256(
            {
                "codec": self.codec,
                "crf": self.crf,
                "keyframe_interval": self.keyframe_interval,
                "output_timescale": self.output_timescale,
                "pixel_format": self.pixel_format,
                "preset": self.preset,
                "profile_id": self.profile_id,
                "topology": "video_only",
            }
        )


H264_MP4_VIDEO_PROFILE = RenderProfile(profile_id="h264-mp4-video-v1")


@dataclass(frozen=True, slots=True)
class RenderPlan:
    """A deterministic, shell-free FFmpeg invocation description."""

    recipe_hash: str
    profile_hash: str
    filter_graph: str
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        sha256_prefixed(self.recipe_hash, "render_plan.recipe_hash")
        sha256_prefixed(self.profile_hash, "render_plan.profile_hash")
        if not self.filter_graph:
            raise ValueError("render_plan.filter_graph must be non-empty")
        if not self.argv:
            raise ValueError("render_plan.argv must be non-empty")
        if self.argv[0] != "ffmpeg":
            raise ValueError("render_plan must invoke ffmpeg directly")
        if "-ss" in self.argv or "-to" in self.argv or "-c" in self.argv:
            raise ValueError("render_plan must not seek or stream-copy")
