"""语义 registry / contract 处理 — 从 semantic_handlers.py 提取的 contract_cache + registry_identity 函数组。

原位置: semantic_handlers.py, 13 funcs, ~885L
包含: 直接证据契约索引、注册表修复报告、契约修复记录、缓存元数据、身份修复编排
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from autocut_core.semantic.engine import (
    _sanitize_path_component,
    AdaptiveConcurrencyController,
    AttemptLedger,
    JobRecorder,
    RateLimiter,
)
from .assignment_contract import (
    POLICY_VERSION as SERIES_ASSIGNMENT_POLICY_VERSION,
)
from .registry_admission import (
    POLICY_VERSION as SERIES_REGISTRY_ADMISSION_POLICY_VERSION,
    compile_series_registry_admission,
    write_series_registry_admission_artifacts,
)
from .registry_alias_repair import (
    POLICY_VERSION as SERIES_REGISTRY_ALIAS_REPAIR_POLICY_VERSION,
)
from .registry_identity_repair import (
    POLICY_VERSION as SERIES_REGISTRY_IDENTITY_REPAIR_POLICY_VERSION,
    STAGE_VERSION as SERIES_REGISTRY_IDENTITY_REPAIR_STAGE_VERSION,
    repair_series_registry_identities,
)
from .registry_recovery import (
    POLICY_VERSION as SERIES_REGISTRY_RECOVERY_POLICY_VERSION,
    load_registry_recovery_candidate,
    load_registry_recovery_state,
    merge_registry_relationship_progress,
    persist_registry_recovery_candidate,
)
from .registry_reference_repair import (
    POLICY_VERSION as SERIES_REGISTRY_REFERENCE_REPAIR_POLICY_VERSION,
)
from .registry_relationship_repair import (
    POLICY_VERSION as SERIES_REGISTRY_RELATIONSHIP_REPAIR_POLICY_VERSION,
    STAGE_VERSION as SERIES_REGISTRY_RELATIONSHIP_REPAIR_STAGE_VERSION,
    is_relationship_closure_only,
    repair_series_registry_relationships,
)
from autocut_core.io import (
    atomic_write_json,
    json_sha256,
    load_json,
)
from autocut_core.schema.compat import response_format
from autocut_core.semantic.window_analysis_contract import (
    POLICY_VERSION as WINDOW_ANALYSIS_POLICY_VERSION,
)

from .contracts import (
    AssignmentContractResult,
    RegistryRecoveryMergeResult,
    SeriesRegistryAdmissionResult,
    SeriesRegistryAliasRepairResult,
    SeriesRegistryIdentityRepairResult,
    SeriesRegistryReferenceRepairResult,
    SeriesRegistryRelationshipRepairResult,
)

# 延迟导入 — 避免循环依赖
_entry_symbol = None


def _get_entry_symbol(name: str) -> Any:
    global _entry_symbol
    if _entry_symbol is None:
        from autocut_core.semantic.batch_runner import _entry_symbol as _es
        _entry_symbol = _es
    return _entry_symbol(name)


def _direct_evidence_contract_indexes(
    context: dict[str, Any] | None,
) -> tuple[
    dict[str, list[str]],
    dict[str, list[str]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    effective_context = context if isinstance(context, dict) else {}
    contract = effective_context.get("direct_evidence_contract", {})
    if not isinstance(contract, dict):
        contract = {}
    allowed_events_by_thread_beat = {
        str(item["thread_beat_id"]): sorted(
            {
                event_id
                for event_id in item.get("allowed_direct_event_ids", []) or []
                if isinstance(event_id, str) and event_id
            }
        )
        for item in contract.get("thread_beats", []) or []
        if isinstance(item, dict)
        and isinstance(item.get("thread_beat_id"), str)
    }
    if not allowed_events_by_thread_beat:
        allowed_events_by_thread_beat = {
            str(item["id"]): sorted(
                {
                    event_id
                    for event_id in item.get("event_ids", []) or []
                    if isinstance(event_id, str) and event_id
                }
            )
            for item in effective_context.get("thread_beats", []) or []
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
    allowed_thread_beats_by_event: dict[str, list[str]] = {
        str(event_id): sorted(
            {
                thread_beat_id
                for thread_beat_id in thread_beat_ids or []
                if isinstance(thread_beat_id, str) and thread_beat_id
            }
        )
        for event_id, thread_beat_ids in (
            contract.get("allowed_thread_beat_ids_by_event", {}) or {}
        ).items()
        if isinstance(event_id, str) and isinstance(thread_beat_ids, list)
    }
    if not allowed_thread_beats_by_event:
        for thread_beat_id, event_ids in allowed_events_by_thread_beat.items():
            for event_id in event_ids:
                allowed_thread_beats_by_event.setdefault(event_id, []).append(
                    thread_beat_id
                )
        allowed_thread_beats_by_event = {
            event_id: sorted(set(thread_beat_ids))
            for event_id, thread_beat_ids in allowed_thread_beats_by_event.items()
        }
    event_contract_by_id = {
        str(item["event_id"]): item
        for item in contract.get("events", []) or []
        if isinstance(item, dict) and isinstance(item.get("event_id"), str)
    }
    if not event_contract_by_id:
        event_contract_by_id = {
            str(item["id"]): {
                "event_id": item["id"],
                "source_id": item.get("source_id"),
                "source_ranges": item.get("source_ranges", []),
                "timeline_segment_refs": [],
                "allowed_thread_beat_ids": allowed_thread_beats_by_event.get(
                    str(item["id"]), []
                ),
            }
            for item in effective_context.get("events", []) or []
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
    candidate_contract_by_id = {
        str(item["candidate_id"]): item
        for item in contract.get("candidates", []) or []
        if isinstance(item, dict)
        and isinstance(item.get("candidate_id"), str)
    }
    return (
        allowed_events_by_thread_beat,
        allowed_thread_beats_by_event,
        event_contract_by_id,
        candidate_contract_by_id,
    )


def inherit_registry_admission_repairs(
    validation: Any,
    parent: Any,
) -> Any:
    """Carry pre-repair admission metadata onto a repaired Registry."""

    return replace(
        validation,
        registry_alias_repair_result=parent.registry_alias_repair_result,
        registry_reference_repair_result=(
            parent.registry_reference_repair_result
        ),
        registry_recovery_merge_result=parent.registry_recovery_merge_result,
    )


def write_series_assignment_repair_report(
    *,
    output_path: Path,
    job: dict[str, Any],
    result: AssignmentContractResult | None,
) -> Path | None:
    if result is None or not result.repairs:
        return None
    report_root = (
        output_path.parent.parent
        if output_path.parent.name == "series-assignment-results"
        else output_path.parent
    )
    report_dir = report_root / "series-assignment-repairs"
    chapter_id = str(
        result.effective_assignment.get("chapter_id")
        or job.get("id")
        or output_path.stem
    )
    report_path = report_dir / f"{_sanitize_path_component(chapter_id)}.json"
    atomic_write_json(
        report_path,
        {
            "schema_version": "1.0",
            "policy_version": SERIES_ASSIGNMENT_POLICY_VERSION,
            "job_id": job.get("id"),
            "chapter_id": result.effective_assignment.get("chapter_id"),
            "raw_output_sha256": result.raw_sha256,
            "effective_output_sha256": result.effective_sha256,
            "repair_count": len(result.repairs),
            "repairs": result.repairs,
            "blocking_findings": [],
        },
    )
    return report_path


def write_series_registry_relationship_repair_report(
    *,
    output_path: Path,
    job: dict[str, Any],
    result: SeriesRegistryRelationshipRepairResult | None,
) -> Path | None:
    if result is None:
        return None
    report_dir = output_path.parent / "series-registry-repairs"
    report_path = report_dir / f"{result.raw_sha256[:16]}.json"
    atomic_write_json(
        report_path,
        {
            **result.as_report(),
            "job_id": job.get("id"),
        },
    )
    return report_path


def write_series_registry_recovery_merge_report(
    *,
    output_path: Path,
    job: dict[str, Any],
    result: RegistryRecoveryMergeResult | None,
) -> Path | None:
    if result is None or not result.progressed:
        return None
    report_dir = output_path.parent / "series-registry-recovery-merges"
    incoming_sha256 = json_sha256(result.incoming_registry)
    report_path = report_dir / f"{incoming_sha256[:16]}.json"
    atomic_write_json(
        report_path,
        {
            "schema_version": "1.0",
            "job_id": job.get("id"),
            **result.as_audit(),
        },
        private=True,
    )
    return report_path


def write_series_registry_alias_repair_report(
    *,
    output_path: Path,
    job: dict[str, Any],
    result: SeriesRegistryAliasRepairResult | None,
) -> Path | None:
    if result is None or not result.repairs:
        return None
    report_dir = output_path.parent / "series-registry-alias-repairs"
    report_path = report_dir / f"{result.raw_sha256[:16]}.json"
    atomic_write_json(
        report_path,
        {
            **result.as_report(),
            "job_id": job.get("id"),
        },
    )
    return report_path


def write_series_registry_reference_repair_report(
    *,
    output_path: Path,
    job: dict[str, Any],
    result: SeriesRegistryReferenceRepairResult | None,
) -> Path | None:
    if result is None or not result.repairs:
        return None
    report_dir = output_path.parent / "series-registry-reference-repairs"
    report_path = report_dir / f"{result.raw_sha256[:16]}.json"
    atomic_write_json(
        report_path,
        {
            **result.as_report(),
            "job_id": job.get("id"),
        },
    )
    return report_path


def write_series_registry_identity_repair_report(
    *,
    output_path: Path,
    job: dict[str, Any],
    result: SeriesRegistryIdentityRepairResult | None,
) -> Path | None:
    if result is None:
        return None
    report_dir = output_path.parent / "series-registry-identity-repairs"
    report_path = report_dir / f"{result.raw_sha256[:16]}.json"
    atomic_write_json(
        report_path,
        {
            **result.as_report(),
            "job_id": job.get("id"),
        },
        private=True,
    )
    return report_path


def record_contract_repairs(
    *,
    recorder: JobRecorder | None,
    output_path: Path,
    job: dict[str, Any],
    validation: Any,
    request_signature: str | None = None,
) -> Path | None:
    from autocut_core.semantic.window_recovery import (
        write_window_analysis_repair_report,
    )

    admission_result = validation.registry_admission_result
    admission_report_path: Path | None = None
    if admission_result is not None:
        admission_paths = write_series_registry_admission_artifacts(
            output_path=output_path,
            result=admission_result,
            request_signature=request_signature,
        )
        admission_report_path = admission_paths["admission"]
        if recorder is not None and admission_result.repairs:
            recorder.record_local_repair(
                repairs=list(admission_result.repairs),
                raw_sha256=admission_result.raw_sha256,
                effective_sha256=admission_result.effective_sha256,
                report_path=str(admission_report_path),
                policy_version=SERIES_REGISTRY_ADMISSION_POLICY_VERSION,
            )
    alias_result = validation.registry_alias_repair_result
    alias_report_path = write_series_registry_alias_repair_report(
        output_path=output_path,
        job=job,
        result=alias_result,
    )
    if (
        recorder is not None
        and alias_result is not None
        and alias_result.repairs
    ):
        recorder.record_local_repair(
            repairs=list(alias_result.repairs),
            raw_sha256=alias_result.raw_sha256,
            effective_sha256=alias_result.effective_sha256,
            report_path=(
                str(alias_report_path)
                if alias_report_path is not None
                else None
            ),
            policy_version=SERIES_REGISTRY_ALIAS_REPAIR_POLICY_VERSION,
        )

    reference_result = validation.registry_reference_repair_result
    reference_report_path = write_series_registry_reference_repair_report(
        output_path=output_path,
        job=job,
        result=reference_result,
    )
    if (
        recorder is not None
        and reference_result is not None
        and reference_result.repairs
    ):
        recorder.record_local_repair(
            repairs=list(reference_result.repairs),
            raw_sha256=reference_result.raw_sha256,
            effective_sha256=reference_result.effective_sha256,
            report_path=(
                str(reference_report_path)
                if reference_report_path is not None
                else None
            ),
            policy_version=SERIES_REGISTRY_REFERENCE_REPAIR_POLICY_VERSION,
        )

    identity_result = validation.registry_identity_repair_result
    identity_report_path = write_series_registry_identity_repair_report(
        output_path=output_path,
        job=job,
        result=identity_result,
    )
    if (
        recorder is not None
        and identity_result is not None
        and identity_result.repairs
    ):
        recorder.record_local_repair(
            repairs=list(identity_result.repairs),
            raw_sha256=identity_result.raw_sha256,
            effective_sha256=identity_result.effective_sha256,
            report_path=(
                str(identity_report_path)
                if identity_report_path is not None
                else None
            ),
            policy_version=SERIES_REGISTRY_IDENTITY_REPAIR_POLICY_VERSION,
        )

    merge_result = validation.registry_recovery_merge_result
    merge_report_path = write_series_registry_recovery_merge_report(
        output_path=output_path,
        job=job,
        result=merge_result,
    )
    if (
        recorder is not None
        and merge_result is not None
        and merge_result.progressed
    ):
        recorder.record_local_repair(
            repairs=list(merge_result.imported_relationships),
            raw_sha256=json_sha256(merge_result.incoming_registry),
            effective_sha256=json_sha256(
                merge_result.effective_registry
            ),
            report_path=(
                str(merge_report_path)
                if merge_report_path is not None
                else None
            ),
            policy_version=SERIES_REGISTRY_RECOVERY_POLICY_VERSION,
        )

    registry_result = validation.registry_repair_result
    if registry_result is not None:
        report_path = write_series_registry_relationship_repair_report(
            output_path=output_path,
            job=job,
            result=registry_result,
        )
        if recorder is not None and registry_result.repairs:
            recorder.record_local_repair(
                repairs=list(registry_result.repairs),
                raw_sha256=registry_result.raw_sha256,
                effective_sha256=registry_result.effective_sha256,
                report_path=(
                    str(report_path)
                    if report_path is not None
                    else None
                ),
                policy_version=(
                    SERIES_REGISTRY_RELATIONSHIP_REPAIR_POLICY_VERSION
                ),
            )
        return (
            report_path
            or identity_report_path
            or merge_report_path
            or reference_report_path
            or alias_report_path
            or admission_report_path
        )

    if any(
        item is not None
        for item in (
            identity_report_path,
            merge_report_path,
            reference_report_path,
            alias_report_path,
            admission_report_path,
        )
    ):
        return (
            identity_report_path
            or merge_report_path
            or reference_report_path
            or alias_report_path
            or admission_report_path
        )

    window_result = validation.window_contract_result
    if window_result is not None:
        report_path = write_window_analysis_repair_report(
            output_path=output_path,
            job=job,
            result=window_result,
        )
        if (
            recorder is not None
            and window_result.repairs
        ):
            recorder.record_local_repair(
                repairs=window_result.repairs,
                raw_sha256=window_result.raw_sha256,
                effective_sha256=window_result.effective_sha256,
                report_path=(
                    str(report_path)
                    if report_path is not None
                    else None
                ),
                policy_version=WINDOW_ANALYSIS_POLICY_VERSION,
            )
        return report_path

    result = validation.contract_result
    report_path = write_series_assignment_repair_report(
        output_path=output_path,
        job=job,
        result=result,
    )
    if (
        recorder is not None
        and result is not None
        and result.repairs
    ):
        recorder.record_local_repair(
            repairs=result.repairs,
            raw_sha256=result.raw_sha256,
            effective_sha256=result.effective_sha256,
            report_path=str(report_path) if report_path is not None else None,
            policy_version=SERIES_ASSIGNMENT_POLICY_VERSION,
        )
    return report_path


def contract_cache_metadata(
    validation: Any,
) -> tuple[str, dict[str, Any]] | None:
    registry_result = validation.registry_repair_result
    merge_result = validation.registry_recovery_merge_result
    alias_result = validation.registry_alias_repair_result
    reference_result = validation.registry_reference_repair_result
    identity_result = validation.registry_identity_repair_result
    admission_result = validation.registry_admission_result
    if (
        registry_result is not None
        and registry_result.repairs
    ) or (
        alias_result is not None
        and alias_result.repairs
    ) or (
        merge_result is not None
        and merge_result.progressed
    ) or (
        reference_result is not None
        and reference_result.repairs
    ) or (
        identity_result is not None
        and identity_result.repairs
    ) or admission_result is not None:
        raw_sha256 = (
            admission_result.raw_sha256
            if admission_result is not None
            else alias_result.raw_sha256
            if alias_result is not None and alias_result.repairs
            else (
                reference_result.raw_sha256
                if reference_result is not None and reference_result.repairs
                else (
                    registry_result.raw_sha256
                    if registry_result is not None and registry_result.repairs
                    else (
                        identity_result.raw_sha256
                        if identity_result is not None and identity_result.repairs
                        else (
                            json_sha256(merge_result.incoming_registry)
                            if merge_result is not None
                            else json_sha256(validation.value)
                        )
                    )
                )
            )
        )
        effective_sha256 = (
            admission_result.effective_sha256
            if admission_result is not None
            else identity_result.effective_sha256
            if identity_result is not None and identity_result.repairs
            else (
                registry_result.effective_sha256
                if registry_result is not None and registry_result.repairs
                else (
                    json_sha256(merge_result.effective_registry)
                    if merge_result is not None and merge_result.progressed
                    else (
                        reference_result.effective_sha256
                        if reference_result is not None and reference_result.repairs
                        else (
                            alias_result.effective_sha256
                            if alias_result is not None
                            else json_sha256(validation.value)
                        )
                    )
                )
            )
        )
        return (
            "series_registry_contract_repair",
            {
                "alias_policy_version": (
                    SERIES_REGISTRY_ALIAS_REPAIR_POLICY_VERSION
                ),
                "reference_policy_version": (
                    SERIES_REGISTRY_REFERENCE_REPAIR_POLICY_VERSION
                ),
                "identity_policy_version": (
                    SERIES_REGISTRY_IDENTITY_REPAIR_POLICY_VERSION
                ),
                "relationship_policy_version": (
                    SERIES_REGISTRY_RELATIONSHIP_REPAIR_POLICY_VERSION
                ),
                "recovery_policy_version": (
                    SERIES_REGISTRY_RECOVERY_POLICY_VERSION
                ),
                "admission_policy_version": (
                    SERIES_REGISTRY_ADMISSION_POLICY_VERSION
                ),
                "admission_status": (
                    admission_result.status
                    if admission_result is not None
                    else "ready"
                ),
                "admission_sha256": (
                    json_sha256(admission_result.admission)
                    if admission_result is not None
                    else None
                ),
                "quarantine_sha256": (
                    json_sha256(admission_result.quarantine)
                    if admission_result is not None
                    else None
                ),
                "admission_actions": (
                    list(admission_result.repairs)
                    if admission_result is not None
                    else []
                ),
                "raw_output_sha256": raw_sha256,
                "effective_output_sha256": effective_sha256,
                "alias_repairs": (
                    list(alias_result.repairs)
                    if alias_result is not None
                    else []
                ),
                "reference_repairs": (
                    list(reference_result.repairs)
                    if reference_result is not None
                    else []
                ),
                "identity_repairs": (
                    list(identity_result.repairs)
                    if identity_result is not None
                    else []
                ),
                "identity_decisions": (
                    list(identity_result.decisions)
                    if identity_result is not None
                    else []
                ),
                "relationship_repairs": (
                    list(registry_result.repairs)
                    if registry_result is not None
                    else []
                ),
                "recovery_relationship_imports": (
                    list(merge_result.imported_relationships)
                    if merge_result is not None
                    else []
                ),
                "recovery_merge_audit": (
                    merge_result.as_audit()
                    if merge_result is not None
                    else None
                ),
                "decisions": (
                    list(registry_result.decisions)
                    if registry_result is not None
                    else []
                ),
            },
        )
    window_result = validation.window_contract_result
    if window_result is not None and window_result.repairs:
        return (
            "vlm_analysis_contract",
            {
                "policy_version": WINDOW_ANALYSIS_POLICY_VERSION,
                "quality_status": window_result.quality_status,
                "raw_output_sha256": window_result.raw_sha256,
                "effective_output_sha256": window_result.effective_sha256,
                "repairs": window_result.repairs,
            },
        )
    result = validation.contract_result
    if result is not None and result.repairs:
        return (
            "series_assignment_contract",
            {
                "policy_version": SERIES_ASSIGNMENT_POLICY_VERSION,
                "raw_output_sha256": result.raw_sha256,
                "effective_output_sha256": result.effective_sha256,
                "repairs": result.repairs,
            },
        )
    return None


def rebuild_cached_registry_admission(
    *,
    value: dict[str, Any],
    cached: dict[str, Any],
    context: dict[str, Any],
) -> SeriesRegistryAdmissionResult | None:
    """Recompile and authenticate a cached raw Registry projection.

    Partial admission deliberately caches the unprojected Registry so policy
    changes never destroy evidence.  A cache hit therefore cannot stop at the
    ordinary relationship-closure result; it must deterministically rebuild
    the admitted core and companions, then match every hash recorded when the
    cache object was created.
    """

    metadata = cached.get("series_registry_contract_repair")
    if not isinstance(metadata, dict):
        return None
    if metadata.get("admission_policy_version") != (
        SERIES_REGISTRY_ADMISSION_POLICY_VERSION
    ):
        return None
    if metadata.get("admission_status") not in {"ready", "partially_ready"}:
        return None
    actions = metadata.get("admission_actions", [])
    result = compile_series_registry_admission(
        value,
        event_index=[
            item
            for item in context.get("event_index", [])
            if isinstance(item, dict)
        ],
        relationship_decisions=(actions if isinstance(actions, list) else []),
    )
    if not result.ok:
        return None
    expected_hashes = {
        "raw_output_sha256": result.raw_sha256,
        "effective_output_sha256": result.effective_sha256,
        "admission_sha256": json_sha256(result.admission),
        "quarantine_sha256": json_sha256(result.quarantine),
    }
    if any(metadata.get(key) != expected for key, expected in expected_hashes.items()):
        return None
    return result


def attempt_series_registry_relationship_repair(
    registry: dict[str, Any],
    *,
    event_index: list[dict[str, Any]],
    parent_job: dict[str, Any],
    parent_context: dict[str, Any],
    parent_response_format: dict[str, Any],
    parent_signature: str,
    output_path: Path,
    backend_name: str,
    cache_dir: Path,
    max_context_chars: int,
    max_inline_mb: float,
    temperature: float,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
    dry_run: bool,
    concurrency: AdaptiveConcurrencyController | None,
    ledger: AttemptLedger | None,
    semantic_retries: int,
) -> tuple[
    Any | None,
    SeriesRegistryRelationshipRepairResult,
]:
    """Run evidence-narrowed relationship jobs against a frozen Registry."""

    repair_root = (
        output_path.parent
        / "intermediate"
        / "series-registry-relationship-repairs"
        / parent_signature[:16]
        / json_sha256(registry)[:16]
    )

    def resolver(
        repair_job_id: str,
        repair_context: dict[str, Any],
        repair_schema: dict[str, Any],
    ) -> dict[str, Any]:
        safe_id = _sanitize_path_component(repair_job_id)
        context_path = repair_root / "contexts" / f"{safe_id}.json"
        repair_output_path = repair_root / "results" / f"{safe_id}.json"
        atomic_write_json(context_path, repair_context, private=True)
        repair_job = {
            "id": repair_job_id,
            "task": "series_registry_relationship_repair",
            "stage_version": SERIES_REGISTRY_RELATIONSHIP_REPAIR_STAGE_VERSION,
            "context_file": str(context_path.resolve()),
            "output": str(repair_output_path.resolve()),
            "max_tokens": 4096,
            "response_format": response_format(
                "series_registry_relationship_repair",
                schema_override=repair_schema,
                revision_override="v3_direct_evidence_review_lock",
            ),
        }
        _get_entry_symbol("run_job")(
            repair_job,
            backend_name=backend_name,
            cache_dir=cache_dir,
            max_context_chars=max_context_chars,
            max_inline_mb=max_inline_mb,
            temperature=temperature,
            max_tokens=4096,
            timeout=timeout,
            retries=retries,
            limiter=limiter,
            dry_run=dry_run,
            concurrency=concurrency,
            ledger=ledger,
            semantic_retries=semantic_retries,
        )
        if dry_run:
            raise RuntimeError(
                "relationship repair cannot resolve during dry-run"
            )
        return load_json(repair_output_path)

    result = repair_series_registry_relationships(
        registry,
        event_index=event_index,
        resolver=resolver,
    )
    validation: Any = None
    if result.ok:
        validation = _get_entry_symbol("validate_and_canonicalize_job_response")(
            "series_registry",
            result.effective_registry,
            parent_response_format,
            parent_job,
            parent_context,
        )
    if validation is not None and validation.errors:
        result = replace(
            result,
            errors=tuple(validation.errors),
        )
        validation = None
    write_series_registry_relationship_repair_report(
        output_path=output_path,
        job=parent_job,
        result=result,
    )
    if not result.ok or validation is None:
        return None, result
    return (
        replace(validation, registry_repair_result=result),
        result,
    )


def attempt_series_registry_identity_repair(
    registry: dict[str, Any],
    *,
    relationship_decisions: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    event_index: list[dict[str, Any]],
    parent_job: dict[str, Any],
    parent_context: dict[str, Any],
    parent_response_format: dict[str, Any],
    parent_signature: str,
    output_path: Path,
    backend_name: str,
    cache_dir: Path,
    max_context_chars: int,
    max_inline_mb: float,
    temperature: float,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
    dry_run: bool,
    concurrency: AdaptiveConcurrencyController | None,
    ledger: AttemptLedger | None,
    semantic_retries: int,
) -> tuple[
    Any | None,
    SeriesRegistryIdentityRepairResult,
]:
    """Resolve reviewed relationship dead ends as identity defects."""

    repair_root = (
        output_path.parent
        / "intermediate"
        / "series-registry-identity-repairs"
        / parent_signature[:16]
        / json_sha256(registry)[:16]
    )

    def resolver(
        repair_job_id: str,
        repair_context: dict[str, Any],
        repair_schema: dict[str, Any],
    ) -> dict[str, Any]:
        safe_id = _sanitize_path_component(repair_job_id)
        context_path = repair_root / "contexts" / f"{safe_id}.json"
        repair_output_path = repair_root / "results" / f"{safe_id}.json"
        atomic_write_json(context_path, repair_context, private=True)
        repair_job = {
            "id": repair_job_id,
            "task": "series_registry_identity_audit",
            "stage_version": SERIES_REGISTRY_IDENTITY_REPAIR_STAGE_VERSION,
            "context_file": str(context_path.resolve()),
            "output": str(repair_output_path.resolve()),
            "max_tokens": 4096,
            "response_format": response_format(
                "series_registry_identity_audit",
                schema_override=repair_schema,
                revision_override="v1_evidence_gated",
            ),
        }
        _get_entry_symbol("run_job")(
            repair_job,
            backend_name=backend_name,
            cache_dir=cache_dir,
            max_context_chars=max_context_chars,
            max_inline_mb=max_inline_mb,
            temperature=temperature,
            max_tokens=4096,
            timeout=timeout,
            retries=retries,
            limiter=limiter,
            dry_run=dry_run,
            concurrency=concurrency,
            ledger=ledger,
            semantic_retries=semantic_retries,
        )
        if dry_run:
            raise RuntimeError("identity repair cannot resolve during dry-run")
        return load_json(repair_output_path)

    result = repair_series_registry_identities(
        registry,
        event_index=event_index,
        relationship_decisions=relationship_decisions,
        resolver=resolver,
    )
    validation: Any = None
    if result.ok:
        validation = _get_entry_symbol("validate_and_canonicalize_job_response")(
            "series_registry",
            result.effective_registry,
            parent_response_format,
            parent_job,
            parent_context,
        )
    if validation is not None and validation.errors:
        result = replace(result, errors=tuple(validation.errors))
        validation = None
    write_series_registry_identity_repair_report(
        output_path=output_path,
        job=parent_job,
        result=result,
    )
    if not result.ok or validation is None:
        return None, result
    return (
        replace(validation, registry_identity_repair_result=result),
        result,
    )