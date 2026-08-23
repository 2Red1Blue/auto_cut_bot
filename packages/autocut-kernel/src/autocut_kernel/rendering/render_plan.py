"""Pure construction of the reference FFmpeg argv for one exact PTS span."""

from __future__ import annotations

from math import lcm
from pathlib import Path

from .models import H264_MP4_VIDEO_PROFILE, Recipe, RenderPlan, RenderProfile


def _output_timescale(recipe: Recipe, profile: RenderProfile) -> int:
    """Choose an MP4 clock that exactly represents every source PTS tick."""
    timescale = lcm(profile.output_timescale, recipe.time_base.denominator)
    if timescale > profile.max_output_timescale:
        raise ValueError("source time base cannot be represented by the configured MP4 timescale")
    return timescale


def build_render_plan(
    recipe: Recipe,
    *,
    source_path: Path,
    output_path: Path,
    profile: RenderProfile = H264_MP4_VIDEO_PROFILE,
) -> RenderPlan:
    """Build the fixed video-only H.264 MP4 plan without performing I/O.

    The graph uses FFmpeg's integer ``trim`` PTS options directly.  It never
    uses input/output seeking, float second expressions, or stream copy.
    """
    output_timescale = _output_timescale(recipe, profile)
    filter_graph = (
        f"[0:v:0]trim=start_pts={recipe.start_pts}:end_pts={recipe.end_pts},"
        "setpts=PTS-STARTPTS[v0]"
    )
    argv = (
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-copyts",
        "-i",
        str(source_path),
        "-filter_complex",
        filter_graph,
        "-map",
        "[v0]",
        "-an",
        "-c:v",
        profile.codec,
        "-pix_fmt",
        profile.pixel_format,
        "-preset",
        profile.preset,
        "-crf",
        str(profile.crf),
        "-g",
        str(profile.keyframe_interval),
        "-video_track_timescale",
        str(output_timescale),
        "-avoid_negative_ts",
        "disabled",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output_path),
    )
    return RenderPlan(recipe.canonical_hash, profile.canonical_hash, output_timescale, filter_graph, argv)
