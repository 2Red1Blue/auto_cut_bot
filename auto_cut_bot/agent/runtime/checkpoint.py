"""Checkpoint persistence layer for the StateGraph engine.

Provides DB-backed checkpoint storage using PostgreSQL with psycopg2.
When the database is unavailable, all methods return safe defaults (None,
empty list, empty dict) without raising exceptions.

Usage::

    ckpt = CheckpointManager(db_url="postgresql://...", schema="autocut")
    ckpt_id = await ckpt.save(state, "running", "source_ready")
    restored = await ckpt.load(state.session_id)
"""

from __future__ import annotations

import json
from typing import Any

from autocut_core.logging import get_logger

logger = get_logger(__name__)

# ── Optional driver import ────────────────────────────────────────────────────

try:
    import psycopg2
    import psycopg2.extras

    _HAS_PSYCOPG2 = True
except ImportError:  # pragma: no cover — optional dependency
    _HAS_PSYCOPG2 = False


# ── Schema-qualified table name ────────────────────────────────────────────────


def _t(schema: str, table: str) -> str:
    """Return schema-qualified table name: ``autocut.agent_checkpoints``."""
    return f"{schema}.{table}"


# ── CREATE TABLE DDL ──────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS {table} (
    id              BIGSERIAL PRIMARY KEY,
    session_id      VARCHAR(64) NOT NULL,
    checkpoint_id   INTEGER NOT NULL,
    node_name       VARCHAR(128) NOT NULL,
    status          VARCHAR(32) NOT NULL,
    state_snapshot  JSONB NOT NULL,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (session_id, checkpoint_id)
)"""


# ── CheckpointManager ─────────────────────────────────────────────────────────


class CheckpointManager:
    """Manages DB-backed checkpoint persistence for the StateGraph engine.

    Checkpoints are stored in the ``{schema}.agent_checkpoints`` table.
    Each checkpoint is scoped to a ``session_id`` with an auto-incrementing
    ``checkpoint_id`` within that session.

    When the database is unavailable (no driver, connection failure, query
    error), all methods return safe defaults: ``None`` for loads, empty
    lists for listings, empty dicts for status queries.
    """

    def __init__(self, db_url: str, schema: str = "autocut") -> None:
        self._db_url = db_url
        self._schema = schema
        self._conn: Any = None
        self._table_ensured: bool = False

    # ── properties ────────────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        """True when a DB URL is configured and psycopg2 is installed."""
        return bool(self._db_url) and _HAS_PSYCOPG2

    # ── connection management ─────────────────────────────────────────────

    def _ensure_connection(self) -> Any:
        """Lazy-connect to PostgreSQL; returns None if unavailable."""
        if not self.is_available:
            return None
        if self._conn is not None and not getattr(self._conn, "closed", True):
            return self._conn
        try:
            self._conn = psycopg2.connect(self._db_url)
            self._conn.autocommit = False
            return self._conn
        except Exception as exc:
            logger.warning("CheckpointManager DB connection failed: %s", exc)
            self._conn = None
            return None

    def _ensure_table(self) -> bool:
        """Create the checkpoints table if it does not exist.

        Returns True on success, False on failure.
        """
        if self._table_ensured:
            return True
        conn = self._ensure_connection()
        if conn is None:
            return False
        try:
            table = _t(self._schema, "agent_checkpoints")
            sql = _CREATE_TABLE_SQL.format(table=table)
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            self._table_ensured = True
            return True
        except Exception as exc:
            logger.warning(
                "CheckpointManager table creation failed: %s", exc
            )
            try:
                conn.rollback()
            except Exception:
                pass
            return False

    def _execute(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a parameterized query; return rows as list of dicts.

        Returns an empty list on any error (connection, query, etc.).
        For write queries, commits on success and returns [].
        """
        if not self._ensure_table():
            return []
        conn = self._ensure_connection()
        if conn is None:
            return []
        try:
            with conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                cur.execute(sql, params)
                if cur.description is not None:
                    rows = list(cur.fetchall())
                else:
                    rows = []
                conn.commit()
                return rows
        except Exception as exc:
            logger.warning(
                "CheckpointManager query failed [%s]: %s",
                exc.__class__.__name__,
                exc,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            return []

    def close(self) -> None:
        """Close the underlying connection, if any."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._table_ensured = False

    # ── public API ────────────────────────────────────────────────────────

    async def save(
        self, state: Any, status: str, node_name: str
    ) -> int:
        """Save a checkpoint for the given state.

        Serializes ``state`` via ``state.to_dict()`` and stores it as JSONB.
        The ``checkpoint_id`` is auto-incremented within the session.

        Returns the DB row id on success, or 0 if the database is unavailable.
        """
        # Late import to avoid circular dependency at module level.
        from auto_cut_bot.agent.runtime.stategraph import AgentState

        if not isinstance(state, AgentState):
            logger.warning(
                "CheckpointManager.save: expected AgentState, got %s",
                type(state).__name__,
            )
            return 0

        state_json = json.dumps(state.to_dict(), ensure_ascii=False, default=str)
        table = _t(self._schema, "agent_checkpoints")

        sql = f"""
            INSERT INTO {table} (session_id, checkpoint_id, node_name, status, state_snapshot)
            VALUES (
                %s,
                COALESCE(
                    (SELECT MAX(checkpoint_id) FROM {table} WHERE session_id = %s), 0
                ) + 1,
                %s, %s, %s::jsonb
            )
            RETURNING id
        """
        rows = self._execute(
            sql,
            (state.session_id, state.session_id, node_name, status, state_json),
        )
        return rows[0]["id"] if rows else 0

    async def load(self, session_id: str) -> Any | None:
        """Load the latest checkpoint for a session.

        Returns an ``AgentState`` instance, or ``None`` if no checkpoint
        exists or the database is unavailable.
        """
        table = _t(self._schema, "agent_checkpoints")
        sql = f"""
            SELECT state_snapshot
            FROM {table}
            WHERE session_id = %s
            ORDER BY checkpoint_id DESC
            LIMIT 1
        """
        rows = self._execute(sql, (session_id,))
        if not rows:
            return None
        return self._deserialize(rows[0]["state_snapshot"])

    async def load_checkpoint(
        self, session_id: str, checkpoint_id: int
    ) -> Any | None:
        """Load a specific checkpoint by its sequence number.

        Returns an ``AgentState`` instance, or ``None`` if not found
        or the database is unavailable.
        """
        table = _t(self._schema, "agent_checkpoints")
        sql = f"""
            SELECT state_snapshot
            FROM {table}
            WHERE session_id = %s AND checkpoint_id = %s
        """
        rows = self._execute(sql, (session_id, checkpoint_id))
        if not rows:
            return None
        return self._deserialize(rows[0]["state_snapshot"])

    async def list_checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        """List all checkpoints for a session, ordered by checkpoint_id.

        Each dict contains: id, checkpoint_id, node_name, status, created_at.
        The ``state_snapshot`` is excluded to keep the listing lightweight.
        """
        table = _t(self._schema, "agent_checkpoints")
        sql = f"""
            SELECT id, checkpoint_id, node_name, status, created_at
            FROM {table}
            WHERE session_id = %s
            ORDER BY checkpoint_id
        """
        rows = self._execute(sql, (session_id,))
        return [dict(r) for r in rows]

    async def get_status(self, session_id: str) -> dict[str, Any]:
        """Get current session status from the latest checkpoint.

        Returns a dict with keys: status, node_name, checkpoint_id, created_at.
        Returns an empty dict if no checkpoint exists or DB is unavailable.
        """
        table = _t(self._schema, "agent_checkpoints")
        sql = f"""
            SELECT status, node_name, checkpoint_id, created_at
            FROM {table}
            WHERE session_id = %s
            ORDER BY checkpoint_id DESC
            LIMIT 1
        """
        rows = self._execute(sql, (session_id,))
        if not rows:
            return {}
        r = rows[0]
        return {
            "status": r["status"],
            "node_name": r["node_name"],
            "checkpoint_id": r["checkpoint_id"],
            "created_at": str(r["created_at"]) if r.get("created_at") else None,
        }

    # ── internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _deserialize(state_snapshot: Any) -> Any | None:
        """Deserialize a JSONB state_snapshot back into an AgentState.

        Handles both dict (already parsed by psycopg2) and str (raw JSON)
        formats.
        """
        from auto_cut_bot.agent.runtime.stategraph import AgentState

        try:
            if isinstance(state_snapshot, str):
                data = json.loads(state_snapshot)
            elif isinstance(state_snapshot, dict):
                data = state_snapshot
            else:
                logger.warning(
                    "CheckpointManager: unexpected state_snapshot type %s",
                    type(state_snapshot).__name__,
                )
                return None
            return AgentState.from_dict(data)
        except Exception as exc:
            logger.warning(
                "CheckpointManager: failed to deserialize state_snapshot: %s", exc
            )
            return None