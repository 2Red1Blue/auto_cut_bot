"""SourceWindowsTool — 扫描视频源并生成滑动窗口清单。

Wraps SourceWindowsStage as a Tool so the LLM agent can trigger
the first pipeline stage: scan local/remote video sources, ffprobe
them, and produce source_manifest, window_manifest, and window_batch.
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
        "input_root": {
            "type": "string",
            "description": "Directory containing video files (local mode).",
        },
        "url_list": {
            "type": "string",
            "description": "Path to a remote URL manifest file (remote mode).",
        },
        "source_kind": {
            "type": "string",
            "enum": ["local", "remote"],
            "description": "Source mode: 'local' scans directories, 'remote' reads URL manifests.",
        },
        "window_seconds": {
            "type": "number",
            "description": "Length of each sliding window in seconds (default 240.0).",
        },
        "overlap_seconds": {
            "type": "number",
            "description": "Overlap between consecutive windows in seconds (default 12.0).",
        },
        "backend": {
            "type": "string",
            "description": "LLM backend name for downstream stages (e.g. 'qwen', 'doubao').",
        },
        "extract_local": {
            "type": "boolean",
            "description": "Whether to extract window video clips via ffmpeg (default true).",
        },
        "ffmpeg": {
            "type": "string",
            "description": "Path to ffmpeg executable (default 'ffmpeg').",
        },
        "overwrite": {
            "type": "boolean",
            "description": "Whether to overwrite existing window clips (default false).",
        },
    },
    "required": ["job_root", "source_kind"],
})
class SourceWindowsTool(Tool):
    """Tool that scans video sources and generates sliding-window manifests.

    This is the first pipeline stage.  It discovers video files, probes them
    with ffprobe, and slices them into overlapping windows for downstream
    semantic analysis.
    """

    human_review = False

    @property
    def name(self) -> str:
        return "source_windows"

    @property
    def description(self) -> str:
        return (
            "Scan video sources (local directory or remote URLs) and generate "
            "sliding-window manifests: source_manifest.json, window_manifest.json, "
            "and window-analysis-batch.json. This is the first pipeline stage."
        )

    @property
    def read_only(self) -> bool:
        return False

    @classmethod
    def config_cls(cls):
        return None

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the source_windows stage.

        Runs SourceWindowsStage with the given configuration, producing
        source_manifest, window_manifest, and window_batch artifacts.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_windows.stage import (
            SourceWindowsStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        job_root.mkdir(parents=True, exist_ok=True)

        extra: dict[str, Any] = {}
        if kwargs.get("input_root"):
            extra["input_root"] = kwargs["input_root"]
        if kwargs.get("url_list"):
            extra["url_list"] = kwargs["url_list"]
        if kwargs.get("ffmpeg"):
            extra["ffmpeg"] = kwargs["ffmpeg"]
        extra["extract_local"] = kwargs.get("extract_local", True)
        extra["overwrite"] = kwargs.get("overwrite", False)

        cfg = PipelineConfig(
            job_root=job_root,
            source_kind=kwargs["source_kind"],
            window_seconds=kwargs.get("window_seconds", 240.0),
            overlap_seconds=kwargs.get("overlap_seconds", 12.0),
            backend=kwargs.get("backend", "qwen"),
            extra=extra,
        )

        bus = ArtifactBus()
        stage = SourceWindowsStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            # DB write: upsert book metadata from source_manifest
            _logger = logging.getLogger(__name__)
            try:
                db = get_db_client(str(job_root))
                if db is not None and db.is_available:
                    manifest_path = job_root / "source_manifest.json"
                    if manifest_path.is_file():
                        with open(manifest_path, "r", encoding="utf-8") as _f:
                            _data = json.load(_f)
                        book_id = _data.get("book_id") or _data.get("id", "")
                        book_name = _data.get("book_name") or _data.get("title", "")
                        total_episodes = _data.get("total_episodes")
                        if book_id:
                            db.upsert_book(
                                book_id=book_id,
                                book_name=book_name,
                                total_episodes=total_episodes,
                            )
            except Exception as _db_err:
                _logger.warning("DB write failed for source_windows: %s", _db_err)

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "source_windows completed successfully.\n\n"
                f"Artifacts:\n- source_manifest: {paths.get('source_manifest', 'N/A')}\n"
                f"- window_manifest: {paths.get('window_manifest', 'N/A')}\n"
                f"- window_batch: {paths.get('window_batch', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"source_windows failed: {exc}")