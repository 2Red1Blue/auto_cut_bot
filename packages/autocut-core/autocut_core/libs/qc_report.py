#!/usr/bin/env python3
"""QC Report assembly and validation – build_report, audio boundary loading, and plan index resolution."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from statistics import median
from typing import Any

from autocut_core.io import json_sha256, load_json, sha256_file
from autocut_core.libs._common import rounded
from autocut_core.libs.editorial_knowledge import (
    diagnostics_as_qc_checks,
    story_coherence_diagnostics,
)
from autocut_core.libs.editorial_plan import (
    build_source_usage,
    unique_duration_for_records,
)
from autocut_core.schema.compat import validate_task_response

# ---------------------------------------------------------------------------
# Constants (from story_audio_boundary_qc.py)
# ---------------------------------------------------------------------------

AUDIO_REPORT_VERSION = "1.1"
from autocut_core.libs.editorial_knowledge import load_knowledge_section

_qc = load_knowledge_section("qc_report") or {}
SAFE_AUDIO_STATUSES = frozenset(
    _qc.get("safe_audio_statuses")
    or {"safe", "safe_source_edge", "not_applicable_no_audio"}
)
KNOWN_AUDIO_STATUSES = frozenset(
    _qc.get("known_audio_statuses")
    or list(SAFE_AUDIO_STATUSES | {"adjustment_required", "blocked_replan", "analysis_error"})
)
PINNED_AUDIO_ENGINES = _qc.get("pinned_audio_engines") or {
    "demucs": "4.1.0",
    "silero-vad": "6.2.1",
    "onnxruntime": "1.24.3",
}

RESULT_RANK = _qc.get("result_rank") or {
    "not_assessed": 0,
    "pass": 0,
    "info": 0,
    "review": 1,
    "block": 2,
}
_raw_report_status = _qc.get("report_status") or {"0": "approved", "1": "review", "2": "blocked"}
REPORT_STATUS = {int(k): v for k, v in _raw_report_status.items()}

# ---------------------------------------------------------------------------
# Helpers from story_audio_boundary_qc.py
# ---------------------------------------------------------------------------


def audio_guard_default() -> Path:
    """Return the default audio boundary guard script path.

    Migrated from _legacy_v4/scripts/story_audio_boundary_qc.py.
    """
    return (
        Path(__file__).resolve().parents[3]
        / "autocut"
        / "scripts"
        / "audio_boundary_guard.py"
    )


def source_identities(plan: dict[str, Any]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for source in plan.get("sources", []):
        path = Path(source["path"]).expanduser().resolve()
        records.append(
            {
                "source_id": source["id"],
                "path": str(path),
                "sha256": sha256_file(path),
            }
        )
    return records


def plan_fingerprint(
    plan: dict[str, Any], identities: list[dict[str, str]]
) -> str:
    payload = {
        "version": plan.get("version"),
        "sources": [
            {
                "id": source.get("id"),
                "path": source.get("path"),
                "duration_seconds": source.get("duration_seconds"),
            }
            for source in plan.get("sources", [])
            if isinstance(source, dict)
        ],
        "outputs": [
            {
                "id": output.get("id"),
                "segments": [
                    {
                        "id": segment.get("id"),
                        "source_id": segment.get("source_id"),
                        "source_start": segment.get("source_start"),
                        "source_end": segment.get("source_end"),
                        "role": segment.get("role"),
                        "boundary_reason": segment.get("boundary_reason"),
                    }
                    for segment in output.get("segments", [])
                    if isinstance(segment, dict)
                ],
            }
            for output in plan.get("outputs", [])
            if isinstance(output, dict)
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def expected_boundary_keys(
    plan: dict[str, Any],
) -> dict[tuple[str, str, str], float]:
    records: dict[tuple[str, str, str], float] = {}
    for output in plan.get("outputs", []):
        for segment in output.get("segments", []):
            for boundary in ("source_start", "source_end"):
                value = segment.get(boundary)
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                ):
                    raise ValueError(
                        f"{output.get('id')}/{segment.get('id')}: invalid {boundary}"
                    )
                records[
                    (str(output.get("id")), str(segment.get("id")), boundary)
                ] = float(value)
    return records


def validate_audio_report(
    audio_plan_path: Path,
    report_path: Path,
    *,
    expected_policy: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    audio_plan = load_json(audio_plan_path)
    report = load_json(report_path)
    if report.get("version") != AUDIO_REPORT_VERSION:
        errors.append(
            f"audio boundary report version must be {AUDIO_REPORT_VERSION}"
        )
        return errors
    identities = source_identities(audio_plan)
    if (
        report.get("source_identities") is not None
        and report.get("source_identities") != identities
    ):
        errors.append("audio boundary source identities are stale")
    if report.get("plan_fingerprint") != plan_fingerprint(
        audio_plan, identities
    ):
        errors.append("audio boundary plan fingerprint is stale")
    if Path(str(report.get("plan_path", ""))).expanduser().resolve() != (
        audio_plan_path.expanduser().resolve()
    ):
        errors.append("audio boundary plan path does not match")
    if report.get("policy") != expected_policy:
        errors.append("audio boundary policy is stale")
    engines = report.get("engines")
    if not isinstance(engines, dict) or any(
        engines.get(name) != version
        for name, version in PINNED_AUDIO_ENGINES.items()
    ):
        errors.append("audio boundary engine versions are invalid")
    expected = expected_boundary_keys(audio_plan)
    actual: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in report.get("boundaries", []):
        if not isinstance(item, dict):
            errors.append("audio boundary report contains a non-object record")
            continue
        key = (
            str(item.get("output_id")),
            str(item.get("segment_id")),
            str(item.get("boundary")),
        )
        if key in actual:
            errors.append(f"duplicate audio boundary: {'/'.join(key)}")
        actual[key] = item
    if set(actual) != set(expected):
        errors.append(
            "audio boundary records do not cover Story clips exactly: "
            f"missing={sorted(set(expected) - set(actual))}, "
            f"extra={sorted(set(actual) - set(expected))}"
        )
    for key, cut in expected.items():
        item = actual.get(key)
        if item is None:
            continue
        planned = item.get("planned_source_seconds")
        if not isinstance(planned, (int, float)) or abs(
            float(planned) - cut
        ) > 0.001:
            errors.append(f"audio boundary timestamp mismatch: {'/'.join(key)}")
        if item.get("status") not in KNOWN_AUDIO_STATUSES:
            errors.append(f"unknown audio boundary status: {'/'.join(key)}")
    if report.get("source_errors"):
        errors.append("audio boundary report contains source analysis errors")
    return errors


# ---------------------------------------------------------------------------
# Helpers from repair_story_audio_boundaries.py
# ---------------------------------------------------------------------------


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
    method_expected = "local-audio-boundary-repair-v1"
    if index.get("method") != method_expected:
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
    max_rounds = 2
    if (
        not isinstance(repair_round, int)
        or isinstance(repair_round, bool)
        or not 1 <= repair_round <= max_rounds
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


# ---------------------------------------------------------------------------
# QC report assembly (from assemble_story_qc.py)
# ---------------------------------------------------------------------------


def worst(values: list[str]) -> int:
    return max((RESULT_RANK.get(value, 2) for value in values), default=0)


def status_from_checks(
    static_checks: list[dict[str, Any]],
    video_results: list[dict[str, Any]],
    category: str,
) -> str:
    values = [item["status"] for item in static_checks]
    values.extend(
        item["checks"][category]
        for item in video_results
        if item["checks"][category] != "not_assessed"
    )
    values.extend(
        finding["severity"]
        for item in video_results
        for finding in item["findings"]
        if finding["category"] == category
    )
    return REPORT_STATUS[worst(values)]


def validate_video_result(
    result: dict[str, Any],
    *,
    context: dict[str, Any],
    known_block_ids: set[str],
    known_clip_ids: set[str],
) -> list[str]:
    errors = validate_task_response("story_video_qc", result)
    for field in ("story_id", "review_id", "review_kind"):
        if result.get(field) != context.get(field):
            errors.append(f"{field} does not match QC context")
    checks = result.get("checks", {})
    kind = context.get("review_kind")
    if isinstance(checks, dict):
        if kind in {"boundary_start", "boundary_end"}:
            if checks.get("coverage") != "not_assessed":
                errors.append("boundary coverage must be not_assessed")
            if checks.get("flow") != "not_assessed":
                errors.append("boundary flow must be not_assessed")
            if checks.get("cut_safety") == "not_assessed":
                errors.append("boundary cut_safety must be assessed")
        elif kind == "junction":
            if checks.get("coverage") != "not_assessed":
                errors.append("junction coverage must be not_assessed")
            if checks.get("flow") == "not_assessed":
                errors.append("junction flow must be assessed")
            if checks.get("cut_safety") == "not_assessed":
                errors.append("junction cut_safety must be assessed")
        elif kind == "story_flow":
            for field in ("coverage", "flow", "cut_safety"):
                if checks.get(field) == "not_assessed":
                    errors.append(f"story_flow {field} must be assessed")
    duration = context.get("duration_seconds")
    findings = result.get("findings", [])
    if isinstance(duration, (int, float)) and isinstance(findings, list):
        for index, finding in enumerate(findings):
            if not isinstance(finding, dict):
                continue
            start = finding.get("proxy_start_seconds")
            end = finding.get("proxy_end_seconds")
            if (
                not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or start < 0
                or end < start
                or end > float(duration) + 0.1
            ):
                errors.append(f"findings[{index}] has invalid proxy range")
            unknown_blocks = set(finding.get("block_ids", [])) - known_block_ids
            unknown_clips = set(finding.get("clip_ids", [])) - known_clip_ids
            if unknown_blocks:
                errors.append(
                    f"findings[{index}] has unknown Block IDs: "
                    f"{sorted(unknown_blocks)}"
                )
            if unknown_clips:
                errors.append(
                    f"findings[{index}] has unknown Clip IDs: "
                    f"{sorted(unknown_clips)}"
                )
    applicable = [
        value
        for value in checks.values()
        if value != "not_assessed"
    ] if isinstance(checks, dict) else []
    applicable.extend(
        finding.get("severity", "block")
        for finding in findings
        if isinstance(finding, dict)
    )
    expected_overall = {0: "pass", 1: "review", 2: "block"}[worst(applicable)]
    if result.get("overall_status") != expected_overall:
        errors.append(
            f"overall_status must be {expected_overall!r} from checks/findings"
        )
    verified = result.get("verified_boundary")
    if kind in {"boundary_start", "boundary_end"}:
        if expected_overall == "pass" and verified != "yes":
            errors.append("passing boundary review must set verified_boundary=yes")
        if verified == "yes" and expected_overall != "pass":
            errors.append("verified_boundary=yes requires passing boundary review")
    elif verified != "not_applicable":
        errors.append("non-boundary review must set verified_boundary=not_applicable")
    return errors


def static_coverage_checks(
    plan: dict[str, Any],
    *,
    qc_admission: dict[str, Any],
) -> list[dict[str, Any]]:
    human_admitted = (
        plan["status"] == "blocked"
        and qc_admission.get("entry_mode")
        == "human_accepted_blocked_plan"
        and bool(qc_admission.get("accepted_blocked_reasons"))
    )
    plan_admitted = (
        plan["status"] == "ready_for_video_qc" or human_admitted
    )
    return [
        {
            "id": "plan-ready",
            "status": "pass" if plan_admitted else "block",
            "description": (
                "Story Plan 已通过结构验证并进入视频 QC。"
                if plan["status"] == "ready_for_video_qc"
                else (
                    "Story Plan 的机器硬失败与原始 blocked reasons "
                    "已由人工逐项接受，本次允许进入视频 QC；"
                    "基础 Plan 状态保持 blocked。"
                    if human_admitted
                    else "Story Plan 自身已阻断且没有有效人工放行。"
                )
            ),
            "related_ids": [plan["story_id"]],
        },
    ]


def static_flow_checks(plan: dict[str, Any]) -> list[dict[str, Any]]:
    del plan
    return []


def static_cut_checks(
    plan: dict[str, Any],
    *,
    proxy_manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    proxy = proxy_manifest["story_proxy"]
    return [
        {
            "id": "proxy-av-streams",
            "status": (
                "pass"
                if proxy["has_video"] and proxy["has_audio"]
                else "block"
            ),
            "description": (
                "Story QC Proxy 含可解码视频轨和音频轨。"
                if proxy["has_video"] and proxy["has_audio"]
                else "Story QC Proxy 缺少可解码音视频轨。"
            ),
            "related_ids": [plan["story_id"]],
        },
    ]


def group(
    *,
    category: str,
    static_checks: list[dict[str, Any]],
    video_results: list[dict[str, Any]],
) -> dict[str, Any]:
    applicable_results = [
        item
        for item in video_results
        if item["checks"][category] != "not_assessed"
    ]
    return {
        "status": status_from_checks(
            static_checks, applicable_results, category
        ),
        "static_checks": static_checks,
        "video_review_ids": [item["review_id"] for item in applicable_results],
        "findings": [
            finding
            for item in applicable_results
            for finding in item["findings"]
            if finding["category"] == category
        ],
    }


def clip_audio_boundary_statuses(
    plan: dict[str, Any],
    *,
    proxy_manifest: dict[str, Any],
    audio_report: dict[str, Any],
) -> tuple[
    list[str],
    list[str],
    list[str],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    boundary_by_key = {
        (
            item.get("output_id"),
            item.get("segment_id"),
            item.get("boundary"),
        ): item
        for item in audio_report.get("boundaries", [])
        if isinstance(item, dict)
    }
    proxy_by_clip = {
        item["clip_id"]: item for item in proxy_manifest["clips"]
    }
    verified: list[str] = []
    review: list[str] = []
    blocked: list[str] = []
    checks: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    audio_boundaries: list[dict[str, Any]] = []
    for block in plan["blocks"]:
        for clip in block["clips"]:
            clip_id = clip["id"]
            records = [
                boundary_by_key.get(
                    (plan["story_id"], clip_id, boundary)
                )
                for boundary in ("source_start", "source_end")
            ]
            if any(item is None for item in records):
                state = "block"
                blocked.append(clip_id)
                description = "Clip 缺少完整的本地双路 VAD 入点/出点记录。"
            elif any(
                item.get("status") == "safe_source_edge"
                and item.get("speech_active_at_cut") is True
                for item in records
                if isinstance(item, dict)
            ):
                review.append(clip_id)
                state = "review"
                description = (
                    "Clip 位于物理源边缘且切点处已有活跃语音；本地 VAD "
                    "无法证明源文件是否以完整台词起止，必须人工听审。"
                )
            elif all(
                item.get("status") in SAFE_AUDIO_STATUSES
                for item in records
                if isinstance(item, dict)
            ):
                verified.append(clip_id)
                state = "pass"
                description = (
                    "Clip 入点和出点均通过原始混音与 Demucs 人声双路 "
                    "Silero VAD 门禁。"
                )
            else:
                blocked.append(clip_id)
                state = "block"
                description = (
                    "Clip 入点或出点进入/贴近活跃语音，存在吞字或说话截断风险。"
                )
            checks.append(
                {
                    "id": f"local-audio-boundary-{clip_id}",
                    "status": state,
                    "description": description,
                    "related_ids": [clip_id],
                }
            )
            for boundary, item in zip(
                ("source_start", "source_end"), records
            ):
                if not isinstance(item, dict):
                    continue
                record = {
                    "clip_id": clip_id,
                    "block_id": block["id"],
                    "source_id": clip["source_id"],
                    "boundary": boundary,
                    "status": item["status"],
                    "planned_source_seconds": item[
                        "planned_source_seconds"
                    ],
                    "speech_active_at_cut": item.get(
                        "speech_active_at_cut", False
                    ),
                    "speech_interval": item.get("speech_interval"),
                    "recommended_source_seconds": item.get(
                        "recommended_source_seconds",
                        item["planned_source_seconds"],
                    ),
                    "adjustment_seconds": item.get(
                        "adjustment_seconds", 0.0
                    ),
                    "reason": item["reason"],
                }
                audio_boundaries.append(record)
                source_edge_review = (
                    item["status"] == "safe_source_edge"
                    and item.get("speech_active_at_cut") is True
                )
                if (
                    item["status"] in SAFE_AUDIO_STATUSES
                    and not source_edge_review
                ):
                    continue
                proxy_clip = proxy_by_clip[clip_id]
                proxy_seconds = (
                    proxy_clip["proxy_start"]
                    if boundary == "source_start"
                    else proxy_clip["proxy_end"]
                )
                suggested_action = (
                    "human_review"
                    if source_edge_review
                    else
                    "adjust_start"
                    if item["status"] == "adjustment_required"
                    and boundary == "source_start"
                    else "adjust_end"
                    if item["status"] == "adjustment_required"
                    else "replan"
                    if item["status"] == "blocked_replan"
                    else "human_review"
                )
                findings.append(
                    {
                        "code": (
                            f"local-audio-source-edge-speech-active-{boundary}"
                            if source_edge_review
                            else f"local-audio-{item['status']}-{boundary}"
                        ),
                        "category": "cut_safety",
                        "severity": "review" if source_edge_review else "block",
                        "description": (
                            (
                                "物理源边缘存在活跃语音，本地 VAD 无法判断台词语义是否完整。"
                                if source_edge_review
                                else item["reason"]
                            )
                            + " 计划切点 "
                            f"{float(item['planned_source_seconds']):.3f}s，"
                            f"建议 {float(item.get('recommended_source_seconds', item['planned_source_seconds'])):.3f}s。"
                        ),
                        "proxy_start_seconds": proxy_seconds,
                        "proxy_end_seconds": proxy_seconds,
                        "block_ids": [block["id"]],
                        "clip_ids": [clip_id],
                        "suggested_action": suggested_action,
                    }
                )
    story_audio_status = (
        "blocked" if blocked else "review" if review else "approved"
    )
    return (
        sorted(verified),
        sorted(review),
        sorted(blocked),
        checks,
        findings,
        {
            "status": story_audio_status,
            "method": "demucs-silero-dual-vad-v1.2",
            "safe_boundary_count": sum(
                item["status"] in SAFE_AUDIO_STATUSES
                and not (
                    item["status"] == "safe_source_edge"
                    and item["speech_active_at_cut"] is True
                )
                for item in audio_boundaries
            ),
            "review_boundary_count": sum(
                item["status"] == "safe_source_edge"
                and item["speech_active_at_cut"] is True
                for item in audio_boundaries
            ),
            "blocking_boundary_count": sum(
                item["status"] not in SAFE_AUDIO_STATUSES
                for item in audio_boundaries
            ),
            "boundaries": audio_boundaries,
        },
    )


def patch_recommendations(
    findings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    for finding in findings:
        action = finding["suggested_action"]
        if action == "none" or finding["severity"] == "info":
            continue
        targets = list(
            dict.fromkeys(finding["clip_ids"] + finding["block_ids"])
        )
        if not targets:
            targets = ["story"]
        key = (action, tuple(targets), finding["description"])
        if key in seen:
            continue
        seen.add(key)
        records.append(
            {
                "action": action,
                "target_ids": targets,
                "reason": finding["description"],
            }
        )
    return records


def load_validated_audio_boundary(
    batch: dict[str, Any],
    *,
    plan_index_path: Path,
    source_manifest_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = batch.get("audio_boundary")
    if not isinstance(metadata, dict):
        raise ValueError("Story QC batch has no local audio boundary metadata")
    audio_plan_path = Path(
        metadata["audio_plan_path"]
    ).expanduser().resolve()
    report_path = Path(metadata["report_path"]).expanduser().resolve()
    guard_path = Path(
        metadata["audio_guard_script"]
    ).expanduser().resolve()
    for path, expected, label in (
        (audio_plan_path, metadata.get("audio_plan_sha256"), "audio plan"),
        (report_path, metadata.get("report_sha256"), "audio report"),
        (
            guard_path,
            metadata.get("audio_guard_script_sha256"),
            "audio guard",
        ),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"missing Story QC {label}: {path}")
        if sha256_file(path) != expected:
            raise ValueError(f"Story QC {label} SHA-256 is stale")
    audio_plan = load_json(audio_plan_path)
    if audio_plan.get("story_plan_index_sha256") != sha256_file(
        plan_index_path
    ):
        raise ValueError("Story audio plan uses a stale Story Plan Index")
    if audio_plan.get("source_manifest_sha256") != sha256_file(
        source_manifest_path
    ):
        raise ValueError("Story audio plan uses a stale Source Manifest")
    local_manifest_value = audio_plan.get("local_source_manifest_path")
    local_manifest_sha256 = audio_plan.get("local_source_manifest_sha256")
    if isinstance(local_manifest_value, str):
        local_manifest_path = Path(local_manifest_value).expanduser().resolve()
        if (
            not local_manifest_path.is_file()
            or sha256_file(local_manifest_path) != local_manifest_sha256
        ):
            raise ValueError("Story audio plan local Source Manifest is stale")
    errors = validate_audio_report(
        audio_plan_path,
        report_path,
        expected_policy=metadata.get("policy", {}),
    )
    if errors:
        raise ValueError(
            "invalid Story local audio boundary report: "
            + "; ".join(errors[:30])
        )
    report = load_json(report_path)
    return metadata, report


def build_report(
    *,
    job_root: Path,
    plan_index_path: Path,
    plan_entry: dict[str, Any],
    approval_entry: dict[str, Any],
    source_manifest_path: Path,
    batch_path: Path,
    proxy_manifest_path: Path,
    jobs: list[dict[str, Any]],
    audio_boundary_metadata: dict[str, Any],
    audio_boundary_report: dict[str, Any],
    boundary_repair_metadata: dict[str, Any],
) -> dict[str, Any]:
    plan_path = Path(plan_entry["path"]).expanduser().resolve()
    plan = load_json(plan_path)
    script_path = Path(approval_entry["script_path"]).expanduser().resolve()
    script = load_json(script_path)
    proxy_manifest = load_json(proxy_manifest_path)
    qc_admission = proxy_manifest.get("story_plan_qc_admission")
    if not isinstance(qc_admission, dict):
        qc_admission = {
            "entry_mode": "machine_validated_plan",
            "original_plan_status": plan.get("status"),
            "admission_path": None,
            "admission_sha256": None,
            "accepted_blocked_reasons": [],
            "human_note": "",
            "decided_at": None,
        }
    if plan.get("status") == "blocked" and (
        qc_admission.get("entry_mode")
        != "human_accepted_blocked_plan"
        or not qc_admission.get("accepted_blocked_reasons")
    ):
        raise ValueError(
            f"{plan['story_id']}: blocked Plan lacks human QC admission"
        )
    if proxy_manifest["story_id"] != plan["story_id"]:
        raise ValueError(f"{plan['story_id']}: proxy manifest identity mismatch")
    known_block_ids = {item["id"] for item in plan["blocks"]}
    known_clip_ids = {
        clip["id"] for block in plan["blocks"] for clip in block["clips"]
    }
    video_results: list[dict[str, Any]] = []
    result_fingerprints: list[dict[str, str]] = []
    expected_review_ids = {
        item["review_id"] for item in proxy_manifest["review_assets"]
    }
    assets_by_id = {
        item["review_id"]: item
        for item in proxy_manifest["review_assets"]
    }
    jobs_by_id = {
        item["id"]: item
        for item in jobs
        if item.get("story_id") == plan["story_id"]
    }
    if set(jobs_by_id) != expected_review_ids:
        raise ValueError(
            f"{plan['story_id']}: QC jobs differ from proxy review assets"
        )
    for review_id in sorted(expected_review_ids):
        job = jobs_by_id[review_id]
        output_path = Path(job["output"]).expanduser().resolve()
        context_path = Path(job["context_file"]).expanduser().resolve()
        media_path = Path(job["media_file"]).expanduser().resolve()
        asset = assets_by_id[review_id]
        expected_context_path = Path(
            asset["context_path"]
        ).expanduser().resolve()
        expected_media_path = Path(asset["path"]).expanduser().resolve()
        if context_path != expected_context_path:
            raise ValueError(
                f"{plan['story_id']}/{review_id}: QC context path "
                "differs from proxy manifest"
            )
        if media_path != expected_media_path:
            raise ValueError(
                f"{plan['story_id']}/{review_id}: QC media path "
                "differs from proxy manifest"
            )
        if not output_path.is_file() or not context_path.is_file():
            raise FileNotFoundError(
                f"{plan['story_id']}: missing QC result/context for {review_id}"
            )
        if sha256_file(context_path) != asset["context_sha256"]:
            raise ValueError(
                f"{plan['story_id']}/{review_id}: QC context is stale"
            )
        result = load_json(output_path)
        context = load_json(context_path)
        errors = validate_video_result(
            result,
            context=context,
            known_block_ids=known_block_ids,
            known_clip_ids=known_clip_ids,
        )
        if errors:
            raise ValueError(
                f"{plan['story_id']}/{review_id}: invalid video QC result: "
                + "; ".join(errors[:30])
            )
        video_results.append(result)
        result_fingerprints.append(
            {"review_id": review_id, "sha256": sha256_file(output_path)}
        )
    coverage_checks = static_coverage_checks(
        plan,
        qc_admission=qc_admission,
    )
    editorial_diagnostics = (
        script.get("feasibility", {}).get("editorial_diagnostics")
        if isinstance(script.get("feasibility"), dict)
        else None
    )
    if not isinstance(editorial_diagnostics, dict):
        editorial_diagnostics = story_coherence_diagnostics(script)
    flow_checks = static_flow_checks(plan) + diagnostics_as_qc_checks(
        editorial_diagnostics
    )
    cut_checks = static_cut_checks(
        plan,
        proxy_manifest=proxy_manifest,
    )
    (
        verified_clip_ids,
        review_clip_ids,
        blocked_clip_ids,
        boundary_checks,
        local_audio_findings,
        local_audio_boundary,
    ) = clip_audio_boundary_statuses(
        plan,
        proxy_manifest=proxy_manifest,
        audio_report=audio_boundary_report,
    )
    cut_checks.extend(boundary_checks)
    coverage_qc = group(
        category="coverage",
        static_checks=coverage_checks,
        video_results=video_results,
    )
    flow_qc = group(
        category="flow",
        static_checks=flow_checks,
        video_results=video_results,
    )
    cut_safety_qc = group(
        category="cut_safety",
        static_checks=cut_checks,
        video_results=video_results,
    )
    findings = local_audio_findings + [
        finding
        for item in video_results
        for finding in item["findings"]
    ]
    top_status = REPORT_STATUS[
        worst(
            [
                coverage_qc["status"].replace("approved", "pass").replace(
                    "blocked", "block"
                ),
                flow_qc["status"].replace("approved", "pass").replace(
                    "blocked", "block"
                ),
                cut_safety_qc["status"].replace("approved", "pass").replace(
                    "blocked", "block"
                ),
            ]
        )
    ]
    story_unresolved = [
        item
        for item in boundary_repair_metadata.get("unresolved", [])
        if isinstance(item, dict)
        and item.get("story_id") == plan["story_id"]
    ]
    if any(item.get("route") == "blocked_replan" for item in story_unresolved):
        story_repair_status = "blocked_replan"
    elif story_unresolved:
        story_repair_status = "review"
    elif int(plan_entry.get("repair_round", 0)):
        story_repair_status = "verified_after_repair"
    else:
        story_repair_status = "not_needed"
    patch_history = [
        item
        for item in plan_entry.get("repair_history", [])
        if isinstance(item, dict)
    ]
    applied_change_count = 0
    for item in patch_history:
        patch_path = Path(item["path"]).expanduser().resolve()
        patch = load_json(patch_path)
        applied_change_count += len(patch.get("changes", []))
    report = {
        "schema_version": "1.0",
        "method": "static-proxy-local-audio-boundary-repair-v3",
        "story_id": plan["story_id"],
        "title": plan["title"],
        "production_slot": plan["production_slot"],
        "status": top_status,
        "story_plan_qc_admission": qc_admission,
        "input_fingerprints": {
            "story_plan_index_sha256": sha256_file(plan_index_path),
            "story_plan_sha256": sha256_file(plan_path),
            "story_script_sha256": sha256_file(script_path),
            "source_manifest_sha256": sha256_file(source_manifest_path),
            "proxy_manifest_sha256": sha256_file(proxy_manifest_path),
            "story_qc_batch_sha256": sha256_file(batch_path),
            "story_audio_boundary_plan_sha256": audio_boundary_metadata[
                "audio_plan_sha256"
            ],
            "story_audio_boundary_report_sha256": audio_boundary_metadata[
                "report_sha256"
            ],
            "story_boundary_repair_metadata_sha256": (
                boundary_repair_metadata["sha256"]
            ),
            "video_results": result_fingerprints,
        },
        "proxy_manifest_path": str(proxy_manifest_path),
        "story_proxy_path": proxy_manifest["story_proxy"]["path"],
        "story_proxy_duration_seconds": proxy_manifest["story_proxy"][
            "duration_seconds"
        ],
        "coverage_qc": coverage_qc,
        "flow_qc": flow_qc,
        "cut_safety_qc": cut_safety_qc,
        "local_audio_boundary": {
            **local_audio_boundary,
            "report_path": audio_boundary_metadata["report_path"],
            "report_sha256": audio_boundary_metadata["report_sha256"],
            "engines": audio_boundary_metadata["engines"],
            "policy": audio_boundary_metadata["policy"],
            "remote_audio_upload": False,
        },
        "boundary_repair": {
            "status": story_repair_status,
            "metadata_path": boundary_repair_metadata["path"],
            "metadata_sha256": boundary_repair_metadata["sha256"],
            "base_story_plan_index_sha256": boundary_repair_metadata[
                "base_story_plan_index_sha256"
            ],
            "effective_story_plan_index_sha256": (
                boundary_repair_metadata[
                    "effective_story_plan_index_sha256"
                ]
            ),
            "repair_round": int(plan_entry.get("repair_round", 0)),
            "applied_change_count": applied_change_count,
            "patch_history": patch_history,
            "unresolved_boundary_ids": [
                item["boundary_id"] for item in story_unresolved
            ],
        },
        "verified_clip_ids": verified_clip_ids,
        "review_clip_ids": review_clip_ids,
        "blocked_clip_ids": blocked_clip_ids,
        "findings": findings,
        "patch_recommendations": patch_recommendations(findings),
    }
    errors = validate_task_response("story_qc_report", report)
    if errors:
        raise ValueError(
            f"{plan['story_id']}: invalid Story QC report: "
            + "; ".join(errors[:40])
        )
    return report


def render_review(
    reports: list[dict[str, Any]], index_status: str
) -> str:
    """Render a human-readable Markdown review of QC reports.

    Migrated from _legacy_v4/scripts/assemble_story_qc.py.
    """
    lines = [
        "# Story QC 复核",
        "",
        f"- Portfolio QC 状态：`{index_status}`",
        f"- Story 数量：{len(reports)}",
        "",
    ]
    for report in sorted(reports, key=lambda item: item["production_slot"]):
        lines.extend(
            [
                f"## 槽位 {report['production_slot']} · {report['title']}",
                "",
                f"- Story ID：`{report['story_id']}`",
                f"- 总状态：`{report['status']}`",
                f"- Coverage：`{report['coverage_qc']['status']}`",
                f"- Flow：`{report['flow_qc']['status']}`",
                f"- Cut Safety：`{report['cut_safety_qc']['status']}`",
                (
                    "- 本地语音边界："
                    f"`{report['local_audio_boundary']['status']}`，"
                    f"safe={report['local_audio_boundary']['safe_boundary_count']}，"
                    f"review={report['local_audio_boundary']['review_boundary_count']}，"
                    f"blocked={report['local_audio_boundary']['blocking_boundary_count']}"
                ),
                (
                    "- 自动边界修复："
                    f"`{report['boundary_repair']['status']}`，"
                    f"round={report['boundary_repair']['repair_round']}，"
                    f"changes={report['boundary_repair']['applied_change_count']}"
                ),
                (
                    "- 边界："
                    f"verified={len(report['verified_clip_ids'])}，"
                    f"review={len(report['review_clip_ids'])}，"
                    f"blocked={len(report['blocked_clip_ids'])}"
                ),
                f"- QC Proxy：`{report['story_proxy_path']}`",
                "",
                "### Findings",
                "",
            ]
        )
        if report["findings"]:
            for finding in report["findings"]:
                lines.append(
                    f"- `{finding['severity']}` `{finding['category']}` "
                    f"{finding['description']}"
                )
        else:
            lines.append("- 无")
        lines.extend(["", "### Patch 建议", ""])
        if report["patch_recommendations"]:
            for item in report["patch_recommendations"]:
                lines.append(
                    f"- `{item['action']}` → "
                    f"{', '.join(item['target_ids'])}：{item['reason']}"
                )
        else:
            lines.append("- 无")
        lines.append("")
    return "\n".join(lines)


def assemble(job_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Assemble the Story QC index from batch results.

    Migrated from _legacy_v4/scripts/assemble_story_qc.py.
    """
    approval_path = job_root / "story-approval.json"
    source_manifest_path = job_root / "source_manifest.json"
    batch_path = job_root / "story-qc-batch.json"
    required = (
        job_root / "story-plans" / "index.json",
        approval_path,
        source_manifest_path,
        batch_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "missing Story QC input: " + ", ".join(missing)
        )
    approval = load_json(approval_path)
    batch = load_json(batch_path)
    _, plan_index_path = resolve_qc_plan_index(job_root, batch)
    plan_index = load_json(plan_index_path)
    if batch.get("story_plan_index_sha256") != sha256_file(plan_index_path):
        raise ValueError("Story QC batch uses a stale Story Plan Index")
    if batch.get("source_manifest_sha256") != sha256_file(source_manifest_path):
        raise ValueError("Story QC batch uses a stale Source Manifest")
    admission_entries: dict[str, dict[str, Any]] = {}
    admission_path_value = batch.get("story_plan_qc_admission_path")
    admission_sha256 = batch.get("story_plan_qc_admission_sha256")
    if isinstance(admission_path_value, str):
        admission_path = Path(admission_path_value).expanduser().resolve()
        if (
            not admission_path.is_file()
            or sha256_file(admission_path) != admission_sha256
        ):
            raise ValueError("Story Plan QC admission is missing or stale")
        from autocut_core.libs.qc_admission import ACCEPTED, validate_admission
        _, admission_entries, admission_errors = validate_admission(
            job_root,
            admission_path,
        )
        if admission_errors:
            raise ValueError(
                "invalid Story Plan QC admission: "
                + "; ".join(admission_errors[:30])
            )
    elif admission_sha256 is not None:
        raise ValueError("Story QC batch has admission hash without path")
    audio_boundary_metadata, audio_boundary_report = (
        load_validated_audio_boundary(
            batch,
            plan_index_path=plan_index_path,
            source_manifest_path=source_manifest_path,
        )
    )
    from autocut_core.libs.qc_admission import ACCEPTED
    approval_entries = {
        item["story_id"]: item
        for item in approval.get("stories", [])
        if isinstance(item, dict) and item.get("decision") == "approved"
    }
    plan_entries = {
        item["story_id"]: item
        for item in plan_index.get("plans", [])
        if isinstance(item, dict)
    }
    proxy_paths = [
        Path(value).expanduser().resolve()
        for value in batch.get("proxy_manifests", [])
        if isinstance(value, str)
    ]
    proxy_by_story: dict[str, Path] = {}
    for path in proxy_paths:
        if not path.is_file():
            raise FileNotFoundError(f"missing Story QC proxy manifest: {path}")
        value = load_json(path)
        errors = validate_task_response("story_qc_proxy_manifest", value)
        if errors:
            raise ValueError(
                f"invalid Story QC proxy manifest {path}: "
                + "; ".join(errors[:30])
            )
        story_id = value["story_id"]
        admission = value.get("story_plan_qc_admission", {})
        if value.get("story_plan_qc_admission") is not None:
            if admission.get("admission_sha256") != admission_sha256:
                raise ValueError(
                    f"{story_id}: proxy admission differs from QC batch"
                )
            if (
                admission.get("entry_mode")
                == "human_accepted_blocked_plan"
                and admission_entries.get(story_id, {}).get("decision")
                != ACCEPTED
            ):
                raise ValueError(
                    f"{story_id}: proxy uses unaccepted blocked Plan"
                )
        if story_id in proxy_by_story:
            raise ValueError(f"duplicate Story QC proxy manifest: {story_id}")
        proxy_by_story[story_id] = path
    if set(proxy_by_story) != set(plan_entries):
        raise ValueError(
            "Story QC proxy manifests do not cover every Story Plan exactly"
        )
    jobs = [item for item in batch.get("jobs", []) if isinstance(item, dict)]
    reports: list[dict[str, Any]] = []
    output_dir = job_root / "story-qc"
    index_entries: list[dict[str, Any]] = []
    for story_id, plan_entry in sorted(
        plan_entries.items(), key=lambda item: item[1]["production_slot"]
    ):
        approval_entry = approval_entries.get(story_id)
        if approval_entry is None:
            raise ValueError(f"{story_id}: Story approval is missing or stale")
        report = build_report(
            job_root=job_root,
            plan_index_path=plan_index_path,
            plan_entry=plan_entry,
            approval_entry=approval_entry,
            source_manifest_path=source_manifest_path,
            batch_path=batch_path,
            proxy_manifest_path=proxy_by_story[story_id],
            jobs=jobs,
            audio_boundary_metadata=audio_boundary_metadata,
            audio_boundary_report=audio_boundary_report,
            boundary_repair_metadata=batch["boundary_repair"],
        )
        report_path = output_dir / f"{story_id}.json"
        atomic_write_json(report_path, report)
        reports.append(report)
        index_entries.append(
            {
                "story_id": story_id,
                "title": report["title"],
                "production_slot": report["production_slot"],
                "status": report["status"],
                "path": str(report_path),
                "report_sha256": sha256_file(report_path),
                "story_plan_sha256": report["input_fingerprints"][
                    "story_plan_sha256"
                ],
                "proxy_manifest_sha256": report["input_fingerprints"][
                    "proxy_manifest_sha256"
                ],
            }
        )
    approved_count = sum(item["status"] == "approved" for item in reports)
    review_count = sum(item["status"] == "review" for item in reports)
    blocked_count = sum(item["status"] == "blocked" for item in reports)
    if not reports or blocked_count:
        index_status = "blocked"
    elif review_count:
        index_status = "review"
    else:
        index_status = "approved"
    index = {
        "schema_version": "1.0",
        "method": "static-proxy-local-audio-boundary-repair-v3",
        "status": index_status,
        "story_plan_index_sha256": sha256_file(plan_index_path),
        "story_qc_batch_sha256": sha256_file(batch_path),
        "report_count": len(reports),
        "approved_count": approved_count,
        "review_count": review_count,
        "blocked_count": blocked_count,
        "reports": index_entries,
    }
    errors = validate_task_response("story_qc_index", index)
    if errors:
        raise ValueError(
            "invalid Story QC Index: " + "; ".join(errors[:40])
        )
    atomic_write_json(output_dir / "index.json", index)
    atomic_write_text(
        job_root / "story-qc-review.md",
        render_review(reports, index_status),
    )
    update_project_stage(
        job_root / "project.json",
        "story_qc",
        index_status,
        inputs={
            "story_plan_index": str(plan_index_path),
            "story_qc_batch": str(batch_path),
        },
        outputs={
            "story_qc_index": str(output_dir / "index.json"),
            "story_qc_review": str(job_root / "story-qc-review.md"),
        },
    )
    return index, reports