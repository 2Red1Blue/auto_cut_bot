"""SourceAgent — Domain Sub-Agent for source material preparation.

Wraps 7 source prep tools into a single coarse-grained tool.
The main agent delegates "prepare source material" to this sub-agent,
which internally orchestrates the pipeline stages:

  1. source_windows     — scan videos, produce window manifests
  2. window_analysis    — VLM semantic analysis per window
  3. event_cards        — compile event cards from summaries
  4. source_script_load — load and parse script text
  5. source_script_save — persist parsed script to DB
  6. source_metadata    — collect source metadata
  7. asr_transcript     — ASR transcription

Output milestone: source_ready.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.context import (
    RequestContext,
    ToolContext,
    current_request_context,
)
from auto_cut_bot.security.workspace_access import current_workspace_scope

if TYPE_CHECKING:
    from auto_cut_bot.agent.subagent import SubagentManager


# ── Task prompt template ────────────────────────────────────────────────────────


def _build_task_prompt(
    job_root: str,
    source_kind: str | None = None,
    input_root: str | None = None,
    backend: str | None = None,
    mode: str | None = None,
) -> str:
    """Build the task description the sub-agent will receive."""
    parts: list[str] = [
        f"Complete source material preparation for job_root={job_root}.",
        "",
        "You have access to the following pipeline tools. Use them in the order listed:",
        "",
        "1. **source_windows** — Scan video sources and generate sliding-window manifests.",
        "   Call with job_root, source_kind, and input_root (if local mode).",
        "",
        "2. **source_metadata** — Collect source metadata (book_id, title, episode count, etc.).",
        "   Call with job_root.",
        "",
        "3. **asr_transcript** — Run ASR transcription on the source videos.",
        "   Call with job_root and backend.",
        "",
        "4. **window_analysis** — Run VLM semantic analysis on each window.",
        "   Call with job_root and backend.",
        "",
        "5. **event_cards** — Compile event cards and highlight/hook candidates.",
        "   Call with job_root.",
        "",
        "6. **source_script_load** — Load the script file for parsing.",
        "   Call with job_root. The agent (you) will parse the script in your own context.",
        "",
        "7. **source_script_save** — Save the parsed script episodes to DB.",
        "   Call with job_root, episodes=[...], and parse_meta={...}.",
        "   Only call this AFTER you have completed parsing.",
        "",
        "IMPORTANT RULES:",
        "- Run stages in order. Each stage depends on the previous one completing.",
        "- source_script_load returns the script text. You must parse it into structured",
        "  JSON episodes in your own context before calling source_script_save.",
        "- If any stage fails, report the error and do not continue to subsequent stages.",
        "- When all stages complete, report: milestone=source_ready.",
    ]
    if source_kind:
        parts.append(f"\nSource kind: {source_kind}")
    if input_root:
        parts.append(f"Input root: {input_root}")
    if backend:
        parts.append(f"Backend: {backend}")
    if mode:
        parts.append(f"Mode: {mode}")

    return "\n".join(parts)


# ── Fallback: direct stage orchestration ────────────────────────────────────────


async def _run_stages_direct(
    job_root: str,
    source_kind: str | None = None,
    input_root: str | None = None,
    backend: str | None = None,
) -> str:
    """Run source prep stages directly (fallback when SubagentManager is unavailable).

    Invokes each stage class in sequence, collecting results.
    """
    from autocut_core import PipelineConfig, ArtifactBus

    from auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_windows.stage import (
        SourceWindowsStage,
    )
    from auto_cut_bot.pipeline.plugins.ac_source_prep.stages.window_analysis.stage import (
        WindowAnalysisStage,
    )
    from auto_cut_bot.pipeline.plugins.ac_source_prep.stages.event_cards.stage import (
        EventCardsStage,
    )

    root = Path(job_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    results: list[str] = []
    effective_backend = backend or "qwen"
    effective_source_kind = source_kind or "local"

    # 1. source_windows
    try:
        cfg = PipelineConfig(
            job_root=root,
            source_kind=effective_source_kind,
            backend=effective_backend,
            extra={"input_root": input_root} if input_root else {},
        )
        bus = ArtifactBus()
        stage = SourceWindowsStage()
        stage.config = cfg
        tasks = stage.prepare(bus)
        artifacts = stage.execute(bus, tasks)
        paths = {a.name: str(a.path) for a in artifacts}
        results.append(f"source_windows: OK — {len(paths)} artifacts")
    except Exception as exc:
        return ToolResult.error(f"source_windows failed: {exc}")

    # 2. window_analysis
    try:
        cfg = PipelineConfig(
            job_root=root,
            backend=effective_backend,
        )
        bus = ArtifactBus()
        stage = WindowAnalysisStage()
        stage.config = cfg
        tasks = stage.prepare(bus)
        artifacts = stage.execute(bus, tasks)
        paths = {a.name: str(a.path) for a in artifacts}
        results.append(f"window_analysis: OK — {len(paths)} artifacts")
    except Exception as exc:
        results.append(f"window_analysis: FAILED — {exc}")
        return ToolResult.error("\n".join(results))

    # 3. event_cards
    try:
        cfg = PipelineConfig(job_root=root)
        bus = ArtifactBus()
        stage = EventCardsStage()
        stage.config = cfg
        tasks = stage.prepare(bus)
        artifacts = stage.execute(bus, tasks)
        paths = {a.name: str(a.path) for a in artifacts}
        results.append(f"event_cards: OK — {len(paths)} artifacts")
    except Exception as exc:
        results.append(f"event_cards: FAILED — {exc}")
        return ToolResult.error("\n".join(results))

    results.append("\nmilestone=source_ready (partial — script/ASR/metadata stages require sub-agent)")
    return "\n".join(results)


# ── Tool ────────────────────────────────────────────────────────────────────────


@tool_parameters({
    "type": "object",
    "properties": {
        "job_root": {
            "type": "string",
            "description": "Pipeline job root directory (absolute path).",
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
        "backend": {
            "type": "string",
            "description": "LLM backend name (e.g. 'qwen', 'doubao').",
        },
        "mode": {
            "type": "string",
            "enum": ["auto", "interactive"],
            "description": "Execution mode: 'auto' runs without pauses, 'interactive' pauses for human input.",
        },
    },
    "required": ["job_root"],
})
class DomainSourceAgentTool(Tool):
    """Domain sub-agent that orchestrates the 7 source preparation pipeline stages.

    Instead of the main agent calling each source-prep tool individually
    (requiring 7+ round-trips), this tool delegates the entire source prep
    workflow to a sub-agent that can call the individual tools autonomously.

    The sub-agent is spawned via SubagentManager.run_inline() and given
    a structured task description with numbered steps.
    """

    _scopes = {"pipeline"}
    read_only = False

    def __init__(self, subagent_manager: "SubagentManager | None" = None) -> None:
        self._subagent_manager = subagent_manager

    @classmethod
    def create(cls, ctx: ToolContext) -> "DomainSourceAgentTool":
        return cls(subagent_manager=ctx.subagent_manager)

    @property
    def name(self) -> str:
        return "source_agent"

    @property
    def description(self) -> str:
        return (
            "Prepare source material for the auto-cut pipeline. "
            "Handles: video scanning, window analysis, event cards, "
            "script parsing (adaptive Direct/MapReduce), metadata collection, "
            "and ASR transcription. "
            "Output milestone: source_ready."
        )

    async def execute(self, **kwargs: Any) -> Any:
        """Delegate source preparation to a sub-agent.

        Validates inputs, builds a task prompt, and either spawns a sub-agent
        via SubagentManager.run_inline() or falls back to direct stage
        orchestration.
        """
        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        source_kind = kwargs.get("source_kind")
        input_root = kwargs.get("input_root")
        backend = kwargs.get("backend")
        mode = kwargs.get("mode")

        task_prompt = _build_task_prompt(
            job_root=str(job_root),
            source_kind=source_kind,
            input_root=input_root,
            backend=backend,
            mode=mode,
        )

        # Try the sub-agent path first
        manager = self._subagent_manager
        request_ctx = current_request_context()

        if manager is not None and request_ctx is not None and request_ctx.runtime is not None:
            return await self._run_via_subagent(
                manager=manager,
                request_ctx=request_ctx,
                task_prompt=task_prompt,
            )

        # Fallback: direct orchestration
        result = await _run_stages_direct(
            job_root=str(job_root),
            source_kind=source_kind,
            input_root=input_root,
            backend=backend,
        )
        return ToolResult(result) if not isinstance(result, ToolResult) else result

    async def _run_via_subagent(
        self,
        manager: "SubagentManager",
        request_ctx: "RequestContext",
        task_prompt: str,
    ) -> str:
        """Run the source preparation task via SubagentManager.run_inline()."""
        origin_channel = request_ctx.channel
        origin_chat_id = request_ctx.chat_id
        session_key = request_ctx.session_key or f"{origin_channel}:{origin_chat_id}"

        try:
            result = await manager.run_inline(
                task=task_prompt,
                label="source-agent",
                runtime=request_ctx.runtime,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                session_key=session_key,
                origin_message_id=request_ctx.message_id,
                workspace_scope=current_workspace_scope(),
            )
            return result
        except Exception as exc:
            return ToolResult.error(
                f"source_agent sub-agent failed: {exc}\n\n"
                "The sub-agent encountered an error. Check the job log for details."
            )