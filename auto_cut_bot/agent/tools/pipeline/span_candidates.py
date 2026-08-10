"""SpanCandidatesTool — 从证据包编译时间跨度 (Span) 候选。

Wraps SpanCandidatesStage as a Tool.  Compiles time-span candidates
from evidence packets, computing optimal clip boundaries for each story.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.context import ToolContext
from auto_cut_bot.pipeline.state import mark_stage_complete


@tool_parameters({
    "type": "object",
    "properties": {
        "job_root": {
            "type": "string",
            "description": "Root directory for the pipeline job (absolute path).",
        },
    },
    "required": ["job_root"],
})
class SpanCandidatesTool(Tool):
    """Tool that compiles time-span candidates from evidence packets.

    Computes optimal clip boundaries for each story using anchor-based
    span compilation.  Produces span-candidates/index.json and a
    human-readable span-candidate-review.md.
    """
    _scopes = {"subagent"}


    human_review = False

    @property
    def name(self) -> str:
        return "span_candidates"

    @property
    def description(self) -> str:
        return (
            "Compile time-span candidates from story evidence packets. "
            "Computes optimal clip boundaries via anchor-based compilation. "
            "Produces span-candidates/index.json and span-candidate-review.md."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the span_candidates stage.

        Loads the evidence index, validates evidence packets, and compiles
        span candidates for each story.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from auto_cut_bot.pipeline.plugins.ac_plan_orchestration.stages.span_candidates.stage import (
            SpanCandidatesStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        evidence_index = job_root / "story-evidence" / "index.json"
        if not evidence_index.is_file():
            return ToolResult.error(
                f"story-evidence/index.json not found at {evidence_index}. "
                "Run story_evidence first."
            )

        cfg = PipelineConfig(
            job_root=job_root,
            backend="qwen",
            extra={},
        )

        bus = ArtifactBus()
        bus.put("story_evidence", {"path": str(evidence_index)}, stage="story_evidence")

        stage = SpanCandidatesStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "span_candidates completed successfully.\n\n"
                f"Artifacts:\n- span_candidates: {paths.get('span_candidates', 'N/A')}\n"
                f"Review: {job_root / 'span-candidate-review.md'}"
            )
        except Exception as exc:
            return ToolResult.error(f"span_candidates failed: {exc}")