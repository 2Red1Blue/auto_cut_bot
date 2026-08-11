"""End-to-end integration test: Phase 1 tools -> Phase 2 agents -> Phase 3 engine."""

import pytest
from unittest.mock import MagicMock, AsyncMock, PropertyMock

from autocut_core.agent.entities import (
    Edge, HumanDecision, Node, NodeType,
    Session, SessionStatus, StateGraph,
)
from autocut_core.agent.engine import StateGraphEngine
from autocut_core.agent.adapters.in_memory import (
    InMemoryCheckpointRepository,
    InMemorySessionStore,
    InMemoryEventEmitter,
)
from autocut_core.agent.plugins.nodes import (
    SubAgentPlugin,
    HITLGatePlugin,
)
from auto_cut_bot.agent.tools.pipeline._domain_result import (
    DomainResult, DomainStatus, Artifact,
)


def _make_mock_agent(name, milestone, status=DomainStatus.SUCCESS):
    agent = MagicMock()
    type(agent).contract = PropertyMock(return_value=MagicMock(
        agent_name=name, milestone=milestone,
    ))
    agent.execute = AsyncMock(return_value=DomainResult(
        agent_name=name,
        status=status,
        artifacts=[Artifact(name=f"{name}_output", path="/tmp/out.json")],
        milestone_reached=milestone,
    ))
    return agent


class TestE2EFullPipeline:

    async def test_full_pipeline_flow(self):
        agent = _make_mock_agent("source_agent", "source_ready")
        plugins = {"sub_agent": SubAgentPlugin(agent)}
        checkpoints = InMemoryCheckpointRepository()
        sessions = InMemorySessionStore()
        events = InMemoryEventEmitter()
        graph = StateGraph(
            nodes={"source_agent": Node(id="source_agent", type=NodeType.SUB_AGENT)},
            edges=[], entry_point="source_agent",
        )
        engine = StateGraphEngine(graph, plugins, checkpoints, sessions, events)
        session = Session(project_id="test-project")
        session = await engine.start(session)
        assert session.status == SessionStatus.COMPLETED
        assert agent.execute.call_count == 1
        saved = await checkpoints.list_by_session(session.id)
        assert len(saved) == 1

    async def test_hitl_flow(self):
        agent = _make_mock_agent("source_agent", "source_ready")
        plugins = {"sub_agent": SubAgentPlugin(agent), "hitl_gate": HITLGatePlugin()}
        checkpoints = InMemoryCheckpointRepository()
        sessions = InMemorySessionStore()
        events = InMemoryEventEmitter()
        graph = StateGraph(
            nodes={
                "source_agent": Node(id="source_agent", type=NodeType.SUB_AGENT),
                "approval_gate": Node(id="approval_gate", type=NodeType.HITL_GATE),
            },
            edges=[Edge(source="source_agent", target="approval_gate")],
            entry_point="source_agent",
        )
        engine = StateGraphEngine(graph, plugins, checkpoints, sessions, events)
        session = Session(project_id="test-hitl", state_snapshot={"_requires_human_review": True})
        session = await engine.start(session)
        assert session.status == SessionStatus.WAITING_FOR_HUMAN
        decision = HumanDecision(approved=True, reason="Looks good")
        session = await engine.resume(session.id, decision)
        assert session.status == SessionStatus.COMPLETED

    async def test_engine_checkpoint_and_events(self):
        agent = _make_mock_agent("source_agent", "source_ready")
        plugins = {"sub_agent": SubAgentPlugin(agent)}
        checkpoints = InMemoryCheckpointRepository()
        sessions = InMemorySessionStore()
        events = InMemoryEventEmitter()
        graph = StateGraph(
            nodes={"source_agent": Node(id="source_agent", type=NodeType.SUB_AGENT)},
            edges=[], entry_point="source_agent",
        )
        engine = StateGraphEngine(graph, plugins, checkpoints, sessions, events)
        session = Session(project_id="test-checkpoint")
        session = await engine.start(session)
        assert session.status == SessionStatus.COMPLETED
        saved = await checkpoints.list_by_session(session.id)
        assert len(saved) == 1
        assert saved[0].node_name == "source_agent"
        session_events = events.get_events(session.id)
        event_types = [e["event_type"] for e in session_events]
        assert "session_started" in event_types
        assert "node_entered" in event_types
        assert "node_completed" in event_types
        assert "session_completed" in event_types
