"""In-memory adapter implementations for the StateGraph Engine ports.

Clean Architecture: Adapters layer. Each class implements a single abstract
interface (SRP) and depends only on the port abstraction (DIP). All
implementations are injectable and require no external infrastructure,
making them suitable for unit tests, integration tests, and local
development.

Classes:
    InMemoryCheckpointRepository -- stores checkpoints in a dict keyed by UUID.
    InMemorySessionStore        -- stores sessions in a dict keyed by UUID.
    InMemoryEventEmitter        -- captures events in a list for assertion.
    FeatureFlagGateway          -- boolean gate that reads use_agent_native_v2.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from state_graph.agent.entities import (
    Checkpoint,
    Session,
    SessionStatus,
)
from state_graph.agent.ports import (
    ICheckpointRepository,
    IEventEmitter,
    ISessionStore,
)


# ---------------------------------------------------------------------------
# InMemoryCheckpointRepository
# ---------------------------------------------------------------------------


class InMemoryCheckpointRepository(ICheckpointRepository):
    """Stores checkpoints in an in-memory dict keyed by UUID.

    SRP: single reason to change -- checkpoint storage format.
    DIP: depends on ICheckpointRepository, not on any concrete DB driver.
    """

    def __init__(self) -> None:
        self._store: dict[UUID, Checkpoint] = {}

    async def save(self, checkpoint: Checkpoint) -> Checkpoint:
        """Persist (or update) a checkpoint in memory."""
        self._store[checkpoint.id] = checkpoint
        return checkpoint

    async def get(self, checkpoint_id: UUID) -> Checkpoint | None:
        """Retrieve a single checkpoint by id."""
        return self._store.get(checkpoint_id)

    async def list_by_session(
        self, session_id: UUID, limit: int = 50,
    ) -> list[Checkpoint]:
        """Return checkpoints belonging to *session_id*, newest first."""
        matching = [
            cp
            for cp in self._store.values()
            if cp.session_id == session_id
        ]
        matching.sort(key=lambda cp: cp.created_at, reverse=True)
        return matching[:limit]

    async def get_latest(self, session_id: UUID) -> Checkpoint | None:
        """Return the most recent checkpoint for *session_id*."""
        matching = [
            cp
            for cp in self._store.values()
            if cp.session_id == session_id
        ]
        if not matching:
            return None
        matching.sort(key=lambda cp: cp.created_at, reverse=True)
        return matching[0]


# ---------------------------------------------------------------------------
# InMemorySessionStore
# ---------------------------------------------------------------------------


class InMemorySessionStore(ISessionStore):
    """Stores sessions in an in-memory dict keyed by UUID.

    SRP: single reason to change -- session storage format.
    DIP: depends on ISessionStore, not on any concrete DB driver.
    """

    def __init__(self) -> None:
        self._store: dict[UUID, Session] = {}

    async def save(self, session: Session) -> Session:
        """Persist (or update) a session in memory."""
        self._store[session.id] = session
        return session

    async def get(self, session_id: UUID) -> Session | None:
        """Retrieve a single session by id."""
        return self._store.get(session_id)

    async def list_by_status(
        self,
        status: SessionStatus | None = None,
        limit: int = 50,
    ) -> list[Session]:
        """Return sessions filtered by status, newest first."""
        if status is None:
            matching = list(self._store.values())
        else:
            matching = [
                s for s in self._store.values() if s.status == status
            ]
        matching.sort(key=lambda s: s.created_at, reverse=True)
        return matching[:limit]

    async def update_status(
        self,
        session_id: UUID,
        status: SessionStatus,
        error: str | None = None,
    ) -> None:
        """Atomically update a session's status (and optional error message)."""
        session = self._store.get(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        session.status = status
        if error is not None:
            session.error_message = error


# ---------------------------------------------------------------------------
# InMemoryEventEmitter
# ---------------------------------------------------------------------------


class InMemoryEventEmitter(IEventEmitter):
    """Captures structured events in a list for test assertion.

    SRP: single reason to change -- event capture format.
    DIP: depends on IEventEmitter, not on any message bus transport.
    """

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    async def emit(
        self,
        session_id: UUID,
        event_type: str,
        node_name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Record a structured event."""
        event: dict[str, Any] = {
            "session_id": session_id,
            "event_type": event_type,
        }
        if node_name is not None:
            event["node_name"] = node_name
        if payload is not None:
            event["payload"] = payload
        self._events.append(event)

    def get_events(self, session_id: UUID) -> list[dict[str, Any]]:
        """Return all events for a given session (most recent last)."""
        return [
            e for e in self._events if e["session_id"] == session_id
        ]

    def clear(self) -> None:
        """Drop all recorded events (useful for test teardown)."""
        self._events.clear()


# ---------------------------------------------------------------------------
# FeatureFlagGateway
# ---------------------------------------------------------------------------


class FeatureFlagGateway:
    """Boolean gate that inspects the use_agent_native_v2 feature flag.

    SRP: single reason to change -- the decision of which engine to route to.
    DIP: this class is a policy object; the caller (orchestrator / API
         handler) reads the boolean and injects the correct engine.

    This gateway does NOT own the engine instances.  It exposes *is_enabled* so
    the caller can decide whether to construct a StateGraphEngine or a
    LegacyPipelineAdapter.
    """

    FLAG_KEY = "use_agent_native_v2"

    def __init__(self, config: dict[str, Any]) -> None:
        self._enabled = bool(config.get(self.FLAG_KEY, False))

    def is_enabled(self) -> bool:
        """Return True when the agent-native V2 engine should be used."""
        return self._enabled