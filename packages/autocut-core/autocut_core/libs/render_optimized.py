"""FFmpeg render optimization — 1 encode + N stream copies.

Replaces the per-clip encoding loop (render_clip × N) with:
  1. render_master() — 1 full-quality encode with ALL I-frames (-g 1)
  2. stream_copy_clip() — frame-precise cut via stream copy (-c copy)
  3. concat — lossless join of all segments

This reduces ~79 H.264 encodes to 1 encode + 79 stream copies (~50ms each),
cutting total render time from ~13 minutes to ~1 minute.

Design doc: docs/design/ffmpeg-render-optimization.md
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any


def _sha256_file(path: Path) -> str:
    """SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Default render profile ───────────────────────────────────────────────────

DEFAULT_PROFILE: dict[str, Any] = {
    "name": "delivery",
    "width": 1080,
    "height": 1920,
    "fps": 25,
    "fit": "contain",
    "video_codec": "libx264",
    "video_crf": 18,
    "video_preset": "medium",
    "pixel_format": "yuv420p",
    "audio_codec": "aac",
    "audio_bitrate_kbps": 192,
    "audio_sample_rate": 48000,
    "audio_channels": 2,
}

# Master file uses CRF 2 points lower for quality headroom
MASTER_CRF_OFFSET = 2


# ── Helpers ──────────────────────────────────────────────────────────────────


def _run(cmd: list[str]) -> None:
    """Run a command, raise on failure with stderr context."""
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip()[-500:]
        raise RuntimeError(
            f"ffmpeg failed (exit {result.returncode}): "
            f"{' '.join(cmd[:8])}...\n{stderr_tail}"
        )


def _ffprobe(path: Path, ffprobe: str = "ffprobe") -> dict[str, Any]:
    """Probe media file metadata."""
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=duration,size:stream=index,codec_type,codec_name,"
         "width,height,r_frame_rate,sample_rate,channels",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {path}")
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not isinstance(video, dict):
        raise RuntimeError(f"No video stream: {path}")
    rate = str(video.get("r_frame_rate", "0/1"))
    try:
        fps = float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "duration_seconds": round(float(payload.get("format", {}).get("duration", 0) or 0), 3),
        "size_bytes": int(payload.get("format", {}).get("size", 0) or path.stat().st_size),
        "width": int(video.get("width", 0) or 0),
        "height": int(video.get("height", 0) or 0),
        "fps": round(fps, 3),
        "video_codec": str(video.get("codec_name", "")),
        "audio_codec": str(audio.get("codec_name", "")) if audio else "",
        "audio_sample_rate": int(audio.get("sample_rate", 0) or 0) if audio else 0,
        "audio_channels": int(audio.get("channels", 0) or 0) if audio else 0,
        "has_audio": audio is not None,
    }


def _cache_valid(path: Path, profile: dict[str, Any], duration: float,
                 ffprobe: str = "ffprobe") -> bool:
    """Check if a cached render output is still valid."""
    if not path.is_file():
        return False
    try:
        obs = _ffprobe(path, ffprobe)
    except (OSError, RuntimeError, ValueError):
        return False
    return (
        obs["width"] == profile["width"]
        and obs["height"] == profile["height"]
        and abs(obs["fps"] - profile["fps"]) <= 0.05
        and obs["has_audio"]
        and abs(obs["duration_seconds"] - duration) <= 0.15
    )




def _validate_recipe(recipe: dict[str, Any]) -> None:
    """Validate recipe structure before any expensive work begins.

    Raises ValueError with a descriptive message if the recipe references
    missing clips, transitions, sources, or has malformed timeline entries.
    """
    errors: list[str] = []

    # Required top-level keys
    for key in ("sources", "clips", "transitions", "timeline", "output_filename"):
        if key not in recipe:
            errors.append(f"missing required key: '{key}'")
    if errors:
        raise ValueError("Recipe validation failed: " + "; ".join(errors))

    source_ids = {s.get("source_id") for s in recipe["sources"] if isinstance(s, dict)}
    clip_ids = {c.get("id") for c in recipe["clips"] if isinstance(c, dict)}
    trans_ids = {t.get("id") for t in recipe["transitions"] if isinstance(t, dict)}

    # Check transitions reference valid clips
    for t in recipe["transitions"]:
        if not isinstance(t, dict):
            errors.append(f"transition entry is not a dict: {t!r}")
            continue
        tid = t.get("id", "?")
        if t.get("from_clip_id") not in clip_ids:
            errors.append(f"transition '{tid}' references missing from_clip_id '{t.get('from_clip_id')}'")
        if t.get("to_clip_id") not in clip_ids:
            errors.append(f"transition '{tid}' references missing to_clip_id '{t.get('to_clip_id')}'")

    # Check clips reference valid sources
    for c in recipe["clips"]:
        if not isinstance(c, dict):
            errors.append(f"clip entry is not a dict: {c!r}")
            continue
        cid = c.get("id", "?")
        if c.get("source_id") not in source_ids:
            errors.append(f"clip '{cid}' references missing source_id '{c.get('source_id')}'")

    # Check timeline entries
    for idx, item in enumerate(recipe["timeline"]):
        if not isinstance(item, dict):
            errors.append(f"timeline[{idx}] is not a dict: {item!r}")
            continue
        kind = item.get("kind")
        ref = item.get("ref_id")
        if kind == "clip":
            if ref not in clip_ids:
                errors.append(f"timeline[{idx}] references missing clip '{ref}'")
        elif kind == "transition":
            if ref not in trans_ids:
                errors.append(f"timeline[{idx}] references missing transition '{ref}'")
        else:
            errors.append(f"timeline[{idx}] has unknown kind '{kind}' (expected 'clip' or 'transition')")

    # Check source files exist
    for s in recipe["sources"]:
        if not isinstance(s, dict):
            continue
        sp = s.get("path")
        if sp and not Path(sp).expanduser().is_file():
            errors.append(f"source '{s.get('source_id')}' file not found: {sp}")

    if errors:
        raise ValueError(
            "Recipe validation failed (" + str(len(errors)) + " issues):\n  - "
            + "\n  - ".join(errors)
        )


# ── Video / Audio filter builders ────────────────────────────────────────────


def _build_geometry_filters(profile: dict[str, Any]) -> list[str]:
    """Build scale + crop/pad + fps filters for the master encode."""
    w, h = profile["width"], profile["height"]
    if profile.get("fit") == "cover":
        return [
            f"scale={w}:{h}:force_original_aspect_ratio=increase",
            f"crop={w}:{h}",
            "setsar=1",
            f"fps={profile['fps']}",
        ]
    else:
        return [
            f"scale={w}:{h}:force_original_aspect_ratio=decrease",
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black",
            "setsar=1",
            f"fps={profile['fps']}",
        ]


def _build_master_vf(
    profile: dict[str, Any],
    *,
    subtitle_path: str | None = None,
    fades: list[dict[str, Any]] | None = None,
) -> str:
    """Build the complete video filter chain for master encoding.

    Includes geometry normalization, optional subtitle burn-in, and
    pre-computed fade effects baked into the master file.
    """
    parts = _build_geometry_filters(profile)

    if subtitle_path:
        escaped = (subtitle_path
            .replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "\\'")
            .replace(",", "\\,"))
        parts.append(f"subtitles={escaped}")

    for fade in sorted(fades or [], key=lambda f: f["time"]):
        if fade["direction"] == "in":
            parts.append(
                f"fade=t=in:st={fade['time']:.6f}:d={fade['duration']:.6f}:color=black"
            )
        else:
            parts.append(
                f"fade=t=out:st={fade['time']:.6f}:d={fade['duration']:.6f}:color=black"
            )

    return ",".join(parts)


# ── Phase 1: Master encode ──────────────────────────────────────────────────


def render_master(
    source_path: str | Path,
    destination: str | Path,
    *,
    profile: dict[str, Any] | None = None,
    subtitle_path: str | None = None,
    fades: list[dict[str, Any]] | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    overwrite: bool = False,
) -> Path:
    """Phase 1: Encode source to all-I-frame master file.

    Master file characteristics:
    - Every frame is an I-frame (-g 1) → arbitrary position stream copy
    - Resolution/fps normalized to profile
    - Subtitles burned in (if provided)
    - Fade effects pre-baked (if provided)
    - CRF lower than delivery (quality headroom for any re-encode)

    Returns the master file path.
    """
    profile = profile or DEFAULT_PROFILE
    destination = Path(destination)
    source_path = Path(source_path).expanduser().resolve()

    cache_key_file = destination.with_suffix(".cache_key")

    # Skip expensive SHA-256 of source file when overwrite is forced
    if not overwrite and destination.is_file() and cache_key_file.is_file():
        source_sha = _sha256_file(source_path)
        profile_key = hashlib.sha256(
            json.dumps(profile, sort_keys=True).encode()
        ).hexdigest()[:16]
        subtitle_key = _sha256_file(Path(subtitle_path)) if subtitle_path else "nosub"
        fades_key = hashlib.sha256(
            json.dumps(fades or [], sort_keys=True).encode()
        ).hexdigest()[:16]
        expected_key = f"{source_sha[:16]}_{profile_key}_{subtitle_key}_{fades_key}"
        if cache_key_file.read_text().strip() == expected_key:
            # Validate the cached file is actually valid media
            try:
                _ffprobe(destination, ffprobe)
                return destination
            except (OSError, RuntimeError):
                pass  # Corrupt — re-encode

    destination.parent.mkdir(parents=True, exist_ok=True)

    master_crf = max(14, profile["video_crf"] - MASTER_CRF_OFFSET)
    vf = _build_master_vf(profile, subtitle_path=subtitle_path, fades=fades)

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source_path),
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-vf", vf,
        "-c:v", str(profile["video_codec"]),
        "-preset", str(profile["video_preset"]),
        "-crf", str(master_crf),
        "-g", "1",  # All I-frames — key to the optimization
        "-pix_fmt", str(profile["pixel_format"]),
        "-c:a", str(profile["audio_codec"]),
        "-b:a", f"{max(256, profile['audio_bitrate_kbps'])}k",
        "-ar", str(profile["audio_sample_rate"]),
        "-ac", str(profile["audio_channels"]),
        "-movflags", "+faststart",
        str(destination),
    ]
    _run(cmd)

    # Write cache key after successful encode
    source_sha = _sha256_file(source_path)
    profile_key = hashlib.sha256(
        json.dumps(profile, sort_keys=True).encode()
    ).hexdigest()[:16]
    subtitle_key = _sha256_file(Path(subtitle_path)) if subtitle_path else "nosub"
    fades_key = hashlib.sha256(
        json.dumps(fades or [], sort_keys=True).encode()
    ).hexdigest()[:16]
    expected_key = f"{source_sha[:16]}_{profile_key}_{subtitle_key}_{fades_key}"
    cache_key_file.write_text(expected_key)
    return destination


# ── Phase 2: Stream copy clip ────────────────────────────────────────────────


def stream_copy_clip(
    master_path: str | Path,
    destination: str | Path,
    *,
    source_start: float,
    duration: float,
    profile: dict[str, Any] | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    overwrite: bool = False,
) -> Path:
    """Phase 2: Stream-copy a clip from the master file.

    Because the master is all-I-frame (-g 1), -c copy can cut at
    ANY frame position with frame-precise accuracy. No re-encoding
    needed — pure I/O, ~50ms per clip.

    Args:
        master_path: Path to the all-I-frame master file.
        destination: Output path for the clip.
        source_start: Start time in seconds (from the source timeline).
        duration: Duration in seconds.
        profile: Render profile (for cache validation).
        ffmpeg: Path to ffmpeg.
        ffprobe: Path to ffprobe.
        overwrite: Force re-render even if cache is valid.

    Returns:
        The destination path.
    """
    profile = profile or DEFAULT_PROFILE
    destination = Path(destination)
    master_path = Path(master_path)

    if not overwrite and _cache_valid(destination, profile, duration, ffprobe):
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{source_start:.6f}",
        "-t", f"{duration:.6f}",
        "-i", str(master_path),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        str(destination),
    ]
    _run(cmd)

    if not _cache_valid(destination, profile, duration, ffprobe):
        raise RuntimeError(f"Stream-copied clip failed validation: {destination}")

    return destination


# ── Phase 3: Black separator ─────────────────────────────────────────────────


def render_black_separator(
    destination: str | Path,
    *,
    duration: float,
    profile: dict[str, Any] | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    overwrite: bool = False,
) -> Path:
    """Generate a silent black video segment for transitions."""
    profile = profile or DEFAULT_PROFILE
    destination = Path(destination)

    if not overwrite and _cache_valid(destination, profile, duration, ffprobe):
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"color=c=black:s={profile['width']}x{profile['height']}:"
              f"d={duration:.6f}:r={profile['fps']}",
        "-f", "lavfi",
        "-i", f"anullsrc=channel_layout=stereo:sample_rate={profile['audio_sample_rate']}",
        "-c:v", str(profile["video_codec"]),
        "-preset", str(profile["video_preset"]),
        "-crf", str(max(14, profile["video_crf"] - MASTER_CRF_OFFSET)),
        "-pix_fmt", str(profile["pixel_format"]),
        "-c:a", str(profile["audio_codec"]),
        "-b:a", f"{profile['audio_bitrate_kbps']}k",
        "-ar", str(profile["audio_sample_rate"]),
        "-ac", str(profile["audio_channels"]),
        "-shortest",
        "-movflags", "+faststart",
        str(destination),
    ]
    _run(cmd)
    return destination


# ── Phase 4: Concat ──────────────────────────────────────────────────────────


def concat_clips(
    clip_paths: list[Path],
    output_path: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    overwrite: bool = False,
) -> Path:
    """Concatenate clips losslessly via concat demuxer + stream copy."""
    output_path = Path(output_path)
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Output exists: {output_path} (use overwrite=True)")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    concat_file = output_path.parent / f".concat_{output_path.stem}.txt"

    # Quote file paths for concat demuxer
    concat_file.write_text(
        "".join(f"file '{_concat_quote(p)}'\n" for p in clip_paths),
        encoding="utf-8",
    )

    tmp = output_path.parent / f".{output_path.stem}-{os.getpid()}.tmp.mp4"
    if tmp.exists():
        tmp.unlink()

    try:
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            "-movflags", "+faststart",
            "-avoid_negative_ts", "make_zero",
            str(tmp),
        ]
        _run(cmd)

        # Validate
        obs = _ffprobe(tmp, ffprobe)
        if not obs["has_audio"]:
            raise RuntimeError("Final render has no audio stream")

        os.replace(tmp, output_path)
    finally:
        if tmp.exists():
            tmp.unlink()
        concat_file.unlink(missing_ok=True)

    return output_path


def _concat_quote(path: Path) -> str:
    """Escape single quotes for ffmpeg concat demuxer."""
    return str(path).replace("'", "'\\''")


# ── High-level orchestration ─────────────────────────────────────────────────


def render_optimized(
    recipe: dict[str, Any],
    output_dir: str | Path,
    cache_dir: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    overwrite: bool = False,
    subtitle_path: str | None = None,
) -> dict[str, Any]:
    """Full optimized render pipeline for a single recipe.

    Args:
        recipe: Validated render recipe dict.
        output_dir: Directory for final output.
        cache_dir: Directory for intermediate files (master, clips, transitions).
        ffmpeg: Path to ffmpeg.
        ffprobe: Path to ffprobe.
        overwrite: Skip cache validation and re-render everything.
        subtitle_path: Optional ASS/SRT subtitle file to burn into master.

    Returns:
        Output metadata dict with path, sha256, duration, etc.
    """
    _validate_recipe(recipe)

    profile = recipe.get("render_profile", DEFAULT_PROFILE)
    sources = {s["source_id"]: s for s in recipe["sources"]}
    clips = {c["id"]: c for c in recipe["clips"]}
    transitions = {t["id"]: t for t in recipe["transitions"]}

    cache_dir = Path(cache_dir)
    output_dir = Path(output_dir)

    # Collect fade events per source
    fades_by_source: dict[str, list[dict[str, Any]]] = {}
    for t in transitions.values():
        if t["type"] != "black_separator":
            continue
        from_clip = clips.get(t["from_clip_id"])
        to_clip = clips.get(t["to_clip_id"])
        if from_clip is None or to_clip is None:
            continue  # _validate_recipe already caught this; guard against partial data
        fades_by_source.setdefault(from_clip["source_id"], []).append({
            "direction": "out",
            "time": max(0.0, float(from_clip["source_end"]) - float(t["fade_out_seconds"])),
            "duration": float(t["fade_out_seconds"]),
        })
        fades_by_source.setdefault(to_clip["source_id"], []).append({
            "direction": "in",
            "time": float(to_clip["source_start"]),
            "duration": float(t["fade_in_seconds"]),
        })

    # Phase 1: Generate master files per source
    masters: dict[str, Path] = {}
    for source_id, source in sources.items():
        master_path = cache_dir / "masters" / f"{source_id}.mp4"
        render_master(
            source_path=source["path"],
            destination=master_path,
            profile=profile,
            subtitle_path=subtitle_path,
            fades=fades_by_source.get(source_id),
            ffmpeg=ffmpeg, ffprobe=ffprobe,
            overwrite=overwrite,
        )
        masters[source_id] = master_path

    # Phase 2: Stream copy each clip
    timeline_media: list[Path] = []
    for item in recipe["timeline"]:
        if item["kind"] == "clip":
            clip = clips[item["ref_id"]]
            media_path = cache_dir / "clips" / f"{clip['id']}.mp4"
            stream_copy_clip(
                master_path=masters[clip["source_id"]],
                destination=media_path,
                source_start=float(clip["source_start"]),
                duration=float(clip["duration_seconds"]),
                profile=profile,
                ffmpeg=ffmpeg, ffprobe=ffprobe,
                overwrite=overwrite,
            )
        else:
            transition = transitions[item["ref_id"]]
            media_path = cache_dir / "transitions" / f"{transition['id']}.mp4"
            render_black_separator(
                destination=media_path,
                duration=float(transition["duration_seconds"]),
                profile=profile,
                ffmpeg=ffmpeg, ffprobe=ffprobe,
                overwrite=overwrite,
            )
        timeline_media.append(media_path)

    # Phase 3: Concat
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / recipe["output_filename"]
    concat_clips(timeline_media, output_path, ffmpeg=ffmpeg, ffprobe=ffprobe,
                 overwrite=overwrite)

    # Validate and return metadata
    obs = _ffprobe(output_path, ffprobe)
    return {
        "story_id": recipe["story_id"],
        "title": recipe["title"],
        "production_slot": recipe["production_slot"],
        "path": str(output_path),
        "sha256": _sha256_file(output_path),
        "duration_seconds": obs["duration_seconds"],
        "size_bytes": obs["size_bytes"],
        "width": obs["width"],
        "height": obs["height"],
        "fps": obs["fps"],
        "video_codec": obs["video_codec"],
        "audio_codec": obs["audio_codec"],
        "audio_sample_rate": obs["audio_sample_rate"],
        "audio_channels": obs["audio_channels"],
    }


__all__ = [
    "DEFAULT_PROFILE",
    "render_master",
    "stream_copy_clip",
    "render_black_separator",
    "concat_clips",
    "render_optimized",
]