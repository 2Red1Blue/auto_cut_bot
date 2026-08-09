#!/usr/bin/env python3
"""Validate approved-QC Story Render Recipes and all input fingerprints."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from autocut_core.libs.build_story_render_recipes import index_status
from autocut_core.libs.repair_story_audio_boundaries import resolve_qc_plan_index
from autocut_core.io import atomic_write_json, load_json, sha256_file
from autocut_core.libs.story_render_common import (
    build_render_recipe,
    ordered_plan_clips,
    resolve_local_sources,
    validate_render_recipe,
)
from autocut_core.libs.story_qc_review import load_validated_review
from autocut_core.schema.compat import validate_task_response
from autocut_core.contracts.qc_validation import validate as validate_story_qc


def validate(job_root: Path, *, check_files: bool = True) -> dict[str, Any]:
    job_root = job_root.expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    qc_validation = validate_story_qc(job_root)
    if not qc_validation["ok"]:
        return {
            "ok": False,
            "errors": [
                f"story_qc: {item}" for item in qc_validation["errors"]
            ],
            "warnings": warnings,
        }
    recipe_index_path = job_root / "story-render-recipes" / "index.json"
    qc_index_path = job_root / "story-qc" / "index.json"
    batch_path = job_root / "story-qc-batch.json"
    source_manifest_path = job_root / "source_manifest.json"
    required = (
        recipe_index_path,
        qc_index_path,
        batch_path,
        source_manifest_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        return {
            "ok": False,
            "errors": [f"missing Story Render artifact: {item}" for item in missing],
            "warnings": warnings,
        }
    index = load_json(recipe_index_path)
    qc_index = load_json(qc_index_path)
    batch = load_json(batch_path)
    review_path, review_entries, review_errors = load_validated_review(job_root)
    if review_errors:
        errors.extend(f"story_qc_review: {item}" for item in review_errors)
    errors.extend(
        f"story_render_recipes.index: {item}"
        for item in validate_task_response("story_render_recipe_index", index)
    )
    try:
        _, effective_index_path = resolve_qc_plan_index(job_root, batch)
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"effective Story Plan Index is invalid: {exc}")
        return {"ok": False, "errors": errors, "warnings": warnings}
    effective_index = load_json(effective_index_path)
    local_manifest_value = index.get("local_source_manifest_path")
    local_manifest_path = (
        Path(local_manifest_value).expanduser().resolve()
        if isinstance(local_manifest_value, str)
        else None
    )
    if local_manifest_path is None or not local_manifest_path.is_file():
        errors.append("local Source Manifest is missing")
        return {"ok": False, "errors": errors, "warnings": warnings}
    expected_index_fingerprints = {
        "story_qc_index_sha256": sha256_file(qc_index_path),
        "effective_story_plan_index_sha256": sha256_file(
            effective_index_path
        ),
        "source_manifest_sha256": sha256_file(source_manifest_path),
        "local_source_manifest_sha256": sha256_file(local_manifest_path),
    }
    for field, expected in expected_index_fingerprints.items():
        if index.get(field) != expected:
            errors.append(f"Story Render Recipe Index {field} is stale")
    if review_path is not None:
        if index.get("story_qc_review_decision_path") != str(review_path):
            errors.append("Story Render Recipe Index human QC decision path is stale")
        if index.get("story_qc_review_decision_sha256") != sha256_file(review_path):
            errors.append("Story Render Recipe Index human QC decision hash is stale")
    plan_entries = {
        item["story_id"]: item
        for item in effective_index.get("plans", [])
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }
    qc_entries = {
        item["story_id"]: item
        for item in qc_index.get("reports", [])
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }
    renderable_story_ids = {
        story_id
        for story_id, item in qc_entries.items()
        if item.get("status") == "approved"
        or review_entries.get(story_id, {}).get("decision") == "accepted_for_render"
    }
    required_source_ids: set[str] = set()
    for story_id, qc_entry in qc_entries.items():
        if story_id not in renderable_story_ids:
            continue
        plan_entry = plan_entries.get(story_id)
        if plan_entry is None:
            errors.append(f"{story_id}: approved Story has no effective Plan")
            continue
        plan_path = Path(plan_entry["path"]).expanduser().resolve()
        if not plan_path.is_file():
            errors.append(f"{story_id}: effective Story Plan is missing")
            continue
        plan = load_json(plan_path)
        required_source_ids.update(
            clip["source_id"] for _, clip in ordered_plan_clips(plan)
        )
    # Include the full local catalog so deterministic re-materialization sees
    # the same later episodes that a Recipe may append after its Story Plan.
    local_manifest_catalog = load_json(local_manifest_path).get("sources", [])
    catalog_source_ids = {
        item["id"]
        for item in local_manifest_catalog
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    try:
        local_sources = resolve_local_sources(
            source_manifest_path,
            local_manifest_path,
            required_source_ids | catalog_source_ids,
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"local render Sources are invalid: {exc}")
        return {"ok": False, "errors": errors, "warnings": warnings}
    repair = batch.get("boundary_repair")
    repair_path = (
        Path(repair["path"]).expanduser().resolve()
        if isinstance(repair, dict) and isinstance(repair.get("path"), str)
        else None
    )
    if (
        repair_path is None
        or not repair_path.is_file()
        or repair.get("sha256") != sha256_file(repair_path)
    ):
        errors.append("Story Boundary Repair metadata is stale")
        return {"ok": False, "errors": errors, "warnings": warnings}
    recipe_entries: dict[str, dict[str, Any]] = {}
    for entry in index.get("recipes", []):
        story_id = entry.get("story_id")
        if not isinstance(story_id, str) or story_id in recipe_entries:
            errors.append(f"invalid or duplicate Render Recipe Story: {story_id!r}")
            continue
        recipe_entries[story_id] = entry
        qc_entry = qc_entries.get(story_id)
        plan_entry = plan_entries.get(story_id)
        authorization = None
        if qc_entry is None:
            errors.append(f"{story_id}: Render Recipe has no QC report")
            continue
        if qc_entry.get("status") != "approved":
            authorization_entry = review_entries.get(story_id)
            if not authorization_entry or authorization_entry.get("decision") != "accepted_for_render":
                errors.append(f"{story_id}: Render Recipe lacks human QC render authorization")
                continue
            authorization = {
                "decision_path": str(review_path),
                "decision_sha256": sha256_file(review_path),
                "decision": authorization_entry["decision"],
                "qc_status": authorization_entry["status"],
                "note": authorization_entry["note"],
                "decided_at": authorization_entry["decided_at"],
            }
        if plan_entry is None:
            errors.append(f"{story_id}: Render Recipe has no effective Story Plan")
            continue
        recipe_path = Path(entry["path"]).expanduser().resolve()
        report_path = Path(qc_entry["path"]).expanduser().resolve()
        plan_path = Path(plan_entry["path"]).expanduser().resolve()
        for label, path in (
            ("recipe", recipe_path),
            ("QC report", report_path),
            ("effective Story Plan", plan_path),
        ):
            if not path.is_file():
                errors.append(f"{story_id}: missing {label}: {path}")
        if not all(path.is_file() for path in (recipe_path, report_path, plan_path)):
            continue
        if entry["recipe_sha256"] != sha256_file(recipe_path):
            errors.append(f"{story_id}: Render Recipe SHA-256 is stale")
        recipe = load_json(recipe_path)
        errors.extend(
            f"{story_id}.recipe: {item}"
            for item in validate_render_recipe(recipe, check_files=check_files)
        )
        fingerprints = {
            "story_qc_index_sha256": sha256_file(qc_index_path),
            "story_qc_report_sha256": sha256_file(report_path),
            "source_manifest_sha256": sha256_file(source_manifest_path),
            "local_source_manifest_sha256": sha256_file(
                local_manifest_path
            ),
            "effective_story_plan_index_sha256": sha256_file(
                effective_index_path
            ),
            "effective_story_plan_sha256": sha256_file(plan_path),
            "boundary_repair_metadata_sha256": sha256_file(repair_path),
        }
        if authorization is not None:
            fingerprints["story_qc_review_decision_sha256"] = sha256_file(review_path)
        try:
            expected_recipe = build_render_recipe(
                plan=load_json(plan_path),
                qc_report=load_json(report_path),
                local_sources=local_sources,
                input_fingerprints=fingerprints,
                human_qc_review=authorization,
            )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"{story_id}: cannot recompute Render Recipe: {exc}")
            continue
        if recipe != expected_recipe:
            errors.append(
                f"{story_id}: Render Recipe differs from deterministic materialization"
            )
        expected_entry = {
            "story_id": recipe["story_id"],
            "title": recipe["title"],
            "production_slot": recipe["production_slot"],
            "path": str(recipe_path),
            "recipe_sha256": sha256_file(recipe_path),
            "story_qc_report_sha256": sha256_file(report_path),
            "effective_story_plan_sha256": sha256_file(plan_path),
            "expected_duration_seconds": recipe[
                "expected_duration_seconds"
            ],
            "output_filename": recipe["output_filename"],
            "render_authorization": (
                "human_accepted_qc" if authorization is not None else "qc_approved"
            ),
        }
        if entry != expected_entry:
            errors.append(f"{story_id}: Render Recipe Index entry is inconsistent")
    if set(recipe_entries) != renderable_story_ids:
        errors.append(
            "Render Recipes do not exactly cover render-authorized Stories: "
            f"missing={sorted(renderable_story_ids - set(recipe_entries))}, "
            f"extra={sorted(set(recipe_entries) - renderable_story_ids)}"
        )
    expected_skipped = [
        {
            "story_id": item["story_id"],
            "title": item["title"],
            "production_slot": item["production_slot"],
            "qc_status": item["status"],
            "reason": (
                "只有 Story QC status=approved，或独立人工 QC 放行记录为 "
                "accepted_for_render，才可进入本地正式渲染。"
            ),
        }
        for item in sorted(
            qc_index["reports"], key=lambda value: value["production_slot"]
        )
        if item["story_id"] not in renderable_story_ids
    ]
    if index.get("skipped") != expected_skipped:
        errors.append("Render Recipe skipped Stories are inconsistent with Story QC")
    expected_counts = {
        "qc_report_count": len(qc_index["reports"]),
        "approved_story_count": int(qc_index["approved_count"]),
        "recipe_count": len(renderable_story_ids),
        "skipped_story_count": len(expected_skipped),
        "status": index_status(
            len(renderable_story_ids), len(expected_skipped)
        ),
    }
    for field, expected in expected_counts.items():
        if index.get(field) != expected:
            errors.append(
                f"Story Render Recipe Index {field} is inconsistent: "
                f"expected {expected!r}, got {index.get(field)!r}"
            )
    if index.get("status") == "partial":
        warnings.append("some Story QC reports remain skipped")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    job_root = args.job_root.expanduser().resolve()
    report = validate(job_root)
    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else job_root / "story-render-recipe-validation.json"
    )
    atomic_write_json(output_path, report)
    for warning in report["warnings"]:
        print(f"WARNING\t{warning}")
    for error in report["errors"]:
        print(f"ERROR\t{error}")
    print(f"STORY_RENDER_RECIPE_VALIDATION\t{output_path}")
    print(f"STATUS\t{'ok' if report['ok'] else 'blocked'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
