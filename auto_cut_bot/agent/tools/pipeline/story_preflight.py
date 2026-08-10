"""StoryPreflightTool — 本地素材可行性预检。

Wraps PreflightStage as a Tool.  Validates each story script for material
coverage, structural consistency, and Teaser contract compliance.
Pure local computation.
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
class StoryPreflightTool(Tool):
    """Tool that performs local material feasibility preflight checks.

    Validates each story script for material coverage, structural
    consistency, and Teaser contract compliance.  Produces
    story-feasibility.json and story-review.md.
    Pure local computation, no LLM calls.
    """
    _scopes = {"subagent"}


    human_review = False

    @property
    def name(self) -> str:
        return "story_preflight"

    @property
    def description(self) -> str:
        return (
            "Run local material feasibility checks on story scripts. "
            "Validates material coverage, structural consistency, and "
            "Teaser contract compliance. Produces story-feasibility.json "
            "and story-review.md."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the story_preflight stage.

        Loads story scripts, portfolio, Series Bible, event cards, and
        highlight/hook catalog, then runs preflight validation.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from auto_cut_bot.pipeline.plugins.ac_story_generation.stages.story_preflight.stage import (
            PreflightStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        required_files = {
            "story_index": job_root / "story-scripts" / "index.json",
            "portfolio": job_root / "story-portfolio.json",
            "bible": job_root / "series-bible.json",
            "event_cards": job_root / "event-cards.jsonl",
            "candidate_catalog": job_root / "highlight-hook-catalog.json",
        }
        missing = [k for k, p in required_files.items() if not p.is_file()]
        if missing:
            return ToolResult.error(
                f"Missing required files: {', '.join(missing)}. "
                "Run story_scripts, story_portfolio, series_bible, and event_cards first."
            )

        cfg = PipelineConfig(
            job_root=job_root,
            backend="qwen",
            extra={},
        )

        bus = ArtifactBus()
        bus.put("story_scripts", {"path": str(required_files["story_index"])}, stage="story_scripts")
        bus.put("story_portfolio", {"path": str(required_files["portfolio"])}, stage="story_portfolio")
        bus.put("series_bible", {"path": str(required_files["bible"])}, stage="series_bible")
        bus.put("event_cards", {"path": str(required_files["event_cards"])}, stage="event_cards")
        bus.put("highlight_hook_catalog", {"path": str(required_files["candidate_catalog"])}, stage="event_cards")

        stage = PreflightStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "story_preflight completed successfully.\n\n"
                f"Artifacts:\n- story_preflight: {paths.get('story_preflight', 'N/A')}\n"
                f"Review: {job_root / 'story-review.md'}"
            )
        except Exception as exc:
            return ToolResult.error(f"story_preflight failed: {exc}")