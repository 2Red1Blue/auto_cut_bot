"""SourceAgent — Domain Sub-Agent for source material preparation.

Wraps source prep tools into a single coarse-grained tool.
The main agent delegates "prepare source material" to this sub-agent,
which internally orchestrates the pipeline stages.

Stage ordering is declared by SOURCE_AGENT_CONTRACT, not hardcoded.
Output milestone: source_ready.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.context import (
    ToolContext,
    current_request_context,
)
from auto_cut_bot.agent.tools.pipeline._domain_agent_base import DomainAgent
from auto_cut_bot.agent.tools.pipeline._domain_contract import (
    SOURCE_AGENT_CONTRACT,
    DomainAgentContract,
)

if TYPE_CHECKING:
    from auto_cut_bot.agent.subagent import SubagentManager


# ── Fallback: direct stage orchestration ────────────────────────────────────────


async def _run_stages_direct(
    job_root: str,
    contract: DomainAgentContract,
    source_kind: str | None = None,
    input_root: str | None = None,
    backend: str | None = None,
) -> str:
    """Run source prep stages directly (fallback when SubagentManager is unavailable).

    Uses StageOrchestrator with per-stage config overrides because:
    - source_windows needs source_kind + input_root
    - window_analysis needs backend
    - event_cards needs only job_root
    """
    from auto_cut_bot.agent.tools.pipeline._stage_orchestrator import (
        StageOrchestrator,
    )
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

    # stage -> config overrides (only stages that can run directly)
    stage_sequence: list[tuple[type, dict[str, Any] | None]] = [
        (SourceWindowsStage, {
            "source_kind": effective_source_kind,
            "extra": {"input_root": input_root} if input_root else {},
        }),
        (WindowAnalysisStage, {"backend": effective_backend}),
        (EventCardsStage, None),
    ]

    stage_results = await StageOrchestrator.execute_sequence(
        stage_sequence,
        job_root=root,
        backend=effective_backend,
    )

    for r in stage_results:
        if r.ok:
            results.append(r.summary_line())
        else:
            results.append(r.summary_line())
            return ToolResult.error("\n".join(results))

    results.append(
        f"\nmilestone={contract.milestone} "
        "(partial — script/ASR/metadata stages require sub-agent)"
    )
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
class DomainSourceAgentTool(Tool, DomainAgent):
    """Domain sub-agent that orchestrates source preparation pipeline stages.

    Instead of the main agent calling each source-prep tool individually
    (requiring many round-trips), this tool delegates the entire source prep
    workflow to a sub-agent that can call the individual tools autonomously.

    The sub-agent is spawned via SubagentManager.run_inline() and given
    a structured task description.

    Inherits from DomainAgent ABC, declaring its contract via the
    SOURCE_AGENT_CONTRACT. Stage ordering and skill injection follow the
    contract rather than hardcoded lists.
    """

    _scopes = {"core"}
    read_only = False

    def __init__(self, subagent_manager: "SubagentManager | None" = None) -> None:
        self._subagent_manager = subagent_manager

    @classmethod
    def create(cls, ctx: ToolContext) -> "DomainSourceAgentTool":
        return cls(subagent_manager=ctx.subagent_manager)

    # ── DomainAgent contract ──────────────────────────────────────────────────

    @property
    def contract(self) -> DomainAgentContract:
        """The immutable contract declaring this agent's responsibilities."""
        return SOURCE_AGENT_CONTRACT

    # ── Tool interface ────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self.contract.agent_name

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

        Validates inputs, builds a task prompt using the contract's
        skill_names, and either spawns a sub-agent via
        SubagentManager.run_inline() or falls back to direct stage
        orchestration.
        """
        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        source_kind = kwargs.get("source_kind")
        input_root = kwargs.get("input_root")
        backend = kwargs.get("backend")
        mode = kwargs.get("mode")

        task_prompt = self._build_task_prompt(
            job_root=str(job_root),
            backend=backend,
            mode=mode,
            source_kind=source_kind,
            input_root=input_root,
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
            contract=self.contract,
            source_kind=source_kind,
            input_root=input_root,
            backend=backend,
        )
        return ToolResult(result) if not isinstance(result, ToolResult) else result