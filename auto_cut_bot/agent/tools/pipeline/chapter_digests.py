"""ChapterDigestsTool — 逐章语义摘要 (每 ~6 集合一)。

Wraps ChapterDigestsStage as a Tool.  Merges multiple episode digests
into chapter-level summaries using LLM.
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
        "backend": {
            "type": "string",
            "description": "LLM backend name (e.g. 'qwen', 'doubao').",
        },
        "episodes_per_chapter": {
            "type": "integer",
            "description": "Number of episodes per chapter (default 6).",
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
class ChapterDigestsTool(Tool):
    """Tool that generates chapter-level summaries by merging episode digests.

    Groups ~6 episodes per chapter and uses LLM to produce higher-level
    narrative summaries in chapter-digests.jsonl.
    """
    _scopes = {"subagent"}


    human_review = False

    @property
    def name(self) -> str:
        return "chapter_digests"

    @property
    def description(self) -> str:
        return (
            "Generate chapter-level summaries (every ~6 episodes) from episode "
            "digests using LLM. Produces chapter-digests.jsonl."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the chapter_digests stage.

        Prepares a semantic batch grouping episodes into chapters,
        runs LLM inference, and assembles chapter digest records.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from autocut_core.stages.ac_series_knowledge.chapter_digests.stage import (
            ChapterDigestsStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        episode_digests_path = job_root / "episode-digests.jsonl"
        if not episode_digests_path.is_file():
            return ToolResult.error(
                f"episode-digests.jsonl not found at {episode_digests_path}. "
                "Run episode_digests first."
            )

        cfg = PipelineConfig(
            job_root=job_root,
            backend=kwargs["backend"],
            episodes_per_chapter=kwargs.get("episodes_per_chapter", 6),
            workers=kwargs.get("workers", 4),
            requests_per_minute=kwargs.get("requests_per_minute", 30),
            semantic_retries=kwargs.get("semantic_retries", 2),
            extra={},
        )

        bus = ArtifactBus()
        bus.put("episode_digests", {"path": str(episode_digests_path)}, stage="episode_digests")

        stage = ChapterDigestsStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            # DB write: upsert episodes with chapter-level updates
            _logger = logging.getLogger(__name__)
            try:
                db = get_db_client(str(job_root))
                if db is not None and db.is_available:
                    digests_path = job_root / "chapter-digests.jsonl"
                    book_id = _get_book_id(job_root)
                    if book_id and digests_path.is_file():
                        chapters: list[dict[str, Any]] = []
                        with open(digests_path, "r", encoding="utf-8") as _f:
                            for _line in _f:
                                _line = _line.strip()
                                if not _line:
                                    continue
                                _rec = json.loads(_line)
                                _ch_id = _rec.get("chapter_id")
                                if _ch_id is not None:
                                    chapters.append(
                                        {
                                            "episode_id": _ch_id,
                                            "chapter_id": _ch_id,
                                            "title": _rec.get("title"),
                                            "summary": _rec.get("summary"),
                                            "source": _rec.get("source", "vlm"),
                                            "vlm_verified": _rec.get("vlm_verified", False),
                                        }
                                    )
                        if chapters:
                            db.upsert_episodes(book_id, chapters)
            except Exception as _db_err:
                _logger.warning("DB write failed for chapter_digests: %s", _db_err)

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "chapter_digests completed successfully.\n\n"
                f"Artifacts:\n- chapter_digests: {paths.get('chapter_digests', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"chapter_digests failed: {exc}")


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