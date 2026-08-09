#!/usr/bin/env python3
"""Fail-closed admission gate for the canonical Story-first pipeline.

This module is intentionally separate from the editorial model validators.  A
valid Story Script or Story Plan is not enough to authorize rendering: the
whole chain must exist, be hash-linked, and use the current opening contract.
Legacy one-off FFmpeg recipes and copied historical MP4s have no admissible
chain and therefore cannot pass this gate.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from autocut_core.io import atomic_write_json, load_json, sha256_file


PIPELINE_GATE_VERSION = "story-first-hard-gate-v2-joint-opening-selection"
ALLOWED_OPENING_STRATEGIES = {
    "causal_explanatory_no_reprise",
    "causal_explanatory_delayed_reprise",
    "original_chronological_opening",
}
MIN_OPENING_STRENGTH = 9


def _required_paths(job_root: Path) -> dict[str, Path]:
    return {
        "project": job_root / "project.json",
        "source_manifest": job_root / "source_manifest.json",
        "series_bible": job_root / "series-bible.json",
        "story_catalog": job_root / "story-catalog.json",
        "story_scripts_index": job_root / "story-scripts" / "index.json",
        "story_script_preflight": job_root / "story-script-preflight.json",
        "story_approval": job_root / "story-approval.json",
        "story_evidence_index": job_root / "story-evidence" / "index.json",
        "span_candidates_index": job_root / "span-candidates" / "index.json",
        "story_plan_preflight": job_root / "story-plan-preflight.json",
        "story_plans_index": job_root / "story-plans" / "index.json",
        "story_qc_batch": job_root / "story-qc-batch.json",
        "story_qc_index": job_root / "story-qc" / "index.json",
    }


def _path_from(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser().resolve()


def _index_by_story(items: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        return {}
    return {
        item["story_id"]: item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }


def _check_hash(
    errors: list[str], *, label: str, path: Path, declared: Any
) -> None:
    if not path.is_file():
        errors.append(f"{label}: missing {path}")
        return
    if declared != sha256_file(path):
        errors.append(f"{label}: SHA-256 is stale")


def _check_opening_contract(
    errors: list[str],
    *,
    story_id: str,
    script: dict[str, Any],
    plan: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
) -> None:
    contract = script.get("teaser_contract")
    if not isinstance(contract, dict):
        errors.append(f"{story_id}: missing teaser_contract; legacy Script rejected")
        return
    strategy = contract.get("opening_strategy")
    if strategy not in ALLOWED_OPENING_STRATEGIES:
        errors.append(
            f"{story_id}: opening_strategy={strategy!r} is not allowed by the current hard gate"
        )
    if strategy == "future_preview_reprise":
        errors.append(f"{story_id}: legacy future_preview_reprise is blocked")
    candidate_id = contract.get("primary_highlight_candidate_id")
    candidate = candidates.get(candidate_id)
    if not isinstance(candidate, dict):
        errors.append(f"{story_id}: primary highlight candidate is absent from current catalog")
        return
    if candidate.get("type") != "highlight":
        errors.append(f"{story_id}: primary opening candidate is not a highlight")
    strength = candidate.get("strength")
    if not isinstance(strength, (int, float)) or strength < MIN_OPENING_STRENGTH:
        errors.append(
            f"{story_id}: opening highlight strength {strength!r} is below the hard minimum "
            f"{MIN_OPENING_STRENGTH}"
        )
    for field in ("source_id", "anchor", "reason"):
        if not isinstance(candidate.get(field), str) or not candidate[field].strip():
            errors.append(f"{story_id}: opening candidate missing readable {field}")
    start = candidate.get("start")
    end = candidate.get("end")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
        errors.append(f"{story_id}: opening candidate has invalid source interval")

    if script.get("edit_mode") not in {"montage", "original_chronological"}:
        errors.append(f"{story_id}: edit_mode is not explicitly declared")
    if not isinstance(script.get("edit_mode_reason"), str) or not script["edit_mode_reason"].strip():
        errors.append(f"{story_id}: edit_mode_reason is missing")
    scope = script.get("scope_policy", {})
    if not isinstance(scope, dict):
        errors.append(f"{story_id}: scope_policy is missing")
    else:
        if scope.get("analysis_unit_policy") != "processing_only":
            errors.append(f"{story_id}: analysis units are incorrectly used as story scope")
        if scope.get("story_scope_policy") != "series_global":
            errors.append(f"{story_id}: story scope is not series_global")
        if scope.get("cross_unit_retrieval_allowed") is not True:
            errors.append(f"{story_id}: cross-unit causal retrieval is not enabled")
    if not isinstance(script.get("editorial_contract"), dict):
        errors.append(f"{story_id}: editorial_contract is missing")

    opening_selection = script.get("opening_selection")
    if not isinstance(opening_selection, dict):
        errors.append(
            f"{story_id}: opening_selection is missing; opening cannot be approved from a clip-first workflow"
        )
    else:
        if opening_selection.get("selection_basis") != "full_series_understanding":
            errors.append(
                f"{story_id}: opening_selection.selection_basis must be full_series_understanding"
            )
        reviewed = opening_selection.get("candidate_pool_reviewed")
        if not isinstance(reviewed, list) or not reviewed or candidate_id not in reviewed:
            errors.append(
                f"{story_id}: opening_selection must include the selected candidate in a non-empty comparison pool"
            )
        validation = opening_selection.get("story_arc_validation")
        if not isinstance(validation, dict) or validation.get("status") != "validated":
            errors.append(
                f"{story_id}: opening_selection.story_arc_validation is not validated"
            )
        else:
            if validation.get("primary_story_thread_id") != script.get(
                "primary_story_thread_id"
            ):
                errors.append(
                    f"{story_id}: opening selection primary thread differs from Story Script primary_story_thread_id"
                )
            shared_events = validation.get("shared_event_ids")
            candidate_events = set(candidate.get("event_ids", []))
            story_events = set(script.get("evidence_event_ids", []))
            for beat in script.get("beats", []):
                if isinstance(beat, dict):
                    story_events.update(
                        item for item in beat.get("event_ids", []) if isinstance(item, str)
                    )
            if (
                not isinstance(shared_events, list)
                or not shared_events
                or not candidate_events.intersection(shared_events)
                or not candidate_events.intersection(story_events)
            ):
                errors.append(
                    f"{story_id}: selected opening has no shared Event evidence with the validated Story Arc"
                )
        comparison = opening_selection.get("candidate_comparison")
        if not isinstance(comparison, list) or not comparison:
            errors.append(
                f"{story_id}: opening_selection.candidate_comparison is missing"
            )
        elif not any(
            isinstance(item, dict)
            and item.get("candidate_id") == candidate_id
            and item.get("decision") == "selected"
            for item in comparison
        ):
            errors.append(
                f"{story_id}: candidate_comparison does not mark the selected opening candidate"
            )

    blocks = plan.get("blocks", [])
    if not isinstance(blocks, list) or not blocks:
        errors.append(f"{story_id}: Story Plan has no opening Block")
        return
    ordered = sorted(blocks, key=lambda item: item.get("play_order", 0))
    first = ordered[0]
    if first.get("role") != "teaser":
        errors.append(f"{story_id}: first Story Plan Block is not teaser")
        return
    clips = first.get("clips", [])
    if not isinstance(clips, list) or not clips:
        errors.append(f"{story_id}: teaser Block has no Clip")
        return
    opening_clip = clips[0]
    if opening_clip.get("source_id") != candidate.get("source_id"):
        errors.append(f"{story_id}: Story Plan teaser source differs from selected highlight")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
        clip_start = opening_clip.get("source_start")
        clip_end = opening_clip.get("source_end")
        if not isinstance(clip_start, (int, float)) or not isinstance(clip_end, (int, float)):
            errors.append(f"{story_id}: teaser Clip lacks source boundaries")
        elif clip_start > start + 0.05 or clip_end < end - 0.05:
            errors.append(
                f"{story_id}: teaser Clip does not cover the selected highlight interval"
            )


def validate(job_root: Path, *, require_seal: bool = False) -> dict[str, Any]:
    job_root = job_root.expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    paths = _required_paths(job_root)
    for label, path in paths.items():
        if not path.is_file():
            errors.append(f"missing canonical pipeline artifact [{label}]: {path}")
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}

    project = load_json(paths["project"])
    scripts_index = load_json(paths["story_scripts_index"])
    script_preflight = load_json(paths["story_script_preflight"])
    approval = load_json(paths["story_approval"])
    plan_preflight = load_json(paths["story_plan_preflight"])
    base_plan_index = load_json(paths["story_plans_index"])
    qc_batch = load_json(paths["story_qc_batch"])
    qc_index = load_json(paths["story_qc_index"])
    catalog_payload = load_json(job_root / "highlight-hook-catalog.json")
    candidates = {
        item.get("id"): item
        for item in catalog_payload.get("candidates", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }

    # The effective Plan Index is the one named by Story QC, not a stale base
    # index left behind by a repair round.
    effective_plan_path = _path_from(qc_batch.get("story_plan_index_path"))
    if effective_plan_path is None or not effective_plan_path.is_file():
        errors.append("Story QC batch has no current effective Story Plan Index")
        effective_plan = base_plan_index
        effective_plan_path = paths["story_plans_index"]
    else:
        effective_plan = load_json(effective_plan_path)

    if approval.get("fulfillment_status") != "ready":
        errors.append("Story Approval is not ready")
    approved = _index_by_story(
        [item for item in approval.get("stories", []) if item.get("decision") == "approved"]
    )
    script_entries = _index_by_story(scripts_index.get("stories"))
    preflight_entries = _index_by_story(script_preflight.get("stories"))
    plan_preflight_entries = _index_by_story(plan_preflight.get("stories"))
    plan_entries = _index_by_story(effective_plan.get("plans"))
    qc_entries = _index_by_story(qc_index.get("reports"))
    story_ids = sorted(plan_entries)
    if not story_ids:
        errors.append("effective Story Plan Index contains no stories")

    review_path = job_root / "story-qc-review-decision.json"
    review_entries: dict[str, dict[str, Any]] = {}
    if review_path.is_file():
        review_entries = _index_by_story(load_json(review_path).get("stories"))

    for story_id in story_ids:
        approval_entry = approved.get(story_id)
        script_entry = script_entries.get(story_id)
        preflight_entry = preflight_entries.get(story_id)
        plan_preflight_entry = plan_preflight_entries.get(story_id)
        plan_entry = plan_entries.get(story_id)
        qc_entry = qc_entries.get(story_id)
        if approval_entry is None:
            errors.append(f"{story_id}: Story Approval is not approved")
            continue
        if not isinstance(script_entry, dict) or not isinstance(script_entry.get("path"), str):
            errors.append(f"{story_id}: Story Script Index entry is missing")
            continue
        script_path = Path(script_entry["path"]).expanduser().resolve()
        if not script_path.is_file():
            errors.append(f"{story_id}: Story Script is missing: {script_path}")
            continue
        script = load_json(script_path)
        if script_entry.get("script_sha256") and script_entry["script_sha256"] != sha256_file(script_path):
            errors.append(f"{story_id}: Story Script Index SHA-256 is stale")
        if approval_entry.get("approved_script_sha256") != sha256_file(script_path):
            errors.append(f"{story_id}: Story Approval does not bind current Story Script")
        if not isinstance(preflight_entry, dict) or preflight_entry.get("feasibility_status") == "not_feasible":
            errors.append(f"{story_id}: Story Script preflight is not feasible")
        if not isinstance(plan_preflight_entry, dict) or plan_preflight_entry.get("status") != "ready":
            errors.append(f"{story_id}: Story Plan preflight is not ready")
        if not isinstance(plan_entry, dict) or plan_entry.get("status") != "ready_for_video_qc":
            errors.append(f"{story_id}: effective Story Plan is not ready_for_video_qc")
        if isinstance(plan_entry, dict):
            plan_path = _path_from(plan_entry.get("path"))
            if plan_path is None or not plan_path.is_file():
                errors.append(f"{story_id}: effective Story Plan file is missing")
                plan = {}
            else:
                if plan_entry.get("plan_sha256") != sha256_file(plan_path):
                    errors.append(f"{story_id}: Story Plan SHA-256 is stale")
                plan = load_json(plan_path)
        else:
            plan = {}
        _check_opening_contract(
            errors,
            story_id=story_id,
            script=script,
            plan=plan,
            candidates=candidates,
        )
        if not isinstance(qc_entry, dict):
            errors.append(f"{story_id}: Story QC report is missing")
        else:
            qc_status = qc_entry.get("status")
            if qc_status == "blocked":
                errors.append(f"{story_id}: Story QC is blocked")
            elif qc_status != "approved":
                review = review_entries.get(story_id)
                if not isinstance(review, dict) or review.get("decision") != "accepted_for_render":
                    errors.append(f"{story_id}: non-approved Story QC has no explicit review decision")
            report_path = _path_from(qc_entry.get("path"))
            if report_path is None or not report_path.is_file():
                errors.append(f"{story_id}: Story QC report file is missing")
            else:
                report = load_json(report_path)
                if qc_entry.get("report_sha256") != sha256_file(report_path):
                    errors.append(f"{story_id}: Story QC report SHA-256 is stale")
                if report.get("blocked_clip_ids"):
                    errors.append(f"{story_id}: Story QC has blocked clips")

    # A seal is created only by the canonical orchestrator immediately before
    # Render Recipe materialization.  Its hashes make later ad-hoc edits stale.
    if require_seal:
        seal_path = job_root / "story-pipeline-gate.json"
        if not seal_path.is_file():
            errors.append(f"missing canonical pipeline seal: {seal_path}")
        else:
            seal = load_json(seal_path)
            if seal.get("status") != "ready_for_render":
                errors.append("canonical pipeline seal is not ready_for_render")
            if seal.get("gate_version") != PIPELINE_GATE_VERSION:
                errors.append("canonical pipeline seal uses an old gate version")
            for label, path in {
                "story_approval": paths["story_approval"],
                "story_script_preflight": paths["story_script_preflight"],
                "story_plan_preflight": paths["story_plan_preflight"],
                "effective_story_plan_index": effective_plan_path,
                "story_qc_index": paths["story_qc_index"],
                "story_qc_batch": paths["story_qc_batch"],
            }.items():
                expected = seal.get("input_sha256", {}).get(label)
                if expected != sha256_file(path):
                    errors.append(f"canonical pipeline seal is stale: {label}")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "gate_version": PIPELINE_GATE_VERSION,
        "story_ids": story_ids,
        "effective_story_plan_index": str(effective_plan_path),
    }


def seal(job_root: Path) -> dict[str, Any]:
    job_root = job_root.expanduser().resolve()
    report = validate(job_root, require_seal=False)
    if not report["ok"]:
        raise ValueError("canonical Story pipeline is not ready: " + "; ".join(report["errors"][:40]))
    paths = _required_paths(job_root)
    effective_plan_path = Path(report["effective_story_plan_index"])
    payload = {
        "schema_version": "1.0",
        "gate_version": PIPELINE_GATE_VERSION,
        "status": "ready_for_render",
        "created_by": "run_pipeline.py:story_render",
        "input_sha256": {
            "story_approval": sha256_file(paths["story_approval"]),
            "story_script_preflight": sha256_file(paths["story_script_preflight"]),
            "story_plan_preflight": sha256_file(paths["story_plan_preflight"]),
            "effective_story_plan_index": sha256_file(effective_plan_path),
            "story_qc_index": sha256_file(paths["story_qc_index"]),
            "story_qc_batch": sha256_file(paths["story_qc_batch"]),
        },
        "story_ids": report["story_ids"],
        "policy": {
            "legacy_direct_ffmpeg_recipe": "blocked",
            "historical_mp4_reuse": "blocked",
            "opening_strength_minimum": MIN_OPENING_STRENGTH,
            "allowed_opening_strategies": sorted(ALLOWED_OPENING_STRATEGIES),
        },
    }
    atomic_write_json(job_root / "story-pipeline-gate.json", payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    seal_parser = sub.add_parser("seal")
    seal_parser.add_argument("job_root", type=Path)
    check_parser = sub.add_parser("check")
    check_parser.add_argument("job_root", type=Path)
    check_parser.add_argument("--require-seal", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "seal":
            result = seal(args.job_root)
            print(f"STORY_PIPELINE_GATE\t{args.job_root / 'story-pipeline-gate.json'}")
            print(f"STATUS\t{result['status']}")
            return 0
        result = validate(args.job_root, require_seal=args.require_seal)
        print(f"STATUS\t{'ok' if result['ok'] else 'blocked'}")
        for error in result["errors"]:
            print(f"ERROR\t{error}")
        return 0 if result["ok"] else 1
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"ERROR\t{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
