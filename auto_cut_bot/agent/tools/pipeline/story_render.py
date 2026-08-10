"""StoryRenderTool — 构建渲染配方并本地渲染成片。

Wraps RenderStage as a Tool.  Builds rendering recipes from QC-reviewed
plans and executes local ffmpeg rendering to produce final video files.
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
        "mode": {
            "type": "string",
            "enum": ["interactive", "auto"],
            "description": "Pipeline mode: 'auto' overwrites existing renders.",
            "default": "auto",
        },
        "render_jobs": {
            "type": "integer",
            "description": "Number of parallel render jobs (default 2).",
        },
    },
    "required": ["job_root"],
})
class StoryRenderTool(Tool):
    """Tool that builds render recipes and renders final video files.

    Builds rendering recipes from QC-reviewed plans, validates them,
    and executes local ffmpeg rendering to produce the final story videos.
    Produces story-renders/index.json.
    """

    human_review = False

    @property
    def name(self) -> str:
        return "story_render"

    @property
    def description(self) -> str:
        return (
            "Build render recipes from QC-reviewed story plans and execute "
            "local ffmpeg rendering to produce final video files. "
            "Produces story-renders/index.json."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the story_render stage.

        Builds render recipes, validates them, and runs ffmpeg rendering
        for all approved stories.
        """
        from autocut_core import PipelineConfig, ArtifactBus
        from auto_cut_bot.pipeline.plugins.ac_render.stages.render_videos.stage import (
            RenderStage,
        )

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        qc_review = job_root / "story-qc" / "index.json"
        if not qc_review.is_file():
            return ToolResult.error(
                f"story-qc/index.json not found at {qc_review}. "
                "Run story_qc and story_qc_review first."
            )

        cfg = PipelineConfig(
            job_root=job_root,
            backend="qwen",
            mode=kwargs.get("mode", "auto"),
            extra={"render_jobs": kwargs.get("render_jobs", 2)},
        )

        bus = ArtifactBus()
        bus.put("story_qc_review", {"path": str(qc_review)}, stage="story_qc_review")

        stage = RenderStage()
        stage.config = cfg

        try:
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            paths = {a.name: str(a.path) for a in artifacts}

            mark_stage_complete(None, self.name, paths)

            return ToolResult(
                "story_render completed successfully.\n\n"
                f"Artifacts:\n- story_render: {paths.get('story_render', 'N/A')}\n"
                f"Render output: {job_root / 'story-renders'}"
            )
        except Exception as exc:
            return ToolResult.error(f"story_render failed: {exc}")