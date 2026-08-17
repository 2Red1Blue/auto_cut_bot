#!/usr/bin/env python3
"""Build immutable local Render Recipes for individually approved Stories."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from autocut_core.libs.repair_story_audio_boundaries import resolve_qc_plan_index
from autocut_core.io import (
    atomic_write_json,
    atomic_write_text,
    load_json,
    sha256_file,
    update_project_stage,
)
from autocut_core.libs.story_render_common import (
    RECIPE_INDEX_METHOD,
    build_render_recipe,
    ordered_plan_clips,
    resolve_local_sources,
)
from autocut_core.libs.story_qc_review import load_validated_review
from autocut_core.schema.compat import validate_task_response
from autocut_core.contracts.qc_validation import validate as validate_story_qc
from autocut_core.libs.story_pipeline_gate import validate as validate_story_pipeline_gate


def choose_local_manifest(
    job_root: Path,
    batch: dict[str, Any],
    explicit: Path | None,
) -> Path:
    if explicit is not None:
        path = explicit.expanduser().resolve()
    else:
        value = batch.get("audio_boundary", {}).get(
            "local_source_manifest_path"
        )
        path = (
            Path(value).expanduser().resolve()
            if isinstance(value, str) and value.strip()
            else job_root / "source_manifest.json"
        )
    if not path.is_file():
        raise FileNotFoundError(f"missing local Source Manifest: {path}")
    declared = batch.get("audio_boundary", {}).get(
        "local_source_manifest_sha256"
    )
    if declared is not None and path == Path(
        batch["audio_boundary"]["local_source_manifest_path"]
    ).expanduser().resolve():
        if declared != sha256_file(path):
            raise ValueError("Story QC local Source Manifest SHA-256 is stale")
    return path


def index_status(recipe_count: int, skipped_count: int) -> str:
    if recipe_count and skipped_count == 0:
        return "complete"
    if recipe_count:
        return "partial"
    return "blocked"


def render_review(index: dict[str, Any]) -> str:
    lines = [
        "# Story Render Recipe Review",
        "",
        f"- Status: `{index['status']}`",
        f"- QC reports: {index['qc_report_count']}",
        f"- Recipes: {index['recipe_count']}",
        f"- Skipped: {index['skipped_story_count']}",
        "",
        "## Ready",
        "",
    ]
    if index["recipes"]:
        for item in index["recipes"]:
            lines.append(
                f"- Slot {item['production_slot']} · `{item['story_id']}` · "
                f"{item['expected_duration_seconds']:.3f}s · "
                f"`{item['output_filename']}`"
                + (
                    " · 人工确认放行（保留 QC/VAD 风险）"
                    if item.get("render_authorization") == "human_accepted_qc"
                    else ""
                )
            )
    else:
        lines.append("- None")
    lines.extend(["", "## Skipped", ""])
    if index["skipped"]:
        for item in index["skipped"]:
            lines.append(
                f"- Slot {item['production_slot']} · `{item['story_id']}` · "
                f"`{item['qc_status']}` · {item['reason']}"
            )
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "每份 Recipe 只使用当前有效 Story Plan、本地原片和一个 "
            "0.35 秒黑场分隔；Teaser 末段与正文首段各附 0.18s 线性 fade "
            "包络；其他 Junction 保持硬切。基础 Story Plan 不足 300 秒时，"
            "Story Plan 不足 300 秒时，先沿最后一个 Source 播放到该集片尾；"
            "仍不足则按集号连续追加后续集，达到 300 秒后继续到达到门槛所在集片尾，"
            "并在 tail_extension.segments 中记录每个真实源文件、集号、时间码和 SHA 绑定。",
            "",
        ]
    )
    return "\n".join(lines)


def build(
    job_root: Path,
    *,
    local_source_manifest: Path | None = None,
    require_qc_validation: bool = True,
) -> dict[str, Any]:
    job_root = job_root.expanduser().resolve()
    pipeline_gate = validate_story_pipeline_gate(job_root, require_seal=True)
    if not pipeline_gate["ok"]:
        raise ValueError(
            "Canonical Story pipeline gate failed: "
            + "; ".join(pipeline_gate["errors"][:40])
        )
    if require_qc_validation:
        qc_validation = validate_story_qc(job_root)
        if not qc_validation["ok"]:
            raise ValueError(
                "Story QC validation failed: "
                + "; ".join(qc_validation["errors"][:30])
            )
    source_manifest_path = job_root / "source_manifest.json"
    qc_index_path = job_root / "story-qc" / "index.json"
    batch_path = job_root / "story-qc-batch.json"
    required_paths = (source_manifest_path, qc_index_path, batch_path)
    missing = [str(path) for path in required_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "missing render input artifact(s): " + ", ".join(missing)
        )
    qc_index = load_json(qc_index_path)
    batch = load_json(batch_path)
    review_path, review_entries, review_errors = load_validated_review(job_root)
    if review_errors:
        raise ValueError(
            "Story QC human review decision is invalid: "
            + "; ".join(review_errors[:30])
        )
    review_sha256 = sha256_file(review_path) if review_path is not None else None
    _, effective_index_path = resolve_qc_plan_index(job_root, batch)
    effective_index = load_json(effective_index_path)
    local_manifest_path = choose_local_manifest(
        job_root, batch, local_source_manifest
    )
    plan_entries = {
        item["story_id"]: item
        for item in effective_index.get("plans", [])
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }
    render_inputs: list[
        tuple[
            dict[str, Any],
            dict[str, Any],
            dict[str, Any],
            Path,
            Path,
            dict[str, Any] | None,
        ]
    ] = []
    skipped: list[dict[str, Any]] = []
    required_source_ids: set[str] = set()
    for report_entry in sorted(
        qc_index["reports"], key=lambda item: item["production_slot"]
    ):
        authorization = None
        if report_entry["status"] != "approved":
            authorization = review_entries.get(report_entry["story_id"])
            if not authorization or authorization.get("decision") != "accepted_for_render":
                reason = (
                    "只有 Story QC status=approved，或独立人工 QC 放行记录为 "
                    "accepted_for_render，才可进入本地正式渲染。"
                )
                skipped.append(
                    {
                        "story_id": report_entry["story_id"],
                        "title": report_entry["title"],
                        "production_slot": report_entry["production_slot"],
                        "qc_status": report_entry["status"],
                        "reason": reason,
                    }
                )
                continue
        if report_entry["status"] == "approved":
            render_authorization = None
        else:
            render_authorization = {
                "decision_path": str(review_path),
                "decision_sha256": review_sha256,
                "decision": authorization["decision"],
                "qc_status": authorization["status"],
                "note": authorization["note"],
                "decided_at": authorization["decided_at"],
            }
        story_id = report_entry["story_id"]
        plan_entry = plan_entries.get(story_id)
        if plan_entry is None:
            raise ValueError(f"{story_id}: effective Story Plan is absent")
        report_path = Path(report_entry["path"]).expanduser().resolve()
        plan_path = Path(plan_entry["path"]).expanduser().resolve()
        if not report_path.is_file() or not plan_path.is_file():
            raise FileNotFoundError(f"{story_id}: QC report or Story Plan is missing")
        if report_entry["report_sha256"] != sha256_file(report_path):
            raise ValueError(f"{story_id}: Story QC report SHA-256 is stale")
        if plan_entry["plan_sha256"] != sha256_file(plan_path):
            raise ValueError(f"{story_id}: effective Story Plan SHA-256 is stale")
        report = load_json(report_path)
        plan = load_json(plan_path)
        if report["input_fingerprints"]["story_plan_sha256"] != sha256_file(
            plan_path
        ):
            raise ValueError(f"{story_id}: QC report binds another Story Plan")
        required_source_ids.update(
            clip["source_id"] for _, clip in ordered_plan_clips(plan)
        )
        render_inputs.append(
            (report_entry, plan_entry, report, report_path, plan_path, render_authorization)
        )
    # Resolve the complete local catalog so deterministic Recipe materialization
    # can continue into later episodes when the selected Story Plan is shorter
    # than the delivery floor. The renderer still only writes Sources actually
    # referenced by each Recipe.
    local_manifest_catalog = load_json(local_manifest_path).get("sources", [])
    catalog_source_ids = {
        item["id"]
        for item in local_manifest_catalog
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    local_sources = resolve_local_sources(
        source_manifest_path,
        local_manifest_path,
        required_source_ids | catalog_source_ids,
    )
    repair = batch.get("boundary_repair")
    if not isinstance(repair, dict):
        raise ValueError("Story QC batch has no Boundary Repair metadata")
    repair_path_value = repair.get("path")
    repair_path = (
        Path(repair_path_value).expanduser().resolve()
        if isinstance(repair_path_value, str)
        else None
    )
    if (
        repair_path is None
        or not repair_path.is_file()
        or repair.get("sha256") != sha256_file(repair_path)
    ):
        raise ValueError("Story Boundary Repair metadata is stale")
    output_dir = job_root / "story-render-recipes"
    output_dir.mkdir(parents=True, exist_ok=True)
    recipes: list[dict[str, Any]] = []
    for (
        report_entry,
        plan_entry,
        report,
        report_path,
        plan_path,
        human_qc_review,
    ) in render_inputs:
        fingerprints = {
            "story_qc_index_sha256": sha256_file(qc_index_path),
            "story_qc_report_sha256": sha256_file(report_path),
            "source_manifest_sha256": sha256_file(source_manifest_path),
            "local_source_manifest_sha256": sha256_file(local_manifest_path),
            "effective_story_plan_index_sha256": sha256_file(
                effective_index_path
            ),
            "effective_story_plan_sha256": sha256_file(plan_path),
            "boundary_repair_metadata_sha256": sha256_file(repair_path),
        }
        if human_qc_review is not None:
            fingerprints["story_qc_review_decision_sha256"] = review_sha256
        plan = load_json(plan_path)
        recipe = build_render_recipe(
            plan=plan,
            qc_report=report,
            local_sources=local_sources,
            input_fingerprints=fingerprints,
            human_qc_review=human_qc_review,
        )
        recipe_path = output_dir / f"{recipe['story_id']}.json"
        atomic_write_json(recipe_path, recipe)
        recipes.append(
            {
                "story_id": recipe["story_id"],
                "title": recipe["title"],
                "production_slot": recipe["production_slot"],
                "path": str(recipe_path),
                "recipe_sha256": sha256_file(recipe_path),
                "story_qc_report_sha256": sha256_file(report_path),
                "effective_story_plan_sha256": plan_entry["plan_sha256"],
                "expected_duration_seconds": recipe[
                    "expected_duration_seconds"
                ],
                "output_filename": recipe["output_filename"],
                "render_authorization": (
                    "human_accepted_qc"
                    if human_qc_review is not None
                    else "qc_approved"
                ),
            }
        )
    index = {
        "schema_version": "1.0",
        "method": RECIPE_INDEX_METHOD,
        "status": index_status(len(recipes), len(skipped)),
        "story_qc_index_sha256": sha256_file(qc_index_path),
        "effective_story_plan_index_sha256": sha256_file(
            effective_index_path
        ),
        "source_manifest_sha256": sha256_file(source_manifest_path),
        "local_source_manifest_path": str(local_manifest_path),
        "local_source_manifest_sha256": sha256_file(local_manifest_path),
        **(
            {
                "story_qc_review_decision_path": str(review_path),
                "story_qc_review_decision_sha256": review_sha256,
            }
            if review_path is not None
            else {}
        ),
        "qc_report_count": len(qc_index["reports"]),
        "approved_story_count": int(qc_index["approved_count"]),
        "recipe_count": len(recipes),
        "skipped_story_count": len(skipped),
        "recipes": sorted(recipes, key=lambda item: item["production_slot"]),
        "skipped": sorted(skipped, key=lambda item: item["production_slot"]),
    }
    schema_errors = validate_task_response("story_render_recipe_index", index)
    if schema_errors:
        raise ValueError(
            "invalid Story Render Recipe Index: "
            + "; ".join(schema_errors[:30])
        )
    index_path = output_dir / "index.json"
    atomic_write_json(index_path, index)
    review_path = job_root / "story-render-review.md"
    atomic_write_text(review_path, render_review(index))
    update_project_stage(
        job_root / "project.json",
        "story_render_recipes",
        index["status"],
        inputs={
            "story_qc_index": str(qc_index_path),
            "effective_story_plan_index": str(effective_index_path),
            "local_source_manifest": str(local_manifest_path),
        },
        outputs={
            "story_render_recipe_index": str(index_path),
            "story_render_review": str(review_path),
        },
        note=(
            f"recipes={len(recipes)}; skipped={len(skipped)}; "
            "0.35s teaser-to-body black separator with 0.18s tri fade envelope"
        ),
    )
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_root", type=Path)
    parser.add_argument("--local-source-manifest", type=Path)
    args = parser.parse_args()
    try:
        index = build(
            args.job_root,
            local_source_manifest=args.local_source_manifest,
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        print(f"ERROR\t{exc}")
        return 1
    index_path = (
        args.job_root.expanduser().resolve()
        / "story-render-recipes"
        / "index.json"
    )
    print(f"STORY_RENDER_RECIPE_INDEX\t{index_path}")
    print(f"STATUS\t{index['status']}")
    return 0 if index["recipe_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
