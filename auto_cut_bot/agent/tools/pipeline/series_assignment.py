"""SeriesAssignmentTool — 剧集到 Series 的合约化分配。

Wraps AssignmentStage as a Tool.  Assigns each episode to a specific
Series based on the registry, episode/chapter digests, and event cards.
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
class SeriesAssignmentTool(Tool):
    """Tool that assigns episodes to Series via contract-based assignment.

    Consumes the series registry, episode digests, chapter digests, and
    event cards to produce a series-assignment-batch.json that maps each
    episode to its canonical Series.
    """
    _scopes = {"subagent"}


    human_review = False

    @property
    def name(self) -> str:
        return "series_assignment"

    @property
    def description(self) -> str:
        return (
            "Assign episodes to Series using contract-based allocation. "
            "Produces series-assignment-batch.json consumed by BibleStage."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the series_assignment stage.

        Prepares and runs the assignment semantic batch, producing
        series-assignment-batch.json.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from auto_cut_bot.pipeline.plugins.ac_series_knowledge.stages.assignments.stage import (
            AssignmentStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        registry_path = job_root / "series-registry.json"
        if not registry_path.is_file():
            return ToolResult.error(
                f"series-registry.json not found at {registry_path}. "
                "Run series_registry first."
            )

        required_files = {
            "episode_digests": job_root / "episode-digests.jsonl",
            "chapter_digests": job_root / "chapter-digests.jsonl",
            "event_cards": job_root / "event-cards.jsonl",
        }
        missing = [k for k, p in required_files.items() if not p.is_file()]
        if missing:
            return ToolResult.error(
                f"Missing required files: {', '.join(missing)}."
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
        bus.put("series_registry", {"path": str(registry_path)}, stage="series_registry")
        bus.put("episode_digests", {"path": str(required_files["episode_digests"])}, stage="episode_digests")
        bus.put("chapter_digests", {"path": str(required_files["chapter_digests"])}, stage="chapter_digests")
        bus.put("event_cards", {"path": str(required_files["event_cards"])}, stage="event_cards")

        stage = AssignmentStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            # DB write: update subjects per assignment
            _logger = logging.getLogger(__name__)
            try:
                db = get_db_client(str(job_root))
                if db is not None and db.is_available:
                    batch_path = job_root / "series-assignment-batch.json"
                    book_id = _get_book_id(job_root)
                    if book_id and batch_path.is_file():
                        with open(batch_path, "r", encoding="utf-8") as _f:
                            _data = json.load(_f)
                        assignments = _data.get("assignments") or _data.get("results", [])
                        for _assign in assignments:
                            _subject_name = _assign.get("subject_name") or _assign.get("name")
                            _subject_id = _assign.get("subject_id")
                            _episode_id = _assign.get("episode_id")
                            _updates: dict[str, Any] = {}
                            if _episode_id is not None:
                                _updates["first_episode"] = _assign.get("first_episode", _episode_id)
                                _updates["last_episode"] = _assign.get("last_episode", _episode_id)
                            if _subject_id is not None and _updates:
                                db.update_subject(_subject_id, **_updates)
                            elif _subject_name:
                                _resolved_id = db.resolve_subject_id(book_id, _subject_name)
                                if _resolved_id is not None and _updates:
                                    db.update_subject(_resolved_id, **_updates)
            except Exception as _db_err:
                _logger.warning("DB write failed for series_assignment: %s", _db_err)

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "series_assignment completed successfully.\n\n"
                f"Artifacts:\n- series_assignment: {paths.get('series_assignment', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"series_assignment failed: {exc}")


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