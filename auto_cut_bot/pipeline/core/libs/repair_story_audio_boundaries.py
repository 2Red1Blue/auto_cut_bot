#!/usr/bin/env python3
"""Create immutable Story Plan versions for locally detected speech cuts."""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
from pathlib import Path
from statistics import median
from typing import Any

from autocut_core.libs.editorial_plan import (
    build_source_usage,
    unique_duration_for_records,
)
from autocut_core.io import atomic_write_json, load_json, sha256_file
from autocut_core.schema.compat import validate_task_response


METHOD = "local-audio-boundary-repair-v1"
MAX_REPAIR_ROUNDS = 2
MAX_AUTO_ADJUSTMENT_SECONDS = 7.0
SAFE_AUDIO_STATUSES = {
    "safe",
    "safe_source_edge",
    "not_applicable_no_audio",
}


def rounded(value: float) -> float:
    return round(float(value), 3)


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip("-.")
    return cleaned or "story"


def ordered_clips(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        clip
        for block in sorted(
            plan["blocks"], key=lambda item: item["play_order"]
        )
        for clip in block["clips"]
    ]


def clips_by_id(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in ordered_clips(plan)}


def overlap_seconds(left: dict[str, Any], right: dict[str, Any]) -> float:
    if left["source_id"] != right["source_id"]:
        return 0.0
    return rounded(
        max(
            0.0,
            min(float(left["source_end"]), float(right["source_end"]))
            - max(
                float(left["source_start"]),
                float(right["source_start"]),
            ),
        )
    )


def illegal_overlap_errors(plan: dict[str, Any]) -> list[str]:
    clips = ordered_clips(plan)
    errors: list[str] = []
    for index, left in enumerate(clips):
        for right in clips[index + 1 :]:
            overlap = overlap_seconds(left, right)
            if overlap <= 0:
                continue
            legal_reprise = (
                left.get("reuse_mode") == "teaser_reprise"
                or right.get("reuse_mode") == "teaser_reprise"
            )
            if not legal_reprise:
                errors.append(
                    f"{left['id']}/{right['id']}: repair creates "
                    f"{overlap:.3f}s of unapproved source overlap"
                )
    return errors


def recompute_plan(plan: dict[str, Any], *, repair_round: int) -> None:
    clips = ordered_clips(plan)
    for clip in clips:
        start = float(clip["source_start"])
        end = float(clip["source_end"])
        source_duration = float(clip["source_duration_seconds"])
        clip["source_start"] = rounded(start)
        clip["source_end"] = rounded(end)
        clip["duration_seconds"] = rounded(end - start)
        ratio = rounded((end - start) / source_duration)
        clip["source_coverage_ratio"] = min(1.0, max(0.0, ratio))
        clip["full_source_like"] = ratio >= 0.85
        clip["boundary_status"] = "needs_video_review"
    playback = rounded(sum(item["duration_seconds"] for item in clips))
    unique = unique_duration_for_records(clips)
    repeated = rounded(max(0.0, playback - unique))
    repeat_ratio = rounded(repeated / playback) if playback else 0.0
    full_source_like = [item for item in clips if item["full_source_like"]]
    full_duration = rounded(
        sum(item["duration_seconds"] for item in full_source_like)
    )
    full_ratio = rounded(full_duration / playback) if playback else 0.0
    teaser_clips = (
        plan["blocks"][0]["clips"]
        if plan["blocks"] and plan["blocks"][0]["role"] == "teaser"
        else []
    )
    teaser_duration = rounded(
        sum(item["duration_seconds"] for item in teaser_clips)
    )
    metrics = plan["editorial_metrics"]
    metrics.update(
        {
            "playback_duration_seconds": playback,
            "unique_source_duration_seconds": unique,
            "repeated_source_duration_seconds": repeated,
            "repeat_ratio": min(1.0, repeat_ratio),
            "full_source_like_clip_count": len(full_source_like),
            "full_source_like_playback_duration_seconds": full_duration,
            "full_source_like_playback_ratio": min(1.0, full_ratio),
            "teaser_duration_seconds": teaser_duration,
            "clip_count": len(clips),
            "median_clip_duration_seconds": (
                rounded(median(item["duration_seconds"] for item in clips))
                if clips
                else 0.0
            ),
        }
    )
    plan["estimated_duration_seconds"] = playback
    plan["source_usage"] = build_source_usage(clips)
    risk = (
        f"本地音频 Boundary Repair 第 {repair_round} 轮已向外扩展切点；"
        "修复范围必须重新执行音频与 Junction QC。"
    )
    if risk not in plan["planning_risks"]:
        plan["planning_risks"].append(risk)


def plan_constraint_errors(plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    clips = ordered_clips(plan)
    for clip in clips:
        start = float(clip["source_start"])
        end = float(clip["source_end"])
        source_duration = float(clip["source_duration_seconds"])
        if start < 0 or end <= start or end > source_duration + 0.001:
            errors.append(f"{clip['id']}: repaired source range is invalid")
    duration = float(plan["estimated_duration_seconds"])
    contract = plan["duration_contract"]
    if duration < float(contract["minimum_seconds"]):
        errors.append("repair would move Story below its duration minimum")
    if duration > float(contract["maximum_seconds"]):
        errors.append("repair would move Story above its duration maximum")
    metrics = plan["editorial_metrics"]
    if float(metrics["teaser_duration_seconds"]) > 30.0 + 0.001:
        errors.append("repair would move Teaser above the 30s hard limit")
    errors.extend(illegal_overlap_errors(plan))
    errors.extend(validate_task_response("story_plan", plan))
    return errors


def unresolved_record(
    item: dict[str, Any],
    *,
    route: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "boundary_id": item.get("id"),
        "story_id": item.get("output_id"),
        "clip_id": item.get("segment_id"),
        "source_id": item.get("source_id"),
        "boundary": item.get("boundary"),
        "status": item.get("status"),
        "route": route,
        "reason": reason,
    }


def proposed_change(
    item: dict[str, Any],
    *,
    max_adjustment_seconds: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if item.get("status") == "safe_source_edge" and item.get(
        "speech_active_at_cut"
    ):
        return None, unresolved_record(
            item,
            route="source_edge_review",
            reason=(
                "物理源边缘已有活跃语音，没有可向外恢复的本源音频，"
                "必须人工听审或验证相邻 Source 连续性。"
            ),
        )
    if item.get("status") in SAFE_AUDIO_STATUSES:
        return None, None
    if item.get("status") != "adjustment_required":
        return None, unresolved_record(
            item,
            route="blocked_replan",
            reason="音频门禁没有提供可自动应用的安全边界。",
        )
    before = item.get("planned_source_seconds")
    after = item.get("recommended_source_seconds")
    if not all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        for value in (before, after)
    ):
        return None, unresolved_record(
            item,
            route="blocked_replan",
            reason="音频门禁没有提供有限的建议时间码。",
        )
    before_value = float(before)
    after_value = float(after)
    adjustment = after_value - before_value
    boundary = item.get("boundary")
    outward = (
        boundary == "source_start" and adjustment < -0.0005
    ) or (
        boundary == "source_end" and adjustment > 0.0005
    )
    if not outward:
        return None, unresolved_record(
            item,
            route="blocked_replan",
            reason="建议时间码不是向外扩边，禁止通过继续裁短掩盖吞音。",
        )
    if abs(adjustment) > max_adjustment_seconds + 0.0005:
        return None, unresolved_record(
            item,
            route="blocked_replan",
            reason=(
                f"建议调整 {adjustment:+.3f}s 超过 "
                f"{max_adjustment_seconds:.3f}s 自动修复上限。"
            ),
        )
    return (
        {
            "boundary_id": item["id"],
            "story_id": item["output_id"],
            "clip_id": item["segment_id"],
            "source_id": item["source_id"],
            "boundary": boundary,
            "before_seconds": rounded(before_value),
            "after_seconds": rounded(after_value),
            "adjustment_seconds": rounded(adjustment),
            "speech_interval": item.get("speech_interval"),
            "reason": item.get("reason", ""),
            "strategy": "extend_to_nearest_safe_silence",
        },
        None,
    )


def apply_change(
    plan: dict[str, Any],
    change: dict[str, Any],
    *,
    repair_round: int,
) -> list[str]:
    clip = clips_by_id(plan).get(change["clip_id"])
    if clip is None:
        return [f"{change['clip_id']}: repaired Clip is missing"]
    if clip.get("source_id") != change.get("source_id"):
        return [f"{change['clip_id']}: repaired Source identity changed"]
    boundary = change["boundary"]
    current = float(clip[boundary])
    if abs(current - float(change["before_seconds"])) > 0.001:
        return [f"{change['clip_id']}: repaired parent boundary is stale"]
    clip[boundary] = rounded(change["after_seconds"])
    recompute_plan(plan, repair_round=repair_round)
    return plan_constraint_errors(plan)


def entry_history(entry: dict[str, Any]) -> list[dict[str, str]]:
    value = entry.get("repair_history", [])
    return [
        {"path": item["path"], "sha256": item["sha256"]}
        for item in value
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and isinstance(item.get("sha256"), str)
    ]


def active_entry(
    base_entry: dict[str, Any],
    current_entry: dict[str, Any],
    *,
    plan_path: Path,
    plan: dict[str, Any],
    repair_round: int,
    history: list[dict[str, str]],
    unresolved: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "story_id": plan["story_id"],
        "title": plan["title"],
        "production_slot": plan["production_slot"],
        "status": plan["status"],
        "path": str(plan_path),
        "plan_sha256": sha256_file(plan_path),
        "story_script_sha256": base_entry["story_script_sha256"],
        "span_candidate_bundle_sha256": base_entry[
            "span_candidate_bundle_sha256"
        ],
        "selection_result_sha256": base_entry["selection_result_sha256"],
        "estimated_duration_seconds": plan["estimated_duration_seconds"],
        "block_count": len(plan["blocks"]),
        "clip_count": len(ordered_clips(plan)),
        "base_plan_path": base_entry["path"],
        "base_plan_sha256": base_entry["plan_sha256"],
        "repair_round": repair_round,
        "repair_history": history,
        "unresolved_boundary_ids": [
            item["boundary_id"] for item in unresolved
        ],
    }


def create_repair_round(
    job_root: Path,
    *,
    base_plan_index_path: Path,
    active_plan_index_path: Path,
    audio_report_path: Path,
    repair_round: int,
    max_rounds: int = MAX_REPAIR_ROUNDS,
    max_adjustment_seconds: float = MAX_AUTO_ADJUSTMENT_SECONDS,
) -> dict[str, Any]:
    if not 1 <= repair_round <= max_rounds:
        raise ValueError("repair round is outside configured limits")
    base_index = load_json(base_plan_index_path)
    active_index = load_json(active_plan_index_path)
    audio_report = load_json(audio_report_path)
    base_entries = {
        item["story_id"]: item for item in base_index["plans"]
    }
    current_entries = {
        item["story_id"]: item for item in active_index["plans"]
    }
    records_by_story: dict[str, list[dict[str, Any]]] = {}
    for item in audio_report.get("boundaries", []):
        if isinstance(item, dict) and isinstance(item.get("output_id"), str):
            records_by_story.setdefault(item["output_id"], []).append(item)
    repair_root = job_root / "story-plan-repairs"
    index_entries: list[dict[str, Any]] = []
    applied_patches: list[dict[str, Any]] = []
    unresolved_all: list[dict[str, Any]] = []
    for story_id, current_entry in sorted(
        current_entries.items(),
        key=lambda pair: pair[1]["production_slot"],
    ):
        base_entry = base_entries.get(story_id)
        if base_entry is None:
            raise ValueError(f"{story_id}: active plan has no base plan")
        parent_path = Path(current_entry["path"]).expanduser().resolve()
        parent_plan = load_json(parent_path)
        working = copy.deepcopy(parent_plan)
        changes: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for item in records_by_story.get(story_id, []):
            change, issue = proposed_change(
                item,
                max_adjustment_seconds=max_adjustment_seconds,
            )
            if issue is not None:
                unresolved.append(issue)
                continue
            if change is None:
                continue
            tentative = copy.deepcopy(working)
            errors = apply_change(
                tentative,
                change,
                repair_round=repair_round,
            )
            if errors:
                unresolved.append(
                    unresolved_record(
                        item,
                        route="blocked_replan",
                        reason="自动扩边违反 Story Plan 硬约束：" + "; ".join(
                            errors[:8]
                        ),
                    )
                )
                continue
            working = tentative
            changes.append(change)
        history = entry_history(current_entry)
        effective_path = parent_path
        effective_round = int(current_entry.get("repair_round", 0))
        if changes:
            story_root = repair_root / safe_name(story_id)
            effective_path = (
                story_root / f"round-{repair_round:02d}.plan.json"
            )
            atomic_write_json(effective_path, working)
            patch_path = (
                story_root / f"round-{repair_round:02d}.patch.json"
            )
            patch = {
                "schema_version": "1.0",
                "method": METHOD,
                "story_id": story_id,
                "repair_round": repair_round,
                "max_rounds": max_rounds,
                "max_auto_adjustment_seconds": max_adjustment_seconds,
                "parent_plan_path": str(parent_path),
                "parent_plan_sha256": sha256_file(parent_path),
                "audio_report_path": str(audio_report_path),
                "audio_report_sha256": sha256_file(audio_report_path),
                "changes": changes,
                "unresolved": unresolved,
                "result_plan_path": str(effective_path),
                "result_plan_sha256": sha256_file(effective_path),
            }
            atomic_write_json(patch_path, patch)
            history = [
                *history,
                {"path": str(patch_path), "sha256": sha256_file(patch_path)},
            ]
            effective_round = repair_round
            applied_patches.append(
                {
                    "story_id": story_id,
                    "path": str(patch_path),
                    "sha256": sha256_file(patch_path),
                    "change_count": len(changes),
                }
            )
        effective_plan = load_json(effective_path)
        index_entries.append(
            active_entry(
                base_entry,
                current_entry,
                plan_path=effective_path,
                plan=effective_plan,
                repair_round=effective_round,
                history=history,
                unresolved=unresolved,
            )
        )
        unresolved_all.extend(unresolved)
    ready_count = sum(
        item["status"] == "ready_for_video_qc" for item in index_entries
    )
    blocked_count = len(index_entries) - ready_count
    status = (
        "ready_for_video_qc"
        if index_entries and ready_count == len(index_entries)
        else "partially_ready"
        if ready_count
        else "blocked"
    )
    repair_status = (
        "blocked_replan"
        if any(item["route"] == "blocked_replan" for item in unresolved_all)
        else "review"
        if unresolved_all
        else "patched"
        if applied_patches
        else "not_needed"
    )
    repair_index = {
        "schema_version": "1.0",
        "method": METHOD,
        "status": status,
        "repair_status": repair_status,
        "base_story_plan_index_path": str(base_plan_index_path),
        "base_story_plan_index_sha256": sha256_file(base_plan_index_path),
        "parent_plan_index_path": str(active_plan_index_path),
        "parent_plan_index_sha256": sha256_file(active_plan_index_path),
        "audio_report_path": str(audio_report_path),
        "audio_report_sha256": sha256_file(audio_report_path),
        "repair_round": repair_round,
        "max_rounds": max_rounds,
        "max_auto_adjustment_seconds": max_adjustment_seconds,
        "plan_count": len(index_entries),
        "ready_plan_count": ready_count,
        "blocked_plan_count": blocked_count,
        "applied_patch_count": len(applied_patches),
        "applied_change_count": sum(
            item["change_count"] for item in applied_patches
        ),
        "unresolved_boundary_count": len(unresolved_all),
        "applied_patches": applied_patches,
        "unresolved": unresolved_all,
        "plans": index_entries,
    }
    index_path = repair_root / f"round-{repair_round:02d}.index.json"
    atomic_write_json(index_path, repair_index)
    errors = validate_repair_index(index_path, base_plan_index_path)
    if errors:
        raise ValueError(
            "invalid Story Boundary Repair Index: " + "; ".join(errors[:30])
        )
    return {
        "changed": bool(applied_patches),
        "index_path": str(index_path),
        "index_sha256": sha256_file(index_path),
        "repair_status": repair_status,
        "applied_patch_count": len(applied_patches),
        "applied_change_count": repair_index["applied_change_count"],
        "unresolved": unresolved_all,
    }


def replay_patch(
    parent: dict[str, Any],
    patch: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    working = copy.deepcopy(parent)
    errors: list[str] = []
    for change in patch.get("changes", []):
        if not isinstance(change, dict):
            errors.append("repair patch contains a non-object change")
            continue
        errors.extend(
            apply_change(
                working,
                change,
                repair_round=int(patch["repair_round"]),
            )
        )
    return working, errors


def validate_repair_index(
    index_path: Path,
    base_plan_index_path: Path,
) -> list[str]:
    errors: list[str] = []
    if not index_path.is_file():
        return [f"missing Story Boundary Repair Index: {index_path}"]
    if not base_plan_index_path.is_file():
        return [f"missing base Story Plan Index: {base_plan_index_path}"]
    index = load_json(index_path)
    base_index = load_json(base_plan_index_path)
    if index.get("method") != METHOD:
        errors.append("Story Boundary Repair method is invalid")
    if index.get("base_story_plan_index_sha256") != sha256_file(
        base_plan_index_path
    ):
        errors.append("Story Boundary Repair base Plan Index is stale")
    parent_index_value = index.get("parent_plan_index_path")
    parent_index_path = (
        Path(parent_index_value).expanduser().resolve()
        if isinstance(parent_index_value, str)
        else None
    )
    if (
        parent_index_path is None
        or not parent_index_path.is_file()
        or sha256_file(parent_index_path)
        != index.get("parent_plan_index_sha256")
    ):
        errors.append("Story Boundary Repair parent Plan Index is stale")
    audio_report_value = index.get("audio_report_path")
    audio_report_path = (
        Path(audio_report_value).expanduser().resolve()
        if isinstance(audio_report_value, str)
        else None
    )
    if (
        audio_report_path is None
        or not audio_report_path.is_file()
        or sha256_file(audio_report_path)
        != index.get("audio_report_sha256")
    ):
        errors.append("Story Boundary Repair audio report is stale")
    repair_round = index.get("repair_round")
    if (
        not isinstance(repair_round, int)
        or isinstance(repair_round, bool)
        or not 1 <= repair_round <= MAX_REPAIR_ROUNDS
    ):
        errors.append("Story Boundary Repair round is invalid")
    base_entries = {
        item["story_id"]: item for item in base_index.get("plans", [])
    }
    entries = {
        item["story_id"]: item
        for item in index.get("plans", [])
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }
    if set(entries) != set(base_entries):
        errors.append("Story Boundary Repair plans do not match base plans")
    for story_id, entry in entries.items():
        base_entry = base_entries.get(story_id)
        if base_entry is None:
            continue
        base_path = Path(base_entry["path"]).expanduser().resolve()
        if sha256_file(base_path) != base_entry["plan_sha256"]:
            errors.append(f"{story_id}: base Story Plan is stale")
            continue
        if (
            entry.get("base_plan_path") != str(base_path)
            or entry.get("base_plan_sha256") != sha256_file(base_path)
        ):
            errors.append(f"{story_id}: base Story Plan binding is stale")
        working = load_json(base_path)
        parent_path = base_path
        parent_sha256 = sha256_file(base_path)
        for history_item in entry_history(entry):
            patch_path = Path(history_item["path"]).expanduser().resolve()
            if (
                not patch_path.is_file()
                or sha256_file(patch_path) != history_item["sha256"]
            ):
                errors.append(f"{story_id}: repair patch is stale")
                continue
            patch = load_json(patch_path)
            patch_audio_value = patch.get("audio_report_path")
            patch_audio_path = (
                Path(patch_audio_value).expanduser().resolve()
                if isinstance(patch_audio_value, str)
                else None
            )
            if (
                patch_audio_path is None
                or not patch_audio_path.is_file()
                or sha256_file(patch_audio_path)
                != patch.get("audio_report_sha256")
            ):
                errors.append(f"{story_id}: repair audio evidence is stale")
            if (
                Path(str(patch.get("parent_plan_path", "")))
                .expanduser()
                .resolve()
                != parent_path
            ):
                errors.append(f"{story_id}: repair parent path is stale")
            if patch.get("parent_plan_sha256") != parent_sha256:
                errors.append(f"{story_id}: repair parent chain is stale")
            replayed, replay_errors = replay_patch(working, patch)
            errors.extend(
                f"{story_id}: {item}" for item in replay_errors
            )
            result_path = Path(
                patch["result_plan_path"]
            ).expanduser().resolve()
            if (
                not result_path.is_file()
                or sha256_file(result_path)
                != patch.get("result_plan_sha256")
            ):
                errors.append(f"{story_id}: repaired Story Plan is stale")
                continue
            if replayed != load_json(result_path):
                errors.append(
                    f"{story_id}: repaired Story Plan is not deterministic"
                )
            working = replayed
            parent_path = result_path
            parent_sha256 = sha256_file(result_path)
        effective_path = Path(entry["path"]).expanduser().resolve()
        if effective_path != parent_path:
            errors.append(f"{story_id}: effective Story Plan path is inconsistent")
            continue
        if entry.get("plan_sha256") != parent_sha256:
            errors.append(f"{story_id}: effective Story Plan SHA-256 is stale")
        schema_errors = validate_task_response("story_plan", working)
        errors.extend(f"{story_id}.plan: {item}" for item in schema_errors)
    return errors


def resolve_qc_plan_index(
    job_root: Path,
    batch: dict[str, Any],
) -> tuple[Path, Path]:
    default_base_path = (
        job_root.expanduser().resolve() / "story-plans" / "index.json"
    )
    base_value = batch.get("base_story_plan_index_path")
    base_path = (
        Path(base_value).expanduser().resolve()
        if isinstance(base_value, str)
        else default_base_path
    )
    value = batch.get("story_plan_index_path")
    active_path = (
        Path(value).expanduser().resolve()
        if isinstance(value, str)
        else base_path
    )
    if not base_path.is_file():
        raise FileNotFoundError(f"missing base Story Plan Index: {base_path}")
    if not active_path.is_file():
        raise FileNotFoundError(
            f"missing effective Story Plan Index: {active_path}"
        )
    if batch.get("base_story_plan_index_sha256") not in {
        None,
        sha256_file(base_path),
    }:
        raise ValueError("Story QC batch uses a stale base Story Plan Index")
    if active_path != base_path:
        errors = validate_repair_index(active_path, base_path)
        if errors:
            raise ValueError(
                "invalid effective Story Plan repair chain: "
                + "; ".join(errors[:30])
            )
    repair = batch.get("boundary_repair")
    if isinstance(repair, dict):
        metadata_value = repair.get("path")
        metadata_path = (
            Path(metadata_value).expanduser().resolve()
            if isinstance(metadata_value, str)
            else None
        )
        if (
            metadata_path is None
            or not metadata_path.is_file()
            or sha256_file(metadata_path) != repair.get("sha256")
        ):
            raise ValueError("Story Boundary Repair metadata is stale")
        metadata = load_json(metadata_path)
        if metadata.get(
            "base_story_plan_index_sha256"
        ) != sha256_file(base_path):
            raise ValueError(
                "Story Boundary Repair metadata uses a stale base Plan Index"
            )
        if metadata.get(
            "effective_story_plan_index_sha256"
        ) != sha256_file(active_path):
            raise ValueError(
                "Story Boundary Repair metadata uses a stale effective Plan Index"
            )
    return base_path, active_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_root", type=Path)
    parser.add_argument("--base-plan-index", type=Path)
    parser.add_argument("--active-plan-index", type=Path)
    parser.add_argument("--audio-report", type=Path, required=True)
    parser.add_argument("--repair-round", type=int, required=True)
    parser.add_argument(
        "--max-rounds", type=int, default=MAX_REPAIR_ROUNDS
    )
    parser.add_argument(
        "--max-adjustment-seconds",
        type=float,
        default=MAX_AUTO_ADJUSTMENT_SECONDS,
    )
    args = parser.parse_args()
    if not 1 <= args.max_rounds <= MAX_REPAIR_ROUNDS:
        parser.error(f"--max-rounds must be in 1..{MAX_REPAIR_ROUNDS}")
    if not 0 < args.max_adjustment_seconds <= MAX_AUTO_ADJUSTMENT_SECONDS:
        parser.error(
            "--max-adjustment-seconds must be in "
            f"(0, {MAX_AUTO_ADJUSTMENT_SECONDS}]"
        )
    job_root = args.job_root.expanduser().resolve()
    base_index = (
        args.base_plan_index.expanduser().resolve()
        if args.base_plan_index
        else job_root / "story-plans" / "index.json"
    )
    result = create_repair_round(
        job_root,
        base_plan_index_path=base_index,
        active_plan_index_path=(
            args.active_plan_index.expanduser().resolve()
            if args.active_plan_index
            else base_index
        ),
        audio_report_path=args.audio_report.expanduser().resolve(),
        repair_round=args.repair_round,
        max_rounds=args.max_rounds,
        max_adjustment_seconds=args.max_adjustment_seconds,
    )
    print(
        "STORY_BOUNDARY_REPAIR\t"
        f"{result['repair_status']}\t{result['index_path']}"
    )
    print(f"PATCHES\t{result['applied_patch_count']}")
    print(f"CHANGES\t{result['applied_change_count']}")
    print(f"UNRESOLVED\t{len(result['unresolved'])}")
    return 0 if result["repair_status"] != "blocked_replan" else 2


if __name__ == "__main__":
    raise SystemExit(main())
