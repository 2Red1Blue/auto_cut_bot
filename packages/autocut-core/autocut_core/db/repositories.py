"""Repository classes extracted from StageDBClient, one per aggregate.

Follows the Single Responsibility Principle: each repository owns the query
logic for exactly one aggregate.  StageDBClient remains the connection owner
and is never modified — repositories are thin wrappers that delegate to it.

Aggregates:
    - GlobalContextRepository   — ``global_context`` table
    - ConfidenceLogRepository   — ``vlm_confidence_log`` table
    - HighlightEvolutionRepository — ``highlight_skill_evolution`` table
"""

from __future__ import annotations

import json
from typing import Any

from autocut_core.logging import get_logger

logger = get_logger(__name__)


# ── GlobalContextRepository ─────────────────────────────────────────────────────


class GlobalContextRepository:
    """Repository for the ``global_context`` table.

    Encapsulates book-level synopsis, themes, and relationships persistance.
    """

    def __init__(self, db_client: Any) -> None:
        """*db_client* is a :class:`StageDBClient` instance (connection owner)."""
        self._db = db_client

    def upsert(
        self,
        book_id: str,
        *,
        synopsis: str | None = None,
        themes: list[str] | None = None,
        relationships: list[dict[str, Any]] | None = None,
        source: str = "api",
    ) -> int:
        """UPSERT global_context for a book.  Returns 1 on success, 0 if the DB
        is unavailable.
        """
        return self._db.upsert_global_context(
            book_id,
            synopsis=synopsis,
            themes=themes,
            relationships=relationships,
            source=source,
        )

    def query(self, book_id: str) -> dict[str, Any] | None:
        """Query global_context for *book_id*.  Returns ``None`` if not found
        or the DB is unavailable.
        """
        return self._db.query_global_context(book_id)


# ── ConfidenceLogRepository ─────────────────────────────────────────────────────


class ConfidenceLogRepository:
    """Repository for the ``vlm_confidence_log`` table.

    Encapsulates per-window VLM confidence metrics recording.
    """

    def __init__(self, db_client: Any) -> None:
        """*db_client* is a :class:`StageDBClient` instance (connection owner)."""
        self._db = db_client

    def write(
        self,
        book_id: str,
        window_id: str,
        *,
        total_dialogue: int = 0,
        high_conf: int = 0,
        low_conf: int = 0,
        characters_seen: list[str] | None = None,
        has_hard_subtitles: bool = False,
        enrichment_triggered: bool = False,
    ) -> int:
        """Insert a confidence-log entry.  Returns 1 on success, 0 if the DB is
        unavailable.
        """
        if not self._db.is_available:
            return 0
        table = _t(self._db.schema, "vlm_confidence_log")
        self._db._execute(
            f"""INSERT INTO {table}
                (book_id, window_id, total_dialogue, high_conf, low_conf,
                 characters_seen, has_hard_subtitles, enrichment_triggered)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                book_id,
                window_id,
                total_dialogue,
                high_conf,
                low_conf,
                characters_seen or [],
                has_hard_subtitles,
                enrichment_triggered,
            ),
        )
        return 1

    def query_by_book(self, book_id: str) -> list[dict[str, Any]]:
        """Return all confidence-log entries for *book_id*, newest first."""
        if not self._db.is_available:
            return []
        table = _t(self._db.schema, "vlm_confidence_log")
        return self._db._execute(
            f"SELECT * FROM {table} WHERE book_id = %s ORDER BY created_at DESC",
            (book_id,),
        )


# ── HighlightEvolutionRepository ────────────────────────────────────────────────


class HighlightEvolutionRepository:
    """Repository for the ``highlight_skill_evolution`` table.

    Encapsulates recording of skill evolution events triggered by highlight
    comparison results.
    """

    def __init__(self, db_client: Any) -> None:
        """*db_client* is a :class:`StageDBClient` instance (connection owner)."""
        self._db = db_client

    def record(
        self,
        skill_version: str,
        window_id: str,
        *,
        api_highlight: dict[str, Any] | None = None,
        vlm_miss_reason: str | None = None,
        skill_update: str | None = None,
    ) -> int:
        """Record a skill-evolution event.  Returns 1 on success, 0 if the DB is
        unavailable.
        """
        if not self._db.is_available:
            return 0
        table = _t(self._db.schema, "highlight_skill_evolution")
        api_highlight_json = (
            json.dumps(api_highlight, ensure_ascii=False)
            if api_highlight
            else "{}"
        )
        self._db._execute(
            f"""INSERT INTO {table}
                (skill_version, window_id, api_highlight, vlm_miss_reason, skill_update)
                VALUES (%s, %s, %s, %s, %s)""",
            (skill_version, window_id, api_highlight_json, vlm_miss_reason, skill_update),
        )
        return 1

    def query_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent skill-evolution records, newest first."""
        if not self._db.is_available:
            return []
        table = _t(self._db.schema, "highlight_skill_evolution")
        return self._db._execute(
            f"SELECT * FROM {table} ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )


# ── helpers ─────────────────────────────────────────────────────────────────────


def _t(schema: str, table: str) -> str:
    """Return schema-qualified table name."""
    return f"{schema}.{table}"