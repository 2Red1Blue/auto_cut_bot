"""StateGraphEngine — DAG executor for the Agent-Native V2 pipeline.

Clean Architecture: Use Cases layer. Orchestrates node execution through
a directed graph, persists checkpoints, and handles HITL resume.

The engine is stateless — all state is in Session + Checkpoint entities.
"""

from __future__ import annotations

import logging
import time
from uuid import UUID

from .entities import (
    Checkpoint,
    HumanDecision,
    NodeType,
    Session,
    SessionStatus,
    StateGraph,
)
from .ports import (
    ICheckpointRepository,
    IEventEmitter,
    INodePlugin,
    ISessionStore,
)

logger = logging.getLogger(__name__)


MAX_REVIEW_RETRIES = 3


class StateGraphEngine:
    """Directed graph executor for agent-native pipeline execution.

    Drives execution through a StateGraph, persisting checkpoints before
    each node invocation and emitting structured events for observability.
    """

    def __init__(
        self,
        graph: StateGraph,
        node_plugins: dict[str, INodePlugin],
        checkpoint_repo: ICheckpointRepository,
        session_store: ISessionStore,
        event_emitter: IEventEmitter,
    ) -> None:
        self._graph = graph
        self._plugins = node_plugins
        self._checkpoints = checkpoint_repo
        self._sessions = session_store
        self._events = event_emitter

    async def start(self, session: Session) -> Session:
        """Start a new session from the graph's entry point."""
        session.status = SessionStatus.RUNNING
        session.current_node = self._graph.entry_point
        session = await self._sessions.save(session)

        await self._events.emit(
            session.id, "session_started",
            payload={"entry_point": session.current_node},
        )

        # Execute the entry node
        await self._execute_node(session, session.current_node)
        return session

    async def resume(
        self, session_id: UUID, decision: HumanDecision
    ) -> Session:
        """Resume a HITL-paused session with a human decision."""
        session = await self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")

        if session.status != SessionStatus.WAITING_FOR_HUMAN:
            raise ValueError(
                f"Session {session_id} is {session.status}, not waiting_for_human"
            )

        # Apply human decision to the latest checkpoint
        latest = await self._checkpoints.get_latest(session_id)
        if latest and latest.node_type == NodeType.HITL_GATE:
            latest.human_decision = {
                "approved": decision.approved,
                "modifications": decision.modifications,
                "reason": decision.reason,
                "timestamp": decision.timestamp,
            }
            await self._checkpoints.save(latest)

        session.status = SessionStatus.RUNNING
        session = await self._sessions.save(session)

        # Inject human decision as user message into state for the Agent
        decision_text = (
            f"Human Review Decision:\n"
            f"- Status: {'Approved' if decision.approved else 'Rejected'}\n"
            f"- Reason: {decision.reason}"
        )
        if decision.modifications:
            import json as _json
            decision_text += f"\n- Modifications: {_json.dumps(decision.modifications, ensure_ascii=False)}"
        session.state_snapshot["_human_decision"] = decision_text

        await self._events.emit(
            session_id, "human_decision",
            node_name=session.current_node,
            payload={"approved": decision.approved},
        )

        # Continue to next node
        next_node = self._graph.get_next_node(session.current_node, session.state_snapshot)
        if next_node:
            await self._execute_node(session, next_node)
        else:
            session.status = SessionStatus.COMPLETED
            session = await self._sessions.save(session)

        return session

    async def _archive_session(self, session: Session) -> None:
        """Archive session state for cross-session learning (Dream).

        Called on session completion. Extracts key decisions and outcomes
        from the session state so Dream can learn from this pipeline run.
        Best-effort — failures are logged but don't block completion.
        """
        try:
            summary = {
                "session_id": str(session.id),
                "project_id": session.project_id,
                "milestone": session.current_milestone,
                "status": session.status.value if hasattr(session.status, 'value') else str(session.status),
                "review_verdict": session.state_snapshot.get("_review_verdict"),
                "retry_count": session.state_snapshot.get("_retry_count", 0),
                "artifacts": session.state_snapshot.get("_artifacts", []),
            }
            logger.info("Session archived for Dream: %s", summary.get("session_id"))
        except Exception as exc:
            logger.warning("Failed to archive session for Dream: %s", exc)

    def _find_retry_node(self, review_node_id: str) -> str | None:
        """Find the node that feeds into a review gate (the one to retry on rejection).

        Walks edges backward to find the first non-review-gate node that feeds
        into this review gate. This is the node that should be re-executed
        with rejection reasons.
        """
        for edge in self._graph.edges:
            if edge.target == review_node_id and edge.condition != "approved":
                return edge.source
        return None

    async def _execute_node(self, session: Session, node_id: str) -> None:
        """Execute a graph node with checkpointing."""
        node = self._graph.get_node(node_id)
        if node is None:
            raise ValueError(f"Node not found: {node_id}")

        session.current_node = node_id

        # Checkpoint before execution
        checkpoint = Checkpoint(
            session_id=session.id,
            node_name=node_id,
            node_type=node.type,
            state_snapshot=dict(session.state_snapshot),
            attempt=1,
            status="completed",
        )
        checkpoint = await self._checkpoints.save(checkpoint)

        await self._events.emit(
            session.id, "node_entered",
            node_name=node_id,
            payload={"node_type": node.type.value},
        )

        # Find the right plugin for this node type
        plugin = self._plugins.get(node.type.value)
        if plugin is None:
            raise ValueError(f"No plugin for node type: {node.type.value}")

        # Execute with timeout
        t0 = time.monotonic()
        try:
            result = await plugin.execute(session.state_snapshot)
            duration_ms = int((time.monotonic() - t0) * 1000)

            checkpoint.duration_ms = duration_ms
            checkpoint.status = result.status
            checkpoint.error_message = result.error

            # Update session state with node output
            session.state_snapshot.update(result.output)

            await self._events.emit(
                session.id, "node_completed",
                node_name=node_id,
                payload={"status": result.status, "duration_ms": duration_ms},
            )

        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            checkpoint.status = "failed"
            checkpoint.error_message = str(exc)
            checkpoint.duration_ms = duration_ms

            logger.error("Node %s failed: %s", node_id, exc)

            await self._events.emit(
                session.id, "node_failed",
                node_name=node_id,
                payload={"error": str(exc), "duration_ms": duration_ms},
            )

            session.status = SessionStatus.FAILED
            session.error_message = str(exc)
            await self._checkpoints.save(checkpoint)
            await self._sessions.save(session)
            return

        await self._checkpoints.save(checkpoint)

        # Handle HITL gate: pause and wait
        if node.type == NodeType.HITL_GATE and result.status == "waiting_human":
            session.status = SessionStatus.WAITING_FOR_HUMAN
            await self._sessions.save(session)
            return

        # Handle REVIEW_GATE: auto-retry on rejection (with circuit breaker)
        if node.type == NodeType.REVIEW_GATE and result.status == "waiting_human":
            verdict = result.output.get("review", {})
            if verdict.get("status") == "rejected":
                retry_count = session.state_snapshot.get("_retry_count", 0)
                if retry_count >= MAX_REVIEW_RETRIES:
                    logger.warning(
                        "Review rejected %d times (max=%d), escalating to HITL",
                        retry_count, MAX_REVIEW_RETRIES,
                    )
                    session.status = SessionStatus.WAITING_FOR_HUMAN
                    session.state_snapshot["_review_escalated"] = True
                    await self._sessions.save(session)
                    return

                retry_node = self._find_retry_node(node_id)
                if retry_node:
                    session.state_snapshot["_review_reasons"] = verdict.get("reasons", [])
                    session.state_snapshot["_review_score"] = verdict.get("score", 0)
                    session.state_snapshot["_retry_count"] = retry_count + 1

                    await self._events.emit(
                        session.id, "review_rejected",
                        node_name=node_id,
                        payload={"retry_node": retry_node, "retry_count": retry_count + 1,
                                 "reasons": verdict.get("reasons", [])},
                    )
                    await self._execute_node(session, retry_node)
                    return
                else:
                    logger.warning("Review rejected but no retry node found for %s", node_id)

            # Approved or no retry → continue to next node
            session.status = SessionStatus.RUNNING

        # Continue to next node
        next_node = self._graph.get_next_node(node_id)
        if next_node:
            await self._execute_node(session, next_node)
        else:
            session.status = SessionStatus.COMPLETED
            await self._sessions.save(session)
            await self._events.emit(
                session.id, "session_completed",
                payload={"milestone": session.current_milestone},
            )
            # Trigger cross-session archiving so Dream can learn from this run
            await self._archive_session(session)
