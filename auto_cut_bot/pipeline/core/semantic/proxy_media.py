#!/usr/bin/env python3
"""Shared low-bitrate proxy rendering for local Story comparisons.

The module is internal: it has no CLI and creates only rebuildable cache
artifacts.  Formal Plan/QC/Render outputs remain owned by their existing
stages.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from autocut_core.io import (
    atomic_write_json,
    atomic_write_text,
    json_sha256,
    load_json,
    sha256_file,
)


PROXY_WIDTH = 360
PROXY_HEIGHT = 640
PROXY_FPS = 12.0
PROXY_VIDEO_BITRATE_KBPS = 180
PROXY_AUDIO_BITRATE_KBPS = 48
MAXIMUM_LOCAL_SECONDS_PER_FINALIST = 200.0
# DashScope rejects an individual JSON string above 28,000,000 characters.
# The inline ``data:video/mp4;base64,...`` value is one such string, so keep
# the binary proxy below 20 MB.  At the fixed 180k/48k proxy bitrates, three
# 200-second finalists remain below this limit with container overhead while
# retaining enough detail for local finalist comparison.
MAXIMUM_INLINE_PROXY_BYTES = 20_000_000


class FinalistProxyUnavailable(RuntimeError):
    pass


def ensure_inline_proxy_size(path: Path) -> None:
    size = path.stat().st_size
    if size > MAXIMUM_INLINE_PROXY_BYTES:
        raise FinalistProxyUnavailable(
            "finalist comparison proxy is too large for inline provider "
            f"transport: {size} bytes exceeds "
            f"{MAXIMUM_INLINE_PROXY_BYTES} bytes"
        )


def rounded(value: float) -> float:
    return round(float(value), 3)


def run_media_command(
    command: list[str], *, label: str
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"{label} failed with exit code {completed.returncode}: "
            f"{completed.stderr[-1200:]}"
        )
    return completed


def probe_media(path: Path, *, ffprobe: str) -> dict[str, Any]:
    completed = run_media_command(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        label=f"ffprobe {path.name}",
    )
    value = json.loads(completed.stdout or "{}")
    streams = value.get("streams", [])
    try:
        duration = float(value.get("format", {}).get("duration"))
    except (TypeError, ValueError) as exc:
        raise FinalistProxyUnavailable(
            f"source duration unavailable: {path}"
        ) from exc
    if not any(
        isinstance(item, dict) and item.get("codec_type") == "video"
        for item in streams
    ):
        raise FinalistProxyUnavailable(f"source has no video stream: {path}")
    return {
        "duration_seconds": rounded(duration),
        "has_audio": any(
            isinstance(item, dict) and item.get("codec_type") == "audio"
            for item in streams
        ),
    }


def _source_records(job_root: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for name in ("source_manifest.json", "local-source-manifest.json"):
        path = job_root / name
        if not path.is_file():
            continue
        value = load_json(path)
        for item in value.get("sources", []):
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                records[item["id"]] = {**records.get(item["id"], {}), **item}
    return records


def resolve_local_sources(
    job_root: Path, source_ids: set[str]
) -> dict[str, Path]:
    records = _source_records(job_root)
    resolved: dict[str, Path] = {}
    for source_id in sorted(source_ids):
        record = records.get(source_id, {})
        path_value = record.get("path") or record.get("local_path")
        if not isinstance(path_value, str) or not path_value:
            raise FinalistProxyUnavailable(
                f"local source path unavailable for {source_id}"
            )
        path = Path(path_value).expanduser()
        if not path.is_absolute():
            path = job_root / path
        path = path.resolve()
        if not path.is_file():
            raise FinalistProxyUnavailable(
                f"local source file missing for {source_id}: {path}"
            )
        declared_sha256 = record.get("sha256")
        if (
            isinstance(declared_sha256, str)
            and declared_sha256
            and sha256_file(path) != declared_sha256
        ):
            raise FinalistProxyUnavailable(
                f"local source hash mismatch for {source_id}: {path}"
            )
        resolved[source_id] = path
    return resolved


def finalist_span_sequences(
    legal_options: dict[str, Any],
) -> list[dict[str, Any]]:
    body_by_id = {
        item["option_id"]: item
        for item in legal_options.get("legal_block_options", [])
    }
    span_by_id = {
        item["span_candidate_id"]: item
        for item in legal_options.get("span_catalog", [])
    }
    result: list[dict[str, Any]] = []
    for partition in legal_options.get("legal_body_partitions", []):
        sequence: list[dict[str, Any]] = []
        for option_id in partition.get("body_option_ids", []):
            option = body_by_id.get(option_id)
            if not isinstance(option, dict):
                raise ValueError(f"unknown finalist body option: {option_id}")
            ordered_ids = sorted(
                option.get("span_candidate_ids", []),
                key=lambda span_id: (
                    int(span_by_id[span_id].get("episode", 0)),
                    float(span_by_id[span_id].get("start", 0.0)),
                    span_id,
                ),
            )
            sequence.extend(span_by_id[span_id] for span_id in ordered_ids)
        result.append(
            {
                "partition_id": partition["partition_id"],
                "spans": sequence,
            }
        )
    return result


def local_comparison_sequences(
    legal_options: dict[str, Any],
) -> list[dict[str, Any]]:
    """Keep the differing run plus one shared context Span on each side."""

    finalists = finalist_span_sequences(legal_options)
    if len(finalists) <= 1:
        return []
    id_sequences = [
        [item["span_candidate_id"] for item in finalist["spans"]]
        for finalist in finalists
    ]
    prefix = 0
    while all(len(values) > prefix for values in id_sequences):
        if len({values[prefix] for values in id_sequences}) != 1:
            break
        prefix += 1
    suffix = 0
    while all(len(values) - suffix - 1 >= prefix for values in id_sequences):
        if len({values[-suffix - 1] for values in id_sequences}) != 1:
            break
        suffix += 1
    if all(values == id_sequences[0] for values in id_sequences[1:]):
        return []
    localized: list[dict[str, Any]] = []
    for finalist in finalists:
        spans = finalist["spans"]
        start = max(0, prefix - 1)
        end = len(spans) - max(0, suffix - 1)
        selected = spans[start:end]
        duration = sum(
            float(span["end"]) - float(span["start"])
            for span in selected
        )
        if duration > MAXIMUM_LOCAL_SECONDS_PER_FINALIST + 0.001:
            raise FinalistProxyUnavailable(
                f"finalist {finalist['partition_id']} local comparison "
                f"requires {duration:.3f}s, above "
                f"{MAXIMUM_LOCAL_SECONDS_PER_FINALIST:.1f}s cap"
            )
        localized.append(
            {
                "partition_id": finalist["partition_id"],
                "spans": selected,
            }
        )
    return localized


def _render_range(
    *,
    source: Path,
    source_info: dict[str, Any],
    start: float,
    end: float,
    destination: Path,
    ffmpeg: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        return
    temporary = destination.with_name(f".{destination.stem}.part.mp4")
    if temporary.exists():
        temporary.unlink()
    duration = float(end) - float(start)
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{float(start):.6f}",
        "-i",
        str(source),
    ]
    if not source_info["has_audio"]:
        command.extend(
            [
                "-f",
                "lavfi",
                "-t",
                f"{duration:.6f}",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
            ]
        )
    command.extend(
        [
            "-t",
            f"{duration:.6f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0" if source_info["has_audio"] else "1:a:0",
            "-vf",
            (
                f"scale={PROXY_WIDTH}:{PROXY_HEIGHT}:"
                "force_original_aspect_ratio=decrease,"
                f"pad={PROXY_WIDTH}:{PROXY_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
                f"setsar=1,fps={PROXY_FPS:g},format=yuv420p"
            ),
            "-af",
            "aresample=async=1:first_pts=0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-b:v",
            f"{PROXY_VIDEO_BITRATE_KBPS}k",
            "-maxrate",
            f"{PROXY_VIDEO_BITRATE_KBPS}k",
            "-bufsize",
            f"{PROXY_VIDEO_BITRATE_KBPS * 2}k",
            "-c:a",
            "aac",
            "-b:a",
            f"{PROXY_AUDIO_BITRATE_KBPS}k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            "-movflags",
            "+faststart",
            str(temporary),
        ]
    )
    try:
        run_media_command(command, label=f"render {destination.name}")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _concat_media(paths: list[Path], destination: Path, *, ffmpeg: str) -> None:
    if not paths:
        raise ValueError("cannot concatenate an empty proxy sequence")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        return
    concat_path = destination.with_name(f".{destination.stem}.concat.txt")
    temporary = destination.with_name(f".{destination.stem}.part.mp4")
    atomic_write_text(
        concat_path,
        "".join(
            "file '" + str(path).replace("'", "'\\''") + "'\n"
            for path in paths
        ),
    )
    try:
        run_media_command(
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(temporary),
            ],
            label=f"concat {destination.name}",
        )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
        if concat_path.exists():
            concat_path.unlink()


def render_finalist_comparison(
    job_root: Path,
    legal_options: dict[str, Any],
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
) -> dict[str, Any] | None:
    sequences = local_comparison_sequences(legal_options)
    if not sequences:
        return None
    ffmpeg_path = ffmpeg or shutil.which("ffmpeg")
    ffprobe_path = ffprobe or shutil.which("ffprobe")
    if not ffmpeg_path or not ffprobe_path:
        raise FinalistProxyUnavailable("ffmpeg/ffprobe is unavailable")
    source_ids = {
        span["source_id"]
        for sequence in sequences
        for span in sequence["spans"]
    }
    sources = resolve_local_sources(job_root, source_ids)
    source_infos = {
        source_id: probe_media(path, ffprobe=ffprobe_path)
        for source_id, path in sources.items()
    }
    for sequence in sequences:
        for span in sequence["spans"]:
            start = float(span["start"])
            end = float(span["end"])
            source_duration = float(
                source_infos[span["source_id"]]["duration_seconds"]
            )
            if (
                start < -0.001
                or end <= start + 0.001
                or end > source_duration + 0.1
            ):
                raise FinalistProxyUnavailable(
                    f"invalid proxy range for {span['span_candidate_id']}: "
                    f"{start:.3f}-{end:.3f} / {source_duration:.3f}s"
                )
    identity = {
        "story_id": legal_options["story_id"],
        "compiler_version": legal_options.get("compiler_version"),
        "partitions": [
            {
                "partition_id": sequence["partition_id"],
                "spans": [
                    [
                        span["span_candidate_id"],
                        span["source_id"],
                        span["start"],
                        span["end"],
                    ]
                    for span in sequence["spans"]
                ],
            }
            for sequence in sequences
        ],
        "sources": {
            source_id: sha256_file(path)
            for source_id, path in sorted(sources.items())
        },
        "proxy_policy": {
            "width": PROXY_WIDTH,
            "height": PROXY_HEIGHT,
            "fps": PROXY_FPS,
            "video_bitrate_kbps": PROXY_VIDEO_BITRATE_KBPS,
            "audio_bitrate_kbps": PROXY_AUDIO_BITRATE_KBPS,
            "maximum_local_seconds_per_finalist": (
                MAXIMUM_LOCAL_SECONDS_PER_FINALIST
            ),
        },
    }
    cache_key = json_sha256(identity)
    cache_dir = (
        job_root
        / ".plan-proxy-cache"
        / legal_options["story_id"]
        / cache_key
    )
    comparison_path = cache_dir / "comparison.mp4"
    metadata_path = cache_dir / "comparison.json"
    if comparison_path.is_file() and metadata_path.is_file():
        metadata = load_json(metadata_path)
        if metadata.get("comparison_sha256") == sha256_file(comparison_path):
            ensure_inline_proxy_size(comparison_path)
            return metadata
    all_parts: list[Path] = []
    finalist_records: list[dict[str, Any]] = []
    cursor = 0.0
    for finalist_index, sequence in enumerate(sequences, start=1):
        finalist_parts: list[Path] = []
        span_records: list[dict[str, Any]] = []
        finalist_start = cursor
        for span_index, span in enumerate(sequence["spans"], start=1):
            part_path = cache_dir / "parts" / (
                f"f{finalist_index:02d}-s{span_index:02d}.mp4"
            )
            _render_range(
                source=sources[span["source_id"]],
                source_info=source_infos[span["source_id"]],
                start=float(span["start"]),
                end=float(span["end"]),
                destination=part_path,
                ffmpeg=ffmpeg_path,
            )
            part_info = probe_media(part_path, ffprobe=ffprobe_path)
            duration = float(part_info["duration_seconds"])
            finalist_parts.append(part_path)
            all_parts.append(part_path)
            span_records.append(
                {
                    "span_candidate_id": span["span_candidate_id"],
                    "source_id": span["source_id"],
                    "source_start": span["start"],
                    "source_end": span["end"],
                    "proxy_start": rounded(cursor),
                    "proxy_end": rounded(cursor + duration),
                }
            )
            cursor += duration
        finalist_records.append(
            {
                "partition_id": sequence["partition_id"],
                "proxy_start": rounded(finalist_start),
                "proxy_end": rounded(cursor),
                "spans": span_records,
            }
        )
    _concat_media(all_parts, comparison_path, ffmpeg=ffmpeg_path)
    ensure_inline_proxy_size(comparison_path)
    comparison_info = probe_media(comparison_path, ffprobe=ffprobe_path)
    comparison_duration = float(comparison_info["duration_seconds"])
    if abs(comparison_duration - cursor) > max(1.0, cursor * 0.01):
        raise FinalistProxyUnavailable(
            "finalist comparison duration drift exceeds tolerance: "
            f"expected={cursor:.3f}s actual={comparison_duration:.3f}s"
        )
    metadata = {
        "status": "ready",
        "schema_version": "1.0",
        "story_id": legal_options["story_id"],
        "cache_key": cache_key,
        "comparison_path": str(comparison_path),
        "comparison_sha256": sha256_file(comparison_path),
        "duration_seconds": rounded(comparison_duration),
        "finalists": finalist_records,
        "identity": identity,
    }
    atomic_write_json(metadata_path, metadata)
    return metadata
