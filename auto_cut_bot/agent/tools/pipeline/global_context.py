"""GlobalContextTool — 提取全剧级上下文（synopsis, themes, relationships）。

Wraps GlobalContextStage as a Tool so the LLM agent can trigger
the second pipeline stage: call Platform API / script fallback to extract
series-level context and write it to the global_context DB table.
"""

from __future__ import annotations

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
            "description": "LLM backend name for downstream stages (e.g. 'qwen', 'doubao').",
        },
        "mode": {
            "type": "string",
            "enum": ["interactive", "auto"],
            "description": "Pipeline mode: 'interactive' or 'auto' (default 'interactive').",
        },
    },
    "required": ["job_root"],
})
class GlobalContextTool(Tool):
    """Tool that extracts series-level global context from Platform API or script.

    This is the second pipeline stage (after source_windows).  It calls the
    Platform API to fetch book metadata (synopsis, themes, character
    relationships), falls back to script data if the API is unavailable,
    and writes the results to the global_context DB table.
    """

    _scopes = {"subagent"}

    human_review = False

    @property
    def name(self) -> str:
        return "global_context"

    @property
    def description(self) -> str:
        return (
            "Extract series-level global context (synopsis, themes, character "
            "relationships) from the Platform API or script fallback. Writes "
            "results to the global_context, subjects, books, and episodes DB "
            "tables. This is the second pipeline stage."
        )

    @property
    def read_only(self) -> bool:
        return False

    @classmethod
    def config_cls(cls):
        return None

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the global_context stage.

        Runs GlobalContextStage with the given configuration, calling the
        Platform API to extract series-level context and persisting it to DB.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from autocut_core.stages.ac_source_prep.global_context.stage import (
            GlobalContextStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        job_root.mkdir(parents=True, exist_ok=True)

        cfg = PipelineConfig(
            job_root=job_root,
            backend=kwargs.get("backend", "qwen"),
            mode=kwargs.get("mode", "interactive"),
        )

        bus = ArtifactBus()
        stage = GlobalContextStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            # DB write: mark stage as complete
            _logger = logging.getLogger(__name__)
            try:
                db = get_db_client(str(job_root))
                if db is not None and db.is_available:
                    gc_path = job_root / "global_context.json"
                    if gc_path.is_file():
                        import json
                        with open(gc_path, "r", encoding="utf-8") as _f:
                            _data = json.load(_f)
                        book_id = _data.get("book_id", "")
                        book_name = _data.get("book_name", "")
                        if book_id:
                            db.upsert_book(
                                book_id=book_id,
                                book_name=book_name,
                            )
            except Exception as _db_err:
                _logger.warning("DB write failed for global_context: %s", _db_err)

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "global_context completed successfully.\n\n"
                f"Artifacts:\n- global_context: {paths.get('global_context', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"global_context failed: {exc}")