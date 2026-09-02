"""Abstract ports (interfaces) for the StateGraph Engine.

Clean Architecture: Use Cases layer. Defines contracts that adapters implement.
No framework or database dependencies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from .entities import (
    Checkpoint,
    NodeResult,
    Session,
    SessionStatus,
)

# ── Domain Agent shared types (defined here to satisfy DIP) ──────────────────

class DomainStatus(str, Enum):
    """Status of a domain agent execution.

    Defined in autocut_core so nodes.py can reference it without importing
    from auto_cut_bot (DIP). auto_cut_bot._domain_result re-exports from here.
    """

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    WAITING_HUMAN = "waiting_human"


@dataclass
class DomainContext:
    """Context passed to a domain agent during execution.

    Contains the job root, configuration, artifact bus, backend, and mode.
    Defined in autocut_core so inner-layer code can reference it without
    importing from auto_cut_bot (DIP).
    """

    job_root: Path | str
    config: Any = None  # PipelineConfig
    bus: Any = None  # ArtifactBus
    backend: str = "qwen"
    mode: str = "auto"


# ── Abstract ports (interfaces) ─────────────────────────────────────────────


class ICheckpointRepository(ABC):
    """Abstract interface for checkpoint persistence."""

    @abstractmethod
    async def save(self, checkpoint: Checkpoint) -> Checkpoint:
        ...

    @abstractmethod
    async def get(self, checkpoint_id: UUID) -> Checkpoint | None:
        ...

    @abstractmethod
    async def list_by_session(
        self, session_id: UUID, limit: int = 50
    ) -> list[Checkpoint]:
        ...

    @abstractmethod
    async def get_latest(self, session_id: UUID) -> Checkpoint | None:
        ...


class ISessionStore(ABC):
    """Abstract interface for session persistence."""

    @abstractmethod
    async def save(self, session: Session) -> Session:
        ...

    @abstractmethod
    async def get(self, session_id: UUID) -> Session | None:
        ...

    @abstractmethod
    async def list_by_status(
        self, status: SessionStatus | None = None, limit: int = 50
    ) -> list[Session]:
        ...

    @abstractmethod
    async def update_status(
        self, session_id: UUID, status: SessionStatus, error: str | None = None
    ) -> None:
        ...


class IEventEmitter(ABC):
    """Abstract interface for structured event emission."""

    @abstractmethod
    async def emit(
        self,
        session_id: UUID,
        event_type: str,
        node_name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        ...


class INodePlugin(ABC):
    """Abstract interface for a graph node plugin.

    Each node type (sub_agent, milestone, hitl_gate) implements this.
    """

    @abstractmethod
    async def execute(self, state: dict[str, Any]) -> NodeResult:
        ...

    @abstractmethod
    def can_handle(self, node_type: str) -> bool:
        ...


class ISubAgent(ABC):
    """Abstract interface for sub-agent execution.

    Domain agents implement this interface. The StateGraph engine depends
    only on ISubAgent, never on concrete DomainAgent implementations.
    This eliminates the DIP violation where autocut_core.plugins imported
    from auto_cut_bot.agent.tools.pipeline.
    """

    @property
    @abstractmethod
    def contract(self) -> Any:
        """The agent's contract (DomainAgentContract)."""
        ...

    @abstractmethod
    async def execute(self, ctx: DomainContext) -> Any:
        """Execute with DomainContext, return DomainResult."""
        ...

    @abstractmethod
    async def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        """StateGraph-compatible invocation."""
        ...
