"""SeriesAssignmentTool — 剧集到 Series 的合约化分配。

Wraps AssignmentStage as a Tool.  Assigns each episode to a specific
Series based on the registry, episode/chapter digests, and event cards.
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
class SeriesAssignmentTool(Tool):
    """Tool that assigns episodes to Series via contract-based assignment.

    Consumes the series registry, episode digests, chapter digests, and
    event cards to produce a series-assignment-batch.json that maps each
    episode to its canonical Series.
    """

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
            return ToolResult(
                "series_assignment completed successfully.\n\n"
                f"Artifacts:\n- series_assignment: {paths.get('series_assignment', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"series_assignment failed: {exc}")