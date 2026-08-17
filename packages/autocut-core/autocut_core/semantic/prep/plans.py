"""autocut_core.semantic.prep.plans — Story Plan 准备阶段。

从 prepare_story_stages.py 提取的 Plan 相关函数。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


from autocut_core.semantic.prep.utils import batch_payload, write_context
from autocut_core.libs.span_compiler import SPAN_COMPILER_METHOD
from autocut_core.io import (
    atomic_write_json,
    load_json,
    sha256_file,
    stable_id,
    update_project_stage,
)
from autocut_core.semantic.plan_arena import CANDIDATE_ARENA_METHOD, split_candidate_contracts
from autocut_core.semantic.plan_generation import plan_generation_sha256
from autocut_core.semantic.prep._plan_options_helpers import (
    build_local_orientation_selection,
    build_synthetic_selection,
    is_unique_option_case,
    orientation_fallback_response_schema,
    rank_legal_body_finalists,
)
from autocut_core.libs.editorial_plan import (
    COMPILER_VERSION,
    PLANNING_CONTRACT_VERSION,
    PREFERRED_MEDIAN_CLIP_SECONDS_RANGE,
    PREFERRED_MINIMUM_CLIP_COUNT_DIVISOR,
    compile_legal_options,
    dynamic_selection_schema,
    validate_option_selection,
)
from autocut_core.semantic.proxy_media import (
    FinalistProxyUnavailable,
    render_finalist_comparison,
)
from autocut_core.schema.compat import validate_task_response
from autocut_core.contracts.teaser_contract import (
    TEASER_MAXIMUM_SECONDS,
    TEASER_PREFERRED_MINIMUM_SECONDS,
)
from autocut_core.contracts.span_validation import validate as validate_span_candidates


LEGACY_PLANNING_CONTRACT_VERSION = "planning-contract-v15-functional-boundary"


def rank_plan_finalist_contract(
    legal_options: dict[str, Any],
) -> dict[str, Any]:
    """Keep a quality-ranked arena of at most three legal Plan finalists.

    原位置: prepare_story_stages.rank_plan_finalist_contract (L90, 145L)
    """
    partitions = list(legal_options.get("legal_body_partitions", []))
    if not partitions:
        raise ValueError(
            "cannot rank Story Plan finalists without a partition"
        )
    finalists = rank_legal_body_finalists(
        partitions,
        legal_block_options=legal_options.get("legal_block_options", []),
        span_catalog=legal_options.get("span_catalog", []),
    )
    teaser_by_id = {
        item["option_id"]: item
        for item in legal_options["legal_teaser_options"]
    }
    finalist_teaser_ids: set[str] = set()
    normalized_finalists: list[dict[str, Any]] = []
    for candidate_rank, partition in enumerate(finalists, start=1):
        compatible = [
            teaser_id
            for teaser_id in partition.get(
                "compatible_teaser_option_ids", []
            )
            if teaser_id in teaser_by_id
        ]
        if teaser_by_id and not compatible:
            raise ValueError(
                "Story Plan finalist has no compatible Teaser"
            )
        if compatible:
            repeat_by_teaser = partition.get(
                "repeat_metrics_by_teaser_option_id", {}
            )

            def teaser_rank(teaser_id: str) -> tuple[Any, ...]:
                teaser = teaser_by_id[teaser_id]
                duration = float(teaser.get("duration_seconds", 0.0))
                repeat = repeat_by_teaser.get(teaser_id, {})
                return (
                    not (8.0 <= duration <= TEASER_MAXIMUM_SECONDS),
                    float(repeat.get("repeat_ratio", 0.0)),
                    abs(duration - 12.0),
                    teaser_id,
                )

            teaser_id = min(compatible, key=teaser_rank)
            finalist_teaser_ids.add(teaser_id)
            partition = {
                **partition,
                "compatible_teaser_option_ids": [teaser_id],
                "repeat_metrics_by_teaser_option_id": {
                    teaser_id: repeat_by_teaser.get(teaser_id, {})
                },
            }
        teaser_id_for_identity = (
            partition.get("compatible_teaser_option_ids") or [""]
        )[0]
        normalized_finalists.append(
            {
                **partition,
                "plan_candidate_id": stable_id(
                    "plan-candidate",
                    {
                        "story_id": legal_options["story_id"],
                        "body_partition_id": partition["partition_id"],
                        "teaser_option_id": teaser_id_for_identity,
                    },
                ),
                "candidate_rank": candidate_rank,
            }
        )
    body_option_ids = {
        option_id
        for partition in normalized_finalists
        for option_id in partition["body_option_ids"]
    }
    body_options = [
        {
            **item,
            "compatible_teaser_option_ids": [
                teaser_id
                for teaser_id in item.get(
                    "compatible_teaser_option_ids", []
                )
                if teaser_id in finalist_teaser_ids
            ],
        }
        for item in legal_options["legal_block_options"]
        if item["option_id"] in body_option_ids
    ]
    selected_span_ids = {
        span_id
        for option in [
            *(
                teaser_by_id[teaser_id]
                for teaser_id in sorted(finalist_teaser_ids)
            ),
            *body_options,
        ]
        for span_id in option["span_candidate_ids"]
    }
    return {
        "schema_version": legal_options["schema_version"],
        "compiler_version": legal_options["compiler_version"],
        "story_id": legal_options["story_id"],
        "production_slot": legal_options["production_slot"],
        "required_beat_ids": legal_options["required_beat_ids"],
        "first_required_body_beat_id": legal_options.get(
            "first_required_body_beat_id"
        ),
        "first_story_block_required_beat_id": legal_options.get(
            "first_story_block_required_beat_id"
        ),
        "teaser_beat_id": legal_options["teaser_beat_id"],
        "highlight_candidate_ids": legal_options[
            "highlight_candidate_ids"
        ],
        "legal_teaser_options": [
            teaser_by_id[teaser_id]
            for teaser_id in sorted(finalist_teaser_ids)
        ],
        "legal_block_options": body_options,
        "legal_body_partitions": normalized_finalists,
        "span_catalog": [
            item
            for item in legal_options.get("span_catalog", [])
            if item["span_candidate_id"] in selected_span_ids
        ],
        "model_contract_finalists": True,
        "partition_selection_mode": "local_quality_finalists",
        "maximum_finalists": 3,
        "finalist_count": len(normalized_finalists),
        "source_legal_options_sha256": legal_options[
            "legal_options_sha256"
        ],
        "preflight_summary": {
            "status": legal_options["preflight"]["status"],
            "failure_codes": legal_options["preflight"][
                "failure_codes"
            ],
        },
    }


def finalist_editorial_density_diagnostics(
    legal_options: dict[str, Any],
    script: dict[str, Any],
) -> dict[str, Any]:
    """Summarize whether at least one local finalist meets edit-density goals.

    This is an audit signal, not a new Plan gate.  The current Span compiler
    has already performed the single evidence-safe compaction pass; if every
    finalist remains sparse, an explicitly declared continuity fallback stays
    visible instead of being silently chopped into invented boundaries.

    原位置: prepare_story_stages.finalist_editorial_density_diagnostics (L237, 68L)
    """
    partitions = list(legal_options.get("legal_body_partitions", []))
    finalists = rank_legal_body_finalists(partitions) if partitions else []
    continuity_fallback_beat_ids = [
        beat_id
        for beat_id in script.get("feasibility", {}).get(
            "continuity_fallback_beat_ids", []
        )
        if isinstance(beat_id, str)
    ]
    rows: list[dict[str, Any]] = []
    for partition in finalists:
        violated = set(partition.get("constraints_violated", []))
        reasons = []
        if "clip_count_below_preferred_minimum" in violated:
            reasons.append("clip_count_below_preferred_minimum")
        if "median_clip_outside_preferred_range" in violated:
            reasons.append("median_clip_outside_preferred_range")
        rows.append(
            {
                "partition_id": partition["partition_id"],
                "physical_span_sequence": list(
                    partition.get("physical_span_sequence", [])
                ),
                "clip_count": int(partition.get("clip_count", 0)),
                "preferred_minimum_clip_count": int(
                    partition.get("preferred_minimum_clip_count", 1)
                ),
                "median_clip_duration_seconds": float(
                    partition.get("median_clip_duration_seconds", 0.0)
                ),
                "functional_boundary_metrics": dict(
                    partition.get("functional_boundary_metrics", {})
                ),
                "status": "below_target" if reasons else "passed",
                "reasons": reasons,
            }
        )
    has_density_safe_finalist = any(
        item["status"] == "passed" for item in rows
    )
    if has_density_safe_finalist:
        status = "passed"
    elif continuity_fallback_beat_ids:
        status = "degraded_continuity_fallback"
    else:
        status = "below_target"
    return {
        "status": status,
        "preferred_median_clip_seconds_range": list(
            PREFERRED_MEDIAN_CLIP_SECONDS_RANGE
        ),
        "span_compiler_method": SPAN_COMPILER_METHOD,
        "automatic_span_recovery_max_attempts": 1,
        "automatic_span_recovery_already_compiled": True,
        "continuity_fallback_beat_ids": continuity_fallback_beat_ids,
        "finalists": rows,
    }


def prepare_plans(args: argparse.Namespace) -> Path:
    """准备 Story Plan 批处理 manifest。

    原位置: prepare_story_stages.prepare_plans (L2567, 852L)
    """
    job_root = args.job_root.resolve()
    candidate_arena = bool(getattr(args, "candidate_arena", False))
    span_report = validate_span_candidates(job_root)
    if not span_report["ok"]:
        raise ValueError(
            "Span Candidate validation failed: "
            + "; ".join(span_report["errors"][:30])
        )
    approval_path = job_root / "story-approval.json"
    span_index_path = job_root / "span-candidates" / "index.json"
    evidence_index_path = job_root / "story-evidence" / "index.json"
    portfolio_path = job_root / "story-portfolio.json"
    for path in (
        approval_path,
        span_index_path,
        evidence_index_path,
        portfolio_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    approval = load_json(approval_path)
    if (
        approval.get("fulfillment_status") != "ready"
        or approval.get("status") != "story_selection_complete"
    ):
        raise ValueError("Story approval exit is not ready for Story Plan")
    span_index = load_json(span_index_path)
    span_errors = validate_task_response("span_candidate_index", span_index)
    if span_errors:
        raise ValueError(
            "invalid Span Candidate Index: " + "; ".join(span_errors[:30])
        )
    evidence_index = load_json(evidence_index_path)
    evidence_entries = {
        item["story_id"]: item for item in evidence_index["packets"]
    }
    approval_entries = {
        item["story_id"]: item
        for item in approval["stories"]
        if item.get("decision") == "approved"
    }
    bundle_entries = {
        item["story_id"]: item for item in span_index["bundles"]
    }
    selected_story_ids = set(approval.get("selected_story_ids", []))
    if (
        set(approval_entries) != selected_story_ids
        or set(evidence_entries) != selected_story_ids
        or not set(bundle_entries) <= selected_story_ids
    ):
        raise ValueError(
            "Story Plan inputs contain stale or non-approved Story identities"
        )
    fingerprints_base = {
        "story_approval_sha256": sha256_file(approval_path),
        "portfolio_sha256": sha256_file(portfolio_path),
        "story_evidence_index_sha256": sha256_file(evidence_index_path),
        "span_candidate_index_sha256": sha256_file(span_index_path),
    }
    jobs = []
    candidate_records: list[dict[str, Any]] = []
    plan_context_chars: dict[str, int] = {}
    prepared_stories: list[dict[str, Any]] = []
    preflight_stories: list[dict[str, Any]] = []
    context_dir = job_root / "intermediate" / "story-plan-contexts"
    output_dir = job_root / "story-plan-selection-results"
    missing_bundle_story_ids = sorted(
        selected_story_ids - set(bundle_entries),
        key=lambda story_id: int(
            approval_entries[story_id]["production_slot"]
        ),
    )
    for story_id in missing_bundle_story_ids:
        evidence_status = evidence_entries[story_id].get(
            "status", "incomplete"
        )
        preflight_stories.append(
            {
                "story_id": story_id,
                "production_slot": approval_entries[story_id][
                    "production_slot"
                ],
                "status": "blocked",
                "failure_codes": ["upstream_evidence_incomplete"],
                "missing_body_beat_ids": [],
                "repair_route": "story_evidence",
                "repair_routes": [
                    {
                        "code": "upstream_evidence_incomplete",
                        "return_to_stage": "story_evidence",
                        "reason": (
                            "该 Story 的 Evidence/Span 尚不完整；保留人工"
                            " Approval，修复该 Story 后重编译。"
                        ),
                    }
                ],
                "treatment_viability": {},
                "teaser_diagnostics": {},
                "editorial_surplus_diagnostics": {},
                "full_source_like_diagnostics": {},
                "body_options_diagnostics": {},
                "body_partition_diagnostics": {},
                "editorial_density_diagnostics": {
                    "status": "blocked",
                    "preferred_median_clip_seconds_range": list(
                        PREFERRED_MEDIAN_CLIP_SECONDS_RANGE
                    ),
                    "span_compiler_method": SPAN_COMPILER_METHOD,
                    "automatic_span_recovery_max_attempts": 1,
                    "automatic_span_recovery_already_compiled": False,
                    "continuity_fallback_beat_ids": [],
                    "finalists": [],
                },
                "teaser_option_count": 0,
                "body_option_count": 0,
                "legal_body_partition_count": 0,
                "legal_options_sha256": "",
                "upstream_status": evidence_status,
            }
        )
    for entry in sorted(
        span_index["bundles"], key=lambda item: int(item["production_slot"])
    ):
        story_id = entry["story_id"]
        approved = approval_entries[story_id]
        script_path = Path(approved["script_path"]).expanduser().resolve()
        bundle_path = Path(entry["path"]).expanduser().resolve()
        packet_path = Path(evidence_entries[story_id]["path"]).expanduser().resolve()
        for path in (script_path, bundle_path, packet_path):
            if not path.is_file():
                raise FileNotFoundError(path)
        script_sha256 = sha256_file(script_path)
        bundle_sha256 = sha256_file(bundle_path)
        packet_sha256 = sha256_file(packet_path)
        if script_sha256 != approved.get("approved_script_sha256"):
            raise ValueError(f"approved Story Script is stale: {story_id}")
        if bundle_sha256 != entry.get("bundle_sha256"):
            raise ValueError(f"Span Candidate Bundle is stale: {story_id}")
        if packet_sha256 != evidence_entries[story_id].get("packet_sha256"):
            raise ValueError(f"Story Evidence Packet is stale: {story_id}")
        script = load_json(script_path)
        bundle = load_json(bundle_path)
        evidence_packet = load_json(packet_path)
        legal_options = compile_legal_options(
            script, bundle, evidence_packet=evidence_packet
        )
        preflight = legal_options["preflight"]
        editorial_density_diagnostics = (
            finalist_editorial_density_diagnostics(legal_options, script)
            if preflight["status"] == "ready"
            else {
                "status": "blocked",
                "preferred_median_clip_seconds_range": list(
                    PREFERRED_MEDIAN_CLIP_SECONDS_RANGE
                ),
                "span_compiler_method": SPAN_COMPILER_METHOD,
                "automatic_span_recovery_max_attempts": 1,
                "automatic_span_recovery_already_compiled": True,
                "continuity_fallback_beat_ids": list(
                    script.get("feasibility", {}).get(
                        "continuity_fallback_beat_ids", []
                    )
                ),
                "finalists": [],
            }
        )
        preflight_stories.append(
            {
                "story_id": story_id,
                "production_slot": entry["production_slot"],
                "status": preflight["status"],
                "failure_codes": preflight["failure_codes"],
                "missing_body_beat_ids": preflight[
                    "missing_body_beat_ids"
                ],
                "repair_route": preflight["repair_route"],
                "repair_routes": preflight.get("repair_routes", []),
                "treatment_viability": preflight.get(
                    "treatment_viability", {}
                ),
                "teaser_diagnostics": preflight[
                    "teaser_diagnostics"
                ],
                "editorial_surplus_diagnostics": preflight[
                    "editorial_surplus_diagnostics"
                ],
                "full_source_like_diagnostics": preflight[
                    "full_source_like_diagnostics"
                ],
                "body_options_diagnostics": preflight.get(
                    "body_options_diagnostics", {}
                ),
                "body_partition_diagnostics": preflight.get(
                    "body_partition_diagnostics", {}
                ),
                "editorial_density_diagnostics": (
                    editorial_density_diagnostics
                ),
                "teaser_option_count": len(
                    legal_options["legal_teaser_options"]
                ),
                "body_option_count": len(
                    legal_options["legal_block_options"]
                ),
                "legal_body_partition_count": len(
                    legal_options.get("legal_body_partitions", [])
                ),
                "legal_options_sha256": legal_options[
                    "legal_options_sha256"
                ],
            }
        )
        prepared_stories.append(
            {
                "entry": entry,
                "story_id": story_id,
                "script": script,
                "script_sha256": script_sha256,
                "bundle_sha256": bundle_sha256,
                "packet_sha256": packet_sha256,
                "legal_options": legal_options,
            }
        )
    preflight_path = job_root / "story-plan-preflight.json"
    blocked_preflights = [
        item for item in preflight_stories if item["status"] != "ready"
    ]
    preflight_payload = {
        "schema_version": "1.0",
        "compiler_version": (
            prepared_stories[0]["legal_options"]["compiler_version"]
            if prepared_stories
            else COMPILER_VERSION
        ),
        "status": "blocked" if blocked_preflights else "ready",
        "story_count": len(preflight_stories),
        "ready_story_count": len(preflight_stories)
        - len(blocked_preflights),
        "blocked_story_count": len(blocked_preflights),
        "stories": preflight_stories,
    }
    generation_sha256 = plan_generation_sha256(
        story_approval_sha256=fingerprints_base[
            "story_approval_sha256"
        ],
        story_evidence_index_sha256=fingerprints_base[
            "story_evidence_index_sha256"
        ],
        span_candidate_index_sha256=fingerprints_base[
            "span_candidate_index_sha256"
        ],
        preflight=preflight_payload,
    )
    preflight_payload["plan_generation_sha256"] = generation_sha256
    atomic_write_json(preflight_path, preflight_payload)
    fingerprints_base["plan_generation_sha256"] = generation_sha256
    # Phase 1 of the generation commit happens before any model context is
    # written.  Even a context-budget preflight failure must deactivate the
    # previous generation immediately.
    atomic_write_json(
        job_root / "story-plans" / "index.json",
        {
            "schema_version": "1.0",
            "method": "legal-option-selection-local-materialization-v2",
            "status": "stale",
            "story_approval_sha256": fingerprints_base[
                "story_approval_sha256"
            ],
            "span_candidate_index_sha256": fingerprints_base[
                "span_candidate_index_sha256"
            ],
            "plan_generation_sha256": generation_sha256,
            "plan_count": 0,
            "ready_plan_count": 0,
            "blocked_plan_count": 0,
            "plans": [],
        },
    )
    if candidate_arena:
        atomic_write_json(
            job_root / "story-plan-candidates" / "index.json",
            {
                "schema_version": "1.0",
                "method": CANDIDATE_ARENA_METHOD,
                "status": "stale",
                "story_approval_sha256": fingerprints_base[
                    "story_approval_sha256"
                ],
                "span_candidate_index_sha256": fingerprints_base[
                    "span_candidate_index_sha256"
                ],
                "plan_generation_sha256": generation_sha256,
                "story_count": 0,
                "candidate_count": 0,
                "ready_candidate_count": 0,
                "blocked_candidate_count": 0,
                "candidates": [],
            },
        )
    batching_blocked = bool(blocked_preflights) and not args.allow_partial
    ready_prepared = [
        prepared
        for prepared in prepared_stories
        if prepared["legal_options"]["preflight"]["status"] == "ready"
    ] if not batching_blocked else []
    for prepared in ready_prepared:
        entry = prepared["entry"]
        story_id = prepared["story_id"]
        script = prepared["script"]
        full_legal_options = prepared["legal_options"]
        legal_options = rank_plan_finalist_contract(full_legal_options)
        if candidate_arena:
            candidate_contracts = split_candidate_contracts(legal_options)
            finalist_proxy = {
                "status": "deferred_to_full_candidate_qc",
                "reason": (
                    "Plan finalists are independently materialized and receive "
                    "full Story QC before deterministic winner election"
                ),
                "required_for_multiple_finalists": False,
            }
            planning_contract_version = PLANNING_CONTRACT_VERSION
        else:
            candidate_contracts = [legal_options]
            planning_contract_version = LEGACY_PLANNING_CONTRACT_VERSION
            try:
                rendered_proxy = render_finalist_comparison(
                    job_root, legal_options
                )
            except FinalistProxyUnavailable as exc:
                finalist_proxy = {
                    "status": "unavailable",
                    "reason": str(exc),
                    "required_for_multiple_finalists": True,
                }
            else:
                finalist_proxy = (
                    rendered_proxy
                    if rendered_proxy is not None
                    else {
                        "status": "not_required",
                        "reason": (
                            "one finalist or no differing local Span sequence"
                        ),
                        "required_for_multiple_finalists": False,
                    }
                )
        target_duration = script["target_duration"]
        surplus = full_legal_options["preflight"][
            "editorial_surplus_diagnostics"
        ]
        minimum_total = float(target_duration["minimum_seconds"])
        preferred_target = float(target_duration["preferred_target_seconds"])
        preferred_median_min, preferred_median_max = (
            PREFERRED_MEDIAN_CLIP_SECONDS_RANGE
        )
        preferred_minimum_clip_count = max(
            [
                int(item.get("preferred_minimum_clip_count", 1))
                for item in legal_options["legal_body_partitions"]
            ],
            default=1,
        )
        editorial_density_contract = {
            "preferred_median_clip_seconds_range": [
                preferred_median_min,
                preferred_median_max,
            ],
            "preferred_minimum_clip_count": preferred_minimum_clip_count,
            "preferred_minimum_clip_count_divisor": (
                PREFERRED_MINIMUM_CLIP_COUNT_DIVISOR
            ),
            "hint": (
                "选择 body 组合时，尽量让 clip 中位时长落在 "
                f"{preferred_median_min:g}–{preferred_median_max:g} 秒、"
                f"clip 总数不少于 {preferred_minimum_clip_count} 条，"
                "避免『三镜头串烧』式的稀疏节奏。"
            ),
        }

        context_base = {
            "schema_version": "1.0",
            "planning_contract_version": planning_contract_version,
            "story_id": story_id,
            "title": script["title"],
            "production_slot": entry["production_slot"],
            "input_fingerprints": {
                **fingerprints_base,
                "story_script_sha256": prepared["script_sha256"],
                "story_evidence_packet_sha256": prepared["packet_sha256"],
                "span_candidate_bundle_sha256": prepared["bundle_sha256"],
            },
            "story_script": script,
            "finalist_proxy_comparison": finalist_proxy,
            "planning_contract": {
                "planning_contract_version": planning_contract_version,
                "model_selects_legal_option_ids_only": True,
                "finalists_selected_locally": True,
                "model_selects_between_finalists": (
                    not candidate_arena
                    and len(legal_options["legal_body_partitions"]) > 1
                ),
                **(
                    {
                        "all_candidates_receive_full_qc": True,
                        "winner_selection_occurs_after_qc": True,
                        "cross_treatment_candidates_forbidden": True,
                    }
                    if candidate_arena
                    else {}
                ),
                "body_teaser_selection_is_composite": True,
                "local_proxy_comparison_status": finalist_proxy["status"],
                "model_timecodes_forbidden": True,
                "local_materialization_required": True,
                "all_must_have_beats_required": True,
                "all_structurally_required_beats_required": True,
                "mode_none_all_authored_beats_required": True,
                "all_required_thread_beats_require_selected_span_support": True,
                "approved_beat_order_is_binding": True,
                "body_options_must_partition_required_beats": True,
                "cross_story_source_reuse_allowed": True,
                "teaser_contract_is_compiler_fixed": True,
                "same_story_overlap_is_compiler_classified": True,
                "same_span_candidate_max_uses": 3,
                "same_span_candidate_reuse_mode": "teaser_reprise",
                "highlight_first_required": True,
                # Plan 2 & 3: teaser 上限 15s；repeat 只保留 10% ratio。
                "teaser_preferred_seconds": [
                    TEASER_PREFERRED_MINIMUM_SECONDS,
                    TEASER_MAXIMUM_SECONDS,
                ],
                "teaser_maximum_seconds": TEASER_MAXIMUM_SECONDS,
                "maximum_repeat_seconds": None,
                "maximum_repeat_ratio": 0.10,
                "full_source_like_threshold": 0.85,
                "short_dense_source_exemption": {
                    "maximum_source_duration_seconds": 180.0,
                    "minimum_semantic_density_ratio": 0.75,
                },
                "maximum_full_source_like_clip_count": 1,
                "maximum_full_source_like_playback_ratio": 0.5,
                "minimum_editorial_surplus_ratio": 0.1,
                "repeat_or_full_episode_padding_forbidden": True,
                "target_duration_seconds": target_duration,
                "minimum_total_duration_seconds": target_duration[
                    "minimum_seconds"
                ],
                "preferred_minimum_total_duration_seconds": target_duration[
                    "preferred_minimum_seconds"
                ],
                "preferred_target_total_duration_seconds": target_duration[
                    "preferred_target_seconds"
                ],
                "maximum_total_duration_seconds": target_duration[
                    "maximum_seconds"
                ],
                "selection_total_duration_must_meet_minimum": True,
                "selection_total_duration_arithmetic_buffer_seconds": 3.0,
                "available_candidate_unique_duration_seconds": surplus[
                    "available_candidate_unique_duration_seconds"
                ],
                "editorial_surplus_seconds": surplus[
                    "editorial_surplus_seconds"
                ],
                "editorial_surplus_ratio": surplus[
                    "editorial_surplus_ratio"
                ],
                "editorial_density": editorial_density_contract,
                "functional_boundary": {
                    "direct_must_show_event_ranges_only": True,
                    "fact_and_context_ranges_excluded": True,
                    "coverage_precedes_selection_precision": True,
                    "ranking_precedes_editorial_density": True,
                    "shared_thread_id_alone_is_not_atomic_causality": True,
                    "continuity_closure_fallback_remains_pinned": True,
                },
                "transitions_out_of_scope": True,
                "video_boundary_verification_out_of_scope": True,
            },
        }
        ambiguous_candidates: list[dict[str, Any]] = []
        for candidate_options in candidate_contracts:
            candidate_id = candidate_options.get("plan_candidate_id")
            if candidate_arena:
                context = {
                    **context_base,
                    "plan_candidate": {
                        "plan_candidate_id": candidate_id,
                        "candidate_rank": candidate_options["candidate_rank"],
                        "candidate_count": candidate_options[
                            "candidate_count"
                        ],
                        "body_partition_id": candidate_options[
                            "body_partition_id"
                        ],
                        "teaser_option_id": candidate_options[
                            "teaser_option_id"
                        ],
                    },
                    "legal_option_contract": candidate_options,
                }
                context_path = (
                    context_dir / story_id / f"{candidate_id}.json"
                )
                output_path = output_dir / story_id / f"{candidate_id}.json"
                context_key = str(candidate_id)
            else:
                context = {
                    **context_base,
                    "legal_option_contract": candidate_options,
                }
                context_path = context_dir / f"{story_id}.json"
                output_path = output_dir / f"{story_id}.json"
                context_key = story_id
            context_chars = write_context(
                context_path, context, args.max_context_chars
            )
            plan_context_chars[context_key] = context_chars
            response_schema = dynamic_selection_schema(candidate_options)
            synthetic_selection_written = False
            orientation_resolution = "model_selection"
            ambiguity_reasons: list[str] = []
            if candidate_arena:
                synthetic_response, ambiguity_reasons = (
                    build_local_orientation_selection(
                        candidate_options, script
                    )
                )
                orientation_resolution = (
                    "local_deterministic"
                    if synthetic_response is not None
                    else "story_model_fallback"
                )
            elif is_unique_option_case(candidate_options):
                synthetic_response = build_synthetic_selection(candidate_options)
            else:
                synthetic_response = None
            if synthetic_response is not None:
                selection_errors = validate_task_response(
                    "story_plan_selection", synthetic_response
                )
                selection_errors.extend(
                    validate_option_selection(
                        synthetic_response, candidate_options
                    )
                )
                if not selection_errors:
                    atomic_write_json(output_path, synthetic_response)
                    synthetic_selection_written = True
                elif candidate_arena:
                    raise ValueError(
                        f"{story_id}/{candidate_id}: invalid locally compiled "
                        "Candidate orientation: "
                        + "; ".join(selection_errors[:30])
                    )
            if candidate_arena:
                record = {
                    "story_id": story_id,
                    "plan_candidate_id": candidate_id,
                    "candidate_rank": candidate_options["candidate_rank"],
                    "candidate_count": candidate_options[
                        "candidate_count"
                    ],
                    "context_file": str(context_path.resolve()),
                    "output": str(output_path.resolve()),
                    "orientation_resolution": orientation_resolution,
                    "orientation_ambiguity_reasons": ambiguity_reasons,
                    "context_chars": context_chars,
                    "context_budget_chars": args.max_context_chars,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": (
                                "story_first_story_plan_candidate_"
                                "orientation_local_v15"
                            ),
                            "strict": True,
                            "schema": response_schema,
                        },
                    },
                }
                candidate_records.append(record)
                if synthetic_selection_written:
                    job_payload = {
                        **record,
                        "id": (
                            f"story-plan-candidate-{story_id}-{candidate_id}"
                        ),
                        "task": "story_plan_selection",
                        "stage_version": (
                            "story-first-story-plan-candidate-orientation-"
                            "local-v15"
                        ),
                        "synthetic_selection": True,
                    }
                    jobs.append(job_payload)
                else:
                    ambiguous_candidates.append(
                        {
                            "candidate_options": candidate_options,
                            "candidate_record": record,
                        }
                    )
            else:
                job_payload = {
                    "id": f"story-plan-selection-{story_id}",
                    "task": "story_plan_selection",
                    "stage_version": "story-first-story-plan-selection-v13",
                    "context_file": str(context_path.resolve()),
                    "output": str(output_path.resolve()),
                    "context_chars": context_chars,
                    "context_budget_chars": args.max_context_chars,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "story_first_story_plan_selection_v13",
                            "strict": True,
                            "schema": response_schema,
                        },
                    },
                    **(
                        {"synthetic_selection": True}
                        if synthetic_selection_written
                        else {}
                    ),
                    **(
                        {"media_file": finalist_proxy["comparison_path"]}
                        if finalist_proxy.get("status") == "ready"
                        else {}
                    ),
                }
                jobs.append(job_payload)
        if candidate_arena and ambiguous_candidates:
            fallback_candidates = [
                item["candidate_options"] for item in ambiguous_candidates
            ]
            fallback_context = {
                **context_base,
                "orientation_fallback_contract": {
                    "method": "story-level-ambiguous-candidate-orientation-v1",
                    "model_selects_candidate": False,
                    "candidate_count": len(fallback_candidates),
                    "candidates": [
                        {
                            "plan_candidate_id": candidate[
                                "plan_candidate_id"
                            ],
                            "candidate_rank": candidate["candidate_rank"],
                            "ambiguity_reasons": item["candidate_record"][
                                "orientation_ambiguity_reasons"
                            ],
                            "legal_option_contract": candidate,
                        }
                        for candidate, item in zip(
                            fallback_candidates, ambiguous_candidates
                        )
                    ],
                },
            }
            fallback_context_path = (
                context_dir / story_id / "orientation-fallback.json"
            )
            fallback_output_path = (
                output_dir / story_id / "orientation-fallback.json"
            )
            fallback_context_chars = write_context(
                fallback_context_path,
                fallback_context,
                args.max_context_chars,
            )
            plan_context_chars[f"{story_id}:orientation-fallback"] = (
                fallback_context_chars
            )
            fallback_schema = orientation_fallback_response_schema(
                story_id=story_id,
                production_slot=int(entry["production_slot"]),
                candidates=fallback_candidates,
            )
            fallback_job_id = f"story-plan-orientation-fallback-{story_id}"
            jobs.append(
                {
                    "id": fallback_job_id,
                    "task": "story_plan_orientation_fallback",
                    "stage_version": (
                        "story-first-story-plan-orientation-fallback-v15"
                    ),
                    "story_id": story_id,
                    "production_slot": int(entry["production_slot"]),
                    "context_file": str(fallback_context_path.resolve()),
                    "output": str(fallback_output_path.resolve()),
                    "context_chars": fallback_context_chars,
                    "context_budget_chars": args.max_context_chars,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": (
                                "story_first_story_plan_orientation_"
                                "fallback_v15"
                            ),
                            "strict": True,
                            "schema": fallback_schema,
                        },
                    },
                }
            )
            for item in ambiguous_candidates:
                item["candidate_record"].update(
                    {
                        "fallback_job_id": fallback_job_id,
                        "fallback_output": str(
                            fallback_output_path.resolve()
                        ),
                    }
                )
    manifest_path = job_root / "story-plan-batch.json"
    manifest = batch_payload(job_root, args.backend, jobs)
    manifest_fields = {
            "plan_generation_sha256": generation_sha256,
            "preflight_path": str(preflight_path),
            "status": (
                "blocked"
                if batching_blocked or not jobs
                else (
                    "partially_ready"
                    if blocked_preflights
                    else "ready"
                )
            ),
            "story_count": len(preflight_stories),
            "ready_story_count": (
                len(ready_prepared) if candidate_arena else len(jobs)
            ),
            "blocked_story_count": len(preflight_stories)
            - len(ready_prepared),
            "active_ready_plan_count": 0,
            "context_budget_chars": args.max_context_chars,
            "maximum_context_chars": max(
                plan_context_chars.values(), default=0
            ),
        }
    if candidate_arena:
        manifest_fields.update(
            {
                "candidate_arena": True,
                "candidate_count": len(candidate_records),
                "candidate_records": candidate_records,
                "orientation_resolution_method": (
                    "local-first-story-level-fallback-v1"
                ),
                "local_orientation_count": sum(
                    item["orientation_resolution"] == "local_deterministic"
                    for item in candidate_records
                ),
                "model_fallback_story_count": len(
                    {
                        item["story_id"]
                        for item in candidate_records
                        if item["orientation_resolution"]
                        == "story_model_fallback"
                    }
                ),
            }
        )
    manifest.update(manifest_fields)
    atomic_write_json(manifest_path, manifest)
    atomic_write_json(
        job_root / "story-plan-validation.json",
        {
            "ok": False,
            "status": "stale",
            **(
                {"validation_subject": "story_plan_candidates"}
                if candidate_arena
                else {}
            ),
            "plan_generation_sha256": generation_sha256,
            "active_ready_plan_count": 0,
            "errors": [
                "Story Plan generation changed; current batch has not been "
                "materialized and validated"
            ],
            "warnings": [],
        },
    )
    update_project_stage(
        job_root / "project.json",
        "story_plans",
        "stale",
        inputs={
            "story_plan_batch": str(manifest_path),
            "span_candidate_index": str(span_index_path),
        },
        outputs={},
        note=(
            "A new Story Plan generation was prepared; prior Plan files "
            f"are historical only. generation={generation_sha256}"
        ),
    )
    update_project_stage(
        job_root / "project.json",
        "story_plan_jobs",
        (
            "blocked"
            if batching_blocked or not jobs
            else (
                "partially_blocked"
                if blocked_preflights
                else "prepared"
            )
        ),
        inputs={
            "story_approval": str(approval_path),
            "span_candidate_index": str(span_index_path),
        },
        outputs={
            "batch_manifest": str(manifest_path),
            "preflight": str(preflight_path),
        },
        note=(
            "Legal Option Compiler prepared the current atomic Plan "
            f"generation; active_jobs={len(jobs)}; "
            f"generation={generation_sha256}"
        ),
    )
    if blocked_preflights:
        reasons = ", ".join(
            f"{item['story_id']}:{'/'.join(item['failure_codes'])}"
            f"->{item['repair_route']}"
            for item in blocked_preflights
        )
        if not args.allow_partial:
            raise ValueError(
                "Story Plan legal-option preflight blocked before Qwen; "
                "current batch was replaced with jobs=[]: " + reasons
            )
        print(
            "PREFLIGHT_PARTIAL\tblocked_stories="
            + reasons,
            flush=True,
        )
    if not jobs:
        if args.allow_partial:
            print(
                "PREFLIGHT_PARTIAL_NO_READY\tno preflight-ready Story to "
                "batch; current manifest contains jobs=[]",
                flush=True,
            )
            return manifest_path
        raise ValueError("no approved Story is available for Story Plan")
    return manifest_path