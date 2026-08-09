#!/usr/bin/env python3
"""Story approval decision logic extracted from _legacy_v4/scripts/story_approval.py.

Provides ``decide_story`` as a pure function that replaces the ``story_approval.py decide``
subprocess call in auto.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autocut_core.io import (
    atomic_write_json,
    load_json,
    sha256_file,
    update_project_stage,
    utc_now,
)
from autocut_core.schema.compat import validate_task_response

DECISIONS = frozenset(
    {
        "pending",
        "approved",
        "rejected",
        "revision_requested",
        "merge_with",
        "split_requested",
    }
)


def load_approval(path: Path) -> dict[str, Any]:
    value = load_json(path)
    if not isinstance(value, dict) or not isinstance(value.get("stories"), list):
        raise ValueError("approval file must contain stories[]")
    return value


def refresh_status(
    approval: dict[str, Any], approval_path: Path, project_path: Path
) -> None:
    decisions = {item.get("decision") for item in approval["stories"]}
    selected_items = sorted(
        (
            item
            for item in approval["stories"]
            if item.get("decision") == "approved"
        ),
        key=lambda item: int(item.get("production_slot", 0)),
    )
    selected_story_ids = [item["story_id"] for item in selected_items]
    unresolved = "pending" in decisions or bool(
        decisions
        & {
            "revision_requested",
            "merge_with",
            "split_requested",
        }
    )
    if unresolved:
        status = "awaiting_story_approval"
        fulfillment_status = "awaiting_decisions"
    else:
        status = "story_selection_complete"
        fulfillment_status = "ready"
    approval["status"] = status
    approval["selected_story_ids"] = selected_story_ids
    approval["fulfillment_status"] = fulfillment_status
    approval["rejected_story_ids"] = sorted(
        item["story_id"]
        for item in approval["stories"]
        if item.get("decision") == "rejected"
    )
    approval["revision_requested_story_ids"] = sorted(
        item["story_id"]
        for item in approval["stories"]
        if item.get("decision") == "revision_requested"
    )
    approval["merge_with_story_ids"] = sorted(
        item["story_id"]
        for item in approval["stories"]
        if item.get("decision") == "merge_with"
    )
    approval["split_requested_story_ids"] = sorted(
        item["story_id"]
        for item in approval["stories"]
        if item.get("decision") == "split_requested"
    )
    atomic_write_json(approval_path, approval)
    update_project_stage(
        project_path,
        "story_approval",
        status,
        inputs={"story_approval": str(approval_path)},
        outputs={"story_approval": str(approval_path)},
        note=f"fulfillment={fulfillment_status}; selected={len(selected_story_ids)}",
    )


def decide_story(
    approval_path: Path,
    story_id: str,
    decision: str,
    *,
    accept_risks: bool = False,
    notes: str = "",
    target_story_id: str | None = None,
    project_path: Path | None = None,
) -> str:
    """Apply a human decision to one Story in the approval manifest.

    Replaces ``story_approval.py decide <approval> <story_id> <decision>``.
    Returns the decision string on success.
    """
    approval_path = approval_path.expanduser().resolve()
    approval = load_approval(approval_path)
    matching = [
        item for item in approval["stories"] if item.get("story_id") == story_id
    ]
    if len(matching) != 1:
        raise ValueError(f"unknown or duplicate story_id: {story_id}")
    if decision not in DECISIONS - {"pending"}:
        raise ValueError(f"invalid decision: {decision}")
    if decision == "merge_with" and not target_story_id:
        raise ValueError("merge_with requires --target-story-id")
    if decision != "merge_with" and target_story_id:
        raise ValueError("--target-story-id is only valid with merge_with")
    item = matching[0]
    script_path = Path(item["script_path"]).expanduser().resolve()
    script = load_json(script_path)
    schema_errors = validate_task_response("story_script", script)
    if schema_errors:
        raise ValueError(
            f"story is not a valid approval artifact: "
            + "; ".join(schema_errors[:20])
        )
    feasibility_status = script["feasibility"]["status"]
    portfolio_path_value = approval.get("story_portfolio_path")
    if not isinstance(portfolio_path_value, str):
        raise ValueError("approval file is missing story_portfolio_path")
    current_portfolio_sha256 = sha256_file(
        Path(portfolio_path_value).expanduser().resolve()
    )
    if (
        decision == "approved"
        and script["portfolio"]["portfolio_sha256"] != current_portfolio_sha256
    ):
        raise ValueError(
            "Story Portfolio changed; regenerate/re-preflight the Script before approval"
        )
    if decision == "approved" and feasibility_status == "not_feasible":
        raise ValueError(
            "not_feasible Story cannot be approved; request revision, reject, merge, or split"
        )
    if (
        decision == "approved"
        and feasibility_status in {"partial", "awaiting_scope_merge"}
        and not accept_risks
    ):
        gate_label = (
            "partial"
            if feasibility_status == "partial"
            else "awaiting_scope_merge (recommend running expand_story_scope "
            "--source script_preflight --apply first)"
        )
        raise ValueError(
            f"{gate_label} Story requires --accept-risks and non-empty "
            "--notes to approve"
        )
    if accept_risks and decision != "approved":
        raise ValueError("--accept-risks is only valid with approved")
    if accept_risks and not notes.strip():
        raise ValueError("--accept-risks requires non-empty --notes")
    current_sha = sha256_file(script_path)
    item["script_sha256"] = current_sha
    item["feasibility_status"] = feasibility_status
    item["estimated_source_duration_min_seconds"] = script["feasibility"][
        "estimated_source_duration_min_seconds"
    ]
    item["estimated_source_duration_max_seconds"] = script["feasibility"][
        "estimated_source_duration_max_seconds"
    ]
    item["material_risks"] = script["feasibility"]["material_risks"]
    item["decision"] = decision
    item["approved_script_sha256"] = (
        current_sha if decision == "approved" else None
    )
    item["target_story_id"] = target_story_id
    item["notes"] = notes
    item["accepted_material_risks"] = accept_risks
    item["decided_at"] = utc_now()
    proj_path = (
        project_path.expanduser().resolve()
        if project_path
        else approval_path.parent / "project.json"
    )
    refresh_status(approval, approval_path, proj_path)
    return decision