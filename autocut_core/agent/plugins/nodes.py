"""Graph node plugins for the StateGraph Engine.

Each plugin implements INodePlugin from autocut_core.agent.ports and
handles a specific node type. New node types can be added by creating
new plugin classes without modifying the engine — satisfying OCP.

DIP: All plugins depend on the INodePlugin interface; the engine depends
only on INodePlugin, never on concrete plugin implementations.

SRP: Each plugin class handles exactly one node type.
"""

from __future__ import annotations

import logging
from typing import Any

from autocut_core.agent.entities import NodeResult
from autocut_core.agent.ports import DomainContext, DomainStatus, INodePlugin, ISubAgent

logger = logging.getLogger(__name__)


class SubAgentPlugin(INodePlugin):
    """Wraps a DomainAgent (via ISubAgent) as a StateGraph node plugin.

    DIP: Depends on ISubAgent interface, not on concrete DomainAgent.
    """

    def __init__(self, agent: ISubAgent) -> None:
        self._agent = agent

    async def execute(self, state: dict[str, Any]) -> NodeResult:
        """Execute the wrapped domain agent against the graph state.

        Constructs a DomainContext from well-known keys in the state dict,
        calls agent.execute(ctx), and translates the result.
        """
        ctx = DomainContext(
            job_root=state.get("job_root", ""),
            config=state.get("config"),
            bus=state.get("bus"),
            backend=state.get("backend", "qwen"),
            mode=state.get("mode", "auto"),
        )

        try:
            result = await self._agent.execute(ctx)
        except Exception:
            logger.exception(
                "Sub-agent '%s' raised an unhandled exception",
                self._agent.contract.agent_name,
            )
            return NodeResult(
                status="failed",
                output=dict(state),
                error=(
                    f"Sub-agent '{self._agent.contract.agent_name}' "
                    "raised an unhandled exception"
                ),
            )

        output: dict[str, Any] = dict(state)
        output["_last_result"] = result
        output["_agent_name"] = self._agent.contract.agent_name

        if result.milestone_reached:
            output["milestone"] = result.milestone_reached
        if result.errors:
            accumulated: list[str] = list(output.get("errors", []))
            accumulated.extend(result.errors)
            output["errors"] = accumulated

        if result.status == DomainStatus.FAILED:
            return NodeResult(
                status="failed",
                output=output,
                error="; ".join(result.errors) if result.errors else "Sub-agent failed",
                duration_ms=result.duration_ms,
            )

        if result.status == DomainStatus.WAITING_HUMAN:
            return NodeResult(
                status="waiting_human",
                output=output,
                duration_ms=result.duration_ms,
            )

        return NodeResult(
            status="completed",
            output=output,
            duration_ms=result.duration_ms,
        )

    def can_handle(self, node_type: str) -> bool:
        return node_type == "sub_agent"


class HITLGatePlugin(INodePlugin):
    """Implements a Human-In-The-Loop gate node.

    Checks state for human_review flag before pausing.
    If the node config has human_review=True or the state has
    _requires_human_review=True, pauses for human input.
    Otherwise skips (auto-approve).
    """

    async def execute(self, state: dict[str, Any]) -> NodeResult:
        # Check if human review is actually required
        node_config = state.get("_node_config", {})
        requires_human = (
            node_config.get("human_review", False) or
            state.get("_requires_human_review", False)
        )
        if not requires_human:
            return NodeResult(status="completed", output=dict(state))

        return NodeResult(
            status="waiting_human",
            output=dict(state),
        )

    def can_handle(self, node_type: str) -> bool:
        return node_type == "hitl_gate"


class MilestoneNode(INodePlugin):
    """Implements a milestone gate node.

    Checks whether all required nodes in the milestone have been
    completed. Reads the '_completed_nodes' list from state to
    determine completion.

    SRP: This plugin only validates milestone completion. It does not
    execute any domain logic.
    """

    async def execute(self, state: dict[str, Any]) -> NodeResult:
        # The milestone definition is expected to be passed in via the
        # node config, which the engine stores in the state under
        # '_node_config' before invoking the plugin.
        node_config: dict[str, Any] = state.get("_node_config", {})
        required_nodes: list[str] = node_config.get("required_nodes", [])
        completed_nodes: list[str] = state.get("_completed_nodes", [])

        if not required_nodes:
            logger.warning(
                "Milestone node executed with no required_nodes configured"
            )
            return NodeResult(status="completed", output=dict(state))

        completed_set = set(completed_nodes)
        missing = [n for n in required_nodes if n not in completed_set]

        output = dict(state)
        output["_milestone_required"] = required_nodes
        output["_milestone_completed"] = list(completed_set & set(required_nodes))
        output["_milestone_missing"] = missing

        if missing:
            return NodeResult(
                status="failed",
                output=output,
                error=f"Milestone not reached — missing nodes: {', '.join(missing)}",
            )

        return NodeResult(status="completed", output=output)

    def can_handle(self, node_type: str) -> bool:
        return node_type == "milestone"


# ── Convenience: pre-built plugin registry for the standard three agents ──────────
#
# Usage:
#   engine = StateGraphEngine(
#       graph=my_graph,
#       node_plugins={
#           "sub_agent": SubAgentPlugin(source_agent),
#           "milestone": MilestoneNode(),
#           "hitl_gate": HITLGatePlugin(),
#       },
#       ...
#   )
#
# Or create multiple SubAgentPlugin instances for different sub-agents
# registered under distinct node_type values:
#   plugins = {
#       "source_agent": SubAgentPlugin(source_agent),
#       "story_agent": SubAgentPlugin(story_agent),
#       "production_agent": SubAgentPlugin(production_agent),
#       "milestone": MilestoneNode(),
#       "hitl_gate": HITLGatePlugin(),
#   }
