"""StoryPlansMaterializeTool — 将选中的 Story Plan 物化为可 QC 的候选。

Wraps MaterializeStage as a Tool.  Expands selected plan options into
concrete, QC-ready story plans with clips, blocks, and duration estimates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.context import ToolContext
from auto_cut_bot.pipeline.state import mark_stage_complete


@tool_parameters({
    "type": "object",
    "properties": {
        "job_root": {
            "type": "string",
            "description": "Root directory for the pipeline job (absolute path).",
        },
        "mode": {
            "type": "string",
            "enum": ["interactive", "auto"],
            "description": "Pipeline mode: 'auto' allows partial materialization.",
            "default": "auto",
        },
    },
    "required": ["job_root"],
})
class StoryPlansMaterializeTool(Tool):
    """Tool that materializes selected story plans into QC-ready candidates.

    Expands the selected plan options into concrete story plans with
    clip definitions, block structures, and estimated durations.
    Produces story-plans/index.json and story-plan-review.md.
    """
    _scopes = {"subagent"}


    human_review = False

    @property
    def name(self) -> str:
        return "story_plans_materialize"

    @property
    def description(self) -> str:
        return (
            "Materialize selected story plan options into concrete, QC-ready "
            "plans with clips, blocks, and duration estimates. Produces "
            "story-plans/index.json and story-plan-review.md."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the story_plans_materialize stage.

        Loads the plan batch, validates span candidates, expands option
        selections, and materializes full story plans.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from auto_cut_bot.pipeline.plugins.ac_plan_orchestration.stages.materialize.stage import (
            MaterializeStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        plan_batch = job_root / "story-plan-batch.json"
        if not plan_batch.is_file():
            return ToolResult.error(
                f"story-plan-batch.json not found at {plan_batch}. "
                "Run story_plans first."
            )

        cfg = PipelineConfig(
            job_root=job_root,
            backend="qwen",
            mode=kwargs.get("mode", "auto"),
            extra={},
        )

        bus = ArtifactBus()
        bus.put("story_plans", {"path": str(plan_batch)}, stage="story_plans")

        stage = MaterializeStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "story_plans_materialize completed successfully.\n\n"
                f"Artifacts:\n- story_plans_materialized: {paths.get('story_plans_materialized', 'N/A')}\n"
                f"Review: {job_root / 'story-plan-review.md'}"
            )
        except Exception as exc:
            return ToolResult.error(f"story_plans_materialize failed: {exc}")