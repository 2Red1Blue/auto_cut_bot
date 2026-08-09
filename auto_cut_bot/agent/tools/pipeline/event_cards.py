"""EventCardsTool — 从窗口分析结果编译中等粒度剧情事件。

Wraps EventCardsStage as a Tool.  Compiles event cards and highlight/hook
candidate catalog from window summaries.
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
class EventCardsTool(Tool):
    """Tool that compiles event cards and highlight/hook candidate catalog.

    Reads window-summaries.jsonl, deduplicates cross-window story beats,
    and produces event-cards.jsonl and a highlight-hook-catalog.json.
    This is a pure local computation stage (no LLM calls).
    """

    human_review = False

    @property
    def name(self) -> str:
        return "event_cards"

    @property
    def description(self) -> str:
        return (
            "Compile medium-granularity event cards and highlight/hook candidates "
            "from window analysis summaries. Produces event-cards.jsonl and "
            "highlight-hook-catalog.json. Pure local computation."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the event_cards stage.

        Loads window summaries, compiles and deduplicates events, and
        writes the event cards and candidate catalog.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from auto_cut_bot.pipeline.plugins.ac_series_knowledge.stages.event_cards.stage import (
            EventCardsStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        summaries_path = job_root / "window-summaries.jsonl"
        if not summaries_path.is_file():
            return ToolResult.error(
                f"window-summaries.jsonl not found at {summaries_path}. "
                "Run window_analysis first."
            )

        cfg = PipelineConfig(
            job_root=job_root,
            backend="qwen",
            extra={},
        )

        bus = ArtifactBus()
        bus.put("window_summaries", {"path": str(summaries_path)}, stage="window_analysis")

        stage = EventCardsStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}
            return ToolResult(
                "event_cards completed successfully.\n\n"
                f"Artifacts:\n- event_cards: {paths.get('event_cards', 'N/A')}\n"
                f"- highlight_hook_catalog: {paths.get('highlight_hook_catalog', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"event_cards failed: {exc}")