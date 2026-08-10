"""StoryCatalogTool — 从 Series Bible 与事件卡片中发现独立故事子弧。

Wraps CatalogStage as a Tool.  Uses LLM to discover independent story
sub-arcs from the Series Bible, event cards, and highlight/hook catalog.
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
class StoryCatalogTool(Tool):
    """Tool that discovers independent story sub-arcs from the Series Bible.

    Consumes the Series Bible, event cards, and highlight/hook catalog
    to produce a broad story-catalog.json listing candidate stories.
    """
    _scopes = {"subagent"}


    human_review = False

    @property
    def name(self) -> str:
        return "story_catalog"

    @property
    def description(self) -> str:
        return (
            "Discover independent story sub-arcs from the Series Bible, event cards, "
            "and highlight/hook catalog using LLM. Produces story-catalog.json."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the story_catalog stage.

        Prepares a semantic batch from the bible, event cards, and catalog,
        runs LLM inference, and assembles story-catalog.json.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from auto_cut_bot.pipeline.plugins.ac_story_generation.stages.story_catalog.stage import (
            CatalogStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        required_files = {
            "bible": job_root / "series-bible.json",
            "event_cards": job_root / "event-cards.jsonl",
            "catalog": job_root / "highlight-hook-catalog.json",
        }
        missing = [k for k, p in required_files.items() if not p.is_file()]
        if missing:
            return ToolResult.error(
                f"Missing required files: {', '.join(missing)}. "
                "Run series_bible and event_cards first."
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
        bus.put("series_bible", {"path": str(required_files["bible"])}, stage="series_bible")
        bus.put("event_cards", {"path": str(required_files["event_cards"])}, stage="event_cards")
        bus.put("highlight_hook_catalog", {"path": str(required_files["catalog"])}, stage="event_cards")

        stage = CatalogStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            # DB write: update book with overall_synopsis from story catalog
            _logger = logging.getLogger(__name__)
            try:
                db = get_db_client(str(job_root))
                if db is not None and db.is_available:
                    catalog_path = job_root / "story-catalog.json"
                    book_id = _get_book_id(job_root)
                    if book_id and catalog_path.is_file():
                        with open(catalog_path, "r", encoding="utf-8") as _f:
                            _data = json.load(_f)
                        _synopsis = _data.get("overall_synopsis") or _data.get("synopsis")
                        _updates: dict[str, Any] = {}
                        if _synopsis:
                            _updates["overall_synopsis"] = _synopsis
                        if _updates:
                            db.update_book(book_id, **_updates)
            except Exception as _db_err:
                _logger.warning("DB write failed for story_catalog: %s", _db_err)

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "story_catalog completed successfully.\n\n"
                f"Artifacts:\n- story_catalog: {paths.get('story_catalog', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"story_catalog failed: {exc}")


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