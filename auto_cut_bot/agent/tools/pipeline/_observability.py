"""Agent-Native Observability: SSE event source for session/node/checkpoint events.

Reuses StateGraphEngine's existing event emission (InMemoryEventEmitter).
Exposes events as SSE stream for frontend Tracer and approval notifications.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class SSEEventSource:
    """SSE event source wrapping InMemoryEventEmitter for streaming.

    Usage (FastAPI):
        @app.get("/api/v2/agent/sessions/{session_id}/events/stream")
        async def stream_events(session_id: str):
            source = SSEEventSource(events, session_id)
            return StreamingResponse(source.stream(), media_type="text/event-stream")
    """

    def __init__(self, event_emitter: Any, session_id: str) -> None:
        self._emitter = event_emitter
        self._session_id = session_id
        self._last_idx = 0

    async def stream(self):
        """Async generator yielding SSE-formatted events."""
        while True:
            events = self._emitter.get_events(self._session_id)
            new_events = events[self._last_idx:]

            for event in new_events:
                yield f"event: {event['event_type']}\n"
                yield f"data: {json.dumps(event, default=str)}\n\n"
                self._last_idx += 1

            await asyncio.sleep(0.5)


class ObservabilityDashboard:
    """Read-only dashboard data: active sessions, checkpoint history, failure rates."""

    @staticmethod
    def build_summary(
        sessions: list[Any],
        checkpoints_by_session: dict[str, list[Any]],
    ) -> dict[str, Any]:
        """Build dashboard summary from session and checkpoint data."""
        total = len(sessions)
        status_counts: dict[str, int] = {}
        failure_rate = 0.0

        for s in sessions:
            status = getattr(s, 'status', 'unknown')
            if hasattr(status, 'value'):
                status = status.value
            status_counts[status] = status_counts.get(status, 0) + 1

        if total > 0:
            failed = status_counts.get('failed', 0)
            failure_rate = round(failed / total, 3)

        return {
            "total_sessions": total,
            "status_breakdown": status_counts,
            "failure_rate": failure_rate,
            "active_sessions": status_counts.get('running', 0) + status_counts.get('waiting_for_human', 0),
            "completed_sessions": status_counts.get('completed', 0),
            "failed_sessions": status_counts.get('failed', 0),
        }
