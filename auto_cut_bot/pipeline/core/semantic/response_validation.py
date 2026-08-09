"""响应校验 — 从 semantic_handlers.py 提取的 response_validation 函数组。

原位置: semantic_handlers.py, 3 funcs, ~358L
依赖: story_schemas, window_analysis_contract, series_registry_*, series_assignment_contract
注: JobResponseValidation 类保留在 semantic_handlers.py，本模块惰性导入。
"""

from __future__ import annotations

import copy
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
from autocut_core.semantic.window_analysis_contract import (
    canonicalize_window_analysis,
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


def validate_job_response(
    task: str,
    value: dict[str, Any],
    response_format_value: dict[str, Any],
) -> list[str]:
    errors = validate_task_response(task, value)
    custom_schema = response_format_value["json_schema"]["schema"]
    if custom_schema is not SCHEMAS[task]:
        errors.extend(validate_schema(value, custom_schema))
    return errors


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
    if task == "window_analysis":
        window_contract_result = canonicalize_window_analysis(value, job=job)
        effective_value = window_contract_result.effective_window

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
    )