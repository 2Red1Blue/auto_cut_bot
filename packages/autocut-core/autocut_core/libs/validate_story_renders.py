#!/usr/bin/env python3
"""Validate final Story MP4 files, profiles, duration and black separators."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from autocut_core.libs.render_story_videos import probe_media
from autocut_core.io import (
    atomic_write_json,
    load_json,
    sha256_file,
    update_project_stage,
)
from autocut_core.schema.compat import validate_task_response
from autocut_core.libs.validate_story_render_recipes import validate as validate_recipes


def decode_check(path: Path, ffmpeg: str) -> str | None:
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return (completed.stderr or "decode check failed").strip()[-1000:]
    return None


def detected_duration(
    path: Path,
    *,
    start: float,
    duration: float,
    ffmpeg: str,
    audio: bool,
) -> float:
    if duration <= 0.1:
        return 0.0
    filter_value = (
        f"silencedetect=noise=-50dB:d={max(0.08, duration * 0.55):.3f}"
        if audio
        else f"blackdetect=d={max(0.08, duration * 0.55):.3f}:pix_th=0.10"
    )
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(path),
        "-ss",
        f"{start:.6f}",
        "-t",
        f"{duration:.6f}",
    ]
    command += (
        ["-vn", "-af", filter_value]
        if audio
        else ["-an", "-vf", filter_value]
    )
    command += ["-f", "null", "-"]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return 0.0
    pattern = (
        r"silence_duration:\s*([0-9.]+)"
        if audio
        else r"black_duration:([0-9.]+)"
    )
    values = [float(item) for item in re.findall(pattern, completed.stderr)]
    return max(values, default=0.0)


def validate(
    job_root: Path,
    *,
    output_dir: Path | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    check_decode: bool = True,
) -> dict[str, Any]:
    job_root = job_root.expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    recipe_validation = validate_recipes(job_root, check_files=True)
    if not recipe_validation["ok"]:
        return {
            "ok": False,
            "errors": [
                f"story_render_recipes: {item}"
                for item in recipe_validation["errors"]
            ],
            "warnings": warnings,
            "outputs": [],
        }
    for executable in (ffmpeg, ffprobe):
        if shutil.which(executable) is None:
            return {
                "ok": False,
                "errors": [f"executable not found: {executable}"],
                "warnings": warnings,
                "outputs": [],
            }
    output_dir = (
        output_dir.expanduser().resolve()
        if output_dir is not None
        else job_root / "story-renders"
    )
    render_index_path = output_dir / "index.json"
    recipe_index_path = job_root / "story-render-recipes" / "index.json"
    if not render_index_path.is_file():
        return {
            "ok": False,
            "errors": [f"missing Story Render Index: {render_index_path}"],
            "warnings": warnings,
            "outputs": [],
        }
    index = load_json(render_index_path)
    recipe_index = load_json(recipe_index_path)
    errors.extend(
        f"story_renders.index: {item}"
        for item in validate_task_response("story_render_index", index)
    )
    if index.get("recipe_index_sha256") != sha256_file(recipe_index_path):
        errors.append("Story Render Index Recipe Index SHA-256 is stale")
    recipe_entries = {
        item["story_id"]: item for item in recipe_index["recipes"]
    }
    output_entries: dict[str, dict[str, Any]] = {}
    output_reports: list[dict[str, Any]] = []
    for output in index.get("outputs", []):
        story_id = output.get("story_id")
        if not isinstance(story_id, str) or story_id in output_entries:
            errors.append(f"invalid or duplicate rendered Story: {story_id!r}")
            continue
        output_entries[story_id] = output
        recipe_entry = recipe_entries.get(story_id)
        if recipe_entry is None:
            errors.append(f"{story_id}: rendered output has no Recipe")
            continue
        recipe_path = Path(recipe_entry["path"]).expanduser().resolve()
        output_path = Path(output["path"]).expanduser().resolve()
        if not recipe_path.is_file():
            errors.append(f"{story_id}: Render Recipe is missing")
            continue
        if not output_path.is_file():
            errors.append(f"{story_id}: rendered MP4 is missing")
            continue
        recipe = load_json(recipe_path)
        if output["recipe_path"] != str(recipe_path):
            errors.append(f"{story_id}: output Recipe path is inconsistent")
        if output["recipe_sha256"] != sha256_file(recipe_path):
            errors.append(f"{story_id}: output Recipe SHA-256 is stale")
        if output["sha256"] != sha256_file(output_path):
            errors.append(f"{story_id}: rendered MP4 SHA-256 is stale")
        try:
            observed = probe_media(output_path, ffprobe)
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"{story_id}: cannot inspect rendered MP4: {exc}")
            continue
        profile = recipe["render_profile"]
        expected_profile = {
            "width": profile["width"],
            "height": profile["height"],
            "fps": float(profile["fps"]),
            "video_codec": "h264",
            "audio_codec": "aac",
            "audio_sample_rate": profile["audio_sample_rate"],
            "audio_channels": profile["audio_channels"],
        }
        for field, expected in expected_profile.items():
            actual = observed[field]
            if field == "fps":
                if abs(float(actual) - expected) > 0.05:
                    errors.append(
                        f"{story_id}: rendered fps expected {expected}, got {actual}"
                    )
            elif actual != expected:
                errors.append(
                    f"{story_id}: rendered {field} expected {expected!r}, got {actual!r}"
                )
        expected_duration = float(recipe["expected_duration_seconds"])
        tolerance = max(
            0.12,
            (len(recipe["timeline"]) + 2) / float(profile["fps"]),
        )
        duration_delta = abs(observed["duration_seconds"] - expected_duration)
        if duration_delta > tolerance:
            errors.append(
                f"{story_id}: duration delta {duration_delta:.3f}s exceeds "
                f"{tolerance:.3f}s"
            )
        transition_checks: list[dict[str, Any]] = []
        for item in recipe["timeline"]:
            if item["kind"] != "transition":
                continue
            inset = min(0.04, float(item["duration_seconds"]) / 8)
            start = float(item["start_seconds"]) + inset
            duration = float(item["duration_seconds"]) - inset * 2
            black_duration = detected_duration(
                output_path,
                start=start,
                duration=duration,
                ffmpeg=ffmpeg,
                audio=False,
            )
            silence_duration = detected_duration(
                output_path,
                start=start,
                duration=duration,
                ffmpeg=ffmpeg,
                audio=True,
            )
            minimum = max(0.12, duration * 0.70)
            if black_duration < minimum:
                errors.append(
                    f"{story_id}: teaser separator is not sufficiently black"
                )
            if silence_duration < minimum:
                errors.append(
                    f"{story_id}: teaser separator audio is not silent"
                )
            transition_checks.append(
                {
                    "transition_id": item["ref_id"],
                    "expected_start_seconds": item["start_seconds"],
                    "expected_duration_seconds": item["duration_seconds"],
                    "detected_black_seconds": round(black_duration, 3),
                    "detected_silence_seconds": round(silence_duration, 3),
                }
            )
        decode_error = decode_check(output_path, ffmpeg) if check_decode else None
        if decode_error is not None:
            errors.append(f"{story_id}: decode check failed: {decode_error}")
        expected_output_fields = {
            "duration_seconds": observed["duration_seconds"],
            "size_bytes": observed["size_bytes"],
            "width": observed["width"],
            "height": observed["height"],
            "fps": observed["fps"],
            "video_codec": observed["video_codec"],
            "audio_codec": observed["audio_codec"],
            "audio_sample_rate": observed["audio_sample_rate"],
            "audio_channels": observed["audio_channels"],
        }
        for field, expected in expected_output_fields.items():
            if output.get(field) != expected:
                errors.append(
                    f"{story_id}: Story Render Index {field} is stale"
                )
        output_reports.append(
            {
                "story_id": story_id,
                "path": str(output_path),
                "sha256": sha256_file(output_path),
                "expected_duration_seconds": expected_duration,
                "observed_duration_seconds": observed["duration_seconds"],
                "duration_delta_seconds": round(duration_delta, 3),
                "decode_check": "passed" if decode_error is None else "failed",
                "transition_checks": transition_checks,
            }
        )
    if set(output_entries) != set(recipe_entries):
        errors.append(
            "rendered outputs do not exactly cover Render Recipes: "
            f"missing={sorted(set(recipe_entries) - set(output_entries))}, "
            f"extra={sorted(set(output_entries) - set(recipe_entries))}"
        )
    if index.get("failures"):
        errors.append("Story Render Index contains render failures")
    expected_status = (
        "complete" if recipe_index["status"] == "complete" else "partial"
    )
    expected_counts = {
        "status": expected_status,
        "recipe_count": recipe_index["recipe_count"],
        "rendered_count": recipe_index["recipe_count"],
        "failed_count": 0,
        "skipped_story_count": recipe_index["skipped_story_count"],
    }
    for field, expected in expected_counts.items():
        if index.get(field) != expected:
            errors.append(
                f"Story Render Index {field} is inconsistent: "
                f"expected {expected!r}, got {index.get(field)!r}"
            )
    if recipe_index["status"] == "partial":
        warnings.append("rendered all approved Stories; non-approved Stories remain skipped")
    return {
        "ok": not errors,
        "status": "passed" if not errors else "blocked",
        "errors": errors,
        "warnings": warnings,
        "render_index_path": str(render_index_path),
        "render_index_sha256": sha256_file(render_index_path),
        "outputs": output_reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument(
        "--skip-decode-check",
        action="store_true",
        help="Diagnostic only; formal validation must not skip decode checks.",
    )
    args = parser.parse_args()
    job_root = args.job_root.expanduser().resolve()
    report = validate(
        job_root,
        output_dir=args.output_dir,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        check_decode=not args.skip_decode_check,
    )
    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else job_root / "story-render-validation.json"
    )
    atomic_write_json(output_path, report)
    update_project_stage(
        job_root / "project.json",
        "story_render_validation",
        report.get("status", "blocked"),
        inputs={
            "story_render_index": report.get("render_index_path", ""),
        },
        outputs={"story_render_validation": str(output_path)},
        note=(
            f"outputs={len(report.get('outputs', []))}; "
            f"errors={len(report['errors'])}"
        ),
    )
    for warning in report["warnings"]:
        print(f"WARNING\t{warning}")
    for error in report["errors"]:
        print(f"ERROR\t{error}")
    print(f"STORY_RENDER_VALIDATION\t{output_path}")
    print(f"STATUS\t{'ok' if report['ok'] else 'blocked'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
