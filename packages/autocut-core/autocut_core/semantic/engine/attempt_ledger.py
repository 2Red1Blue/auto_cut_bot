"""尝试台账 (Attempt Ledger) — 从 semantic_engine.py 提取。

包含:
  - AttemptLedger: 批次级尝试/错误台账
  - JobRecorder: 逐任务记录句柄
  - _sanitize_path_component: 路径分量清洗
  - 错误类型常量
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from autocut_core.io import atomic_write_json, load_json, utc_now

# ── 常量 ────────────────────────────────────────────────────────────────

ATTEMPT_LEDGER_DIRNAME = ".model-attempts"
ATTEMPT_LEDGER_SCHEMA_VERSION = "1.0"
ERROR_KIND_TRANSPORT = "transport_error"
ERROR_KIND_RATE_LIMIT = "rate_limit"

# 从 series_assignment_contract 迁入
SERIES_ASSIGNMENT_POLICY_VERSION = "series-assignment-accounting-v5-typed-coda"
ERROR_KIND_HTTP = "provider_http_error"
ERROR_KIND_NON_JSON = "non_json"
ERROR_KIND_SCHEMA = "schema_error"
ERROR_KIND_IDENTITY = "identity_error"
ERROR_KIND_SEMANTIC_CONTRACT = "semantic_contract_error"
ERROR_KIND_UNKNOWN = "unknown"
KNOWN_ERROR_KINDS = frozenset({
    ERROR_KIND_TRANSPORT, ERROR_KIND_RATE_LIMIT, ERROR_KIND_HTTP,
    ERROR_KIND_NON_JSON, ERROR_KIND_SCHEMA, ERROR_KIND_IDENTITY,
    ERROR_KIND_SEMANTIC_CONTRACT, ERROR_KIND_UNKNOWN,
})


# ── 工具 ────────────────────────────────────────────────────────────────


def _sanitize_path_component(value: str) -> str:
    """清洗路径分量: 只保留字母数字与 -_., 避免逃出台账目录。"""
    cleaned = "".join(
        ch if (ch.isalnum() or ch in "-_.") else "-" for ch in str(value)
    )
    return cleaned.strip("-.") or "unknown"


# ── JobRecorder ─────────────────────────────────────────────────────────


class JobRecorder:
    """AttemptLedger 返回的逐任务记录句柄。"""

    def __init__(
        self,
        *,
        ledger: "AttemptLedger",
        job_id: str,
        task: str,
        signature: str | None,
    ) -> None:
        self._ledger = ledger
        self.job_id = job_id
        self.task = task
        self.signature = signature
        self._attempts: list[dict[str, Any]] = []
        self._semantic_errors: list[dict[str, Any]] = []
        self._local_repairs: list[dict[str, Any]] = []
        self._cache_hit_signature: str | None = None
        self._first_attempt_succeeded: bool = False
        self._final_status: str | None = None
        self._job_root = ledger._job_dir(task, job_id)
        self._dir = self._job_root / "invocations" / ledger.invocation_id
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def invocation_id(self) -> str:
        return self._ledger.invocation_id

    def record_cache_hit(self, signature: str) -> None:
        self._cache_hit_signature = signature
        attempt = {
            "attempt_index": 0, "kind": "cache_hit",
            "signature": signature, "timestamp": utc_now(),
        }
        self._attempts.append(attempt)
        self._write_attempt(attempt)

    def record_http_attempt(
        self, *, attempt_index: int, started_at: str, ended_at: str,
        latency_ms: float, http_status: int | None, error_kind: str | None,
        throttled: bool, response_sha256: str | None,
        token_usage: dict[str, Any] | None = None,
    ) -> None:
        if error_kind is not None and error_kind not in KNOWN_ERROR_KINDS:
            error_kind = ERROR_KIND_UNKNOWN
        is_first_http_attempt = not any(
            item.get("kind") == "http" for item in self._attempts
        )
        attempt = {
            "attempt_index": attempt_index,
            "ledger_sequence": sum(1 for item in self._attempts if item.get("kind") == "http") + 1,
            "kind": "http", "started_at": started_at, "ended_at": ended_at,
            "latency_ms": round(latency_ms, 3), "http_status": http_status,
            "error_kind": error_kind, "throttled": throttled,
            "response_sha256": response_sha256, "token_usage": token_usage,
        }
        self._attempts.append(attempt)
        if is_first_http_attempt and error_kind is None and http_status is not None and 200 <= http_status < 300:
            self._first_attempt_succeeded = True
        self._write_attempt(attempt)

    def record_semantic_error(self, *, error_kind: str, errors: list[str]) -> None:
        if error_kind not in KNOWN_ERROR_KINDS:
            error_kind = ERROR_KIND_UNKNOWN
        record = {"error_kind": error_kind, "errors": list(errors[:20]), "timestamp": utc_now()}
        self._semantic_errors.append(record)
        self._first_attempt_succeeded = False

    def record_local_repair(
        self, *, repairs: list[dict[str, Any]], raw_sha256: str,
        effective_sha256: str, report_path: str | None,
        policy_version: str = SERIES_ASSIGNMENT_POLICY_VERSION,
    ) -> None:
        record = {
            "policy_version": policy_version, "repairs": list(repairs),
            "repair_count": len(repairs), "raw_output_sha256": raw_sha256,
            "effective_output_sha256": effective_sha256,
            "report_path": report_path, "timestamp": utc_now(),
        }
        self._local_repairs.append(record)
        atomic_write_json(self._dir / f"local-repair-{len(self._local_repairs):03d}.json", record, private=True)
        atomic_write_json(self._job_root / f"local-repair-{len(self._local_repairs):03d}.json", record, private=True)

    def finalize(self, status: str) -> None:
        self._final_status = status
        payload = {
            "schema_version": ATTEMPT_LEDGER_SCHEMA_VERSION,
            "invocation_id": self._ledger.invocation_id,
            "job_id": self.job_id, "task": self.task,
            "signature": self.signature, "final_status": status,
            "attempts": self._attempts,
            "attempt_count": sum(1 for item in self._attempts if item.get("kind") == "http"),
            "semantic_errors": self._semantic_errors,
            "local_repairs": self._local_repairs,
            "local_repair_count": sum(item.get("repair_count", 0) for item in self._local_repairs),
            "cache_hit": self._cache_hit_signature is not None,
            "first_attempt_succeeded": self._first_attempt_succeeded,
            "finalized_at": utc_now(),
        }
        final_path = self._dir / "final.json"
        atomic_write_json(final_path, payload, private=True)
        atomic_write_json(self._job_root / "final.json", payload, private=True)
        self._ledger._on_job_finalized(payload)

    def _write_attempt(self, attempt: dict[str, Any]) -> None:
        index = attempt.get("ledger_sequence", attempt["attempt_index"])
        name = "attempt-cache-hit.json" if attempt.get("kind") == "cache_hit" else f"attempt-{index:03d}.json"
        atomic_write_json(self._dir / name, attempt, private=True)
        atomic_write_json(self._job_root / name, attempt, private=True)


# ── AttemptLedger ────────────────────────────────────────────────────────


class AttemptLedger:
    """批次级尝试/错误台账。"""

    def __init__(self, root_dir: Path) -> None:
        self._root = root_dir.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.invocation_id = _sanitize_path_component(
            f"{utc_now()}-{os.getpid()}-{id(self):x}"
        )
        self._lock = threading.Lock()
        self._finalized: list[dict[str, Any]] = []

    def begin_job(self, *, job_id: str, task: str, signature: str | None = None) -> JobRecorder:
        return JobRecorder(ledger=self, job_id=job_id or "unknown", task=task or "unknown", signature=signature)

    def _job_dir(self, task: str, job_id: str) -> Path:
        directory = self._root / _sanitize_path_component(task) / _sanitize_path_component(job_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory

    def _on_job_finalized(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._finalized.append(payload)

    @staticmethod
    def _summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
        total_attempts = sum(item.get("attempt_count", 0) for item in records)
        cache_hits = sum(1 for item in records if item.get("cache_hit"))
        first_pass_succeeded = sum(
            1 for item in records
            if item.get("first_attempt_succeeded") and item.get("final_status") == "succeeded"
        )
        eventually_succeeded = sum(1 for item in records if item.get("final_status") == "succeeded")
        failed = sum(1 for item in records if item.get("final_status") == "failed")
        repaired_jobs = sum(1 for item in records if item.get("local_repair_count", 0) > 0)
        local_contract_repairs = sum(item.get("local_repair_count", 0) for item in records)
        retry_count = sum(max(0, item.get("attempt_count", 0) - 1) for item in records)
        per_error_kind: dict[str, int] = {}
        for item in records:
            for attempt in item.get("attempts", []):
                kind = attempt.get("error_kind")
                if kind:
                    per_error_kind[kind] = per_error_kind.get(kind, 0) + 1
            for sem in item.get("semantic_errors", []):
                kind = sem.get("error_kind")
                if kind:
                    per_error_kind[kind] = per_error_kind.get(kind, 0) + 1
        return {
            "schema_version": ATTEMPT_LEDGER_SCHEMA_VERSION,
            "jobs": len(records), "total_http_attempts": total_attempts,
            "cache_hits": cache_hits, "first_pass_succeeded": first_pass_succeeded,
            "eventually_succeeded": eventually_succeeded, "failed": failed,
            "repaired_jobs": repaired_jobs, "local_contract_repairs": local_contract_repairs,
            "retry_count": retry_count, "per_error_kind_count": dict(sorted(per_error_kind.items())),
        }

    def summary(self) -> dict[str, Any]:
        with self._lock:
            records = list(self._finalized)
        return {**self._summarize_records(records), "root": str(self._root), "invocation_id": self.invocation_id}

    def summary_for_jobs(self, job_keys: set[tuple[str, str]]) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        for task, job_id in sorted(job_keys):
            final_path = self._root / _sanitize_path_component(task) / _sanitize_path_component(job_id) / "final.json"
            if final_path.is_file():
                value = load_json(final_path)
                if isinstance(value, dict):
                    records.append(value)
        return {**self._summarize_records(records), "root": str(self._root), "invocation_id": self.invocation_id}

    def history_summary(self, job_keys: set[tuple[str, str]]) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        for task, job_id in sorted(job_keys):
            invocation_root = self._root / _sanitize_path_component(task) / _sanitize_path_component(job_id) / "invocations"
            if not invocation_root.is_dir():
                continue
            for final_path in sorted(invocation_root.glob("*/final.json")):
                value = load_json(final_path)
                if isinstance(value, dict):
                    records.append(value)
        return {**self._summarize_records(records), "root": str(self._root)}