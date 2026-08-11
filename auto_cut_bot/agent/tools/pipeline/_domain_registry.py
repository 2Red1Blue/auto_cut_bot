"""DomainRegistry — central registry mapping domain agents to their tools.

Individual tools register themselves into the registry keyed by parent agent
name. Domain agents query the registry to discover their child tools rather
than hardcoding imports. This enables plugin-based extensibility.

Also provides the canonical tool-to-agent migration mapping for Phase 2.
"""

from __future__ import annotations

from typing import Any

from auto_cut_bot.agent.tools.pipeline._domain_contract import (
    SOURCE_AGENT_CONTRACT,
    STORY_AGENT_CONTRACT,
    PRODUCTION_AGENT_CONTRACT,
    DomainAgentContract,
)


class DomainRegistry:
    """Registry mapping domain agents to their child tools.

    Tools call register() to associate themselves with a domain agent.
    Domain agents call get_tools() to discover their children.
    """

    _instance: "DomainRegistry | None" = None

    def __init__(self) -> None:
        self._tools: dict[str, list[str]] = {}
        self._contracts: dict[str, DomainAgentContract] = {}
        self._deprecated_tools: dict[str, str] = {}

    @classmethod
    def instance(cls) -> "DomainRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, tool_name: str, parent_agent: str) -> None:
        """Register a tool as belonging to a domain agent."""
        self._tools.setdefault(parent_agent, []).append(tool_name)

    def register_contract(self, contract: DomainAgentContract) -> None:
        """Register a domain agent contract."""
        self._contracts[contract.agent_name] = contract

    def mark_deprecated(self, tool_name: str, parent_agent: str) -> None:
        """Mark a standalone tool as deprecated in favor of its domain agent."""
        self._deprecated_tools[tool_name] = parent_agent

    def get_tools(self, agent_name: str) -> list[str]:
        """Get all tools registered for a domain agent."""
        return list(self._tools.get(agent_name, []))

    def get_contract(self, agent_name: str) -> DomainAgentContract | None:
        """Get the contract for a domain agent."""
        return self._contracts.get(agent_name)

    def get_all_contracts(self) -> dict[str, DomainAgentContract]:
        """Get all registered domain agent contracts."""
        return dict(self._contracts)

    def is_deprecated(self, tool_name: str) -> str | None:
        """Return the parent agent name if the tool is deprecated, else None."""
        return self._deprecated_tools.get(tool_name)

    @property
    def deprecated_tools(self) -> dict[str, str]:
        return dict(self._deprecated_tools)


# ── Singleton instance ─────────────────────────────────────────────────────────

_registry = DomainRegistry.instance()

# Register the three domain agent contracts
_registry.register_contract(SOURCE_AGENT_CONTRACT)
_registry.register_contract(STORY_AGENT_CONTRACT)
_registry.register_contract(PRODUCTION_AGENT_CONTRACT)


# ── Tool-to-agent migration mapping ────────────────────────────────────────────

TOOL_MIGRATION_MAP: dict[str, str] = {
    # SourceAgent (13 tools)
    "source_windows": "source_agent",
    "source_script_load": "source_agent",
    "source_script_save": "source_agent",
    "source_script_chunk_parse": "source_agent",
    "source_transcripts": "source_agent",
    "window_analysis": "source_agent",
    "event_cards": "source_agent",
    "episode_digests": "source_agent",
    "chapter_digests": "source_agent",
    "series_registry": "source_agent",
    "series_assignment": "source_agent",
    # source_metadata: planned, not yet implemented
    # reconciliation: planned, not yet implemented
    # StoryAgent (11 tools)
    "series_bible": "story_agent",
    "story_catalog": "story_agent",
    "story_portfolio": "story_agent",
    "story_treatments": "story_agent",
    "story_scripts": "story_agent",
    "story_preflight": "story_agent",
    "story_approval": "story_agent",
    "story_evidence": "story_agent",
    "span_candidates": "story_agent",
    "story_plans": "story_agent",
    "story_plans_materialize": "story_agent",
    # story_plans_preflight: planned, not yet implemented
    # story_plans_qc_admission: planned, not yet implemented
    # ProductionAgent (3 tools)
    "story_qc": "production_agent",
    "story_qc_review": "production_agent",
    "story_render": "production_agent",
    # Infrastructure (shared)
    "database_write": "pipeline_orchestrator",
    "db_query": "pipeline_orchestrator",
    "orchestrator": "pipeline_orchestrator",
}


def get_agent_for_tool(tool_name: str) -> str | None:
    """Return the domain agent that owns a given tool."""
    return TOOL_MIGRATION_MAP.get(tool_name)


def get_tools_for_agent(agent_name: str) -> list[str]:
    """Return all tools owned by a given domain agent."""
    return [t for t, a in TOOL_MIGRATION_MAP.items() if a == agent_name]
