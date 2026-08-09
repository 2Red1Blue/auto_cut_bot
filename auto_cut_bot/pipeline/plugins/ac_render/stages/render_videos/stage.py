"""story_render Stage — 构建渲染配方并本地渲染成片。

输入: story_qc_review
输出: story_render (story-renders/index.json)
"""

from __future__ import annotations

from pathlib import Path

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.io import update_project_stage


class RenderStage(Stage):
    """Render — 构建渲染配方并本地渲染成片。"""

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="story_render",
            input_artifacts=["story_qc_review"],
            output_artifacts=["story_render"],
            description="构建渲染配方并渲染 Story 成片",
            db_reads=[],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="render", payload={
            "qc_review": self.resolve_artifact_path(bus, "story_qc_review", "story_qc_review"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root: Path = cfg.job_root

        from autocut_core.libs.build_story_render_recipes import build as _build_recipes
        from autocut_core.libs.validate_story_render_recipes import validate as _validate_recipes
        from autocut_core.libs.validate_story_renders import validate as _validate_renders
        from autocut_core.libs.render_story_videos import render as _render

        _build_recipes(root)
        recipe_report = _validate_recipes(root)
        if not recipe_report["ok"]:
            for err in recipe_report["errors"][:10]:
                print(f"[render_videos] WARNING: {err}")

        _render(
            root,
            jobs=cfg.extra.get("render_jobs", 2),
            overwrite=(cfg.mode == "auto"),
        )
        render_report = _validate_renders(root)
        if not render_report["ok"]:
            for err in render_report["errors"][:10]:
                print(f"[render_videos] ERROR: {err}")

        index_path = root / "story-renders" / "index.json"
        ref = bus.put("story_render", {"path": str(index_path)}, stage="story_render")
        update_project_stage(root / "project.json", "story_render", "completed",
                             outputs={"story_render": str(index_path)})
        return [ref]