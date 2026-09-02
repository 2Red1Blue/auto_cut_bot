"""REST API for the StateGraph Engine — FastAPI router.

Clean Architecture: Adapters layer. Translates HTTP requests into engine calls
and domain entities, with Pydantic models for request/response serialization.

All endpoints are methods on the SessionAPI class for dependency injection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from autocut_core import get_logger
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from .engine import StateGraphEngine
from .entities import (
    Checkpoint,
    HumanDecision,
    Session,
    SessionStatus,
)
from .ports import ICheckpointRepository, ISessionStore

logger = get_logger(__name__)

# ── Pydantic models ──────────────────────────────────────────────────────────


class GraphConfig(BaseModel):
    """Optional graph configuration for a new session."""

    nodes: dict[str, dict[str, Any]] | None = Field(default=None)
    edges: list[dict[str, str]] | None = Field(default=None)
    entry_point: str | None = Field(default=None)
    milestones: list[dict[str, Any]] | None = Field(default=None)


class StartSessionRequest(BaseModel):
    """Request body for starting a new agent-native session."""

    project_id: str = Field(..., min_length=1, description="Project identifier")
    graph_config: GraphConfig | None = Field(default=None, description="Optional graph overrides")


class StartSessionResponse(BaseModel):
    """Response body after starting a session."""

    session_id: UUID
    status: SessionStatus


class ResumeSessionRequest(BaseModel):
    """Request body for resuming a HITL-paused session."""

    approved: bool = Field(..., description="Whether the human approved the gate")
    modifications: dict[str, Any] | None = Field(
        default=None, description="Optional modifications to apply"
    )
    reason: str = Field(default="", description="Reason for the decision")


class AbandonSessionRequest(BaseModel):
    """Request body for abandoning a session."""

    reason: str = Field(default="", description="Reason for abandonment")


class SessionStateResponse(BaseModel):
    """Response body for current session state."""

    session_id: UUID
    status: SessionStatus
    current_node: str
    milestone: str
    error: str | None


class CheckpointResponse(BaseModel):
    """Response body for a single checkpoint."""

    id: UUID
    session_id: UUID
    node_name: str
    node_type: str
    status: str
    attempt: int
    error_message: str | None
    duration_ms: int
    created_at: str

    @classmethod
    def from_entity(cls, cp: Checkpoint) -> CheckpointResponse:
        return cls(
            id=cp.id,
            session_id=cp.session_id or UUID(int=0),
            node_name=cp.node_name,
            node_type=cp.node_type.value,
            status=cp.status,
            attempt=cp.attempt,
            error_message=cp.error_message,
            duration_ms=cp.duration_ms,
            created_at=cp.created_at,
        )


class SessionListItem(BaseModel):
    """Response body for a session in the list endpoint."""

    session_id: UUID
    project_id: str
    status: SessionStatus
    current_node: str
    error_message: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_entity(cls, s: Session) -> SessionListItem:
        return cls(
            session_id=s.id,
            project_id=s.project_id,
            status=s.status,
            current_node=s.current_node,
            error_message=s.error_message,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )


class SessionListResponse(BaseModel):
    """Response body for listing sessions."""

    sessions: list[SessionListItem]
    total: int
    limit: int
    offset: int


class CheckpointListResponse(BaseModel):
    """Response body for listing checkpoints."""

    checkpoints: list[CheckpointResponse]
    total: int
    limit: int
    offset: int


class ErrorResponse(BaseModel):
    """Standard error response."""

    detail: str


# ── API class ────────────────────────────────────────────────────────────────


class SessionAPI:
    """FastAPI router for the StateGraph Engine session endpoints.

    Uses dependency injection: instantiate with the engine, session store,
    and checkpoint repository, then mount the router on your FastAPI app.
    """

    router: APIRouter

    def __init__(
        self,
        engine: StateGraphEngine,
        session_store: ISessionStore,
        checkpoint_repo: ICheckpointRepository,
    ) -> None:
        self._engine = engine
        self._sessions = session_store
        self._checkpoints = checkpoint_repo
        self.router = APIRouter(prefix="/api/v2/agent", tags=["agent"])
        self._register_routes()

    # ── Route registration ───────────────────────────────────────────────────

    def _register_routes(self) -> None:
        router = self.router

        router.add_api_route(
            "/sessions",
            self.list_sessions,
            methods=["GET"],
            response_model=SessionListResponse,
            summary="List sessions",
        )
        router.add_api_route(
            "/sessions",
            self.start_session,
            methods=["POST"],
            response_model=StartSessionResponse,
            status_code=status.HTTP_201_CREATED,
            summary="Start a new agent-native session",
        )
        router.add_api_route(
            "/sessions/{session_id}",
            self.get_session,
            methods=["GET"],
            response_model=SessionStateResponse,
            summary="Get current session state",
        )
        router.add_api_route(
            "/sessions/{session_id}/checkpoints",
            self.list_checkpoints,
            methods=["GET"],
            response_model=CheckpointListResponse,
            summary="List checkpoints for a session",
        )
        router.add_api_route(
            "/sessions/{session_id}/resume",
            self.resume_session,
            methods=["POST"],
            response_model=SessionStateResponse,
            summary="Resume a HITL-paused session",
        )
        router.add_api_route(
            "/sessions/{session_id}/abandon",
            self.abandon_session,
            methods=["POST"],
            response_model=SessionStateResponse,
            summary="Abandon a running or waiting_for_human session",
        )

    # ── Endpoints ────────────────────────────────────────────────────────────

    async def start_session(
        self, body: StartSessionRequest
    ) -> StartSessionResponse:
        """Start a new agent-native session from the graph entry point."""
        now = _utcnow()
        graph_config = body.graph_config.model_dump() if body.graph_config else {}

        session = Session(
            id=uuid4(),
            project_id=body.project_id,
            graph_config=graph_config,
            status=SessionStatus.RUNNING,
            created_at=now,
            updated_at=now,
        )

        try:
            session = await self._engine.start(session)
        except ValueError as exc:
            logger.warning("Failed to start session: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected error starting session")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            ) from exc

        return StartSessionResponse(
            session_id=session.id, status=session.status
        )

    async def resume_session(
        self, session_id: UUID, body: ResumeSessionRequest
    ) -> SessionStateResponse:
        """Resume a HITL-paused session with a human decision."""
        decision = HumanDecision(
            approved=body.approved,
            modifications=body.modifications,
            reason=body.reason,
            timestamp=_utcnow(),
        )

        try:
            session = await self._engine.resume(session_id, decision)
        except ValueError as exc:
            logger.warning("Failed to resume session %s: %s", session_id, exc)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected error resuming session %s", session_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            ) from exc

        return _session_to_state(session)

    async def get_session(self, session_id: UUID) -> SessionStateResponse:
        """Get the current state of a session."""
        session = await self._sessions.get(session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session not found: {session_id}",
            )
        return _session_to_state(session)

    async def list_checkpoints(
        self,
        session_id: UUID,
        limit: int = Query(default=50, ge=1, le=200, description="Max results"),
        offset: int = Query(default=0, ge=0, description="Pagination offset"),
    ) -> CheckpointListResponse:
        """List checkpoints for a session, newest first."""
        # Verify the session exists
        session = await self._sessions.get(session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session not found: {session_id}",
            )

        # Fetch all checkpoints from the repository
        all_checkpoints = await self._checkpoints.list_by_session(session_id)

        total = len(all_checkpoints)
        paginated = all_checkpoints[offset : offset + limit]

        return CheckpointListResponse(
            checkpoints=[CheckpointResponse.from_entity(cp) for cp in paginated],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def list_sessions(
        self,
        status_filter: SessionStatus | None = Query(
            default=None, alias="status", description="Filter by session status"
        ),
        project_id: str | None = Query(
            default=None, description="Filter by project ID"
        ),
        limit: int = Query(default=50, ge=1, le=200, description="Max results"),
        offset: int = Query(default=0, ge=0, description="Pagination offset"),
    ) -> SessionListResponse:
        """List sessions with optional status and project_id filters."""
        all_sessions = await self._sessions.list_by_status(status=status_filter)

        # Apply project_id filter if provided (in-memory since the port
        # interface does not have a combined filter)
        if project_id:
            all_sessions = [s for s in all_sessions if s.project_id == project_id]

        total = len(all_sessions)
        paginated = all_sessions[offset : offset + limit]

        return SessionListResponse(
            sessions=[SessionListItem.from_entity(s) for s in paginated],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def abandon_session(
        self, session_id: UUID, body: AbandonSessionRequest
    ) -> SessionStateResponse:
        """Abandon a running or waiting_for_human session."""
        session = await self._sessions.get(session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session not found: {session_id}",
            )

        if session.status not in (
            SessionStatus.RUNNING,
            SessionStatus.WAITING_FOR_HUMAN,
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Session {session_id} is {session.status.value}, "
                    f"cannot abandon"
                ),
            )

        session.status = SessionStatus.ABANDONED
        session.error_message = body.reason or "Abandoned by user"
        session.updated_at = _utcnow()
        session.completed_at = _utcnow()

        try:
            session = await self._sessions.save(session)
        except Exception as exc:
            logger.exception("Failed to save abandoned session %s", session_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            ) from exc

        logger.info(
            "Session %s abandoned: %s", session_id, session.error_message
        )

        return _session_to_state(session)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_to_state(session: Session) -> SessionStateResponse:
    return SessionStateResponse(
        session_id=session.id,
        status=session.status,
        current_node=session.current_node,
        milestone=session.current_milestone,
        error=session.error_message,
    )
