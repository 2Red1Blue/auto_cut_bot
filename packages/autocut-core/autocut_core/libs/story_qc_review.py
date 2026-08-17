#!/usr/bin/env python3
"""Record explicit human acceptance of Story QC risks for rendering.

This is deliberately separate from ``story-qc/<story-id>.json``.  A human
decision may authorize rendering a ``review`` or ``blocked`` report, but it
never changes that report, its local VAD result, or the portfolio QC status.
All decisions are bound to the current QC Index and individual report hashes.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from autocut_core.io import atomic_write_json, load_json, sha256_file, utc_now


METHOD = "human-story-qc-review-v1"
PENDING = "pending"
ACCEPTED = "accepted_for_render"
REJECTED = "rejected"
NOT_REQUIRED = "not_required"


def _index_path(job_root: Path) -> Path:
    return job_root / "story-qc" / "index.json"


def _report_entries(job_root: Path) -> dict[str, dict[str, Any]]:
    index_path = _index_path(job_root)
    if not index_path.is_file():
        raise FileNotFoundError(f"missing Story QC Index: {index_path}")
    index = load_json(index_path)
    result: dict[str, dict[str, Any]] = {}
    for entry in index.get("reports", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("story_id"), str):
            raise ValueError("Story QC Index contains an invalid report entry")
        story_id = entry["story_id"]
        if story_id in result:
            raise ValueError(f"duplicate Story QC report: {story_id}")
        report_path = Path(entry["path"]).expanduser().resolve()
        if not report_path.is_file():
            raise FileNotFoundError(f"missing Story QC report: {report_path}")
        if entry.get("report_sha256") != sha256_file(report_path):
            raise ValueError(f"stale Story QC report hash: {story_id}")
        result[story_id] = {
            "story_id": story_id,
            "title": entry.get("title"),
            "production_slot": entry.get("production_slot"),
            "status": entry.get("status"),
            "path": str(report_path),
            "report_sha256": sha256_file(report_path),
        }
    if not result:
        raise ValueError("Story QC Index has no reports")
    return result


def _aggregate_status(stories: list[dict[str, Any]]) -> str:
    decisions = {item.get("decision") for item in stories}
    if any(item == PENDING for item in decisions):
        return PENDING
    if any(item == REJECTED for item in decisions):
        return REJECTED
    return "ready_for_render"


def initialize(job_root: Path, output_path: Path) -> dict[str, Any]:
    job_root = job_root.expanduser().resolve()
    index_path = _index_path(job_root)
    reports = _report_entries(job_root)
    stories: list[dict[str, Any]] = []
    for item in sorted(reports.values(), key=lambda value: value["production_slot"]):
        status = item["status"]
        stories.append(
            {
                **item,
                "decision": NOT_REQUIRED if status == "approved" else PENDING,
                "note": "",
                "decided_at": None,
            }
        )
    artifact = {
        "schema_version": "1.0",
        "method": METHOD,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "story_qc_index_path": str(index_path.resolve()),
        "story_qc_index_sha256": sha256_file(index_path),
        "status": _aggregate_status(stories),
        "stories": stories,
    }
    atomic_write_json(output_path, artifact)
    return artifact


def validate_review(
    job_root: Path, review_path: Path
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]:
    job_root = job_root.expanduser().resolve()
    review_path = review_path.expanduser().resolve()
    errors: list[str] = []
    artifact = load_json(review_path)
    index_path = _index_path(job_root)
    if artifact.get("method") != METHOD:
        errors.append("Story QC review decision method is invalid")
    if artifact.get("story_qc_index_path") != str(index_path.resolve()):
        errors.append("Story QC review decision points to another QC Index")
    if not index_path.is_file():
        return artifact, {}, errors + ["Story QC Index is missing"]
    current_index_hash = sha256_file(index_path)
    if artifact.get("story_qc_index_sha256") != current_index_hash:
        errors.append("Story QC review decision uses a stale QC Index")
    try:
        reports = _report_entries(job_root)
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        return artifact, {}, errors + [str(exc)]
    entries = {
        item.get("story_id"): item
        for item in artifact.get("stories", [])
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }
    if set(entries) != set(reports):
        errors.append("Story QC review decision must cover every QC report exactly")
    for story_id, report in reports.items():
        item = entries.get(story_id)
        if item is None:
            continue
        for field in ("title", "production_slot", "status", "path", "report_sha256"):
            if item.get(field) != report.get(field):
                errors.append(f"{story_id}: review decision {field} is stale")
        decision = item.get("decision")
        note = item.get("note")
        expected = NOT_REQUIRED if report["status"] == "approved" else None
        if expected == NOT_REQUIRED:
            if decision != NOT_REQUIRED:
                errors.append(f"{story_id}: approved QC must be not_required")
        elif decision not in {PENDING, ACCEPTED, REJECTED}:
            errors.append(f"{story_id}: invalid human QC decision")
        if decision in {ACCEPTED, REJECTED} and (
            not isinstance(note, str) or not note.strip()
        ):
            errors.append(f"{story_id}: human QC decision requires a note")
    if artifact.get("status") != _aggregate_status(list(entries.values())):
        errors.append("Story QC review decision aggregate status is stale")
    return artifact, entries, errors


def decide(
    job_root: Path,
    review_path: Path,
    *,
    story_ids: set[str],
    all_nonapproved: bool,
    decision: str,
    note: str,
) -> dict[str, Any]:
    artifact, entries, errors = validate_review(job_root, review_path)
    if errors:
        raise ValueError("; ".join(errors))
    unknown = story_ids - set(entries)
    if unknown:
        raise ValueError(f"unknown Story IDs: {sorted(unknown)}")
    targets = {
        story_id
        for story_id, item in entries.items()
        if item.get("status") != "approved"
        and (all_nonapproved or story_id in story_ids)
    }
    if not targets:
        raise ValueError("no non-approved Story QC report matches the decision target")
    for item in artifact["stories"]:
        if item.get("story_id") in targets:
            item["decision"] = decision
            item["note"] = note.strip()
            item["decided_at"] = utc_now()
    artifact["updated_at"] = utc_now()
    artifact["status"] = _aggregate_status(artifact["stories"])
    atomic_write_json(review_path, artifact)
    return artifact


def load_validated_review(
    job_root: Path,
) -> tuple[Path | None, dict[str, dict[str, Any]], list[str]]:
    path = job_root / "story-qc-review-decision.json"
    if not path.is_file():
        return None, {}, []
    _, entries, errors = validate_review(job_root, path)
    return path, entries, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("job_root", type=Path)
    init.add_argument("--output", type=Path)
    decide_parser = subparsers.add_parser("decide")
    decide_parser.add_argument("job_root", type=Path)
    decide_parser.add_argument("--review", type=Path)
    decide_parser.add_argument("--story-id", action="append", default=[])
    decide_parser.add_argument("--all-nonapproved", action="store_true")
    decide_parser.add_argument(
        "--decision", choices=(ACCEPTED, REJECTED), required=True
    )
    decide_parser.add_argument("--note", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("job_root", type=Path)
    status.add_argument("--review", type=Path)
    args = parser.parse_args()
    job_root = args.job_root.expanduser().resolve()
    default_path = job_root / "story-qc-review-decision.json"
    if args.command == "init":
        output = args.output.expanduser().resolve() if args.output else default_path
        artifact = initialize(job_root, output)
        print(f"STORY_QC_REVIEW_DECISION\t{output}")
        print(f"STATUS\t{artifact['status']}")
        return 0
    review_path = (
        args.review.expanduser().resolve() if args.review else default_path
    )
    if args.command == "decide":
        artifact = decide(
            job_root,
            review_path,
            story_ids=set(args.story_id),
            all_nonapproved=args.all_nonapproved,
            decision=args.decision,
            note=args.note,
        )
        print(f"STORY_QC_REVIEW_DECISION\t{review_path}")
        print(f"STATUS\t{artifact['status']}")
        return 0
    artifact, _, errors = validate_review(job_root, review_path)
    print(f"STATUS\t{artifact.get('status', 'invalid')}")
    print(f"ERRORS\t{len(errors)}")
    for error in errors:
        print(f"ERROR\t{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
