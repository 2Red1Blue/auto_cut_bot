"""StoryPortfolioTool — 对故事目录做 Primary/Reserve 分槽与去重。

Wraps PortfolioStage as a Tool.  Pure local computation that selects
stories from the catalog, splits them into Primary/Reserve slots, and
deduplicates against the Series Bible.
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
    },
    "required": ["job_root"],
})
class StoryPortfolioTool(Tool):
    """Tool that builds the Story Portfolio with Primary/Reserve slots.

    Reads the story catalog and Series Bible, selects stories, and splits
    them into Primary (production) and Reserve (backup) slots.  Pure local
    computation, no LLM calls.
    """
    _scopes = {"subagent"}


    human_review = False

    @property
    def name(self) -> str:
        return "story_portfolio"

    @property
    def description(self) -> str:
        return (
            "Build the Story Portfolio by selecting stories from the catalog "
            "and splitting them into Primary/Reserve slots. Deduplicates against "
            "the Series Bible. Produces story-portfolio.json. Pure local computation."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the story_portfolio stage.

        Loads the story catalog and Series Bible, runs portfolio building,
        and writes story-portfolio.json.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from auto_cut_bot.pipeline.plugins.ac_story_generation.stages.story_portfolio.stage import (
            PortfolioStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        required_files = {
            "catalog": job_root / "story-catalog.json",
            "bible": job_root / "series-bible.json",
        }
        missing = [k for k, p in required_files.items() if not p.is_file()]
        if missing:
            return ToolResult.error(
                f"Missing required files: {', '.join(missing)}. "
                "Run story_catalog and series_bible first."
            )

        cfg = PipelineConfig(
            job_root=job_root,
            backend="qwen",
            extra={},
        )

        bus = ArtifactBus()
        bus.put("story_catalog", {"path": str(required_files["catalog"])}, stage="story_catalog")
        bus.put("series_bible", {"path": str(required_files["bible"])}, stage="series_bible")

        stage = PortfolioStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "story_portfolio completed successfully.\n\n"
                f"Artifacts:\n- story_portfolio: {paths.get('story_portfolio', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"story_portfolio failed: {exc}")