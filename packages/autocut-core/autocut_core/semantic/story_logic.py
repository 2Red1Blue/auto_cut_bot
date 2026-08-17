"""故事逻辑 — 从 semantic_handlers.py 提取的 retry_logic + story_content 联合函数组。

原位置: semantic_handlers.py, 25 funcs, ~1618L
子模块: treatment分支, retry投影, story_script compile修复, preflight, treatment审计
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


from autocut_core.libs.script_preflight import preflight_script
from autocut_core.semantic.engine import _sanitize_path_component, ERROR_KIND_SCHEMA, ERROR_KIND_SEMANTIC_CONTRACT
from autocut_core.io import atomic_write_json, json_sha256, load_json, utc_now

from autocut_core.semantic.granularity import BROAD
from autocut_core.libs.editorial_plan import validate_option_selection
from autocut_core.schema.compat import (
    SCHEMAS,
    STORY_SCRIPT_HIGHLIGHT_SELECTION_PROMPT_VERSION,
    response_format,
    task_prompt,
    validate_schema,
    validate_task_response,
)
from autocut_core.contracts.teaser_contract import (
    TEASER_MAXIMUM_SECONDS,
    TEASER_PREFERRED_MINIMUM_SECONDS,
)

from .contracts import WindowAnalysisContractResult

# 从 autocut_core.semantic.utils 导入
from autocut_core.semantic.utils import records_by_id as _records_by_id

# 延迟导入 — 避免循环依赖
_direct_evidence_contract_indexes = None
_entry_symbol = None
_compaction_retry_projection = None
_JobResponseValidation = None
_StoryScriptAdmissionResult = None
_StoryScriptCompileReplacementResult = None

# 常量 — 从 semantic_handlers.py 直接复制, 避免循环 import
TREATMENT_RETRY_POLICY_VERSION = (
    "story-script-treatment-retry-v5-contract-checklists"
)
STORY_SCRIPT_COMPILE_REPAIR_RESPONSE_SCHEMA_VERSION = "1.0"
STORY_SCRIPT_COMPILE_REPAIR_FAILURE_CODES = frozenset(
    {
        "beat_physical_compaction_required",
        "beat_event_thread_beat_mismatch",
    }
)
STORY_SCRIPT_COMPILE_REPAIR_PROCESS_CODES = frozenset(
    {
        *STORY_SCRIPT_COMPILE_REPAIR_FAILURE_CODES,
        "story_script_compile_preservation_violation",
        "story_script_compile_replacement_invalid",
    }
)
STORY_SCRIPT_ORDINARY_SEMANTIC_RETRY_LIMIT = 1
STORY_SCRIPT_COMPILE_REPAIR_LIMIT = 2
TREATMENT_RETRY_IN_PLACE_FAILURE_CODES = frozenset(
    {
        "chronological_opening_not_mainline",
        "chronological_teaser_forbidden",
        "cold_open_definition_invalid",
        "delayed_reprise_beat_missing",
        "delayed_reprise_before_explanation",
        "delayed_reprise_progression_missing",
        "no_reprise_declares_reprise",
        "opening_explanation_is_teaser",
        "opening_explanation_missing",
        "teaser_delayed_reprise_missing",
        "teaser_must_show_outside_stitch_window",
        "treatment_unknown_beat_ids",
    }
)


def _get_direct_evidence_contract_indexes():
    global _direct_evidence_contract_indexes
    if _direct_evidence_contract_indexes is None:
        from autocut_core.semantic.registry import _direct_evidence_contract_indexes as _fn
        _direct_evidence_contract_indexes = _fn
    return _direct_evidence_contract_indexes


def _get_entry_symbol(name: str) -> Any:
    global _entry_symbol
    if _entry_symbol is None:
        from autocut_core.semantic.batch_runner import _entry_symbol as _es
        _entry_symbol = _es
    return _entry_symbol(name)


def _get_compaction_retry_projection():
    global _compaction_retry_projection
    if _compaction_retry_projection is None:
        from autocut_core.semantic.response_validation import compaction_retry_projection as _fn
        _compaction_retry_projection = _fn
    return _compaction_retry_projection


def _get_JobResponseValidation():
    global _JobResponseValidation
    if _JobResponseValidation is None:
        from autocut_core.semantic.types import JobResponseValidation as _cls
        _JobResponseValidation = _cls
    return _JobResponseValidation


def _get_StoryScriptAdmissionResult():
    global _StoryScriptAdmissionResult
    if _StoryScriptAdmissionResult is None:
        from autocut_core.semantic.types import StoryScriptAdmissionResult as _cls
        _StoryScriptAdmissionResult = _cls
    return _StoryScriptAdmissionResult


def _get_StoryScriptCompileReplacementResult():
    global _StoryScriptCompileReplacementResult
    if _StoryScriptCompileReplacementResult is None:
        from autocut_core.semantic.types import StoryScriptCompileReplacementResult as _cls
        _StoryScriptCompileReplacementResult = _cls
    return _StoryScriptCompileReplacementResult



# === _treatment_schema_branches (L168-L182) ===
def _treatment_schema_branches(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    try:
        treatment_schema = payload["response_format"]["json_schema"][
            "schema"
        ]["properties"]["teaser_contract"]
    except (KeyError, TypeError):
        return []
    if not isinstance(treatment_schema, dict):
        return []
    branches = treatment_schema.get("anyOf")
    if isinstance(branches, list):
        return [item for item in branches if isinstance(item, dict)]
    return [treatment_schema]


# === _treatment_branch_value (L185-L189) ===
def _treatment_branch_value(
    branch: dict[str, Any], field: str
) -> str:
    value = branch.get("properties", {}).get(field, {}).get("const")
    return value if isinstance(value, str) else ""


# === treatment_retry_decision (L192-L302) ===
def treatment_retry_decision(
    payload: dict[str, Any],
    *,
    treatment_viability: dict[str, Any] | None,
    treatment_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Choose one executable Treatment branch for a semantic retry.

    Structural mistakes that can be fixed without changing the compiled
    Treatment keep the selected Option.  Proven Treatment infeasibility must
    move to an untried compiled alternate, preferring the preflight's explicit
    recommendation and using chronological only as the final fallback.
    """
    viability = treatment_viability or {}
    failure_codes = {
        str(item)
        for item in viability.get("failure_codes", []) or []
        if isinstance(item, str) and item
    }
    if not failure_codes:
        return None
    branches = _treatment_schema_branches(payload)
    branch_by_id = {
        _treatment_branch_value(branch, "treatment_option_id"): branch
        for branch in branches
        if _treatment_branch_value(branch, "treatment_option_id")
    }
    if not branch_by_id:
        return None
    selected_id = str(
        viability.get("selected_treatment_option_id") or ""
    )
    attempted_ids = {
        str(item.get("treatment_option_id") or "")
        for item in treatment_attempts or []
        if isinstance(item, dict)
        and item.get("verdict") == "rejected"
        and item.get("treatment_option_id")
    }
    selected_rejection_count = sum(
        1
        for item in treatment_attempts or []
        if isinstance(item, dict)
        and item.get("verdict") == "rejected"
        and item.get("treatment_option_id") == selected_id
    )
    if (
        selected_id in branch_by_id
        and failure_codes <= TREATMENT_RETRY_IN_PLACE_FAILURE_CODES
        and selected_rejection_count <= 1
    ):
        return {
            "mode": "repair_current",
            "target_treatment_option_id": selected_id,
            "failed_treatment_option_ids": sorted(
                attempted_ids - {selected_id}
            ),
            "failure_codes": sorted(failure_codes),
            "branch": branch_by_id[selected_id],
        }

    ordered_candidates: list[str] = []

    def add_candidate(value: Any) -> None:
        if (
            isinstance(value, str)
            and value
            and value not in ordered_candidates
        ):
            ordered_candidates.append(value)

    add_candidate(
        viability.get("recommended_alternate_treatment_option_id")
    )
    for value in viability.get("alternate_treatment_option_ids", []) or []:
        add_candidate(value)
    add_candidate(
        viability.get("chronological_fallback_treatment_option_id")
    )
    for option_id, branch in branch_by_id.items():
        if (
            _treatment_branch_value(branch, "strategy")
            == "chronological_compression"
        ):
            add_candidate(option_id)
    for option_id in sorted(branch_by_id):
        add_candidate(option_id)
    target_id = next(
        (
            option_id
            for option_id in ordered_candidates
            if option_id in branch_by_id
            and option_id != selected_id
            and option_id not in attempted_ids
        ),
        "",
    )
    if not target_id:
        raise ValueError(
            "Treatment semantic retry has no untried compiled Option; "
            f"failed={sorted(attempted_ids | {selected_id})}"
        )
    return {
        "mode": "alternate",
        "target_treatment_option_id": target_id,
        "failed_treatment_option_ids": sorted(
            attempted_ids | ({selected_id} if selected_id else set())
        ),
        "failure_codes": sorted(failure_codes),
        "branch": branch_by_id[target_id],
    }


# === _lock_treatment_option_branch (L305-L322) ===
def _lock_treatment_option_branch(
    payload: dict[str, Any],
    treatment_option_id: str,
) -> bool:
    """Narrow a retry schema to one already compiled Treatment Option."""

    if not treatment_option_id:
        return False
    for branch in _treatment_schema_branches(payload):
        if (
            _treatment_branch_value(branch, "treatment_option_id")
            == treatment_option_id
        ):
            payload["response_format"]["json_schema"]["schema"][
                "properties"
            ]["teaser_contract"] = copy.deepcopy(branch)
            return True
    return False


# === mismatch_retry_projection (L439-L508) ===
def mismatch_retry_projection(
    invalid_value: dict[str, Any] | None,
    context: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Describe illegal Event/Thread Beat bindings without rewriting them."""

    if not isinstance(invalid_value, dict):
        return []
    (
        allowed_events_by_thread_beat,
        allowed_thread_beats_by_event,
        event_contract_by_id,
        _,
    ) = _get_direct_evidence_contract_indexes()(context)
    projections: list[dict[str, Any]] = []
    for beat_index, beat in enumerate(invalid_value.get("beats", []) or []):
        if not isinstance(beat, dict):
            continue
        retrieval = beat.get("retrieval_requirements", {})
        if not isinstance(retrieval, dict):
            retrieval = {}
        current_thread_beat_ids = sorted(
            {
                item
                for item in retrieval.get("thread_beat_ids", []) or []
                if isinstance(item, str) and item
            }
        )
        if not current_thread_beat_ids:
            continue
        allowed_event_ids = {
            event_id
            for thread_beat_id in current_thread_beat_ids
            for event_id in allowed_events_by_thread_beat.get(
                thread_beat_id, []
            )
        }
        mismatched_event_ids = sorted(
            set(_beat_direct_event_ids(beat)) - allowed_event_ids
        )
        if not mismatched_event_ids:
            continue
        involved_event_ids = sorted(
            set(_beat_direct_event_ids(beat)) | allowed_event_ids
        )
        projections.append(
            {
                "beat_id": str(
                    beat.get("id") or f"beats[{beat_index}]"
                ),
                "current_thread_beat_ids": current_thread_beat_ids,
                "mismatched_event_ids": mismatched_event_ids,
                "allowed_thread_beat_ids_by_event": {
                    event_id: allowed_thread_beats_by_event.get(event_id, [])
                    for event_id in mismatched_event_ids
                },
                "allowed_direct_event_ids_by_thread_beat": {
                    thread_beat_id: allowed_events_by_thread_beat.get(
                        thread_beat_id, []
                    )
                    for thread_beat_id in current_thread_beat_ids
                },
                "event_physical_units": [
                    copy.deepcopy(event_contract_by_id[event_id])
                    for event_id in involved_event_ids
                    if event_id in event_contract_by_id
                ],
            }
        )
    return projections


# === story_script_preservation_contract (L511-L584) ===
def story_script_preservation_contract(
    value: dict[str, Any],
) -> dict[str, Any]:
    """Project the quality obligations a compile-only rewrite must retain."""

    teaser = value.get("teaser_contract", {})
    if not isinstance(teaser, dict):
        teaser = {}
    must_show: dict[str, dict[str, set[str]]] = {}
    for beat in value.get("beats", []) or []:
        if not isinstance(beat, dict):
            continue
        for item in beat.get("must_show", []) or []:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            record = must_show.setdefault(
                item["id"],
                {"evidence_event_ids": set(), "evidence_fact_ids": set()},
            )
            record["evidence_event_ids"].update(
                event_id
                for event_id in item.get("evidence_event_ids", []) or []
                if isinstance(event_id, str) and event_id
            )
            record["evidence_fact_ids"].update(
                fact_id
                for fact_id in item.get("evidence_fact_ids", []) or []
                if isinstance(fact_id, str) and fact_id
            )
    return {
        "schema_version": "1.0",
        "treatment_identity": {
            key: teaser.get(key)
            for key in (
                "treatment_option_id",
                "strategy",
                "mode",
                "reprise_policy",
                "primary_highlight_candidate_id",
            )
        },
        "primary_story_thread_id": value.get("primary_story_thread_id"),
        "selected_thread_beat_ids": list(
            value.get("selected_thread_beat_ids", []) or []
        ),
        "required_thread_beat_ids": list(
            value.get("required_thread_beat_ids", []) or []
        ),
        "required_fact_ids": sorted(
            {
                item
                for item in value.get("required_fact_ids", []) or []
                if isinstance(item, str)
            }
        ),
        "intentional_mystery_fact_ids": sorted(
            {
                item
                for item in value.get("intentional_mystery_fact_ids", []) or []
                if isinstance(item, str)
            }
        ),
        "central_question": value.get("central_question"),
        "start_state": value.get("start_state"),
        "end_state": value.get("end_state"),
        "local_payoff": value.get("local_payoff"),
        "must_show_obligations": {
            must_show_id: {
                "evidence_event_ids": sorted(record["evidence_event_ids"]),
                "evidence_fact_ids": sorted(record["evidence_fact_ids"]),
            }
            for must_show_id, record in sorted(must_show.items())
        },
    }


# === validate_story_script_preservation (L587-L633) ===
def validate_story_script_preservation(
    contract: dict[str, Any],
    candidate: dict[str, Any],
) -> list[str]:
    """Reject quality loss while allowing Editorial Beat IDs to change."""

    errors: list[str] = []
    actual = story_script_preservation_contract(candidate)
    for field in (
        "treatment_identity",
        "primary_story_thread_id",
        "selected_thread_beat_ids",
        "required_thread_beat_ids",
        "required_fact_ids",
        "intentional_mystery_fact_ids",
        "central_question",
        "start_state",
        "end_state",
        "local_payoff",
    ):
        if actual.get(field) != contract.get(field):
            errors.append(
                "story_script_compile_preservation_violation: "
                f"{field} changed during compile-only rewrite"
            )
    actual_must_show = actual.get("must_show_obligations", {})
    for must_show_id, required in (
        contract.get("must_show_obligations", {}) or {}
    ).items():
        observed = actual_must_show.get(must_show_id)
        if not isinstance(observed, dict):
            errors.append(
                "story_script_compile_preservation_violation: "
                f"must_show {must_show_id} was removed"
            )
            continue
        for field in ("evidence_event_ids", "evidence_fact_ids"):
            missing = sorted(
                set(required.get(field, []) or [])
                - set(observed.get(field, []) or [])
            )
            if missing:
                errors.append(
                    "story_script_compile_preservation_violation: "
                    f"must_show {must_show_id}.{field} weakened; missing={missing}"
                )
    return errors


# === story_script_compile_failure_beat_ids (L645-L679) ===
def story_script_compile_failure_beat_ids(
    validation: JobResponseValidation,
    *,
    context: dict[str, Any],
    fallback_beat_ids: list[str] | tuple[str, ...] = (),
) -> list[str]:
    """Return failed Editorial Beat IDs in their current Script order."""

    admission = validation.story_script_admission
    failed = {
        item
        for item in (
            admission.split_regeneration_beat_ids
            if admission is not None
            else ()
        )
        if isinstance(item, str) and item
    }
    failed.update(
        str(item.get("beat_id"))
        for item in mismatch_retry_projection(validation.value, context)
        if isinstance(item.get("beat_id"), str) and item.get("beat_id")
    )
    if not failed:
        failed.update(
            item
            for item in fallback_beat_ids
            if isinstance(item, str) and item
        )
    ordered = [
        str(beat.get("id"))
        for beat in validation.value.get("beats", []) or []
        if isinstance(beat, dict) and beat.get("id") in failed
    ]
    return list(dict.fromkeys(ordered))


# === _story_script_compile_max_beats (L682-L691) ===
def _story_script_compile_max_beats(
    response_format_value: dict[str, Any],
) -> int:
    try:
        maximum = response_format_value["json_schema"]["schema"][
            "properties"
        ]["beats"]["maxItems"]
    except (KeyError, TypeError):
        return 14
    return maximum if isinstance(maximum, int) and maximum > 0 else 14


# === story_script_compile_repair_response_format (L694-L808) ===
def story_script_compile_repair_response_format(
    *,
    base_script: dict[str, Any],
    failed_beat_ids: list[str] | tuple[str, ...],
    response_format_value: dict[str, Any],
) -> dict[str, Any]:
    """Build a strict schema that exposes only failed Beat replacements."""

    try:
        beat_schema = copy.deepcopy(
            response_format_value["json_schema"]["schema"]["properties"][
                "beats"
            ]["items"]
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "Story Script response schema has no beats.items contract"
        ) from exc
    beats = [
        item
        for item in base_script.get("beats", []) or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    base_ids = [str(item["id"]) for item in beats]
    failed = list(
        dict.fromkeys(
            item
            for item in failed_beat_ids
            if isinstance(item, str) and item
        )
    )
    unknown = sorted(set(failed) - set(base_ids))
    if not failed or unknown:
        raise ValueError(
            "compile repair requires existing failed Beat IDs; "
            f"failed={failed}, unknown={unknown}"
        )
    ordered_failed = [item for item in base_ids if item in set(failed)]
    maximum_beats = _story_script_compile_max_beats(response_format_value)
    maximum_replacements_per_beat = max(
        1,
        maximum_beats - (len(beats) - len(ordered_failed)),
    )
    teaser = base_script.get("teaser_contract", {})
    if not isinstance(teaser, dict):
        teaser = {}
    explanation_ids = set(teaser.get("explanation_beat_ids", []) or [])
    reprise_ids = set(teaser.get("reprise_beat_ids", []) or [])

    def reference_schema(required: bool) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "maxItems": maximum_replacements_per_beat,
        }
        if required:
            schema["minItems"] = 1
        else:
            schema["maxItems"] = 0
        return schema

    branches = []
    for beat_id in ordered_failed:
        properties = {
            "replace_beat_id": {
                "type": "string",
                "const": beat_id,
            },
            "replacement_beats": {
                "type": "array",
                "items": copy.deepcopy(beat_schema),
                "minItems": 1,
                "maxItems": maximum_replacements_per_beat,
            },
            "explanation_beat_ids": reference_schema(
                beat_id in explanation_ids
            ),
            "reprise_beat_ids": reference_schema(beat_id in reprise_ids),
        }
        branches.append(
            {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            }
        )
    replacement_item_schema = (
        branches[0] if len(branches) == 1 else {"anyOf": branches}
    )
    root_properties = {
        "base_script_sha256": {
            "type": "string",
            "const": json_sha256(base_script),
        },
        "replacements": {
            "type": "array",
            "items": replacement_item_schema,
            "minItems": len(ordered_failed),
            "maxItems": len(ordered_failed),
        },
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "story_script_compile_beat_replacements_v1",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": root_properties,
                "required": list(root_properties),
                "additionalProperties": False,
            },
        },
    }


# === merge_story_script_compile_replacements (L811-L999) ===
def merge_story_script_compile_replacements(
    *,
    base_script: dict[str, Any],
    replacement_value: dict[str, Any],
    failed_beat_ids: list[str] | tuple[str, ...],
    maximum_beats: int,
) -> StoryScriptCompileReplacementResult:
    """Apply replacement fragments while freezing every unaffected field."""

    base = copy.deepcopy(base_script)
    base_sha256 = json_sha256(base_script)
    failed = list(
        dict.fromkeys(
            item
            for item in failed_beat_ids
            if isinstance(item, str) and item
        )
    )
    errors: list[str] = []
    if replacement_value.get("base_script_sha256") != base_sha256:
        errors.append("base_script_sha256 does not match the repair base")
    raw_replacements = replacement_value.get("replacements")
    if not isinstance(raw_replacements, list):
        raw_replacements = []
        errors.append("replacements must be an array")
    replacement_by_id: dict[str, dict[str, Any]] = {}
    for item in raw_replacements:
        if not isinstance(item, dict):
            errors.append("every replacement must be an object")
            continue
        replace_id = item.get("replace_beat_id")
        if not isinstance(replace_id, str) or not replace_id:
            errors.append("replace_beat_id must be non-empty")
            continue
        if replace_id in replacement_by_id:
            errors.append(f"Beat {replace_id} is replaced more than once")
            continue
        replacement_by_id[replace_id] = item
    missing = sorted(set(failed) - set(replacement_by_id))
    extra = sorted(set(replacement_by_id) - set(failed))
    if missing:
        errors.append(f"failed Beats missing replacements: {missing}")
    if extra:
        errors.append(f"non-failed Beats cannot be replaced: {extra}")

    base_beats = base.get("beats")
    if not isinstance(base_beats, list):
        base_beats = []
        errors.append("base Script beats must be an array")
    base_beat_ids = [
        str(item.get("id"))
        for item in base_beats
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    unknown_failed = sorted(set(failed) - set(base_beat_ids))
    if unknown_failed:
        errors.append(f"failed Beats are absent from base Script: {unknown_failed}")

    replacement_ids_by_old: dict[str, list[str]] = {}
    reference_remap: dict[str, dict[str, list[str]]] = {}
    for replace_id, item in replacement_by_id.items():
        values = item.get("replacement_beats")
        if not isinstance(values, list) or not values:
            errors.append(f"Beat {replace_id} needs at least one replacement Beat")
            values = []
        replacement_ids = [
            str(beat.get("id"))
            for beat in values
            if isinstance(beat, dict) and isinstance(beat.get("id"), str)
        ]
        if len(replacement_ids) != len(values):
            errors.append(
                f"Beat {replace_id} replacement contains an invalid Beat ID"
            )
        replacement_ids_by_old[replace_id] = replacement_ids
        replacement_id_set = set(replacement_ids)
        reference_remap[replace_id] = {}
        for field in ("explanation_beat_ids", "reprise_beat_ids"):
            remap = item.get(field)
            if not isinstance(remap, list):
                errors.append(f"Beat {replace_id}.{field} must be an array")
                remap = []
            if len(remap) != len(set(remap)):
                errors.append(f"Beat {replace_id}.{field} contains duplicates")
            unknown_remap = sorted(set(remap) - replacement_id_set)
            if unknown_remap:
                errors.append(
                    f"Beat {replace_id}.{field} references non-replacement "
                    f"Beat IDs: {unknown_remap}"
                )
            reference_remap[replace_id][field] = list(remap)

    teaser = base.get("teaser_contract")
    teaser = copy.deepcopy(teaser) if isinstance(teaser, dict) else {}
    for field in ("explanation_beat_ids", "reprise_beat_ids"):
        original_refs = teaser.get(field, [])
        if not isinstance(original_refs, list):
            original_refs = []
        for replace_id in failed:
            remap = reference_remap.get(replace_id, {}).get(field, [])
            if replace_id in original_refs and not remap:
                errors.append(
                    f"Beat {replace_id}.{field} must remap its frozen reference"
                )
            if replace_id not in original_refs and remap:
                errors.append(
                    f"Beat {replace_id}.{field} cannot create a new reference"
                )
        rewritten_refs: list[str] = []
        for beat_id in original_refs:
            if beat_id in set(failed):
                rewritten_refs.extend(
                    reference_remap.get(beat_id, {}).get(field, [])
                )
            else:
                rewritten_refs.append(beat_id)
        teaser[field] = rewritten_refs

    merged_beats: list[dict[str, Any]] = []
    for beat in base_beats:
        if not isinstance(beat, dict):
            continue
        beat_id = beat.get("id")
        if beat_id in replacement_by_id:
            merged_beats.extend(
                copy.deepcopy(
                    replacement_by_id[beat_id].get("replacement_beats", [])
                )
            )
        else:
            merged_beats.append(copy.deepcopy(beat))
    merged_ids = [
        item.get("id") for item in merged_beats if isinstance(item, dict)
    ]
    duplicate_ids = sorted(
        {
            beat_id
            for beat_id in merged_ids
            if isinstance(beat_id, str) and merged_ids.count(beat_id) > 1
        }
    )
    if duplicate_ids:
        errors.append(f"replacement Beat IDs must be unique: {duplicate_ids}")
    if len(merged_beats) > maximum_beats:
        errors.append(
            f"replacement would exceed Story Script Beat limit: "
            f"{len(merged_beats)} > {maximum_beats}"
        )

    if not errors:
        base["beats"] = merged_beats
        base["teaser_contract"] = teaser
    unchanged_hashes = {
        str(beat["id"]): json_sha256(beat)
        for beat in base_beats
        if isinstance(beat, dict)
        and isinstance(beat.get("id"), str)
        and beat["id"] not in set(failed)
    }
    effective_value = base if not errors else copy.deepcopy(base_script)
    effective_by_id = {
        str(beat["id"]): beat
        for beat in effective_value.get("beats", []) or []
        if isinstance(beat, dict) and isinstance(beat.get("id"), str)
    }
    unchanged_preserved = all(
        beat_id in effective_by_id
        and json_sha256(effective_by_id[beat_id]) == beat_hash
        for beat_id, beat_hash in unchanged_hashes.items()
    )
    if not unchanged_preserved:
        errors.append("an unaffected Beat changed during local replacement")
        effective_value = copy.deepcopy(base_script)
    audit = {
        "schema_version": STORY_SCRIPT_COMPILE_REPAIR_RESPONSE_SCHEMA_VERSION,
        "base_script_sha256": base_sha256,
        "failed_beat_ids": failed,
        "replacement_beat_ids": replacement_ids_by_old,
        "reference_remap": reference_remap,
        "unchanged_beat_sha256": unchanged_hashes,
        "unchanged_beats_preserved": unchanged_preserved,
        "result_script_sha256": json_sha256(effective_value),
        "errors": list(errors),
    }
    return _get_StoryScriptCompileReplacementResult()(
        value=effective_value,
        errors=errors,
        audit=audit,
    )


# === story_script_compile_repair_payload (L1002-L1066) ===
def story_script_compile_repair_payload(
    payload: dict[str, Any],
    *,
    base_script: dict[str, Any],
    failed_beat_ids: list[str] | tuple[str, ...],
    error_summary: str,
    context: dict[str, Any],
    compile_preservation_contract: dict[str, Any],
    compaction_projection: list[dict[str, Any]] | None,
    mismatch_projection: list[dict[str, Any]] | None,
    repair_round: int,
    response_format_value: dict[str, Any],
) -> dict[str, Any]:
    """Create a compile-only request that cannot rewrite the whole Script."""

    retried = semantic_retry_payload(
        payload,
        error_summary=error_summary,
        invalid_value=base_script,
        compaction_beat_ids=failed_beat_ids,
        compaction_projection=compaction_projection,
        mismatch_projection=mismatch_projection,
        context=context,
        compile_preservation_contract=compile_preservation_contract,
    )
    retried["response_format"] = story_script_compile_repair_response_format(
        base_script=base_script,
        failed_beat_ids=failed_beat_ids,
        response_format_value=response_format_value,
    )
    failed_set = set(failed_beat_ids)
    failed_snapshots = [
        copy.deepcopy(beat)
        for beat in base_script.get("beats", []) or []
        if isinstance(beat, dict) and beat.get("id") in failed_set
    ]
    retried["messages"][-1]["content"].append(
        {
            "type": "text",
            "text": (
                "本次是 Story Script compile repair 第 "
                f"{repair_round} 轮。禁止返回或重写整份 Story Script；strict "
                "Schema 只接受失败 Editorial Beat 的 replacements。每个 "
                "replace_beat_id 必须恰好出现一次，replacement_beats 只承担该"
                "局部叙事功能；未失败 Beat 和所有顶层合同由本地代码冻结。若旧 "
                "Beat 位于 teaser_contract.explanation_beat_ids 或 "
                "reprise_beat_ids，使用同名 remap 数组明确指向 replacement Beats；"
                "否则对应数组必须为空。base_script_sha256="
                f"{json_sha256(base_script)}；failed_beat_ids="
                + json.dumps(list(failed_beat_ids), ensure_ascii=False)
                + "；failed_beat_snapshots="
                + json.dumps(
                    failed_snapshots,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "。Candidate suggestion 和 retrieval candidate_id 同样会进入"
                "物理包络；若 Candidate 的 physical_unit_ids 与本 replacement "
                "Beat 的 direct Event physical_unit_id 不一致，必须删除该 Candidate "
                "引用，不能因为它是 Hook/Highlight 建议就保留。Candidate 数组允许"
                "为空。"
            ),
        }
    )
    return retried


# === story_script_retry_contract_projection (L1069-L1159) ===
def story_script_retry_contract_projection(
    invalid_value: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project exact cross-array obligations for a full semantic retry."""

    if not isinstance(invalid_value, dict):
        return {}
    beats = [
        item
        for item in invalid_value.get("beats", []) or []
        if isinstance(item, dict)
    ]
    selected = [
        item
        for item in invalid_value.get("selected_thread_beat_ids", []) or []
        if isinstance(item, str) and item
    ]
    retrieved = {
        item
        for beat in beats
        for item in (
            beat.get("retrieval_requirements", {}).get(
                "thread_beat_ids", []
            )
            if isinstance(beat.get("retrieval_requirements"), dict)
            else []
        )
        if isinstance(item, str) and item
    }
    primary_thread_id = str(
        invalid_value.get("primary_story_thread_id") or ""
    )
    primary_thread_missing_beat_ids = []
    for beat in beats:
        retrieval = beat.get("retrieval_requirements", {})
        story_thread_ids = (
            retrieval.get("story_thread_ids", [])
            if isinstance(retrieval, dict)
            else []
        )
        if (
            beat.get("thread_role") == "primary"
            and primary_thread_id not in story_thread_ids
        ):
            primary_thread_missing_beat_ids.append(str(beat.get("id") or "?"))

    projection: dict[str, Any] = {
        "selected_thread_beat_ids": selected,
        "retrieved_thread_beat_ids": sorted(retrieved),
        "missing_selected_thread_beat_ids": sorted(set(selected) - retrieved),
        "primary_story_thread_id": primary_thread_id,
        "primary_thread_missing_beat_ids": primary_thread_missing_beat_ids,
    }
    teaser = invalid_value.get("teaser_contract", {})
    if isinstance(teaser, dict) and teaser.get("reprise_policy") == "delayed":
        positions = {
            str(beat.get("id")): index
            for index, beat in enumerate(beats)
            if isinstance(beat.get("id"), str)
        }
        explanation_ids = [
            item
            for item in teaser.get("explanation_beat_ids", []) or []
            if isinstance(item, str) and item in positions
        ]
        reprise_ids = [
            item
            for item in teaser.get("reprise_beat_ids", []) or []
            if isinstance(item, str) and item in positions
        ]
        between_ids: list[str] = []
        if explanation_ids and reprise_ids:
            last_explanation = max(positions[item] for item in explanation_ids)
            first_reprise = min(positions[item] for item in reprise_ids)
            between_ids = [
                str(beat["id"])
                for beat in beats[last_explanation + 1 : first_reprise]
                if isinstance(beat.get("id"), str)
                and beat.get("thread_role") == "primary"
                and beat.get("role")
                in {"escalation", "turn_or_reveal", "payoff", "end_hook"}
            ]
        projection["delayed_reprise"] = {
            "explanation_beat_ids": explanation_ids,
            "reprise_beat_ids": reprise_ids,
            "qualifying_progression_beat_ids_between": between_ids,
            "minimum_progression_beats": int(
                teaser.get("reprise_delay_minimum_progression_beats", 1)
            ),
        }
    return projection


# === semantic_retry_payload (L1162-L1391) ===
def semantic_retry_payload(
    payload: dict[str, Any],
    *,
    error_summary: str,
    treatment_viability: dict[str, Any] | None = None,
    treatment_attempts: list[dict[str, Any]] | None = None,
    invalid_value: dict[str, Any] | None = None,
    compaction_beat_ids: list[str] | tuple[str, ...] | None = None,
    compaction_projection: list[dict[str, Any]] | None = None,
    mismatch_projection: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    compile_preservation_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a fresh request with a narrow output-correction instruction.

    The invalid answer itself is deliberately not replayed: it can be large,
    contain malformed JSON, or repeat video-derived text.  For physical
    compaction failures, only the failed Beat's direct-evidence projection is
    carried forward. Ordinary correction retries keep the original strict
    schema. Treatment semantic retries replace only ``teaser_contract`` with
    one compiled branch; the accepted response is still cached under the
    stage/signature that includes this retry policy.
    """
    retried = copy.deepcopy(payload)
    compile_treatment_identity = (
        compile_preservation_contract.get("treatment_identity", {})
        if isinstance(compile_preservation_contract, dict)
        else {}
    )
    compile_treatment_option_id = str(
        compile_treatment_identity.get("treatment_option_id") or ""
    )
    treatment_decision = None
    compile_treatment_locked = False
    if compile_preservation_contract is not None:
        compile_treatment_locked = _lock_treatment_option_branch(
            retried,
            compile_treatment_option_id,
        )
    else:
        treatment_decision = treatment_retry_decision(
            retried,
            treatment_viability=treatment_viability,
            treatment_attempts=treatment_attempts,
        )
    if treatment_decision is not None:
        retried["response_format"]["json_schema"]["schema"]["properties"][
            "teaser_contract"
        ] = copy.deepcopy(treatment_decision["branch"])
    messages = retried.get("messages")
    if not isinstance(messages, list) or not messages:
        return retried
    user_message = messages[-1]
    content = user_message.get("content") if isinstance(user_message, dict) else None
    if not isinstance(content, list):
        return retried
    summary = " ".join(str(error_summary).split())[:1000]
    payload_text = " ".join(
        item.get("text", "")
        for item in content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    )
    treatment_fallback_markers = (
        "delayed_reprise",
        "delayed reprise",
        "no_reprise",
        "no reprise",
        "reprise_",
        "teaser reprise",
        "stitch_window",
        "stitch window",
        "stitch 窗",
        "no_reprise_mandatory_body_replay",
        "teaser_highlight_withheld_fact_conflict",
        "treatment_viability",
    )
    treatment_fallback = (
        '"story_treatment"' in payload_text
        and any(
            marker in summary.lower()
            for marker in treatment_fallback_markers
        )
    )
    treatment_fallback_instruction = ""
    if treatment_decision is not None:
        target_id = treatment_decision["target_treatment_option_id"]
        failure_codes = treatment_decision["failure_codes"]
        if treatment_decision["mode"] == "repair_current":
            treatment_fallback_instruction = (
                " 本次重试的 strict Schema 已锁定当前 Treatment Option "
                f"{target_id}；保持该 Option 的 strategy/mode/reprise policy，"
                f"只修复这些确定性结构错误：{failure_codes}。不得切换或自造"
                " Treatment Option。"
            )
        else:
            treatment_fallback_instruction = (
                " 已失败的 Treatment Option 不得重复。此次 strict Schema "
                f"已锁定可执行备选 {target_id}；失败项为 "
                f"{treatment_decision['failed_treatment_option_ids']}，"
                "必须逐字服从锁定 Option 的 strategy/mode/reprise policy，"
                "不得切回失败项或自造 Option。"
            )
    elif treatment_fallback:
        treatment_fallback_instruction = (
            " 这类错误允许你放弃当前 Treatment，但只能从当前上下文 "
            "story_treatment.options 中逐字复制一个已经编译的完整 Option，"
            "并让 treatment_option_id、strategy、mode、reprise_policy 及其"
            "全部约束保持同一 Option 内一致；不得自造 Option ID、策略或放宽"
            "合同。根据错误与当前 Beat 的必须证据比较所有已编译"
            "Option：必须在正文重现开场时不得选 no-reprise；"
            "delayed-reprise 只在 explanation、主线推进和声明的 reprise "
            "都能满足时选择；其他情况使用已编译的 "
            "chronological_compression 作为最后的安全方案。"
        )
    elif compile_preservation_contract is not None:
        treatment_fallback_instruction = (
            " 这是 compile-only 重写，不是 Treatment 重新选择。"
            f"必须保持 Treatment Option {compile_treatment_option_id} 及其 "
            "strategy/mode/reprise policy 不变，不得切换或自造 Option。"
            + (
                "本次 strict Schema 已锁定该 Option。"
                if compile_treatment_locked
                else "本地 preservation validator 会逐字段验证该 Option。"
            )
        )
    compaction_instruction = ""
    compact_beats = [
        item
        for item in compaction_projection or []
        if isinstance(item, dict)
    ]
    if not compact_beats:
        compact_beats = _get_compaction_retry_projection()(
            invalid_value,
            compaction_beat_ids,
            context=context,
        )
    if compact_beats:
        compact_projection_text = json.dumps(
            compact_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        compaction_instruction = (
            " 失败响应中需要物理拆分的 Beat 紧凑投影如下："
            f"{compact_projection_text}。必须用因果有序的更小 Editorial Beats "
            "替换这些 Beat，或删除不属于该新 Beat 的冗余 direct Event 引用；"
            "每个替换 Beat 的 event_ids、must_show.evidence_event_ids 与 "
            "retrieval_requirements.event_ids 只保留该局部动作/对白/反应所需的"
            "直接证据。Thread Beat ID 可以继续承担功能覆盖，但不得把 Thread "
            "Beat 的完整 Event 清单原样复制进每个新 Beat。保持 Treatment、"
            "因果顺序、required Thread Beat、Payoff 与 strict Schema 不变，"
            "并把总 Beat 数控制在 Schema 上限内。Candidate suggestion 与 "
            "retrieval candidate_id 同样进入物理包络；如果投影中的 Candidate "
            "physical range/unit 与 Event physical_unit_id 不一致，删除 Candidate "
            "引用，Candidate 数组可以为空。"
        )
    mismatch_instruction = ""
    effective_mismatch_projection = [
        item
        for item in mismatch_projection or []
        if isinstance(item, dict)
    ]
    if not effective_mismatch_projection:
        effective_mismatch_projection = mismatch_retry_projection(
            invalid_value,
            context,
        )
    if effective_mismatch_projection:
        mismatch_instruction = (
            " Event/Thread Beat 身份错挂投影如下："
            + json.dumps(
                effective_mismatch_projection,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "。只可把 direct Event 移到其 allowed Thread Beat 对应的 "
            "Editorial Beat，或从当前 Beat 删除不属于其局部叙事功能的冗余引用；"
            "不得修改 Series Bible 的 Thread Beat/Event 归属，不得删除 required "
            "Thread Beat。"
        )
    preservation_instruction = ""
    if compile_preservation_contract is not None:
        preservation_instruction = (
            " compile-only 质量保持合同如下："
            + json.dumps(
                compile_preservation_contract,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "。Treatment、selected/required Thread Beats、required Facts、"
            "intentional mystery、中心问题、起止状态、Local Payoff 必须不变；"
            "原 must-show ID 及其 Event/Fact AND 证据不得删除或弱化。"
            "允许 must-show 移到拆分后的新 Beat，也允许删除普通冗余 event_ids。"
        )
    retry_contract_projection = story_script_retry_contract_projection(
        invalid_value
    )
    retry_contract_instruction = ""
    if retry_contract_projection:
        retry_contract_instruction = (
            " 完整重试前必须逐项核对以下跨数组合同："
            + json.dumps(
                retry_contract_projection,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "。新响应必须满足 selected_thread_beat_ids 与全部 Beat 的 "
            "retrieval_requirements.thread_beat_ids 并集之差为空；每个 "
            "thread_role=primary 的 Beat 都必须检索 primary_story_thread_id。"
            "若 reprise_policy=delayed，explanation_beat_ids 只能列恢复前因的"
            " Beat，不能把 reprise 前的所有 Beat 全塞进去；最后一个 explanation "
            "之后、首个 reprise 之前必须至少留下规定数量的 primary "
            "escalation/turn_or_reveal/payoff/end_hook Beat。"
        )
    content.append(
        {
            "type": "text",
            "text": (
                "上一响应未通过机器校验，错误摘要："
                f"{summary}。请重新完成同一任务；只能输出一个符合既定 strict "
                "JSON Schema 的 JSON object，不要输出数组、Markdown、代码围栏、"
                "解释或推理过程。所有 const/enum/required 字段必须逐项满足。"
                f"{treatment_fallback_instruction}{compaction_instruction}"
                f"{mismatch_instruction}{preservation_instruction}"
                f"{retry_contract_instruction}"
            ),
        }
    )
    return retried


# === story_script_preflight_admission (L1469-L1567) ===
def story_script_preflight_admission(
    value: dict[str, Any],
    context: dict[str, Any],
) -> StoryScriptAdmissionResult:
    """Run the production Story Script preflight without writing artifacts."""

    try:
        preflight = preflight_script(
            value,
            events=_records_by_id(context.get("events", [])),
            candidates=_records_by_id(context.get("candidates", [])),
            characters=_records_by_id(context.get("characters", [])),
            relationships=_records_by_id(context.get("relationships", [])),
            facts=_records_by_id(context.get("facts", [])),
            threads=_records_by_id(context.get("story_threads", [])),
            thread_beats=_records_by_id(context.get("thread_beats", [])),
            questions=_records_by_id(context.get("open_questions", [])),
            # Source durations only clip observational padding. Teaser physical
            # obligations and structural feasibility use exact Event/Candidate
            # ranges and therefore remain fully enforceable at cache admission.
            source_durations={},
            context_padding_seconds=12.0,
            usable_ratio=0.6,
            treatment_options=(
                context.get("story_treatment", {}).get("options", [])
                if isinstance(context.get("story_treatment"), dict)
                else []
            ),
            timeline_segments=(
                context.get("timeline_segments", [])
                if isinstance(context.get("timeline_segments"), list)
                else []
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        detail = " ".join(str(exc).split())[:800]
        code = "story_script_preflight_exception"
        return _get_StoryScriptAdmissionResult()(
            feasibility_status="not_feasible",
            failure_codes=[code],
            repair_route="story_script",
            errors=[f"{code}: {detail}"],
            treatment_viability=None,
        )

    feasibility = preflight.get("feasibility", {})
    teaser = feasibility.get("teaser_diagnostics", {})
    status = str(feasibility.get("status") or "not_feasible")
    failure_codes = sorted(
        {
            str(item)
            for item in (
                list(feasibility.get("failure_codes", []) or [])
                + list(teaser.get("failure_codes", []) or [])
            )
            if isinstance(item, str) and item
        }
    )
    repair_route = str(teaser.get("repair_route") or "story_script")
    treatment_viability = feasibility.get("treatment_viability")
    if not isinstance(treatment_viability, dict):
        treatment_viability = None
    split_regeneration_beat_ids = tuple(
        item
        for item in feasibility.get("split_regeneration_beat_ids", []) or []
        if isinstance(item, str) and item
    )
    if status != "not_feasible" and not failure_codes:
        return _get_StoryScriptAdmissionResult()(
            feasibility_status=status,
            failure_codes=failure_codes,
            repair_route=repair_route,
            errors=[],
            treatment_viability=treatment_viability,
            split_regeneration_beat_ids=split_regeneration_beat_ids,
        )
    if not failure_codes:
        failure_codes = ["story_script_not_feasible"]
    risks = [
        " ".join(str(item).split())
        for item in feasibility.get("material_risks", []) or []
        if isinstance(item, str) and item.strip()
    ]
    detail = " | ".join(risks[:8]) or "local preflight marked Story not feasible"
    errors = [
        (
            "story_script_preflight "
            f"failure_code={code} repair_route={repair_route}: {detail}"
        )
        for code in failure_codes
    ]
    return _get_StoryScriptAdmissionResult()(
        feasibility_status=status,
        failure_codes=failure_codes,
        repair_route=repair_route,
        errors=errors,
        treatment_viability=treatment_viability,
        split_regeneration_beat_ids=split_regeneration_beat_ids,
    )


# === broad_script_prompt (L1655-L1692) ===
def broad_script_prompt(context: dict[str, Any]) -> str:
    primary_story_thread_id = str(
        context.get("story_treatment", {}).get(
            "primary_story_thread_id", ""
        )
    )
    return (
        "\n\n【Broad Script 覆盖与物理编译合同】selected_thread_beat_ids 必须覆盖"
        "本 Story source_thread_beat_ids 的 80%–100%，全部 required Thread "
        "Beat 必须选择；Broad 模式 beats 允许 4–14 个。"
        "direct_evidence_contract.thread_beats[].allowed_direct_event_ids 是"
        "可选证据菜单，不是必须逐项复制的完成清单。每个 selected Thread Beat "
        "只选择能证明其叙事功能的最少 direct Event；严禁把完整 Event 数组复制进"
        "一个 Editorial Beat。一个 Editorial Beat 可以在 retrieval_requirements."
        "thread_beat_ids 中承担多个 Thread Beat，但该 Beat 的全部 direct Event "
        "必须来自同一个 physical_unit_id；如果各 Thread Beat 的代表 Event 不在"
        "同一 physical unit，就必须拆成多个 Editorial Beat。多 Thread Beat 功能"
        "覆盖绝不授权跨 source、跨远距离 gap 或跨不兼容 Timeline Segment 的物理"
        "合并。Candidate suggestion 与 retrieval candidate_id 也属于物理证据；"
        "Candidate physical_unit_ids 与 Beat 的 direct Event physical_unit_id 不同"
        "时必须省略 Candidate，Candidate 数组允许为空。Teaser 选择 primary "
        "Highlight 后，teaser_intent 的 direct must-show "
        "只从 direct_evidence_contract.teaser_candidates 中该 Candidate 的 "
        "safest_direct_event_ids 选择；不得加入邻近但只用于背景解释的 Event。"
        "该 Candidate 的 directly_revealed_fact_ids 不得同时写进 Teaser 的 "
        "must_not_reveal_fact_ids。输出前做集合核对：selected_thread_beat_ids 减去"
        "所有 Beats 的 retrieval_requirements.thread_beat_ids 并集必须为空，且每个"
        " Beat 的 thread_beat_ids 至少一个。"
        "任何 required_before_fact_ids 必须在严格更早的 Beat 已经 introduced，"
        "禁止在同一 Beat 同时 required_before 与 introduced。最后一个 body Beat、"
        "payoff 或 end_hook 必须回到主线，thread_role=primary，且 "
        "retrieval_requirements.story_thread_ids 必须包含 primary_story_thread_id="
        f"{primary_story_thread_id}。若 reprise_policy=delayed，explanation_beat_ids "
        "只列恢复开场前因的 Beat；最后一个 explanation 与首个 reprise 之间必须"
        "至少有一个不属于 explanation/reprise 数组的 primary escalation、"
        "turn_or_reveal、payoff 或 end_hook Beat，作为主线新推进。不要为了减少 "
        "Beat 数量破坏上述物理、Fact、Treatment 或主线合同。"
    )


# === story_script_compile_repair_codes (L1845-L1872) ===
def story_script_compile_repair_codes(
    validation: JobResponseValidation,
) -> set[str]:
    """Return typed compile-only codes, or an empty set for other failures."""

    admission = validation.story_script_admission
    if admission is None or validation.schema_errors:
        return set()
    codes = {
        code
        for code in admission.failure_codes
        if isinstance(code, str) and code
    }
    if not codes or not codes <= STORY_SCRIPT_COMPILE_REPAIR_PROCESS_CODES:
        return set()
    unrelated_identity_errors = [
        error
        for error in validation.identity_errors
        if not any(code in error for code in codes)
    ]
    unrelated_contract_errors = [
        error
        for error in validation.contract_errors
        if not any(code in error for code in codes)
    ]
    if unrelated_identity_errors or unrelated_contract_errors:
        return set()
    return codes


# === apply_story_script_preservation_validation (L1875-L1926) ===
def apply_story_script_preservation_validation(
    validation: JobResponseValidation,
    contract: dict[str, Any] | None,
) -> tuple[JobResponseValidation, list[str]]:
    if not isinstance(contract, dict):
        return validation, []
    preservation_errors = validate_story_script_preservation(
        contract,
        validation.value,
    )
    if not preservation_errors:
        return validation, []
    admission = validation.story_script_admission
    if admission is None:
        admission = _get_StoryScriptAdmissionResult()(
            feasibility_status="not_feasible",
            failure_codes=[
                "story_script_compile_preservation_violation"
            ],
            repair_route="story_script",
            errors=list(preservation_errors),
        )
    else:
        admission = replace(
            admission,
            feasibility_status="not_feasible",
            failure_codes=list(
                dict.fromkeys(
                    [
                        *admission.failure_codes,
                        "story_script_compile_preservation_violation",
                    ]
                )
            ),
            errors=list(
                dict.fromkeys(
                    [*admission.errors, *preservation_errors]
                )
            ),
        )
    return (
        replace(
            validation,
            contract_errors=list(
                dict.fromkeys(
                    [*validation.contract_errors, *preservation_errors]
                )
            ),
            story_script_admission=admission,
        ),
        preservation_errors,
    )


# === story_script_compile_replacement_rejection (L1929-L1954) ===
def story_script_compile_replacement_rejection(
    *,
    value: dict[str, Any],
    errors: list[str],
    failed_beat_ids: list[str] | tuple[str, ...],
) -> JobResponseValidation:
    """Keep fragment/merge failures inside the bounded compile repair phase."""

    normalized = [
        "story_script_compile_replacement_invalid: " + str(error)
        for error in errors
        if str(error)
    ] or ["story_script_compile_replacement_invalid: unknown fragment error"]
    return _get_JobResponseValidation()(
        value=copy.deepcopy(value),
        schema_errors=[],
        identity_errors=[],
        contract_errors=normalized,
        story_script_admission=StoryScriptAdmissionResult(
            feasibility_status="not_feasible",
            failure_codes=["story_script_compile_replacement_invalid"],
            repair_route="story_script",
            errors=normalized,
            split_regeneration_beat_ids=tuple(failed_beat_ids),
        ),
    )


# === story_script_compile_failure_signature (L1957-L1997) ===
def story_script_compile_failure_signature(
    validation: JobResponseValidation,
    *,
    context: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    admission = validation.story_script_admission
    compaction_ids = (
        list(admission.split_regeneration_beat_ids)
        if admission is not None
        else []
    )
    compaction = _get_compaction_retry_projection()(
        validation.value,
        compaction_ids,
        context=context,
    )
    mismatch = mismatch_retry_projection(validation.value, context)
    preservation_errors = sorted(
        {
            error
            for error in validation.contract_errors
            if "story_script_compile_preservation_violation" in error
        }
    )
    replacement_errors = sorted(
        {
            error
            for error in validation.contract_errors
            if "story_script_compile_replacement_invalid" in error
        }
    )
    signature_payload = {
        "failure_codes": sorted(
            story_script_compile_repair_codes(validation)
        ),
        "compaction_projection": compaction,
        "mismatch_projection": mismatch,
        "preservation_errors": preservation_errors,
        "replacement_errors": replacement_errors,
    }
    return json_sha256(signature_payload), compaction, mismatch


# === treatment_attempt_audit_path (L2666-L2682) ===
def treatment_attempt_audit_path(
    output_path: Path,
    *,
    story_id: str,
) -> Path:
    """Return the stable per-Story Treatment attempt audit path."""

    job_root = (
        output_path.parent.parent
        if output_path.parent.name == "story-scripts"
        else output_path.parent
    )
    return (
        job_root
        / "story-treatment-attempts"
        / f"{_sanitize_path_component(story_id)}.json"
    )


# === write_treatment_attempt_audit (L2685-L2759) ===
def write_treatment_attempt_audit(
    *,
    output_path: Path,
    context: dict[str, Any],
    job: dict[str, Any],
    signature: str,
    attempts: list[dict[str, Any]],
    final_status: str,
) -> Path | None:
    """Persist immutable-by-signature Treatment selection attempts.

    The audit contains hashes and verdicts, not full model responses.  The
    generic semantic diagnostics retain the rejected payload privately.
    """

    if job.get("task") != "story_script_draft":
        return None
    story_id = str(
        context.get("story", {}).get("story_id")
        or job.get("id")
        or output_path.stem
    )
    path = treatment_attempt_audit_path(output_path, story_id=story_id)
    existing = load_json(path) if path.is_file() else {}
    generations = [
        item
        for item in existing.get("generations", [])
        if isinstance(item, dict)
        and item.get("request_signature") != signature
    ]
    final_attempt = attempts[-1] if attempts else {}
    generations.append(
        {
            "request_signature": signature,
            "stage_version": job.get("stage_version"),
            "updated_at": utc_now(),
            "status": final_status,
            "attempt_count": len(attempts),
            "attempts": attempts,
            "final_treatment_option_id": final_attempt.get(
                "treatment_option_id", ""
            ),
            "final_strategy": final_attempt.get("strategy", ""),
        }
    )
    treatment_context = context.get("story_treatment", {})
    atomic_write_json(
        path,
        {
            "schema_version": "1.4",
            "story_id": story_id,
            "treatment_options_sha256": context.get(
                "treatment_options_sha256", ""
            ),
            "recommended_treatment_option_id": (
                treatment_context.get("recommended_treatment_option_id", "")
                if isinstance(treatment_context, dict)
                else ""
            ),
            "active_request_signature": signature,
            "generations": generations,
            "script_recovery_attempts": [
                item
                for item in existing.get("script_recovery_attempts", [])
                if isinstance(item, dict)
            ],
            "plan_recovery_attempts": [
                item
                for item in existing.get("plan_recovery_attempts", [])
                if isinstance(item, dict)
            ],
        },
        private=True,
    )
    return path


# === treatment_attempt_record (L2762-L2877) ===
def treatment_attempt_record(
    *,
    validation: JobResponseValidation,
    semantic_attempt: int,
    context: dict[str, Any] | None = None,
    preservation_contract: dict[str, Any] | None = None,
    request_phase: str = "initial",
    ordinary_retry_used: int = 0,
    compile_repair_used: int = 0,
    compile_repair_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contract = validation.value.get("teaser_contract", {})
    if not isinstance(contract, dict):
        contract = {}
    admission = validation.story_script_admission
    viability = (
        admission.treatment_viability
        if admission is not None
        and isinstance(admission.treatment_viability, dict)
        else {}
    )
    if admission is not None and admission.failure_codes:
        failure_codes = admission.failure_codes
    elif validation.schema_errors:
        failure_codes = ["story_script_schema_invalid"]
    elif validation.identity_errors:
        failure_codes = ["story_script_identity_mismatch"]
    elif validation.contract_errors:
        failure_codes = ["story_script_contract_invalid"]
    else:
        failure_codes = []
    split_regeneration_beat_ids = (
        list(admission.split_regeneration_beat_ids)
        if admission is not None
        else []
    )
    compaction_projection = _get_compaction_retry_projection()(
        validation.value,
        split_regeneration_beat_ids,
        context=context,
    )
    mismatch_projection = mismatch_retry_projection(
        validation.value,
        context,
    )
    compile_codes = sorted(story_script_compile_repair_codes(validation))
    preservation_errors = [
        error
        for error in validation.contract_errors
        if "story_script_compile_preservation_violation" in error
    ]
    replacement_errors = [
        error
        for error in validation.contract_errors
        if "story_script_compile_replacement_invalid" in error
    ]
    compile_failure_signature = (
        json_sha256(
            {
                "failure_codes": compile_codes,
                "compaction_projection": compaction_projection,
                "mismatch_projection": mismatch_projection,
                "preservation_errors": sorted(set(preservation_errors)),
                "replacement_errors": sorted(set(replacement_errors)),
            }
        )
        if compile_codes
        else ""
    )
    base = {
        "semantic_attempt": semantic_attempt,
        "request_phase": request_phase,
        "ordinary_retry_used": ordinary_retry_used,
        "compile_repair_used": compile_repair_used,
        "treatment_option_id": str(
            contract.get("treatment_option_id") or ""
        ),
        "strategy": str(contract.get("strategy") or ""),
        "analysis_sha256": json_sha256(validation.value),
        "verdict": "rejected" if validation.errors else "accepted",
        "failure_codes": list(dict.fromkeys(failure_codes)),
        "treatment_failure_codes": list(
            dict.fromkeys(viability.get("failure_codes", []) or [])
        ),
        "recommended_alternate_treatment_option_id": str(
            viability.get("recommended_alternate_treatment_option_id") or ""
        ),
        "alternate_treatment_option_ids": list(
            viability.get("alternate_treatment_option_ids", []) or []
        ),
        "split_regeneration_beat_ids": split_regeneration_beat_ids,
        "failure_class": (
            "compile_only" if compile_codes else "semantic"
        ),
        "compile_failure_codes": compile_codes,
        "compile_failure_signature": compile_failure_signature,
        "compaction_retry_projection": compaction_projection,
        "mismatch_retry_projection": mismatch_projection,
        "preservation_contract_sha256": (
            json_sha256(preservation_contract)
            if isinstance(preservation_contract, dict)
            else ""
        ),
        "preservation_contract": (
            copy.deepcopy(preservation_contract)
            if isinstance(preservation_contract, dict)
            else None
        ),
        "preservation_errors": preservation_errors,
        "compile_repair_audit": (
            copy.deepcopy(compile_repair_audit)
            if isinstance(compile_repair_audit, dict)
            else None
        ),
    }
    return {**base, "attempt_sha256": json_sha256(base)}


# === treatment_retry_viability_from_validation (L2880-L2901) ===
def treatment_retry_viability_from_validation(
    validation: JobResponseValidation,
) -> dict[str, Any] | None:
    admission = validation.story_script_admission
    if admission is None:
        return None
    viability = copy.deepcopy(admission.treatment_viability or {})
    in_place_admission_codes = {
        code
        for code in admission.failure_codes
        if code in TREATMENT_RETRY_IN_PLACE_FAILURE_CODES
    }
    viability_codes = {
        str(item)
        for item in viability.get("failure_codes", []) or []
        if isinstance(item, str) and item
    }
    combined_codes = viability_codes | in_place_admission_codes
    if not combined_codes:
        return None
    viability["failure_codes"] = sorted(combined_codes)
    return viability


# === treatment_retry_viability_from_attempt (L2904-L2932) ===
def treatment_retry_viability_from_attempt(
    attempt: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(attempt, dict) or attempt.get("verdict") != "rejected":
        return None
    raw_failure_codes = (
        attempt.get("treatment_failure_codes")
        if "treatment_failure_codes" in attempt
        else attempt.get("failure_codes")
    )
    failure_codes = [
        item
        for item in raw_failure_codes or []
        if isinstance(item, str) and item
    ]
    if not failure_codes:
        return None
    return {
        "selected_treatment_option_id": str(
            attempt.get("treatment_option_id") or ""
        ),
        "failure_codes": failure_codes,
        "recommended_alternate_treatment_option_id": str(
            attempt.get("recommended_alternate_treatment_option_id") or ""
        ),
        "alternate_treatment_option_ids": list(
            attempt.get("alternate_treatment_option_ids", []) or []
        ),
    }


