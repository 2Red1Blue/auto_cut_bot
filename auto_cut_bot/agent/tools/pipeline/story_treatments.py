"""StoryTreatmentsTool — 为入选故事编译 Treatment 讲法方案。

Wraps TreatmentsStage as a Tool.  Pure local computation that compiles
three treatment strategies (linear, cold open, etc.) for each story in
the portfolio.
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
    },
    "required": ["job_root"],
})
class StoryTreatmentsTool(Tool):
    """Tool that compiles treatment strategies for portfolio stories.

    Generates three treatment strategies (linear narrative, cold open, etc.)
    for each story in the portfolio.  Pure local computation, no LLM calls.
    Produces story-treatment-options.json.
    """
    _scopes = {"subagent"}


    human_review = False

    @property
    def name(self) -> str:
        return "story_treatments"

    @property
    def description(self) -> str:
        return (
            "Compile treatment strategies (linear, cold open, etc.) for each story "
            "in the portfolio. Produces story-treatment-options.json. "
            "Pure local computation."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the story_treatments stage.

        Loads the story catalog, portfolio, Series Bible, and highlight/hook
        catalog, then compiles treatment options.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from autocut_core.stages.ac_story_generation.story_treatments.stage import (
            TreatmentsStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        required_files = {
            "catalog": job_root / "story-catalog.json",
            "portfolio": job_root / "story-portfolio.json",
            "bible": job_root / "series-bible.json",
            "candidate": job_root / "highlight-hook-catalog.json",
        }
        missing = [k for k, p in required_files.items() if not p.is_file()]
        if missing:
            return ToolResult.error(
                f"Missing required files: {', '.join(missing)}. "
                "Run story_catalog, story_portfolio, and series_bible first."
            )

        cfg = PipelineConfig(
            job_root=job_root,
            backend="qwen",
            extra={},
        )

        bus = ArtifactBus()
        bus.put("story_catalog", {"path": str(required_files["catalog"])}, stage="story_catalog")
        bus.put("story_portfolio", {"path": str(required_files["portfolio"])}, stage="story_portfolio")
        bus.put("series_bible", {"path": str(required_files["bible"])}, stage="series_bible")
        bus.put("highlight_hook_catalog", {"path": str(required_files["candidate"])}, stage="event_cards")

        stage = TreatmentsStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "story_treatments completed successfully.\n\n"
                f"Artifacts:\n- story_treatments: {paths.get('story_treatments', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"story_treatments failed: {exc}")