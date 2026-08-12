"""ConfidenceCheckTool — VLM 输出质量门控 + 按需触发补充数据源。

Wraps ConfidenceCheckStage as a Tool so the LLM agent can trigger
the confidence check stage: assess VLM output quality, detect hard
subtitles, check boundary continuity and character naming consistency,
and suggest enrichment actions when confidence is low.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.context import ToolContext
from auto_cut_bot.pipeline.state import get_db_client, mark_stage_complete


@tool_parameters({
    "type": "object",
    "properties": {
        "job_root": {
            "type": "string",
            "description": "Root directory for the pipeline job (absolute path).",
        },
        "backend": {
            "type": "string",
            "description": "LLM backend name (e.g. 'qwen', 'doubao'). Default 'qwen'.",
        },
        "mode": {
            "type": "string",
            "enum": ["full", "summary_only"],
            "description": "Execution mode: 'full' runs all checks, 'summary_only' skips DB writes. Default 'full'.",
        },
    },
    "required": ["job_root"],
})
class ConfidenceCheckTool(Tool):
    """Tool that runs confidence-check gating on VLM analysis output.

    Consumes window_summaries (from WindowAnalysisStage) and produces
    a confidence_report with per-window assessments and a global summary.
    Low-confidence windows trigger enrichment suggestions (ASR, script
    injection, character reference) for the agent to act on.
    """

    _scopes = {"subagent"}

    human_review = False

    @property
    def name(self) -> str:
        return "confidence_check"

    @property
    def description(self) -> str:
        return (
            "Run confidence-check gating on VLM analysis output. "
            "Assesses per-window dialogue confidence, detects hard subtitles, "
            "checks boundary continuity and character naming consistency, "
            "and suggests enrichment actions (ASR, script injection) when "
            "confidence is low. Produces confidence-report.json."
        )

    @property
    def read_only(self) -> bool:
        return False

    @classmethod
    def config_cls(cls):
        return None

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the confidence_check stage.

        Runs ConfidenceCheckStage with the given configuration, producing
        a confidence_report artifact with per-window assessments and
        a global summary.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from auto_cut_bot.pipeline.plugins.ac_source_prep.stages.confidence_check.stage import (
            ConfidenceCheckStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        job_root.mkdir(parents=True, exist_ok=True)

        extra: dict[str, Any] = {}
        if kwargs.get("mode"):
            extra["mode"] = kwargs["mode"]

        cfg = PipelineConfig(
            job_root=job_root,
            backend=kwargs.get("backend", "qwen"),
            extra=extra,
        )

        bus = ArtifactBus()
        stage = ConfidenceCheckStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            # DB write: upsert job metadata from confidence report
            _logger = logging.getLogger(__name__)
            try:
                db = get_db_client(str(job_root))
                if db is not None and db.is_available:
                    report_path = job_root / "confidence-report.json"
                    if report_path.is_file():
                        with open(report_path, "r", encoding="utf-8") as _f:
                            _data = json.load(_f)
                        summary = _data.get("global_summary", {})
                        if summary:
                            db.upsert_confidence_summary(
                                total_windows=summary.get("total_windows", 0),
                                high_confidence=summary.get("high_confidence_windows", 0),
                                low_confidence=summary.get("low_confidence_windows", 0),
                                enrichment_triggered=summary.get("enrichment_triggered_count", 0),
                                status=summary.get("status", "unknown"),
                            )
            except Exception as _db_err:
                _logger.warning("DB write failed for confidence_check: %s", _db_err)

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "confidence_check completed successfully.\n\n"
                f"Artifacts:\n- confidence_report: {paths.get('confidence_report', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"confidence_check failed: {exc}")