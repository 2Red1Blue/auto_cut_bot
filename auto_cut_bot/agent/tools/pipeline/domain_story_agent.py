"""StoryAgent — Domain Sub-Agent for story generation.

Wraps story generation pipeline tools into a single coarse-grained tool.
The main agent delegates "generate story content" to this sub-agent,
which internally orchestrates the pipeline stages.

Stage ordering and skill injection are declared by STORY_AGENT_CONTRACT,
not hardcoded.  The contract is the single source of truth for which
stages this agent owns and in what order they execute.

Output milestone: script_approved.
HITL: pauses at story_approval for human review.
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
    STORY_AGENT_CONTRACT,
    DomainAgentContract,
)
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
            "description": "Execution mode: 'auto' runs without pauses, 'interactive' pauses at story_approval for human review.",
        },
    },
    "required": ["job_root", "backend"],
})
class DomainStoryAgentTool(Tool, DomainAgent):
    """Domain sub-agent that orchestrates the story generation pipeline stages.

    Instead of the main agent calling each story-generation tool individually
    (requiring many round-trips), this tool delegates the entire story generation
    workflow to a sub-agent that can call the individual tools autonomously.

    The sub-agent is spawned via SubagentManager.run_inline() and given
    a structured task description built from the contract's skill_names.

    Inherits from DomainAgent ABC, declaring its contract via the
    STORY_AGENT_CONTRACT. Stage ordering and skill injection follow the
    contract rather than hardcoded lists.

    HITL: story_approval is a human-in-the-loop gate. In interactive mode,
    the sub-agent pauses and reports "awaiting approval" when it reaches
    the approval stage. The human reviewer must provide decisions before
    the workflow can continue.
    """

    _scopes = {"core"}
    read_only = False

    def __init__(self, subagent_manager: "SubagentManager | None" = None) -> None:
        self._subagent_manager = subagent_manager

    @classmethod
    def create(cls, ctx: ToolContext) -> "DomainStoryAgentTool":
        return cls(subagent_manager=ctx.subagent_manager)

    # ── DomainAgent contract ──────────────────────────────────────────────────

    @property
    def contract(self) -> DomainAgentContract:
        """The immutable contract declaring this agent's responsibilities."""
        return STORY_AGENT_CONTRACT

    # ── Tool interface ────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self.contract.agent_name

    @property
    def description(self) -> str:
        return (
            "Generate story content: Series Bible, character registry, "
            "story catalog, portfolio, treatments, scripts, preflight, "
            "and human approval. "
            "Output milestone: script_approved. "
            "HITL: pauses at story_approval for human review."
        )

    async def execute(self, **kwargs: Any) -> Any:
        """Delegate story generation to a sub-agent.

        Validates inputs, builds a task prompt from the contract, and either
        spawns a sub-agent via SubagentManager.run_inline() or returns an
        error if no sub-agent manager is available.
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
                "story_agent requires a SubagentManager. "
                "Ensure the tool is used within an active agent session."
            )

        if request_ctx is None or request_ctx.runtime is None:
            return ToolResult.error(
                "story_agent requires an active request context with a runtime. "
                "Ensure the tool is invoked from within an agent loop."
            )

        return await self._run_via_subagent(
            manager=manager,
            request_ctx=request_ctx,
            task_prompt=task_prompt,
        )

    # ── Domain-specific prompt construction ────────────────────────────────────

    def _build_task_prompt(
        self,
        job_root: str,
        backend: str | None = None,
        mode: str | None = None,
        **extra: Any,
    ) -> str:
        """Build the task prompt for the story sub-agent.

        Delegates to the base class for common structure (skill injection,
        goal, milestone, error handling) and appends story-specific HITL
        and reserve activation rules.
        """
        base = super()._build_task_prompt(
            job_root=job_root,
            backend=backend,
            mode=mode,
            **extra,
        )

        story_rules: list[str] = [
            "",
            "- At story_approval: in interactive mode, if human input is required, pause and",
            "  report \"awaiting approval\" with the preflight report path.",
            "",
            "**Reserve Activation:**",
            "If any primary story is rejected at the approval stage, activate the next",
            "available reserve story from the portfolio. Re-run story_treatments, story_scripts,",
            "story_preflight, and story_approval for the reserve story.",
            "",
            "When all stories are approved, report: milestone=script_approved.",
        ]

        return base + "\n".join(story_rules)
