#!/usr/bin/env python3
"""Render validated approved Story Recipes locally with FFmpeg."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from fractions import Fraction
from pathlib import Path
from typing import Any

from autocut_core.io import (
    atomic_write_json,
    load_json,
    sha256_file,
    update_project_stage,
)
from autocut_core.libs.story_render_common import RENDER_METHOD, validate_render_recipe
from autocut_core.schema.compat import validate_task_response
from autocut_core.libs.validate_story_render_recipes import validate as validate_recipes
from autocut_core.libs.story_pipeline_gate import validate as validate_story_pipeline_gate


def run(command: list[str]) -> None:
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"command failed with exit code {completed.returncode}")


def probe_media(path: Path, ffprobe: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:"
            "stream=index,codec_type,codec_name,width,height,"
            "r_frame_rate,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {path}")
    payload = json.loads(completed.stdout or "{}")
    streams = payload.get("streams", [])
    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        None,
    )
    if not isinstance(video, dict):
        raise RuntimeError(f"rendered media has no video stream: {path}")
    rate = str(video.get("r_frame_rate", "0/1"))
    try:
        fps = float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "duration_seconds": round(
            float(payload.get("format", {}).get("duration", 0) or 0), 3
        ),
        "size_bytes": int(
            payload.get("format", {}).get("size", 0) or path.stat().st_size
        ),
        "width": int(video.get("width", 0) or 0),
        "height": int(video.get("height", 0) or 0),
        "fps": round(fps, 3),
        "video_codec": str(video.get("codec_name", "")),
        "audio_codec": str(audio.get("codec_name", "")) if audio else "",
        "audio_sample_rate": int(audio.get("sample_rate", 0) or 0)
        if audio
        else 0,
        "audio_channels": int(audio.get("channels", 0) or 0)
        if audio
        else 0,
        "has_audio": audio is not None,
    }


def source_has_audio(path: Path, ffprobe: str) -> bool:
    try:
        return bool(probe_media(path, ffprobe)["has_audio"])
    except RuntimeError:
        return False


def video_filter(
    profile: dict[str, Any],
    duration: float,
    *,
    fade_in_seconds: float = 0.0,
    fade_out_seconds: float = 0.0,
) -> str:
    width = profile["width"]
    height = profile["height"]
    if profile["fit"] == "cover":
        geometry = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
    else:
        geometry = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
        )
    filters = [
        geometry,
        "setsar=1",
        f"fps={profile['fps']}",
        f"trim=duration={duration:.6f}",
        "setpts=PTS-STARTPTS",
        f"format={profile['pixel_format']}",
    ]
    if fade_in_seconds > 0:
        filters.append(
            f"fade=t=in:st=0:d={fade_in_seconds:.6f}:color=black"
        )
    if fade_out_seconds > 0:
        start = max(0.0, duration - fade_out_seconds)
        filters.append(
            f"fade=t=out:st={start:.6f}:d={fade_out_seconds:.6f}:color=black"
        )
    return ",".join(filters)


def audio_filter(
    profile: dict[str, Any],
    duration: float,
    *,
    fade_in_seconds: float = 0.0,
    fade_out_seconds: float = 0.0,
    fade_curve: str = "tri",
) -> str:
    filters = [
        f"aresample={profile['audio_sample_rate']}:async=1:first_pts=0",
        f"aformat=sample_rates={profile['audio_sample_rate']}:"
        "channel_layouts=stereo",
        f"apad=whole_dur={duration:.6f}",
        f"atrim=0:{duration:.6f}",
        "asetpts=PTS-STARTPTS",
    ]
    if fade_in_seconds > 0:
        filters.append(
            f"afade=t=in:st=0:d={fade_in_seconds:.6f}:curve={fade_curve}"
        )
    if fade_out_seconds > 0:
        start = max(0.0, duration - fade_out_seconds)
        filters.append(
            f"afade=t=out:st={start:.6f}:d={fade_out_seconds:.6f}:"
            f"curve={fade_curve}"
        )
    return ",".join(filters)


def codec_args(profile: dict[str, Any]) -> list[str]:
    return [
        "-c:v",
        str(profile["video_codec"]),
        "-preset",
        str(profile["video_preset"]),
        "-crf",
        str(profile["video_crf"]),
        "-pix_fmt",
        str(profile["pixel_format"]),
        "-c:a",
        str(profile["audio_codec"]),
        "-b:a",
        f"{profile['audio_bitrate_kbps']}k",
        "-ar",
        str(profile["audio_sample_rate"]),
        "-ac",
        str(profile["audio_channels"]),
    ]


def cache_media_matches(
    path: Path,
    *,
    profile: dict[str, Any],
    duration: float,
    ffprobe: str,
) -> bool:
    if not path.is_file():
        return False
    try:
        observed = probe_media(path, ffprobe)
    except (OSError, RuntimeError, ValueError):
        return False
    return (
        observed["width"] == profile["width"]
        and observed["height"] == profile["height"]
        and abs(observed["fps"] - profile["fps"]) <= 0.05
        and observed["has_audio"]
        and abs(observed["duration_seconds"] - duration) <= 0.15
    )


def render_clip(
    *,
    clip: dict[str, Any],
    source: dict[str, Any],
    destination: Path,
    profile: dict[str, Any],
    ffmpeg: str,
    ffprobe: str,
    fade_in_seconds: float = 0.0,
    fade_out_seconds: float = 0.0,
    fade_curve: str = "tri",
) -> None:
    duration = float(clip["duration_seconds"])
    if cache_media_matches(
        destination,
        profile=profile,
        duration=duration,
        ffprobe=ffprobe,
    ):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_path = Path(source["path"]).expanduser().resolve()
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{float(clip['source_start']):.6f}",
        "-t",
        f"{duration:.6f}",
        "-i",
        str(source_path),
    ]
    has_audio = source_has_audio(source_path, ffprobe)
    if not has_audio:
        command += [
            "-f",
            "lavfi",
            "-t",
            f"{duration:.6f}",
            "-i",
            "anullsrc=channel_layout=stereo:"
            f"sample_rate={profile['audio_sample_rate']}",
        ]
    command += [
        "-map",
        "0:v:0",
        "-map",
        "0:a:0" if has_audio else "1:a:0",
        "-vf",
        video_filter(
            profile,
            duration,
            fade_in_seconds=fade_in_seconds,
            fade_out_seconds=fade_out_seconds,
        ),
        "-af",
        audio_filter(
            profile,
            duration,
            fade_in_seconds=fade_in_seconds,
            fade_out_seconds=fade_out_seconds,
            fade_curve=fade_curve,
        ),
        *codec_args(profile),
        "-movflags",
        "+faststart",
        "-avoid_negative_ts",
        "make_zero",
        str(destination),
    ]
    run(command)
    if not cache_media_matches(
        destination,
        profile=profile,
        duration=duration,
        ffprobe=ffprobe,
    ):
        raise RuntimeError(f"rendered Clip failed validation: {destination}")


def render_black_separator(
    *,
    destination: Path,
    duration: float,
    profile: dict[str, Any],
    ffmpeg: str,
    ffprobe: str,
) -> None:
    if cache_media_matches(
        destination,
        profile=profile,
        duration=duration,
        ffprobe=ffprobe,
    ):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={profile['width']}x{profile['height']}:"
        f"r={profile['fps']}:d={duration:.6f}",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:"
        f"sample_rate={profile['audio_sample_rate']}",
        "-t",
        f"{duration:.6f}",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        *codec_args(profile),
        "-movflags",
        "+faststart",
        "-shortest",
        str(destination),
    ]
    run(command)
    if not cache_media_matches(
        destination,
        profile=profile,
        duration=duration,
        ffprobe=ffprobe,
    ):
        raise RuntimeError(f"black separator failed validation: {destination}")


def concat_quote(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def render_recipe(
    *,
    recipe_path: Path,
    output_dir: Path,
    cache_root: Path,
    ffmpeg: str,
    ffprobe: str,
    overwrite: bool,
) -> dict[str, Any]:
    recipe_path = recipe_path.expanduser().resolve()
    recipe = load_json(recipe_path)
    recipe_errors = validate_render_recipe(recipe, check_files=True)
    if recipe_errors:
        raise ValueError("; ".join(recipe_errors[:30]))
    recipe_sha256 = sha256_file(recipe_path)
    profile = recipe["render_profile"]
    cache_dir = (
        cache_root
        / recipe["story_id"]
        / recipe_sha256
    )
    sources = {item["source_id"]: item for item in recipe["sources"]}
    clips = {item["id"]: item for item in recipe["clips"]}
    transitions = {item["id"]: item for item in recipe["transitions"]}
    # Map each Clip that touches a black_separator to its fade envelope:
    # the transition's from_clip_id gets fade_out on its trailing edge, and
    # the transition's to_clip_id gets fade_in on its leading edge. These
    # values are baked into that Clip's cached MP4 so the concat later just
    # sees pre-attenuated media on both sides of the black gap.
    clip_fade_in: dict[str, float] = {}
    clip_fade_out: dict[str, float] = {}
    fade_curve = "tri"
    for transition in transitions.values():
        if transition["type"] != "black_separator":
            continue
        clip_fade_out[transition["from_clip_id"]] = float(
            transition["fade_out_seconds"]
        )
        clip_fade_in[transition["to_clip_id"]] = float(
            transition["fade_in_seconds"]
        )
        fade_curve = transition["fade_curve"]
    timeline_media: list[Path] = []
    for item in recipe["timeline"]:
        if item["kind"] == "clip":
            clip = clips[item["ref_id"]]
            media_path = cache_dir / "clips" / f"{clip['id']}.mp4"
            render_clip(
                clip=clip,
                source=sources[clip["source_id"]],
                destination=media_path,
                profile=profile,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                fade_in_seconds=clip_fade_in.get(clip["id"], 0.0),
                fade_out_seconds=clip_fade_out.get(clip["id"], 0.0),
                fade_curve=fade_curve,
            )
        else:
            transition = transitions[item["ref_id"]]
            if (
                transition["type"] != "black_separator"
                or transition["audio_policy"] != "silence"
            ):
                raise ValueError(
                    f"unsupported transition: {transition['type']}"
                )
            media_path = (
                cache_dir
                / "transitions"
                / f"{transition['id']}.mp4"
            )
            render_black_separator(
                destination=media_path,
                duration=float(transition["duration_seconds"]),
                profile=profile,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
            )
        timeline_media.append(media_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / recipe["output_filename"]
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"output exists; pass --overwrite to replace it: {output_path}"
        )
    concat_path = cache_dir / "concat.txt"
    concat_path.parent.mkdir(parents=True, exist_ok=True)
    concat_path.write_text(
        "".join(
            f"file '{concat_quote(path)}'\n" for path in timeline_media
        ),
        encoding="utf-8",
    )
    temporary = output_dir / (
        f".{output_path.stem}-{os.getpid()}.tmp.mp4"
    )
    if temporary.exists():
        temporary.unlink()
    try:
        command = [
            ffmpeg,
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
            "-avoid_negative_ts",
            "make_zero",
            str(temporary),
        ]
        run(command)
        observed = probe_media(temporary, ffprobe)
        if (
            observed["width"] != profile["width"]
            or observed["height"] != profile["height"]
            or not observed["has_audio"]
        ):
            raise RuntimeError("final rendered media profile is invalid")
        os.replace(temporary, output_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    observed = probe_media(output_path, ffprobe)
    output = {
        "story_id": recipe["story_id"],
        "title": recipe["title"],
        "production_slot": recipe["production_slot"],
        "recipe_path": str(recipe_path),
        "recipe_sha256": recipe_sha256,
        "path": str(output_path),
        "sha256": sha256_file(output_path),
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
    errors = validate_task_response("story_render_output", output)
    if errors:
        raise RuntimeError(
            "rendered output schema is invalid: " + "; ".join(errors[:20])
        )
    return output


def render_job(**kwargs: Any) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    recipe_path = Path(kwargs["recipe_path"])
    try:
        return render_recipe(**kwargs), None
    except (
        FileNotFoundError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        try:
            story_id = load_json(recipe_path).get("story_id", recipe_path.stem)
        except (OSError, ValueError):
            story_id = recipe_path.stem
        return None, {"story_id": str(story_id), "reason": str(exc)}


def render(
    job_root: Path,
    *,
    output_dir: Path | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    jobs: int = 2,
    overwrite: bool = False,
) -> dict[str, Any]:
    job_root = job_root.expanduser().resolve()
    if not 1 <= jobs <= 8:
        raise ValueError("jobs must be in 1..8")
    pipeline_gate = validate_story_pipeline_gate(job_root, require_seal=True)
    if not pipeline_gate["ok"]:
        raise ValueError(
            "Canonical Story pipeline gate failed: "
            + "; ".join(pipeline_gate["errors"][:40])
        )
    validation = validate_recipes(job_root, check_files=True)
    if not validation["ok"]:
        raise ValueError(
            "Story Render Recipe validation failed: "
            + "; ".join(validation["errors"][:30])
        )
    for executable in (ffmpeg, ffprobe):
        if shutil.which(executable) is None:
            raise FileNotFoundError(f"executable not found: {executable}")
    recipe_index_path = job_root / "story-render-recipes" / "index.json"
    recipe_index = load_json(recipe_index_path)
    output_dir = (
        output_dir.expanduser().resolve()
        if output_dir is not None
        else job_root / "story-renders"
    )
    cache_root = job_root / ".render-cache" / "story-render"
    entries = sorted(
        recipe_index["recipes"], key=lambda item: item["production_slot"]
    )
    outputs: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    worker_count = min(jobs, len(entries))
    kwargs_list = [
        {
            "recipe_path": Path(entry["path"]),
            "output_dir": output_dir,
            "cache_root": cache_root,
            "ffmpeg": ffmpeg,
            "ffprobe": ffprobe,
            "overwrite": overwrite,
        }
        for entry in entries
    ]
    if worker_count <= 1:
        results = [render_job(**kwargs) for kwargs in kwargs_list]
    else:
        futures: dict[
            Future[tuple[dict[str, Any] | None, dict[str, str] | None]],
            int,
        ] = {}
        results = []
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="story-render",
        ) as executor:
            for index, kwargs in enumerate(kwargs_list):
                futures[executor.submit(render_job, **kwargs)] = index
            ordered_results: dict[
                int,
                tuple[dict[str, Any] | None, dict[str, str] | None],
            ] = {}
            for future in as_completed(futures):
                ordered_results[futures[future]] = future.result()
            results = [ordered_results[index] for index in range(len(entries))]
    for output, failure in results:
        if output is not None:
            outputs.append(output)
        if failure is not None:
            failures.append(failure)
    if not outputs:
        status = "failed"
    elif failures or recipe_index["status"] != "complete":
        status = "partial"
    else:
        status = "complete"
    index = {
        "schema_version": "1.0",
        "method": RENDER_METHOD,
        "status": status,
        "recipe_index_sha256": sha256_file(recipe_index_path),
        "recipe_count": recipe_index["recipe_count"],
        "rendered_count": len(outputs),
        "failed_count": len(failures),
        "skipped_story_count": recipe_index["skipped_story_count"],
        "outputs": outputs,
        "failures": failures,
    }
    schema_errors = validate_task_response("story_render_index", index)
    if schema_errors:
        raise ValueError(
            "invalid Story Render Index: " + "; ".join(schema_errors[:30])
        )
    index_path = output_dir / "index.json"
    atomic_write_json(index_path, index)
    update_project_stage(
        job_root / "project.json",
        "story_render",
        status,
        inputs={"story_render_recipe_index": str(recipe_index_path)},
        outputs={"story_render_index": str(index_path)},
        note=(
            f"rendered={len(outputs)}/{recipe_index['recipe_count']}; "
            f"failed={len(failures)}; skipped_qc={recipe_index['skipped_story_count']}"
        ),
    )
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        index = render(
            args.job_root,
            output_dir=args.output_dir,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
            jobs=args.jobs,
            overwrite=args.overwrite,
        )
    except (
        FileNotFoundError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"ERROR\t{exc}")
        return 1
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else args.job_root.expanduser().resolve() / "story-renders"
    )
    for output in index["outputs"]:
        print(f"RENDERED\t{output['story_id']}\t{output['path']}")
    for failure in index["failures"]:
        print(f"FAILED\t{failure['story_id']}\t{failure['reason']}")
    print(f"STORY_RENDER_INDEX\t{output_dir / 'index.json'}")
    print(f"STATUS\t{index['status']}")
    return 0 if not index["failures"] and index["rendered_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
