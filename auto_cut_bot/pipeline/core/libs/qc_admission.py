#!/usr/bin/env python3
"""Validate explicit human admission of blocked Story Plans to QC."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autocut_core.io import json_sha256, load_json, sha256_file

METHOD = "human-story-plan-qc-admission-v1"
ACCEPTED = "accepted_for_qc"
REJECTED = "rejected"
PENDING = "pending"
NOT_REQUIRED = "not_required"


def plan_entries(index: dict[str, Any]) -> list[dict[str, Any]]:
    values = index.get("plans")
    if not isinstance(values, list) or not values:
        raise ValueError("Story Plan Index requires non-empty plans[]")
    return [item for item in values if isinstance(item, dict)]


def load_current_plan(entry: dict[str, Any]) -> tuple[Path, dict[str, Any], str]:
    path_value = entry.get("path")
    if not isinstance(path_value, str):
        raise ValueError("Story Plan entry requires path")
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Story Plan is missing: {path}")
    digest = sha256_file(path)
    if entry.get("plan_sha256") != digest:
        raise ValueError(f"Story Plan Index has stale hash: {path}")
    plan = load_json(path)
    if plan.get("story_id") != entry.get("story_id"):
        raise ValueError(f"Story Plan identity mismatch: {path}")
    if plan.get("status") != entry.get("status"):
        raise ValueError(f"Story Plan status differs from Index: {path}")
    return path, plan, digest


def compute_status(stories: list[dict[str, Any]]) -> str:
    blocked_decisions = [
        item["decision"]
        for item in stories
        if item["plan_status"] == "blocked"
    ]
    if not blocked_decisions:
        return "ready_for_story_qc"
    if REJECTED in blocked_decisions:
        return "rejected"
    if all(item == ACCEPTED for item in blocked_decisions):
        return "ready_for_story_qc"
    if any(item == ACCEPTED for item in blocked_decisions):
        return "partially_accepted"
    return "pending"


def validate_admission(
    job_root: Path,
    admission_path: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]:
    errors: list[str] = []
    artifact = load_json(admission_path)
    index_path = job_root / "story-plans" / "index.json"
    if artifact.get("method") != METHOD:
        errors.append("Story Plan QC admission method is invalid")
    if not index_path.is_file():
        errors.append("Story Plan Index is missing")
        return artifact, {}, errors
    if artifact.get("story_plan_index_path") != str(index_path.resolve()):
        errors.append("Story Plan QC admission points to another Plan Index")
    if artifact.get("story_plan_index_sha256") != sha256_file(index_path):
        errors.append("Story Plan QC admission uses a stale Plan Index")
    index = load_json(index_path)
    expected_entries = {
        item.get("story_id"): item
        for item in plan_entries(index)
        if isinstance(item.get("story_id"), str)
    }
    admission_entries = {
        item.get("story_id"): item
        for item in artifact.get("stories", [])
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }
    if set(admission_entries) != set(expected_entries):
        errors.append(
            "Story Plan QC admission must cover every materialized Plan exactly"
        )
    for story_id, index_entry in expected_entries.items():
        admission = admission_entries.get(story_id)
        if admission is None:
            continue
        try:
            plan_path, plan, digest = load_current_plan(index_entry)
        except (OSError, ValueError) as exc:
            errors.append(f"{story_id}: {exc}")
            continue
        if admission.get("plan_path") != str(plan_path):
            errors.append(f"{story_id}: admission Plan path is stale")
        if admission.get("plan_sha256") != digest:
            errors.append(f"{story_id}: admission Plan hash is stale")
        if admission.get("plan_status") != plan.get("status"):
            errors.append(f"{story_id}: admission Plan status is stale")
        blocked_reasons = list(plan.get("blocked_reasons", []))
        if admission.get("blocked_reasons") != blocked_reasons:
            errors.append(f"{story_id}: admitted blocked reasons changed")
        if admission.get("blocked_reasons_sha256") != json_sha256(
            blocked_reasons
        ):
            errors.append(f"{story_id}: blocked reasons hash is stale")
        decision = admission.get("decision")
        note = admission.get("note")
        if plan["status"] == "ready_for_video_qc":
            if decision != NOT_REQUIRED:
                errors.append(
                    f"{story_id}: ready Plan admission must be not_required"
                )
        elif plan["status"] == "blocked":
            if decision not in {PENDING, ACCEPTED, REJECTED}:
                errors.append(f"{story_id}: invalid admission decision")
            if decision in {ACCEPTED, REJECTED} and (
                not isinstance(note, str) or not note.strip()
            ):
                errors.append(
                    f"{story_id}: human admission decision requires note"
                )
            if decision == ACCEPTED and not blocked_reasons:
                errors.append(
                    f"{story_id}: blocked Plan has no auditable reasons"
                )
    expected_status = compute_status(list(admission_entries.values()))
    if artifact.get("status") != expected_status:
        errors.append("Story Plan QC admission aggregate status is stale")
    return artifact, admission_entries, errors