"""EpisodeDigestsTool — 逐集语义摘要生成。

Wraps EpisodeDigestsStage as a Tool.  Uses LLM to generate per-episode
narrative summaries from event cards, window summaries, and manifests.
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
class EpisodeDigestsTool(Tool):
    """Tool that generates per-episode narrative summaries via LLM.

    Consumes source_manifest, window_manifest, window_summaries,
    event_cards, and highlight_hook_catalog to produce a structured
    episode-digests.jsonl with per-episode plot summaries.
    """
    _scopes = {"subagent"}


    human_review = False

    @property
    def name(self) -> str:
        return "episode_digests"

    @property
    def description(self) -> str:
        return (
            "Generate per-episode narrative summaries using LLM. "
            "Produces episode-digests.jsonl with structured plot summaries "
            "for each episode."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the episode_digests stage.

        Prepares a semantic batch, runs LLM inference, and assembles
        per-episode digest records.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from autocut_core.stages.ac_series_knowledge.episode_digests.stage import (
            EpisodeDigestsStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        required_files = {
            "source_manifest": job_root / "source_manifest.json",
            "window_manifest": job_root / "window_manifest.json",
            "window_summaries": job_root / "window-summaries.jsonl",
            "event_cards": job_root / "event-cards.jsonl",
            "catalog": job_root / "highlight-hook-catalog.json",
        }
        missing = [k for k, p in required_files.items() if not p.is_file()]
        if missing:
            return ToolResult.error(
                f"Missing required files: {', '.join(missing)}. "
                "Run source_windows, window_analysis, and event_cards first."
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
        bus.put("source_manifest", {"path": str(required_files["source_manifest"])}, stage="source_windows")
        bus.put("window_manifest", {"path": str(required_files["window_manifest"])}, stage="source_windows")
        bus.put("window_summaries", {"path": str(required_files["window_summaries"])}, stage="window_analysis")
        bus.put("event_cards", {"path": str(required_files["event_cards"])}, stage="event_cards")
        bus.put("highlight_hook_catalog", {"path": str(required_files["catalog"])}, stage="event_cards")

        stage = EpisodeDigestsStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            # DB write: upsert episodes from episode digests
            _logger = logging.getLogger(__name__)
            try:
                db = get_db_client(str(job_root))
                if db is not None and db.is_available:
                    digests_path = job_root / "episode-digests.jsonl"
                    book_id = _get_book_id(job_root)
                    if book_id and digests_path.is_file():
                        episodes: list[dict[str, Any]] = []
                        with open(digests_path, "r", encoding="utf-8") as _f:
                            for _line in _f:
                                _line = _line.strip()
                                if not _line:
                                    continue
                                _rec = json.loads(_line)
                                _ep_id = _rec.get("episode_id")
                                if _ep_id is not None:
                                    episodes.append(
                                        {
                                            "episode_id": _ep_id,
                                            "chapter_id": _rec.get("chapter_id"),
                                            "title": _rec.get("title"),
                                            "summary": _rec.get("summary"),
                                            "is_free": _rec.get("is_free", False),
                                            "scene_count": _rec.get("scene_count"),
                                            "duration": _rec.get("duration"),
                                            "source": _rec.get("source", "vlm"),
                                            "vlm_verified": _rec.get("vlm_verified", False),
                                        }
                                    )
                        if episodes:
                            db.upsert_episodes(book_id, episodes)
            except Exception as _db_err:
                _logger.warning("DB write failed for episode_digests: %s", _db_err)

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "episode_digests completed successfully.\n\n"
                f"Artifacts:\n- episode_digests: {paths.get('episode_digests', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"episode_digests failed: {exc}")


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