"""StageDBClient — Stage-level DB client for pipeline CRUD operations.

Covers all 10 tables in the ``autocut`` schema.
When db_url is None (default), all methods become no-ops — DB is an accelerator,
not a required component. Every DB call is wrapped in try/except to never crash
the pipeline.

Dependencies (optional):
  - psycopg2 (primary, synchronous) — ``pip install psycopg2-binary``
  - asyncpg (alternative, async) — ``pip install asyncpg``
  Neither installed → client is always a no-op (same as db_url=None).
"""

from __future__ import annotations

from typing import Any

from autocut_core.logging import get_logger

logger = get_logger(__name__)

# ── Optional driver imports ──────────────────────────────────────────────────
try:
    import psycopg2
    import psycopg2.extras

    _HAS_PSYCOPG2 = True
except ImportError:  # pragma: no cover — optional dependency
    _HAS_PSYCOPG2 = False

try:
    import asyncpg  # noqa: F401 — reserved for future async support

    _HAS_ASYNCPG = True
except ImportError:  # pragma: no cover — optional dependency
    _HAS_ASYNCPG = False

# ── Schema-qualified table name helper ────────────────────────────────────────


def _t(schema: str, table: str) -> str:
    """Return schema-qualified table name: ``autocut.books``."""
    return f"{schema}.{table}"


# ── StageDBClient ─────────────────────────────────────────────────────────────


class StageDBClient:
    """Stage-level DB client — instantiated per Stage as needed.

    Usage::

        db = StageDBClient(db_url="postgresql://...", schema="autocut")
        if db.is_available:
            db.upsert_book(book_id="42000023011", book_name="When Lucifer Kneels for Love")

    When ``db_url`` is None or no driver is installed, all methods return
    sensible defaults (empty lists, 0 rows affected, None) without raising.
    """

    def __init__(
        self,
        db_url: str | None = None,
        schema: str = "autocut",
        auto_migrate: bool = True,
    ) -> None:
        self._db_url = db_url
        self._schema = schema
        self._conn: Any = None
        if auto_migrate:
            self._auto_migrate()

    def _auto_migrate(self) -> None:
        """Best-effort apply pending migrations on first use.

        Ensures the schema (tables/columns) is present and up-to-date so the
        pipeline never fails because the DB is missing tables. No-ops when DB
        is unavailable (mirrors the client's graceful degradation).
        """
        if not self.is_available:
            return
        try:
            from auto_cut_bot.pipeline.core.db.migrate import ensure_schema

            ensure_schema(self._db_url, schema=self._schema)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Auto-migration skipped: %s", exc)

    # ── properties ────────────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        """True when a DB URL is configured and a driver is installed."""
        return self._db_url is not None and _HAS_PSYCOPG2

    @property
    def schema(self) -> str:
        return self._schema

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
            logger.warning("DB connection failed: %s", exc)
            self._conn = None
            return None

    def _execute(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> list[dict[str, Any]]:
        """Execute a parameterized query; return rows as list of dicts.

        Returns an empty list on any error (connection, query, etc.).
        For write queries, commits on success and returns [].
        """
        conn = self._ensure_connection()
        if conn is None:
            return []
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                if cur.description is not None:
                    rows = list(cur.fetchall())
                else:
                    rows = []
                conn.commit()
                return rows
        except Exception as exc:
            logger.warning("DB query failed [%s]: %s", exc.__class__.__name__, exc)
            try:
                conn.rollback()
            except Exception:
                pass
            return []

    def _execute_values(
        self,
        sql: str,
        params_list: list[tuple[Any, ...]],
        *,
        fetch: bool = False,
    ) -> list[dict[str, Any]]:
        """Execute a parameterized query with multiple value sets.

        Uses ``psycopg2.extras.execute_values`` for efficient batch inserts.
        Returns fetched rows if fetch=True, otherwise [].
        """
        conn = self._ensure_connection()
        if conn is None:
            return []
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                psycopg2.extras.execute_values(cur, sql, params_list)
                rows = list(cur.fetchall()) if fetch and cur.description else []
                conn.commit()
                return rows
        except Exception as exc:
            logger.warning("DB batch query failed [%s]: %s", exc.__class__.__name__, exc)
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

    # ══════════════════════════════════════════════════════════════════════
    # 1. books
    # ══════════════════════════════════════════════════════════════════════

    def upsert_book(
        self,
        book_id: str,
        book_name: str,
        *,
        total_episodes: int | None = None,
        source_type: str = "vlm_only",
        overall_synopsis: str | None = None,
        genre: str | None = None,
        sub_genre: str | None = None,
        mood: str | None = None,
        era: str | None = None,
        language: str = "zh",
        tags: list[str] | None = None,
        script_parsed: dict[str, Any] | None = None,
        script_sha: str | None = None,
        script_raw_path: str | None = None,
    ) -> int:
        """UPSERT a book record. Returns 1 on success, 0 on failure/no-op."""
        if not self.is_available:
            return 0

        table = _t(self._schema, "books")
        sql = f"""
            INSERT INTO {table} (
                book_id, book_name, total_episodes, source_type,
                overall_synopsis, genre, sub_genre, mood, era,
                language, tags, script_parsed, script_sha, script_raw_path
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            ON CONFLICT (book_id) DO UPDATE SET
                book_name = EXCLUDED.book_name,
                total_episodes = COALESCE(EXCLUDED.total_episodes, books.total_episodes),
                source_type = EXCLUDED.source_type,
                overall_synopsis = COALESCE(EXCLUDED.overall_synopsis, books.overall_synopsis),
                genre = COALESCE(EXCLUDED.genre, books.genre),
                sub_genre = COALESCE(EXCLUDED.sub_genre, books.sub_genre),
                mood = COALESCE(EXCLUDED.mood, books.mood),
                era = COALESCE(EXCLUDED.era, books.era),
                language = COALESCE(EXCLUDED.language, books.language),
                tags = COALESCE(EXCLUDED.tags, books.tags),
                script_parsed = COALESCE(EXCLUDED.script_parsed, books.script_parsed),
                script_sha = COALESCE(EXCLUDED.script_sha, books.script_sha),
                script_raw_path = COALESCE(EXCLUDED.script_raw_path, books.script_raw_path),
                updated_at = now()
        """
        import json

        tags_json = json.dumps(tags or [], ensure_ascii=False)
        script_parsed_json = (
            json.dumps(script_parsed, ensure_ascii=False) if script_parsed else None
        )

        self._execute(
            sql,
            (
                book_id,
                book_name,
                total_episodes,
                source_type,
                overall_synopsis,
                genre,
                sub_genre,
                mood,
                era,
                language,
                tags_json,
                script_parsed_json,
                script_sha,
                script_raw_path,
            ),
        )
        return 1

    def query_book(self, book_id: str) -> dict[str, Any] | None:
        """Query a single book by ID. Returns None if not found or unavailable."""
        if not self.is_available:
            return None
        table = _t(self._schema, "books")
        rows = self._execute(
            f"SELECT * FROM {table} WHERE book_id = %s", (book_id,)
        )
        return dict(rows[0]) if rows else None

    def update_book(self, book_id: str, **updates: Any) -> int:
        """Update specific fields on a book. Returns 1 on success, 0 otherwise."""
        if not self.is_available or not updates:
            return 0
        table = _t(self._schema, "books")
        set_clauses = [f"{_safe_ident(k)} = %s" for k in updates]
        params = list(updates.values()) + [book_id]
        sql = f"UPDATE {table} SET {', '.join(set_clauses)}, updated_at = now() WHERE book_id = %s"
        self._execute(sql, tuple(params))
        return 1

    # ══════════════════════════════════════════════════════════════════════
    # 2. subjects
    # ══════════════════════════════════════════════════════════════════════

    def upsert_subjects(
        self, book_id: str, subjects: list[dict[str, Any]], source: str = "vlm"
    ) -> dict[str, int]:
        """UPSERT multiple subjects with multi-source evidence tracking.
        
        Returns ``{name: subject_id}`` mapping.

        Each subject dict may contain: name, aliases, persona, personality,
        traits, tone, voice_timbre, visual_features, relationship, role,
        first_episode, last_episode, vlm_verified.
        
        Args:
            book_id: Book identifier
            subjects: List of subject dicts
            source: Data source identifier ('api', 'script', 'vlm', 'asr', 'manual')
        """
        if not self.is_available or not subjects:
            return {}

        table = _t(self._schema, "subjects")
        import json

        name_to_id: dict[str, int] = {}
        for subj in subjects:
            name = subj.get("name")
            if not name:
                continue
            
            # Build evidence for this source
            evidence_data = {
                "persona": subj.get("persona"),
                "personality": subj.get("personality", []),
                "traits": subj.get("traits"),
                "tone": subj.get("tone"),
                "voice_timbre": subj.get("voice_timbre"),
                "visual_features": subj.get("visual_features"),
                "relationship": subj.get("relationship"),
                "role": subj.get("role"),
            }
            # Filter out None/empty values
            evidence_data = {k: v for k, v in evidence_data.items() if v}
            evidence_json = json.dumps({source: evidence_data}, ensure_ascii=False)
            
            # Only generate JSON for non-empty arrays, otherwise NULL to preserve existing values
            aliases_val = subj.get("aliases", [])
            aliases_json = json.dumps(aliases_val, ensure_ascii=False) if aliases_val else None
            personality_val = subj.get("personality", [])
            personality_json = json.dumps(personality_val, ensure_ascii=False) if personality_val else None

            sql = f"""
                INSERT INTO {table} (
                    book_id, name, aliases, persona, personality, traits, tone,
                    voice_timbre, visual_features, relationship, role,
                    first_episode, last_episode, source, vlm_verified, sources_evidence
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
                )
                ON CONFLICT (book_id, name) DO UPDATE SET
                    aliases = COALESCE(EXCLUDED.aliases, subjects.aliases),
                    persona = COALESCE(NULLIF(EXCLUDED.persona, ''), subjects.persona),
                    personality = COALESCE(EXCLUDED.personality, subjects.personality),
                    traits = COALESCE(NULLIF(EXCLUDED.traits, ''), subjects.traits),
                    tone = COALESCE(NULLIF(EXCLUDED.tone, ''), subjects.tone),
                    voice_timbre = COALESCE(NULLIF(EXCLUDED.voice_timbre, ''), subjects.voice_timbre),
                    visual_features = COALESCE(NULLIF(EXCLUDED.visual_features, ''), subjects.visual_features),
                    relationship = COALESCE(NULLIF(EXCLUDED.relationship, ''), subjects.relationship),
                    role = COALESCE(NULLIF(EXCLUDED.role, ''), subjects.role),
                    first_episode = COALESCE(EXCLUDED.first_episode, subjects.first_episode),
                    last_episode = COALESCE(EXCLUDED.last_episode, subjects.last_episode),
                    source = subjects.source,
                    vlm_verified = COALESCE(EXCLUDED.vlm_verified, subjects.vlm_verified),
                    sources_evidence = subjects.sources_evidence || EXCLUDED.sources_evidence
                RETURNING id, name
            """
            rows = self._execute(
                sql,
                (
                    book_id,
                    name,
                    aliases_json,
                    subj.get("persona"),
                    personality_json,
                    subj.get("traits"),
                    subj.get("tone"),
                    subj.get("voice_timbre"),
                    subj.get("visual_features"),
                    subj.get("relationship"),
                    subj.get("role"),
                    subj.get("first_episode"),
                    subj.get("last_episode"),
                    source,
                    subj.get("vlm_verified", False),
                    evidence_json,
                ),
            )
            for row in rows:
                name_to_id[row["name"]] = row["id"]

        return name_to_id

    def upsert_subject(
        self,
        book_id: str,
        name: str,
        *,
        aliases: list[str] | None = None,
        persona: str | None = None,
        personality: list[str] | None = None,
        traits: str | None = None,
        tone: str | None = None,
        voice_timbre: str | None = None,
        visual_features: str | None = None,
        relationship: str | None = None,
        role: str | None = None,
        first_episode: int | None = None,
        last_episode: int | None = None,
        source: str = "vlm",
        vlm_verified: bool = False,
    ) -> int | None:
        """UPSERT a single subject. Returns subject id or None."""
        result = self.upsert_subjects(
            book_id,
            [
                {
                    "name": name,
                    "aliases": aliases or [],
                    "persona": persona,
                    "personality": personality or [],
                    "traits": traits,
                    "tone": tone,
                    "voice_timbre": voice_timbre,
                    "visual_features": visual_features,
                    "relationship": relationship,
                    "role": role,
                    "first_episode": first_episode,
                    "last_episode": last_episode,
                    "source": source,
                    "vlm_verified": vlm_verified,
                }
            ],
        )
        return result.get(name)

    def query_subjects(
        self, book_id: str, names: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Query subjects for a book, optionally filtered by name list."""
        if not self.is_available:
            return []
        table = _t(self._schema, "subjects")
        if names:
            sql = f"SELECT * FROM {table} WHERE book_id = %s AND name = ANY(%s)"
            rows = self._execute(sql, (book_id, names))
        else:
            sql = f"SELECT * FROM {table} WHERE book_id = %s"
            rows = self._execute(sql, (book_id,))
        return [dict(r) for r in rows]

    def update_subject(self, subject_id: int, **updates: Any) -> int:
        """Update specific fields on a subject. Returns 1 on success."""
        if not self.is_available or not updates:
            return 0
        table = _t(self._schema, "subjects")
        set_clauses = [f"{_safe_ident(k)} = %s" for k in updates]
        params = list(updates.values()) + [subject_id]
        sql = f"UPDATE {table} SET {', '.join(set_clauses)} WHERE id = %s"
        self._execute(sql, tuple(params))
        return 1

    def resolve_subject_id(self, book_id: str, name: str) -> int | None:
        """Resolve a subject name to its ID via three-tier matching.

        1. Exact match on ``subjects.name``
        2. Alias match in ``subjects.aliases`` JSONB array
        3. Normalized match (strip parens, lowercase)

        Returns None if no match found.
        """
        if not self.is_available or not name:
            return None
        table = _t(self._schema, "subjects")

        # Tier 1: exact match
        rows = self._execute(
            f"SELECT id FROM {table} WHERE book_id = %s AND name = %s",
            (book_id, name),
        )
        if rows:
            return rows[0]["id"]

        # Tier 2: alias match
        rows = self._execute(
            f"SELECT id, name FROM {table} WHERE book_id = %s AND aliases @> %s::jsonb",
            (book_id, f'["{name}"]'),
        )
        if rows:
            return rows[0]["id"]

        # Tier 3: normalized match
        normalized = _normalize_name(name)
        rows = self._execute(
            f"SELECT id, name FROM {table} WHERE book_id = %s", (book_id,)
        )
        for row in rows:
            if _normalize_name(row["name"]) == normalized:
                return row["id"]

        return None

    # ══════════════════════════════════════════════════════════════════════
    # 3. relationships
    # ══════════════════════════════════════════════════════════════════════

    def upsert_relationships(
        self, book_id: str, relationships: list[dict[str, Any]], source: str = "api"
    ) -> int:
        """UPSERT multiple relationships with multi-source evidence tracking.
        
        Returns count of upserted rows.

        Each relationship dict must contain: source_subject_id, target_subject_id.
        Optional: description.
        
        Args:
            book_id: Book identifier
            relationships: List of relationship dicts
            source: Data source identifier ('api', 'script', 'vlm', 'asr', 'manual')
        """
        if not self.is_available or not relationships:
            return 0

        import json
        table = _t(self._schema, "relationships")
        count = 0
        for rel in relationships:
            source_id = rel.get("source_subject_id")
            target_id = rel.get("target_subject_id")
            if source_id is None or target_id is None:
                continue
            if source_id == target_id:
                continue  # no self-relations
            
            # Build evidence for this source
            evidence_data = {
                "description": rel.get("description"),
            }
            # Filter out None/empty values
            evidence_data = {k: v for k, v in evidence_data.items() if v}
            evidence_json = json.dumps({source: evidence_data}, ensure_ascii=False)

            sql = f"""
                INSERT INTO {table} (
                    book_id, source_subject_id, target_subject_id, description, source, sources_evidence
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT DO NOTHING
            """
            self._execute(
                sql,
                (
                    book_id,
                    source_id,
                    target_id,
                    rel.get("description"),
                    source,
                    evidence_json,
                ),
            )
            count += 1

        return count

    def query_relationships(
        self, book_id: str, subject_names: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Query relationships for a book, optionally filtered by involved subjects."""
        if not self.is_available:
            return []
        s = self._schema
        if subject_names:
            sql = f"""
                SELECT r.*,
                       s1.name AS source_name, s2.name AS target_name
                FROM {_t(s, 'relationships')} r
                JOIN {_t(s, 'subjects')} s1 ON r.source_subject_id = s1.id
                JOIN {_t(s, 'subjects')} s2 ON r.target_subject_id = s2.id
                WHERE r.book_id = %s
                  AND (s1.name = ANY(%s) OR s2.name = ANY(%s))
            """
            rows = self._execute(sql, (book_id, subject_names, subject_names))
        else:
            sql = f"""
                SELECT r.*,
                       s1.name AS source_name, s2.name AS target_name
                FROM {_t(s, 'relationships')} r
                JOIN {_t(s, 'subjects')} s1 ON r.source_subject_id = s1.id
                JOIN {_t(s, 'subjects')} s2 ON r.target_subject_id = s2.id
                WHERE r.book_id = %s
            """
            rows = self._execute(sql, (book_id,))
        return [dict(r) for r in rows]

    # ══════════════════════════════════════════════════════════════════════
    # 4. episodes
    # ══════════════════════════════════════════════════════════════════════

    def upsert_episodes(
        self, book_id: str, episodes: list[dict[str, Any]], source: str = "vlm"
    ) -> int:
        """UPSERT multiple episodes with multi-source evidence tracking.
        
        Returns count of upserted rows.

        Each episode dict may contain: episode_id, chapter_id, title, summary,
        is_free, scene_count, duration, vlm_verified.
        
        Args:
            book_id: Book identifier
            episodes: List of episode dicts
            source: Data source identifier ('api', 'script', 'vlm', 'asr', 'manual')
        """
        if not self.is_available or not episodes:
            return 0

        import json
        table = _t(self._schema, "episodes")
        count = 0
        for ep in episodes:
            episode_id = ep.get("episode_id")
            if episode_id is None:
                continue
            
            # Build evidence for this source
            evidence_data = {
                "title": ep.get("title"),
                "summary": ep.get("summary"),
                "scene_count": ep.get("scene_count"),
                "duration": ep.get("duration"),
            }
            # Filter out None/empty values
            evidence_data = {k: v for k, v in evidence_data.items() if v is not None}
            evidence_json = json.dumps({source: evidence_data}, ensure_ascii=False)
            
            sql = f"""
                INSERT INTO {table} (
                    book_id, episode_id, chapter_id, title, summary,
                    is_free, scene_count, duration, source, vlm_verified, sources_evidence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (book_id, episode_id) DO UPDATE SET
                    chapter_id = COALESCE(EXCLUDED.chapter_id, episodes.chapter_id),
                    title = COALESCE(NULLIF(EXCLUDED.title, ''), episodes.title),
                    summary = COALESCE(NULLIF(EXCLUDED.summary, ''), episodes.summary),
                    is_free = COALESCE(EXCLUDED.is_free, episodes.is_free),
                    scene_count = COALESCE(EXCLUDED.scene_count, episodes.scene_count),
                    duration = COALESCE(EXCLUDED.duration, episodes.duration),
                    source = episodes.source,
                    vlm_verified = COALESCE(EXCLUDED.vlm_verified, episodes.vlm_verified),
                    sources_evidence = episodes.sources_evidence || EXCLUDED.sources_evidence
            """
            self._execute(
                sql,
                (
                    book_id,
                    episode_id,
                    ep.get("chapter_id"),
                    ep.get("title"),
                    ep.get("summary"),
                    ep.get("is_free", True),
                    ep.get("scene_count"),
                    ep.get("duration"),
                    source,
                    ep.get("vlm_verified", False),
                    evidence_json,
                ),
            )
            count += 1
        return count

    def query_episodes(
        self, book_id: str, *, is_free: bool | None = None
    ) -> list[dict[str, Any]]:
        """Query episodes for a book, optionally filtered by is_free."""
        if not self.is_available:
            return []
        table = _t(self._schema, "episodes")
        if is_free is not None:
            sql = f"SELECT * FROM {table} WHERE book_id = %s AND is_free = %s ORDER BY episode_id"
            rows = self._execute(sql, (book_id, is_free))
        else:
            sql = f"SELECT * FROM {table} WHERE book_id = %s ORDER BY episode_id"
            rows = self._execute(sql, (book_id,))
        return [dict(r) for r in rows]

    # ══════════════════════════════════════════════════════════════════════
    # 5. subtitles
    # ══════════════════════════════════════════════════════════════════════

    def insert_subtitles(
        self,
        book_id: str,
        episode_id: int,
        segments: list[dict[str, Any]],
        *,
        source: str = "api",
    ) -> int:
        """UPSERT subtitle segments with multi-source tracking.
        
        Returns count of upserted rows.
        
        The subtitles table has per-source text columns (asr_text, api_text, script_text).
        This method writes to the column matching the source parameter.
        On conflict (same book_id + episode_id + start_time), it merges rather than duplicates.

        Each segment dict: start_time, end_time, text (required).
        Optional: speaker, tone, emotion, group_id, group_tone, confidence, cer_estimate.
        """
        if not self.is_available or not segments:
            return 0

        table = _t(self._schema, "subtitles")
        count = 0
        for seg in segments:
            if "start_time" not in seg or "end_time" not in seg or "text" not in seg:
                continue
            
            # Determine which source column to write to
            asr_text = seg["text"] if source == "asr" else None
            api_text = seg["text"] if source == "api" else None
            script_text = seg["text"] if source == "script" else None

            sql = f"""
                INSERT INTO {table} (
                    book_id, episode_id, start_time, end_time, speaker,
                    text, tone, emotion, group_id, group_tone, source,
                    confidence, cer_estimate,
                    asr_text, api_text, script_text
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT (book_id, episode_id, start_time) DO UPDATE SET
                    end_time = COALESCE(EXCLUDED.end_time, subtitles.end_time),
                    speaker = COALESCE(EXCLUDED.speaker, subtitles.speaker),
                    text = CASE 
                        WHEN subtitles.text IS NULL OR subtitles.text = '' 
                        THEN EXCLUDED.text 
                        ELSE subtitles.text 
                    END,
                    tone = COALESCE(EXCLUDED.tone, subtitles.tone),
                    emotion = COALESCE(EXCLUDED.emotion, subtitles.emotion),
                    group_id = COALESCE(EXCLUDED.group_id, subtitles.group_id),
                    group_tone = COALESCE(EXCLUDED.group_tone, subtitles.group_tone),
                    confidence = COALESCE(EXCLUDED.confidence, subtitles.confidence),
                    cer_estimate = COALESCE(EXCLUDED.cer_estimate, subtitles.cer_estimate),
                    asr_text = COALESCE(EXCLUDED.asr_text, subtitles.asr_text),
                    api_text = COALESCE(EXCLUDED.api_text, subtitles.api_text),
                    script_text = COALESCE(EXCLUDED.script_text, subtitles.script_text)
            """
            self._execute(
                sql,
                (
                    book_id,
                    episode_id,
                    seg["start_time"],
                    seg["end_time"],
                    seg.get("speaker"),
                    seg["text"],
                    seg.get("tone"),
                    seg.get("emotion"),
                    seg.get("group_id"),
                    seg.get("group_tone"),
                    source,
                    seg.get("confidence"),
                    seg.get("cer_estimate"),
                    asr_text,
                    api_text,
                    script_text,
                ),
            )
            count += 1
        return count

    def query_subtitles(
        self,
        book_id: str,
        episode_id: int,
        *,
        start_time: float | None = None,
        end_time: float | None = None,
        source: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query subtitles with optional time range and source filter."""
        if not self.is_available:
            return []
        table = _t(self._schema, "subtitles")
        conditions = ["book_id = %s", "episode_id = %s"]
        params: list[Any] = [book_id, episode_id]

        if start_time is not None and end_time is not None:
            conditions.append("start_time <= %s AND end_time >= %s")
            params.extend([end_time, start_time])
        elif start_time is not None:
            conditions.append("start_time >= %s")
            params.append(start_time)
        elif end_time is not None:
            conditions.append("end_time <= %s")
            params.append(end_time)

        if source is not None:
            conditions.append("source = %s")
            params.append(source)

        sql = f"SELECT * FROM {table} WHERE {' AND '.join(conditions)} ORDER BY start_time"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"

        rows = self._execute(sql, tuple(params))
        return [dict(r) for r in rows]

    # ══════════════════════════════════════════════════════════════════════
    # 6. speaker_mappings
    # ══════════════════════════════════════════════════════════════════════

    def upsert_speaker_mappings(
        self,
        book_id: str,
        episode_id: int,
        mappings: list[dict[str, Any]],
    ) -> int:
        """UPSERT speaker mappings (speaker_label → mapped_subject_id).

        Each mapping dict: speaker_label (required).
        Optional: mapped_subject_id, confidence, resolved_by.
        """
        if not self.is_available or not mappings:
            return 0

        table = _t(self._schema, "speaker_mappings")
        count = 0
        for m in mappings:
            speaker_label = m.get("speaker_label")
            if not speaker_label:
                continue
            sql = f"""
                INSERT INTO {table} (
                    book_id, episode_id, speaker_label, mapped_subject_id,
                    confidence, resolved_by
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (book_id, episode_id, speaker_label) DO UPDATE SET
                    mapped_subject_id = COALESCE(
                        EXCLUDED.mapped_subject_id, speaker_mappings.mapped_subject_id
                    ),
                    confidence = COALESCE(
                        EXCLUDED.confidence, speaker_mappings.confidence
                    ),
                    resolved_by = COALESCE(
                        EXCLUDED.resolved_by, speaker_mappings.resolved_by
                    ),
                    resolved_at = CASE
                        WHEN EXCLUDED.mapped_subject_id IS NOT NULL
                        THEN now()
                        ELSE speaker_mappings.resolved_at
                    END
            """
            self._execute(
                sql,
                (
                    book_id,
                    episode_id,
                    speaker_label,
                    m.get("mapped_subject_id"),
                    m.get("confidence", 0.0),
                    m.get("resolved_by"),
                ),
            )
            count += 1
        return count

    def update_speaker_mapping(
        self,
        mapping_id: int,
        *,
        mapped_subject_id: int | None = None,
        confidence: float | None = None,
        resolved_by: str | None = None,
    ) -> int:
        """Update a single speaker mapping. Returns 1 on success."""
        if not self.is_available:
            return 0
        table = _t(self._schema, "speaker_mappings")
        updates: dict[str, Any] = {}
        if mapped_subject_id is not None:
            updates["mapped_subject_id"] = mapped_subject_id
        if confidence is not None:
            updates["confidence"] = confidence
        if resolved_by is not None:
            updates["resolved_by"] = resolved_by
        if mapped_subject_id is not None:
            updates["resolved_at"] = "now()"

        if not updates:
            return 0

        set_parts = []
        params: list[Any] = []
        for k, v in updates.items():
            if v == "now()":
                set_parts.append(f"{_safe_ident(k)} = now()")
            else:
                set_parts.append(f"{_safe_ident(k)} = %s")
                params.append(v)
        params.append(mapping_id)

        sql = f"UPDATE {table} SET {', '.join(set_parts)} WHERE id = %s"
        self._execute(sql, tuple(params))
        return 1

    # ══════════════════════════════════════════════════════════════════════
    # 7. subject_episodes
    # ══════════════════════════════════════════════════════════════════════

    def upsert_subject_episodes(
        self,
        book_id: str,
        entries: list[dict[str, Any]],
    ) -> int:
        """UPSERT subject-episode linkage records.

        Each entry dict: subject_id, episode_id (required).
        Optional: relationship, visual_features, appears_in_episode, source.
        """
        if not self.is_available or not entries:
            return 0

        table = _t(self._schema, "subject_episodes")
        count = 0
        for entry in entries:
            subject_id = entry.get("subject_id")
            episode_id = entry.get("episode_id")
            if subject_id is None or episode_id is None:
                continue
            sql = f"""
                INSERT INTO {table} (
                    subject_id, book_id, episode_id, relationship,
                    visual_features, appears_in_episode, source
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (subject_id, episode_id) DO UPDATE SET
                    relationship = COALESCE(
                        EXCLUDED.relationship, subject_episodes.relationship
                    ),
                    visual_features = COALESCE(
                        EXCLUDED.visual_features, subject_episodes.visual_features
                    ),
                    appears_in_episode = COALESCE(
                        EXCLUDED.appears_in_episode, subject_episodes.appears_in_episode
                    ),
                    source = EXCLUDED.source
            """
            self._execute(
                sql,
                (
                    subject_id,
                    book_id,
                    episode_id,
                    entry.get("relationship"),
                    entry.get("visual_features"),
                    entry.get("appears_in_episode", True),
                    entry.get("source", "api"),
                ),
            )
            count += 1
        return count

    # ══════════════════════════════════════════════════════════════════════
    # 8. shots
    # ══════════════════════════════════════════════════════════════════════

    def insert_shots(
        self,
        book_id: str,
        episode_id: int,
        shots: list[dict[str, Any]],
    ) -> int:
        """INSERT shot records. Returns count of inserted rows.

        Each shot dict: start_time, end_time (required).
        Optional: scene, subjects, actions, is_highlight, highlight_score,
        highlight_reason, related_srt_range, source.
        """
        if not self.is_available or not shots:
            return 0

        table = _t(self._schema, "shots")
        import json

        params_list: list[tuple[Any, ...]] = []
        for shot in shots:
            if "start_time" not in shot or "end_time" not in shot:
                continue
            subjects_json = json.dumps(
                shot.get("subjects", []), ensure_ascii=False
            )
            params_list.append(
                (
                    book_id,
                    episode_id,
                    shot["start_time"],
                    shot["end_time"],
                    shot.get("scene"),
                    subjects_json,
                    shot.get("actions"),
                    shot.get("is_highlight", False),
                    shot.get("highlight_score"),
                    shot.get("highlight_reason"),
                    shot.get("related_srt_range"),
                    shot.get("source", "api"),
                )
            )

        if not params_list:
            return 0

        sql = f"""
            INSERT INTO {table} (
                book_id, episode_id, start_time, end_time, scene,
                subjects, actions, is_highlight, highlight_score,
                highlight_reason, related_srt_range, source
            ) VALUES %s
        """
        self._execute_values(sql, params_list)
        return len(params_list)

    def query_shots(
        self,
        book_id: str,
        episode_id: int,
        *,
        start_time: float | None = None,
        end_time: float | None = None,
    ) -> list[dict[str, Any]]:
        """Query shots with optional time range."""
        if not self.is_available:
            return []
        table = _t(self._schema, "shots")
        conditions = ["book_id = %s", "episode_id = %s"]
        params: list[Any] = [book_id, episode_id]

        if start_time is not None and end_time is not None:
            conditions.append("start_time <= %s AND end_time >= %s")
            params.extend([end_time, start_time])
        elif start_time is not None:
            conditions.append("start_time >= %s")
            params.append(start_time)
        elif end_time is not None:
            conditions.append("end_time <= %s")
            params.append(end_time)

        sql = f"SELECT * FROM {table} WHERE {' AND '.join(conditions)} ORDER BY start_time"
        rows = self._execute(sql, tuple(params))
        return [dict(r) for r in rows]

    # ══════════════════════════════════════════════════════════════════════
    # 9. scenes
    # ══════════════════════════════════════════════════════════════════════

    def upsert_scenes(
        self, book_id: str, scenes: list[dict[str, Any]]
    ) -> int:
        """UPSERT multiple scenes. Returns count of upserted rows.

        Each scene dict: scene_id (required).
        Optional: episode_id, scene_order, heading, location, time_of_day,
        is_flashback, flashback_label, characters_present, dialogues,
        raw_description, distilled_summary, meta_tags, start_time, end_time,
        alignment_confidence, alignment_source, source, detected_in_video,
        vlm_verified.
        """
        if not self.is_available or not scenes:
            return 0

        table = _t(self._schema, "scenes")
        import json

        count = 0
        for sc in scenes:
            scene_id = sc.get("scene_id")
            if not scene_id:
                continue
            characters_present = sc.get("characters_present", [])
            if isinstance(characters_present, list):
                characters_present = characters_present
            dialogues_json = json.dumps(
                sc.get("dialogues", []), ensure_ascii=False
            )
            meta_tags_json = json.dumps(
                sc.get("meta_tags", {}), ensure_ascii=False
            )

            sql = f"""
                INSERT INTO {table} (
                    scene_id, book_id, episode_id, scene_order, heading,
                    location, time_of_day, is_flashback, flashback_label,
                    characters_present, dialogues, raw_description,
                    distilled_summary, meta_tags, start_time, end_time,
                    alignment_confidence, alignment_source, source,
                    detected_in_video, vlm_verified
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (book_id, scene_id) DO UPDATE SET
                    episode_id = COALESCE(EXCLUDED.episode_id, scenes.episode_id),
                    scene_order = COALESCE(EXCLUDED.scene_order, scenes.scene_order),
                    heading = COALESCE(EXCLUDED.heading, scenes.heading),
                    location = COALESCE(EXCLUDED.location, scenes.location),
                    time_of_day = COALESCE(EXCLUDED.time_of_day, scenes.time_of_day),
                    is_flashback = COALESCE(
                        EXCLUDED.is_flashback, scenes.is_flashback
                    ),
                    flashback_label = COALESCE(
                        EXCLUDED.flashback_label, scenes.flashback_label
                    ),
                    characters_present = COALESCE(
                        EXCLUDED.characters_present, scenes.characters_present
                    ),
                    dialogues = COALESCE(EXCLUDED.dialogues, scenes.dialogues),
                    raw_description = COALESCE(
                        EXCLUDED.raw_description, scenes.raw_description
                    ),
                    distilled_summary = COALESCE(
                        EXCLUDED.distilled_summary, scenes.distilled_summary
                    ),
                    meta_tags = COALESCE(EXCLUDED.meta_tags, scenes.meta_tags),
                    start_time = COALESCE(EXCLUDED.start_time, scenes.start_time),
                    end_time = COALESCE(EXCLUDED.end_time, scenes.end_time),
                    alignment_confidence = COALESCE(
                        EXCLUDED.alignment_confidence, scenes.alignment_confidence
                    ),
                    alignment_source = COALESCE(
                        EXCLUDED.alignment_source, scenes.alignment_source
                    ),
                    source = EXCLUDED.source,
                    detected_in_video = COALESCE(
                        EXCLUDED.detected_in_video, scenes.detected_in_video
                    ),
                    vlm_verified = COALESCE(
                        EXCLUDED.vlm_verified, scenes.vlm_verified
                    ),
                    vlm_verified_at = CASE
                        WHEN EXCLUDED.vlm_verified = true
                        THEN now()
                        ELSE scenes.vlm_verified_at
                    END
            """
            self._execute(
                sql,
                (
                    scene_id,
                    book_id,
                    sc.get("episode_id"),
                    sc.get("scene_order"),
                    sc.get("heading"),
                    sc.get("location"),
                    sc.get("time_of_day"),
                    sc.get("is_flashback", False),
                    sc.get("flashback_label"),
                    characters_present,
                    dialogues_json,
                    sc.get("raw_description"),
                    sc.get("distilled_summary"),
                    meta_tags_json,
                    sc.get("start_time"),
                    sc.get("end_time"),
                    sc.get("alignment_confidence"),
                    sc.get("alignment_source"),
                    sc.get("source", "vlm"),
                    sc.get("detected_in_video", False),
                    sc.get("vlm_verified", False),
                ),
            )
            count += 1
        return count

    def query_scenes(
        self,
        book_id: str,
        episode_id: int | None = None,
        *,
        start_time: float | None = None,
        end_time: float | None = None,
    ) -> list[dict[str, Any]]:
        """Query scenes with optional episode and time range filters."""
        if not self.is_available:
            return []
        table = _t(self._schema, "scenes")
        conditions = ["book_id = %s"]
        params: list[Any] = [book_id]

        if episode_id is not None:
            conditions.append("episode_id = %s")
            params.append(episode_id)

        if start_time is not None and end_time is not None:
            conditions.append("start_time <= %s AND end_time >= %s")
            params.extend([end_time, start_time])

        sql = f"SELECT * FROM {table} WHERE {' AND '.join(conditions)} ORDER BY scene_order"
        rows = self._execute(sql, tuple(params))
        return [dict(r) for r in rows]

    def update_scene(self, book_id: str, scene_id: str, **updates: Any) -> int:
        """Update specific fields on a scene. Returns 1 on success."""
        if not self.is_available or not updates:
            return 0
        table = _t(self._schema, "scenes")
        set_clauses = [f"{_safe_ident(k)} = %s" for k in updates]
        params = list(updates.values()) + [book_id, scene_id]
        sql = f"UPDATE {table} SET {', '.join(set_clauses)} WHERE book_id = %s AND scene_id = %s"
        self._execute(sql, tuple(params))
        return 1

    # ══════════════════════════════════════════════════════════════════════
    # 10. boundaries
    # ══════════════════════════════════════════════════════════════════════

    def insert_boundaries(
        self,
        book_id: str,
        boundaries: list[dict[str, Any]],
    ) -> int:
        """INSERT boundary records. Returns count of inserted rows.

        Each boundary dict: boundary_id, episode_id, event_type, start_time,
        end_time, source_table (required).
        Optional: description, subjects, source_id, confidence, precision.
        """
        if not self.is_available or not boundaries:
            return 0

        table = _t(self._schema, "boundaries")
        import json

        count = 0
        for b in boundaries:
            required = [
                "boundary_id",
                "episode_id",
                "event_type",
                "start_time",
                "end_time",
                "source_table",
            ]
            if any(b.get(k) is None for k in required):
                continue

            subjects_json = json.dumps(b.get("subjects", []), ensure_ascii=False)

            sql = f"""
                INSERT INTO {table} (
                    boundary_id, book_id, episode_id, event_type,
                    start_time, end_time, description, subjects,
                    source_table, source_id, confidence, precision
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (book_id, boundary_id) DO NOTHING
            """
            self._execute(
                sql,
                (
                    b["boundary_id"],
                    book_id,
                    b["episode_id"],
                    b["event_type"],
                    b["start_time"],
                    b["end_time"],
                    b.get("description"),
                    subjects_json,
                    b["source_table"],
                    b.get("source_id"),
                    b.get("confidence", "low"),
                    b.get("precision", 2.0),
                ),
            )
            count += 1
        return count

    def query_boundaries(
        self,
        book_id: str,
        *,
        episode_id: int | None = None,
        event_types: list[str] | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query boundaries with optional filters."""
        if not self.is_available:
            return []
        table = _t(self._schema, "boundaries")
        conditions = ["book_id = %s"]
        params: list[Any] = [book_id]

        if episode_id is not None:
            conditions.append("episode_id = %s")
            params.append(episode_id)

        if event_types:
            conditions.append("event_type = ANY(%s)")
            params.append(event_types)

        if start_time is not None and end_time is not None:
            conditions.append("start_time >= %s AND end_time <= %s")
            params.extend([start_time, end_time])

        sql = f"SELECT * FROM {table} WHERE {' AND '.join(conditions)}"
        if order_by is not None:
            sql += f" ORDER BY {_safe_ident(order_by)}"
        else:
            sql += " ORDER BY start_time"

        rows = self._execute(sql, tuple(params))
        return [dict(r) for r in rows]

    def update_boundary(
        self, book_id: str, boundary_id: str, **updates: Any
    ) -> int:
        """Update specific fields on a boundary. Returns 1 on success."""
        if not self.is_available or not updates:
            return 0
        table = _t(self._schema, "boundaries")
        set_clauses = [f"{_safe_ident(k)} = %s" for k in updates]
        params = list(updates.values()) + [book_id, boundary_id]
        sql = (
            f"UPDATE {table} SET {', '.join(set_clauses)}, corrected_at = now() "
            f"WHERE book_id = %s AND boundary_id = %s"
        )
        self._execute(sql, tuple(params))
        return 1

    # ══════════════════════════════════════════════════════════════════════
    # Key composite queries
    # ══════════════════════════════════════════════════════════════════════

    def get_window_context(
        self,
        book_id: str,
        episode_id: int,
        window_start: float,
        window_end: float,
        *,
        subject_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get all context needed for VLM window analysis.

        Returns a dict with keys: episode_scenes, current_scenes, subtitles,
        shots, subjects, relationships.

        When DB is unavailable, returns an empty dict for each key.
        """
        if not self.is_available:
            return {
                "episode_scenes": [],
                "current_scenes": [],
                "subtitles": [],
                "shots": [],
                "subjects": [],
                "relationships": [],
            }

        s = self._schema

        episode_scenes = self._execute(
            f"""
                SELECT scene_order, scene_id, heading, characters_present,
                       distilled_summary, is_flashback
                FROM {_t(s, 'scenes')}
                WHERE book_id = %s AND episode_id = %s
                ORDER BY scene_order
            """,
            (book_id, episode_id),
        )

        current_scenes = self._execute(
            f"""
                SELECT scene_id, heading, raw_description, dialogues
                FROM {_t(s, 'scenes')}
                WHERE book_id = %s AND episode_id = %s
                  AND start_time <= %s AND end_time >= %s
            """,
            (book_id, episode_id, window_end, window_start),
        )

        subtitles = self._execute(
            f"""
                SELECT speaker, text, tone
                FROM {_t(s, 'subtitles')}
                WHERE book_id = %s AND episode_id = %s
                  AND start_time <= %s AND end_time >= %s
                ORDER BY start_time
                LIMIT 10
            """,
            (book_id, episode_id, window_end, window_start),
        )

        shots = self._execute(
            f"""
                SELECT scene, subjects, actions, is_highlight
                FROM {_t(s, 'shots')}
                WHERE book_id = %s AND episode_id = %s
                  AND start_time <= %s AND end_time >= %s
            """,
            (book_id, episode_id, window_end, window_start),
        )

        subjects: list[dict[str, Any]] = []
        if subject_names:
            subjects = self._execute(
                f"""
                    SELECT name, visual_features, persona, personality, traits, tone
                    FROM {_t(s, 'subjects')}
                    WHERE book_id = %s AND name = ANY(%s)
                """,
                (book_id, subject_names),
            )
        else:
            subjects = self._execute(
                f"""
                    SELECT name, visual_features, persona, personality, traits, tone
                    FROM {_t(s, 'subjects')}
                    WHERE book_id = %s
                """,
                (book_id,),
            )

        relationships: list[dict[str, Any]] = []
        if subject_names:
            relationships = self._execute(
                f"""
                    SELECT s1.name AS source_name, s2.name AS target_name, r.description
                    FROM {_t(s, 'relationships')} r
                    JOIN {_t(s, 'subjects')} s1 ON r.source_subject_id = s1.id
                    JOIN {_t(s, 'subjects')} s2 ON r.target_subject_id = s2.id
                    WHERE r.book_id = %s
                      AND (s1.name = ANY(%s) OR s2.name = ANY(%s))
                """,
                (book_id, subject_names, subject_names),
            )
        else:
            relationships = self._execute(
                f"""
                    SELECT s1.name AS source_name, s2.name AS target_name, r.description
                    FROM {_t(s, 'relationships')} r
                    JOIN {_t(s, 'subjects')} s1 ON r.source_subject_id = s1.id
                    JOIN {_t(s, 'subjects')} s2 ON r.target_subject_id = s2.id
                    WHERE r.book_id = %s
                """,
                (book_id,),
            )

        return {
            "episode_scenes": [dict(r) for r in episode_scenes],
            "current_scenes": [dict(r) for r in current_scenes],
            "subtitles": [dict(r) for r in subtitles],
            "shots": [dict(r) for r in shots],
            "subjects": [dict(r) for r in subjects],
            "relationships": [dict(r) for r in relationships],
        }

    def get_free_boundaries(
        self,
        book_id: str,
        *,
        episode_id: int | None = None,
        event_types: list[str] | None = None,
        target_start: float | None = None,
        target_end: float | None = None,
    ) -> list[dict[str, Any]]:
        """Query boundaries from free episodes only.

        Joins ``boundaries`` with ``episodes`` and filters ``is_free=true``.
        Results are ordered by confidence (high > medium > low) then precision.
        """
        if not self.is_available:
            return []

        s = self._schema
        default_types = ["highlight", "action", "dialogue"]
        types = event_types if event_types else default_types

        conditions = [
            "b.book_id = %s",
            "e.is_free = true",
            "b.event_type = ANY(%s)",
        ]
        params: list[Any] = [book_id, types]

        if episode_id is not None:
            conditions.append("b.episode_id = %s")
            params.append(episode_id)

        if target_start is not None and target_end is not None:
            conditions.append("b.start_time >= %s AND b.end_time <= %s")
            params.extend([target_start, target_end])

        sql = f"""
            SELECT b.boundary_id, b.event_type, b.description, b.subjects,
                   b.confidence, b.precision, b.episode_id, b.start_time, b.end_time
            FROM {_t(s, 'boundaries')} b
            JOIN {_t(s, 'episodes')} e
              ON b.book_id = e.book_id AND b.episode_id = e.episode_id
            WHERE {' AND '.join(conditions)}
            ORDER BY
              b.confidence = 'high' DESC,
              b.confidence = 'medium' DESC,
              b.precision ASC
        """

        rows = self._execute(sql, tuple(params))
        return [dict(r) for r in rows]

    def get_book_context(self, book_id: str) -> dict[str, Any]:
        """Load all context for a book in a single query (story generation).

        Returns a dict with keys: book, subjects, relationships, episodes,
        scenes, boundaries. When DB is unavailable, returns empty dicts.
        """
        if not self.is_available:
            return {
                "book": None,
                "subjects": [],
                "relationships": [],
                "episodes": [],
                "scenes": [],
                "boundaries": [],
            }

        s = self._schema

        book_rows = self._execute(
            f"SELECT * FROM {_t(s, 'books')} WHERE book_id = %s", (book_id,)
        )
        book = dict(book_rows[0]) if book_rows else None

        subjects = self._execute(
            f"SELECT * FROM {_t(s, 'subjects')} WHERE book_id = %s", (book_id,)
        )

        relationships = self._execute(
            f"""
                SELECT r.*, s1.name AS source_name, s2.name AS target_name
                FROM {_t(s, 'relationships')} r
                JOIN {_t(s, 'subjects')} s1 ON r.source_subject_id = s1.id
                JOIN {_t(s, 'subjects')} s2 ON r.target_subject_id = s2.id
                WHERE r.book_id = %s
            """,
            (book_id,),
        )

        episodes = self._execute(
            f"SELECT * FROM {_t(s, 'episodes')} WHERE book_id = %s ORDER BY episode_id",
            (book_id,),
        )

        scenes = self._execute(
            f"SELECT * FROM {_t(s, 'scenes')} WHERE book_id = %s ORDER BY episode_id, scene_order",
            (book_id,),
        )

        boundaries = self._execute(
            f"SELECT * FROM {_t(s, 'boundaries')} WHERE book_id = %s ORDER BY start_time",
            (book_id,),
        )

        return {
            "book": book,
            "subjects": [dict(r) for r in subjects],
            "relationships": [dict(r) for r in relationships],
            "episodes": [dict(r) for r in episodes],
            "scenes": [dict(r) for r in scenes],
            "boundaries": [dict(r) for r in boundaries],
        }

    # ══════════════════════════════════════════════════════════════════════
    # Convenience: apply_database_patch
    # ══════════════════════════════════════════════════════════════════════

    def apply_database_patch(
        self, book_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        """Apply a database_patch from window_analysis.

        The patch dict may contain:
          - scenes: list of scene dicts to upsert
          - subjects: list of subject dicts to upsert
          - boundaries: list of boundary dicts to insert
          - subject_updates: list of {subject_id, ...updates}

        Returns a summary dict with counts for each operation.
        """
        result: dict[str, Any] = {
            "scenes_upserted": 0,
            "subjects_upserted": 0,
            "boundaries_inserted": 0,
            "subjects_updated": 0,
        }

        if not self.is_available:
            return result

        if "scenes" in patch:
            result["scenes_upserted"] = self.upsert_scenes(book_id, patch["scenes"])

        if "subjects" in patch:
            name_to_id = self.upsert_subjects(book_id, patch["subjects"])
            result["subjects_upserted"] = len(name_to_id)

        if "boundaries" in patch:
            result["boundaries_inserted"] = self.insert_boundaries(
                book_id, patch["boundaries"]
            )

        if "subject_updates" in patch:
            for update in patch["subject_updates"]:
                subject_id = update.pop("subject_id", None)
                if subject_id is not None:
                    self.update_subject(subject_id, **update)
                    result["subjects_updated"] += 1

        return result

    # ══════════════════════════════════════════════════════════════════════
    # Reconciliation: apply multi-source fusion results
    # ══════════════════════════════════════════════════════════════════════

    def apply_reconciliation(
        self,
        book_id: str,
        subject_updates: list[dict[str, Any]] | None = None,
        episode_updates: list[dict[str, Any]] | None = None,
        relationship_updates: list[dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        """Apply reconciliation results to canonical fields.
        
        Args:
            book_id: Book identifier
            subject_updates: List of {id, field1, field2, ...} dicts
            episode_updates: List of {(book_id, episode_id), field1, field2, ...} tuples
            relationship_updates: List of {id, field1, field2, ...} dicts
        
        Returns:
            Dict with counts: subjects_updated, episodes_updated, relationships_updated
        """
        result = {
            "subjects_updated": 0,
            "episodes_updated": 0,
            "relationships_updated": 0,
        }
        
        if not self.is_available:
            return result
        
        # Update subjects
        if subject_updates:
            for update in subject_updates:
                subject_id = update.pop("id", None)
                if subject_id is not None:
                    self.update_subject(subject_id, **update)
                    result["subjects_updated"] += 1
        
        # Update episodes
        if episode_updates:
            for key, fields in episode_updates:
                book_id, episode_id = key
                set_clauses = [f"{k} = %s" for k in fields.keys()]
                values = list(fields.values())
                table = _t(self._schema, "episodes")
                sql = f"""
                    UPDATE {table}
                    SET {', '.join(set_clauses)}
                    WHERE book_id = %s AND episode_id = %s
                """
                self._execute(sql, tuple(values) + (book_id, episode_id))
                result["episodes_updated"] += 1
        
        # Update relationships
        if relationship_updates:
            for update in relationship_updates:
                rel_id = update.pop("id", None)
                if rel_id is not None:
                    set_clauses = [f"{k} = %s" for k in update.keys()]
                    values = list(update.values())
                    table = _t(self._schema, "relationships")
                    sql = f"""
                        UPDATE {table}
                        SET {', '.join(set_clauses)}
                        WHERE id = %s
                    """
                    self._execute(sql, tuple(values) + (rel_id,))
                    result["relationships_updated"] += 1
        
        return result


# ══════════════════════════════════════════════════════════════════════
    # 11. source_provenance
    # ══════════════════════════════════════════════════════════════════════

    def insert_provenance(self, records: list[dict[str, Any]]) -> int:
        """Insert provenance records using ON CONFLICT upsert.

        Each record dict must contain: entity_table, entity_id, field_path.
        Optional: values, canonical_source, resolved_at, resolved_by.

        Returns the number of records upserted (0 if unavailable).
        """
        if not self.is_available or not records:
            return 0

        table = _t(self._schema, "source_provenance")
        import json as _json

        count = 0
        for rec in records:
            entity_table = rec.get("entity_table")
            entity_id = rec.get("entity_id")
            field_path = rec.get("field_path")
            if not entity_table or not entity_id or not field_path:
                continue

            values_val = rec.get("values")
            if isinstance(values_val, (dict, list)):
                values_val = _json.dumps(values_val, ensure_ascii=False, default=str)
            elif isinstance(values_val, str):
                pass  # already JSON string
            else:
                values_val = "{}"

            sql = f"""
                INSERT INTO {table} (
                    entity_table, entity_id, field_path,
                    values, canonical_source, resolved_at, resolved_by
                ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
                ON CONFLICT (entity_table, entity_id, field_path) DO UPDATE SET
                    values = EXCLUDED.values,
                    canonical_source = EXCLUDED.canonical_source,
                    resolved_at = EXCLUDED.resolved_at,
                    resolved_by = EXCLUDED.resolved_by
            """
            self._execute(
                sql,
                (
                    entity_table,
                    entity_id,
                    field_path,
                    values_val,
                    rec.get("canonical_source", ""),
                    rec.get("resolved_at"),
                    rec.get("resolved_by", "auto_policy"),
                ),
            )
            count += 1

        return count

    # ══════════════════════════════════════════════════════════════════════
    # 12. source_conflicts
    # ══════════════════════════════════════════════════════════════════════

    def upsert_conflicts(self, conflicts: list[dict[str, Any]]) -> int:
        """UPSERT conflict records.

        Each conflict dict must contain: entity_table, entity_id, field_path.
        Optional: candidates, severity, status, resolution, created_at, resolved_at.

        On conflict, updates candidates, severity, and status (preserving any
        existing resolution).

        Returns the number of conflicts upserted (0 if unavailable).
        """
        if not self.is_available or not conflicts:
            return 0

        table = _t(self._schema, "source_conflicts")
        import json as _json

        count = 0
        for conf in conflicts:
            entity_table = conf.get("entity_table")
            entity_id = conf.get("entity_id")
            field_path = conf.get("field_path")
            if not entity_table or not entity_id or not field_path:
                continue

            candidates_val = conf.get("candidates")
            if isinstance(candidates_val, (dict, list)):
                candidates_val = _json.dumps(candidates_val, ensure_ascii=False, default=str)
            elif isinstance(candidates_val, str):
                pass
            else:
                candidates_val = "{}"

            resolution_val = conf.get("resolution")
            if isinstance(resolution_val, (dict, list)):
                resolution_val = _json.dumps(resolution_val, ensure_ascii=False, default=str)
            elif isinstance(resolution_val, str):
                pass
            else:
                resolution_val = None

            sql = f"""
                INSERT INTO {table} (
                    entity_table, entity_id, field_path,
                    candidates, severity, status, resolution,
                    created_at, resolved_at
                ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (entity_table, entity_id, field_path) DO UPDATE SET
                    candidates = EXCLUDED.candidates,
                    severity = EXCLUDED.severity,
                    status = EXCLUDED.status,
                    created_at = EXCLUDED.created_at
            """
            self._execute(
                sql,
                (
                    entity_table,
                    entity_id,
                    field_path,
                    candidates_val,
                    conf.get("severity", "low"),
                    conf.get("status", "pending"),
                    resolution_val,
                    conf.get("created_at"),
                    conf.get("resolved_at"),
                ),
            )
            count += 1

        return count

    def get_pending_conflicts(self, book_id: str) -> list[dict[str, Any]]:
        """Query all pending conflicts for a given book.

        Matches against entity_id in source_conflicts (which is the subject name
        for subject-level conflicts).  Returns rows ordered by severity (high
        first) and then by creation time.

        Args:
            book_id: Used to filter conflicts — entity_id is matched against
                     subject names belonging to this book via a subquery on
                     the subjects table.

        Returns:
            List of conflict dicts; empty list when DB is unavailable.
        """
        if not self.is_available:
            return []

        s = self._schema
        sql = f"""
            SELECT sc.*
            FROM {_t(s, 'source_conflicts')} sc
            WHERE sc.entity_table = 'subjects'
              AND sc.status = 'pending'
              AND sc.entity_id IN (
                  SELECT name FROM {_t(s, 'subjects')} WHERE book_id = %s
              )
            ORDER BY
              CASE sc.severity
                  WHEN 'high' THEN 1
                  WHEN 'medium' THEN 2
                  WHEN 'low' THEN 3
                  ELSE 4
              END,
              sc.created_at ASC
        """
        rows = self._execute(sql, (book_id,))
        return [dict(r) for r in rows]

    def resolve_conflict(self, conflict_id: int, resolution: dict[str, Any]) -> int:
        """Mark a conflict as resolved with a resolution payload.

        Sets status to 'resolved', records the resolution JSONB, and sets
        resolved_at to now().

        Args:
            conflict_id: The integer primary key of the conflict.
            resolution: Dict with at least a 'chosen_value' key and optionally
                        'chosen_source', 'reason', 'resolved_by'.

        Returns:
            1 on success, 0 on failure or if DB is unavailable.
        """
        if not self.is_available:
            return 0

        import json as _json

        resolution_json = _json.dumps(resolution, ensure_ascii=False, default=str)

        table = _t(self._schema, "source_conflicts")
        sql = f"""
            UPDATE {table}
            SET status = 'resolved',
                resolution = %s::jsonb,
                resolved_at = now()
            WHERE id = %s
        """
        self._execute(sql, (resolution_json, conflict_id))
        return 1


# ── Internal helpers ──────────────────────────────────────────────────────────


def _safe_ident(name: str) -> str:
    """Return a safe SQL identifier, rejecting obviously malicious input."""
    if not name or not isinstance(name, str):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    # Only allow alphanumeric + underscore
    if not name.replace("_", "").isalnum():
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def _normalize_name(name: str) -> str:
    """Normalize a subject name for fuzzy matching.

    Removes parentheses content, strips whitespace, lowercases.
    """
    import re

    result = re.sub(r"\([^)]*\)", "", name)
    result = result.strip().lower()
    return result
