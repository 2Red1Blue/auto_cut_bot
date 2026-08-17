"""Tests for Phase 3 StateGraph Engine.

Clean-pytest methodology: Fake-based testing (InMemory adapters), AAA pattern.
Tests cover StateGraphEngine, InMemoryAdapters, and FeatureFlagGateway.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

# Monkey-patch autocut_core.get_logger before importing any module that
# depends on it (engine.py, api.py, in_memory.py all do).
import autocut_core

autocut_core.get_logger = lambda name: logging.getLogger(name)

from auto_cut_bot.agent.state_graph.adapters.in_memory import (  # noqa: E402
    FeatureFlagGateway,
    InMemoryCheckpointRepository,
    InMemoryEventEmitter,
    InMemorySessionStore,
)
from auto_cut_bot.agent.state_graph.engine import StateGraphEngine  # noqa: E402
from auto_cut_bot.agent.state_graph.entities import (  # noqa: E402
    Checkpoint,
    Edge,
    HumanDecision,
    Node,
    NodeResult,
    NodeType,
    Session,
    SessionStatus,
    StateGraph,
)
from auto_cut_bot.agent.state_graph.ports import INodePlugin  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_success_plugin(output: dict | None = None) -> MagicMock:
    """Create a mock INodePlugin that returns a completed NodeResult."""
    plugin = MagicMock(spec=INodePlugin)
    plugin.execute.return_value = NodeResult(status="completed", output=output or {})
    return plugin


def _make_waiting_plugin() -> MagicMock:
    """Create a mock INodePlugin that returns a waiting_human NodeResult."""
    plugin = MagicMock(spec=INodePlugin)
    plugin.execute.return_value = NodeResult(status="waiting_human", output={})
    return plugin


def _make_failing_plugin(error: str = "simulated node failure") -> MagicMock:
    """Create a mock INodePlugin that raises an exception during execute."""
    plugin = MagicMock(spec=INodePlugin)
    plugin.execute.side_effect = RuntimeError(error)
    return plugin


def _make_two_node_graph(
    source_type: NodeType = NodeType.SUB_AGENT,
    story_type: NodeType = NodeType.SUB_AGENT,
) -> StateGraph:
    """Create a simple 2-node graph: source_agent -> story_agent."""
    return StateGraph(
        nodes={
            "source_agent": Node(id="source_agent", type=source_type),
            "story_agent": Node(id="story_agent", type=story_type),
        },
        edges=[Edge(source="source_agent", target="story_agent")],
        entry_point="source_agent",
    )


def _make_single_node_graph(node_type: NodeType = NodeType.SUB_AGENT) -> StateGraph:
    """Create a graph with only one node and no outgoing edges."""
    return StateGraph(
        nodes={"only_node": Node(id="only_node", type=node_type)},
        edges=[],
        entry_point="only_node",
    )


def _make_session(**overrides) -> Session:
    """Create a Session with sensible defaults, overridable via kwargs."""
    defaults = {
        "id": uuid4(),
        "project_id": "test-project",
        "status": SessionStatus.RUNNING,
    }
    defaults.update(overrides)
    return Session(**defaults)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def graph() -> StateGraph:
    return _make_two_node_graph()


@pytest.fixture
def checkpoint_repo() -> InMemoryCheckpointRepository:
    return InMemoryCheckpointRepository()


@pytest.fixture
def session_store() -> InMemorySessionStore:
    return InMemorySessionStore()


@pytest.fixture
def event_emitter() -> InMemoryEventEmitter:
    return InMemoryEventEmitter()


@pytest.fixture
def engine(
    graph: StateGraph,
    checkpoint_repo: InMemoryCheckpointRepository,
    session_store: InMemorySessionStore,
    event_emitter: InMemoryEventEmitter,
) -> StateGraphEngine:
    """Build a StateGraphEngine with two success plugins wired to SUB_AGENT nodes."""
    plugins: dict[str, INodePlugin] = {
        NodeType.SUB_AGENT.value: _make_success_plugin(),
    }
    return StateGraphEngine(
        graph=graph,
        node_plugins=plugins,
        checkpoint_repo=checkpoint_repo,
        session_store=session_store,
        event_emitter=event_emitter,
    )


@pytest.fixture
def session() -> Session:
    return _make_session()


# ── TestStateGraphEngine ─────────────────────────────────────────────────────


class TestStateGraphEngine:
    """Tests for the StateGraphEngine use-case class."""

    async def test_start_session_executes_entry_node(
        self,
        engine: StateGraphEngine,
        session: Session,
    ):
        """Starting a session should execute the entry node and continue through the graph."""
        result = await engine.start(session)

        assert result.status == SessionStatus.COMPLETED
        assert result.current_node == "story_agent"

    async def test_resume_session_after_hitl(
        self,
        checkpoint_repo: InMemoryCheckpointRepository,
        session_store: InMemorySessionStore,
        event_emitter: InMemoryEventEmitter,
    ):
        """Resuming a HITL-paused session should apply the decision and continue."""
        graph = _make_two_node_graph(
            source_type=NodeType.HITL_GATE,
            story_type=NodeType.SUB_AGENT,
        )
        plugins: dict[str, INodePlugin] = {
            NodeType.HITL_GATE.value: _make_waiting_plugin(),
            NodeType.SUB_AGENT.value: _make_success_plugin(),
        }
        engine = StateGraphEngine(
            graph=graph,
            node_plugins=plugins,
            checkpoint_repo=checkpoint_repo,
            session_store=session_store,
            event_emitter=event_emitter,
        )
        session = _make_session()

        # Start — should pause at HITL_GATE
        session = await engine.start(session)
        assert session.status == SessionStatus.WAITING_FOR_HUMAN
        assert session.current_node == "source_agent"

        # Resume with approval
        decision = HumanDecision(approved=True, reason="looks good")
        session = await engine.resume(session.id, decision)

        assert session.status == SessionStatus.COMPLETED
        assert session.current_node == "story_agent"

        # Checkpoint should have the human decision stored
        latest = await checkpoint_repo.get_latest(session.id)
        assert latest is not None
        assert latest.human_decision is not None
        assert latest.human_decision["approved"] is True
        assert latest.human_decision["reason"] == "looks good"

    async def test_resume_non_waiting_session_raises(
        self,
        engine: StateGraphEngine,
        session: Session,
    ):
        """Resuming a session that is not WAITING_FOR_HUMAN should raise ValueError."""
        session = await engine.start(session)

        with pytest.raises(ValueError, match="not waiting_for_human"):
            await engine.resume(session.id, HumanDecision(approved=True))

    async def test_resume_nonexistent_session_raises(
        self,
        engine: StateGraphEngine,
    ):
        """Resuming a session that does not exist should raise ValueError."""
        fake_id = uuid4()

        with pytest.raises(ValueError, match="Session not found"):
            await engine.resume(fake_id, HumanDecision(approved=True))

    async def test_node_failure_sets_session_failed(
        self,
        checkpoint_repo: InMemoryCheckpointRepository,
        session_store: InMemorySessionStore,
        event_emitter: InMemoryEventEmitter,
    ):
        """When a node raises an exception, the session should be marked FAILED."""
        graph = _make_single_node_graph(NodeType.SUB_AGENT)
        plugins: dict[str, INodePlugin] = {
            NodeType.SUB_AGENT.value: _make_failing_plugin("boom"),
        }
        engine = StateGraphEngine(
            graph=graph,
            node_plugins=plugins,
            checkpoint_repo=checkpoint_repo,
            session_store=session_store,
            event_emitter=event_emitter,
        )
        session = _make_session()

        session = await engine.start(session)

        assert session.status == SessionStatus.FAILED
        assert session.error_message == "boom"

        # Checkpoint should reflect failure
        latest = await checkpoint_repo.get_latest(session.id)
        assert latest is not None
        assert latest.status == "failed"
        assert latest.error_message == "boom"

        # Failure event should have been emitted
        failed_events = [
            e
            for e in event_emitter.get_events(session.id)
            if e["event_type"] == "node_failed"
        ]
        assert len(failed_events) == 1
        assert failed_events[0]["payload"]["error"] == "boom"

    async def test_completed_session_after_last_node(
        self,
        checkpoint_repo: InMemoryCheckpointRepository,
        session_store: InMemorySessionStore,
        event_emitter: InMemoryEventEmitter,
    ):
        """When the graph reaches a node with no successors, the session should be COMPLETED."""
        graph = _make_single_node_graph(NodeType.SUB_AGENT)
        plugins: dict[str, INodePlugin] = {
            NodeType.SUB_AGENT.value: _make_success_plugin({"result": "done"}),
        }
        engine = StateGraphEngine(
            graph=graph,
            node_plugins=plugins,
            checkpoint_repo=checkpoint_repo,
            session_store=session_store,
            event_emitter=event_emitter,
        )
        session = _make_session()

        session = await engine.start(session)

        assert session.status == SessionStatus.COMPLETED
        assert session.current_node == "only_node"
        assert session.state_snapshot.get("result") == "done"

        # session_completed event should have been emitted
        completed_events = [
            e
            for e in event_emitter.get_events(session.id)
            if e["event_type"] == "session_completed"
        ]
        assert len(completed_events) == 1

    async def test_checkpoint_saved_before_execution(
        self,
        checkpoint_repo: InMemoryCheckpointRepository,
        session_store: InMemorySessionStore,
        event_emitter: InMemoryEventEmitter,
    ):
        """Each node execution should persist a checkpoint before the plugin runs."""
        graph = _make_two_node_graph()
        plugins: dict[str, INodePlugin] = {
            NodeType.SUB_AGENT.value: _make_success_plugin({"step": "ok"}),
        }
        engine = StateGraphEngine(
            graph=graph,
            node_plugins=plugins,
            checkpoint_repo=checkpoint_repo,
            session_store=session_store,
            event_emitter=event_emitter,
        )
        session = _make_session()

        session = await engine.start(session)

        # Both nodes should have checkpoints
        checkpoints = await checkpoint_repo.list_by_session(session.id)
        assert len(checkpoints) == 2

        node_names = {cp.node_name for cp in checkpoints}
        assert node_names == {"source_agent", "story_agent"}

        # Each checkpoint should have completed status and a snapshot
        for cp in checkpoints:
            assert cp.status == "completed"
            assert cp.error_message is None
            assert cp.duration_ms >= 0

    async def test_events_emitted_on_node_transition(
        self,
        checkpoint_repo: InMemoryCheckpointRepository,
        session_store: InMemorySessionStore,
        event_emitter: InMemoryEventEmitter,
    ):
        """Node transitions should emit session_started, node_entered, node_completed,
        and session_completed events."""
        graph = _make_two_node_graph()
        plugins: dict[str, INodePlugin] = {
            NodeType.SUB_AGENT.value: _make_success_plugin(),
        }
        engine = StateGraphEngine(
            graph=graph,
            node_plugins=plugins,
            checkpoint_repo=checkpoint_repo,
            session_store=session_store,
            event_emitter=event_emitter,
        )
        session = _make_session()

        session = await engine.start(session)

        events = event_emitter.get_events(session.id)

        # Verify event types in order
        event_types = [e["event_type"] for e in events]
        assert "session_started" in event_types
        assert event_types.count("node_entered") == 2
        assert event_types.count("node_completed") == 2
        assert "session_completed" in event_types

        # Verify node names in node_entered events
        entered_nodes = [
            e["node_name"] for e in events if e["event_type"] == "node_entered"
        ]
        assert entered_nodes == ["source_agent", "story_agent"]

        # session_started should have entry_point payload
        started_event = events[0]
        assert started_event["event_type"] == "session_started"
        assert started_event["payload"]["entry_point"] == "source_agent"


# ── TestInMemoryAdapters ─────────────────────────────────────────────────────


class TestInMemoryAdapters:
    """Tests for InMemory port implementations."""

    async def test_checkpoint_repo_save_and_get(
        self, checkpoint_repo: InMemoryCheckpointRepository
    ):
        """Saving a checkpoint and retrieving it by ID should return the same object."""
        cp = Checkpoint(
            session_id=uuid4(),
            node_name="source_agent",
            node_type=NodeType.SUB_AGENT,
            state_snapshot={"key": "value"},
            status="completed",
            created_at="2024-01-01T00:00:00Z",
        )

        saved = await checkpoint_repo.save(cp)
        assert saved.id == cp.id

        retrieved = await checkpoint_repo.get(cp.id)
        assert retrieved is not None
        assert retrieved.id == cp.id
        assert retrieved.node_name == "source_agent"
        assert retrieved.status == "completed"
        assert retrieved.state_snapshot == {"key": "value"}

    async def test_checkpoint_repo_get_latest(
        self, checkpoint_repo: InMemoryCheckpointRepository
    ):
        """get_latest should return the most recently created checkpoint for a session."""
        session_id = uuid4()
        cp1 = Checkpoint(
            session_id=session_id,
            node_name="source_agent",
            created_at="2024-01-01T00:00:00Z",
        )
        cp2 = Checkpoint(
            session_id=session_id,
            node_name="story_agent",
            created_at="2024-01-01T00:01:00Z",
        )

        await checkpoint_repo.save(cp1)
        await checkpoint_repo.save(cp2)

        latest = await checkpoint_repo.get_latest(session_id)
        assert latest is not None
        assert latest.node_name == "story_agent"

    async def test_session_store_save_and_get(
        self, session_store: InMemorySessionStore
    ):
        """Saving a session and retrieving it by ID should return the same object."""
        session = _make_session(
            project_id="p1",
            status=SessionStatus.RUNNING,
            current_node="source_agent",
        )

        saved = await session_store.save(session)
        assert saved.id == session.id

        retrieved = await session_store.get(session.id)
        assert retrieved is not None
        assert retrieved.id == session.id
        assert retrieved.project_id == "p1"
        assert retrieved.status == SessionStatus.RUNNING
        assert retrieved.current_node == "source_agent"

    async def test_session_store_list_by_status(
        self, session_store: InMemorySessionStore
    ):
        """list_by_status should filter sessions by their status."""
        running = _make_session(
            id=uuid4(), status=SessionStatus.RUNNING, created_at="2024-01-01T00:01:00Z"
        )
        completed = _make_session(
            id=uuid4(),
            status=SessionStatus.COMPLETED,
            created_at="2024-01-01T00:00:00Z",
        )
        failed = _make_session(
            id=uuid4(), status=SessionStatus.FAILED, created_at="2024-01-01T00:02:00Z"
        )

        for s in (running, completed, failed):
            await session_store.save(s)

        running_sessions = await session_store.list_by_status(SessionStatus.RUNNING)
        assert len(running_sessions) == 1
        assert running_sessions[0].id == running.id

        completed_sessions = await session_store.list_by_status(SessionStatus.COMPLETED)
        assert len(completed_sessions) == 1
        assert completed_sessions[0].id == completed.id

        # No filter returns all sessions, sorted newest first
        all_sessions = await session_store.list_by_status()
        assert len(all_sessions) == 3
        assert all_sessions[0].id == failed.id  # newest first
        assert all_sessions[1].id == running.id
        assert all_sessions[2].id == completed.id

    async def test_event_emitter_records_events(self):
        """Emitted events should be retrievable by session ID."""
        emitter = InMemoryEventEmitter()
        session_id = uuid4()

        await emitter.emit(session_id, "node_entered", node_name="source_agent")
        await emitter.emit(
            session_id, "node_completed", node_name="source_agent", payload={"ok": True}
        )
        await emitter.emit(uuid4(), "node_entered", node_name="other")  # different session

        events = emitter.get_events(session_id)
        assert len(events) == 2

        assert events[0]["event_type"] == "node_entered"
        assert events[0]["node_name"] == "source_agent"

        assert events[1]["event_type"] == "node_completed"
        assert events[1]["node_name"] == "source_agent"
        assert events[1]["payload"] == {"ok": True}

    async def test_event_emitter_clear(self):
        """Clearing events should remove all recorded events."""
        emitter = InMemoryEventEmitter()
        session_id = uuid4()

        await emitter.emit(session_id, "node_entered", node_name="source_agent")
        assert len(emitter.get_events(session_id)) == 1

        emitter.clear()
        assert len(emitter.get_events(session_id)) == 0


# ── TestFeatureFlagGateway ───────────────────────────────────────────────────


class TestFeatureFlagGateway:
    """Tests for the FeatureFlagGateway boolean gate."""

    def test_gateway_enabled_routes_to_engine(self):
        """When use_agent_native_v2 is True, is_enabled should return True."""
        gateway = FeatureFlagGateway({"use_agent_native_v2": True})
        assert gateway.is_enabled() is True

    def test_gateway_disabled_routes_to_legacy(self):
        """When use_agent_native_v2 is False, is_enabled should return False."""
        gateway = FeatureFlagGateway({"use_agent_native_v2": False})
        assert gateway.is_enabled() is False

    def test_gateway_missing_flag_defaults_to_disabled(self):
        """When the flag key is absent, is_enabled should default to False."""
        gateway = FeatureFlagGateway({})
        assert gateway.is_enabled() is False

    def test_gateway_falsy_values_return_false(self):
        """Falsy values like None or 0 should result in is_enabled returning False."""
        gateway_none = FeatureFlagGateway({"use_agent_native_v2": None})
        assert gateway_none.is_enabled() is False

        gateway_zero = FeatureFlagGateway({"use_agent_native_v2": 0})
        assert gateway_zero.is_enabled() is False
