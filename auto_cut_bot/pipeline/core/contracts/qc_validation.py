"""Validate Story QC proxies, video results, reports, hashes and status."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from autocut_core.libs.qc_report import (
    build_report,
    load_validated_audio_boundary,
    resolve_qc_plan_index,
)
from autocut_core.libs.qc_admission import validate_admission
from autocut_core.io import atomic_write_json, load_json, sha256_file
from autocut_core.schema.compat import validate_task_response


def validate(job_root: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    base_plan_index_path = job_root / "story-plans" / "index.json"
    approval_path = job_root / "story-approval.json"
    source_manifest_path = job_root / "source_manifest.json"
    batch_path = job_root / "story-qc-batch.json"
    qc_index_path = job_root / "story-qc" / "index.json"
    required = (
        base_plan_index_path,
        approval_path,
        source_manifest_path,
        batch_path,
        qc_index_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        return {
            "ok": False,
            "errors": [f"missing Story QC artifact: {item}" for item in missing],
            "warnings": warnings,
        }
    approval = load_json(approval_path)
    batch = load_json(batch_path)
    try:
        _, plan_index_path = resolve_qc_plan_index(job_root, batch)
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        return {
            "ok": False,
            "errors": [f"Story QC effective Story Plan is invalid: {exc}"],
            "warnings": warnings,
        }
    plan_index = load_json(plan_index_path)
    index = load_json(qc_index_path)
    errors.extend(
        f"story_qc.index: {item}"
        for item in validate_task_response("story_qc_index", index)
    )
    current_plan_index_sha256 = sha256_file(plan_index_path)
    current_batch_sha256 = sha256_file(batch_path)
    current_source_manifest_sha256 = sha256_file(source_manifest_path)
    if batch.get("story_plan_index_sha256") != current_plan_index_sha256:
        errors.append("Story QC batch Story Plan Index SHA-256 is stale")
    if batch.get("source_manifest_sha256") != current_source_manifest_sha256:
        errors.append("Story QC batch Source Manifest SHA-256 is stale")
    admission_path_value = batch.get("story_plan_qc_admission_path")
    admission_sha256 = batch.get("story_plan_qc_admission_sha256")
    if isinstance(admission_path_value, str):
        admission_path = Path(admission_path_value).expanduser().resolve()
        if (
            not admission_path.is_file()
            or sha256_file(admission_path) != admission_sha256
        ):
            errors.append("Story Plan QC admission is missing or stale")
        else:
            _, _, admission_errors = validate_admission(
                job_root,
                admission_path,
            )
            errors.extend(
                f"story_plan_qc_admission: {item}"
                for item in admission_errors
            )
    elif admission_sha256 is not None:
        errors.append("Story QC batch admission hash has no path")
    if index.get("story_plan_index_sha256") != current_plan_index_sha256:
        errors.append("Story QC Index Story Plan Index SHA-256 is stale")
    if index.get("story_qc_batch_sha256") != current_batch_sha256:
        errors.append("Story QC Index batch SHA-256 is stale")
    try:
        audio_boundary_metadata, audio_boundary_report = (
            load_validated_audio_boundary(
                batch,
                plan_index_path=plan_index_path,
                source_manifest_path=source_manifest_path,
            )
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"Story local audio boundary is invalid: {exc}")
        return {"ok": False, "errors": errors, "warnings": warnings}
    plan_entries = {
        item["story_id"]: item
        for item in plan_index.get("plans", [])
        if isinstance(item, dict)
    }
    approval_entries = {
        item["story_id"]: item
        for item in approval.get("stories", [])
        if isinstance(item, dict) and item.get("decision") == "approved"
    }
    report_entries: dict[str, dict[str, Any]] = {}
    for entry in index.get("reports", []):
        if not isinstance(entry, dict) or not isinstance(
            entry.get("story_id"), str
        ):
            errors.append("Story QC Index contains invalid report entry")
            continue
        story_id = entry["story_id"]
        if story_id in report_entries:
            errors.append(f"Story QC Index contains duplicate Story: {story_id}")
        report_entries[story_id] = entry
    if set(report_entries) != set(plan_entries):
        errors.append(
            "Story QC reports differ from Story Plans: "
            f"missing={sorted(set(plan_entries) - set(report_entries))}, "
            f"extra={sorted(set(report_entries) - set(plan_entries))}"
        )
    proxy_by_story: dict[str, Path] = {}
    for value in batch.get("proxy_manifests", []):
        if not isinstance(value, str):
            errors.append("Story QC batch has non-string proxy manifest path")
            continue
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            errors.append(f"missing Story QC proxy manifest: {path}")
            continue
        manifest = load_json(path)
        schema_errors = validate_task_response(
            "story_qc_proxy_manifest", manifest
        )
        errors.extend(
            f"{path.name}: {item}" for item in schema_errors
        )
        story_id = manifest.get("story_id")
        if not isinstance(story_id, str) or story_id in proxy_by_story:
            errors.append(f"invalid or duplicate QC proxy Story: {story_id!r}")
            continue
        proxy_by_story[story_id] = path
        plan_entry = plan_entries.get(story_id)
        approval_entry = approval_entries.get(story_id)
        if plan_entry is None or approval_entry is None:
            continue
        plan_path = Path(plan_entry["path"]).expanduser().resolve()
        script_path = Path(approval_entry["script_path"]).expanduser().resolve()
        expected_fingerprints = {
            "story_plan_index_sha256": current_plan_index_sha256,
            "story_plan_sha256": sha256_file(plan_path),
            "story_script_sha256": sha256_file(script_path),
            "source_manifest_sha256": current_source_manifest_sha256,
        }
        if manifest.get("input_fingerprints") != expected_fingerprints:
            errors.append(f"{story_id}: QC proxy input fingerprints are stale")
        media_records = [manifest.get("story_proxy", {})] + list(
            manifest.get("review_assets", [])
        )
        for media in media_records:
            path_value = media.get("path")
            if not isinstance(path_value, str):
                errors.append(f"{story_id}: QC media path is invalid")
                continue
            media_path = Path(path_value).expanduser().resolve()
            if not media_path.is_file():
                errors.append(f"{story_id}: missing QC media {media_path}")
                continue
            if media.get("sha256") != sha256_file(media_path):
                errors.append(f"{story_id}: QC media SHA-256 is stale")
        for asset in manifest.get("review_assets", []):
            context_value = asset.get("context_path")
            context_path = (
                Path(context_value).expanduser().resolve()
                if isinstance(context_value, str)
                else None
            )
            if context_path is None or not context_path.is_file():
                errors.append(
                    f"{story_id}/{asset.get('review_id')}: QC context is missing"
                )
            elif asset.get("context_sha256") != sha256_file(context_path):
                errors.append(
                    f"{story_id}/{asset.get('review_id')}: "
                    "QC context SHA-256 is stale"
                )
    if set(proxy_by_story) != set(plan_entries):
        errors.append("QC proxy manifests do not cover Story Plans exactly")
    jobs = [item for item in batch.get("jobs", []) if isinstance(item, dict)]
    seen_job_ids: set[str] = set()
    for job in jobs:
        job_id = job.get("id")
        if not isinstance(job_id, str) or job_id in seen_job_ids:
            errors.append(f"Story QC batch has invalid/duplicate job ID: {job_id!r}")
            continue
        seen_job_ids.add(job_id)
        if job.get("task") != "story_video_qc":
            errors.append(f"{job_id}: unexpected Story QC task")
        for field in ("context_file", "media_file", "output"):
            value = job.get(field)
            if not isinstance(value, str) or not Path(
                value
            ).expanduser().resolve().is_file():
                errors.append(f"{job_id}: missing {field}")
    reports: list[dict[str, Any]] = []
    for story_id, entry in report_entries.items():
        path_value = entry.get("path")
        if not isinstance(path_value, str):
            errors.append(f"{story_id}: QC report path is invalid")
            continue
        report_path = Path(path_value).expanduser().resolve()
        if not report_path.is_file():
            errors.append(f"{story_id}: missing QC report")
            continue
        if entry.get("report_sha256") != sha256_file(report_path):
            errors.append(f"{story_id}: QC report SHA-256 is stale")
        report = load_json(report_path)
        reports.append(report)
        schema_errors = validate_task_response("story_qc_report", report)
        errors.extend(
            f"{story_id}.report: {item}" for item in schema_errors
        )
        plan_entry = plan_entries.get(story_id)
        approval_entry = approval_entries.get(story_id)
        proxy_path = proxy_by_story.get(story_id)
        if not all(
            (
                isinstance(plan_entry, dict),
                isinstance(approval_entry, dict),
                isinstance(proxy_path, Path),
            )
        ):
            continue
        assert proxy_path is not None
        try:
            expected = build_report(
                job_root=job_root,
                plan_index_path=plan_index_path,
                plan_entry=plan_entry,
                approval_entry=approval_entry,
                source_manifest_path=source_manifest_path,
                batch_path=batch_path,
                proxy_manifest_path=proxy_path,
                jobs=jobs,
                audio_boundary_metadata=audio_boundary_metadata,
                audio_boundary_report=audio_boundary_report,
                boundary_repair_metadata=batch["boundary_repair"],
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
            errors.append(f"{story_id}: cannot recompute QC report: {exc}")
            continue
        if report != expected:
            errors.append(
                f"{story_id}: QC report differs from deterministic aggregation"
            )
        expected_entry = {
            "story_id": story_id,
            "title": report.get("title"),
            "production_slot": report.get("production_slot"),
            "status": report.get("status"),
            "path": str(report_path),
            "report_sha256": sha256_file(report_path),
            "story_plan_sha256": report.get("input_fingerprints", {}).get(
                "story_plan_sha256"
            ),
            "proxy_manifest_sha256": report.get(
                "input_fingerprints", {}
            ).get("proxy_manifest_sha256"),
        }
        if entry != expected_entry:
            errors.append(f"{story_id}: QC Index entry is inconsistent")
    approved_count = sum(item.get("status") == "approved" for item in reports)
    review_count = sum(item.get("status") == "review" for item in reports)
    blocked_count = sum(item.get("status") == "blocked" for item in reports)
    expected_status = (
        "blocked"
        if not reports or blocked_count
        else "review"
        if review_count
        else "approved"
    )
    expected_counts = {
        "report_count": len(reports),
        "approved_count": approved_count,
        "review_count": review_count,
        "blocked_count": blocked_count,
        "status": expected_status,
    }
    for field, expected in expected_counts.items():
        if index.get(field) != expected:
            errors.append(
                f"Story QC Index {field} is inconsistent: "
                f"expected {expected!r}, got {index.get(field)!r}"
            )
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
        else job_root / "story-qc-validation.json"
    )
    atomic_write_json(output_path, report)
    for warning in report["warnings"]:
        print(f"WARNING\t{warning}")
    for error in report["errors"]:
        print(f"ERROR\t{error}")
    print(f"STORY_QC_VALIDATION\t{output_path}")
    print(f"STATUS\t{'ok' if report['ok'] else 'blocked'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())