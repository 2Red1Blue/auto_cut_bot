"""Contract tests for domain agent architecture — Phase 2."""

import pytest
from auto_cut_bot.agent.tools.pipeline._domain_contract import (
    SOURCE_AGENT_CONTRACT,
    STORY_AGENT_CONTRACT,
    PRODUCTION_AGENT_CONTRACT,
    DomainAgentContract,
)
from auto_cut_bot.agent.tools.pipeline._domain_registry import (
    TOOL_MIGRATION_MAP,
    get_agent_for_tool,
    get_tools_for_agent,
)
from auto_cut_bot.cli.agent import agent


class TestDomainAgentContracts:
    def test_all_contracts_have_agent_name(self):
        for c in [SOURCE_AGENT_CONTRACT, STORY_AGENT_CONTRACT, PRODUCTION_AGENT_CONTRACT]:
            assert c.agent_name

    def test_all_contracts_have_stage_names(self):
        for c in [SOURCE_AGENT_CONTRACT, STORY_AGENT_CONTRACT, PRODUCTION_AGENT_CONTRACT]:
            assert len(c.stage_names) > 0

    def test_all_contracts_have_milestone(self):
        for c in [SOURCE_AGENT_CONTRACT, STORY_AGENT_CONTRACT, PRODUCTION_AGENT_CONTRACT]:
            assert c.milestone

    def test_contracts_are_immutable(self):
        with pytest.raises(Exception):
            SOURCE_AGENT_CONTRACT.agent_name = "changed"

    def test_milestones_are_correct_order(self):
        milestones = [
            SOURCE_AGENT_CONTRACT.milestone,
            STORY_AGENT_CONTRACT.milestone,
            PRODUCTION_AGENT_CONTRACT.milestone,
        ]
        assert milestones == ["source_ready", "script_approved", "rendered"]


class TestToolMigrationMap:
    def test_no_overlapping_tools(self):
        source = set(get_tools_for_agent("source_agent"))
        story = set(get_tools_for_agent("story_agent"))
        prod = set(get_tools_for_agent("production_agent"))
        assert source.isdisjoint(story)
        assert source.isdisjoint(prod)
        assert story.isdisjoint(prod)

    def test_all_tools_have_valid_agent(self):
        valid = {"source_agent", "story_agent", "production_agent", "pipeline_orchestrator"}
        for tool, agent in TOOL_MIGRATION_MAP.items():
            assert agent in valid, f"Tool '{tool}' has unknown agent '{agent}'"

    def test_source_agent_tool_count(self):
        assert len(get_tools_for_agent("source_agent")) == 11

    def test_story_agent_tool_count(self):
        assert len(get_tools_for_agent("story_agent")) == 11

    def test_production_agent_tool_count(self):
        assert len(get_tools_for_agent("production_agent")) == 3

    def test_get_agent_for_tool(self):
        assert get_agent_for_tool("source_script_load") == "source_agent"
        assert get_agent_for_tool("series_bible") == "story_agent"
        assert get_agent_for_tool("story_render") == "production_agent"

    def test_orchestrator_tools_not_in_domain_agents(self):
        orch = set(get_tools_for_agent("pipeline_orchestrator"))
        all_domain = (set(get_tools_for_agent("source_agent")) |
                      set(get_tools_for_agent("story_agent")) |
                      set(get_tools_for_agent("production_agent")))
        assert orch.isdisjoint(all_domain)
