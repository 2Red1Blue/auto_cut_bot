"""WindowAnalysisTool — 批量 VLM 逐窗语义分析 + 多源数据融合。

Wraps WindowAnalysisStage as a Tool. Consumes the window_batch from
source_windows and runs LLM inference on each window, then fuses
VLM output with optional source_metadata and asr_transcript data.
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
class WindowAnalysisTool(Tool):
    """Tool that runs VLM semantic analysis on every window in the batch.

    Each window is sent to the configured LLM for content analysis
    (characters, events, boundaries, subjects).  Results are fused
    with optional source_metadata and ASR transcript data for
    cross-validation.
    """
    _scopes = {"subagent"}


    human_review = False

    @property
    def name(self) -> str:
        return "window_analysis"

    @property
    def description(self) -> str:
        return (
            "Run batch VLM semantic analysis on every sliding window. "
            "Each window is analyzed for characters, events, boundaries, "
            "and subjects. Outputs window-summaries.jsonl with fused multi-source data."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the window_analysis stage.

        Loads the window_batch manifest, runs LLM inference on each window,
        fuses VLM output with optional metadata sources, and writes
        window-summaries.jsonl.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from autocut_core.stages.ac_source_prep.window_analysis.stage import (
            WindowAnalysisStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        window_batch = job_root / "window-analysis-batch.json"
        if not window_batch.is_file():
            return ToolResult.error(
                f"window-analysis-batch.json not found at {window_batch}. "
                "Run source_windows first."
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
        bus.put("window_batch", {"path": str(window_batch)}, stage="source_windows")

        stage = WindowAnalysisStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            # DB write: apply database_patch from window summaries
            _logger = logging.getLogger(__name__)
            try:
                db = get_db_client(str(job_root))
                if db is not None and db.is_available:
                    summaries_path = job_root / "window-summaries.jsonl"
                    book_id = _get_book_id(job_root)
                    if book_id and summaries_path.is_file():
                        merged_patch: dict[str, Any] = {
                            "scenes": [],
                            "subjects": [],
                            "boundaries": [],
                        }
                        with open(summaries_path, "r", encoding="utf-8") as _f:
                            for _line in _f:
                                _line = _line.strip()
                                if not _line:
                                    continue
                                _rec = json.loads(_line)
                                _patch = _rec.get("database_patch", {})
                                for _key in ("scenes", "subjects", "boundaries"):
                                    if _key in _patch and isinstance(_patch[_key], list):
                                        merged_patch.setdefault(_key, []).extend(_patch[_key])
                                if "subject_updates" in _patch:
                                    merged_patch.setdefault("subject_updates", []).extend(
                                        _patch["subject_updates"]
                                    )
                        db.apply_database_patch(book_id, merged_patch)
            except Exception as _db_err:
                _logger.warning("DB write failed for window_analysis: %s", _db_err)

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "window_analysis completed successfully.\n\n"
                f"Artifacts:\n- window_summaries: {paths.get('window_summaries', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"window_analysis failed: {exc}")


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