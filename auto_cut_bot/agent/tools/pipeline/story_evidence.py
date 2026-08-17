"""StoryEvidenceTool — 为已批准故事构建 Story Evidence Packet (证据包)。

Wraps EvidenceStage as a Tool.  Builds evidence packets for each approved
story, linking scripts to source material with timestamped anchors.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.context import ToolContext
from auto_cut_bot.agent.runtime.state import mark_stage_complete


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
class StoryEvidenceTool(Tool):
    """Tool that builds Story Evidence Packets for approved stories.

    Links each approved story script to source material with timestamped
    anchors, producing story-evidence/index.json.  This is a local
    assembly stage.
    """
    _scopes = {"subagent"}


    human_review = False

    @property
    def name(self) -> str:
        return "story_evidence"

    @property
    def description(self) -> str:
        return (
            "Build Story Evidence Packets for each approved story, linking "
            "script segments to source material with timestamped anchors. "
            "Produces story-evidence/index.json."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the story_evidence stage.

        Loads the approval manifest and preflight data, builds evidence
        packets for each approved story, and writes the evidence index.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from autocut_core.stages.ac_plan_orchestration.evidence.stage import (
            EvidenceStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        approval_path = job_root / "story-approval.json"
        if not approval_path.is_file():
            return ToolResult.error(
                f"story-approval.json not found at {approval_path}. "
                "Run story_approval first."
            )

        cfg = PipelineConfig(
            job_root=job_root,
            backend="qwen",
            extra={},
        )

        bus = ArtifactBus()
        bus.put("story_approval", {"path": str(approval_path)}, stage="story_approval")

        stage = EvidenceStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "story_evidence completed successfully.\n\n"
                f"Artifacts:\n- story_evidence: {paths.get('story_evidence', 'N/A')}"
            )
        except Exception as exc:
            return ToolResult.error(f"story_evidence failed: {exc}")