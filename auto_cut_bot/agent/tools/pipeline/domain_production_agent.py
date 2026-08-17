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
from auto_cut_bot.agent.tools.pipeline._domain_agent_base import DomainAgent
from auto_cut_bot.agent.tools.pipeline._domain_contract import (
    PRODUCTION_AGENT_CONTRACT,
    DomainAgentContract,
)
from auto_cut_bot.agent.tools.pipeline._skill_context import inject_skill_context
from auto_cut_bot.security.workspace_access import current_workspace_scope

if TYPE_CHECKING:
    from auto_cut_bot.agent.subagent import SubagentManager


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
class DomainProductionAgentTool(Tool, DomainAgent):
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

    _scopes = {"core"}
    read_only = False

    def __init__(self, subagent_manager: "SubagentManager | None" = None) -> None:
        self._subagent_manager = subagent_manager

    @classmethod
    def create(cls, ctx: ToolContext) -> "DomainProductionAgentTool":
        return cls(subagent_manager=ctx.subagent_manager)

    # ── DomainAgent ABC interface ──────────────────────────────────────────────

    @property
    def contract(self) -> DomainAgentContract:
        """The immutable contract declaring this agent's responsibilities.

        Production agent depends on the contract for skill_names, stage_names,
        and milestone — never on hardcoded stage names.
        """
        return PRODUCTION_AGENT_CONTRACT

    def _build_task_prompt(
        self,
        job_root: str,
        backend: str | None = None,
        mode: str | None = None,
        **extra: Any,
    ) -> str:
        """Build the task description the sub-agent will receive.

        Skill content is injected from the contract's skill_names —
        the contract is the single source of truth for which skills
        this agent requires.  Production-specific rules (HITL gate,
        deterministic execution) are added on top of the contract-driven
        base.
        """
        parts: list[str] = [
            inject_skill_context(list(self.contract.skill_names)),
            "",
            f"Goal: Complete production for job_root={job_root}.",
            "",
            "IMPORTANT RULES:",
            "- Run stages in the order described in the Skills above.",
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
        for key, value in extra.items():
            if value:
                parts.append(f"{key}: {value}")

        return "\n".join(parts)

    # ── Tool interface ────────────────────────────────────────────────────────

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

        Validates inputs, builds a task prompt from the contract, and
        spawns a sub-agent via SubagentManager.run_inline().
        """
        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        backend = kwargs["backend"]
        mode = kwargs.get("mode")

        task_prompt = self._build_task_prompt(
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