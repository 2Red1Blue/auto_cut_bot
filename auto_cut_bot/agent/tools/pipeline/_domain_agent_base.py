"""DomainAgent ABC — abstract base class for domain sub-agents.

Defines the uniform interface that all domain agents must implement:
  invoke(state) -> state

Where state is the shared StateGraph state object. Sub-agents read from
and write to well-defined keys in the state, never communicating directly
with each other.

In Phase 2 (pre-StateGraph), domain agents also support a direct execution
path via execute(ctx) -> DomainResult, used by the PipelineOrchestrator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from auto_cut_bot.agent.tools.pipeline._domain_contract import DomainAgentContract
from auto_cut_bot.agent.tools.pipeline._domain_result import DomainContext, DomainResult

if TYPE_CHECKING:
    from auto_cut_bot.agent.subagent import SubagentManager
    from auto_cut_bot.agent.tools.context import RequestContext


class DomainAgent(ABC):
    """Abstract base class for domain sub-agents.

    Each domain agent owns a coherent slice of the production pipeline
    (Source, Story, or Production) and exposes a uniform interface.

    Sub-agents do not share mutable state. The StateGraph engine (Phase 3)
    is the sole owner of the state object. Sub-agents receive a snapshot
    and return updates.
    """

    @property
    @abstractmethod
    def contract(self) -> DomainAgentContract:
        """The immutable contract declaring this agent's responsibilities."""
        ...

    @abstractmethod
    async def execute(self, ctx: DomainContext) -> DomainResult:
        """Execute this domain agent's pipeline stages.

        In Phase 2, this is called directly by the PipelineOrchestrator.
        In Phase 3, this is wrapped by the StateGraph engine's node executor.

        Args:
            ctx: DomainContext with job_root, config, bus, backend, mode.

        Returns:
            DomainResult with status, artifacts, errors, and milestone.
        """
        ...

    async def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        """StateGraph-compatible invocation (Phase 3).

        Validates prerequisites, delegates to execute(), and merges results.
        """
        from auto_cut_bot.agent.tools.pipeline._domain_validator import DomainValidator

        ctx = DomainContext(
            job_root=state.get("job_root", ""),
            config=state.get("config"),
            backend=state.get("backend", "qwen"),
            mode=state.get("mode", "auto"),
        )

        # Validate prerequisites (non-blocking: warn but don't stop)
        if ctx.bus is not None:
            missing = DomainValidator.validate_prerequisites(self.contract, ctx.bus)
            if missing:
                import logging
                logging.getLogger(__name__).warning(
                    "Agent %s: missing prerequisites: %s",
                    self.contract.agent_name, missing,
                )

        result = await self.execute(ctx)

        # Merge result into state
        state["_last_result"] = result
        if result.milestone_reached:
            state["milestone"] = result.milestone_reached
        if result.errors:
            state.setdefault("errors", []).extend(result.errors)

        return state

    def _build_task_prompt(
        self,
        job_root: str,
        backend: str | None = None,
        mode: str | None = None,
        **extra: Any,
    ) -> str:
        """Build the task prompt for the sub-agent.

        Uses the contract's skill_names to inject skill context.
        Subclasses may override for domain-specific prompt construction.
        """
        from auto_cut_bot.agent.tools.pipeline._skill_context import inject_skill_context

        parts: list[str] = [
            inject_skill_context(list(self.contract.skill_names)),
            "",
            f"Goal: Complete {self.contract.description.lower()}",
            f"Job root: {job_root}",
            "",
            "IMPORTANT RULES:",
            "- Run stages in the order described in the Skill above.",
            "- If any stage fails, report the error and do not continue.",
            f"- When all stages complete, report: milestone={self.contract.milestone}.",
        ]
        if backend:
            parts.append(f"Backend: {backend}")
        if mode:
            parts.append(f"Mode: {mode}")
        for key, value in extra.items():
            if value:
                parts.append(f"{key}: {value}")

        return "\n".join(parts)

    async def _run_via_subagent(
        self,
        manager: "SubagentManager",
        request_ctx: "RequestContext",
        task_prompt: str,
    ) -> str:
        """Run the task via SubagentManager.run_inline().

        Uses AgentBuilder to assemble the agent's identity (SOUL.md + AGENTS.md)
        and inject it as the system prompt. Each agent code gets its own
        session key for context isolation.
        """
        from auto_cut_bot.agent.tools.base import ToolResult
        from auto_cut_bot.security.workspace_access import current_workspace_scope
        from auto_cut_bot.agents.registry import AgentBuilder

        agent_name = self.contract.agent_name
        agent_code = agent_name.replace("_agent", "")  # "source_agent" → "source"

        # 1. Build agent instance with identity + rules
        try:
            instance = AgentBuilder.build(
                agent_code if agent_code in ("editor", "reviewer") else "editor",
                has_pipeline_context=True,
            )
            system_prompt = instance.instructions
        except ValueError:
            # Fallback: agent_code not in registry, use task_prompt as-is
            system_prompt = ""

        # 2. Build full task with identity
        if system_prompt:
            full_task = f"{system_prompt}\n\n---\n\n{task_prompt}"
        else:
            full_task = task_prompt

        # 3. Independent session key per agent (context isolation)
        label = agent_name.replace("_", "-")
        origin_channel = request_ctx.channel
        origin_chat_id = request_ctx.chat_id
        base_session = request_ctx.session_key or f"{origin_channel}:{origin_chat_id}"
        session_key = f"{base_session}:{agent_name}"

        try:
            result = await manager.run_inline(
                task=full_task,
                label=label,
                runtime=request_ctx.runtime,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                session_key=session_key,  # ← 独立 session
                origin_message_id=request_ctx.message_id,
                workspace_scope=current_workspace_scope(),
            )
            return result
        except Exception as exc:
            return ToolResult.error(
                f"{agent_name} sub-agent failed: {exc}\n\n"
                "The sub-agent encountered an error. Check the job log for details."
            )
