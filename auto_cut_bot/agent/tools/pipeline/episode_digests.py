"""EpisodeDigestsTool — 逐集语义摘要生成。

Wraps EpisodeDigestsStage as a Tool.  Uses LLM to generate per-episode
narrative summaries from event cards, window summaries, and manifests.
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
        from auto_cut_bot.pipeline.plugins.ac_series_knowledge.stages.episode_digests.stage import (
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
            return ToolResult(
                "episode_digests completed successfully.\n\n"
                f"Artifacts:\n- episode_digests: {paths.get('episode_digests', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"episode_digests failed: {exc}")