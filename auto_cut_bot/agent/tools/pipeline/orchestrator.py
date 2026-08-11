"""PipelineOrchestratorTool — 一键执行完整自动剪辑流水线。

Wraps the PipelineOrchestrator from autocut_core as a single Tool,
so the LLM agent can trigger the entire 21-stage pipeline in one call
instead of 21 separate tool invocations.

The orchestrator handles:
  - Stage discovery via entry_points + filesystem scan
  - Sequential execution with caching / checkpointing
  - Human-review pause points (story_approval, story_qc_review)
  - Auto-mode decision functions for human nodes
  - Recovery / rework loops
  - Failure logging and project.json state tracking
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters


@tool_parameters({
    "type": "object",
    "properties": {
        "job_root": {
            "type": "string",
            "description": "Root directory for the pipeline job (absolute path). All artifacts are written here.",
        },
        "mode": {
            "type": "string",
            "enum": ["auto", "interactive"],
            "description": "Execution mode: 'auto' runs human nodes with decision functions; 'interactive' pauses for human input.",
        },
        "backend": {
            "type": "string",
            "description": "LLM backend name (e.g. 'qwen', 'doubao'). Defaults to config or env var.",
        },
        "from_stage": {
            "type": "string",
            "description": "Start from this stage (skip all stages before it). Leave empty to run from the beginning.",
        },
        "to_stage": {
            "type": "string",
            "description": "Stop after this stage (skip all stages after it). Leave empty to run to the end.",
        },
        "source_kind": {
            "type": "string",
            "enum": ["local", "remote"],
            "description": "Source mode: 'local' scans directories, 'remote' reads URL manifests.",
        },
        "input_root": {
            "type": "string",
            "description": "Directory containing video files (local mode).",
        },
        "url_list": {
            "type": "string",
            "description": "Path to a remote URL manifest file (remote mode).",
        },
        "workers": {
            "type": "integer",
            "description": "Number of concurrent LLM workers (default from config).",
        },
        "requests_per_minute": {
            "type": "integer",
            "description": "Rate limit for LLM API calls (default from config).",
        },
        "dry_run": {
            "type": "boolean",
            "description": "If true, list stages without executing them.",
        },
        "force": {
            "type": "boolean",
            "description": "If true, force re-execution even if cache is valid.",
        },
    },
    "required": ["job_root"],
})
class PipelineOrchestratorTool(Tool):
    """Tool that orchestrates the full auto-cut pipeline.

    Instead of calling 21 individual stage tools sequentially
    (each requiring a round-trip through the LLM), this single tool
    runs the entire pipeline: source prep → series knowledge →
    story generation → plan orchestration → QC → render.

    The orchestrator discovers stages, manages caching, handles
    human-review nodes, and writes project.json checkpoints.
    """

    human_review = False
    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "pipeline_orchestrator"

    @property
    def description(self) -> str:
        return (
            "Run the complete auto-cut pipeline in one call. "
            "Executes all 21 stages (source_windows through story_render) "
            "sequentially with caching, checkpointing, and human-review "
            "pause points. Use this instead of calling individual stage "
            "tools one at a time."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the full pipeline orchestration.

        Sets up PipelineConfig, discovers stages, and runs the
        PipelineOrchestrator from start to finish.
        """
        from autocut_core import PipelineConfig, StageRegistry
        from autocut_core.orchestrator.pipeline import PipelineOrchestrator

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        job_root.mkdir(parents=True, exist_ok=True)

        # Build config
        config_kwargs: dict[str, Any] = {"job_root": job_root}
        if kwargs.get("mode"):
            config_kwargs["mode"] = kwargs["mode"]
        if kwargs.get("backend"):
            config_kwargs["backend"] = kwargs["backend"]
        if kwargs.get("workers"):
            config_kwargs["workers"] = kwargs["workers"]
        if kwargs.get("requests_per_minute"):
            config_kwargs["requests_per_minute"] = kwargs["requests_per_minute"]
        if kwargs.get("dry_run"):
            config_kwargs["dry_run"] = True
        if kwargs.get("source_kind"):
            config_kwargs["source_kind"] = kwargs["source_kind"]

        # Handle extra config for source_windows
        extra: dict[str, Any] = {}
        if kwargs.get("input_root"):
            extra["input_root"] = kwargs["input_root"]
        if kwargs.get("url_list"):
            extra["url_list"] = kwargs["url_list"]
        if extra:
            config_kwargs["extra"] = extra

        cfg = PipelineConfig(**config_kwargs)

        # Discover stages
        registry = StageRegistry()
        registry.discover()

        stages = registry.pipeline_order()
        if not stages:
            return ToolResult.error(
                "No pipeline stages discovered. "
                "Ensure autocut_core is installed with stage plugins."
            )

        # Run orchestrator
        orchestrator = PipelineOrchestrator(
            cfg, registry,
            force=kwargs.get("force", False),
        )

        from_stage = kwargs.get("from_stage")
        to_stage = kwargs.get("to_stage")

        try:
            import asyncio
            await asyncio.to_thread(
                orchestrator.run,
                from_stage=from_stage,
                to_stage=to_stage,
            )

            # Read project.json for summary
            project_path = job_root / "project.json"
            summary_lines: list[str] = []
            if project_path.is_file():
                import json as _json
                project = _json.loads(project_path.read_text(encoding="utf-8"))
                stage_states = project.get("stages", {})
                completed = sum(
                    1 for s in stage_states.values()
                    if isinstance(s, dict) and s.get("status") == "completed"
                )
                failed = sum(
                    1 for s in stage_states.values()
                    if isinstance(s, dict) and s.get("status") == "failed"
                )
                summary_lines.append(
                    f"Pipeline complete: {completed} completed, "
                    f"{failed} failed, {len(stage_states)} total"
                )

            return ToolResult(
                "pipeline_orchestrator completed successfully.\n\n"
                f"Job root: {job_root}\n"
                f"Stages executed: {len(stages)}\n"
                + "\n".join(summary_lines)
            )
        except Exception as exc:
            return ToolResult.error(
                f"pipeline_orchestrator failed: {exc}\n\n"
                f"Job root: {job_root}\n"
                f"Check {job_root}/failure.json for details."
            )