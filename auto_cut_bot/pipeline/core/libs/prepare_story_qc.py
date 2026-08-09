#!/usr/bin/env python3
"""Render hard-cut Story QC proxies and prepare strict video-review jobs."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from autocut_core.io import (
    atomic_write_json,
    atomic_write_text,
    load_json,
    sha256_file,
    stable_id,
    update_project_stage,
)
from autocut_core.schema.compat import (
    story_video_qc_response_format,
    validate_task_response,
)
from autocut_core.libs.story_audio_boundary_qc import (
    audio_guard_default,
    local_source_map,
    prepare_and_run as prepare_and_run_audio_qc,
)
from autocut_core.libs.repair_story_audio_boundaries import (
    MAX_AUTO_ADJUSTMENT_SECONDS,
    MAX_REPAIR_ROUNDS,
    SAFE_AUDIO_STATUSES,
    create_repair_round,
    validate_repair_index,
)
from autocut_core.libs.qc_admission import (
    ACCEPTED,
    validate_admission,
)
from autocut_core.contracts.plan_validation import validate as validate_story_plans


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip("-.")
    return cleaned or "story-qc"


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
        raise RuntimeError(f"{label} failed with exit code {completed.returncode}")
    return completed


def probe_media(locator: str, *, ffprobe: str, label: str) -> dict[str, Any]:
    completed = run_media_command(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            locator,
        ],
        label=f"ffprobe {label}",
    )
    value = json.loads(completed.stdout or "{}")
    streams = value.get("streams", [])
    duration = value.get("format", {}).get("duration")
    if not isinstance(streams, list) or not any(
        isinstance(item, dict) and item.get("codec_type") == "video"
        for item in streams
    ):
        raise ValueError(f"{label}: source has no decodable video stream")
    try:
        duration_seconds = float(duration)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}: source duration is unavailable") from exc
    return {
        "duration_seconds": rounded(duration_seconds),
        "has_video": True,
        "has_audio": any(
            isinstance(item, dict) and item.get("codec_type") == "audio"
            for item in streams
        ),
    }


def video_filter(
    *, width: int, height: int, fps: float, marker_at: float | None
) -> str:
    filters = [
        (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
        ),
        "setsar=1",
        f"fps={fps:g}",
        "format=yuv420p",
    ]
    if marker_at is not None:
        start = max(0.0, marker_at - 0.08)
        end = marker_at + 0.08
        filters.append(
            "drawbox=x=0:y=0:w=iw:h=ih:color=red@0.90:t=8:"
            f"enable='between(t,{start:.3f},{end:.3f})'"
        )
    return ",".join(filters)


def render_range(
    *,
    locator: str,
    source_info: dict[str, Any],
    start: float,
    end: float,
    destination: Path,
    ffmpeg: str,
    width: int,
    height: int,
    fps: float,
    video_bitrate_kbps: int,
    audio_bitrate_kbps: int,
    marker_at: float | None = None,
    force: bool = False,
    label: str,
) -> None:
    if end <= start:
        raise ValueError(f"{label}: non-positive render range")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and not force:
        return
    temporary = destination.with_name(f".{destination.stem}.part.mp4")
    if temporary.exists():
        temporary.unlink()
    duration = end - start
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.6f}",
        "-i",
        locator,
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
            video_filter(
                width=width,
                height=height,
                fps=fps,
                marker_at=marker_at,
            ),
            "-af",
            "aresample=async=1:first_pts=0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-b:v",
            f"{video_bitrate_kbps}k",
            "-maxrate",
            f"{video_bitrate_kbps}k",
            "-bufsize",
            f"{video_bitrate_kbps * 2}k",
            "-c:a",
            "aac",
            "-b:a",
            f"{audio_bitrate_kbps}k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            str(temporary),
        ]
    )
    try:
        run_media_command(command, label=label)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def concat_quote(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def concat_media(
    paths: list[Path],
    destination: Path,
    *,
    ffmpeg: str,
    force: bool,
    label: str,
) -> None:
    if not paths:
        raise ValueError(f"{label}: no media to concatenate")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and not force:
        return
    concat_path = destination.with_name(f".{destination.stem}.concat.txt")
    temporary = destination.with_name(f".{destination.stem}.part.mp4")
    atomic_write_text(
        concat_path,
        "".join(f"file '{concat_quote(path)}'\n" for path in paths),
    )
    if temporary.exists():
        temporary.unlink()
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
            label=label,
        )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
        if concat_path.exists():
            concat_path.unlink()


def exact_remote_locators(job_root: Path) -> dict[str, str]:
    batch_path = job_root / "window-analysis-batch.json"
    if not batch_path.is_file():
        return {}
    batch = load_json(batch_path)
    result: dict[str, str] = {}
    for job in batch.get("jobs", []):
        if not isinstance(job, dict):
            continue
        source_id = job.get("source_id")
        media_url = job.get("media_url")
        if (
            isinstance(source_id, str)
            and isinstance(media_url, str)
            and source_id not in result
        ):
            result[source_id] = media_url
    return result


def source_locators(
    job_root: Path,
    source_manifest: dict[str, Any],
    *,
    required_source_ids: set[str] | None = None,
    local_source_manifest: dict[str, Any] | None = None,
) -> dict[str, str]:
    local_locators = {
        item["id"]: item["path"]
        for item in local_source_map(
            source_manifest,
            local_source_manifest,
            required_source_ids,
        )
    } if local_source_manifest is not None and required_source_ids is not None else {}
    exact_urls = exact_remote_locators(job_root)
    result: dict[str, str] = {}
    for source in source_manifest.get("sources", []):
        if not isinstance(source, dict) or not isinstance(source.get("id"), str):
            continue
        source_id = source["id"]
        if (
            required_source_ids is not None
            and source_id not in required_source_ids
        ):
            continue
        if source_id in local_locators:
            result[source_id] = local_locators[source_id]
            continue
        path_value = source.get("path")
        if isinstance(path_value, str):
            path = Path(path_value).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(
                    f"{source_id}: local source path does not exist"
                )
            result[source_id] = str(path)
            continue
        if source_id in exact_urls:
            result[source_id] = exact_urls[source_id]
            continue
        public_url = source.get("url")
        if isinstance(public_url, str) and public_url.startswith(("http://", "https://")):
            result[source_id] = public_url
            continue
        raise ValueError(
            f"{source_id}: no usable local path or exact remote URL for QC rendering"
        )
    if required_source_ids is not None:
        missing = required_source_ids - set(result)
        if missing:
            raise ValueError(
                "Story Plans reference unknown Sources: "
                + ", ".join(sorted(missing))
            )
    return result


def ordered_plan_clips(plan: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for block in sorted(plan["blocks"], key=lambda item: item["play_order"]):
        for clip_index, clip in enumerate(block["clips"], start=1):
            records.append(
                {
                    **clip,
                    "_block_id": block["id"],
                    "_block_role": block["role"],
                    "_block_play_order": block["play_order"],
                    "_clip_order_in_block": clip_index,
                }
            )
    return records


def script_summary(script: dict[str, Any]) -> dict[str, Any]:
    return {
        "story_promise": script["story_promise"],
        "central_question": script["central_question"],
        "start_state": script["start_state"],
        "end_state": script["end_state"],
        "local_payoff": script["local_payoff"],
        "ending_hook_intent": script["ending_hook_intent"],
        "beats": [
            {
                "id": beat["id"],
                "role": beat["role"],
                "concrete_story_content": beat["concrete_story_content"],
                "must_show": beat["must_show"],
                "must_not_reveal_fact_ids": beat[
                    "must_not_reveal_fact_ids"
                ],
                "viewer_state_before": beat["viewer_state_before"],
                "viewer_state_after": beat["viewer_state_after"],
            }
            for beat in script["beats"]
        ],
    }


def media_record(
    path: Path, *, ffprobe: str, label: str
) -> dict[str, Any]:
    probed = probe_media(str(path), ffprobe=ffprobe, label=label)
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "duration_seconds": probed["duration_seconds"],
        "size_bytes": path.stat().st_size,
        "has_video": probed["has_video"],
        "has_audio": probed["has_audio"],
    }


def review_asset(
    *,
    review_id: str,
    kind: str,
    path: Path,
    duration_seconds: float,
    cut_at_seconds: float,
    context_path: Path,
    block_ids: list[str],
    clip_ids: list[str],
    edge_ids: list[str],
) -> dict[str, Any]:
    return {
        "review_id": review_id,
        "review_kind": kind,
        "path": str(path),
        "sha256": sha256_file(path),
        "duration_seconds": rounded(duration_seconds),
        "cut_at_seconds": rounded(cut_at_seconds),
        "context_path": str(context_path),
        "context_sha256": sha256_file(context_path),
        "block_ids": block_ids,
        "clip_ids": clip_ids,
        "edge_ids": edge_ids,
    }


def final_boundary_repair_state(
    audio_report: dict[str, Any],
    *,
    applied_change_count: int,
    max_rounds: int,
) -> tuple[str, list[dict[str, Any]]]:
    unresolved: list[dict[str, Any]] = []
    for item in audio_report.get("boundaries", []):
        if not isinstance(item, dict):
            continue
        source_edge_review = (
            item.get("status") == "safe_source_edge"
            and item.get("speech_active_at_cut") is True
        )
        if source_edge_review:
            unresolved.append(
                {
                    "boundary_id": item.get("id"),
                    "story_id": item.get("output_id"),
                    "clip_id": item.get("segment_id"),
                    "route": "source_edge_review",
                    "reason": (
                        "物理源边缘已有活跃语音，无法从当前 Source 向外恢复。"
                    ),
                }
            )
        elif item.get("status") not in SAFE_AUDIO_STATUSES:
            unresolved.append(
                {
                    "boundary_id": item.get("id"),
                    "story_id": item.get("output_id"),
                    "clip_id": item.get("segment_id"),
                    "route": "blocked_replan",
                    "reason": (
                        f"完成最多 {max_rounds} 轮自动扩边后，"
                        "本地音频门禁仍未通过。"
                    ),
                }
            )
    if any(item["route"] == "blocked_replan" for item in unresolved):
        return "blocked_replan", unresolved
    if unresolved:
        return "review", unresolved
    if applied_change_count:
        return "verified_after_repair", []
    return "not_needed", []


def prepare_audio_boundary_with_repair(
    job_root: Path,
    *,
    base_plan_index_path: Path,
    local_source_manifest_path: Path | None,
    audio_python: Path,
    audio_guard_script: Path,
    cache_dir: Path,
    device: str,
    workers: int,
    force: bool,
    auto_repair: bool,
    max_repair_rounds: int,
    max_adjustment_seconds: float,
    include_blocked: bool,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    active_index_path = base_plan_index_path
    repair_attempts: list[dict[str, Any]] = []
    applied_change_count = 0
    analysis_round_count = 0
    final_audio: dict[str, Any] | None = None
    final_report: dict[str, Any] | None = None
    maximum_analysis_round = max_repair_rounds if auto_repair else 0
    audio_root = job_root / ".qc-cache" / "story-audio-boundary"
    for analysis_round in range(maximum_analysis_round + 1):
        analysis_round_count += 1
        audio_plan_path = (
            audio_root / f"round-{analysis_round:02d}.plan.json"
        )
        report_path = (
            audio_root / f"round-{analysis_round:02d}.report.json"
        )
        final_audio = prepare_and_run_audio_qc(
            job_root,
            plan_index_path=active_index_path,
            local_source_manifest_path=local_source_manifest_path,
            audio_python=audio_python,
            audio_guard_script=audio_guard_script,
            cache_dir=cache_dir,
            report_path=report_path,
            audio_plan_path=audio_plan_path,
            device=device,
            force=force,
            include_blocked=include_blocked,
            workers=workers,
        )
        final_report = load_json(report_path)
        if analysis_round >= maximum_analysis_round:
            break
        if not any(
            isinstance(item, dict)
            and item.get("status") == "adjustment_required"
            for item in final_report.get("boundaries", [])
        ):
            break
        repair_round = analysis_round + 1
        attempt = create_repair_round(
            job_root,
            base_plan_index_path=base_plan_index_path,
            active_plan_index_path=active_index_path,
            audio_report_path=report_path,
            repair_round=repair_round,
            max_rounds=max_repair_rounds,
            max_adjustment_seconds=max_adjustment_seconds,
        )
        repair_attempts.append(attempt)
        if not attempt["changed"]:
            break
        applied_change_count += int(attempt["applied_change_count"])
        active_index_path = Path(
            attempt["index_path"]
        ).expanduser().resolve()
        repair_errors = validate_repair_index(
            active_index_path,
            base_plan_index_path,
        )
        if repair_errors:
            raise ValueError(
                "invalid effective Story Plan repair chain: "
                + "; ".join(repair_errors[:30])
            )
    assert final_audio is not None and final_report is not None
    repair_status, unresolved = final_boundary_repair_state(
        final_report,
        applied_change_count=applied_change_count,
        max_rounds=max_repair_rounds,
    )
    repair_metadata = {
        "schema_version": "1.0",
        "method": "local-audio-boundary-repair-controller-v1",
        "status": repair_status,
        "enabled": auto_repair,
        "max_rounds": max_repair_rounds,
        "max_auto_adjustment_seconds": max_adjustment_seconds,
        "base_story_plan_index_path": str(base_plan_index_path),
        "base_story_plan_index_sha256": sha256_file(
            base_plan_index_path
        ),
        "effective_story_plan_index_path": str(active_index_path),
        "effective_story_plan_index_sha256": sha256_file(
            active_index_path
        ),
        "analysis_round_count": analysis_round_count,
        "applied_repair_round_count": sum(
            bool(item["changed"]) for item in repair_attempts
        ),
        "applied_change_count": applied_change_count,
        "attempts": repair_attempts,
        "unresolved": unresolved,
        "final_audio_report_path": final_audio["report_path"],
        "final_audio_report_sha256": final_audio["report_sha256"],
    }
    metadata_path = job_root / "story-boundary-repair.json"
    atomic_write_json(metadata_path, repair_metadata)
    repair_metadata["path"] = str(metadata_path)
    repair_metadata["sha256"] = sha256_file(metadata_path)
    return active_index_path, final_audio, repair_metadata


def prepare_story(
    *,
    job_root: Path,
    plan_index_sha256: str,
    plan_entry: dict[str, Any],
    approval_entry: dict[str, Any],
    admission_entry: dict[str, Any] | None,
    admission_path: Path | None,
    admission_sha256: str | None,
    source_manifest: dict[str, Any],
    source_manifest_sha256: str,
    locators: dict[str, str],
    source_infos: dict[str, dict[str, Any]],
    ffmpeg: str,
    ffprobe: str,
    width: int,
    height: int,
    fps: float,
    video_bitrate_kbps: int,
    audio_bitrate_kbps: int,
    review_width: int,
    review_height: int,
    review_video_bitrate_kbps: int,
    review_audio_bitrate_kbps: int,
    junction_handle_seconds: float,
    force: bool,
) -> tuple[Path, list[dict[str, Any]]]:
    plan_path = Path(plan_entry["path"]).expanduser().resolve()
    plan_sha256 = sha256_file(plan_path)
    plan = load_json(plan_path)
    human_admitted = (
        plan["status"] == "blocked"
        and isinstance(admission_entry, dict)
        and admission_entry.get("decision") == ACCEPTED
    )
    if plan["status"] != "ready_for_video_qc" and not human_admitted:
        raise ValueError(
            f"{plan['story_id']}: blocked Story Plan requires a valid "
            "human QC admission"
        )
    qc_admission = {
        "entry_mode": (
            "human_accepted_blocked_plan"
            if human_admitted
            else "machine_validated_plan"
        ),
        "original_plan_status": plan["status"],
        "admission_path": (
            str(admission_path) if admission_path is not None else None
        ),
        "admission_sha256": admission_sha256,
        "accepted_blocked_reasons": (
            list(admission_entry.get("blocked_reasons", []))
            if human_admitted
            else []
        ),
        "human_note": (
            admission_entry.get("note", "") if human_admitted else ""
        ),
        "decided_at": (
            admission_entry.get("decided_at") if human_admitted else None
        ),
    }
    script_path = Path(approval_entry["script_path"]).expanduser().resolve()
    script_sha256 = sha256_file(script_path)
    if script_sha256 != approval_entry.get("approved_script_sha256"):
        raise ValueError(f"{plan['story_id']}: approved Story Script is stale")
    script = load_json(script_path)
    admission_cache_key = (
        admission_sha256[:16]
        if isinstance(admission_sha256, str)
        else "machine"
    )
    cache_key = (
        f"{plan_sha256[:16]}-{source_manifest_sha256[:16]}-"
        f"{admission_cache_key}"
    )
    cache_dir = (
        job_root
        / ".qc-cache"
        / "story-qc"
        / safe_name(plan["story_id"])
        / cache_key
    )
    shared_cache_dir = (
        job_root
        / ".qc-cache"
        / "story-qc-shared"
        / safe_name(plan["story_id"])
        / source_manifest_sha256[:16]
    )
    clip_dir = shared_cache_dir / "clips"
    review_dir = shared_cache_dir / "junctions"
    context_dir = cache_dir / "contexts"
    ordered = ordered_plan_clips(plan)
    clip_assets: dict[str, Path] = {}
    proxy_clips: list[dict[str, Any]] = []
    proxy_cursor = 0.0
    for index, clip in enumerate(ordered, start=1):
        source_id = clip["source_id"]
        locator = locators[source_id]
        info = source_infos[source_id]
        start = float(clip["source_start"])
        end = float(clip["source_end"])
        if start < 0 or end > info["duration_seconds"] + 0.1 or end <= start:
            raise ValueError(
                f"{plan['story_id']}/{clip['id']}: source range is invalid"
            )
        clip_media_id = stable_id(
            "qc-clip-media",
            {
                "source_id": source_id,
                "source_start": rounded(start),
                "source_end": rounded(end),
                "width": width,
                "height": height,
                "fps": fps,
                "video_bitrate_kbps": video_bitrate_kbps,
                "audio_bitrate_kbps": audio_bitrate_kbps,
            },
        )
        asset_path = clip_dir / f"{clip_media_id}.mp4"
        render_range(
            locator=locator,
            source_info=info,
            start=start,
            end=end,
            destination=asset_path,
            ffmpeg=ffmpeg,
            width=width,
            height=height,
            fps=fps,
            video_bitrate_kbps=video_bitrate_kbps,
            audio_bitrate_kbps=audio_bitrate_kbps,
            force=force,
            label=f"{plan['story_id']}/{clip['id']} proxy",
        )
        clip_assets[clip["id"]] = asset_path
        duration = end - start
        proxy_clips.append(
            {
                "clip_id": clip["id"],
                "block_id": clip["_block_id"],
                "block_play_order": clip["_block_play_order"],
                "clip_order_in_block": clip["_clip_order_in_block"],
                "source_id": source_id,
                "episode": clip["episode"],
                "source_start": rounded(start),
                "source_end": rounded(end),
                "proxy_start": rounded(proxy_cursor),
                "proxy_end": rounded(proxy_cursor + duration),
                "boundary_status": clip["boundary_status"],
            }
        )
        proxy_cursor += duration
    story_proxy_path = cache_dir / "story-proxy.mp4"
    concat_media(
        [clip_assets[item["clip_id"]] for item in proxy_clips],
        story_proxy_path,
        ffmpeg=ffmpeg,
        force=force,
        label=f"{plan['story_id']} story proxy",
    )
    story_proxy = media_record(
        story_proxy_path,
        ffprobe=ffprobe,
        label=f"{plan['story_id']} story proxy",
    )
    review_assets: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    flow_review_id = stable_id(
        "qc-review",
        {"story_id": plan["story_id"], "kind": "story_flow"},
    )
    flow_context_path = context_dir / f"{flow_review_id}.json"
    flow_context = {
        "schema_version": "1.0",
        "story_id": plan["story_id"],
        "review_id": flow_review_id,
        "review_kind": "story_flow",
        "duration_seconds": story_proxy["duration_seconds"],
        "cut_at_seconds": 0.0,
        "title": plan["title"],
        "production_slot": plan["production_slot"],
        "story_plan_sha256": plan_sha256,
        "story_script": script_summary(script),
        "blocks": plan["blocks"],
        "sequence_edges": plan["sequence_edges"],
        "proxy_clips": proxy_clips,
        "checks_requested": ["coverage", "flow", "cut_safety"],
        "local_audio_boundary_authoritative": True,
        "story_plan_qc_admission": qc_admission,
        "instruction": (
            "吞字、词音节和说话切断由本地 Demucs + 双路 Silero VAD 门禁判定；"
            "本任务只检查故事覆盖、叙事流和画面可见的动作/反应切点。"
        ),
    }
    atomic_write_json(flow_context_path, flow_context)
    flow_asset = {
        "review_id": flow_review_id,
        "review_kind": "story_flow",
        "path": str(story_proxy_path),
        "sha256": story_proxy["sha256"],
        "duration_seconds": story_proxy["duration_seconds"],
        "cut_at_seconds": 0.0,
        "context_path": str(flow_context_path),
        "context_sha256": sha256_file(flow_context_path),
        "block_ids": [item["id"] for item in plan["blocks"]],
        "clip_ids": [item["clip_id"] for item in proxy_clips],
        "edge_ids": [item["id"] for item in plan["sequence_edges"]],
    }
    review_assets.append(flow_asset)
    jobs.append(
        {
            "id": flow_review_id,
            "task": "story_video_qc",
            "stage_version": "story-video-qc-v4-dynamic-schema",
            "story_id": plan["story_id"],
            "review_kind": "story_flow",
            "response_format": story_video_qc_response_format(
                story_id=plan["story_id"],
                review_id=flow_review_id,
                review_kind="story_flow",
            ),
            "context_file": str(flow_context_path),
            "media_file": str(story_proxy_path),
            "output": str(
                (
                    job_root
                    / "story-qc-video-results"
                    / f"{flow_review_id}.json"
                ).resolve()
            ),
        }
    )
    edges = {
        (item["from_block_id"], item["to_block_id"]): item
        for item in plan["sequence_edges"]
    }
    for left, right in zip(ordered, ordered[1:]):
        left_duration = float(left["source_end"]) - float(left["source_start"])
        right_duration = float(right["source_end"]) - float(right["source_start"])
        left_keep = min(junction_handle_seconds, left_duration)
        right_keep = min(junction_handle_seconds, right_duration)
        review_id = stable_id(
            "qc-review",
            {
                "story_id": plan["story_id"],
                "kind": "junction",
                "left": left["id"],
                "right": right["id"],
            },
        )
        junction_media_id = stable_id(
            "qc-junction-media",
            {
                "left_source_id": left["source_id"],
                "left_start": rounded(
                    float(left["source_end"]) - left_keep
                ),
                "left_end": rounded(left["source_end"]),
                "right_source_id": right["source_id"],
                "right_start": rounded(right["source_start"]),
                "right_end": rounded(
                    float(right["source_start"]) + right_keep
                ),
                "width": review_width,
                "height": review_height,
                "fps": fps,
                "video_bitrate_kbps": review_video_bitrate_kbps,
                "audio_bitrate_kbps": review_audio_bitrate_kbps,
            },
        )
        parts_dir = review_dir / ".parts"
        left_part = parts_dir / f"{junction_media_id}-left.mp4"
        right_part = parts_dir / f"{junction_media_id}-right.mp4"
        render_range(
            locator=locators[left["source_id"]],
            source_info=source_infos[left["source_id"]],
            start=float(left["source_end"]) - left_keep,
            end=float(left["source_end"]),
            destination=left_part,
            ffmpeg=ffmpeg,
            width=review_width,
            height=review_height,
            fps=fps,
            video_bitrate_kbps=review_video_bitrate_kbps,
            audio_bitrate_kbps=review_audio_bitrate_kbps,
            force=force,
            label=f"{plan['story_id']}/{review_id} left",
        )
        render_range(
            locator=locators[right["source_id"]],
            source_info=source_infos[right["source_id"]],
            start=float(right["source_start"]),
            end=float(right["source_start"]) + right_keep,
            destination=right_part,
            ffmpeg=ffmpeg,
            width=review_width,
            height=review_height,
            fps=fps,
            video_bitrate_kbps=review_video_bitrate_kbps,
            audio_bitrate_kbps=review_audio_bitrate_kbps,
            force=force,
            label=f"{plan['story_id']}/{review_id} right",
        )
        asset_path = review_dir / f"{junction_media_id}.mp4"
        concat_media(
            [left_part, right_part],
            asset_path,
            ffmpeg=ffmpeg,
            force=force,
            label=f"{plan['story_id']}/{review_id} junction",
        )
        asset_probe = probe_media(
            str(asset_path),
            ffprobe=ffprobe,
            label=f"{plan['story_id']}/{review_id} junction",
        )
        edge = edges.get((left["_block_id"], right["_block_id"]))
        edge_id = (
            edge["id"]
            if edge is not None
            else f"within-{left['_block_id']}-{left['id']}--{right['id']}"
        )
        context_path = context_dir / f"{review_id}.json"
        context = {
            "schema_version": "1.0",
            "story_id": plan["story_id"],
            "review_id": review_id,
            "review_kind": "junction",
            "duration_seconds": asset_probe["duration_seconds"],
            "cut_at_seconds": rounded(left_keep),
            "title": plan["title"],
            "production_slot": plan["production_slot"],
            "junction_input_id": junction_media_id,
            "block_ids": list(
                dict.fromkeys([left["_block_id"], right["_block_id"]])
            ),
            "clip_ids": [left["id"], right["id"]],
            "edge_ids": [edge_id],
            "temporal_relation": (
                edge["temporal_relation"] if edge is not None else "continuation"
            ),
            "orientation_required": (
                edge["orientation_required"] if edge is not None else False
            ),
            "orientation_strategy": (
                edge["orientation_strategy"] if edge is not None else "none"
            ),
            "left_clip": {
                key: value
                for key, value in left.items()
                if not key.startswith("_")
            },
            "right_clip": {
                key: value
                for key, value in right.items()
                if not key.startswith("_")
            },
            "checks_requested": ["flow", "cut_safety"],
            "local_audio_boundary_authoritative": True,
            "story_plan_qc_admission": qc_admission,
            "instruction": (
                f"代理视频约在 {left_keep:.3f} 秒处从前一 Clip 硬切到后一 Clip。"
                "吞字、词音节和说话切断由本地双路 VAD 门禁判定；"
                "这里只检查叙事衔接和画面动作/反应连续性。"
            ),
        }
        atomic_write_json(context_path, context)
        review_assets.append(
            review_asset(
                review_id=review_id,
                kind="junction",
                path=asset_path,
                duration_seconds=asset_probe["duration_seconds"],
                cut_at_seconds=left_keep,
                context_path=context_path,
                block_ids=context["block_ids"],
                clip_ids=context["clip_ids"],
                edge_ids=[edge_id],
            )
        )
        jobs.append(
            {
                "id": review_id,
                "task": "story_video_qc",
                "stage_version": "story-video-qc-v4-dynamic-schema",
                "story_id": plan["story_id"],
                "review_kind": "junction",
                "response_format": story_video_qc_response_format(
                    story_id=plan["story_id"],
                    review_id=review_id,
                    review_kind="junction",
                ),
                "context_file": str(context_path),
                "media_file": str(asset_path),
                "output": str(
                    (
                        job_root
                        / "story-qc-video-results"
                        / f"{review_id}.json"
                    ).resolve()
                ),
            }
        )
    manifest = {
        "schema_version": "1.0",
        "method": "hard-cut-qc-proxy-v1",
        "story_id": plan["story_id"],
        "title": plan["title"],
        "production_slot": plan["production_slot"],
        "story_plan_qc_admission": qc_admission,
        "input_fingerprints": {
            "story_plan_index_sha256": plan_index_sha256,
            "story_plan_sha256": plan_sha256,
            "story_script_sha256": script_sha256,
            "source_manifest_sha256": source_manifest_sha256,
        },
        "profile": {
            "width": width,
            "height": height,
            "fps": fps,
            "video_bitrate_kbps": video_bitrate_kbps,
            "audio_bitrate_kbps": audio_bitrate_kbps,
            "junction_handle_seconds": junction_handle_seconds,
        },
        "story_proxy": story_proxy,
        "clips": proxy_clips,
        "review_assets": review_assets,
    }
    errors = validate_task_response("story_qc_proxy_manifest", manifest)
    if errors:
        raise ValueError(
            f"{plan['story_id']}: invalid QC proxy manifest: "
            + "; ".join(errors[:40])
        )
    manifest_path = cache_dir / "proxy-manifest.json"
    atomic_write_json(manifest_path, manifest)
    return manifest_path, jobs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_root", type=Path)
    parser.add_argument("--backend", choices=("qwen", "doubao"), default="qwen")
    parser.add_argument(
        "--story-plan-index",
        type=Path,
        help=(
            "Use an explicitly selected Story Plan Index instead of the "
            "job's active story-plans/index.json. The selected index and all "
            "referenced plans remain hash-bound in the QC batch."
        ),
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--width", type=int, default=360)
    parser.add_argument("--height", type=int, default=640)
    parser.add_argument("--fps", type=float, default=25)
    parser.add_argument("--video-bitrate-kbps", type=int, default=180)
    parser.add_argument("--audio-bitrate-kbps", type=int, default=48)
    parser.add_argument("--review-width", type=int, default=540)
    parser.add_argument("--review-height", type=int, default=960)
    parser.add_argument("--review-video-bitrate-kbps", type=int, default=900)
    parser.add_argument("--review-audio-bitrate-kbps", type=int, default=96)
    parser.add_argument("--junction-handle-seconds", type=float, default=4.0)
    parser.add_argument("--local-audio-source-manifest", type=Path)
    parser.add_argument("--story-plan-qc-admission", type=Path)
    parser.add_argument("--audio-boundary-python", type=Path)
    parser.add_argument("--audio-boundary-script", type=Path)
    parser.add_argument("--audio-boundary-cache-dir", type=Path)
    parser.add_argument("--audio-device", choices=("cpu", "cuda", "mps"), default="cpu")
    parser.add_argument("--audio-workers", type=int, default=3)
    parser.add_argument("--disable-auto-audio-repair", action="store_true")
    parser.add_argument(
        "--audio-repair-max-rounds",
        type=int,
        default=MAX_REPAIR_ROUNDS,
    )
    parser.add_argument(
        "--audio-repair-max-adjustment-seconds",
        type=float,
        default=MAX_AUTO_ADJUSTMENT_SECONDS,
    )
    parser.add_argument("--force-audio-boundary", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Allow QC to proceed when only a subset of approved Stories "
            "have Story Plans. Missing Plans are recorded as warnings."
        ),
    )
    args = parser.parse_args()
    for executable in (args.ffmpeg, args.ffprobe):
        if shutil.which(executable) is None:
            parser.error(f"executable not found: {executable}")
    if min(args.width, args.height, args.review_width, args.review_height) < 64:
        parser.error("proxy dimensions must be at least 64")
    if args.fps < 1:
        parser.error("--fps must be at least 1")
    if min(
        args.video_bitrate_kbps,
        args.audio_bitrate_kbps,
        args.review_video_bitrate_kbps,
        args.review_audio_bitrate_kbps,
    ) <= 0:
        parser.error("bitrates must be positive")
    if args.junction_handle_seconds < 0.25:
        parser.error("QC junction handles must be at least 0.25 seconds")
    if not 1 <= args.audio_workers <= 4:
        parser.error("--audio-workers must be in 1..4")
    if not 1 <= args.audio_repair_max_rounds <= MAX_REPAIR_ROUNDS:
        parser.error(
            f"--audio-repair-max-rounds must be in 1..{MAX_REPAIR_ROUNDS}"
        )
    if not (
        0
        < args.audio_repair_max_adjustment_seconds
        <= MAX_AUTO_ADJUSTMENT_SECONDS
    ):
        parser.error(
            "--audio-repair-max-adjustment-seconds must be in "
            f"(0, {MAX_AUTO_ADJUSTMENT_SECONDS}]"
        )
    job_root = args.job_root.expanduser().resolve()
    base_plan_index_path = (
        args.story_plan_index.expanduser().resolve()
        if args.story_plan_index
        else job_root / "story-plans" / "index.json"
    )
    if args.story_plan_index:
        if not base_plan_index_path.is_file():
            raise ValueError(
                f"selected Story Plan Index is missing: {base_plan_index_path}"
            )
        selected_index = load_json(base_plan_index_path)
        selected_errors: list[str] = []
        if selected_index.get("method") != "continuity-first-story-plan-replan-v1":
            selected_errors.extend(
                validate_task_response("story_plan_index", selected_index)
            )
        if not isinstance(selected_index.get("plans"), list) or not selected_index[
            "plans"
        ]:
            selected_errors.append("selected Story Plan Index must contain plans")
        for item in selected_index.get("plans", []):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            plan_path = Path(item["path"]).expanduser().resolve()
            if not plan_path.is_file():
                selected_errors.append(
                    f"missing selected Story Plan: {plan_path}"
                )
                continue
            if item.get("plan_sha256") != sha256_file(plan_path):
                selected_errors.append(
                    f"selected Story Plan SHA-256 is stale: {plan_path}"
                )
            selected_errors.extend(
                f"{item.get('story_id', '<unknown>')}.plan: {error}"
                for error in validate_task_response(
                    "story_plan", load_json(plan_path)
                )
            )
        if selected_errors:
            raise ValueError(
                "Selected Story Plans are invalid: "
                + "; ".join(selected_errors[:30])
            )
    else:
        plan_report = validate_story_plans(
            job_root, allow_partial=args.allow_partial
        )
        if not plan_report["ok"]:
            raise ValueError(
                "Story Plans are invalid: "
                + "; ".join(plan_report["errors"][:30])
            )
    plan_index_path = base_plan_index_path
    approval_path = job_root / "story-approval.json"
    source_manifest_path = job_root / "source_manifest.json"
    plan_index = load_json(plan_index_path)
    approval = load_json(approval_path)
    source_manifest = load_json(source_manifest_path)
    default_local_source_manifest = job_root / "local-source-manifest.json"
    local_audio_source_manifest = (
        args.local_audio_source_manifest.expanduser().resolve()
        if args.local_audio_source_manifest
        else (
            default_local_source_manifest
            if default_local_source_manifest.is_file()
            else None
        )
    )
    blocked_entries = [
        item
        for item in plan_index.get("plans", [])
        if isinstance(item, dict) and item.get("status") == "blocked"
    ]
    unsupported_entries = [
        item
        for item in plan_index.get("plans", [])
        if isinstance(item, dict)
        and item.get("status") not in {"ready_for_video_qc", "blocked"}
    ]
    if unsupported_entries:
        raise ValueError("Story Plan Index contains unsupported statuses")
    admission_path = (
        args.story_plan_qc_admission.expanduser().resolve()
        if args.story_plan_qc_admission
        else job_root / "story-plan-qc-admission.json"
    )
    admission_entries: dict[str, dict[str, Any]] = {}
    admission_sha256: str | None = None
    if blocked_entries:
        if not admission_path.is_file():
            raise ValueError(
                "blocked Story Plans require story-plan-qc-admission.json"
            )
        _, admission_entries, admission_errors = validate_admission(
            job_root,
            admission_path,
        )
        if admission_errors:
            raise ValueError(
                "invalid Story Plan QC admission: "
                + "; ".join(admission_errors[:30])
            )
        unaccepted = [
            item["story_id"]
            for item in blocked_entries
            if admission_entries.get(item["story_id"], {}).get("decision")
            != ACCEPTED
        ]
        if unaccepted:
            raise ValueError(
                "blocked Story Plans lack human QC admission: "
                + ", ".join(sorted(unaccepted))
            )
        admission_sha256 = sha256_file(admission_path)
    elif admission_path.is_file():
        _, admission_entries, admission_errors = validate_admission(
            job_root,
            admission_path,
        )
        if admission_errors:
            raise ValueError(
                "invalid Story Plan QC admission: "
                + "; ".join(admission_errors[:30])
            )
        admission_sha256 = sha256_file(admission_path)
    if any(
        item.get("status") != "ready_for_video_qc"
        for item in plan_index.get("plans", [])
        if isinstance(item, dict)
    ) and not blocked_entries:
        raise ValueError("Story Plans cannot enter Story QC")
    approval_entries = {
        item["story_id"]: item
        for item in approval.get("stories", [])
        if isinstance(item, dict) and item.get("decision") == "approved"
    }
    required_source_ids: set[str] = set()
    for entry in plan_index.get("plans", []):
        if not isinstance(entry, dict) or not isinstance(
            entry.get("path"), str
        ):
            continue
        plan = load_json(Path(entry["path"]).expanduser().resolve())
        required_source_ids.update(
            clip["source_id"]
            for block in plan.get("blocks", [])
            if isinstance(block, dict)
            for clip in block.get("clips", [])
            if isinstance(clip, dict) and isinstance(clip.get("source_id"), str)
        )
    if not required_source_ids:
        raise ValueError("Story Plans do not select any Sources")
    locators = source_locators(
        job_root,
        source_manifest,
        required_source_ids=required_source_ids,
        local_source_manifest=(
            load_json(local_audio_source_manifest)
            if local_audio_source_manifest is not None
            else None
        ),
    )
    source_infos = {
        source_id: probe_media(
            locator,
            ffprobe=args.ffprobe,
            label=source_id,
        )
        for source_id, locator in locators.items()
    }
    plan_index_sha256 = sha256_file(plan_index_path)
    source_manifest_sha256 = sha256_file(source_manifest_path)
    (
        plan_index_path,
        audio_boundary,
        boundary_repair,
    ) = prepare_audio_boundary_with_repair(
        job_root,
        base_plan_index_path=base_plan_index_path,
        local_source_manifest_path=local_audio_source_manifest,
        audio_python=(
            args.audio_boundary_python.expanduser().resolve()
            if args.audio_boundary_python
            else job_root / ".venv-audio-boundary" / "bin" / "python"
        ),
        audio_guard_script=(
            args.audio_boundary_script.expanduser().resolve()
            if args.audio_boundary_script
            else audio_guard_default()
        ),
        cache_dir=(
            args.audio_boundary_cache_dir.expanduser().resolve()
            if args.audio_boundary_cache_dir
            else job_root / ".audio-boundary-cache"
        ),
        device=args.audio_device,
        force=args.force_audio_boundary,
        workers=args.audio_workers,
        auto_repair=not args.disable_auto_audio_repair,
        max_repair_rounds=args.audio_repair_max_rounds,
        max_adjustment_seconds=args.audio_repair_max_adjustment_seconds,
        include_blocked=bool(blocked_entries),
    )
    plan_index = load_json(plan_index_path)
    plan_index_sha256 = sha256_file(plan_index_path)
    manifests: list[str] = []
    jobs: list[dict[str, Any]] = []
    for entry in sorted(
        plan_index["plans"], key=lambda item: item["production_slot"]
    ):
        approval_entry = approval_entries.get(entry["story_id"])
        if approval_entry is None:
            raise ValueError(
                f"{entry['story_id']}: Story Plan is not currently approved"
            )
        manifest_path, story_jobs = prepare_story(
            job_root=job_root,
            plan_index_sha256=plan_index_sha256,
            plan_entry=entry,
            approval_entry=approval_entry,
            admission_entry=admission_entries.get(entry["story_id"]),
            admission_path=(
                admission_path if admission_sha256 is not None else None
            ),
            admission_sha256=admission_sha256,
            source_manifest=source_manifest,
            source_manifest_sha256=source_manifest_sha256,
            locators=locators,
            source_infos=source_infos,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
            width=args.width,
            height=args.height,
            fps=args.fps,
            video_bitrate_kbps=args.video_bitrate_kbps,
            audio_bitrate_kbps=args.audio_bitrate_kbps,
            review_width=args.review_width,
            review_height=args.review_height,
            review_video_bitrate_kbps=args.review_video_bitrate_kbps,
            review_audio_bitrate_kbps=args.review_audio_bitrate_kbps,
            junction_handle_seconds=args.junction_handle_seconds,
            force=args.force,
        )
        manifests.append(str(manifest_path))
        jobs.extend(story_jobs)
    if not jobs:
        raise ValueError("Story QC has no video review jobs")
    batch = {
        "schema_version": "1.0",
        "backend": args.backend,
        "cache_dir": str((job_root / ".story-cache").resolve()),
        "stage_version": "story-qc-v4-dynamic-schema",
        "story_plan_index_path": str(plan_index_path),
        "story_plan_index_sha256": plan_index_sha256,
        "base_story_plan_index_path": str(base_plan_index_path),
        "base_story_plan_index_sha256": sha256_file(base_plan_index_path),
        "source_manifest_sha256": source_manifest_sha256,
        "audio_boundary": audio_boundary,
        "boundary_repair": boundary_repair,
        "story_plan_qc_admission_path": (
            str(admission_path) if admission_sha256 is not None else None
        ),
        "story_plan_qc_admission_sha256": admission_sha256,
        "proxy_manifests": manifests,
        "jobs": jobs,
    }
    batch_path = job_root / "story-qc-batch.json"
    atomic_write_json(batch_path, batch)
    update_project_stage(
        job_root / "project.json",
        "story_qc_prepare",
        "ready",
        inputs={
            "base_story_plan_index": str(base_plan_index_path),
            "effective_story_plan_index": str(plan_index_path),
            "source_manifest": str(source_manifest_path),
        },
        outputs={
            "story_qc_batch": str(batch_path),
            "proxy_manifest_count": str(len(manifests)),
            "video_review_job_count": str(len(jobs)),
            "boundary_repair_status": boundary_repair["status"],
            "boundary_repair_change_count": str(
                boundary_repair["applied_change_count"]
            ),
        },
    )
    print(f"STORY_QC_BATCH\t{batch_path}")
    print(f"PROXY_MANIFESTS\t{len(manifests)}")
    print(f"VIDEO_REVIEW_JOBS\t{len(jobs)}")
    print(
        "LOCAL_AUDIO_BOUNDARY\t"
        f"{audio_boundary['status']}\t{audio_boundary['report_path']}"
    )
    print(
        "BOUNDARY_REPAIR\t"
        f"{boundary_repair['status']}\t"
        f"changes={boundary_repair['applied_change_count']}\t"
        f"rounds={boundary_repair['applied_repair_round_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
