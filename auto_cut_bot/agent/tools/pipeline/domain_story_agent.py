"""StoryAgent — Domain Sub-Agent for story generation.

Wraps 11 story generation pipeline tools into a single coarse-grained tool.
The main agent delegates "generate story content" to this sub-agent,
which internally orchestrates the pipeline stages:

  Series Bible:
    1. episode_digests    — generate episode-level summaries
    2. chapter_digests    — generate chapter-level summaries
    3. series_registry    — register series metadata
    4. series_assignment  — assign episodes to series
    5. series_bible       — build the Series Bible

  Story Discovery & Planning:
    6. story_catalog      — discover and catalog story candidates
    7. story_portfolio    — compile story portfolio
    8. story_treatments   — write story treatments

  Script Generation & Approval:
    9. story_scripts      — generate full scripts
   10. story_preflight    — preflight feasibility check
   11. story_approval     — HUMAN REVIEW NODE (HITL gate)

Output milestone: script_approved.
HITL: pauses at story_approval for human review.
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
from auto_cut_bot.pipeline import context_packer, grounded_gen, query_tools
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
        f"Complete story generation for job_root={job_root}.",
        "",
        "You have access to the following pipeline tools. Work through the steps in order:",
        "",
        "**Phase 1 — Build Series Bible:**",
        "1. **episode_digests** — Generate episode-level summaries from the source material.",
        "   Call with job_root.",
        "2. **chapter_digests** — Generate chapter-level summaries from the source material.",
        "   Call with job_root.",
        "3. **series_registry** — Register series metadata (title, genre, synopsis, etc.).",
        "   Call with job_root.",
        "4. **series_assignment** — Assign episodes to series.",
        "   Call with job_root.",
        "5. **series_bible** — Build the complete Series Bible.",
        "   Call with job_root and backend.",
        "",
        "**Phase 2 — Discover and Plan Stories:**",
        "6. **story_catalog** — Discover and catalog all story candidates.",
        "   Call with job_root and backend.",
        "7. **story_portfolio** — Compile the story portfolio (primary + reserve stories).",
        "   Call with job_root.",
        "8. **story_treatments** — Write detailed story treatments.",
        "   Call with job_root and backend.",
        "",
        "**Phase 3 — Generate Scripts, Preflight, and Approval:**",
        "9. **story_scripts** — Generate full scripts from treatments.",
        "   Call with job_root and backend.",
        "10. **story_preflight** — Run preflight feasibility checks on all scripts.",
        "   Call with job_root. This produces story-preflight.json.",
        "11. **story_approval** — HUMAN REVIEW NODE.",
        "   This is a human-in-the-loop gate. Call story_approval with job_root and mode.",
        "   In interactive mode: if no decisions are provided, the tool returns a prompt",
        "   asking for human review. You MUST report \"awaiting approval\" and pause.",
        "   In auto mode: decisions are generated automatically.",
        "",
        "**Reserve Activation:**",
        "If any primary story is rejected at the approval stage, activate the next",
        "available reserve story from the portfolio. Re-run story_treatments, story_scripts,",
        "story_preflight, and story_approval for the reserve story.",
        "",
        "IMPORTANT RULES:",
        "- Run stages in order. Each stage depends on the previous one completing.",
        "- If any stage fails, report the error and do not continue to subsequent stages.",
        "- At story_approval: in interactive mode, if human input is required, pause and",
        "  report \"awaiting approval\" with the preflight report path.",
        "- If a primary story is rejected, activate reserve from the portfolio before",
        "  reporting final status.",
        "- When all stories are approved, report: milestone=script_approved.",
        "",
        "SKILL CONTEXT:",
        "- Read the Skill file at skills/ac_story_generation/SKILL.md for detailed tool",
        "  descriptions, data layer query tools, and writing discipline rules.",
        "- The Skill declares which tools are available and how to use them.",
        "- All data layer tools (query_tools, context_packer, grounded_gen) are documented",
        "  in the Skill — use them as described there.",
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
            "description": "Execution mode: 'auto' runs without pauses, 'interactive' pauses at story_approval for human review.",
        },
    },
    "required": ["job_root", "backend"],
})
class DomainStoryAgentTool(Tool):
    """Domain sub-agent that orchestrates the 11 story generation pipeline stages.

    Instead of the main agent calling each story-generation tool individually
    (requiring 11+ round-trips), this tool delegates the entire story generation
    workflow to a sub-agent that can call the individual tools autonomously.

    The sub-agent is spawned via SubagentManager.run_inline() and given
    a structured task description with numbered steps.

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

    @property
    def name(self) -> str:
        return "story_agent"

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

        Validates inputs, builds a task prompt, and either spawns a sub-agent
        via SubagentManager.run_inline() or returns an error if no sub-agent
        manager is available.
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

    async def _run_via_subagent(
        self,
        manager: "SubagentManager",
        request_ctx: "RequestContext",
        task_prompt: str,
    ) -> str:
        """Run the story generation task via SubagentManager.run_inline()."""
        origin_channel = request_ctx.channel
        origin_chat_id = request_ctx.chat_id
        session_key = request_ctx.session_key or f"{origin_channel}:{origin_chat_id}"

        try:
            result = await manager.run_inline(
                task=task_prompt,
                label="story-agent",
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
                f"story_agent sub-agent failed: {exc}\n\n"
                "The sub-agent encountered an error. Check the job log for details."
            )