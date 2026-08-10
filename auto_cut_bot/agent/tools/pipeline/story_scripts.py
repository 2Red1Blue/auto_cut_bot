"""StoryScriptsTool — 基于入选故事与讲法方案, 用 LLM 生成 Story Script。

Wraps ScriptsStage as a Tool.  Uses LLM to generate story scripts for
each portfolio story, guided by treatment options, the Series Bible, and
event cards.
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
class StoryScriptsTool(Tool):
    """Tool that generates story scripts using LLM.

    For each portfolio story, generates a structured script (segments, clips,
    editorial decisions) informed by the treatment options, Series Bible,
    and event cards.  Produces story-scripts/index.json.
    """

    human_review = False

    @property
    def name(self) -> str:
        return "story_scripts"

    @property
    def description(self) -> str:
        return (
            "Generate story scripts for each portfolio story using LLM. "
            "Produces story-scripts/index.json with structured scripts per story."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the story_scripts stage.

        Prepares a semantic batch, runs LLM inference for each story,
        and assembles an index of all generated scripts.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from auto_cut_bot.pipeline.plugins.ac_story_generation.stages.story_scripts.stage import (
            ScriptsStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        required_files = {
            "catalog": job_root / "story-catalog.json",
            "portfolio": job_root / "story-portfolio.json",
            "treatments": job_root / "story-treatment-options.json",
            "bible": job_root / "series-bible.json",
            "event_cards": job_root / "event-cards.jsonl",
            "candidate": job_root / "highlight-hook-catalog.json",
        }
        missing = [k for k, p in required_files.items() if not p.is_file()]
        if missing:
            return ToolResult.error(
                f"Missing required files: {', '.join(missing)}. "
                "Run story_catalog, story_portfolio, story_treatments, "
                "series_bible, and event_cards first."
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
        bus.put("story_catalog", {"path": str(required_files["catalog"])}, stage="story_catalog")
        bus.put("story_portfolio", {"path": str(required_files["portfolio"])}, stage="story_portfolio")
        bus.put("story_treatments", {"path": str(required_files["treatments"])}, stage="story_treatments")
        bus.put("series_bible", {"path": str(required_files["bible"])}, stage="series_bible")
        bus.put("event_cards", {"path": str(required_files["event_cards"])}, stage="event_cards")
        bus.put("highlight_hook_catalog", {"path": str(required_files["candidate"])}, stage="event_cards")

        stage = ScriptsStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "story_scripts completed successfully.\n\n"
                f"Artifacts:\n- story_scripts: {paths.get('story_scripts', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"story_scripts failed: {exc}")