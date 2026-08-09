"""ChapterDigestsTool — 逐章语义摘要 (每 ~6 集合一)。

Wraps ChapterDigestsStage as a Tool.  Merges multiple episode digests
into chapter-level summaries using LLM.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.context import ToolContext


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
        from auto_cut_bot.pipeline.plugins.ac_series_knowledge.stages.chapter_digests.stage import (
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
            return ToolResult(
                "chapter_digests completed successfully.\n\n"
                f"Artifacts:\n- chapter_digests: {paths.get('chapter_digests', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"chapter_digests failed: {exc}")