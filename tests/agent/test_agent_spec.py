
import pytest

from auto_cut_bot.agents.registry import AgentBuilder, AgentInstance, AgentRegistry
from auto_cut_bot.agents.spec import EDITOR_SPEC, REVIEWER_SPEC, AgentSpec


class TestAgentSpec:
    def test_editor_has_soul(self):
        assert len(EDITOR_SPEC.soul) > 50
        assert "我是" in EDITOR_SPEC.soul

    def test_reviewer_has_soul(self):
        assert len(REVIEWER_SPEC.soul) > 50
        assert "独立审核" in REVIEWER_SPEC.soul

    def test_editor_has_rules(self):
        assert len(EDITOR_SPEC.rules) > 50
        # The reviewer is declared as a sub-agent; the editor's rules use the
        # localized review term rather than the registry code name.
        assert "审核" in EDITOR_SPEC.rules

    def test_reviewer_has_rules(self):
        assert len(REVIEWER_SPEC.rules) > 50
        assert "db_query" in REVIEWER_SPEC.rules.lower()

    def test_editor_has_subagents(self):
        assert "reviewer" in EDITOR_SPEC.subagents

    def test_reviewer_has_no_subagents(self):
        assert REVIEWER_SPEC.subagents == ()

    def test_editor_has_20plus_tools(self):
        assert len(EDITOR_SPEC.tools) >= 20

    def test_reviewer_only_has_db_query(self):
        assert len(REVIEWER_SPEC.tools) == 1
        assert REVIEWER_SPEC.tools[0].tool_name == "db_query"
        assert REVIEWER_SPEC.tools[0].read_only is True

    def test_spec_is_immutable(self):
        with pytest.raises(Exception):
            EDITOR_SPEC.code = "changed"


class TestAgentRegistry:
    def test_register_and_get(self):
        registry = AgentRegistry()
        assert registry.get("editor") is not None
        assert registry.get("reviewer") is not None
        assert registry.get("unknown") is None

    def test_list_all(self):
        registry = AgentRegistry()
        agents = registry.list_all()
        assert "editor" in agents
        assert "reviewer" in agents

    def test_get_subagents(self):
        registry = AgentRegistry()
        subs = registry.get_subagents("editor")
        assert len(subs) == 1
        assert subs[0].code == "reviewer"

    def test_reviewer_has_no_subagents(self):
        registry = AgentRegistry()
        assert registry.get_subagents("reviewer") == []


class TestAgentBuilder:
    def test_build_editor_with_pipeline(self):
        instance = AgentBuilder.build("editor", has_pipeline_context=True)
        assert instance.spec.code == "editor"
        assert len(instance.tools) >= 20
        assert "Pipeline Context" in instance.instructions

    def test_build_reviewer(self):
        instance = AgentBuilder.build("reviewer", has_review_context=True)
        assert instance.spec.code == "reviewer"
        assert len(instance.tools) == 1
        assert instance.tools[0] == "db_query"
        assert "独立审核" in instance.instructions

    def test_build_unknown_raises(self):
        with pytest.raises(ValueError):
            AgentBuilder.build("unknown")

    def test_editor_delegation_tools(self):
        instance = AgentBuilder.build("editor", has_pipeline_context=True)
        assert any("start_subagent_task_reviewer" in t for t in instance.tools)
        assert any("read_subagent_task_reviewer" in t for t in instance.tools)

    def test_pipeline_tools_filtered_when_no_pipeline(self):
        instance = AgentBuilder.build("editor", has_pipeline_context=False)
        assert len(instance.tools) < 20
        assert "source_script_load" not in instance.tools

    def test_reviewer_still_has_db_query_without_review_context(self):
        """db_query is the only tool, and it's read_only. Without review context, it should still be available."""
        instance = AgentBuilder.build("reviewer", has_review_context=False)
        assert instance.tools == []  # read_only tools filtered without review context

    def test_model_override(self):
        spec = AgentSpec(code="test", name="test", soul="", rules="", tools=(), model_override="gpt-4")
        instance = AgentInstance(spec=spec, instructions="", tools=[], model=spec.model_override)
        assert instance.model == "gpt-4"

    def test_editor_and_reviewer_are_independent_instances(self):
        editor = AgentBuilder.build("editor", has_pipeline_context=True)
        reviewer = AgentBuilder.build("reviewer", has_review_context=True)
        assert editor.spec.code != reviewer.spec.code
        assert len(editor.tools) > len(reviewer.tools)
        assert "reviewer" in editor.spec.subagents
        assert reviewer.spec.subagents == ()
