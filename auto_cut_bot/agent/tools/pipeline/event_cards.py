"""EventCardsTool — 从窗口分析结果编译中等粒度剧情事件。

Wraps EventCardsStage as a Tool.  Compiles event cards and highlight/hook
candidate catalog from window summaries.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.context import ToolContext
from auto_cut_bot.pipeline.state import get_db_client, mark_stage_complete


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
    _scopes = {"subagent"}


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

            # DB write: insert boundaries from event cards
            _logger = logging.getLogger(__name__)
            try:
                db = get_db_client(str(job_root))
                if db is not None and db.is_available:
                    cards_path = job_root / "event-cards.jsonl"
                    book_id = _get_book_id(job_root)
                    if book_id and cards_path.is_file():
                        boundaries: list[dict[str, Any]] = []
                        with open(cards_path, "r", encoding="utf-8") as _f:
                            for _line in _f:
                                _line = _line.strip()
                                if not _line:
                                    continue
                                _rec = json.loads(_line)
                                _b = _rec.get("boundary") or _rec
                                _bid = _b.get("boundary_id") or _b.get("event_id")
                                if _bid:
                                    boundaries.append(
                                        {
                                            "boundary_id": _bid,
                                            "episode_id": _b.get("episode_id"),
                                            "event_type": _b.get("event_type", "event"),
                                            "start_time": _b.get("start_time"),
                                            "end_time": _b.get("end_time"),
                                            "description": _b.get("description"),
                                            "subjects": _b.get("subjects", []),
                                            "source_table": "event_cards",
                                            "source_id": _bid,
                                            "confidence": _b.get("confidence", "medium"),
                                            "precision": _b.get("precision", 2.0),
                                        }
                                    )
                        if boundaries:
                            db.insert_boundaries(book_id, boundaries)
            except Exception as _db_err:
                _logger.warning("DB write failed for event_cards: %s", _db_err)

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "event_cards completed successfully.\n\n"
                f"Artifacts:\n- event_cards: {paths.get('event_cards', 'N/A')}\n"
                f"- highlight_hook_catalog: {paths.get('highlight_hook_catalog', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"event_cards failed: {exc}")


def _get_book_id(job_root: Path) -> str | None:
    """Extract book_id from source_manifest.json."""
    manifest = job_root / "source_manifest.json"
    if not manifest.is_file():
        return None
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("book_id") or data.get("id")
    except Exception:
        return None