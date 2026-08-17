"""SeriesBibleTool — Series Bible 汇总。

Wraps BibleStage as a Tool.  Assembles the Series Bible from the registry,
assignment results, episode digests, and event cards.  Produces both a
structured JSON and a human-readable Markdown review view.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.context import ToolContext
from auto_cut_bot.agent.runtime.state import get_db_client, mark_stage_complete


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
    _scopes = {"subagent"}


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
        from autocut_core.stages.ac_series_knowledge.bible.stage import (
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

            # DB write: update book with bible summary
            _logger = logging.getLogger(__name__)
            try:
                db = get_db_client(str(job_root))
                if db is not None and db.is_available:
                    bible_path = job_root / "series-bible.json"
                    book_id = _get_book_id(job_root)
                    if book_id and bible_path.is_file():
                        with open(bible_path, "r", encoding="utf-8") as _f:
                            _data = json.load(_f)
                        _summary = _data.get("summary") or _data.get("bible_summary")
                        _genre = _data.get("genre")
                        _sub_genre = _data.get("sub_genre")
                        _mood = _data.get("mood")
                        _era = _data.get("era")
                        _updates: dict[str, Any] = {}
                        if _summary:
                            _updates["overall_synopsis"] = _summary
                        if _genre:
                            _updates["genre"] = _genre
                        if _sub_genre:
                            _updates["sub_genre"] = _sub_genre
                        if _mood:
                            _updates["mood"] = _mood
                        if _era:
                            _updates["era"] = _era
                        if _updates:
                            db.update_book(book_id, **_updates)
            except Exception as _db_err:
                _logger.warning("DB write failed for series_bible: %s", _db_err)

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "series_bible completed successfully.\n\n"
                f"Artifacts:\n- series_bible: {paths.get('series_bible', 'N/A')}\n"
                f"Markdown review: {job_root / 'series-bible.md'}"
            )
        except Exception as exc:
            return ToolResult.error(f"series_bible failed: {exc}")


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