"""SeriesBibleTool — Series Bible 汇总。

Wraps BibleStage as a Tool.  Assembles the Series Bible from the registry,
assignment results, episode digests, and event cards.  Produces both a
structured JSON and a human-readable Markdown review view.
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
    },
    "required": ["job_root"],
})
class SeriesBibleTool(Tool):
    """Tool that assembles the Series Bible from registry and assignment data.

    Produces series-bible.json (structured JSON) and series-bible.md
    (human-readable Markdown review view).  This is a pure local assembly
    stage (no LLM calls).
    """

    human_review = False

    @property
    def name(self) -> str:
        return "series_bible"

    @property
    def description(self) -> str:
        return (
            "Assemble the Series Bible from registry, assignment, episode digests, "
            "and event cards. Produces series-bible.json and series-bible.md. "
            "Pure local assembly."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the series_bible stage.

        Loads registry, assignment batch, episode digests, event cards,
        source/window manifests, and assembles the bible.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from auto_cut_bot.pipeline.plugins.ac_series_knowledge.stages.bible.stage import (
            BibleStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        required_files = {
            "series_registry": job_root / "series-registry.json",
            "assignment_batch": job_root / "series-assignment-batch.json",
            "episode_digests": job_root / "episode-digests.jsonl",
            "event_cards": job_root / "event-cards.jsonl",
        }
        missing = [k for k, p in required_files.items() if not p.is_file()]
        if missing:
            return ToolResult.error(
                f"Missing required files: {', '.join(missing)}. "
                "Run series_registry and series_assignment first."
            )

        cfg = PipelineConfig(
            job_root=job_root,
            backend="qwen",
            extra={},
        )

        bus = ArtifactBus()
        bus.put("series_registry", {"path": str(required_files["series_registry"])}, stage="series_registry")
        bus.put("series_assignment", {"path": str(required_files["assignment_batch"])}, stage="series_assignment")
        bus.put("episode_digests", {"path": str(required_files["episode_digests"])}, stage="episode_digests")
        bus.put("event_cards", {"path": str(required_files["event_cards"])}, stage="event_cards")

        stage = BibleStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}
            return ToolResult(
                "series_bible completed successfully.\n\n"
                f"Artifacts:\n- series_bible: {paths.get('series_bible', 'N/A')}\n"
                f"Markdown review: {job_root / 'series-bible.md'}"
            )
        except Exception as exc:
            return ToolResult.error(f"series_bible failed: {exc}")