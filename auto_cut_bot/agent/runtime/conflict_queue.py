"""Conflict queue for HITL (Human-in-the-Loop) resolution (Doc 22).

Manages source conflicts that cannot be resolved automatically by the
deterministic merge operator. Conflicts are queued, classified by severity,
and resolved either by human decision, auto-policy, or VLM arbitration.

Components:
  1. SourceConflict — dataclass for a single unresolved field-level conflict
  2. ConflictQueue — thread-safe in-memory queue with optional DB persistence
  3. Severity inference — deterministic mapping from field path to severity
  4. HITL integration — check_and_interrupt for StateGraph engine hook
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# 1. SourceConflict data model
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class SourceConflict:
    """A single unresolved conflict between two or more data sources.

    Fields:
        id: Unique conflict identifier (assigned by queue on add).
        entity_id: Which entity, e.g. "characters[3]".
        field_path: Field name within the entity, e.g. "persona".
        candidates: Source -> value, e.g. {"llm_pass1": {...}, "api": {...}}.
        severity: "high" (blocks progress) or "low" (informational).
        status: Lifecycle — "pending", "auto_resolved", or "human_resolved".
        resolution: The final resolved value after decision.
        created_at: ISO 8601 timestamp when the conflict was created.
        resolved_at: ISO 8601 timestamp when resolved (None if pending).
        resolved_by: "auto_policy", "human", or "vlm".
    """

    id: str | None = None
    entity_id: str = ""
    field_path: str = ""
    candidates: dict[str, Any] = field(default_factory=dict)
    severity: str = "low"
    status: str = "pending"
    resolution: dict[str, Any] | None = None
    created_at: str = ""
    resolved_at: str | None = None
    resolved_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "entity_id": self.entity_id,
            "field_path": self.field_path,
            "candidates": dict(self.candidates),
            "severity": self.severity,
            "status": self.status,
            "resolution": self.resolution,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceConflict:
        return cls(
            id=data.get("id"),
            entity_id=data.get("entity_id", ""),
            field_path=data.get("field_path", ""),
            candidates=dict(data.get("candidates", {})),
            severity=data.get("severity", "low"),
            status=data.get("status", "pending"),
            resolution=data.get("resolution"),
            created_at=data.get("created_at", ""),
            resolved_at=data.get("resolved_at"),
            resolved_by=data.get("resolved_by"),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Severity inference (deterministic)
# ═══════════════════════════════════════════════════════════════════════════════

HIGH_IMPACT_FIELDS: set[str] = {
    "characters[].persona",
    "characters[].name",
    "scenes[].location",
    "episodes[].episode_number",
}

HIGH_IMPACT_DEPENDENCIES: set[str] = {
    "story_scripts",
    "story_render",
}

RENDERING_FIELDS: set[str] = {
    "characters[].name",
    "episodes[].episode_number",
    "episodes[].duration",
}


def infer_severity(
    field_path: str,
    downstream_dependencies: list[str] | None = None,
) -> str:
    """Infer conflict severity based on downstream impact.

    High severity:
    - Field is depended on by story_scripts or story_render.
    - Field is part of rendering decision (character count, duration).

    Low severity:
    - Field is informational only.
    - Field is used only in optional QC steps.

    Args:
        field_path: Normalised field path (e.g. "characters[].persona").
        downstream_dependencies: List of downstream stage names that depend
            on this field. If empty or None, only the field_path lookup is used.

    Returns:
        "high" or "low".
    """
    if field_path in HIGH_IMPACT_FIELDS:
        return "high"

    if field_path in RENDERING_FIELDS:
        return "high"

    if downstream_dependencies:
        if any(dep in HIGH_IMPACT_DEPENDENCIES for dep in downstream_dependencies):
            return "high"

    return "low"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. ConflictQueue
# ═══════════════════════════════════════════════════════════════════════════════


class ConflictQueue:
    """Manages the conflict resolution queue.

    In-memory by default. When ``db_url`` is provided, conflicts are also
    persisted to PostgreSQL via the ``{schema}.conflict_queue`` table.

    Usage::

        queue = ConflictQueue()
        cid = queue.add(SourceConflict(
            entity_id="characters[3]",
            field_path="persona",
            candidates={"llm_pass1": {...}, "api": {...}},
            severity="high",
        ))
        blocked = queue.is_blocked(session_id)

    Thread-safety: the internal list is guarded by a simple lock. For
    multi-process deployments, use the DB-backed persistence path.
    """

    def __init__(self, db_url: str = "", schema: str = "autocut") -> None:
        import threading

        self._db_url = db_url
        self._schema = schema
        self._lock = threading.Lock()
        self._conflicts: dict[str, SourceConflict] = {}
        self._counter: int = 0

    # ── public API ────────────────────────────────────────────────────────

    def add(self, conflict: SourceConflict) -> str:
        """Add a conflict to the queue. Returns the assigned conflict ID.

        If no ``created_at`` is set, it is populated with the current UTC
        timestamp. If no ``id`` is set, a sequential ID is generated.
        """
        if not conflict.created_at:
            conflict.created_at = datetime.now(timezone.utc).isoformat()

        with self._lock:
            self._counter += 1
            conflict_id = conflict.id or f"cf_{self._counter:06d}"
            conflict.id = conflict_id
            self._conflicts[conflict_id] = conflict

        self._persist(conflict)
        return conflict_id

    def get(self, conflict_id: str) -> SourceConflict | None:
        """Retrieve a conflict by its ID."""
        with self._lock:
            return self._conflicts.get(conflict_id)

    def get_pending(
        self, session_id: str = "", severity: str = ""
    ) -> list[SourceConflict]:
        """Get pending conflicts, optionally filtered by severity.

        Args:
            session_id: Reserved for future DB-backed session scoping.
            severity: If non-empty, filter by severity ("high" or "low").
        """
        with self._lock:
            result = [
                c for c in self._conflicts.values()
                if c.status == "pending"
            ]
            if severity:
                result = [c for c in result if c.severity == severity]
            return result

    def get_high_severity(
        self, session_id: str = ""
    ) -> list[SourceConflict]:
        """Get high-severity conflicts that block progress."""
        return self.get_pending(session_id=session_id, severity="high")

    def resolve(
        self,
        conflict_id: str,
        resolution: dict[str, Any],
        resolved_by: str = "human",
    ) -> bool:
        """Resolve a conflict with a decision.

        Args:
            conflict_id: The conflict to resolve.
            resolution: The final resolved value (e.g. {"canonical": {...}}).
            resolved_by: Who resolved it — "human", "auto_policy", or "vlm".

        Returns:
            True if the conflict was found and resolved, False otherwise.
        """
        with self._lock:
            conflict = self._conflicts.get(conflict_id)
            if conflict is None:
                return False

            conflict.status = "human_resolved" if resolved_by == "human" else "auto_resolved"
            conflict.resolution = resolution
            conflict.resolved_at = datetime.now(timezone.utc).isoformat()
            conflict.resolved_by = resolved_by

        self._persist(conflict)
        return True

    def is_blocked(self, session_id: str = "") -> bool:
        """Check if there are high-severity unresolved conflicts.

        Returns True if the pipeline is blocked (has at least one pending
        high-severity conflict), False if it can proceed.
        """
        with self._lock:
            for conflict in self._conflicts.values():
                if (
                    conflict.status == "pending"
                    and conflict.severity == "high"
                ):
                    return True
        return False

    def count(self, severity: str = "", status: str = "") -> int:
        """Count conflicts, optionally filtered by severity and/or status."""
        with self._lock:
            result = list(self._conflicts.values())
            if severity:
                result = [c for c in result if c.severity == severity]
            if status:
                result = [c for c in result if c.status == status]
            return len(result)

    def clear(self) -> None:
        """Remove all conflicts from the queue."""
        with self._lock:
            self._conflicts.clear()
            self._counter = 0

    def to_list(self) -> list[dict[str, Any]]:
        """Export all conflicts as a list of dicts."""
        with self._lock:
            return [c.to_dict() for c in self._conflicts.values()]

    # ── internal helpers ──────────────────────────────────────────────────

    def _persist(self, conflict: SourceConflict) -> None:
        """Persist a conflict to the database if configured."""
        if not self._db_url:
            return
        try:
            self._db_insert(conflict)
        except Exception:
            pass

    def _db_insert(self, conflict: SourceConflict) -> None:
        """Insert or update a conflict row in PostgreSQL."""
        import json

        try:
            import psycopg2
        except ImportError:
            return

        table = f"{self._schema}.conflict_queue"
        try:
            conn = psycopg2.connect(self._db_url)
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id              VARCHAR(64) PRIMARY KEY,
                        entity_id       VARCHAR(256) NOT NULL,
                        field_path      VARCHAR(256) NOT NULL,
                        candidates      JSONB NOT NULL DEFAULT '{{}}',
                        severity        VARCHAR(16) NOT NULL DEFAULT 'low',
                        status          VARCHAR(32) NOT NULL DEFAULT 'pending',
                        resolution      JSONB,
                        created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        resolved_at     TIMESTAMP WITH TIME ZONE,
                        resolved_by     VARCHAR(32)
                    )
                    """
                )
                cur.execute(
                    f"""
                    INSERT INTO {table}
                        (id, entity_id, field_path, candidates, severity,
                         status, resolution, created_at, resolved_at, resolved_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        status = EXCLUDED.status,
                        resolution = EXCLUDED.resolution,
                        resolved_at = EXCLUDED.resolved_at,
                        resolved_by = EXCLUDED.resolved_by
                    """,
                    (
                        conflict.id,
                        conflict.entity_id,
                        conflict.field_path,
                        json.dumps(conflict.candidates, ensure_ascii=False, default=str),
                        conflict.severity,
                        conflict.status,
                        json.dumps(conflict.resolution, ensure_ascii=False, default=str) if conflict.resolution else None,
                        conflict.created_at,
                        conflict.resolved_at,
                        conflict.resolved_by,
                    ),
                )
            conn.commit()
            conn.close()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# 4. HITL integration
# ═══════════════════════════════════════════════════════════════════════════════


def check_and_interrupt(state: Any, queue: ConflictQueue) -> bool:
    """Check if there are blocking conflicts. If so, set HITL interrupt.

    Intended to be called at the start of each pipeline stage that depends
    on merged data. When the queue has high-severity unresolved conflicts,
    the state is set to ``waiting_for_human`` and the caller should yield
    to the HITL loop.

    Args:
        state: An AgentState instance (from stategraph.py).
        queue: The ConflictQueue managing conflicts for this session.

    Returns:
        True if interrupted (blocked), False if the pipeline can proceed.
    """
    session_id = getattr(state, "session_id", "")

    if queue.is_blocked(session_id):
        state.status = "waiting_for_human"
        state.interrupt_reason = "source_conflicts"
        state.interrupt_data = {
            "conflicts": [
                c.to_dict() for c in queue.get_high_severity(session_id)
            ],
        }
        return True

    return False


__all__ = [
    "SourceConflict",
    "ConflictQueue",
    "HIGH_IMPACT_FIELDS",
    "HIGH_IMPACT_DEPENDENCIES",
    "RENDERING_FIELDS",
    "infer_severity",
    "check_and_interrupt",
]