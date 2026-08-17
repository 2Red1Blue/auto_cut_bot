"""响应校验 — 从 semantic_handlers.py 提取的 response_validation 函数组。

原位置: semantic_handlers.py, 3 funcs, ~358L
依赖: story_schemas, vlm_analysis_contract, series_registry_*, series_assignment_contract
注: JobResponseValidation 类保留在 semantic_handlers.py，本模块惰性导入。
"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path
from typing import Any

from .assignment_contract import (
    canonicalize_series_assignment,
)
from .registry_admission import (
    compile_series_registry_admission,
)
from .registry_alias_repair import (
    canonicalize_series_registry_aliases,
)
from .registry_contract import (
    validate_series_registry_contract,
)
from .registry_reference_repair import (
    canonicalize_series_registry_references,
)
from autocut_core.schema.compat import (
    SCHEMAS,
    validate_schema,
    validate_task_response,
)
from autocut_core.semantic.vlm_analysis_contract import (
    canonicalize_vlm_analysis,
)

from .contracts import (
    AssignmentContractResult,
    RegistryRecoveryMergeResult,
    SeriesRegistryAdmissionResult,
    SeriesRegistryAliasRepairResult,
    SeriesRegistryIdentityRepairResult,
    SeriesRegistryReferenceRepairResult,
    SeriesRegistryRelationshipRepairResult,
    WindowAnalysisContractResult,
)

# 延迟导入 — 避免循环依赖
_entry_symbol = None
_direct_evidence_contract_indexes = None
_story_script_preflight_admission = None
_JobResponseValidation = None


def _get_entry_symbol(name: str) -> Any:
    global _entry_symbol
    if _entry_symbol is None:
        from autocut_core.semantic.batch_runner import _entry_symbol as _es
        _entry_symbol = _es
    return _entry_symbol(name)


def _get_direct_evidence_contract_indexes():
    global _direct_evidence_contract_indexes
    if _direct_evidence_contract_indexes is None:
        from autocut_core.semantic.registry import _direct_evidence_contract_indexes as _fn
        _direct_evidence_contract_indexes = _fn
    return _direct_evidence_contract_indexes


def _get_story_script_preflight_admission():
    global _story_script_preflight_admission
    if _story_script_preflight_admission is None:
        from autocut_core.semantic.story_logic import story_script_preflight_admission as _fn
        _story_script_preflight_admission = _fn
    return _story_script_preflight_admission


def _get_JobResponseValidation():
    global _JobResponseValidation
    if _JobResponseValidation is None:
        from autocut_core.semantic.types import JobResponseValidation as _cls
        _JobResponseValidation = _cls
    return _JobResponseValidation


# ── Event-ID truncation repair ─────────────────────────────────────
# Some models (Doubao in particular) systematically drop the last hex
# character of event-IDs, producing 17-char strings instead of the
# correct 18-char form.  If the truncated prefix uniquely matches a
# valid ID from the context we repair it deterministically before
# schema validation so that the enum/pattern checks pass.

_EVENT_ID_VALID = re.compile(r"^event-[0-9a-f]{12}$")
_EVENT_ID_TRUNCATED = re.compile(r"^event-[0-9a-f]{11}$")


def _build_event_id_prefix_map(valid_ids: set[str]) -> dict[str, str]:
    """Map 17-char truncated prefix → unique 18-char valid ID.

    Ambiguous prefixes (collisions) are excluded — we never guess when
    there is more than one candidate.
    """
    mapping: dict[str, str] = {}
    collisions: set[str] = set()
    for eid in valid_ids:
        if _EVENT_ID_VALID.match(eid):
            prefix = eid[:17]
            if prefix in mapping:
                collisions.add(prefix)
            else:
                mapping[prefix] = eid
    for p in collisions:
        mapping.pop(p, None)
    return mapping


def _repair_truncated_ids_in_value(
    obj: Any,
    prefix_map: dict[str, str],
    repairs: list[dict[str, Any]],
    path: str = "$",
) -> Any:
    """Recursively walk *obj*, replacing truncated event-IDs in-place."""
    if isinstance(obj, str):
        if _EVENT_ID_TRUNCATED.match(obj) and obj in prefix_map:
            repaired = prefix_map[obj]
            repairs.append({
                "type": "truncated_event_id",
                "path": path,
                "original": obj,
                "repaired": repaired,
            })
            return repaired
        return obj
    if isinstance(obj, dict):
        return {
            k: _repair_truncated_ids_in_value(
                v, prefix_map, repairs, f"{path}.{k}"
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [
            _repair_truncated_ids_in_value(
                v, prefix_map, repairs, f"{path}[{i}]"
            )
            for i, v in enumerate(obj)
        ]
    return obj



def compaction_retry_projection(
    invalid_value: dict[str, Any] | None,
    compaction_beat_ids: list[str] | tuple[str, ...] | None,
    *,
    context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Project only direct evidence needed to repair physically wide Beats."""

    if not isinstance(invalid_value, dict):
        return []
    compaction_ids = {
        item
        for item in compaction_beat_ids or []
        if isinstance(item, str) and item
    }
    if not compaction_ids:
        return []
    (
        _,
        allowed_thread_beats_by_event,
        event_contract_by_id,
        candidate_contract_by_id,
    ) = _get_direct_evidence_contract_indexes()(context)
    compact_beats: list[dict[str, Any]] = []
    beats = invalid_value.get("beats", [])
    if not isinstance(beats, list):
        return []
    for beat in beats:
        if not isinstance(beat, dict) or beat.get("id") not in compaction_ids:
            continue
        retrieval = beat.get("retrieval_requirements", {})
        if not isinstance(retrieval, dict):
            retrieval = {}
        event_ids = beat.get("event_ids", [])
        if not isinstance(event_ids, list):
            event_ids = []
        retrieval_event_ids = retrieval.get("event_ids", [])
        if not isinstance(retrieval_event_ids, list):
            retrieval_event_ids = []
        direct_event_ids = sorted(
            {
                item
                for item in [*event_ids, *retrieval_event_ids]
                if isinstance(item, str) and item
            }
        )
        must_show = beat.get("must_show", [])
        if not isinstance(must_show, list):
            must_show = []
        must_show_groups = []
        must_show_conjunctive_groups = []
        for item in must_show:
            if not isinstance(item, dict):
                continue
            evidence_event_ids = item.get("evidence_event_ids", [])
            if not isinstance(evidence_event_ids, list):
                evidence_event_ids = []
            must_show_groups.append(
                {
                    "id": item.get("id"),
                    "event_ids": [
                        event_id
                        for event_id in evidence_event_ids
                        if isinstance(event_id, str) and event_id
                    ][:8],
                }
            )
            must_show_conjunctive_groups.append(
                {
                    "id": item.get("id"),
                    "evidence_event_ids": [
                        event_id
                        for event_id in evidence_event_ids
                        if isinstance(event_id, str) and event_id
                    ][:8],
                    "evidence_fact_ids": [
                        fact_id
                        for fact_id in item.get(
                            "evidence_fact_ids", []
                        )
                        or []
                        if isinstance(fact_id, str) and fact_id
                    ][:8],
                    "coverage_semantics": "all_direct_events_required",
                }
            )
        candidate_suggestions = beat.get("candidate_suggestions", [])
        if not isinstance(candidate_suggestions, list):
            candidate_suggestions = []
        retrieval_candidate_ids = retrieval.get("candidate_ids", [])
        if not isinstance(retrieval_candidate_ids, list):
            retrieval_candidate_ids = []
        candidate_ids = sorted(
            {
                item
                for item in [
                    *candidate_suggestions,
                    *retrieval_candidate_ids,
                ]
                if isinstance(item, str) and item
            }
        )
        thread_beat_ids = sorted(
            {
                item
                for item in retrieval.get("thread_beat_ids", []) or []
                if isinstance(item, str) and item
            }
        )
        compact_beats.append(
            {
                "beat_id": beat.get("id"),
                "direct_event_ids": direct_event_ids[:16],
                "must_show_event_groups": must_show_groups[:8],
                "must_show_conjunctive_groups": (
                    must_show_conjunctive_groups[:8]
                ),
                "candidate_ids": candidate_ids[:8],
                "thread_beat_ids": thread_beat_ids,
                "allowed_thread_beat_ids_by_event": {
                    event_id: allowed_thread_beats_by_event.get(
                        event_id, []
                    )
                    for event_id in direct_event_ids[:16]
                },
                "event_physical_units": [
                    copy.deepcopy(event_contract_by_id[event_id])
                    for event_id in direct_event_ids[:16]
                    if event_id in event_contract_by_id
                ],
                "candidate_physical_units": [
                    copy.deepcopy(candidate_contract_by_id[candidate_id])
                    for candidate_id in candidate_ids[:8]
                    if candidate_id in candidate_contract_by_id
                ],
            }
        )
    return compact_beats


# ── story_catalog canonicalize ─────────────────────────────────────
# Doubao (and some other models) frequently output story_catalog with:
#   1. Flat structure (story fields at root instead of inside stories[])
#   2. Wrong field names (story_title→title, central_characters→character_ids, etc.)
#   3. Missing wrapper fields (schema_version, story_granularity)
# This repair layer maps approximate outputs to the expected schema before
# strict validation, following the same pattern as canonicalize_vlm_analysis.

_STORY_FIELD_ALIASES: dict[str, str] = {
    # LLM output name → expected schema name
    "story_title": "title",
    "title": "title",
    "story_summary": "logline",
    "synopsis": "logline",
    "premise": "logline",
    "summary": "logline",
    "story_synopsis": "logline",
    "central_conflict": "central_question",
    "core_conflict": "central_question",
    "center_conflict": "central_question",
    "central_characters": "character_ids",
    "center_characters": "character_ids",
    "central_character": "character_ids",
    "center_character": "character_ids",
    "central_character_ids": "character_ids",
    "center_character_ids": "character_ids",
    "local_payoff": "payoff_summary",
    "story_arc": "start_state",
    "story_progression": "start_state",
    "narrative_arc": "start_state",
    "narrative_development": "start_state",
    "plot_development": "start_state",
    "necessary_background": "end_state",
    "required_background": "end_state",
    "hook_potential": "open_hook_summary",
    "same_thread_hook_potential": "open_hook_summary",
    "same_line_hook_potential": "open_hook_summary",
    "sequel_hook_potential": "open_hook_summary",
    "cross_story_hook_potential": "open_hook_summary",
    "series_hook_potential": "open_hook_summary",
    "followup_hook_potential": "open_hook_summary",
    "serial_hook_potential": "open_hook_summary",
    "hook_possibilities": "open_hook_summary",
    "hook_possibility": "open_hook_summary",
    "same_line_hook_possibility": "open_hook_summary",
    "same_line_hook": "open_hook_summary",
    "same_thread_hook_possibility": "open_hook_summary",
    "open_hook": "open_hook_summary",
}

# Default values for fields the LLM rarely outputs but are required by schema.
# These are safe defaults that pass validation; downstream stages will refine.
_DEFAULT_SCORES = {
    "story_completeness": 5,
    "independent_clarity": 5,
    "highlight_relevance": 5,
    "source_sufficiency": 5,
    "causal_clarity": 5,
    "hook_alignment": 5,
    "background_cost": 5,
}

# Fields that LLM sometimes outputs but are NOT in the schema — drop silently
_STORY_EXTRA_FIELDS = frozenset({
    "episode_ids", "phases", "option_type",
    "required_thread_beat_ids", "non_coda_thread_beat_ids",
    "local_payoff_beat_ids",
    "central_character_id",  # singular variant; character_ids is the canonical field
})


# Fields whose values are fully determined by upstream code and injected
# after LLM generation.  Kept in sync with granularity._SYSTEM_INJECTED_STORY_FIELDS.
_SYSTEM_INJECTED_FIELDS = frozenset({
    "story_id",
    "subarc_option_id",
    "story_thread_ids",
    "source_thread_beat_ids",
    "subarc_start_beat_id",
    "subarc_end_beat_id",
    "required_bridge_beat_ids",
    "evidence_event_ids",
    "estimated_source_seconds",
    "duration_feasibility",
    "character_ids",
    "relationship_ids",
    "required_fact_ids",
})

# Valid score sub-field names — extra keys from LLM are stripped
_VALID_SCORE_KEYS = frozenset(_DEFAULT_SCORES.keys())


def _canonicalize_story_object(
    story: dict[str, Any],
    *,
    option: dict[str, Any] | None = None,
    job_story_id: str | None = None,
    job_option_id: str | None = None,
    bible: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Canonicalize a single story object.

    Two-phase repair:
      Phase A — Map LLM field names to canonical names (FREE fields only).
      Phase B — Inject all deterministic fields from option/job (CONST/ENUM).
    """
    # ── Phase A: alias-map FREE fields from LLM output ──
    free: dict[str, Any] = {}
    for key, value in story.items():
        if key in _STORY_EXTRA_FIELDS:
            continue
        mapped = _STORY_FIELD_ALIASES.get(key, key)
        # Only keep FREE fields; system-injected fields are overwritten below
        if mapped not in _SYSTEM_INJECTED_FIELDS and mapped not in free:
            free[mapped] = value

    # Type-fix: start_state / end_state may be dicts
    for sf in ("start_state", "end_state"):
        val = free.get(sf)
        if isinstance(val, dict):
            parts = [str(v) for v in val.values() if isinstance(v, str)]
            free[sf] = " ".join(parts)[:500] if parts else ""
        elif not isinstance(val, str):
            free[sf] = ""

    # Fill missing FREE fields with safe defaults
    if not free.get("title"):
        free["title"] = (free.get("central_question") or "")[:80] or "Untitled Story"
    if not free.get("logline"):
        for fb in ("payoff_summary", "start_state", "central_question"):
            v = free.get(fb)
            if isinstance(v, str) and len(v) > 10:
                free["logline"] = v[:200]
                break
        else:
            free["logline"] = "Story generated from subarc option"
    if not free.get("central_question"):
        free["central_question"] = free.get("title", "What happens?")
    if not free.get("start_state"):
        free["start_state"] = free.get("end_state") or "Setup"
    if not free.get("end_state"):
        free["end_state"] = free.get("start_state") or "Resolution"
    if not free.get("payoff_summary"):
        free["payoff_summary"] = free.get("logline", "See logline")
    # Coerce all string fields that LLM may output as list/dict
    for str_field in ("title", "logline", "central_question", "start_state",
                      "end_state", "payoff_summary", "open_hook_summary",
                      "overlap_notes", "recommendation_reason"):
        val = free.get(str_field)
        if isinstance(val, list):
            free[str_field] = "; ".join(str(v) for v in val if v)[:500]
        elif isinstance(val, dict):
            parts = [str(v) for v in val.values() if isinstance(v, str)]
            free[str_field] = " ".join(parts)[:500] if parts else ""
        elif not isinstance(val, str):
            free[str_field] = str(val) if val else ""

    # Scores: strip extra keys, coerce to int [1,10], fill missing with defaults
    raw_scores = free.get("scores")
    if isinstance(raw_scores, dict):
        scores: dict[str, int] = {}
        for k in _VALID_SCORE_KEYS:
            if k in raw_scores:
                try:
                    v = int(round(float(raw_scores[k])))
                    scores[k] = max(1, min(10, v))
                except (TypeError, ValueError):
                    scores[k] = _DEFAULT_SCORES[k]
            else:
                scores[k] = _DEFAULT_SCORES[k]
    else:
        scores = dict(_DEFAULT_SCORES)
    free["scores"] = scores

    # recommendation_reason
    if not free.get("recommendation_reason"):
        n_events = len(option.get("evidence_event_ids", [])) if option else 0
        free["recommendation_reason"] = (
            f"Evidence-backed subarc with {n_events} events"
        )

    # Filter suggested candidate IDs to valid enum sets from option
    if option:
        valid_hl = set(option.get("highlight_candidate_ids", []))
        valid_hk = set(option.get("hook_candidate_ids", []))
        hl = free.get("suggested_highlight_candidate_ids", [])
        hk = free.get("suggested_hook_candidate_ids", [])
        if isinstance(hl, list) and valid_hl:
            free["suggested_highlight_candidate_ids"] = [
                x for x in hl if isinstance(x, str) and x in valid_hl
            ]
        if isinstance(hk, list) and valid_hk:
            free["suggested_hook_candidate_ids"] = [
                x for x in hk if isinstance(x, str) and x in valid_hk
            ]

    # ── Phase B: inject all deterministic fields ──
    result: dict[str, Any] = {}
    result.update(free)

    # CONST fields from option / job
    result["story_id"] = job_story_id or story.get("story_id", "")
    result["subarc_option_id"] = job_option_id or story.get("subarc_option_id", "")
    if option:
        result["story_thread_ids"] = list(option.get("story_thread_ids", []))
        result["source_thread_beat_ids"] = list(option.get("source_thread_beat_ids", []))
        result["subarc_start_beat_id"] = option.get("subarc_start_beat_id", "")
        result["subarc_end_beat_id"] = option.get("subarc_end_beat_id", "")
        result["required_bridge_beat_ids"] = list(option.get("required_bridge_beat_ids", []))
        result["evidence_event_ids"] = list(option.get("evidence_event_ids", []))
        result["estimated_source_seconds"] = option.get("estimated_source_seconds", 0)
        result["duration_feasibility"] = option.get("duration_feasibility", "viable")
        result["required_fact_ids"] = list(option.get("required_fact_ids", []))

        # Compute character_ids / relationship_ids from bible + option
        if bible:
            from autocut_core.semantic.granularity import (
                compute_option_character_ids,
                compute_option_relationship_ids,
            )
            thread_by_id = {
                t["id"]: t
                for t in (bible.get("story_threads") or [])
                if isinstance(t, dict) and isinstance(t.get("id"), str)
            }
            relationships = [
                r for r in (bible.get("relationships") or [])
                if isinstance(r, dict) and isinstance(r.get("id"), str)
            ]
            char_ids = compute_option_character_ids(option, thread_by_id)
            rel_ids = compute_option_relationship_ids(char_ids, relationships)
            result["character_ids"] = char_ids or ["char-unknown"]
            result["relationship_ids"] = rel_ids
        else:
            result["character_ids"] = story.get("character_ids", []) or ["char-unknown"]
            result["relationship_ids"] = story.get("relationship_ids", [])
    else:
        # No option available — keep whatever LLM produced (will fail downstream)
        for f in ("story_thread_ids", "source_thread_beat_ids",
                   "required_bridge_beat_ids", "evidence_event_ids",
                   "required_fact_ids"):
            if f not in result:
                result[f] = []
        for f in ("subarc_start_beat_id", "subarc_end_beat_id"):
            if f not in result:
                result[f] = ""
        if "estimated_source_seconds" not in result:
            result["estimated_source_seconds"] = 0
        if "duration_feasibility" not in result:
            result["duration_feasibility"] = "viable"
        if "character_ids" not in result or not result["character_ids"]:
            result["character_ids"] = ["char-unknown"]
        if "relationship_ids" not in result:
            result["relationship_ids"] = []

    # Ensure character_ids is a non-empty list
    if not isinstance(result.get("character_ids"), list) or not result["character_ids"]:
        result["character_ids"] = ["char-unknown"]

    return result


def _expected_catalog_sha256(job: dict[str, Any] | None) -> str:
    """Extract expected subarc_option_catalog_sha256 from per-job schema const."""
    if not isinstance(job, dict):
        return ""
    try:
        rf = job.get("response_format", {})
        schema = rf.get("json_schema", {}).get("schema", {})
        props = schema.get("properties", {})
        sha_prop = props.get("subarc_option_catalog_sha256", {})
        return sha_prop.get("const", "")
    except (TypeError, AttributeError):
        return ""


def canonicalize_story_catalog(
    value: dict[str, Any],
    *,
    job: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    response_format_value: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Canonicalize story_catalog LLM output before validation.

    The LLM only needs to produce ~10 narrative (FREE) fields.
    All deterministic (CONST/ENUM) fields are injected from the
    subarc option in context.  This eliminates the need for the LLM
    to reproduce long ID arrays verbatim.
    """
    if not isinstance(value, dict):
        return value

    job_story_id = job.get("story_id") if isinstance(job, dict) else None
    job_option_id = job.get("subarc_option_id") if isinstance(job, dict) else None

    # Extract option and bible from context
    option = None
    bible = None
    if isinstance(context, dict):
        options = context.get("subarc_options", [])
        if isinstance(options, list) and options:
            option = options[0] if isinstance(options[0], dict) else None
        bible = context.get("series_bible")

    result = dict(value)

    # ── Fix 1: flat output → wrap in stories[], strip non-wrapper keys ──
    _WRAPPER_KEYS = frozenset({"schema_version", "story_granularity",
                                "subarc_option_catalog_sha256", "stories"})
    if "stories" not in result:
        flat_markers = {"subarc_option_id", "central_conflict", "story_title",
                        "central_characters", "source_thread_beat_ids"}
        if flat_markers & set(result.keys()):
            story_fields = {k: v for k, v in result.items()
                           if k not in _WRAPPER_KEYS}
            result = {k: v for k, v in result.items() if k in _WRAPPER_KEYS}
            result["stories"] = [story_fields]

    # Strip any remaining non-wrapper keys at root (e.g. leaked story fields)
    extra_root_keys = [k for k in result if k not in _WRAPPER_KEYS]
    for k in extra_root_keys:
        del result[k]

    # ── Fix 2: ensure wrapper fields (overwrite — LLM may output wrong values) ──
    result["schema_version"] = "1.2"
    result["story_granularity"] = "broad"
    result["subarc_option_catalog_sha256"] = _expected_catalog_sha256(job) or result.get("subarc_option_catalog_sha256", "")

    # ── Fix 3: canonicalize each story ──
    stories = result.get("stories")
    if isinstance(stories, list):
        result["stories"] = [
            _canonicalize_story_object(
                s,
                option=option,
                job_story_id=job_story_id,
                job_option_id=job_option_id,
                bible=bible if isinstance(bible, dict) else None,
            ) if isinstance(s, dict) else s
            for s in stories
        ]

    return result


def validate_job_response(
    task: str,
    value: dict[str, Any],
    response_format_value: dict[str, Any],
) -> list[str]:
    custom_schema = response_format_value["json_schema"]["schema"]
    if custom_schema is not SCHEMAS[task]:
        # When a per-job custom schema is provided (e.g. story_catalog with
        # broad-mode fields like story_granularity / subarc_option_catalog_sha256,
        # or story_script_draft with locked teaser_contract.mode), validate
        # ONLY against the custom schema.  The custom schema is the one actually
        # sent to the LLM via response_format and is always a strict superset or
        # narrowing of the static schema.  Validating against both would produce
        # false "unknown properties" errors from the static schema for fields
        # that only exist in the custom variant.
        return validate_schema(value, custom_schema)
    return validate_task_response(task, value)


def validate_and_canonicalize_job_response(
    task: str,
    value: dict[str, Any],
    response_format_value: dict[str, Any],
    job: dict[str, Any],
    context: dict[str, Any],
) -> Any:  # returns JobResponseValidation
    """Run local admission repair, schema, identity, and semantic contracts."""

    JobResponseValidation = _get_JobResponseValidation()

    window_contract_result: WindowAnalysisContractResult | None = None
    registry_alias_repair_result: (
        SeriesRegistryAliasRepairResult | None
    ) = None
    registry_reference_repair_result: (
        SeriesRegistryReferenceRepairResult | None
    ) = None
    registry_admission_result: SeriesRegistryAdmissionResult | None = None
    effective_value = value
    event_id_truncation_repairs: list[dict[str, Any]] = []
    if task in ("vlm_analysis", "window_analysis"):
        window_contract_result = canonicalize_vlm_analysis(value, job=job)
        effective_value = window_contract_result.effective_window

    # ── Pre-schema repair: canonicalize story_catalog ────────────
    # Doubao frequently outputs wrong field names, flat structure,
    # or hallucinated IDs.  Map to expected schema and repair IDs
    # against per-job enum constraints before strict validation.
    if task == "story_catalog":
        effective_value = canonicalize_story_catalog(
            effective_value,
            job=job,
            context=context,
            response_format_value=response_format_value,
        )

    # ── Pre-schema repair: fix truncated event-IDs ──────────────
    # Some models truncate the last hex character of event-IDs (18→17 chars).
    # Before strict enum/pattern validation, attempt deterministic repair.
    raw_event_index = context.get("event_index") if isinstance(context, dict) else None
    if isinstance(raw_event_index, list):
        valid_event_ids = {
            item.get("id")
            for item in raw_event_index
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        if valid_event_ids:
            _prefix_map = _build_event_id_prefix_map(valid_event_ids)
            if _prefix_map:
                effective_value = _repair_truncated_ids_in_value(
                    effective_value, _prefix_map, event_id_truncation_repairs
                )

    schema_errors = _get_entry_symbol("validate_job_response")(
        task, effective_value, response_format_value
    )
    identity_errors = _get_entry_symbol("validate_identity")(
        task, effective_value, job, context
    )
    contract_errors: list[str] = []
    story_script_admission = None
    if window_contract_result is not None:
        contract_errors.extend(window_contract_result.errors)
    if (
        not schema_errors and task == "story_script_draft"
    ):
        story_script_admission = _get_story_script_preflight_admission()(
            effective_value,
            context,
        )
        contract_errors.extend(story_script_admission.errors)
    if not schema_errors and not identity_errors and task == "series_registry":
        raw_event_index = context.get("event_index")
        known_event_ids = (
            {
                item.get("id")
                for item in raw_event_index
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            if isinstance(raw_event_index, list)
            else None
        )
        alias_result = canonicalize_series_registry_aliases(effective_value)
        if alias_result.repairs:
            registry_alias_repair_result = alias_result
            effective_value = alias_result.effective_registry
            schema_errors = _get_entry_symbol("validate_job_response")(
                task,
                effective_value,
                response_format_value,
            )
            identity_errors = _get_entry_symbol("validate_identity")(
                task,
                effective_value,
                job,
                context,
            )
        if not schema_errors and not identity_errors:
            reference_result = canonicalize_series_registry_references(
                effective_value,
                known_event_ids=known_event_ids,
            )
            if reference_result.repairs:
                registry_reference_repair_result = reference_result
                effective_value = reference_result.effective_registry
                schema_errors = _get_entry_symbol("validate_job_response")(
                    task,
                    effective_value,
                    response_format_value,
                )
                identity_errors = _get_entry_symbol("validate_identity")(
                    task,
                    effective_value,
                    job,
                    context,
                )
        if not schema_errors and not identity_errors:
            contract_errors = validate_series_registry_contract(
                effective_value,
                known_event_ids=known_event_ids,
                event_index=(
                    raw_event_index
                    if isinstance(raw_event_index, list)
                    else None
                ),
            ).errors
        if not schema_errors and not identity_errors and not contract_errors:
            registry_admission_result = compile_series_registry_admission(
                effective_value,
                event_index=(
                    [
                        item
                        for item in raw_event_index
                        if isinstance(item, dict)
                    ]
                    if isinstance(raw_event_index, list)
                    else []
                ),
            )
            if registry_admission_result.ok:
                effective_value = registry_admission_result.effective_registry
                schema_errors = _get_entry_symbol("validate_job_response")(
                    task,
                    effective_value,
                    response_format_value,
                )
                identity_errors = _get_entry_symbol("validate_identity")(
                    task,
                    effective_value,
                    job,
                    context,
                )
            else:
                contract_errors = list(registry_admission_result.errors)
    if (
        schema_errors
        or identity_errors
        or contract_errors
        or task != "series_assignment"
    ):
        return JobResponseValidation(
            value=effective_value,
            schema_errors=schema_errors,
            identity_errors=identity_errors,
            contract_errors=contract_errors,
            window_contract_result=window_contract_result,
            registry_alias_repair_result=registry_alias_repair_result,
            registry_reference_repair_result=(
                registry_reference_repair_result
            ),
            registry_admission_result=registry_admission_result,
            story_script_admission=story_script_admission,
                event_id_truncation_repairs=event_id_truncation_repairs,
        )

    event_by_id = {
        item["id"]: item
        for item in context.get("event_index", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    registry_thread_ids = {
        item["id"]
        for item in context.get("series_registry", {}).get("story_threads", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    registry_thread_kinds = {
        item["id"]: str(item.get("thread_kind") or "")
        for item in context.get("series_registry", {}).get("story_threads", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    registry_admission_context = context.get("series_registry_admission", {})
    if not isinstance(registry_admission_context, dict):
        registry_admission_context = {}
    result = canonicalize_series_assignment(
        effective_value,
        context_episodes=context.get("episodes", []),
        event_by_id=event_by_id,
        registry_thread_ids=registry_thread_ids,
        registry_thread_kinds=registry_thread_kinds,
        repair_redundant_exclusions=True,
        registry_quarantined_event_ids={
            item
            for item in registry_admission_context.get(
                "quarantined_event_ids", []
            )
            if isinstance(item, str)
        },
        registry_quarantine_sha256=registry_admission_context.get(
            "quarantine_sha256"
        ),
        registry_language=str(
            context.get("series_registry", {}).get("language") or "zh"
        ),
        repair_quarantined_episodes=True,
    )
    if result.errors:
        return JobResponseValidation(
            value=effective_value,
            schema_errors=schema_errors,
            identity_errors=identity_errors,
            contract_errors=result.errors,
            contract_result=result,
            window_contract_result=window_contract_result,
                event_id_truncation_repairs=event_id_truncation_repairs,
        )

    # A canonicalized response must still satisfy both static and per-job
    # dynamic schemas.  This should be a no-op for deletion-only repairs and
    # guards future contract changes from emitting an invalid output.
    effective_schema_errors = _get_entry_symbol("validate_job_response")(
        task, result.effective_assignment, response_format_value
    )
    effective_identity_errors = _get_entry_symbol("validate_identity")(
        task, result.effective_assignment, job, context
    )
    return JobResponseValidation(
        value=result.effective_assignment,
        schema_errors=effective_schema_errors,
        identity_errors=effective_identity_errors,
        contract_errors=[],
        contract_result=result,
        window_contract_result=window_contract_result,
            event_id_truncation_repairs=event_id_truncation_repairs,
    )