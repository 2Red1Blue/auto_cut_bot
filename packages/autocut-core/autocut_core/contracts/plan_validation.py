"""Validate Story Plans against approved Scripts and current Span Bundles."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from autocut_core.libs.editorial_plan import (
    current_plan_generation,
    expand_option_selection,
    is_obvious_backward_episode_jump,
    materialize_plan,
    validate_option_selection,
)
from autocut_core.io import atomic_write_json, load_json, sha256_file
from autocut_core.schema.compat import validate_schema, validate_task_response
from autocut_core.contracts.span_validation import validate as validate_span_candidates


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


def validate_editorial_plan_contract(
    plan: dict[str, Any], script: dict[str, Any]
) -> list[str]:
    """Enforce the story contract after local Option materialization.

    This is intentionally deterministic and conservative: it does not infer
    new plot facts, but it does prevent a legal-looking Option selection from
    extending beyond the approved story arc or moving the ending Hook off the
    primary thread.
    """
    errors: list[str] = []
    contract = script.get("editorial_contract", {})
    if not isinstance(contract, dict) or not contract:
        return errors  # scripts are checked by the existing contracts
    script_profile = script.get("genre_profile")
    if script_profile and plan.get("genre_profile") != script_profile:
        errors.append("genre_profile: Story Plan does not match approved Story Script")
    if set(plan.get("golden_case_ids", [])) != set(
        script.get("golden_case_ids", [])
    ):
        errors.append("golden_case_ids: Story Plan does not match approved Story Script")
    if script.get("edit_mode") and plan.get("edit_mode") != script.get("edit_mode"):
        errors.append("edit_mode: Story Plan does not match approved Story Script")
    primary = contract.get("primary_story_thread_id")
    if not isinstance(primary, str) or not primary:
        errors.append("editorial_contract.primary_story_thread_id is missing")
        return errors
    selected_thread_beats = set(script.get("selected_thread_beat_ids", []))
    covered_thread_beats = set(
        plan.get("coverage", {}).get("covered_thread_beat_ids", [])
    )
    unexpected = sorted(covered_thread_beats - selected_thread_beats)
    if unexpected and contract.get("future_arc_injection_forbidden"):
        errors.append(
            "future_arc_injection: Plan 使用了批准 Story Script 之外的 Thread Beat "
            + repr(unexpected)
        )
    beats = {
        item.get("id"): item
        for item in script.get("beats", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    primary_beat_ids = {
        beat_id
        for beat_id, beat in beats.items()
        if primary in beat.get("retrieval_requirements", {}).get(
            "story_thread_ids", []
        )
    }
    blocks = plan.get("blocks", [])
    opening_contract = script.get("teaser_contract", {})
    opening_strategy = (
        opening_contract.get("opening_strategy", "future_preview_reprise")
        if isinstance(opening_contract, dict)
        else "future_preview_reprise"
    )
    if blocks:
        reprise_block_indexes = [
            index
            for index, block in enumerate(blocks)
            if any(
                isinstance(clip, dict)
                and clip.get("reuse_mode") == "teaser_reprise"
                for clip in block.get("clips", [])
            )
        ]
        if reprise_block_indexes and opening_strategy in {
            "causal_explanatory_no_reprise",
            "causal_explanatory_opening",
            "original_chronological_opening",
        }:
            errors.append(
                "opening_strategy: causal_explanatory_no_reprise 不允许正文重放开场高光。"
            )
        if reprise_block_indexes and opening_strategy == "causal_explanatory_delayed_reprise":
            explanation_ids = set(
                item
                for item in opening_contract.get("explanation_beat_ids", [])
                if isinstance(item, str)
            )
            explanation_blocks = [
                index
                for index, block in enumerate(blocks)
                if explanation_ids & set(block.get("beat_ids", []))
            ]
            first_reprise = min(reprise_block_indexes)
            last_explanation = max(explanation_blocks, default=0)
            progression_blocks = sum(
                any(
                    beats.get(beat_id, {}).get("role")
                    in {"escalation", "turn_or_reveal", "payoff", "end_hook"}
                    for beat_id in block.get("beat_ids", [])
                )
                for block in blocks[last_explanation + 1:first_reprise]
            )
            minimum_progression = int(
                opening_contract.get(
                    "reprise_delay_minimum_progression_beats", 1
                )
            )
            if progression_blocks < minimum_progression:
                errors.append(
                    "opening_strategy: delayed_reprise 在完成前因解释和至少一次新推进前重放了开场高光。"
                )
    if blocks and not primary_beat_ids.intersection(blocks[0].get("beat_ids", [])):
        errors.append(
            "primary_thread_not_in_opening: 成片开头没有承接声明的主线。"
        )
    continuity = contract.get("continuity_contract", {})
    if isinstance(continuity, dict) and continuity:
        expected_edge_pairs = list(zip(blocks, blocks[1:]))
        edge_pairs = {
            (edge.get("from_block_id"), edge.get("to_block_id"))
            for edge in plan.get("sequence_edges", [])
            if isinstance(edge, dict)
        }
        for previous, current in expected_edge_pairs:
            pair = (previous.get("id"), current.get("id"))
            if pair not in edge_pairs:
                errors.append(
                    f"continuity_bridge_missing: {pair[0]}→{pair[1]} 缺少 sequence edge，不能证明跨段承接。"
                )
        if any(
            edge.get("temporal_relation") == "preview_future"
            for edge in plan.get("sequence_edges", [])
            if isinstance(edge, dict)
        ):
            errors.append(
                "future_arc_injection: Story Plan 不得用 preview_future 作为正文跨段承接。"
            )
    hook_ids = {
        beat_id
        for beat_id, beat in beats.items()
        if beat.get("role") == "end_hook"
    }
    if hook_ids and blocks and not hook_ids.intersection(blocks[-1].get("beat_ids", [])):
        errors.append(
            "ending_hook_not_last: End Hook 没有位于最后一个 Block。"
        )
    if hook_ids and not hook_ids.intersection(primary_beat_ids):
        errors.append(
            "ending_hook_not_primary_thread: End Hook 没有回到 primary_story_thread_id。"
        )
    ending_policy = contract.get("ending_policy", {})
    ending_intent = script.get("ending_hook_intent", {})
    if (
        isinstance(ending_policy, dict)
        and ending_policy
        and not hook_ids
        and isinstance(ending_intent, dict)
        and ending_intent.get("may_be_empty") is True
        and blocks
        and blocks[-1].get("clips")
    ):
        final_clip = blocks[-1]["clips"][-1]
        try:
            final_end = float(final_clip["source_end"])
            source_tail = float(final_clip["source_duration_seconds"])
        except (KeyError, TypeError, ValueError):
            final_end = source_tail = -1.0
        if final_end < 0 or abs(final_end - source_tail) > 0.05:
            errors.append(
                "ending_fallback_not_at_episode_tail: 无合法 Hook 时，最后一个 Story Clip 必须落到当前故事线的集尾。"
            )
    extension = contract.get("duration_extension_policy", {})
    if isinstance(extension, dict) and extension:
        expected_extension = {
            "trigger": "below_minimum_duration",
            "minimum_seconds": 300,
            "after_threshold": "continue_to_threshold_episode_tail",
            "same_primary_thread_only": True,
            "must_be_forward_chronological": True,
            "no_cross_thread_fill": True,
            "no_duplicate_or_functionless_fill": True,
            "stop_without_evidence": True,
        }
        for key, expected in expected_extension.items():
            if extension.get(key) != expected:
                errors.append(
                    f"duration_extension_policy_invalid: {key} 必须为 {expected!r}。"
                )
    for block in blocks[1:]:
        if (
            contract.get("same_line_extension_only")
            and block.get("temporal_relation_from_previous") == "preview_future"
        ):
            errors.append(
                "future_arc_injection: 正文不得在已批准主线之后预览并接入未来完整弧。"
            )
    return list(dict.fromkeys(errors))


def validate(job_root: Path, *, allow_partial: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    span_report = validate_span_candidates(job_root)
    if not span_report["ok"]:
        errors.extend(
            f"span_candidates: {item}" for item in span_report["errors"]
        )
        return {"ok": False, "errors": errors, "warnings": warnings}
    plan_index_path = job_root / "story-plans" / "index.json"
    approval_path = job_root / "story-approval.json"
    portfolio_path = job_root / "story-portfolio.json"
    evidence_index_path = job_root / "story-evidence" / "index.json"
    span_index_path = job_root / "span-candidates" / "index.json"
    batch_path = job_root / "story-plan-batch.json"
    preflight_path = job_root / "story-plan-preflight.json"
    required_paths = (
        plan_index_path,
        approval_path,
        portfolio_path,
        evidence_index_path,
        span_index_path,
        batch_path,
        preflight_path,
    )
    missing = [str(path) for path in required_paths if not path.is_file()]
    if missing:
        return {
            "ok": False,
            "errors": [f"missing Story Plan input: {item}" for item in missing],
            "warnings": warnings,
        }
    index = load_json(plan_index_path)
    errors.extend(
        f"story_plans.index: {item}"
        for item in validate_task_response("story_plan_index", index)
    )
    approval = load_json(approval_path)
    evidence_index = load_json(evidence_index_path)
    span_index = load_json(span_index_path)
    batch = load_json(batch_path)
    preflight = load_json(preflight_path)
    try:
        generation_sha256 = current_plan_generation(job_root)
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"cannot derive current Story Plan generation: {exc}")
        generation_sha256 = ""
    if preflight.get("plan_generation_sha256") != generation_sha256:
        errors.append("Story Plan preflight generation is stale")
    if batch.get("plan_generation_sha256") != generation_sha256:
        errors.append("Story Plan batch generation is stale")
    if index.get("plan_generation_sha256") != generation_sha256:
        errors.append("Story Plan Index generation is stale")
    current_approval_sha256 = sha256_file(approval_path)
    current_span_index_sha256 = sha256_file(span_index_path)
    if index.get("story_approval_sha256") != current_approval_sha256:
        errors.append("Story Plan Index approval SHA-256 is stale")
    if index.get("span_candidate_index_sha256") != current_span_index_sha256:
        errors.append("Story Plan Index Span Candidate SHA-256 is stale")
    if index.get("status") == "stale":
        errors.append(
            "Story Plan generation is stale: current generation has not "
            "been materialized"
        )
        return {
            "ok": False,
            "status": "stale",
            "plan_generation_sha256": generation_sha256,
            "active_ready_plan_count": 0,
            "errors": errors,
            "warnings": warnings,
        }
    approved_entries = {
        item["story_id"]: item
        for item in approval.get("stories", [])
        if isinstance(item, dict) and item.get("decision") == "approved"
    }
    evidence_entries = {
        item["story_id"]: item
        for item in evidence_index.get("packets", [])
        if isinstance(item, dict)
    }
    bundle_entries = {
        item["story_id"]: item
        for item in span_index.get("bundles", [])
        if isinstance(item, dict)
    }
    plan_entries = indexed(
        index.get("plans", []),
        field="story_id",
        where="story_plans.index.plans",
        errors=errors,
    )
    job_outputs: dict[str, Path] = {}
    jobs_by_story: dict[str, dict[str, Any]] = {}
    contexts_by_story: dict[str, dict[str, Any]] = {}
    for job_index, job in enumerate(batch.get("jobs", [])):
        if not isinstance(job, dict) or job.get("task") != "story_plan_selection":
            continue
        context_path = Path(job.get("context_file", "")).expanduser().resolve()
        if not context_path.is_file():
            errors.append(
                f"story_plan_batch.jobs[{job_index}] context is missing"
            )
            continue
        context = load_json(context_path)
        story_id = context.get("story_id")
        output_value = job.get("output")
        if (
            not isinstance(story_id, str)
            or not isinstance(output_value, str)
            or story_id in job_outputs
        ):
            errors.append(
                f"story_plan_batch.jobs[{job_index}] has invalid identity"
            )
            continue
        job_outputs[story_id] = Path(output_value).expanduser().resolve()
        jobs_by_story[story_id] = job
        contexts_by_story[story_id] = context
    expected_story_ids = set(approved_entries)
    for label, observed in (
        ("Evidence Packets", set(evidence_entries)),
        ("Span Bundles", set(bundle_entries)),
        ("Story Plan batch", set(job_outputs)),
        ("Story Plans", set(plan_entries)),
    ):
        extra = observed - expected_story_ids
        missing = expected_story_ids - observed
        if extra:
            errors.append(
                f"{label} include non-approved Stories: {sorted(extra)}"
            )
        if missing:
            if allow_partial and label in {"Story Plan batch", "Story Plans"}:
                warnings.append(
                    f"{label} cover only a subset of approved Stories: "
                    f"missing={sorted(missing)} (allow_partial=True)"
                )
            else:
                errors.append(
                    f"{label} differ from approved Story selection: "
                    f"missing={sorted(missing)}, extra={sorted(extra)}"
                )
    portfolio_sha256 = sha256_file(portfolio_path)
    evidence_index_sha256 = sha256_file(evidence_index_path)
    observed_statuses: list[str] = []
    observed_slots: list[int] = []
    for story_id, entry in plan_entries.items():
        plan_path_value = entry.get("path")
        if not isinstance(plan_path_value, str):
            errors.append(f"{story_id}: Story Plan path must be a string")
            continue
        plan_path = Path(plan_path_value).expanduser().resolve()
        if not plan_path.is_file():
            errors.append(f"{story_id}: missing Story Plan {plan_path}")
            continue
        if entry.get("plan_sha256") != sha256_file(plan_path):
            errors.append(f"{story_id}: Story Plan SHA-256 is stale")
        plan = load_json(plan_path)
        errors.extend(
            f"{story_id}.plan: {item}"
            for item in validate_task_response("story_plan", plan)
        )
        approved_for_contract = approved_entries.get(story_id)
        if isinstance(approved_for_contract, dict):
            script_for_contract_path = Path(
                approved_for_contract.get("script_path", "")
            ).expanduser().resolve()
            if script_for_contract_path.is_file():
                errors.extend(
                    f"{story_id}.editorial_contract: {item}"
                    for item in validate_editorial_plan_contract(
                        plan, load_json(script_for_contract_path)
                    )
                )
        blocks = plan.get("blocks", [])
        if blocks:
            teaser_clips = blocks[0].get("clips", [])
            if (
                blocks[0].get("role") != "teaser"
                or len(teaser_clips) != 1
                or teaser_clips[0].get("reuse_mode") != "none"
            ):
                errors.append(
                    f"{story_id}: single_highlight Teaser must be the first "
                    "Block and contain exactly one non-reprise Clip"
                )
        edges_by_target = {
            item.get("to_block_id"): item
            for item in plan.get("sequence_edges", [])
            if isinstance(item, dict)
        }
        for previous_block, current_block in zip(blocks, blocks[1:]):
            if not is_obvious_backward_episode_jump(
                previous_block.get("clips", []),
                current_block.get("clips", []),
            ):
                continue
            edge = edges_by_target.get(current_block.get("id"), {})
            if (
                edge.get("temporal_relation") != "flashback_context"
                or edge.get("orientation_required") is not True
                or edge.get("orientation_strategy") == "none"
            ):
                errors.append(
                    f"{story_id}: obvious backward episode jump into "
                    f"{current_block.get('id')} must be flashback_context "
                    "with explicit orientation"
                )
        observed_statuses.append(str(plan.get("status")))
        if isinstance(plan.get("production_slot"), int):
            observed_slots.append(plan["production_slot"])
        approved = approved_entries.get(story_id)
        evidence_entry = evidence_entries.get(story_id)
        bundle_entry = bundle_entries.get(story_id)
        selection_path = job_outputs.get(story_id)
        if not all(
            (
                isinstance(approved, dict),
                isinstance(evidence_entry, dict),
                isinstance(bundle_entry, dict),
                isinstance(selection_path, Path),
            )
        ):
            continue
        assert selection_path is not None
        if not selection_path.is_file():
            errors.append(f"{story_id}: selection result is missing")
            continue
        script_path = Path(approved["script_path"]).expanduser().resolve()
        packet_path = Path(evidence_entry["path"]).expanduser().resolve()
        bundle_path = Path(bundle_entry["path"]).expanduser().resolve()
        if not all(path.is_file() for path in (script_path, packet_path, bundle_path)):
            errors.append(f"{story_id}: current Story Plan input is missing")
            continue
        script = load_json(script_path)
        allowed_thread_beat_ids = set(script.get("selected_thread_beat_ids", []))
        planned_thread_beat_ids = set(
            item
            for block in plan.get("blocks", [])
            if isinstance(block, dict)
            for item in block.get("thread_beat_ids", [])
            if isinstance(item, str)
        )
        future_thread_beat_ids = sorted(
            planned_thread_beat_ids - allowed_thread_beat_ids
        )
        if future_thread_beat_ids:
            errors.append(
                f"{story_id}: Story Plan 注入了批准 Script 范围之外的 Thread Beat，"
                f"禁止用后续剧情填充时长: {future_thread_beat_ids}"
            )
        hook_beat_ids = {
            item.get("id")
            for item in script.get("beats", [])
            if isinstance(item, dict) and item.get("role") == "end_hook"
        }
        if hook_beat_ids and plan.get("blocks"):
            final_beat_ids = set(plan["blocks"][-1].get("beat_ids", []))
            if not hook_beat_ids <= final_beat_ids:
                errors.append(
                    f"{story_id}: 结尾没有落在批准 Script 的 end_hook，"
                    "不得用后续完整收尾替代未完成态"
                )
        fingerprints = {
            "story_approval_sha256": current_approval_sha256,
            "portfolio_sha256": portfolio_sha256,
            "story_script_sha256": sha256_file(script_path),
            "story_evidence_index_sha256": evidence_index_sha256,
            "story_evidence_packet_sha256": sha256_file(packet_path),
            "span_candidate_index_sha256": current_span_index_sha256,
            "span_candidate_bundle_sha256": sha256_file(bundle_path),
            "selection_result_sha256": sha256_file(selection_path),
            "plan_generation_sha256": generation_sha256,
        }
        if plan.get("input_fingerprints") != fingerprints:
            errors.append(f"{story_id}: Story Plan input fingerprints are stale")
        selection = load_json(selection_path)
        selection_errors = validate_task_response(
            "story_plan_selection", selection
        )
        job = jobs_by_story.get(story_id, {})
        response_schema = job.get("response_format", {}).get(
            "json_schema", {}
        ).get("schema")
        if isinstance(response_schema, dict):
            selection_errors.extend(
                validate_schema(selection, response_schema)
            )
        legal_options = contexts_by_story.get(story_id, {}).get(
            "legal_option_contract"
        )
        if isinstance(legal_options, dict):
            selection_errors.extend(
                validate_option_selection(selection, legal_options)
            )
        else:
            selection_errors.append(
                "Story Plan context has no legal_option_contract"
            )
        if selection_errors:
            errors.append(
                f"{story_id}: invalid selection result: "
                + "; ".join(selection_errors[:20])
            )
            continue
        try:
            expanded_selection = expand_option_selection(
                selection, legal_options
            )
            expected_plan = materialize_plan(
                expanded_selection,
                script=load_json(script_path),
                bundle=load_json(bundle_path),
                evidence_packet=load_json(packet_path),
                fingerprints=fingerprints,
            )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"{story_id}: cannot rematerialize Story Plan: {exc}")
            continue
        if plan != expected_plan:
            errors.append(
                f"{story_id}: Story Plan differs from deterministic local materialization"
            )
        expected_entry = {
            "story_id": story_id,
            "title": plan.get("title"),
            "production_slot": plan.get("production_slot"),
            "status": plan.get("status"),
            "path": str(plan_path),
            "plan_sha256": sha256_file(plan_path),
            "story_script_sha256": fingerprints["story_script_sha256"],
            "span_candidate_bundle_sha256": fingerprints[
                "span_candidate_bundle_sha256"
            ],
            "selection_result_sha256": fingerprints[
                "selection_result_sha256"
            ],
            "estimated_duration_seconds": plan.get(
                "estimated_duration_seconds"
            ),
            "block_count": len(plan.get("blocks", [])),
            "clip_count": sum(
                len(item.get("clips", [])) for item in plan.get("blocks", [])
            ),
        }
        if entry != expected_entry:
            errors.append(f"{story_id}: Story Plan Index entry is inconsistent")
    if index.get("plan_count") != len(plan_entries):
        errors.append("Story Plan Index plan_count is inconsistent")
    ready_count = observed_statuses.count("ready_for_video_qc")
    blocked_count = observed_statuses.count("blocked")
    if index.get("ready_plan_count") != ready_count:
        errors.append("Story Plan Index ready_plan_count is inconsistent")
    if index.get("blocked_plan_count") != blocked_count:
        errors.append("Story Plan Index blocked_plan_count is inconsistent")
    if plan_entries and ready_count == len(plan_entries):
        expected_status = "ready_for_video_qc"
    elif ready_count:
        expected_status = "partially_ready"
    else:
        expected_status = "blocked"
    if index.get("status") != expected_status:
        errors.append("Story Plan Index status is inconsistent")
    if len(set(observed_slots)) != len(observed_slots):
        errors.append("Story Plan production slots must be unique")
    if expected_status != "ready_for_video_qc":
        warnings.append(
            "Story Plan portfolio is not ready for Selected Video QC"
        )
    if any(
        load_json(Path(entry["path"]))["video_review_span_candidate_ids"]
        for entry in plan_entries.values()
        if isinstance(entry.get("path"), str) and Path(entry["path"]).is_file()
    ):
        warnings.append(
            "Selected Span boundaries still require targeted video review"
        )
    generation_is_active = bool(generation_sha256) and all(
        artifact.get("plan_generation_sha256") == generation_sha256
        for artifact in (preflight, batch, index)
    )
    return {
        "ok": not errors,
        "status": "current" if not errors else "invalid_or_stale",
        "plan_generation_sha256": generation_sha256,
        "active_ready_plan_count": ready_count if generation_is_active else 0,
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_root", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Treat missing Story Plans / batch jobs for approved Stories as "
            "warnings instead of errors."
        ),
    )
    args = parser.parse_args()
    job_root = args.job_root.expanduser().resolve()
    report = validate(job_root, allow_partial=args.allow_partial)
    report_path = (
        args.report.expanduser().resolve()
        if args.report
        else job_root / "story-plan-validation.json"
    )
    atomic_write_json(report_path, report)
    print(f"STATUS\t{'OK' if report['ok'] else 'FAILED'}")
    print(f"ERRORS\t{len(report['errors'])}")
    print(f"WARNINGS\t{len(report['warnings'])}")
    print(f"REPORT\t{report_path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())