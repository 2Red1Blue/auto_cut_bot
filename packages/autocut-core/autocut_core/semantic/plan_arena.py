#!/usr/bin/env python3
"""Deterministic contracts for post-QC Story Plan candidate election."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from autocut_core.io import load_json, sha256_file, stable_id


CANDIDATE_ARENA_METHOD = "risk-separated-story-plan-candidate-arena-v1"
CANDIDATE_QC_METHOD = "rank-round-story-plan-candidates-v3"
CANDIDATE_QC_SCHEMA_VERSION = "1.2"
WINNER_SELECTION_METHOD = "approved-first-story-plan-winner-v1"
QC_PROJECTION_METHOD = "single-candidate-existing-qc-projection-v1"
PLAN_VALIDATION_BLOCKED_SKIP_REASON = "plan_validation_blocked"
AUTO_SAFE_REVIEW_CODES = {
    "local-audio-fade-fallback-source_start",
    "local-audio-fade-fallback-source_end",
}


def plan_candidate_id(
    *, story_id: str, body_partition_id: str, teaser_option_id: str
) -> str:
    return stable_id(
        "plan-candidate",
        {
            "story_id": story_id,
            "body_partition_id": body_partition_id,
            "teaser_option_id": teaser_option_id,
        },
    )


def split_candidate_contracts(
    legal_options: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return one locked legal-option contract per Arena candidate."""

    partitions = legal_options.get("legal_body_partitions", [])
    teaser_by_id = {
        item["option_id"]: item
        for item in legal_options.get("legal_teaser_options", [])
    }
    body_by_id = {
        item["option_id"]: item
        for item in legal_options.get("legal_block_options", [])
    }
    span_by_id = {
        item["span_candidate_id"]: item
        for item in legal_options.get("span_catalog", [])
    }
    result: list[dict[str, Any]] = []
    for fallback_rank, partition in enumerate(partitions, start=1):
        teaser_ids = list(partition.get("compatible_teaser_option_ids", []))
        if teaser_by_id and len(teaser_ids) != 1:
            raise ValueError(
                f"{partition.get('partition_id')}: Arena finalist must have "
                "exactly one compatible Teaser"
            )
        teaser_id = teaser_ids[0] if teaser_ids else ""
        candidate_id = partition.get("plan_candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            candidate_id = plan_candidate_id(
                story_id=legal_options["story_id"],
                body_partition_id=partition["partition_id"],
                teaser_option_id=teaser_id,
            )
        body_options = []
        for option_id in partition.get("body_option_ids", []):
            option = body_by_id.get(option_id)
            if option is None:
                raise ValueError(
                    f"{partition['partition_id']}: unknown Body Option "
                    f"{option_id}"
                )
            body_options.append(option)
        teaser_options = [teaser_by_id[teaser_id]] if teaser_id else []
        span_ids = {
            span_id
            for option in [*teaser_options, *body_options]
            for span_id in option.get("span_candidate_ids", [])
        }
        missing_span_ids = span_ids - set(span_by_id)
        if missing_span_ids:
            raise ValueError(
                f"{partition['partition_id']}: unknown Span Candidate IDs "
                f"{sorted(missing_span_ids)}"
            )
        result.append(
            {
                **legal_options,
                "legal_teaser_options": teaser_options,
                "legal_block_options": body_options,
                "legal_body_partitions": [partition],
                "span_catalog": [
                    span_by_id[span_id] for span_id in sorted(span_ids)
                ],
                "plan_candidate_id": candidate_id,
                "candidate_rank": int(
                    partition.get("candidate_rank", fallback_rank)
                ),
                "candidate_count": len(partitions),
                "body_partition_id": partition["partition_id"],
                "teaser_option_id": teaser_id,
                "partition_selection_mode": "post_qc_candidate_arena",
            }
        )
    candidate_ids = [item["plan_candidate_id"] for item in result]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("Story Plan Arena contains duplicate candidate identity")
    return result


def validate_candidate_index(index: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if index.get("schema_version") != "1.0":
        errors.append("candidate index schema_version must be 1.0")
    if index.get("method") != CANDIDATE_ARENA_METHOD:
        errors.append("candidate index method is unsupported")
    if index.get("status") not in {
        "ready_for_candidate_qc",
        "partially_ready",
        "blocked",
        "stale",
    }:
        errors.append("candidate index status is unsupported")
    candidates = index.get("candidates")
    if not isinstance(candidates, list):
        return errors + ["candidate index candidates must be an array"]
    seen_ids: set[str] = set()
    ranks_by_story: dict[str, list[int]] = defaultdict(list)
    declared_counts_by_story: dict[str, set[int]] = defaultdict(set)
    for position, item in enumerate(candidates):
        where = f"candidates[{position}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object")
            continue
        candidate_id = item.get("plan_candidate_id")
        story_id = item.get("story_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            errors.append(f"{where}.plan_candidate_id must be non-empty")
        elif candidate_id in seen_ids:
            errors.append(f"duplicate plan_candidate_id: {candidate_id}")
        else:
            seen_ids.add(candidate_id)
        if not isinstance(story_id, str) or not story_id:
            errors.append(f"{where}.story_id must be non-empty")
            continue
        rank = item.get("candidate_rank")
        count = item.get("candidate_count")
        if not isinstance(rank, int) or rank < 1:
            errors.append(f"{where}.candidate_rank must be positive")
        else:
            ranks_by_story[story_id].append(rank)
        if not isinstance(count, int) or count < 1:
            errors.append(f"{where}.candidate_count must be positive")
        else:
            declared_counts_by_story[story_id].add(count)
        if item.get("status") not in {"ready_for_video_qc", "blocked"}:
            errors.append(f"{where}.status is unsupported")
        path_value = item.get("path")
        if not isinstance(path_value, str) or not path_value:
            errors.append(f"{where}.path must be non-empty")
        else:
            path = Path(path_value).expanduser().resolve()
            if not path.is_file():
                errors.append(f"{where}.path is missing")
            elif item.get("plan_sha256") != sha256_file(path):
                errors.append(f"{where}.plan_sha256 is stale")
    for story_id, ranks in ranks_by_story.items():
        expected = list(range(1, len(ranks) + 1))
        if sorted(ranks) != expected:
            errors.append(
                f"{story_id}: candidate ranks must be contiguous {expected}"
            )
        if declared_counts_by_story[story_id] != {len(ranks)}:
            errors.append(
                f"{story_id}: candidate_count must equal {len(ranks)}"
            )
    if index.get("candidate_count") != len(candidates):
        errors.append("candidate_count differs from candidates length")
    if index.get("story_count") != len(ranks_by_story):
        errors.append("story_count differs from Candidate Story identities")
    if index.get("ready_candidate_count") != sum(
        item.get("status") == "ready_for_video_qc"
        for item in candidates
        if isinstance(item, dict)
    ):
        errors.append("ready_candidate_count is inconsistent")
    if index.get("blocked_candidate_count") != sum(
        item.get("status") == "blocked"
        for item in candidates
        if isinstance(item, dict)
    ):
        errors.append("blocked_candidate_count is inconsistent")
    return errors


def validate_qc_projection(
    workspace_root: Path, projection_path: Path
) -> tuple[dict[str, Any] | None, list[str]]:
    """Verify that an isolated QC root projects one already-validated Candidate."""

    errors: list[str] = []
    if not projection_path.is_file():
        return None, [f"Candidate QC projection is missing: {projection_path}"]
    projection = load_json(projection_path)
    if projection.get("schema_version") != "1.0":
        errors.append("Candidate QC projection schema_version must be 1.0")
    if projection.get("method") != QC_PROJECTION_METHOD:
        errors.append("Candidate QC projection method is unsupported")
    candidate_index_path = Path(
        projection.get("candidate_index_path", "")
    ).expanduser().resolve()
    validation_path = Path(
        projection.get("candidate_validation_path", "")
    ).expanduser().resolve()
    if not candidate_index_path.is_file():
        errors.append("Candidate QC projection Candidate Index is missing")
        return None, errors
    if not validation_path.is_file():
        errors.append("Candidate QC projection validation report is missing")
        return None, errors
    if projection.get("candidate_index_sha256") != sha256_file(
        candidate_index_path
    ):
        errors.append("Candidate QC projection Candidate Index is stale")
    if projection.get("candidate_validation_sha256") != sha256_file(
        validation_path
    ):
        errors.append("Candidate QC projection validation report is stale")
    candidate_index = load_json(candidate_index_path)
    errors.extend(validate_candidate_index(candidate_index))
    validation = load_json(validation_path)
    if (
        validation.get("ok") is not True
        or validation.get("status") != "current"
        or validation.get("validation_subject") != "story_plan_candidates"
        or validation.get("plan_generation_sha256")
        != candidate_index.get("plan_generation_sha256")
    ):
        errors.append("Candidate QC projection has no current Plan validation")
    candidate_id = projection.get("plan_candidate_id")
    matches = [
        item
        for item in candidate_index.get("candidates", [])
        if item.get("plan_candidate_id") == candidate_id
    ]
    if len(matches) != 1:
        errors.append("Candidate QC projection identity is absent or ambiguous")
        return None, errors
    candidate = matches[0]
    if candidate.get("status") != "ready_for_video_qc":
        errors.append("Candidate QC projection Plan is not ready for video QC")
    if projection.get("story_id") != candidate.get("story_id"):
        errors.append("Candidate QC projection story_id is inconsistent")
    formal_index_path = workspace_root / "story-plans" / "index.json"
    if not formal_index_path.is_file():
        errors.append("Candidate QC projected Story Plan Index is missing")
        return candidate, errors
    formal_index = load_json(formal_index_path)
    projected_plans = formal_index.get("plans", [])
    if (
        formal_index.get("status") != "ready_for_video_qc"
        or formal_index.get("plan_generation_sha256")
        != candidate_index.get("plan_generation_sha256")
        or formal_index.get("story_approval_sha256")
        != candidate_index.get("story_approval_sha256")
        or formal_index.get("span_candidate_index_sha256")
        != candidate_index.get("span_candidate_index_sha256")
        or not isinstance(projected_plans, list)
        or len(projected_plans) != 1
        or projected_plans[0] != candidate
    ):
        errors.append(
            "Candidate QC projected Story Plan Index differs from Candidate Index"
        )
    return candidate, errors


def validate_candidate_qc_index(
    index: dict[str, Any], candidate_index: dict[str, Any]
) -> list[str]:
    """Validate complete, independently checked QC coverage of the Arena."""

    errors = validate_candidate_index(candidate_index)
    if index.get("schema_version") != CANDIDATE_QC_SCHEMA_VERSION:
        errors.append(
            "Candidate QC Index schema_version must be "
            f"{CANDIDATE_QC_SCHEMA_VERSION}"
        )
    if index.get("method") != CANDIDATE_QC_METHOD:
        errors.append("Candidate QC Index method is unsupported")
    if index.get("status") not in {"partial", "complete"}:
        errors.append("Candidate QC Index status must be partial or complete")
    reports = index.get("reports")
    if not isinstance(reports, list):
        return errors + ["Candidate QC Index reports must be an array"]
    candidates = {
        item["plan_candidate_id"]: item
        for item in candidate_index.get("candidates", [])
        if isinstance(item, dict)
        and isinstance(item.get("plan_candidate_id"), str)
    }
    reports_by_id: dict[str, dict[str, Any]] = {}
    for position, entry in enumerate(reports):
        where = f"reports[{position}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be an object")
            continue
        candidate_id = entry.get("plan_candidate_id")
        if not isinstance(candidate_id, str) or candidate_id not in candidates:
            errors.append(f"{where}.plan_candidate_id is unknown")
            continue
        if candidate_id in reports_by_id:
            errors.append(f"duplicate Candidate QC report: {candidate_id}")
            continue
        reports_by_id[candidate_id] = entry
        candidate = candidates[candidate_id]
        if (
            entry.get("story_id") != candidate.get("story_id")
            or entry.get("candidate_rank") != candidate.get("candidate_rank")
        ):
            errors.append(f"{where} Candidate identity is inconsistent")
        if candidate.get("status") != "ready_for_video_qc":
            errors.append(
                f"{where} reports a Candidate that is not Plan-valid"
            )
        if entry.get("status") not in {"approved", "review", "blocked"}:
            errors.append(f"{where}.status is unsupported")
        for path_field, hash_field in (
            ("path", "report_sha256"),
            ("effective_plan_path", "effective_plan_sha256"),
            ("workspace_validation_path", "workspace_validation_sha256"),
        ):
            value = entry.get(path_field)
            if not isinstance(value, str):
                errors.append(f"{where}.{path_field} must be a path")
                continue
            path = Path(value).expanduser().resolve()
            if not path.is_file():
                errors.append(f"{where}.{path_field} is missing")
            elif entry.get(hash_field) != sha256_file(path):
                errors.append(f"{where}.{hash_field} is stale")
        report_path_value = entry.get("path")
        validation_path_value = entry.get("workspace_validation_path")
        if isinstance(report_path_value, str) and Path(
            report_path_value
        ).expanduser().resolve().is_file():
            report = load_json(Path(report_path_value).expanduser().resolve())
            if (
                report.get("story_id") != entry.get("story_id")
                or report.get("status") != entry.get("status")
                or report.get("findings", []) != entry.get("findings", [])
                or report.get("input_fingerprints", {}).get(
                    "story_plan_sha256"
                )
                != entry.get("effective_plan_sha256")
            ):
                errors.append(f"{where} report identity/fingerprints are stale")
        if isinstance(validation_path_value, str) and Path(
            validation_path_value
        ).expanduser().resolve().is_file():
            validation = load_json(
                Path(validation_path_value).expanduser().resolve()
            )
            if validation.get("ok") is not True:
                errors.append(f"{where} workspace Story QC validation failed")
    skipped = index.get("skipped_candidates", [])
    if not isinstance(skipped, list):
        return errors + ["Candidate QC skipped_candidates must be an array"]
    skipped_by_id: dict[str, dict[str, Any]] = {}
    for position, entry in enumerate(skipped):
        where = f"skipped_candidates[{position}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be an object")
            continue
        candidate_id = entry.get("plan_candidate_id")
        candidate = candidates.get(candidate_id)
        if candidate is None:
            errors.append(f"{where}.plan_candidate_id is unknown")
            continue
        if candidate_id in skipped_by_id or candidate_id in reports_by_id:
            errors.append(f"duplicate/overlapping Candidate outcome: {candidate_id}")
            continue
        skipped_by_id[candidate_id] = entry
        if (
            entry.get("story_id") != candidate.get("story_id")
            or entry.get("candidate_rank") != candidate.get("candidate_rank")
        ):
            errors.append(f"{where} Candidate identity is inconsistent")
        reason = entry.get("reason")
        if reason == "earlier_candidate_approved":
            winner_id = entry.get("approved_candidate_id")
            winner = reports_by_id.get(winner_id)
            winner_candidate = candidates.get(winner_id)
            if (
                winner is None
                or winner.get("status") != "approved"
                or winner_candidate is None
                or winner_candidate.get("story_id")
                != candidate.get("story_id")
                or int(winner_candidate.get("candidate_rank", 10**9))
                >= int(candidate.get("candidate_rank", 0))
            ):
                errors.append(
                    f"{where} is not justified by an earlier approved "
                    "Candidate"
                )
        elif reason == PLAN_VALIDATION_BLOCKED_SKIP_REASON:
            plan_path = Path(candidate.get("path", "")).expanduser().resolve()
            if candidate.get("status") != "blocked":
                errors.append(
                    f"{where} can quarantine only a blocked Candidate Plan"
                )
            elif not plan_path.is_file():
                errors.append(f"{where} blocked Candidate Plan is missing")
            else:
                plan = load_json(plan_path)
                if (
                    entry.get("plan_sha256") != candidate.get("plan_sha256")
                    or entry.get("blocked_reasons")
                    != plan.get("blocked_reasons")
                    or entry.get("repair_routes") != plan.get("repair_routes")
                ):
                    errors.append(
                        f"{where} blocked Candidate audit is stale"
                    )
                blocked_reasons = plan.get("blocked_reasons")
                repair_routes = plan.get("repair_routes")
                if (
                    not isinstance(blocked_reasons, list)
                    or not blocked_reasons
                    or not all(
                        isinstance(item, str) and item
                        for item in blocked_reasons
                    )
                ):
                    errors.append(
                        f"{where} blocked Candidate reasons are not typed"
                    )
                if (
                    not isinstance(repair_routes, list)
                    or not repair_routes
                    or not all(isinstance(item, dict) for item in repair_routes)
                ):
                    errors.append(
                        f"{where} blocked Candidate repair routes are not typed"
                    )
        else:
            errors.append(f"{where}.reason is unsupported")
    covered_ids = set(reports_by_id) | set(skipped_by_id)
    if index.get("status") == "complete" and covered_ids != set(candidates):
        errors.append(
            "complete Candidate QC outcomes must cover the Arena exactly"
        )
    if index.get("status") == "partial" and not covered_ids < set(candidates):
        errors.append("partial Candidate QC must leave unresolved Candidates")
    if index.get("report_count") != len(reports):
        errors.append("Candidate QC report_count is inconsistent")
    if index.get("skipped_candidate_count") != len(skipped):
        errors.append("Candidate QC skipped_candidate_count is inconsistent")
    for status in ("approved", "review", "blocked"):
        if index.get(f"{status}_count") != sum(
            entry.get("status") == status for entry in reports
        ):
            errors.append(f"Candidate QC {status}_count is inconsistent")
    return errors


def is_auto_safe_review(report: dict[str, Any]) -> bool:
    if report.get("status") != "review":
        return False
    non_info = [
        item
        for item in report.get("findings", [])
        if isinstance(item, dict) and item.get("severity") != "info"
    ]
    return bool(non_info) and all(
        item.get("code") in AUTO_SAFE_REVIEW_CODES for item in non_info
    )


def elect_story_winners(
    candidate_index: dict[str, Any], candidate_qc_index: dict[str, Any]
) -> dict[str, Any]:
    """Elect one Plan per Story without weakening the QC quality contract."""

    errors = validate_candidate_index(candidate_index)
    if errors:
        raise ValueError("invalid candidate index: " + "; ".join(errors[:30]))
    candidates_by_id = {
        item["plan_candidate_id"]: item
        for item in candidate_index["candidates"]
    }
    reports_by_candidate = {
        item["plan_candidate_id"]: item
        for item in candidate_qc_index.get("reports", [])
        if isinstance(item, dict)
        and isinstance(item.get("plan_candidate_id"), str)
    }
    skipped_ids = {
        item.get("plan_candidate_id")
        for item in candidate_qc_index.get("skipped_candidates", [])
        if isinstance(item, dict)
    }
    if set(reports_by_candidate) | skipped_ids != set(candidates_by_id):
        raise ValueError(
            "candidate QC reports/skips must cover the Candidate Arena exactly"
        )
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = (
        defaultdict(list)
    )
    for candidate_id, report in reports_by_candidate.items():
        candidate = candidates_by_id[candidate_id]
        grouped[candidate["story_id"]].append(
            (candidate, report)
        )
    winners: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    skipped_by_candidate = {
        item.get("plan_candidate_id"): item
        for item in candidate_qc_index.get("skipped_candidates", [])
        if isinstance(item, dict)
    }
    candidates_by_story: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates_by_id.values():
        candidates_by_story[candidate["story_id"]].append(candidate)
    for story_id, story_candidates in sorted(candidates_by_story.items()):
        rows = grouped.get(story_id, [])
        if not rows:
            rejected.append(
                {
                    "story_id": story_id,
                    "reason": "no_plan_valid_candidate",
                    "candidate_statuses": [
                        {
                            "plan_candidate_id": candidate[
                                "plan_candidate_id"
                            ],
                            "status": candidate.get("status"),
                            "skip_reason": skipped_by_candidate.get(
                                candidate["plan_candidate_id"], {}
                            ).get("reason", ""),
                        }
                        for candidate in sorted(
                            story_candidates,
                            key=lambda item: item["candidate_rank"],
                        )
                    ],
                }
            )
            continue
        approved = [row for row in rows if row[1].get("status") == "approved"]
        auto_safe = [row for row in rows if is_auto_safe_review(row[1])]
        eligible = approved or auto_safe
        if not eligible:
            rejected.append(
                {
                    "story_id": story_id,
                    "reason": "no_approved_or_auto_safe_review_candidate",
                    "candidate_statuses": [
                        {
                            "plan_candidate_id": candidate["plan_candidate_id"],
                            "status": report.get("status"),
                        }
                        for candidate, report in sorted(
                            rows, key=lambda row: row[0]["candidate_rank"]
                        )
                    ],
                }
            )
            continue
        candidate, report = min(
            eligible, key=lambda row: row[0]["candidate_rank"]
        )
        winners.append(
            {
                "story_id": story_id,
                "plan_candidate_id": candidate["plan_candidate_id"],
                "candidate_rank": candidate["candidate_rank"],
                "selection_class": (
                    "approved" if approved else "auto_safe_review"
                ),
                "plan_path": report.get("effective_plan_path", candidate["path"]),
                "plan_sha256": report.get(
                    "effective_plan_sha256", candidate["plan_sha256"]
                ),
                "qc_report_path": report["path"],
                "qc_report_sha256": report["report_sha256"],
            }
        )
    return {
        "schema_version": "1.0",
        "method": WINNER_SELECTION_METHOD,
        "status": (
            "ready" if winners and not rejected else "partial" if winners else "blocked"
        ),
        "candidate_arena_sha256": candidate_qc_index.get(
            "candidate_arena_sha256"
        ),
        "candidate_qc_index_sha256": candidate_qc_index.get("index_sha256"),
        "winner_count": len(winners),
        "rejected_story_count": len(rejected),
        "winners": winners,
        "rejected_stories": rejected,
    }
