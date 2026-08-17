"""StoryPlansTool — Story Plan 选项生成与语义选择。

Wraps PlanOptionsStage as a Tool.  Uses LLM to generate and select
story plan options from span candidate bundles.
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
class StoryPlansTool(Tool):
    """Tool that generates and selects story plan options using LLM.

    Consumes span candidate bundles and generates plan options with
    semantic selection.  Produces story-plan-batch.json for downstream
    materialization.
    """
    _scopes = {"subagent"}


    human_review = False

    @property
    def name(self) -> str:
        return "story_plans"

    @property
    def description(self) -> str:
        return (
            "Generate and select story plan options from span candidate bundles "
            "using LLM. Produces story-plan-batch.json for downstream materialization."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the story_plans stage.

        Prepares plan options from span candidates, runs LLM-based
        semantic selection, and writes the plan batch.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from autocut_core.stages.ac_plan_orchestration.plan_options.stage import (
            PlanOptionsStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        span_index = job_root / "span-candidates" / "index.json"
        if not span_index.is_file():
            return ToolResult.error(
                f"span-candidates/index.json not found at {span_index}. "
                "Run span_candidates first."
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
        bus.put("span_candidates", {"path": str(span_index)}, stage="span_candidates")

        stage = PlanOptionsStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "story_plans completed successfully.\n\n"
                f"Artifacts:\n- story_plans: {paths.get('story_plans', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"story_plans failed: {exc}")