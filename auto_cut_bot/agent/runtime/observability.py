"""Observability module for the Agent-Native pipeline.

Provides structured JSONL logging, stage-level metrics collection,
milestone tracking, and session-level aggregation for pipeline runs.

Usage::

    from auto_cut_bot.agent.runtime.observability import MetricsCollector

    collector = MetricsCollector(session_id="run-42", job_root="/tmp/jobs/42")
    collector.record_milestone("source_windows_start", dt)
    collector.record_stage("source_windows", 1500, 32000, "completed")
    collector.record_stage("source_transcripts", 2300, 45000, "completed")
    collector.record_error("window_analysis", "API timeout", RuntimeError("timeout"))
    summary = collector.get_session_summary()
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autocut_core.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class PipelineMetrics:
    """Single stage or milestone measurement within a pipeline run."""

    session_id: str
    milestone: str = ""
    stage: str = ""
    duration_ms: float = 0.0
    tokens_used: int = 0
    status: str = "pending"  # pending | running | completed | failed | skipped
    recorded_at: str = ""

    def __post_init__(self) -> None:
        if not self.recorded_at:
            self.recorded_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "milestone": self.milestone,
            "stage": self.stage,
            "duration_ms": self.duration_ms,
            "tokens_used": self.tokens_used,
            "status": self.status,
            "recorded_at": self.recorded_at,
        }


# ---------------------------------------------------------------------------
# Structured JSONL emitter
# ---------------------------------------------------------------------------


def _format_log_line(
    session_id: str, event_type: str, payload: dict[str, Any]
) -> str:
    """Produce a single JSONL line with timestamp, session_id, event_type, payload."""
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "event_type": event_type,
        "payload": payload,
    }
    return json.dumps(record, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# MetricsCollector
# ---------------------------------------------------------------------------


class MetricsCollector:
    """Collects structured metrics for a single pipeline session.

    Writes JSONL log lines to ``<job_root>/.sd-viz/observability.jsonl``.
    All methods are thread-safe via an internal lock.

    Parameters:
        session_id: Unique identifier for this pipeline run.
        job_root:   Root directory for the job; ``.sd-viz/`` is created inside it.
    """

    def __init__(self, session_id: str, job_root: str) -> None:
        self.session_id = session_id
        self._job_root = Path(job_root).expanduser().resolve()
        self._viz_dir = self._job_root / ".sd-viz"
        self._log_path = self._viz_dir / "observability.jsonl"
        self._lock = threading.Lock()
        self._stages: list[PipelineMetrics] = []
        self._milestones: list[PipelineMetrics] = []
        self._errors: list[dict[str, Any]] = []

        # Ensure the viz directory exists.
        try:
            self._viz_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create viz dir %s: %s", self._viz_dir, exc)

    # -- public recording API -------------------------------------------------

    def record_stage(
        self,
        stage_name: str,
        duration_ms: float,
        tokens: int = 0,
        status: str = "completed",
    ) -> None:
        """Record a stage execution metric and emit a JSONL log line."""
        metric = PipelineMetrics(
            session_id=self.session_id,
            stage=stage_name,
            duration_ms=duration_ms,
            tokens_used=tokens,
            status=status,
        )
        line = _format_log_line(
            self.session_id,
            "stage",
            {
                "stage": stage_name,
                "duration_ms": duration_ms,
                "tokens": tokens,
                "status": status,
            },
        )
        with self._lock:
            self._stages.append(metric)
            self._append_line(line)

    def record_milestone(self, milestone: str, achieved_at: datetime | None = None) -> None:
        """Record a logical milestone (e.g. 'source_windows_start') and emit a JSONL log line."""
        ts = (achieved_at or datetime.now(timezone.utc)).isoformat()
        metric = PipelineMetrics(
            session_id=self.session_id,
            milestone=milestone,
            status="achieved",
            recorded_at=ts,
        )
        line = _format_log_line(
            self.session_id,
            "milestone",
            {"milestone": milestone, "achieved_at": ts},
        )
        with self._lock:
            self._milestones.append(metric)
            self._append_line(line)

    def record_error(
        self,
        error_message: str,
        stage: str = "",
        exc: BaseException | None = None,
    ) -> None:
        """Record an error event and emit a JSONL log line."""
        payload: dict[str, Any] = {
            "error": error_message,
            "stage": stage,
        }
        if exc is not None:
            payload["exception_type"] = type(exc).__name__
            payload["exception_args"] = list(exc.args)
        line = _format_log_line(self.session_id, "error", payload)
        with self._lock:
            self._errors.append(payload)
            self._append_line(line)

    # -- summary --------------------------------------------------------------

    def get_session_summary(self) -> dict[str, Any]:
        """Return an aggregated summary dict for the session.

        Includes: session_id, total stages, completion rate, total duration,
        total tokens, milestone count, error count, and per-stage breakdown.
        """
        with self._lock:
            stages = list(self._stages)
            milestones = list(self._milestones)
            errors = list(self._errors)

        total_duration = sum(s.duration_ms for s in stages)
        total_tokens = sum(s.tokens_used for s in stages)
        completed = sum(1 for s in stages if s.status == "completed")
        failed = sum(1 for s in stages if s.status == "failed")

        return {
            "session_id": self.session_id,
            "total_stages": len(stages),
            "completed": completed,
            "failed": failed,
            "completion_rate": (completed / len(stages)) if stages else 0.0,
            "total_duration_ms": total_duration,
            "total_tokens": total_tokens,
            "milestone_count": len(milestones),
            "error_count": len(errors),
            "stages": [
                {
                    "stage": s.stage,
                    "duration_ms": s.duration_ms,
                    "tokens": s.tokens_used,
                    "status": s.status,
                }
                for s in stages
            ],
            "milestones": [m.milestone for m in milestones],
            "errors": errors,
        }

    # -- internal helpers -----------------------------------------------------

    def _append_line(self, line: str) -> None:
        """Append a single JSONL line to the observability log file."""
        try:
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            logger.warning("Failed to write observability log: %s", exc)


# ---------------------------------------------------------------------------
# Session-level stats helper
# ---------------------------------------------------------------------------


def get_pipeline_stats(session_id: str, job_root: str) -> dict[str, Any]:
    """Read and aggregate metrics from an existing observability JSONL file.

    Returns a dict with aggregated stats, or an empty dict if the file
    does not exist.
    """
    viz_dir = Path(job_root).expanduser().resolve() / ".sd-viz"
    log_path = viz_dir / "observability.jsonl"

    if not log_path.exists():
        logger.warning("No observability log found at %s", log_path)
        return {}

    stages: list[dict[str, Any]] = []
    milestones: list[str] = []
    errors: list[dict[str, Any]] = []
    total_duration: float = 0.0
    total_tokens: int = 0
    completed: int = 0
    failed: int = 0

    try:
        with open(log_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                payload = entry.get("payload", {})
                event_type = entry.get("event_type", "")

                if event_type == "stage":
                    dur = float(payload.get("duration_ms", 0))
                    tokens = int(payload.get("tokens", 0))
                    status = payload.get("status", "")
                    stages.append({
                        "stage": payload.get("stage", ""),
                        "duration_ms": dur,
                        "tokens": tokens,
                        "status": status,
                    })
                    total_duration += dur
                    total_tokens += tokens
                    if status == "completed":
                        completed += 1
                    elif status == "failed":
                        failed += 1

                elif event_type == "milestone":
                    milestones.append(payload.get("milestone", ""))

                elif event_type == "error":
                    errors.append(payload)

    except OSError as exc:
        logger.warning("Failed to read observability log: %s", exc)
        return {}

    total_stages = len(stages)
    return {
        "session_id": session_id,
        "total_stages": total_stages,
        "completed": completed,
        "failed": failed,
        "completion_rate": (completed / total_stages) if total_stages else 0.0,
        "total_duration_ms": total_duration,
        "total_tokens": total_tokens,
        "milestone_count": len(milestones),
        "error_count": len(errors),
        "stages": stages,
        "milestones": milestones,
        "errors": errors,
    }


__all__ = [
    "PipelineMetrics",
    "MetricsCollector",
    "get_pipeline_stats",
]