"""Validate Span Candidate bundles against current Story Evidence Packets."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from autocut_core.libs.span_compiler import (
    candidate_must_show_support,
    is_full_source_like,
    semantic_density_ratio,
)
from autocut_core.io import atomic_write_json, load_json, sha256_file, stable_id
from autocut_core.schema.compat import validate_task_response


def _validate_evidence():
    from autocut_core.contracts.evidence_validation import validate as _v
    return _v


def indexed(
    records: list[dict[str, Any]], *, field: str, where: str, errors: list[str]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(records):
        value = item.get(field)
        if not isinstance(value, str) or not value:
            errors.append(f"{where}[{index}].{field} must be non-empty")
        elif value in result:
            errors.append(f"{where} contains duplicate {field}: {value}")
        else:
            result[value] = item
    return result


def check_subset(
    values: Any, known: set[str], where: str, errors: list[str]
) -> set[str]:
    if not isinstance(values, list):
        errors.append(f"{where} must be an array")
        return set()
    selected = {item for item in values if isinstance(item, str) and item}
    unknown = sorted(selected - known)
    if unknown:
        errors.append(f"{where} contains unknown IDs: {unknown}")
    return selected


def expected_coverage_status(
    candidate_ids: set[str],
    *,
    candidates: dict[str, dict[str, Any]],
) -> str:
    if not candidate_ids:
        return "missing"
    if all(
        candidates[item_id].get("boundary_status") == "needs_video_review"
        for item_id in candidate_ids
    ):
        return "needs_video_review"
    return "covered"


def validate(job_root: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    evidence_report = _validate_evidence()(job_root)
    if not evidence_report["ok"]:
        errors.extend(
            f"story_evidence: {item}" for item in evidence_report["errors"]
        )
        return {"ok": False, "errors": errors, "warnings": warnings}
    index_path = job_root / "span-candidates" / "index.json"
    evidence_index_path = job_root / "story-evidence" / "index.json"
    if not index_path.is_file():
        return {
            "ok": False,
            "errors": [f"missing Span Candidate Index: {index_path}"],
            "warnings": warnings,
        }
    index = load_json(index_path)
    errors.extend(
        f"span_candidates.index: {item}"
        for item in validate_task_response("span_candidate_index", index)
    )
    evidence_index = load_json(evidence_index_path)
    evidence_index_sha256 = sha256_file(evidence_index_path)
    if index.get("story_evidence_index_sha256") != evidence_index_sha256:
        errors.append("Span Candidate Index Story Evidence SHA-256 is stale")
    evidence_entries = {
        item["story_id"]: item
        for item in evidence_index.get("packets", [])
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }
    bundle_entries = indexed(
        index.get("bundles", []),
        field="story_id",
        where="span_candidates.index.bundles",
        errors=errors,
    )
    eligible_evidence_ids = {
        story_id
        for story_id, entry in evidence_entries.items()
        if entry.get("status") != "incomplete"
    }
    if set(bundle_entries) != eligible_evidence_ids:
        errors.append(
            "Span Candidate bundles differ from eligible Story Evidence: "
            f"missing={sorted(eligible_evidence_ids - set(bundle_entries))}, "
            f"extra={sorted(set(bundle_entries) - eligible_evidence_ids)}"
        )
    global_identities: dict[str, tuple[str, float, float]] = {}
    total_references = 0
    observed_bundle_statuses: list[str] = []
    for story_id, entry in bundle_entries.items():
        path_value = entry.get("path")
        if not isinstance(path_value, str):
            errors.append(f"{story_id}: bundle path must be a string")
            continue
        bundle_path = Path(path_value).expanduser().resolve()
        if not bundle_path.is_file():
            errors.append(f"{story_id}: missing bundle {bundle_path}")
            continue
        if entry.get("bundle_sha256") != sha256_file(bundle_path):
            errors.append(f"{story_id}: bundle SHA-256 is stale")
        bundle = load_json(bundle_path)
        errors.extend(
            f"{story_id}.bundle: {item}"
            for item in validate_task_response(
                "span_candidate_bundle", bundle
            )
        )
        observed_bundle_statuses.append(str(bundle.get("status")))
        for field in ("story_id", "title", "production_slot", "status"):
            if bundle.get(field) != entry.get(field):
                errors.append(f"{story_id}: bundle {field} differs from index")
        if entry.get("candidate_count") != len(bundle.get("candidates", [])):
            errors.append(f"{story_id}: candidate_count is inconsistent")
        total_references += len(bundle.get("candidates", []))
        evidence_entry = evidence_entries.get(story_id)
        if not isinstance(evidence_entry, dict):
            continue
        packet_path = Path(evidence_entry["path"]).expanduser().resolve()
        if not packet_path.is_file():
            errors.append(f"{story_id}: Story Evidence Packet is missing")
            continue
        packet_sha256 = sha256_file(packet_path)
        packet = load_json(packet_path)
        expected_fingerprints = {
            "story_evidence_index_sha256": evidence_index_sha256,
            "story_evidence_packet_sha256": packet_sha256,
            "story_script_sha256": packet["approval_binding"][
                "story_script_sha256"
            ],
        }
        if bundle.get("input_fingerprints") != expected_fingerprints:
            errors.append(f"{story_id}: bundle input fingerprints are stale")
        if entry.get("story_evidence_packet_sha256") != packet_sha256:
            errors.append(f"{story_id}: index Evidence Packet SHA-256 is stale")
        sources = {
            item["id"]: item
            for item in packet["evidence_catalog"]["sources"]
        }
        events = {
            item["id"]: item
            for item in packet["evidence_catalog"]["events"]
        }
        evidence_candidates = {
            item["id"]: item
            for item in packet["evidence_catalog"]["candidates"]
        }
        evidence_thread_beat_ids = {
            item["id"]
            for item in packet["evidence_catalog"].get("thread_beats", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        window_ids = {
            item["window_id"]
            for item in packet["evidence_catalog"]["windows"]
        }
        packet_beats = {
            item["beat_id"]: item for item in packet["beat_evidence"]
        }
        must_show_ids = {
            item["must_show_id"]
            for beat in packet["beat_evidence"]
            for item in beat["must_show_evidence"]
        }
        candidates = indexed(
            bundle.get("candidates", []),
            field="span_candidate_id",
            where=f"{story_id}.candidates",
            errors=errors,
        )
        for candidate_id, candidate in candidates.items():
            where = f"{story_id}.candidates[{candidate_id}]"
            source_id = candidate.get("source_id")
            source = sources.get(source_id)
            if source is None:
                errors.append(f"{where} has unknown source_id")
                continue
            start, end = candidate.get("start"), candidate.get("end")
            if (
                not isinstance(start, (int, float))
                or isinstance(start, bool)
                or not isinstance(end, (int, float))
                or isinstance(end, bool)
                or end <= start
            ):
                errors.append(f"{where} has invalid source range")
                continue
            expected_id = stable_id(
                "span",
                {
                    "source_id": source_id,
                    "start": float(start),
                    "end": float(end),
                },
            )
            if candidate_id != expected_id:
                errors.append(f"{where} does not have a stable range identity")
            identity = (source_id, float(start), float(end))
            previous = global_identities.get(candidate_id)
            if previous is not None and previous != identity:
                errors.append(
                    f"{candidate_id}: same ID has different ranges across Stories"
                )
            global_identities[candidate_id] = identity
            if candidate.get("episode") != source.get("episode"):
                errors.append(f"{where}.episode differs from Source")
            if float(end) > float(source["duration_seconds"]) + 0.001:
                errors.append(f"{where} exceeds Source duration")
            if candidate.get("duration_seconds") != round(
                float(end) - float(start), 3
            ):
                errors.append(f"{where}.duration_seconds is inconsistent")
            source_duration = round(
                float(source["duration_seconds"]), 3
            )
            expected_ratio = min(
                1.0,
                round((float(end) - float(start)) / source_duration, 3)
                if source_duration > 0
                else 0.0,
            )
            if candidate.get("source_duration_seconds") != source_duration:
                errors.append(
                    f"{where}.source_duration_seconds is inconsistent"
                )
            if candidate.get("source_coverage_ratio") != expected_ratio:
                errors.append(
                    f"{where}.source_coverage_ratio is inconsistent"
                )
            expected_semantic_density = semantic_density_ratio(
                candidate.get("semantic_segment_refs", []),
                float(start),
                float(end),
            )
            expected_full_source_like = is_full_source_like(
                expected_ratio,
                source_duration,
                expected_semantic_density,
            )
            if (
                candidate.get("full_source_like")
                != expected_full_source_like
            ):
                errors.append(f"{where}.full_source_like is inconsistent")
            check_subset(
                candidate.get("supports_beat_ids"),
                set(packet_beats),
                f"{where}.supports_beat_ids",
                errors,
            )
            provenance_tiers = set(candidate.get("provenance_tiers", []))
            functional_support = {
                *candidate.get("supports_beat_ids", []),
                *candidate.get("supports_thread_beat_ids", []),
                *candidate.get("supports_must_show_ids", []),
            }
            if provenance_tiers == {"context"} and functional_support:
                errors.append(
                    f"{where}: context-only Span cannot claim functional "
                    "Beat/must-show/Thread Beat support"
                )
            if not provenance_tiers.intersection({"direct", "candidate"}) and (
                candidate.get("supports_beat_ids")
            ):
                errors.append(
                    f"{where}: functional Beat support lacks direct/candidate "
                    "provenance"
                )
            check_subset(
                candidate.get("supports_thread_beat_ids"),
                evidence_thread_beat_ids,
                f"{where}.supports_thread_beat_ids",
                errors,
            )
            check_subset(
                candidate.get("supports_must_show_ids"),
                must_show_ids,
                f"{where}.supports_must_show_ids",
                errors,
            )
            expected_must_show_support = set(
                candidate_must_show_support(candidate, packet_beats)
            )
            if set(
                candidate.get("supports_must_show_ids", [])
            ) != expected_must_show_support:
                errors.append(
                    f"{where}.supports_must_show_ids is not the "
                    "deterministic intersection with direct must-show "
                    "Event evidence"
                )
            check_subset(
                candidate.get("event_ids"),
                set(events),
                f"{where}.event_ids",
                errors,
            )
            check_subset(
                candidate.get("candidate_ids"),
                set(evidence_candidates),
                f"{where}.candidate_ids",
                errors,
            )
            for anchor_index, anchor in enumerate(
                candidate.get("anchor_refs", [])
            ):
                anchor_where = f"{where}.anchor_refs[{anchor_index}]"
                if anchor.get("origin") == "event":
                    if anchor.get("origin_id") not in events:
                        errors.append(f"{anchor_where} has unknown Event")
                elif anchor.get("origin") == "candidate":
                    if anchor.get("origin_id") not in evidence_candidates:
                        errors.append(f"{anchor_where} has unknown Candidate")
                if (
                    not isinstance(anchor.get("start"), (int, float))
                    or not isinstance(anchor.get("end"), (int, float))
                    or anchor["end"] <= anchor["start"]
                ):
                    errors.append(f"{anchor_where} has invalid range")
                check_subset(
                    anchor.get("evidence_window_ids"),
                    window_ids,
                    f"{anchor_where}.evidence_window_ids",
                    errors,
                )
            for segment_index, segment in enumerate(
                candidate.get("semantic_segment_refs", [])
            ):
                segment_where = (
                    f"{where}.semantic_segment_refs[{segment_index}]"
                )
                if segment.get("source_id") != source_id:
                    errors.append(f"{segment_where} has a different source_id")
                check_subset(
                    segment.get("window_ids"),
                    window_ids,
                    f"{segment_where}.window_ids",
                    errors,
                )
            if (
                not bundle["compiler_policy"]["emits_verified_boundaries"]
                and candidate.get("boundary_status") == "verified"
            ):
                errors.append(f"{where} illegally claims verified boundaries")
            reasons = candidate.get("boundary_evidence", {}).get(
                "review_reasons", []
            )
            if (
                candidate.get("boundary_status") == "needs_video_review"
                and not reasons
            ):
                errors.append(
                    f"{where} needs_video_review without a review reason"
                )
            if (
                candidate.get("boundary_status") == "proposed"
                and reasons
            ):
                errors.append(f"{where} proposed boundary has review reasons")
        coverage = indexed(
            bundle.get("beat_coverage", []),
            field="beat_id",
            where=f"{story_id}.beat_coverage",
            errors=errors,
        )
        if set(coverage) != set(packet_beats):
            errors.append(f"{story_id}: Beat coverage differs from Evidence Packet")
        missing_must_have = False
        has_review = False
        for beat_id, item in coverage.items():
            selected_ids = check_subset(
                item.get("candidate_ids"),
                set(candidates),
                f"{story_id}.beat_coverage[{beat_id}].candidate_ids",
                errors,
            )
            expected_status = expected_coverage_status(
                selected_ids, candidates=candidates
            )
            if item.get("status") != expected_status:
                errors.append(
                    f"{story_id}.beat_coverage[{beat_id}] status is inconsistent"
                )
            if expected_status == "missing" and item.get("must_have"):
                missing_must_have = True
            if expected_status == "needs_video_review":
                has_review = True
        expected_bundle_status = (
            "incomplete"
            if missing_must_have
            else "needs_video_review"
            if has_review
            else "ready"
        )
        if bundle.get("status") != expected_bundle_status:
            errors.append(f"{story_id}: bundle status is inconsistent")
    if index.get("story_count") != len(bundle_entries):
        errors.append("Span Candidate Index story_count is inconsistent")
    if index.get("candidate_reference_count") != total_references:
        errors.append(
            "Span Candidate Index candidate_reference_count is inconsistent"
        )
    if index.get("unique_span_candidate_count") != len(global_identities):
        errors.append(
            "Span Candidate Index unique_span_candidate_count is inconsistent"
        )
    expected_index_status = (
        "partially_ready"
        if set(evidence_entries) - eligible_evidence_ids
        else "incomplete"
        if "incomplete" in observed_bundle_statuses
        else "needs_video_review"
        if "needs_video_review" in observed_bundle_statuses
        else "ready"
    )
    if index.get("status") != expected_index_status:
        errors.append("Span Candidate Index status is inconsistent")
    if index.get("status") == "partially_ready":
        warnings.append(
            "Span Candidates cover only evidence-ready Stories; incomplete "
            "Stories remain selected for repair"
        )
    elif index.get("status") == "needs_video_review":
        warnings.append(
            "Span Candidates are compiled but require targeted video review"
        )
    if index.get("status") == "incomplete":
        warnings.append("Span Candidate coverage is incomplete")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_root", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    job_root = args.job_root.expanduser().resolve()
    report = validate(job_root)
    report_path = (
        args.report.expanduser().resolve()
        if args.report
        else job_root / "span-candidate-validation.json"
    )
    atomic_write_json(report_path, report)
    print(f"STATUS\t{'OK' if report['ok'] else 'FAILED'}")
    print(f"ERRORS\t{len(report['errors'])}")
    print(f"WARNINGS\t{len(report['warnings'])}")
    print(f"REPORT\t{report_path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())