"""Pure construction of the reference FFmpeg argv for one exact PTS span."""

from __future__ import annotations

from pathlib import Path

from .models import H264_MP4_VIDEO_PROFILE, Recipe, RenderPlan, RenderProfile


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
        str(profile.output_timescale),
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output_path),
    )
    return RenderPlan(recipe.canonical_hash, profile.canonical_hash, filter_graph, argv)
