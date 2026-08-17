"""StoryQCTool — 物化 Plan 的视频质检 (语义 + 规则)。

Wraps StoryQCStage as a Tool.  Runs video quality checks on the
materialized story plans, combining semantic (LLM) review with
rule-based checks (audio boundary detection, clip validation).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.context import ToolContext
from auto_cut_bot.agent.runtime.state import mark_stage_complete


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
        "mode": {
            "type": "string",
            "enum": ["interactive", "auto"],
            "description": "Pipeline mode: 'auto' allows partial results.",
            "default": "auto",
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
class StoryQCTool(Tool):
    """Tool that runs video quality checks on materialized story plans.

    Combines semantic (LLM) review of clip transitions and visual quality
    with rule-based checks including audio boundary detection.  Produces
    story-qc/index.json with per-story QC reports.
    """
    _scopes = {"subagent"}


    human_review = False

    @property
    def name(self) -> str:
        return "story_qc"

    @property
    def description(self) -> str:
        return (
            "Run video quality checks on materialized story plans. "
            "Combines semantic LLM review with rule-based audio boundary "
            "detection. Produces story-qc/index.json with QC reports."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the story_qc stage.

        Runs three phases: prepare story QC (audio boundary, admission),
        run semantic batch for visual review, and assemble QC reports.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from autocut_core.stages.ac_qc.story_qc.stage import (
            StoryQCStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        plans_index = job_root / "story-plans" / "index.json"
        if not plans_index.is_file():
            return ToolResult.error(
                f"story-plans/index.json not found at {plans_index}. "
                "Run story_plans_materialize first."
            )

        cfg = PipelineConfig(
            job_root=job_root,
            backend=kwargs["backend"],
            mode=kwargs.get("mode", "auto"),
            workers=kwargs.get("workers", 4),
            requests_per_minute=kwargs.get("requests_per_minute", 30),
            semantic_retries=kwargs.get("semantic_retries", 2),
            extra={},
        )

        bus = ArtifactBus()
        bus.put("story_plans_materialized", {"path": str(plans_index)}, stage="story_plans_materialize")

        stage = StoryQCStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "story_qc completed successfully.\n\n"
                f"Artifacts:\n- story_qc: {paths.get('story_qc', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"story_qc failed: {exc}")