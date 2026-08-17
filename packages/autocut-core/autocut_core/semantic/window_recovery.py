"""窗口媒体恢复 — 从 semantic_handlers.py 提取的 window_recovery 函数组。

原位置: semantic_handlers.py L5532-L5708, 5 funcs, ~200L
依赖: window_media_recovery, _entry_symbol, semantic_engine
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any


from autocut_core.semantic.engine import ATTEMPT_LEDGER_DIRNAME, _sanitize_path_component
from autocut_core.io import atomic_write_json, json_sha256
from autocut_core.semantic.window_media_recovery import (
    WindowMediaRecoveryError,
    build_window_media_recovery_job,
    load_window_media_recovery_report,
    mark_window_media_recovery_outcome,
)

# 延迟导入 — 避免循环依赖
_entry_symbol = None


def _get_entry_symbol(name: str) -> Any:
    global _entry_symbol
    if _entry_symbol is None:
        from autocut_core.semantic.batch_runner import _entry_symbol as _es
        _entry_symbol = _es
    return _entry_symbol(name)


def write_window_analysis_repair_report(
    *,
    output_path: Path,
    job: dict[str, Any],
    result: Any = None,
) -> Path | None:
    """写入窗口分析修复报告。"""
    from autocut_core.semantic.window_analysis_contract import POLICY_VERSION as WINDOW_ANALYSIS_POLICY_VERSION

    if result is None or not result.repairs:
        return None
    report_dir = output_path.parent / ".repairs"
    job_id = str(job.get("id") or output_path.stem)
    report_path = report_dir / f"{_sanitize_path_component(job_id)}.json"
    atomic_write_json(
        report_path,
        {
            "schema_version": "1.0",
            "policy_version": WINDOW_ANALYSIS_POLICY_VERSION,
            "job_id": job.get("id"),
            "window_id": result.effective_window.get("window_id"),
            "quality_status": result.quality_status,
            "raw_output_sha256": result.raw_sha256,
            "effective_output_sha256": result.effective_sha256,
            "repair_count": len(result.repairs),
            "repairs": result.repairs,
            "blocking_findings": result.blockers,
        },
    )
    return report_path


def selected_job_recovery_failures(
    manifest: dict[str, Any],
    selected_job_ids: set[str],
) -> dict[str, dict[str, Any]]:
    """Return structured prior failures for explicitly filtered reruns."""
    if not selected_job_ids:
        return {}
    runtime = manifest.get("runtime_metadata")
    if not isinstance(runtime, dict):
        return {}
    failures = runtime.get("failures")
    if not isinstance(failures, list):
        return {}
    return {
        str(item["id"]): item
        for item in failures
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item["id"] in selected_job_ids
    }


def selected_job_recovery_errors(
    manifest: dict[str, Any],
    selected_job_ids: set[str],
) -> dict[str, str]:
    """Return prior failure summaries for explicitly filtered reruns."""
    recovery_errors: dict[str, str] = {}
    for job_id, item in selected_job_recovery_failures(
        manifest, selected_job_ids,
    ).items():
        error = item.get("error")
        if isinstance(error, str) and error.strip():
            recovery_errors[job_id] = error
    return recovery_errors


def _window_recovery_preflight_job(
    job: dict[str, Any],
    error: WindowMediaRecoveryError,
) -> dict[str, Any]:
    failed_job = copy.deepcopy(job)
    failed_job["_window_media_recovery_preflight_error"] = {
        "code": error.code,
        "detail": error.detail,
    }
    return failed_job


def prepare_window_recovery_jobs(
    manifest: dict[str, Any],
    *,
    manifest_path: Path,
    jobs: list[dict[str, Any]],
    selected_job_ids: set[str],
    max_inline_mb: float,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Replace only eligible failed Window jobs with validated local clips."""
    if dry_run:
        return jobs
    recovery_scope = selected_job_ids or {
        str(item.get("id"))
        for item in jobs
        if isinstance(item.get("id"), str)
    }
    prior_failures = selected_job_recovery_failures(manifest, recovery_scope)
    config = manifest.get("parallel_remote_download")
    local_manifest_value = (
        config.get("local_source_manifest_path")
        if isinstance(config, dict)
        else None
    )
    prepared: list[dict[str, Any]] = []
    for job in jobs:
        if (
            job.get("task") not in ("vlm_analysis", "window_analysis")
            or job.get("media_url_mode") != "full_source"
        ):
            prepared.append(job)
            continue
        report = load_window_media_recovery_report(job)
        prior = prior_failures.get(str(job.get("id") or ""))
        should_recover = bool(
            report is not None
            or (
                isinstance(prior, dict)
                and prior.get("repair_route") == "local_window_media"
                and prior.get("media_recovery_attempted") is not True
            )
        )
        if not should_recover:
            prepared.append(job)
            continue
        if report is not None and report.get("status") == "failed":
            prepared.append(
                _window_recovery_preflight_job(
                    job,
                    WindowMediaRecoveryError(
                        "WINDOW_RECOVERY_ALREADY_FAILED",
                        "the same physical Window recovery signature already "
                        "failed; change the source/Window/policy before retrying",
                    ),
                )
            )
            continue
        if not isinstance(local_manifest_value, str):
            prepared.append(
                _window_recovery_preflight_job(
                    job,
                    WindowMediaRecoveryError(
                        "WINDOW_RECOVERY_SOURCE_NOT_READY",
                        "batch has no local_source_manifest_path",
                    ),
                )
            )
            continue
        failure_codes = (
            report.get("failure_codes", [])
            if isinstance(report, dict)
            else (prior.get("failure_codes", []) if isinstance(prior, dict) else [])
        )
        blockers = (
            report.get("blockers", [])
            if isinstance(report, dict)
            else (prior.get("window_blockers", []) if isinstance(prior, dict) else [])
        )
        try:
            artifact = _get_entry_symbol("prepare_window_media_artifact")(
                job,
                local_source_manifest_path=Path(
                    local_manifest_value
                ).expanduser().resolve(),
                cache_root=manifest_path.parent / ".window-media-cache",
                max_inline_mb=max_inline_mb,
            )
            prepared.append(
                build_window_media_recovery_job(
                    job,
                    artifact,
                    failure_codes=[
                        str(item) for item in failure_codes if isinstance(item, str)
                    ],
                    blockers=[
                        item for item in blockers if isinstance(item, dict)
                    ],
                    original_failure_request_signature=(
                        prior.get("request_signature")
                        if isinstance(prior, dict)
                        and isinstance(prior.get("request_signature"), str)
                        else None
                    ),
                )
            )
        except WindowMediaRecoveryError as exc:
            prepared.append(_window_recovery_preflight_job(job, exc))
    return prepared