"""WindowAnalysisTool — 批量 VLM 逐窗语义分析 + 多源数据融合。

Wraps WindowAnalysisStage as a Tool. Consumes the window_batch from
source_windows and runs LLM inference on each window, then fuses
VLM output with optional source_metadata and asr_transcript data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.context import ToolContext


@tool_parameters({
    "type": "object",
    "properties": {
        "job_root": {
            "type": "string",
            "description": "Root directory for the pipeline job (absolute path).",
        },
        "backend": {
            "type": "string",
            "description": "LLM backend name (e.g. 'qwen', 'doubao').",
        },
        "workers": {
            "type": "integer",
            "description": "Number of concurrent LLM workers (default 4).",
        },
        "requests_per_minute": {
            "type": "integer",
            "description": "Rate limit for LLM API calls (default 30).",
        },
        "semantic_retries": {
            "type": "integer",
            "description": "Number of retries on schema validation failure (default 2).",
        },
    },
    "required": ["job_root", "backend"],
})
class WindowAnalysisTool(Tool):
    """Tool that runs VLM semantic analysis on every window in the batch.

    Each window is sent to the configured LLM for content analysis
    (characters, events, boundaries, subjects).  Results are fused
    with optional source_metadata and ASR transcript data for
    cross-validation.
    """

    human_review = False

    @property
    def name(self) -> str:
        return "window_analysis"

    @property
    def description(self) -> str:
        return (
            "Run batch VLM semantic analysis on every sliding window. "
            "Each window is analyzed for characters, events, boundaries, "
            "and subjects. Outputs window-summaries.jsonl with fused multi-source data."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the window_analysis stage.

        Loads the window_batch manifest, runs LLM inference on each window,
        fuses VLM output with optional metadata sources, and writes
        window-summaries.jsonl.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from auto_cut_bot.pipeline.plugins.ac_source_prep.stages.window_analysis.stage import (
            WindowAnalysisStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        window_batch = job_root / "window-analysis-batch.json"
        if not window_batch.is_file():
            return ToolResult.error(
                f"window-analysis-batch.json not found at {window_batch}. "
                "Run source_windows first."
            )

        cfg = PipelineConfig(
            job_root=job_root,
            backend=kwargs["backend"],
            workers=kwargs.get("workers", 4),
            requests_per_minute=kwargs.get("requests_per_minute", 30),
            semantic_retries=kwargs.get("semantic_retries", 2),
            extra={},
        )

        bus = ArtifactBus()
        bus.put("window_batch", {"path": str(window_batch)}, stage="source_windows")

        stage = WindowAnalysisStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}
            return ToolResult(
                "window_analysis completed successfully.\n\n"
                f"Artifacts:\n- window_summaries: {paths.get('window_summaries', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"window_analysis failed: {exc}")