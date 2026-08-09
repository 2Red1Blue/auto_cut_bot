"""run_batch() 实现 — 语义批次推理编排器。

将 ``run_semantic_batch`` 的核心逻辑提取为可调用的 ``run_batch()`` 函数,
各 Stage 直接 import 调用。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from autocut_core.telemetry import trace
from autocut_core.semantic.engine import (
    ABSOLUTE_MAX_SEMANTIC_WORKERS,
    ATTEMPT_LEDGER_DIRNAME,
    AdaptiveConcurrencyController,
    AttemptLedger,
    RateLimiter,
    UNPACED_BACKENDS,
    resolve_worker_count,
    refresh_parallel_download,
    launch_parallel_remote_download,
    parse_worker_setting,
)
from autocut_core.semantic.types import (
    StoryScriptSemanticRejection,
    WindowAnalysisSemanticRejection,
)
from autocut_core.semantic.batch_runner import run_job
from autocut_core.semantic.window_recovery import (
    selected_job_recovery_errors,
    prepare_window_recovery_jobs,
)
from autocut_core.semantic.window_media_recovery import (
    WindowMediaRecoveryError,
    mark_window_media_recovery_outcome,
)
from autocut_core.io import (
    atomic_write_json,
    load_json,
    mark_pipeline_failure_recovered,
    record_pipeline_failure,
    update_project_stage,
    utc_now,
)


@trace
def run_batch(
    manifest_path: Path,
    *,
    backend: str = "qwen",
    workers: str | int = "auto",
    requests_per_minute: float = 0.0,
    max_context_chars: int = 600000,
    max_inline_mb: float = 48.0,
    max_tokens: int = 65536,
    temperature: float = 0.1,
    timeout: float = 600.0,
    retries: int = 2,
    semantic_retries: int = 1,
    fail_fast: bool = False,
    dry_run: bool = False,
    job_ids: list[str] | None = None,
    context_injection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行语义批次推理 — 替代 ``python run_semantic_batch.py <manifest>``。

    参数与 CLI 参数一一对应, 返回 runtime_metadata 字典。
    失败时记录到 project.json 但不抛异常。
    """
    manifest_path = manifest_path.expanduser().resolve()
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("jobs"), list):
        raise ValueError("manifest must be an object with jobs[]")

    manifest_jobs = [
        item for item in manifest["jobs"] if isinstance(item, dict)
    ]
    all_manifest_job_keys = {
        (str(item.get("task") or "unknown"), str(item.get("id") or "unknown"))
        for item in manifest_jobs
    }
    jobs = list(manifest_jobs)
    selected_job_ids = set(job_ids or [])
    recovery_errors = selected_job_recovery_errors(manifest, selected_job_ids)

    if selected_job_ids:
        known_job_ids = {
            str(item.get("id"))
            for item in jobs
            if isinstance(item.get("id"), str)
        }
        unknown_job_ids = sorted(selected_job_ids - known_job_ids)
        if unknown_job_ids:
            raise ValueError(f"unknown --job-id values: {unknown_job_ids}")
        jobs = [
            item for item in jobs if item.get("id") in selected_job_ids
        ]

    jobs = prepare_window_recovery_jobs(
        manifest,
        manifest_path=manifest_path,
        jobs=jobs,
        selected_job_ids=selected_job_ids,
        max_inline_mb=max_inline_mb,
        dry_run=dry_run,
    )

    resolved_workers = resolve_worker_count(
        parse_worker_setting(str(workers)),
        backend_name=backend,
        jobs=jobs,
    )

    effective_rpm = (
        float(requests_per_minute)
        if requests_per_minute > 0
        else (0.0 if backend in UNPACED_BACKENDS else 30.0)
    )

    cache_dir = Path(
        manifest.get("cache_dir", manifest_path.parent / ".story-cache")
    ).expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    limiter = RateLimiter(effective_rpm)
    concurrency = AdaptiveConcurrencyController(resolved_workers)

    download_launch, download_process = launch_parallel_remote_download(
        manifest, manifest_path=manifest_path, dry_run=dry_run
    )

    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    attempt_ledger = AttemptLedger(
        manifest_path.parent / ATTEMPT_LEDGER_DIRNAME
    )

    common = {
        "backend_name": backend,
        "cache_dir": cache_dir,
        "max_context_chars": max_context_chars,
        "max_inline_mb": max_inline_mb,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout,
        "retries": retries,
        "semantic_retries": semantic_retries,
        "limiter": limiter,
        "concurrency": concurrency,
        "dry_run": dry_run,
        "ledger": attempt_ledger,
        "context_injection": context_injection,
    }

    with ThreadPoolExecutor(max_workers=resolved_workers) as executor:
        futures = {
            executor.submit(
                run_job,
                job,
                prior_failure_error=recovery_errors.get(
                    str(job.get("id") or "")
                ),
                **common,
            ): job
            for job in jobs
        }
        for future in as_completed(futures):
            job = futures[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as exc:
                failure = {
                    "id": job.get("id"),
                    "task": job.get("task"),
                    "status": "failed",
                    "error": str(exc),
                }
                if isinstance(exc, StoryScriptSemanticRejection):
                    failure.update({
                        "story_id": exc.story_id,
                        "disposition": "rejected",
                        "failure_codes": exc.failure_codes,
                        "repair_route": exc.repair_route,
                        "failure_class": exc.failure_class,
                        "compile_repair_stop_reason": exc.compile_repair_stop_reason,
                    })
                if isinstance(exc, WindowAnalysisSemanticRejection):
                    failure.update({
                        "failure_codes": exc.failure_codes,
                        "window_blockers": exc.blockers,
                        "repair_route": exc.repair_route,
                        "media_recovery_attempted": exc.media_recovery_attempted,
                        "request_signature": exc.request_signature,
                    })
                if isinstance(exc, WindowMediaRecoveryError):
                    failure.update({
                        "failure_codes": [exc.code],
                        "window_blockers": [],
                        "repair_route": "window_analysis",
                        "media_recovery_attempted": False,
                    })
                recovery_metadata = job.get("window_media_recovery")
                if isinstance(recovery_metadata, dict):
                    mark_window_media_recovery_outcome(
                        job, status="failed",
                        request_signature=recovery_metadata.get("_request_signature"),
                        error=str(exc),
                    )
                failures.append(failure)
                if fail_fast:
                    for pending in futures:
                        pending.cancel()
                    break

    results.sort(key=lambda item: str(item.get("id")))
    failures.sort(key=lambda item: str(item.get("id")))

    download_summary = refresh_parallel_download(download_launch, download_process)

    runtime_metadata: dict[str, Any] = {
        "schema_version": "1.0",
        "finished_at": utc_now(),
        "manifest": str(manifest_path),
        "backend": backend,
        "dry_run": dry_run,
        "execution_policy": {
            "workers_requested": workers,
            "workers_resolved": resolved_workers,
            "requests_per_minute": effective_rpm,
            "semantic_retries": semantic_retries,
            "adaptive_concurrency": concurrency.snapshot(),
        },
        "succeeded": len(results),
        "failed": len(failures),
        "results": results,
        "failures": failures,
        "parallel_remote_download": download_summary,
        "attempt_ledger": {
            **attempt_ledger.summary_for_jobs(all_manifest_job_keys),
            "current_invocation": attempt_ledger.summary(),
            "history": attempt_ledger.history_summary(all_manifest_job_keys),
        },
    }

    # 选定子集时合并历史结果
    if selected_job_ids:
        previous_runtime = manifest.get("runtime_metadata", {})
        previous_results = {
            item.get("id"): item
            for item in previous_runtime.get("results", []) or []
            if isinstance(item, dict) and item.get("id") not in selected_job_ids
        }
        previous_failures = {
            item.get("id"): item
            for item in previous_runtime.get("failures", []) or []
            if isinstance(item, dict) and item.get("id") not in selected_job_ids
        }
        for item in results:
            previous_results[item.get("id")] = item
            previous_failures.pop(item.get("id"), None)
        for item in failures:
            previous_failures[item.get("id")] = item
            previous_results.pop(item.get("id"), None)
        runtime_metadata["results"] = sorted(
            previous_results.values(), key=lambda item: str(item.get("id"))
        )
        runtime_metadata["failures"] = sorted(
            previous_failures.values(), key=lambda item: str(item.get("id"))
        )
        runtime_metadata["succeeded"] = len(runtime_metadata["results"])
        runtime_metadata["failed"] = len(runtime_metadata["failures"])
        runtime_metadata["job_filter"] = sorted(selected_job_ids)

    manifest_with_runtime = load_json(manifest_path)
    manifest_with_runtime["runtime_metadata"] = runtime_metadata
    atomic_write_json(manifest_path, manifest_with_runtime)

    batch_tasks = sorted({
        str(item.get("task"))
        for item in manifest.get("jobs", [])
        if isinstance(item, dict) and item.get("task")
    })

    failure_stage = (
        batch_tasks[0] if len(batch_tasks) == 1 else manifest_path.stem
    )
    effective_failures = [
        item for item in runtime_metadata.get("failures", [])
        if isinstance(item, dict)
    ]
    story_script_only = batch_tasks == ["story_script_draft"]
    isolated_story_rejections = bool(
        story_script_only and runtime_metadata.get("results") and effective_failures
    )
    registry_partial = bool(
        batch_tasks == ["series_registry"]
        and any(
            item.get("quality_status") == "partially_ready"
            for item in runtime_metadata.get("results", [])
            if isinstance(item, dict)
        )
    )

    runtime_metadata["batch_outcome"] = (
        "completed_with_story_rejections"
        if isolated_story_rejections
        else (
            "failed"
            if effective_failures
            else ("partially_ready" if registry_partial else "succeeded")
        )
    )

    manifest_with_runtime = load_json(manifest_path)
    manifest_with_runtime["runtime_metadata"] = runtime_metadata
    atomic_write_json(manifest_path, manifest_with_runtime)

    if effective_failures and not isolated_story_rejections:
        record_pipeline_failure(
            manifest_path.parent,
            stage=failure_stage,
            error=(
                f"{len(effective_failures)} semantic job(s) failed: "
                + "; ".join(
                    f"{item.get('id')}: {item.get('error')}"
                    for item in effective_failures[:20]
                )
            ),
            error_code="semantic_batch_failed",
            details={
                "manifest": str(manifest_path),
                "failed_job_ids": [item.get("id") for item in effective_failures],
                "failure_count": len(effective_failures),
            },
        )
    elif not dry_run:
        mark_pipeline_failure_recovered(manifest_path.parent, stage=failure_stage)
        if batch_tasks == ["series_registry"]:
            update_project_stage(
                manifest_path.parent / "project.json",
                "series_registry_job",
                "partially_ready" if registry_partial else "ready",
                inputs={"batch_manifest": str(manifest_path)},
                outputs={
                    "series_registry": str(manifest_path.parent / "series-registry.json"),
                    "series_registry_admission": str(manifest_path.parent / "series-registry-admission.json"),
                    "series_registry_quarantine": str(manifest_path.parent / "series-registry-quarantine.json"),
                    "series_registry_validation": str(manifest_path.parent / "series-registry-validation.json"),
                },
                note=(
                    "Registry core is ready; isolated identities are retained "
                    "in the hash-bound quarantine artifact"
                    if registry_partial
                    else "Registry core and admission validation are ready"
                ),
            )

    has_failure = bool(failures and not isolated_story_rejections)
    return runtime_metadata