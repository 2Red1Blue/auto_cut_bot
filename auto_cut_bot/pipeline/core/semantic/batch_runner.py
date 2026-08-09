"""语义批量运行器 — 从 semantic_handlers.py 提取的 run_job 核心 + 辅助函数。

原位置: semantic_handlers.py, 4 funcs, ~1495L
包含: run_job (LLM 编排核心), _entry_symbol (动态调度), _beat_direct_event_ids, semantic_validation_error_summary
"""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from autocut_core.backends._base import MULTIMODAL_TASKS, TEXT_TASKS, get_backend
from autocut_core.semantic.engine import (
    ERROR_KIND_IDENTITY,
    ERROR_KIND_NON_JSON,
    ERROR_KIND_SCHEMA,
    ERROR_KIND_SEMANTIC_CONTRACT,
    JUNCTION_CONTENT_SIGNATURE_VERSION,
    SYSTEM_PROMPT,
    AdaptiveConcurrencyController,
    AttemptLedger,
    JobRecorder,
    RateLimiter,
    _sanitize_path_component,
    call_provider,
    parse_model_json,
    sanitize_url,
)
from autocut_core.io import (
    atomic_write_json,
    json_sha256,
    load_json,
    sha256_file,
    utc_now,
)
from autocut_core.schema.compat import (
    SCHEMAS,
    STORY_SCRIPT_HIGHLIGHT_SELECTION_PROMPT_VERSION,
    WINDOW_ANALYSIS_PROMPT_VERSION,
    response_format,
    task_prompt,
    validate_schema,
    validate_task_response,
)
from autocut_core.semantic.window_analysis_contract import (
    POLICY_VERSION as WINDOW_ANALYSIS_POLICY_VERSION,
    canonicalize_window_analysis,
    supports_local_window_media_recovery,
)
from autocut_core.semantic.window_media_recovery import (
    WindowMediaRecoveryError,
    build_window_media_recovery_job,
    load_window_media_recovery_report,
    mark_window_media_recovery_outcome,
    prepare_window_media_artifact,
)


def _entry_symbol(name: str) -> Any:
    """通过 ``run_semantic_batch`` 入口命名空间解析 *name*。

    测试套件会在 ``run_semantic_batch`` 上打补丁 (``call_provider``、
    ``validate_identity``、prompt 版本常量等)。经入口模块解析这些调用点,
    可使补丁在拆分后仍然生效; 单独导入本模块时透明回退到本地定义。
    """
    entry = sys.modules.get("run_semantic_batch")
    if entry is not None:
        return getattr(entry, name)
    # Fallback: check semantic_handlers module (shim), then local globals
    sh = sys.modules.get("semantic_handlers")
    if sh is not None and hasattr(sh, name):
        return getattr(sh, name)
    # Last resort: try autocut_core.semantic.types for class definitions
    from autocut_core.semantic import types as _types
    if hasattr(_types, name):
        return getattr(_types, name)
    return globals()[name]


def _beat_direct_event_ids(beat: dict[str, Any]) -> list[str]:
    retrieval = beat.get("retrieval_requirements", {})
    if not isinstance(retrieval, dict):
        retrieval = {}
    return sorted(
        {
            event_id
            for event_id in [
                *(beat.get("event_ids", []) or []),
                *(retrieval.get("event_ids", []) or []),
                *[
                    event_id
                    for must_show in beat.get("must_show", []) or []
                    if isinstance(must_show, dict)
                    for event_id in must_show.get("evidence_event_ids", []) or []
                ],
            ]
            if isinstance(event_id, str) and event_id
        }
    )


def semantic_validation_error_summary(
    validation: Any,
    *,
    errors: list[str] | None = None,
) -> str:
    from autocut_core.semantic.types import JobResponseValidation

    effective_errors = validation.errors if errors is None else errors
    window_result = validation.window_contract_result
    if window_result is not None:
        eligible = (
            not validation.schema_errors
            and not validation.identity_errors
            and supports_local_window_media_recovery(window_result)
        )
        return json.dumps(
            {
                "repair_route": (
                    "local_window_media" if eligible else "window_analysis"
                ),
                "failure_codes": sorted(
                    {
                        str(item.get("code"))
                        for item in window_result.blockers
                        if item.get("code")
                    }
                ),
                "blockers": window_result.blockers,
                "errors": effective_errors[:20],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    admission = validation.story_script_admission
    if admission is None:
        return "; ".join(effective_errors[:20])
    return json.dumps(
        {
            "repair_route": admission.repair_route,
            "failure_codes": admission.failure_codes,
            "feasibility_status": admission.feasibility_status,
            "errors": effective_errors[:20],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def run_job(
    job: dict[str, Any],
    *,
    backend_name: str,
    cache_dir: Path,
    max_context_chars: int,
    max_inline_mb: float,
    temperature: float,
    max_tokens: int,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
    dry_run: bool,
    concurrency: AdaptiveConcurrencyController | None = None,
    ledger: AttemptLedger | None = None,
    semantic_retries: int = 1,
    prior_failure_error: str | None = None,
    context_injection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行单个语义任务的完整编排: 校验 → 组装请求 → 调用 provider →
    解析与校验响应 → 失败时按任务策略重试/修复 → 写入缓存与输出文件。

    返回含 status/id/output 的结果字典; 语义拒收时抛
    StoryScriptSemanticRejection / WindowAnalysisSemanticRejection。
    """
    # Lazy imports from new modules (no more references to old semantic_handlers)
    from autocut_core.semantic.types import (
        JobResponseValidation,
        StoryScriptAdmissionResult,
        StoryScriptSemanticRejection,
        WindowAnalysisSemanticRejection,
    )
    from autocut_core.semantic.request import (
        build_request,
        identity_prompt,
        load_context,
        media_item,
        request_signature_media_identity,
        response_format_for_job,
        validate_identity,
    )
    from autocut_core.semantic.response_validation import (
        compaction_retry_projection,
        validate_job_response,
    )
    from autocut_core.semantic.story_logic import (
        TREATMENT_RETRY_POLICY_VERSION,
        STORY_SCRIPT_COMPILE_REPAIR_FAILURE_CODES,
        STORY_SCRIPT_COMPILE_REPAIR_LIMIT,
        STORY_SCRIPT_COMPILE_REPAIR_PROCESS_CODES,
        STORY_SCRIPT_COMPILE_REPAIR_RESPONSE_SCHEMA_VERSION,
        STORY_SCRIPT_ORDINARY_SEMANTIC_RETRY_LIMIT,
        TREATMENT_RETRY_IN_PLACE_FAILURE_CODES,
        apply_story_script_preservation_validation,
        merge_story_script_compile_replacements,
        mismatch_retry_projection,
        semantic_retry_payload,
        story_script_compile_failure_beat_ids,
        story_script_compile_failure_signature,
        story_script_compile_repair_codes,
        story_script_compile_repair_payload,
        story_script_compile_replacement_rejection,
        story_script_preservation_contract,
        _story_script_compile_max_beats,
    )
    from autocut_core.semantic.utils import semantic_diagnostic_path
    from autocut_core.semantic.registry import (
        attempt_series_registry_identity_repair,
        attempt_series_registry_relationship_repair,
        contract_cache_metadata,
        inherit_registry_admission_repairs,
        rebuild_cached_registry_admission,
        record_contract_repairs,
    )
    from autocut_core.semantic.story_logic import (
        treatment_attempt_audit_path,
        treatment_attempt_record,
        treatment_retry_viability_from_attempt,
        treatment_retry_viability_from_validation,
        write_treatment_attempt_audit,
    )
    from .registry_admission import (
        compile_series_registry_admission,
    )
    from .registry_identity_repair import (
        repair_series_registry_identities,
    )
    from .registry_recovery import (
        load_registry_recovery_candidate,
        load_registry_recovery_state,
        merge_registry_relationship_progress,
        persist_registry_recovery_candidate,
    )
    from .registry_relationship_repair import (
        is_relationship_closure_only,
    )

    from .contracts import (
        RegistryRecoveryMergeResult,
        SeriesRegistryIdentityRepairResult,
        SeriesRegistryRelationshipRepairResult,
    )

    recovery_preflight_error = job.get("_window_media_recovery_preflight_error")
    if isinstance(recovery_preflight_error, dict):
        raise WindowMediaRecoveryError(
            str(
                recovery_preflight_error.get("code")
                or "WINDOW_RECOVERY_PREFLIGHT_FAILED"
            ),
            str(recovery_preflight_error.get("detail") or "recovery preflight failed"),
        )
    task = job.get("task")
    if task not in set(SCHEMAS):
        raise ValueError(f"unsupported task {task!r}")
    if task in MULTIMODAL_TASKS and not (job.get("media_file") or job.get("media_url")):
        raise ValueError(f"{task} job requires media")
    if (
        task in TEXT_TASKS
        and task != "story_plan_selection"
        and (job.get("media_file") or job.get("media_url"))
    ):
        raise ValueError(f"{task} job must be text-only")
    context, context_text = load_context(job, max_context_chars)
    response_format_value = response_format_for_job(task, job)
    media, media_identity = media_item(
        job, max_inline_mb, encode_payload=not dry_run
    )
    effective_max_tokens = job.get("max_tokens", max_tokens)
    if (
        not isinstance(effective_max_tokens, int)
        or isinstance(effective_max_tokens, bool)
        or effective_max_tokens <= 0
    ):
        raise ValueError("job.max_tokens must be a positive integer")
    if (
        not isinstance(semantic_retries, int)
        or isinstance(semantic_retries, bool)
        or not 0 <= semantic_retries <= 5
    ):
        raise ValueError("semantic_retries must be an integer in 0..5")
    ordinary_semantic_retry_limit = semantic_retries
    effective_semantic_retries = semantic_retries
    compile_repair_limit = 0
    if task == "story_script_draft":
        ordinary_semantic_retry_limit = min(
            semantic_retries,
            STORY_SCRIPT_ORDINARY_SEMANTIC_RETRY_LIMIT,
        )
        compile_repair_limit = (
            STORY_SCRIPT_COMPILE_REPAIR_LIMIT
            if semantic_retries > 0
            else 0
        )
        effective_semantic_retries = (
            ordinary_semantic_retry_limit + compile_repair_limit
        )
    if prior_failure_error is not None and not isinstance(
        prior_failure_error,
        str,
    ):
        raise ValueError("prior_failure_error must be a string or None")
    payload, backend = build_request(
        backend_name,
        task,
        job,
        context,
        context_text,
        media,
        response_format_value,
        temperature=temperature,
        max_tokens=effective_max_tokens,
        context_injection=context_injection,
    )
    recovery_metadata = job.get("window_media_recovery")
    recovery_identity = None
    signature_media_identity = request_signature_media_identity(
        job, context, media_identity
    )
    if isinstance(recovery_metadata, dict):
        recovery_identity = {
            "policy_version": recovery_metadata.get("policy_version"),
            "original_job_identity": recovery_metadata.get(
                "original_job_identity"
            ),
            "source_sha256": recovery_metadata.get("source_sha256"),
            "clip_sha256": recovery_metadata.get("clip_sha256"),
            "start": recovery_metadata.get("start"),
            "end": recovery_metadata.get("end"),
        }
        signature_media_identity.pop("reference", None)
    signature_payload = {
        "stage_version": job.get("stage_version"),
        "task": task,
        "model": payload["model"],
        "context_sha256": json_sha256(context),
        "media_identity": signature_media_identity,
        "window_media_recovery": recovery_identity,
        "provider_extra_headers": dict(backend.extra_headers),
        "response_schema": payload["response_format"],
        "temperature": temperature,
        "max_tokens": payload["max_tokens"],
        **(
            {"prompt_contract_version": _entry_symbol("WINDOW_ANALYSIS_PROMPT_VERSION")}
            if task == "window_analysis"
            else {}
        ),
        **(
            {
                "story_script_prompt_contract_version": (
                    _entry_symbol("STORY_SCRIPT_HIGHLIGHT_SELECTION_PROMPT_VERSION")
                )
            }
            if task == "story_script_draft"
            else {}
        ),
        **(
            {
                "treatment_retry_policy_version": (
                    TREATMENT_RETRY_POLICY_VERSION
                )
            }
            if task == "story_script_draft"
            else {}
        ),
    }
    signature = json_sha256(signature_payload)
    if isinstance(recovery_metadata, dict):
        recovery_metadata["_request_signature"] = signature
    recorder = (
        ledger.begin_job(
            job_id=str(job.get("id") or "unknown"),
            task=str(task),
            signature=signature,
        )
        if ledger is not None
        else None
    )
    if isinstance(recovery_metadata, dict):
        mark_window_media_recovery_outcome(
            job,
            status="running",
            request_signature=signature,
            attempt_ledger_invocation_id=(
                recorder.invocation_id if recorder is not None else None
            ),
        )
    object_path = cache_dir / "objects" / signature[:2] / f"{signature}.json"
    output_value = job.get("output")
    if not isinstance(output_value, str):
        if recorder is not None:
            recorder.finalize("failed")
        raise ValueError("job.output is required")
    output_path = Path(output_value).expanduser().resolve()
    treatment_attempts: list[dict[str, Any]] = []
    treatment_audit_path: Path | None = None
    if task == "story_script_draft":
        story_id = str(
            context.get("story", {}).get("story_id")
            or job.get("id")
            or output_path.stem
        )
        treatment_audit_path = treatment_attempt_audit_path(
            output_path,
            story_id=story_id,
        )
        if treatment_audit_path.is_file():
            existing_audit = load_json(treatment_audit_path)
            treatment_attempts = next(
                (
                    list(item.get("attempts", []))
                    for item in existing_audit.get("generations", [])
                    if isinstance(item, dict)
                    and item.get("request_signature") == signature
                ),
                [],
            )
    if object_path.is_file():
        cached = load_json(object_path)
        value = cached.get("analysis") if isinstance(cached, dict) else None
        if isinstance(value, dict):
            validation = _entry_symbol("validate_and_canonicalize_job_response")(
                task,
                value,
                response_format_value,
                job,
                context,
            )
            if task == "series_registry" and validation.errors:
                cached_admission = rebuild_cached_registry_admission(
                    value=validation.value,
                    cached=cached,
                    context=context,
                )
                if cached_admission is not None:
                    validation = JobResponseValidation(
                        value=cached_admission.effective_registry,
                        schema_errors=[],
                        identity_errors=[],
                        contract_errors=[],
                        registry_alias_repair_result=(
                            validation.registry_alias_repair_result
                        ),
                        registry_reference_repair_result=(
                            validation.registry_reference_repair_result
                        ),
                        registry_admission_result=cached_admission,
                    )
            if not validation.errors:
                if task == "story_script_draft" and not treatment_attempts:
                    treatment_attempts.append(
                        treatment_attempt_record(
                            validation=validation,
                            semantic_attempt=1,
                            context=context,
                        )
                    )
                treatment_audit_path = write_treatment_attempt_audit(
                    output_path=output_path,
                    context=context,
                    job=job,
                    signature=signature,
                    attempts=treatment_attempts,
                    final_status="accepted_cache_hit",
                )
                cache_contract = contract_cache_metadata(validation)
                if cache_contract is not None:
                    metadata_key, metadata_value = cache_contract
                    cached["analysis"] = (
                        validation.registry_admission_result.raw_registry
                        if validation.registry_admission_result is not None
                        else validation.value
                    )
                    cached[metadata_key] = metadata_value
                    atomic_write_json(object_path, cached)
                atomic_write_json(output_path, validation.value)
                if isinstance(recovery_metadata, dict):
                    mark_window_media_recovery_outcome(
                        job,
                        status="succeeded",
                        request_signature=signature,
                        output_sha256=sha256_file(output_path),
                        attempt_ledger_invocation_id=(
                            recorder.invocation_id if recorder is not None else None
                        ),
                    )
                if recorder is not None:
                    recorder.record_cache_hit(signature)
                repair_report_path = record_contract_repairs(
                    recorder=recorder,
                    output_path=output_path,
                    job=job,
                    validation=validation,
                    request_signature=signature,
                )
                if recorder is not None:
                    recorder.finalize("succeeded")
                return {
                    "id": job.get("id"),
                    "task": task,
                    "status": "cache_hit",
                    "output": str(output_path),
                    "signature": signature,
                    "quality_status": validation.quality_status,
                    "local_repair_count": len(
                        validation.local_repairs
                    ),
                    "repair_report": (
                        str(repair_report_path)
                        if repair_report_path is not None
                        else None
                    ),
                    "treatment_attempt_audit": (
                        str(treatment_audit_path)
                        if treatment_audit_path is not None
                        else None
                    ),
                    "media_recovery_report": (
                        recovery_metadata.get("report_path")
                        if isinstance(recovery_metadata, dict)
                        else None
                    ),
                }
            if recorder is not None:
                if validation.schema_errors:
                    recorder.record_semantic_error(
                        error_kind=ERROR_KIND_SCHEMA,
                        errors=validation.schema_errors,
                    )
                if validation.identity_errors:
                    recorder.record_semantic_error(
                        error_kind=ERROR_KIND_IDENTITY,
                        errors=validation.identity_errors,
                    )
                if validation.contract_errors:
                    recorder.record_semantic_error(
                        error_kind=ERROR_KIND_SEMANTIC_CONTRACT,
                        errors=validation.contract_errors,
                    )
    if dry_run:
        if recorder is not None:
            recorder.finalize("dry_run")
        return {
            "id": job.get("id"),
            "task": task,
            "status": "dry_run",
            "output": str(output_path),
            "signature": signature,
            "model": payload["model"],
            "context_chars": len(context_text),
            "media": media_identity,
        }
    synthetic_marker = None
    if job.get("synthetic_selection"):
        synthetic_marker = "synthetic_selection"
    elif job.get("synthetic_digest"):
        synthetic_marker = "synthetic_digest"
    if synthetic_marker is not None:
        if not output_path.is_file():
            if recorder is not None:
                recorder.finalize("failed")
            raise FileNotFoundError(
                f"{synthetic_marker} job {job.get('id')!r} references "
                f"missing output file {output_path}"
            )
        value = load_json(output_path)
        validation = _entry_symbol("validate_and_canonicalize_job_response")(
            task,
            value,
            response_format_value,
            job,
            context,
        )
        if validation.errors:
            if recorder is not None:
                recorder.record_semantic_error(
                    error_kind=ERROR_KIND_SCHEMA,
                    errors=validation.errors,
                )
                recorder.finalize("failed")
            raise ValueError(
                f"{synthetic_marker} failed post-write validation: "
                + "; ".join(validation.errors[:20])
            )
        atomic_write_json(output_path, validation.value)
        repair_report_path = record_contract_repairs(
            recorder=recorder,
            output_path=output_path,
            job=job,
            validation=validation,
            request_signature=signature,
        )
        if recorder is not None:
            recorder.finalize("succeeded")
        return {
            "id": job.get("id"),
            "task": task,
            "status": synthetic_marker,
            "output": str(output_path),
            "signature": signature,
            "quality_status": validation.quality_status,
            "local_repair_count": len(validation.local_repairs),
            "repair_report": (
                str(repair_report_path)
                if repair_report_path is not None
                else None
            ),
        }
    request_payload = payload
    if prior_failure_error and prior_failure_error.strip():
        prior_treatment_attempt = (
            treatment_attempts[-1]
            if task == "story_script_draft" and treatment_attempts
            else None
        )
        request_payload = semantic_retry_payload(
            payload,
            error_summary=prior_failure_error,
            treatment_viability=(
                treatment_retry_viability_from_attempt(
                    prior_treatment_attempt
                )
                if task == "story_script_draft"
                else None
            ),
            treatment_attempts=(
                treatment_attempts
                if task == "story_script_draft"
                else None
            ),
            compaction_beat_ids=(
                prior_treatment_attempt.get(
                    "split_regeneration_beat_ids", []
                )
                if task == "story_script_draft"
                and isinstance(prior_treatment_attempt, dict)
                else None
            ),
            compaction_projection=(
                prior_treatment_attempt.get(
                    "compaction_retry_projection", []
                )
                if task == "story_script_draft"
                and isinstance(prior_treatment_attempt, dict)
                else None
            ),
            mismatch_projection=(
                prior_treatment_attempt.get(
                    "mismatch_retry_projection", []
                )
                if task == "story_script_draft"
                and isinstance(prior_treatment_attempt, dict)
                else None
            ),
            context=context,
            compile_preservation_contract=(
                prior_treatment_attempt.get("preservation_contract")
                if task == "story_script_draft"
                and isinstance(prior_treatment_attempt, dict)
                and prior_treatment_attempt.get("failure_class")
                == "compile_only"
                else None
            ),
        )
    value: dict[str, Any] | None = None
    accepted_validation: Any = None
    last_semantic_error: Exception | None = None
    last_validation: Any = None
    prior_compile_attempt = (
        treatment_attempts[-1]
        if task == "story_script_draft"
        and treatment_attempts
        and treatment_attempts[-1].get("failure_class") == "compile_only"
        else None
    )
    compile_preservation_contract: dict[str, Any] | None = (
        copy.deepcopy(prior_compile_attempt.get("preservation_contract"))
        if isinstance(prior_compile_attempt, dict)
        and isinstance(
            prior_compile_attempt.get("preservation_contract"), dict
        )
        else None
    )
    previous_compile_failure_signature = (
        str(prior_compile_attempt.get("compile_failure_signature") or "")
        if isinstance(prior_compile_attempt, dict)
        else ""
    )
    compile_repair_stop_reason = ""
    ordinary_retry_used = 0
    compile_repair_used = 0
    current_request_phase = (
        "ordinary_retry"
        if prior_failure_error and prior_failure_error.strip()
        else "initial"
    )
    compile_base_script: dict[str, Any] | None = None
    compile_failed_beat_ids: list[str] = []
    current_compile_merge_audit: dict[str, Any] | None = None
    if task == "series_registry":
        registry_event_index = [
            item
            for item in context.get("event_index", [])
            if isinstance(item, dict)
        ]
        recovery_candidate = load_registry_recovery_candidate(
            output_path=output_path,
            signature=signature,
        )
        if recovery_candidate is not None:
            candidate_validation = _entry_symbol("validate_and_canonicalize_job_response")(
                task,
                recovery_candidate,
                response_format_value,
                job,
                context,
            )
            if (
                not candidate_validation.schema_errors
                and not candidate_validation.identity_errors
                and is_relationship_closure_only(
                    candidate_validation.value
                )
            ):
                recovery_repair_result: (
                    SeriesRegistryRelationshipRepairResult | None
                ) = None
                try:
                    repaired_validation, recovery_repair_result = (
                        attempt_series_registry_relationship_repair(
                            candidate_validation.value,
                            event_index=registry_event_index,
                            parent_job=job,
                            parent_context=context,
                            parent_response_format=response_format_value,
                            parent_signature=signature,
                            output_path=output_path,
                            backend_name=backend_name,
                            cache_dir=cache_dir,
                            max_context_chars=max_context_chars,
                            max_inline_mb=max_inline_mb,
                            temperature=temperature,
                            timeout=timeout,
                            retries=retries,
                            limiter=limiter,
                            dry_run=dry_run,
                            concurrency=concurrency,
                            ledger=ledger,
                            semantic_retries=semantic_retries,
                        )
                    )
                except Exception:
                    repaired_validation = None
                if repaired_validation is not None:
                    repaired_validation = inherit_registry_admission_repairs(
                        repaired_validation,
                        candidate_validation,
                    )
                    value = repaired_validation.value
                    accepted_validation = repaired_validation
                elif recovery_repair_result is not None:
                    identity_validation: Any = None
                    identity_result: (
                        SeriesRegistryIdentityRepairResult | None
                    ) = None
                    try:
                        identity_validation, identity_result = (
                            attempt_series_registry_identity_repair(
                                recovery_repair_result.effective_registry,
                                relationship_decisions=(
                                    recovery_repair_result.decisions
                                ),
                                event_index=registry_event_index,
                                parent_job=job,
                                parent_context=context,
                                parent_response_format=response_format_value,
                                parent_signature=signature,
                                output_path=output_path,
                                backend_name=backend_name,
                                cache_dir=cache_dir,
                                max_context_chars=max_context_chars,
                                max_inline_mb=max_inline_mb,
                                temperature=temperature,
                                timeout=timeout,
                                retries=retries,
                                limiter=limiter,
                                dry_run=dry_run,
                                concurrency=concurrency,
                                ledger=ledger,
                                semantic_retries=semantic_retries,
                            )
                        )
                    except Exception:
                        identity_validation = None
                    if identity_validation is not None:
                        identity_validation = inherit_registry_admission_repairs(
                            identity_validation,
                            candidate_validation,
                        )
                        identity_validation = replace(
                            identity_validation,
                            registry_repair_result=recovery_repair_result,
                        )
                        value = identity_validation.value
                        accepted_validation = identity_validation
                    else:
                        persist_registry_recovery_candidate(
                            recovery_repair_result.effective_registry,
                            output_path=output_path,
                            signature=signature,
                            errors=recovery_repair_result.errors,
                            event_index=registry_event_index,
                            source="filtered_recovery_relationship_repair",
                            repairs=recovery_repair_result.repairs,
                            decisions=recovery_repair_result.decisions,
                        )
                        recovery_errors = (
                            identity_result.errors
                            if identity_result is not None
                            else recovery_repair_result.errors
                        )
                        if recovery_errors:
                            recovery_error_summary = "; ".join(
                                recovery_errors[:20]
                            )
                            request_payload = semantic_retry_payload(
                                payload,
                                error_summary=recovery_error_summary,
                            )
                            last_semantic_error = ValueError(
                                recovery_error_summary
                            )

    for semantic_attempt in (
        range(effective_semantic_retries + 1) if value is None else ()
    ):
        try:
            response = _entry_symbol("call_provider")(
                backend,
                request_payload,
                timeout=timeout,
                retries=retries,
                limiter=limiter,
                concurrency=(
                    concurrency
                    if concurrency is not None
                    else AdaptiveConcurrencyController(1)
                ),
                recorder=recorder,
            )
        except Exception:
            if task == "story_script_draft" and treatment_attempts:
                write_treatment_attempt_audit(
                    output_path=output_path,
                    context=context,
                    job=job,
                    signature=signature,
                    attempts=treatment_attempts,
                    final_status="transport_failed",
                )
            if recorder is not None:
                recorder.finalize("failed")
            raise
        if current_request_phase == "compile_repair":
            compile_repair_used += 1
        try:
            parsed_value = parse_model_json(response)
        except Exception as exc:
            last_semantic_error = exc
            if recorder is not None:
                recorder.record_semantic_error(
                    error_kind=ERROR_KIND_NON_JSON,
                    errors=[str(exc)],
                )
            if current_request_phase == "compile_repair":
                parse_validation = story_script_compile_replacement_rejection(
                    value=(
                        compile_base_script
                        if isinstance(compile_base_script, dict)
                        else {}
                    ),
                    errors=[f"model response is not valid JSON: {exc}"],
                    failed_beat_ids=compile_failed_beat_ids,
                )
                last_validation = parse_validation
                parse_record = treatment_attempt_record(
                    validation=parse_validation,
                    semantic_attempt=len(treatment_attempts) + 1,
                    context=context,
                    preservation_contract=compile_preservation_contract,
                    request_phase=current_request_phase,
                    ordinary_retry_used=ordinary_retry_used,
                    compile_repair_used=compile_repair_used,
                )
                treatment_attempts.append(parse_record)
                previous_compile_failure_signature = str(
                    parse_record.get("compile_failure_signature") or ""
                )
                if compile_repair_used >= compile_repair_limit:
                    compile_repair_stop_reason = "exhausted"
                    break
                if (
                    not isinstance(compile_base_script, dict)
                    or not compile_failed_beat_ids
                    or not isinstance(compile_preservation_contract, dict)
                ):
                    compile_repair_stop_reason = "invalid_state"
                    break
                request_payload = story_script_compile_repair_payload(
                    payload,
                    base_script=compile_base_script,
                    failed_beat_ids=compile_failed_beat_ids,
                    error_summary=str(exc),
                    context=context,
                    compile_preservation_contract=(
                        compile_preservation_contract
                    ),
                    compaction_projection=parse_record.get(
                        "compaction_retry_projection", []
                    ),
                    mismatch_projection=parse_record.get(
                        "mismatch_retry_projection", []
                    ),
                    repair_round=compile_repair_used + 1,
                    response_format_value=response_format_value,
                )
            else:
                if ordinary_retry_used >= ordinary_semantic_retry_limit:
                    break
                ordinary_retry_used += 1
                current_request_phase = "ordinary_retry"
                request_payload = semantic_retry_payload(
                    payload,
                    error_summary=str(exc),
                    context=context,
                )
            continue
        current_compile_merge_audit = None
        if task == "story_script_draft" and current_request_phase == "compile_repair":
            if not isinstance(compile_base_script, dict):
                validation = story_script_compile_replacement_rejection(
                    value={},
                    errors=["compile repair base Script is unavailable"],
                    failed_beat_ids=compile_failed_beat_ids,
                )
            else:
                fragment_errors = validate_schema(
                    parsed_value,
                    request_payload["response_format"]["json_schema"][
                        "schema"
                    ],
                )
                if fragment_errors:
                    validation = story_script_compile_replacement_rejection(
                        value=compile_base_script,
                        errors=fragment_errors,
                        failed_beat_ids=compile_failed_beat_ids,
                    )
                else:
                    merge_result = merge_story_script_compile_replacements(
                        base_script=compile_base_script,
                        replacement_value=parsed_value,
                        failed_beat_ids=compile_failed_beat_ids,
                        maximum_beats=_story_script_compile_max_beats(
                            response_format_value
                        ),
                    )
                    current_compile_merge_audit = merge_result.audit
                    if merge_result.errors:
                        validation = story_script_compile_replacement_rejection(
                            value=compile_base_script,
                            errors=merge_result.errors,
                            failed_beat_ids=compile_failed_beat_ids,
                        )
                    else:
                        validation = _entry_symbol("validate_and_canonicalize_job_response")(
                            task,
                            merge_result.value,
                            response_format_value,
                            job,
                            context,
                        )
        else:
            validation = _entry_symbol("validate_and_canonicalize_job_response")(
                task,
                parsed_value,
                request_payload.get("response_format", response_format_value),
                job,
                context,
            )
        last_validation = validation
        preservation_errors: list[str] = []
        if task == "story_script_draft":
            if compile_preservation_contract is not None:
                validation, preservation_errors = (
                    apply_story_script_preservation_validation(
                        validation,
                        compile_preservation_contract,
                    )
                )
            compile_codes = story_script_compile_repair_codes(validation)
            if (
                current_request_phase == "compile_repair"
                and validation.errors
                and not compile_codes
            ):
                validation = story_script_compile_replacement_rejection(
                    value=validation.value,
                    errors=validation.errors,
                    failed_beat_ids=(
                        [
                            replacement_id
                            for replacement_ids in (
                                current_compile_merge_audit or {}
                            ).get("replacement_beat_ids", {}).values()
                            for replacement_id in replacement_ids
                        ]
                        or compile_failed_beat_ids
                    ),
                )
                compile_codes = story_script_compile_repair_codes(validation)
            if (
                compile_preservation_contract is None
                and compile_codes
                and compile_codes
                <= STORY_SCRIPT_COMPILE_REPAIR_FAILURE_CODES
            ):
                compile_preservation_contract = (
                    story_script_preservation_contract(validation.value)
                )
            record = treatment_attempt_record(
                validation=validation,
                semantic_attempt=len(treatment_attempts) + 1,
                context=context,
                preservation_contract=compile_preservation_contract,
                request_phase=current_request_phase,
                ordinary_retry_used=ordinary_retry_used,
                compile_repair_used=compile_repair_used,
                compile_repair_audit=current_compile_merge_audit,
            )
            if not any(
                item.get("attempt_sha256") == record["attempt_sha256"]
                for item in treatment_attempts
                if isinstance(item, dict)
            ):
                treatment_attempts.append(record)
            current_compile_failure_signature = str(
                record.get("compile_failure_signature") or ""
            )
            no_progress_detected = bool(
                current_compile_failure_signature
                and previous_compile_failure_signature
                and current_compile_failure_signature
                == previous_compile_failure_signature
            )
            if current_compile_failure_signature:
                previous_compile_failure_signature = (
                    current_compile_failure_signature
                )
        else:
            record = {}
            no_progress_detected = False
        last_validation = validation
        attempted_merge_result = None
        if (
            task == "series_registry"
            and validation.errors
            and not validation.schema_errors
            and not validation.identity_errors
            and is_relationship_closure_only(validation.value)
        ):
            from .contracts import RegistryRecoveryMergeResult
            incumbent = load_registry_recovery_candidate(
                output_path=output_path,
                signature=signature,
            )
            if incumbent is not None:
                attempted_merge_result = (
                    merge_registry_relationship_progress(
                        incumbent,
                        validation.value,
                        event_index=registry_event_index,
                    )
                )
                recovery_value = (
                    attempted_merge_result.effective_registry
                    if attempted_merge_result.progressed
                    else incumbent
                )
                validation = _entry_symbol("validate_and_canonicalize_job_response")(
                    task,
                    recovery_value,
                    response_format_value,
                    job,
                    context,
                )
                if attempted_merge_result.progressed:
                    validation = replace(
                        validation,
                        registry_recovery_merge_result=(
                            attempted_merge_result
                        ),
                    )
        errors = validation.errors
        if not errors:
            value = validation.value
            accepted_validation = validation
            break
        diagnostic = semantic_diagnostic_path(
            output_path=output_path,
            job_id=str(job.get("id") or output_path.stem),
            signature=signature,
            semantic_attempt=semantic_attempt + 1,
            recorder=recorder,
        )
        atomic_write_json(
            diagnostic,
            {
                "task": task,
                "signature": signature,
                "invocation_id": (
                    recorder.invocation_id
                    if recorder is not None
                    else None
                ),
                "model": payload["model"],
                "semantic_attempt": semantic_attempt + 1,
                "errors": errors,
                "analysis": parsed_value,
                "effective_analysis_sha256": json_sha256(validation.value),
                "local_repairs": validation.local_repairs,
                "failure_codes": (
                    validation.story_script_admission.failure_codes
                    if validation.story_script_admission is not None
                    else (
                        sorted(
                            {
                                str(item.get("code"))
                                for item in (
                                    validation.window_contract_result.blockers
                                    if validation.window_contract_result is not None
                                    else []
                                )
                                if item.get("code")
                            }
                        )
                    )
                ),
                "repair_route": (
                    validation.story_script_admission.repair_route
                    if validation.story_script_admission is not None
                    else (
                        "local_window_media"
                        if (
                            validation.window_contract_result is not None
                            and not validation.schema_errors
                            and not validation.identity_errors
                            and supports_local_window_media_recovery(
                                validation.window_contract_result
                            )
                        )
                        else None
                    )
                ),
                "window_blockers": (
                    validation.window_contract_result.blockers
                    if validation.window_contract_result is not None
                    else []
                ),
                "registry_recovery_merge": (
                    attempted_merge_result.as_audit()
                    if attempted_merge_result is not None
                    else None
                ),
                "story_script_compile_repair": (
                    {
                        "request_phase": record.get("request_phase"),
                        "ordinary_retry_used": record.get(
                            "ordinary_retry_used", 0
                        ),
                        "compile_repair_used": record.get(
                            "compile_repair_used", 0
                        ),
                        "failure_class": record.get("failure_class"),
                        "compile_failure_codes": record.get(
                            "compile_failure_codes", []
                        ),
                        "compile_failure_signature": record.get(
                            "compile_failure_signature", ""
                        ),
                        "compaction_retry_projection": record.get(
                            "compaction_retry_projection", []
                        ),
                        "mismatch_retry_projection": record.get(
                            "mismatch_retry_projection", []
                        ),
                        "preservation_contract_sha256": record.get(
                            "preservation_contract_sha256", ""
                        ),
                        "preservation_errors": record.get(
                            "preservation_errors", []
                        ),
                        "replacement_merge": record.get(
                            "compile_repair_audit"
                        ),
                    }
                    if task == "story_script_draft"
                    else None
                ),
            },
            private=True,
        )
        if recorder is not None:
            if validation.schema_errors:
                recorder.record_semantic_error(
                    error_kind=ERROR_KIND_SCHEMA,
                    errors=validation.schema_errors,
                )
            if validation.identity_errors:
                recorder.record_semantic_error(
                    error_kind=ERROR_KIND_IDENTITY,
                    errors=validation.identity_errors,
                )
            if validation.contract_errors:
                recorder.record_semantic_error(
                    error_kind=ERROR_KIND_SEMANTIC_CONTRACT,
                    errors=validation.contract_errors,
                )
        if (
            task == "series_registry"
            and not validation.schema_errors
            and not validation.identity_errors
            and is_relationship_closure_only(validation.value)
        ):
            persist_registry_recovery_candidate(
                validation.value,
                output_path=output_path,
                signature=signature,
                errors=errors,
                event_index=registry_event_index,
                source="model_response",
                repairs=validation.local_repairs,
            )
            repair_result: (
                SeriesRegistryRelationshipRepairResult | None
            ) = None
            try:
                repaired_validation, repair_result = (
                    attempt_series_registry_relationship_repair(
                        validation.value,
                        event_index=[
                            item
                            for item in context.get("event_index", [])
                            if isinstance(item, dict)
                        ],
                        parent_job=job,
                        parent_context=context,
                        parent_response_format=response_format_value,
                        parent_signature=signature,
                        output_path=output_path,
                        backend_name=backend_name,
                        cache_dir=cache_dir,
                        max_context_chars=max_context_chars,
                        max_inline_mb=max_inline_mb,
                        temperature=temperature,
                        timeout=timeout,
                        retries=retries,
                        limiter=limiter,
                        dry_run=dry_run,
                        concurrency=concurrency,
                        ledger=ledger,
                        semantic_retries=semantic_retries,
                    )
                )
            except Exception:
                repaired_validation = None
            if repaired_validation is not None:
                repaired_validation = inherit_registry_admission_repairs(
                    repaired_validation,
                    validation,
                )
                value = repaired_validation.value
                accepted_validation = repaired_validation
                break
            if repair_result is not None:
                identity_validation: Any = None
                identity_result: SeriesRegistryIdentityRepairResult | None = None
                try:
                    identity_validation, identity_result = (
                        attempt_series_registry_identity_repair(
                            repair_result.effective_registry,
                            relationship_decisions=repair_result.decisions,
                            event_index=registry_event_index,
                            parent_job=job,
                            parent_context=context,
                            parent_response_format=response_format_value,
                            parent_signature=signature,
                            output_path=output_path,
                            backend_name=backend_name,
                            cache_dir=cache_dir,
                            max_context_chars=max_context_chars,
                            max_inline_mb=max_inline_mb,
                            temperature=temperature,
                            timeout=timeout,
                            retries=retries,
                            limiter=limiter,
                            dry_run=dry_run,
                            concurrency=concurrency,
                            ledger=ledger,
                            semantic_retries=semantic_retries,
                        )
                    )
                except Exception:
                    identity_validation = None
                if identity_validation is not None:
                    identity_validation = inherit_registry_admission_repairs(
                        identity_validation,
                        validation,
                    )
                    identity_validation = replace(
                        identity_validation,
                        registry_repair_result=repair_result,
                    )
                    value = identity_validation.value
                    accepted_validation = identity_validation
                    break
                persist_registry_recovery_candidate(
                    repair_result.effective_registry,
                    output_path=output_path,
                    signature=signature,
                    errors=repair_result.errors,
                    event_index=registry_event_index,
                    source="relationship_repair",
                    repairs=repair_result.repairs,
                    decisions=repair_result.decisions,
                )
                remaining_errors = (
                    identity_result.errors
                    if identity_result is not None
                    else repair_result.errors
                )
                if remaining_errors:
                    errors = list(remaining_errors)
                    if recorder is not None:
                        recorder.record_semantic_error(
                            error_kind=ERROR_KIND_SEMANTIC_CONTRACT,
                            errors=errors,
                        )
        error_summary = semantic_validation_error_summary(
            validation,
            errors=errors,
        )
        last_semantic_error = ValueError(error_summary)
        if no_progress_detected:
            compile_repair_stop_reason = "no_progress"
            break
        if (
            task == "story_script_draft"
            and compile_preservation_contract is not None
        ):
            if compile_repair_used >= compile_repair_limit:
                compile_repair_stop_reason = "exhausted"
                break
            fallback_ids = list(compile_failed_beat_ids)
            if isinstance(current_compile_merge_audit, dict):
                fallback_ids = [
                    replacement_id
                    for replacement_ids in current_compile_merge_audit.get(
                        "replacement_beat_ids", {}
                    ).values()
                    for replacement_id in replacement_ids
                ] or fallback_ids
            compile_base_script = copy.deepcopy(validation.value)
            compile_failed_beat_ids = story_script_compile_failure_beat_ids(
                validation,
                context=context,
                fallback_beat_ids=fallback_ids,
            )
            if not compile_failed_beat_ids:
                compile_repair_stop_reason = "invalid_state"
                break
            request_payload = story_script_compile_repair_payload(
                payload,
                base_script=compile_base_script,
                failed_beat_ids=compile_failed_beat_ids,
                error_summary=error_summary,
                context=context,
                compile_preservation_contract=compile_preservation_contract,
                compaction_projection=record.get(
                    "compaction_retry_projection", []
                ),
                mismatch_projection=record.get(
                    "mismatch_retry_projection", []
                ),
                repair_round=compile_repair_used + 1,
                response_format_value=response_format_value,
            )
            current_request_phase = "compile_repair"
            continue
        if ordinary_retry_used >= ordinary_semantic_retry_limit:
            break
        ordinary_retry_used += 1
        current_request_phase = "ordinary_retry"
        request_payload = semantic_retry_payload(
            payload,
            error_summary=error_summary,
            treatment_viability=(
                treatment_retry_viability_from_validation(validation)
                if task == "story_script_draft"
                else None
            ),
            treatment_attempts=(
                treatment_attempts
                if task == "story_script_draft"
                else None
            ),
            invalid_value=(
                validation.value
                if task == "story_script_draft"
                else None
            ),
            compaction_beat_ids=(
                validation.story_script_admission.split_regeneration_beat_ids
                if task == "story_script_draft"
                and validation.story_script_admission is not None
                else None
            ),
            compaction_projection=(
                record.get("compaction_retry_projection", [])
                if task == "story_script_draft"
                and isinstance(record, dict)
                else None
            ),
            mismatch_projection=(
                record.get("mismatch_retry_projection", [])
                if task == "story_script_draft"
                and isinstance(record, dict)
                else None
            ),
            context=context,
        )
    if value is None and task == "series_registry":
        recovery_state = load_registry_recovery_state(
            output_path=output_path,
            signature=signature,
        )
        partial_candidate = (
            recovery_state.get("effective_registry")
            if isinstance(recovery_state, dict)
            else None
        )
        relationship_decisions = (
            recovery_state.get("decisions", [])
            if isinstance(recovery_state, dict)
            else []
        )
        if isinstance(partial_candidate, dict):
            partial_admission = compile_series_registry_admission(
                partial_candidate,
                event_index=registry_event_index,
                relationship_decisions=(
                    relationship_decisions
                    if isinstance(relationship_decisions, list)
                    else []
                ),
            )
            if partial_admission.ok:
                value = partial_admission.effective_registry
                accepted_validation = JobResponseValidation(
                    value=value,
                    schema_errors=[],
                    identity_errors=[],
                    contract_errors=[],
                    registry_admission_result=partial_admission,
                )
    if value is None:
        if recorder is not None:
            recorder.finalize("failed")
        if task == "window_analysis":
            window_result = (
                last_validation.window_contract_result
                if last_validation is not None
                else None
            )
            blockers = list(window_result.blockers) if window_result else []
            failure_codes = sorted(
                {
                    str(item.get("code"))
                    for item in blockers
                    if item.get("code")
                }
            )
            if last_validation is not None:
                if last_validation.schema_errors:
                    failure_codes.append("WINDOW_RESPONSE_SCHEMA_INVALID")
                if last_validation.identity_errors:
                    failure_codes.append("WINDOW_IDENTITY_MISMATCH")
            if not failure_codes:
                failure_codes = ["WINDOW_MODEL_OUTPUT_INVALID"]
            eligible = bool(
                window_result is not None
                and last_validation is not None
                and not last_validation.schema_errors
                and not last_validation.identity_errors
                and supports_local_window_media_recovery(window_result)
                and not isinstance(recovery_metadata, dict)
            )
            repair_route = (
                "local_window_media" if eligible else "window_analysis"
            )
            if isinstance(recovery_metadata, dict):
                mark_window_media_recovery_outcome(
                    job,
                    status="failed",
                    request_signature=signature,
                    error=str(
                        last_semantic_error
                        or "physical Window response failed admission"
                    ),
                    attempt_ledger_invocation_id=(
                        recorder.invocation_id if recorder is not None else None
                    ),
                )
            raise WindowAnalysisSemanticRejection(
                str(
                    last_semantic_error
                    or "Window Analysis semantic admission failed"
                ),
                failure_codes=failure_codes,
                blockers=blockers,
                repair_route=repair_route,
                media_recovery_attempted=isinstance(recovery_metadata, dict),
                request_signature=signature,
            )
        if task == "story_script_draft":
            treatment_audit_path = write_treatment_attempt_audit(
                output_path=output_path,
                context=context,
                job=job,
                signature=signature,
                attempts=treatment_attempts,
                final_status=(
                    f"compile_repair_{compile_repair_stop_reason}"
                    if compile_repair_stop_reason
                    else "rejected"
                ),
            )
            admission = (
                last_validation.story_script_admission
                if last_validation is not None
                else None
            )
            failure_codes: list[str] = []
            repair_route = "story_script"
            if admission is not None and admission.failure_codes:
                failure_codes = admission.failure_codes
                repair_route = admission.repair_route
            elif last_validation is not None:
                if last_validation.schema_errors:
                    failure_codes.append("story_script_schema_invalid")
                if last_validation.identity_errors:
                    failure_codes.append("story_script_identity_mismatch")
                if last_validation.contract_errors:
                    failure_codes.append("story_script_contract_invalid")
            if not failure_codes:
                failure_codes = ["story_script_model_output_invalid"]
            failure_class = (
                "compile_only"
                if compile_preservation_contract is not None
                else "semantic"
            )
            if compile_repair_stop_reason:
                failure_codes = list(
                    dict.fromkeys(
                        [
                            *failure_codes,
                            "story_script_compile_repair_"
                            + compile_repair_stop_reason,
                        ]
                    )
                )
            raise StoryScriptSemanticRejection(
                str(
                    last_semantic_error
                    or "Story Script semantic admission failed"
                ),
                story_id=str(
                    context.get("story", {}).get("story_id")
                    or job.get("id")
                    or "unknown"
                ),
                failure_codes=failure_codes,
                repair_route=repair_route,
                failure_class=failure_class,
                compile_repair_stop_reason=compile_repair_stop_reason,
            )
        if last_semantic_error is not None:
            raise last_semantic_error
        raise RuntimeError("semantic response failed without a diagnostic")
    cached = {
        "schema_version": "1.0",
        "created_at": utc_now(),
        "task": task,
        "backend": backend.name,
        "provider": backend.provider,
        "model": payload["model"],
        "signature": signature,
        "request_identity": signature_payload,
        "analysis": (
            accepted_validation.registry_admission_result.raw_registry
            if accepted_validation is not None
            and accepted_validation.registry_admission_result is not None
            else value
        ),
    }
    if accepted_validation is not None:
        cache_contract = contract_cache_metadata(accepted_validation)
        if cache_contract is not None:
            metadata_key, metadata_value = cache_contract
            cached[metadata_key] = metadata_value
    if isinstance(recovery_metadata, dict):
        cached["window_media_recovery"] = recovery_identity
    atomic_write_json(object_path, cached)
    atomic_write_json(output_path, value)
    treatment_audit_path = write_treatment_attempt_audit(
        output_path=output_path,
        context=context,
        job=job,
        signature=signature,
        attempts=treatment_attempts,
        final_status="accepted",
    )
    if isinstance(recovery_metadata, dict):
        mark_window_media_recovery_outcome(
            job,
            status="succeeded",
            request_signature=signature,
            output_sha256=sha256_file(output_path),
            attempt_ledger_invocation_id=(
                recorder.invocation_id if recorder is not None else None
            ),
        )
    repair_report_path = (
        record_contract_repairs(
            recorder=recorder,
            output_path=output_path,
            job=job,
            validation=accepted_validation,
            request_signature=signature,
        )
        if accepted_validation is not None
        else None
    )
    if recorder is not None:
        recorder.finalize("succeeded")
    return {
        "id": job.get("id"),
        "task": task,
        "status": "succeeded",
        "output": str(output_path),
        "signature": signature,
        "quality_status": (
            accepted_validation.quality_status
            if accepted_validation is not None
            else "valid"
        ),
        "local_repair_count": (
            len(accepted_validation.local_repairs)
            if accepted_validation is not None
            else 0
        ),
        "repair_report": (
            str(repair_report_path)
            if repair_report_path is not None
            else None
        ),
        "treatment_attempt_audit": (
            str(treatment_audit_path)
            if treatment_audit_path is not None
            else None
        ),
        "media_recovery_report": (
            recovery_metadata.get("report_path")
            if isinstance(recovery_metadata, dict)
            else None
        ),
    }