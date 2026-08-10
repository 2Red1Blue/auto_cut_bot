"""SeriesRegistryTool — Series Registry 准入与修复链。

Wraps RegistryStage as a Tool.  Runs admission and the five-phase repair
chain (alias, identity, reference, relationship, recovery) on entities
extracted from chapter digests.
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
class SeriesRegistryTool(Tool):
    """Tool that runs series registry admission and repair chain.

    Admits entities into the series knowledge base and runs the five-phase
    repair chain: alias repair, identity repair, reference repair,
    relationship repair, and quarantine recovery. Produces
    series-registry.json.
    """

    human_review = False

    @property
    def name(self) -> str:
        return "series_registry"

    @property
    def description(self) -> str:
        return (
            "Run Series Registry admission and repair chain on entities extracted "
            "from chapter/episode digests and event cards. Produces "
            "series-registry.json with admission and quarantine sub-products."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the series_registry stage.

        Prepares the registry semantic batch from chapter digests, episode
        digests, and event cards, then runs the admission and repair chain.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from auto_cut_bot.pipeline.plugins.ac_series_knowledge.stages.registry.stage import (
            RegistryStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        required_files = {
            "episode_digests": job_root / "episode-digests.jsonl",
            "chapter_digests": job_root / "chapter-digests.jsonl",
            "event_cards": job_root / "event-cards.jsonl",
        }
        missing = [k for k, p in required_files.items() if not p.is_file()]
        if missing:
            return ToolResult.error(
                f"Missing required files: {', '.join(missing)}. "
                "Run episode_digests, chapter_digests, and event_cards first."
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
        bus.put("episode_digests", {"path": str(required_files["episode_digests"])}, stage="episode_digests")
        bus.put("chapter_digests", {"path": str(required_files["chapter_digests"])}, stage="chapter_digests")
        bus.put("event_cards", {"path": str(required_files["event_cards"])}, stage="event_cards")

        stage = RegistryStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            # DB write: upsert subjects and relationships from registry
            _logger = logging.getLogger(__name__)
            try:
                db = get_db_client(str(job_root))
                if db is not None and db.is_available:
                    registry_path = job_root / "series-registry.json"
                    book_id = _get_book_id(job_root)
                    if book_id and registry_path.is_file():
                        with open(registry_path, "r", encoding="utf-8") as _f:
                            _data = json.load(_f)
                        subjects = _data.get("subjects") or _data.get("entities", [])
                        if subjects:
                            name_to_id = db.upsert_subjects(book_id, subjects)
                            relationships = _data.get("relationships", [])
                            if relationships and name_to_id:
                                resolved_rels: list[dict[str, Any]] = []
                                for _rel in relationships:
                                    _src_name = _rel.get("source") or _rel.get("source_name")
                                    _tgt_name = _rel.get("target") or _rel.get("target_name")
                                    _src_id = name_to_id.get(_src_name) if _src_name else None
                                    _tgt_id = name_to_id.get(_tgt_name) if _tgt_name else None
                                    if _src_id is not None and _tgt_id is not None:
                                        resolved_rels.append(
                                            {
                                                "source_subject_id": _src_id,
                                                "target_subject_id": _tgt_id,
                                                "description": _rel.get("description"),
                                                "source": _rel.get("source", "api"),
                                            }
                                        )
                                if resolved_rels:
                                    db.upsert_relationships(book_id, resolved_rels)
            except Exception as _db_err:
                _logger.warning("DB write failed for series_registry: %s", _db_err)

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "series_registry completed successfully.\n\n"
                f"Artifacts:\n- series_registry: {paths.get('series_registry', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"series_registry failed: {exc}")


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