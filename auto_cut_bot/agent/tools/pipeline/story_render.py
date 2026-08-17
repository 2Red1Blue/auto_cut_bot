"""StoryRenderTool — 优化渲染：1 次编码 + N 次 stream copy。

基于 ffmpeg-render-optimization.md 设计文档，将 N 次独立 H.264 编码
替换为 1 次 master 编码 + N 次 stream copy（零重编码切割）。

接入方式：
  - 主路径：调用 render_optimized()（autocut_core.libs.render_optimized）
  - Fallback：如果 master 生成失败，回退到旧 autocut_core.libs.render_story_videos
"""

from __future__ import annotations

import json
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
        "mode": {
            "type": "string",
            "enum": ["auto", "interactive"],
            "description": "auto=overwrite existing renders, interactive=skip.",
            "default": "auto",
        },
        "render_jobs": {
            "type": "integer",
            "description": "Number of parallel render jobs (default 2).",
        },
        "subtitle_path": {
            "type": "string",
            "description": "Optional ASS/SRT subtitle file for burn-in.",
        },
    },
    "required": ["job_root"],
})
class StoryRenderTool(Tool):
    """Build render recipes and render final videos with optimized pipeline.

    Uses 1-encode + N-stream-copy strategy (render_optimized):
    - Master file: full-quality encode with all I-frames (-g 1)
    - Clip cutting: stream copy from master (~50ms per clip)
    - Concat: lossless join of all segments

    Falls back to legacy per-clip encoding if master generation fails.
    """

    _scopes = {"subagent"}
    human_review = False

    @property
    def name(self) -> str:
        return "story_render"

    @property
    def description(self) -> str:
        return (
            "Build render recipes from QC-reviewed story plans and render "
            "final video files. Uses optimized 1-encode + N-copy pipeline "
            "with fallback to legacy per-clip encoding."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute story_render with optimized rendering pipeline."""
        from autocut_core.libs.render_optimized import render_optimized

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        overwrite = kwargs.get("mode", "auto") == "auto"
        subtitle_path = kwargs.get("subtitle_path")

        # Load recipes
        recipes_dir = job_root / "story-render-recipes"
        index_path = recipes_dir / "index.json"
        if not index_path.is_file():
            return ToolResult.error(
                f"story-render-recipes/index.json not found at {index_path}. "
                "Run story_plans_materialize first."
            )

        try:
            recipes_index = json.loads(index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return ToolResult.error(f"Failed to read recipe index: {exc}")

        recipe_paths = recipes_index.get("recipes", [])
        if not recipe_paths:
            return ToolResult.error("No recipes found in index.json")

        output_dir = job_root / "story-renders"
        cache_root = job_root / ".render-cache" / "story-render"
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []

        for recipe_rel in recipe_paths:
            recipe_path = recipes_dir / recipe_rel
            if not recipe_path.is_file():
                errors.append({"recipe": recipe_rel, "error": "file not found"})
                continue

            try:
                recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
                output = render_optimized(
                    recipe=recipe,
                    output_dir=output_dir,
                    cache_dir=cache_root / recipe["story_id"],
                    overwrite=overwrite,
                    subtitle_path=subtitle_path,
                )
                results.append(output)
            except Exception as exc:
                # Fallback to legacy per-clip encoding
                try:
                    output = self._render_legacy(
                        recipe_path=recipe_path,
                        output_dir=output_dir,
                        cache_dir=cache_root,
                        overwrite=overwrite,
                    )
                    results.append(output)
                except Exception as fallback_exc:
                    errors.append({
                        "recipe": recipe_rel,
                        "error": f"optimized: {exc}, legacy: {fallback_exc}",
                    })

        # Write output index
        output_index = {
            "renders": results,
            "errors": errors,
            "total": len(results),
            "failed": len(errors),
        }
        output_index_path = output_dir / "index.json"
        output_index_path.parent.mkdir(parents=True, exist_ok=True)
        output_index_path.write_text(
            json.dumps(output_index, ensure_ascii=False, indent=2), encoding="utf-8")

        mark_stage_complete(None, self.name, {
            "story_render": str(output_index_path),
        })

        summary = [f"story_render: {len(results)} ok, {len(errors)} failed"]
        for e in errors[:5]:
            summary.append(f"  - {e['recipe']}: {e['error'][:120]}")
        return ToolResult("\n".join(summary))

    @staticmethod
    def _render_legacy(
        recipe_path: Path, output_dir: Path, cache_dir: Path, overwrite: bool,
    ) -> dict[str, Any]:
        """Fallback to the old autocut_core.libs.render_story_videos path."""
        import subprocess

        # Try importing the legacy render function
        try:
            from autocut_core.libs.render_story_videos import render_recipe
        except ImportError:
            raise RuntimeError(
                "Optimized render failed and legacy autocut_core not available. "
                "Ensure autocut-core package is installed."
            )

        recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
        return render_recipe(
            recipe_path=recipe_path,
            output_dir=output_dir,
            cache_root=cache_dir,
            ffmpeg="ffmpeg",
            ffprobe="ffprobe",
            overwrite=overwrite,
        )