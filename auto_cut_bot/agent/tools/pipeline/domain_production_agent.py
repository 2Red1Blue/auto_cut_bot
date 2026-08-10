"""ProductionAgent — Domain Sub-Agent for production rendering.

Wraps 7 production tools into a single coarse-grained tool.
The main agent delegates "complete production" to this sub-agent,
which internally orchestrates the pipeline stages:

  1. story_evidence         — retrieve evidence for story plans
  2. span_candidates        — find candidate spans from evidence
  3. story_plans            — generate story plans
  4. story_plans_materialize — materialize plans into concrete assets
  5. story_qc               — quality check on materialized plans
  6. story_qc_review        — HUMAN REVIEW NODE (HITL gate)
  7. story_render           — final video rendering

Output milestone: rendered.
HITL: pauses at story_qc_review for human review.
Production is deterministic: no nested LLM calls beyond what the
individual tools already do.
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
    backend: str,
    mode: str | None = None,
) -> str:
    """Build the task description the sub-agent will receive."""
    parts: list[str] = [
        f"Complete production for job_root={job_root}.",
        "",
        "You have access to the following pipeline tools. Work through the steps in order:",
        "",
        "**Phase 1 — Evidence & Span Candidates:**",
        "1. **story_evidence** — Retrieve evidence from the source material for story plans.",
        "   Call with job_root.",
        "2. **span_candidates** — Find candidate spans from the retrieved evidence.",
        "   Call with job_root.",
        "",
        "**Phase 2 — Plan Generation & Materialization:**",
        "3. **story_plans** — Generate story plans from the evidence and span candidates.",
        "   Call with job_root and backend.",
        "4. **story_plans_materialize** — Materialize plans into concrete assets.",
        "   Call with job_root. This produces the materialized plan artifacts.",
        "",
        "**Phase 3 — Quality Check & Human Review:**",
        "5. **story_qc** — Run quality checks on the materialized plans.",
        "   Call with job_root. This produces QC results.",
        "6. **story_qc_review** — HUMAN REVIEW NODE.",
        "   This is a human-in-the-loop gate. Call story_qc_review with job_root.",
        "   In interactive mode: if no decisions are provided, the tool returns a prompt",
        "   asking for human review. You MUST report \"awaiting review\" and pause.",
        "   In auto mode: decisions are generated automatically.",
        "",
        "**Phase 4 — Final Rendering:**",
        "7. **story_render** — Perform final video rendering from the approved plans.",
        "   Call with job_root and backend.",
        "",
        "IMPORTANT RULES:",
        "- Run stages in order. Each stage depends on the previous one completing.",
        "- If any stage fails, report the error and do not continue to subsequent stages.",
        "- At story_qc_review: in interactive mode, if human input is required, pause and",
        "  report \"awaiting review\" with the QC results summary.",
        "- Production is deterministic: do NOT make additional LLM calls beyond what the",
        "  individual tools already perform internally.",
        "- When all stages complete, report: milestone=rendered.",
    ]
    if backend:
        parts.append(f"\nBackend: {backend}")
    if mode:
        parts.append(f"Mode: {mode}")

    return "\n".join(parts)


# ── Tool ────────────────────────────────────────────────────────────────────────


@tool_parameters({
    "type": "object",
    "properties": {
        "job_root": {
            "type": "string",
            "description": "Pipeline job root directory (absolute path).",
        },
        "backend": {
            "type": "string",
            "description": "LLM backend name (e.g. 'qwen', 'doubao').",
        },
        "mode": {
            "type": "string",
            "enum": ["auto", "interactive"],
            "description": "Execution mode: 'auto' runs without pauses, 'interactive' pauses at story_qc_review for human review.",
        },
    },
    "required": ["job_root", "backend"],
})
class DomainProductionAgentTool(Tool):
    """Domain sub-agent that orchestrates the 7 production pipeline stages.

    Instead of the main agent calling each production tool individually
    (requiring 7+ round-trips), this tool delegates the entire production
    workflow to a sub-agent that can call the individual tools autonomously.

    The sub-agent is spawned via SubagentManager.run_inline() and given
    a structured task description with numbered steps.

    HITL: story_qc_review is a human-in-the-loop gate. In interactive mode,
    the sub-agent pauses and reports "awaiting review" when it reaches
    the QC review stage. The human reviewer must provide decisions before
    the workflow can continue.

    Production is deterministic: no nested LLM calls beyond what the
    individual tools already do.
    """

    _scopes = {"pipeline"}
    read_only = False

    def __init__(self, subagent_manager: "SubagentManager | None" = None) -> None:
        self._subagent_manager = subagent_manager

    @classmethod
    def create(cls, ctx: ToolContext) -> "DomainProductionAgentTool":
        return cls(subagent_manager=ctx.subagent_manager)

    @property
    def name(self) -> str:
        return "production_agent"

    @property
    def description(self) -> str:
        return (
            "Execute production: evidence retrieval, span candidates, "
            "story plans, plan materialization, quality check, "
            "human QC review, and final rendering. "
            "Output milestone: rendered. "
            "HITL: pauses at story_qc_review for human review."
        )

    async def execute(self, **kwargs: Any) -> Any:
        """Delegate production to a sub-agent.

        Validates inputs, builds a task prompt, and spawns a sub-agent
        via SubagentManager.run_inline().
        """
        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        backend = kwargs["backend"]
        mode = kwargs.get("mode")

        task_prompt = _build_task_prompt(
            job_root=str(job_root),
            backend=backend,
            mode=mode,
        )

        manager = self._subagent_manager
        request_ctx = current_request_context()

        if manager is None:
            return ToolResult.error(
                "production_agent requires a SubagentManager. "
                "Ensure the tool is used within an active agent session."
            )

        if request_ctx is None or request_ctx.runtime is None:
            return ToolResult.error(
                "production_agent requires an active request context with a runtime. "
                "Ensure the tool is invoked from within an agent loop."
            )

        return await self._run_via_subagent(
            manager=manager,
            request_ctx=request_ctx,
            task_prompt=task_prompt,
        )

    async def _run_via_subagent(
        self,
        manager: "SubagentManager",
        request_ctx: "RequestContext",
        task_prompt: str,
    ) -> str:
        """Run the production task via SubagentManager.run_inline()."""
        origin_channel = request_ctx.channel
        origin_chat_id = request_ctx.chat_id
        session_key = request_ctx.session_key or f"{origin_channel}:{origin_chat_id}"

        try:
            result = await manager.run_inline(
                task=task_prompt,
                label="production-agent",
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
                f"production_agent sub-agent failed: {exc}\n\n"
                "The sub-agent encountered an error. Check the job log for details."
            )