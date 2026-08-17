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

    def __init__(self, db_url: str | None = None, schema: str = "autocut") -> None:
        self._db_url = db_url
        self._schema = schema
        self._conn: Any = None

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
        """Lazy-connect to PostgreSQL; returns None if unavailable.
        
        Also runs automatic schema initialization on first connection.
        """
        if not self.is_available:
            return None
        if self._conn is not None and not getattr(self._conn, "closed", True):
            return self._conn
        try:
            self._conn = psycopg2.connect(self._db_url)
            self._conn.autocommit = False
            # Auto-initialize schema on first connection
            self._auto_init_schema()
            return self._conn
        except Exception as exc:
            logger.warning("DB connection failed: %s", exc)
            self._conn = None
            return None

    def _auto_init_schema(self) -> None:
        """Automatically create schema and run pending migrations.
        
        - Creates 'autocut' schema if not exists
        - Creates '_migrations' tracking table if not exists
        - Runs any unapplied migration scripts from deploy/db/
        """
        if self._conn is None:
            return
            
        try:
            with self._conn.cursor() as cur:
                # 1. Ensure schema exists
                cur.execute(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                    (self._schema,)
                )
                schema_exists = cur.fetchone() is not None
                if not schema_exists:
                    logger.info("Creating schema '%s'", self._schema)
                    cur.execute(f"CREATE SCHEMA {self._schema}")
                
                # 2. Create migrations tracking table FIRST
                migrations_table = f"{self._schema}._migrations"
                cur.execute(f"""
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = %s AND table_name = '_migrations'
                """, (self._schema,))
                migrations_table_exists = cur.fetchone() is not None
                
                if not migrations_table_exists:
                    logger.info("Creating migrations tracking table")
                    cur.execute(f"""
                        CREATE TABLE {migrations_table} (
                            id SERIAL PRIMARY KEY,
                            name VARCHAR(255) NOT NULL UNIQUE,
                            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    # Commit immediately so the table exists even if later steps fail
                    self._conn.commit()
                
                # 3. Find and apply pending migrations
                import os
                from pathlib import Path
                
                deploy_db_dir = Path(__file__).parent.parent.parent / "deploy" / "db"
                if not deploy_db_dir.exists():
                    logger.debug("deploy/db/ directory not found, skipping migrations")
                    return
                
                # Get list of already applied migrations
                cur.execute(f"SELECT name FROM {migrations_table}")
                applied = {row[0] for row in cur.fetchall()}
                
                # Find all migration files (init.sql + migration_*.sql)
                migration_files = []
                init_sql = deploy_db_dir / "init.sql"
                if init_sql.exists():
                    migration_files.append(("init.sql", init_sql))
                
                for f in sorted(deploy_db_dir.glob("migration_*.sql")):
                    migration_files.append((f.name, f))
                
                # Check if tables already exist (Docker auto-init)
                if "init.sql" not in applied:
                    cur.execute(f"""
                        SELECT 1 FROM information_schema.tables 
                        WHERE table_schema = %s AND table_name = 'books'
                    """, (self._schema,))
                    if cur.fetchone():
                        logger.info("Tables already exist (Docker auto-init), marking init.sql as applied")
                        cur.execute(
                            f"INSERT INTO {migrations_table} (name) VALUES (%s)",
                            ("init.sql",)
                        )
                        self._conn.commit()
                        applied.add("init.sql")
                
                # Apply pending migrations
                for name, path in migration_files:
                    if name in applied:
                        continue

                    logger.info("Applying migration: %s", name)
                    sql = path.read_text(encoding="utf-8")

                    try:
                        # Execute migration
                        cur.execute(sql)

                        # Record as applied
                        cur.execute(
                            f"INSERT INTO {migrations_table} (name) VALUES (%s)",
                            (name,)
                        )
                        self._conn.commit()
                        logger.info("Migration %s applied successfully", name)
                    except Exception as e:
                        logger.warning("Migration %s failed: %s", name, e)
                        self._conn.rollback()
                        # Continue with other migrations

                # 4. Run in-code auto-migrations (VLM-first architecture)
                self.auto_migrate()

                logger.info("Schema initialization complete")
                
        except Exception as exc:
            logger.warning("Schema initialization failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass

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
        """UPSERT subtitle segments with source tracking.

        Returns count of upserted rows.
        On conflict (same book_id + episode_id + start_time + source), merges rather than duplicates.

        Each segment dict: start_time, end_time, text (required).
        Optional: speaker, tone, emotion, group_id, group_tone, confidence, cer_estimate,
        kind, language, text_zh, position.
        """
        if not self.is_available or not segments:
            return 0

        table = _t(self._schema, "subtitles")
        count = 0
        for seg in segments:
            if "start_time" not in seg or "end_time" not in seg or "text" not in seg:
                continue

            sql = f"""
                INSERT INTO {table} (
                    book_id, episode_id, start_time, end_time, speaker,
                    text, tone, emotion, group_id, group_tone, source,
                    confidence, cer_estimate, kind, language, text_zh, position
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (book_id, episode_id, start_time, source) DO UPDATE SET
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
                    source = EXCLUDED.source,
                    confidence = COALESCE(EXCLUDED.confidence, subtitles.confidence),
                    cer_estimate = COALESCE(EXCLUDED.cer_estimate, subtitles.cer_estimate),
                    kind = COALESCE(EXCLUDED.kind, subtitles.kind),
                    language = COALESCE(EXCLUDED.language, subtitles.language),
                    text_zh = COALESCE(EXCLUDED.text_zh, subtitles.text_zh),
                    position = COALESCE(EXCLUDED.position, subtitles.position)
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
                    seg.get("kind"),
                    seg.get("language"),
                    seg.get("text_zh"),
                    seg.get("position"),
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

    def update_subtitle(self, subtitle_id: int, **updates: Any) -> int:
        """Update specific fields on a subtitle row. Returns 1 on success."""
        if not self.is_available or not updates:
            return 0
        table = _t(self._schema, "subtitles")
        set_clauses = [f"{_safe_ident(k)} = %s" for k in updates]
        params = list(updates.values()) + [subtitle_id]
        sql = f"UPDATE {table} SET {', '.join(set_clauses)} WHERE id = %s"
        self._execute(sql, tuple(params))
        return 1

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
        highlight_reason, related_srt_range, source, vlm_event_type, vlm_window_id,
        precise_start, precise_end, global_rank, rank_score, rank_criteria.
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
                    shot.get("vlm_event_type"),
                    shot.get("vlm_window_id"),
                    shot.get("precise_start"),
                    shot.get("precise_end"),
                    shot.get("global_rank"),
                    shot.get("rank_score"),
                    shot.get("rank_criteria"),
                )
            )

        if not params_list:
            return 0

        sql = f"""
            INSERT INTO {table} (
                book_id, episode_id, start_time, end_time, scene,
                subjects, actions, is_highlight, highlight_score,
                highlight_reason, related_srt_range, source,
                vlm_event_type, vlm_window_id,
                precise_start, precise_end, global_rank, rank_score, rank_criteria
            ) VALUES %s
        """
        self._execute_values(sql, params_list)
        return len(params_list)

    def replace_shots(
        self,
        book_id: str,
        episode_id: int,
        shots: list[dict[str, Any]],
        sources: list[str],
    ) -> int:
        """原子替换指定 source 的 shots：同一事务内 DELETE → INSERT。

        用于幂等重跑：VLM 阶段重跑同一部剧/同一集时，先按
        (book_id, episode_id, source) 精确删除旧的 shots，再插入新批次。
        规避浮点 start_time/end_time 无法做 UNIQUE 约束的问题，且单一事务
        保证原子性（DELETE 成功但 INSERT 失败时整体回滚，不会丢数据）。

        Args:
            book_id: 剧 ID
            episode_id: 集 ID（global_context 的 API 高光用 0）
            shots: 新的 shot 字典列表（字段同 insert_shots）
            sources: 需要被替换的 source 清单，如
                     ['vlm', 'vlm_highlight_boundary'] 或 ['api']

        Returns:
            写入的 shot 数量；若 DB 不可用或 shots 为空返回 0。
        """
        if not self.is_available:
            return 0
        if not shots:
            # 空批次也执行 DELETE，确保重跑时清空旧数据
            return self._delete_shots_by_source(book_id, episode_id, sources)
        conn = self._ensure_connection()
        if conn is None:
            return 0
        import json
        table = _t(self._schema, "shots")
        
        # 收集 shots 中实际包含的 episode_id 列表
        # 当 episode_id=0 时（如 global_context 的 API 高光），shots 可能跨多集
        actual_episode_ids = list({s.get("episode_id", episode_id) for s in shots})
        
        # DELETE: 按实际 episode_id 列表删除，保证幂等
        if len(actual_episode_ids) == 1:
            delete_sql = (
                f"DELETE FROM {table} "
                f"WHERE book_id = %s AND episode_id = %s AND source = ANY(%s)"
            )
            delete_params = (book_id, actual_episode_ids[0], sources)
        else:
            delete_sql = (
                f"DELETE FROM {table} "
                f"WHERE book_id = %s AND episode_id = ANY(%s) AND source = ANY(%s)"
            )
            delete_params = (book_id, actual_episode_ids, sources)
        
        insert_sql = f"""
            INSERT INTO {table} (
                book_id, episode_id, start_time, end_time, scene,
                subjects, actions, is_highlight, highlight_score,
                highlight_reason, related_srt_range, source,
                vlm_event_type, vlm_window_id,
                precise_start, precise_end, global_rank, rank_score, rank_criteria,
                emotion, conflict, visual_impact
            ) VALUES %s
        """
        params_list = [
            (
                s.get("book_id", book_id),
                s.get("episode_id", episode_id),
                s.get("start_time"),
                s.get("end_time"),
                s.get("scene", ""),
                json.dumps(s.get("subjects", []), ensure_ascii=False),
                s.get("actions"),
                bool(s.get("is_highlight", False)),
                s.get("highlight_score"),
                s.get("highlight_reason"),
                s.get("related_srt_range"),
                s.get("source", "api"),
                s.get("vlm_event_type"),
                s.get("vlm_window_id"),
                s.get("precise_start"),
                s.get("precise_end"),
                s.get("global_rank"),
                s.get("rank_score"),
                s.get("rank_criteria"),
                s.get("emotion", ""),
                s.get("conflict", ""),
                s.get("visual_impact", ""),
            )
            for s in shots
        ]
        # 关键：手动事务，DELETE 与 INSERT 在同一 cursor / 同一 commit 内，
        # 不经过 _execute（其内部各自 commit，不原子）。
        try:
            with conn.cursor() as cur:
                cur.execute(delete_sql, delete_params)
                psycopg2.extras.execute_values(cur, insert_sql, params_list)
                conn.commit()
            return len(params_list)
        except Exception as exc:
            logger.warning(
                "replace_shots failed [book=%s, ep=%s, sources=%s]: %s",
                book_id,
                episode_id,
                sources,
                exc,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def _delete_shots_by_source(
        self,
        book_id: str,
        episode_id: int,
        sources: list[str],
    ) -> int:
        """仅删除指定 source 的 shots（供空批次重跑清空旧数据）。"""
        if not self.is_available or not sources:
            return 0
        table = _t(self._schema, "shots")
        sql = (
            f"DELETE FROM {table} "
            f"WHERE book_id = %s AND episode_id = %s AND source = ANY(%s)"
        )
        conn = self._ensure_connection()
        if conn is None:
            return 0
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (book_id, episode_id, sources))
                deleted = cur.rowcount
                conn.commit()
            return deleted or 0
        except Exception as exc:
            logger.warning(
                "delete_shots_by_source failed [book=%s, ep=%s]: %s",
                book_id,
                episode_id,
                exc,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            return 0

    def replace_story_beats(
        self,
        book_id: str,
        episode_id: int,
        beats: list[dict[str, Any]],
        source: str = "vlm",
    ) -> int:
        """原子替换指定 episode 的 story_beats（DELETE + INSERT 同一事务）。"""
        if not self.is_available:
            return 0
        import json
        table = _t(self._schema, "story_beats")
        conn = self._ensure_connection()
        if conn is None:
            return 0

        delete_sql = f"DELETE FROM {table} WHERE book_id = %s AND episode_id = %s AND source = %s"
        if not beats:
            try:
                with conn.cursor() as cur:
                    cur.execute(delete_sql, (book_id, episode_id, source))
                    conn.commit()
                return 0
            except Exception as exc:
                logger.warning("replace_story_beats (delete-only) failed [book=%s, ep=%s]: %s", book_id, episode_id, exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

        insert_sql = f"""
            INSERT INTO {table} (
                book_id, episode_id, window_id, start_time, end_time,
                function, summary, characters, cause, effect, open_question,
                event_id, temporal_mode, merged_into_event, source
            ) VALUES %s
        """
        params_list = [
            (
                book_id,
                episode_id,
                b.get("window_id", ""),
                b.get("start_time", 0),
                b.get("end_time", 0),
                b.get("function", ""),
                b.get("summary", ""),
                json.dumps(b.get("characters", []), ensure_ascii=False),
                b.get("cause", ""),
                b.get("effect", ""),
                b.get("open_question", ""),
                b.get("event_id"),
                b.get("temporal_mode"),
                b.get("merged_into_event"),
                b.get("source", source),
            )
            for b in beats
        ]
        try:
            with conn.cursor() as cur:
                cur.execute(delete_sql, (book_id, episode_id, source))
                psycopg2.extras.execute_values(cur, insert_sql, params_list)
                conn.commit()
            return len(params_list)
        except Exception as exc:
            logger.warning(
                "replace_story_beats failed [book=%s, ep=%s, n=%d]: %s",
                book_id, episode_id, len(beats), exc,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def query_story_beats(
        self,
        book_id: str,
        episode_id: int,
        *,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query story_beats for a book/episode, optionally filtered by source."""
        if not self.is_available:
            return []
        table = _t(self._schema, "story_beats")
        conditions = ["book_id = %s", "episode_id = %s"]
        params: list[Any] = [book_id, episode_id]

        if source is not None:
            conditions.append("source = %s")
            params.append(source)

        sql = f"SELECT * FROM {table} WHERE {' AND '.join(conditions)} ORDER BY start_time"
        rows = self._execute(sql, tuple(params))
        return [dict(r) for r in rows]

    # ══════════════════════════════════════════════════════════════════════
    # 11. highlight_candidates
    # ══════════════════════════════════════════════════════════════════════

    def replace_highlight_candidates(
        self,
        book_id: str,
        episode_id: int,
        candidates: list[dict[str, Any]],
        source: str = "vlm",
    ) -> int:
        """原子替换指定 episode/source 的 highlight_candidates（DELETE + INSERT 同一事务）。"""
        if not self.is_available:
            return 0
        import json
        table = _t(self._schema, "highlight_candidates")
        conn = self._ensure_connection()
        if conn is None:
            return 0

        delete_sql = f"DELETE FROM {table} WHERE book_id = %s AND episode_id = %s AND source = %s"
        if not candidates:
            try:
                with conn.cursor() as cur:
                    cur.execute(delete_sql, (book_id, episode_id, source))
                    conn.commit()
                return 0
            except Exception as exc:
                logger.warning("replace_highlight_candidates (delete-only) failed [book=%s, ep=%s]: %s", book_id, episode_id, exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

        insert_sql = f"""
            INSERT INTO {table} (
                book_id, episode_id, start_time, end_time, score,
                reason, candidate_type, source, metadata
            ) VALUES %s
        """
        params_list = [
            (
                book_id,
                episode_id,
                c.get("start_time", 0),
                c.get("end_time", 0),
                c.get("score"),
                c.get("reason"),
                c.get("candidate_type"),
                c.get("source", source),
                json.dumps(c.get("metadata", {}), ensure_ascii=False),
            )
            for c in candidates
        ]
        try:
            with conn.cursor() as cur:
                cur.execute(delete_sql, (book_id, episode_id, source))
                psycopg2.extras.execute_values(cur, insert_sql, params_list)
                conn.commit()
            return len(params_list)
        except Exception as exc:
            logger.warning(
                "replace_highlight_candidates failed [book=%s, ep=%s, n=%d]: %s",
                book_id, episode_id, len(candidates), exc,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def query_highlight_candidates(
        self,
        book_id: str,
        episode_id: int | None = None,
        *,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query highlight_candidates, optionally filtered by episode_id and source."""
        if not self.is_available:
            return []
        table = _t(self._schema, "highlight_candidates")
        conditions = ["book_id = %s"]
        params: list[Any] = [book_id]

        if episode_id is not None:
            conditions.append("episode_id = %s")
            params.append(episode_id)

        if source is not None:
            conditions.append("source = %s")
            params.append(source)

        sql = f"SELECT * FROM {table} WHERE {' AND '.join(conditions)} ORDER BY score DESC NULLS LAST, start_time"
        rows = self._execute(sql, tuple(params))
        return [dict(r) for r in rows]

    # ══════════════════════════════════════════════════════════════════════
    # 12. span_candidates
    # ══════════════════════════════════════════════════════════════════════

    def replace_span_candidates(
        self,
        book_id: str,
        episode_id: int,
        spans: list[dict[str, Any]],
        source: str = "vlm",
    ) -> int:
        """原子替换指定 episode/source 的 span_candidates（DELETE + INSERT 同一事务）。"""
        if not self.is_available:
            return 0
        import json
        table = _t(self._schema, "span_candidates")
        conn = self._ensure_connection()
        if conn is None:
            return 0

        delete_sql = f"DELETE FROM {table} WHERE book_id = %s AND episode_id = %s AND source = %s"
        if not spans:
            try:
                with conn.cursor() as cur:
                    cur.execute(delete_sql, (book_id, episode_id, source))
                    conn.commit()
                return 0
            except Exception as exc:
                logger.warning("replace_span_candidates (delete-only) failed [book=%s, ep=%s]: %s", book_id, episode_id, exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

        insert_sql = f"""
            INSERT INTO {table} (
                book_id, episode_id, candidate_id, start_time, end_time,
                span_type, confidence, features, source
            ) VALUES %s
        """
        params_list = [
            (
                book_id,
                episode_id,
                s.get("candidate_id", ""),
                s.get("start_time", 0),
                s.get("end_time", 0),
                s.get("span_type"),
                s.get("confidence"),
                json.dumps(s.get("features", {}), ensure_ascii=False),
                s.get("source", source),
            )
            for s in spans
        ]
        try:
            with conn.cursor() as cur:
                cur.execute(delete_sql, (book_id, episode_id, source))
                psycopg2.extras.execute_values(cur, insert_sql, params_list)
                conn.commit()
            return len(params_list)
        except Exception as exc:
            logger.warning(
                "replace_span_candidates failed [book=%s, ep=%s, n=%d]: %s",
                book_id, episode_id, len(spans), exc,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def query_span_candidates(
        self,
        book_id: str,
        episode_id: int | None = None,
        *,
        source: str | None = None,
        span_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query span_candidates, optionally filtered by episode_id, source, span_type."""
        if not self.is_available:
            return []
        table = _t(self._schema, "span_candidates")
        conditions = ["book_id = %s"]
        params: list[Any] = [book_id]

        if episode_id is not None:
            conditions.append("episode_id = %s")
            params.append(episode_id)

        if source is not None:
            conditions.append("source = %s")
            params.append(source)

        if span_type is not None:
            conditions.append("span_type = %s")
            params.append(span_type)

        sql = f"SELECT * FROM {table} WHERE {' AND '.join(conditions)} ORDER BY confidence DESC NULLS LAST, start_time"
        rows = self._execute(sql, tuple(params))
        return [dict(r) for r in rows]

    # ══════════════════════════════════════════════════════════════════════
    # 13. final_clips
    # ══════════════════════════════════════════════════════════════════════

    def replace_final_clips(
        self,
        book_id: str,
        episode_id: int,
        clips: list[dict[str, Any]],
        source: str = "selector",
    ) -> int:
        """原子替换指定 episode/source 的 final_clips（DELETE + INSERT 同一事务）。"""
        if not self.is_available:
            return 0
        import json
        table = _t(self._schema, "final_clips")
        conn = self._ensure_connection()
        if conn is None:
            return 0

        delete_sql = f"DELETE FROM {table} WHERE book_id = %s AND episode_id = %s AND source = %s"
        if not clips:
            try:
                with conn.cursor() as cur:
                    cur.execute(delete_sql, (book_id, episode_id, source))
                    conn.commit()
                return 0
            except Exception as exc:
                logger.warning("replace_final_clips (delete-only) failed [book=%s, ep=%s]: %s", book_id, episode_id, exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

        insert_sql = f"""
            INSERT INTO {table} (
                book_id, episode_id, clip_id, start_time, end_time,
                title, description, tags, score, clip_order, source, metadata
            ) VALUES %s
        """
        params_list = [
            (
                book_id,
                episode_id,
                c.get("clip_id", ""),
                c.get("start_time", 0),
                c.get("end_time", 0),
                c.get("title"),
                c.get("description"),
                c.get("tags", []),
                c.get("score"),
                c.get("clip_order"),
                c.get("source", source),
                json.dumps(c.get("metadata", {}), ensure_ascii=False),
            )
            for c in clips
        ]
        try:
            with conn.cursor() as cur:
                cur.execute(delete_sql, (book_id, episode_id, source))
                psycopg2.extras.execute_values(cur, insert_sql, params_list)
                conn.commit()
            return len(params_list)
        except Exception as exc:
            logger.warning(
                "replace_final_clips failed [book=%s, ep=%s, n=%d]: %s",
                book_id, episode_id, len(clips), exc,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def query_final_clips(
        self,
        book_id: str,
        episode_id: int | None = None,
        *,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query final_clips, optionally filtered by episode_id and source, ordered by clip_order."""
        if not self.is_available:
            return []
        table = _t(self._schema, "final_clips")
        conditions = ["book_id = %s"]
        params: list[Any] = [book_id]

        if episode_id is not None:
            conditions.append("episode_id = %s")
            params.append(episode_id)

        if source is not None:
            conditions.append("source = %s")
            params.append(source)

        sql = f"SELECT * FROM {table} WHERE {' AND '.join(conditions)} ORDER BY clip_order ASC NULLS LAST, start_time"
        rows = self._execute(sql, tuple(params))
        return [dict(r) for r in rows]

    # ══════════════════════════════════════════════════════════════════════
    # 14. story_events
    # ══════════════════════════════════════════════════════════════════════

    def replace_story_events(
        self,
        book_id: str,
        episode_id: int,
        events: list[dict[str, Any]],
        source: str = "story_analyzer",
    ) -> int:
        """原子替换指定 episode/source 的 story_events（DELETE + INSERT 同一事务）。"""
        if not self.is_available:
            return 0
        import json
        table = _t(self._schema, "story_events")
        conn = self._ensure_connection()
        if conn is None:
            return 0

        delete_sql = f"DELETE FROM {table} WHERE book_id = %s AND episode_id = %s AND source = %s"
        if not events:
            try:
                with conn.cursor() as cur:
                    cur.execute(delete_sql, (book_id, episode_id, source))
                    conn.commit()
                return 0
            except Exception as exc:
                logger.warning("replace_story_events (delete-only) failed [book=%s, ep=%s]: %s", book_id, episode_id, exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

        insert_sql = f"""
            INSERT INTO {table} (
                book_id, episode_id, event_id, event_type, summary,
                participants, cause, effect, start_time, end_time,
                temporal_mode, importance, source, metadata
            ) VALUES %s
        """
        params_list = [
            (
                book_id,
                episode_id,
                e.get("event_id", ""),
                e.get("event_type"),
                e.get("summary"),
                e.get("participants", []),
                e.get("cause"),
                e.get("effect"),
                e.get("start_time"),
                e.get("end_time"),
                e.get("temporal_mode"),
                e.get("importance"),
                e.get("source", source),
                json.dumps(e.get("metadata", {}), ensure_ascii=False),
            )
            for e in events
        ]
        try:
            with conn.cursor() as cur:
                cur.execute(delete_sql, (book_id, episode_id, source))
                psycopg2.extras.execute_values(cur, insert_sql, params_list)
                conn.commit()
            return len(params_list)
        except Exception as exc:
            logger.warning(
                "replace_story_events failed [book=%s, ep=%s, n=%d]: %s",
                book_id, episode_id, len(events), exc,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def query_story_events(
        self,
        book_id: str,
        episode_id: int | None = None,
        *,
        source: str | None = None,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query story_events, optionally filtered by episode_id, source, event_type."""
        if not self.is_available:
            return []
        table = _t(self._schema, "story_events")
        conditions = ["book_id = %s"]
        params: list[Any] = [book_id]

        if episode_id is not None:
            conditions.append("episode_id = %s")
            params.append(episode_id)

        if source is not None:
            conditions.append("source = %s")
            params.append(source)

        if event_type is not None:
            conditions.append("event_type = %s")
            params.append(event_type)

        sql = f"SELECT * FROM {table} WHERE {' AND '.join(conditions)} ORDER BY importance DESC NULLS LAST, start_time"
        rows = self._execute(sql, tuple(params))
        return [dict(r) for r in rows]

    # ══════════════════════════════════════════════════════════════════════
    # 15. story_threads
    # ══════════════════════════════════════════════════════════════════════

    def replace_story_threads(
        self,
        book_id: str,
        episode_id: int,
        threads: list[dict[str, Any]],
        source: str = "thread_analyzer",
    ) -> int:
        """原子替换指定 book/source 的 story_threads（DELETE + INSERT 同一事务）。

        Note: threads are book-level (not episode-level), episode_id is used
        for context but threads span episodes.
        """
        if not self.is_available:
            return 0
        import json
        table = _t(self._schema, "story_threads")
        conn = self._ensure_connection()
        if conn is None:
            return 0

        delete_sql = f"DELETE FROM {table} WHERE book_id = %s AND source = %s"
        if not threads:
            try:
                with conn.cursor() as cur:
                    cur.execute(delete_sql, (book_id, source))
                    conn.commit()
                return 0
            except Exception as exc:
                logger.warning("replace_story_threads (delete-only) failed [book=%s]: %s", book_id, exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

        insert_sql = f"""
            INSERT INTO {table} (
                book_id, thread_id, thread_name, description, thread_type,
                importance, status, first_episode, last_episode, source, metadata
            ) VALUES %s
        """
        params_list = [
            (
                book_id,
                t.get("thread_id", ""),
                t.get("thread_name", ""),
                t.get("description"),
                t.get("thread_type"),
                t.get("importance"),
                t.get("status"),
                t.get("first_episode", episode_id),
                t.get("last_episode", episode_id),
                t.get("source", source),
                json.dumps(t.get("metadata", {}), ensure_ascii=False),
            )
            for t in threads
        ]
        try:
            with conn.cursor() as cur:
                cur.execute(delete_sql, (book_id, source))
                psycopg2.extras.execute_values(cur, insert_sql, params_list)
                conn.commit()
            return len(params_list)
        except Exception as exc:
            logger.warning(
                "replace_story_threads failed [book=%s, n=%d]: %s",
                book_id, len(threads), exc,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def query_story_threads(
        self,
        book_id: str,
        *,
        source: str | None = None,
        thread_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query story_threads for a book, optionally filtered by source, thread_type, status."""
        if not self.is_available:
            return []
        table = _t(self._schema, "story_threads")
        conditions = ["book_id = %s"]
        params: list[Any] = [book_id]

        if source is not None:
            conditions.append("source = %s")
            params.append(source)

        if thread_type is not None:
            conditions.append("thread_type = %s")
            params.append(thread_type)

        if status is not None:
            conditions.append("status = %s")
            params.append(status)

        sql = f"SELECT * FROM {table} WHERE {' AND '.join(conditions)} ORDER BY importance DESC NULLS LAST, first_episode"
        rows = self._execute(sql, tuple(params))
        return [dict(r) for r in rows]

    # ══════════════════════════════════════════════════════════════════════
    # 16. story_thread_beats
    # ══════════════════════════════════════════════════════════════════════

    def replace_story_thread_beats(
        self,
        book_id: str,
        episode_id: int,
        beats: list[dict[str, Any]],
        source: str = "thread_analyzer",
    ) -> int:
        """原子替换指定 episode/source 的 story_thread_beats（DELETE + INSERT 同一事务）。"""
        if not self.is_available:
            return 0
        import json
        table = _t(self._schema, "story_thread_beats")
        conn = self._ensure_connection()
        if conn is None:
            return 0

        delete_sql = f"DELETE FROM {table} WHERE book_id = %s AND episode_id = %s AND source = %s"
        if not beats:
            try:
                with conn.cursor() as cur:
                    cur.execute(delete_sql, (book_id, episode_id, source))
                    conn.commit()
                return 0
            except Exception as exc:
                logger.warning("replace_story_thread_beats (delete-only) failed [book=%s, ep=%s]: %s", book_id, episode_id, exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

        insert_sql = f"""
            INSERT INTO {table} (
                book_id, episode_id, thread_id, beat_id, event_id,
                beat_summary, beat_order, start_time, end_time, source, metadata
            ) VALUES %s
        """
        params_list = [
            (
                book_id,
                episode_id,
                b.get("thread_id", ""),
                b.get("beat_id", ""),
                b.get("event_id"),
                b.get("beat_summary"),
                b.get("beat_order"),
                b.get("start_time"),
                b.get("end_time"),
                b.get("source", source),
                json.dumps(b.get("metadata", {}), ensure_ascii=False),
            )
            for b in beats
        ]
        try:
            with conn.cursor() as cur:
                cur.execute(delete_sql, (book_id, episode_id, source))
                psycopg2.extras.execute_values(cur, insert_sql, params_list)
                conn.commit()
            return len(params_list)
        except Exception as exc:
            logger.warning(
                "replace_story_thread_beats failed [book=%s, ep=%s, n=%d]: %s",
                book_id, episode_id, len(beats), exc,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def query_story_thread_beats(
        self,
        book_id: str,
        episode_id: int | None = None,
        *,
        thread_id: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query story_thread_beats, optionally filtered by episode_id, thread_id, source."""
        if not self.is_available:
            return []
        table = _t(self._schema, "story_thread_beats")
        conditions = ["book_id = %s"]
        params: list[Any] = [book_id]

        if episode_id is not None:
            conditions.append("episode_id = %s")
            params.append(episode_id)

        if thread_id is not None:
            conditions.append("thread_id = %s")
            params.append(thread_id)

        if source is not None:
            conditions.append("source = %s")
            params.append(source)

        sql = f"SELECT * FROM {table} WHERE {' AND '.join(conditions)} ORDER BY beat_order ASC NULLS LAST, start_time"
        rows = self._execute(sql, tuple(params))
        return [dict(r) for r in rows]

    # ══════════════════════════════════════════════════════════════════════
    # 17. story_facts
    # ══════════════════════════════════════════════════════════════════════

    def replace_story_facts(
        self,
        book_id: str,
        episode_id: int,
        facts: list[dict[str, Any]],
        source: str = "fact_extractor",
    ) -> int:
        """原子替换指定 book/source 的 story_facts（DELETE + INSERT 同一事务）。

        Note: facts are book-level knowledge, episode_id is used for context
        when facts are extracted from a specific episode.
        """
        if not self.is_available:
            return 0
        import json
        table = _t(self._schema, "story_facts")
        conn = self._ensure_connection()
        if conn is None:
            return 0

        delete_sql = f"DELETE FROM {table} WHERE book_id = %s AND source = %s"
        if not facts:
            try:
                with conn.cursor() as cur:
                    cur.execute(delete_sql, (book_id, source))
                    conn.commit()
                return 0
            except Exception as exc:
                logger.warning("replace_story_facts (delete-only) failed [book=%s]: %s", book_id, exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

        insert_sql = f"""
            INSERT INTO {table} (
                book_id, fact_id, fact_type, subject, fact_text,
                confidence, first_episode, last_episode, related_events, source, metadata
            ) VALUES %s
        """
        params_list = [
            (
                book_id,
                f.get("fact_id", ""),
                f.get("fact_type"),
                f.get("subject"),
                f.get("fact_text", ""),
                f.get("confidence"),
                f.get("first_episode", episode_id),
                f.get("last_episode", episode_id),
                f.get("related_events", []),
                f.get("source", source),
                json.dumps(f.get("metadata", {}), ensure_ascii=False),
            )
            for f in facts
        ]
        try:
            with conn.cursor() as cur:
                cur.execute(delete_sql, (book_id, source))
                psycopg2.extras.execute_values(cur, insert_sql, params_list)
                conn.commit()
            return len(params_list)
        except Exception as exc:
            logger.warning(
                "replace_story_facts failed [book=%s, n=%d]: %s",
                book_id, len(facts), exc,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def query_story_facts(
        self,
        book_id: str,
        *,
        source: str | None = None,
        fact_type: str | None = None,
        subject: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query story_facts for a book, optionally filtered by source, fact_type, subject."""
        if not self.is_available:
            return []
        table = _t(self._schema, "story_facts")
        conditions = ["book_id = %s"]
        params: list[Any] = [book_id]

        if source is not None:
            conditions.append("source = %s")
            params.append(source)

        if fact_type is not None:
            conditions.append("fact_type = %s")
            params.append(fact_type)

        if subject is not None:
            conditions.append("subject = %s")
            params.append(subject)

        sql = f"SELECT * FROM {table} WHERE {' AND '.join(conditions)} ORDER BY confidence DESC NULLS LAST, first_episode"
        rows = self._execute(sql, tuple(params))
        return [dict(r) for r in rows]

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

    def update_highlight_ranking(
        self,
        shot_id: int,
        global_rank: int,
        rank_score: float,
        rank_criteria: dict[str, Any],
    ) -> bool:
        """Update highlight ranking fields on a shot record.

        Args:
            shot_id: The shot record ID to update.
            global_rank: Rank position (1 = best).
            rank_score: Numeric ranking score.
            rank_criteria: JSONB criteria used for ranking.

        Returns True on success, False when DB is unavailable.
        """
        if not self.is_available:
            return False
        import json

        table = _t(self._schema, "shots")
        sql = f"""
            UPDATE {table}
            SET global_rank = %s, rank_score = %s, rank_criteria = %s::jsonb
            WHERE id = %s
        """
        self._execute(
            sql,
            (
                global_rank,
                rank_score,
                json.dumps(rank_criteria, ensure_ascii=False),
                shot_id,
            ),
        )
        return True

    def query_highlights(
        self,
        book_id: str,
        episode: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query highlight shots with ranking information.

        Returns shots where is_highlight = true, ordered by global_rank.
        Optionally filtered by episode_id.

        Args:
            book_id: Book identifier.
            episode: Optional episode ID to filter by.

        Returns list of shot dicts, empty list when DB is unavailable.
        """
        if not self.is_available:
            return []
        table = _t(self._schema, "shots")
        if episode is not None:
            sql = f"""
                SELECT * FROM {table}
                WHERE book_id = %s AND episode_id = %s AND is_highlight = true
                ORDER BY global_rank ASC NULLS LAST, rank_score DESC NULLS LAST
            """
            rows = self._execute(sql, (book_id, episode))
        else:
            sql = f"""
                SELECT * FROM {table}
                WHERE book_id = %s AND is_highlight = true
                ORDER BY global_rank ASC NULLS LAST, rank_score DESC NULLS LAST
            """
            rows = self._execute(sql, (book_id,))
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



    # ── Pipeline run versioning & snapshots ────────────────────────────

    # 哪些表需要快照（按依赖顺序排列，回退时按此顺序先删后插）
    _SNAPSHOT_TABLES = [
        "boundaries",
        "shots",
        "scenes",
        "subtitles",
        "speaker_mappings",
        "subject_episodes",
        "relationships",
        "subjects",
        "episodes",
        "books",
    ]

    def start_pipeline_run(
        self,
        book_id: str,
        job_name: str,
        config: dict[str, Any] | None = None,
    ) -> int | None:
        """创建一条 pipeline_run 记录，返回 run_id。"""
        if not self.is_available:
            return None
        import json

        table = _t(self._schema, "pipeline_runs")
        rows = self._execute(
            f"""INSERT INTO {table} (book_id, job_name, config)
                VALUES (%s, %s, %s) RETURNING id""",
            (book_id, job_name, json.dumps(config or {}, ensure_ascii=False)),
        )
        if rows:
            return rows[0]["id"]
        return None

    def complete_pipeline_run(
        self, run_id: int, status: str = "completed", notes: str | None = None
    ) -> None:
        """标记 pipeline_run 完成/失败。"""
        if not self.is_available or run_id is None:
            return
        table = _t(self._schema, "pipeline_runs")
        self._execute(
            f"UPDATE {table} SET status = %s, completed_at = now(), notes = %s WHERE id = %s",
            (status, notes, run_id),
        )

    def snapshot_book(self, book_id: str) -> dict[str, list[dict[str, Any]]]:
        """快照某本书在所有相关表中的数据。

        返回 {"table_name": [row1, row2, ...], ...}，
        只包含该 book_id 有数据的表。
        """
        result: dict[str, list[dict[str, Any]]] = {}
        if not self.is_available:
            return result

        for table_name in self._SNAPSHOT_TABLES:
            table = _t(self._schema, table_name)
            try:
                rows = self._execute(
                    f"SELECT * FROM {table} WHERE book_id = %s ORDER BY 1",
                    (book_id,),
                )
                if rows:
                    # 转成普通 dict（RealDictCursor 返回 OrderedDict）
                    result[table_name] = [dict(r) for r in rows]
            except Exception as exc:
                logger.warning("snapshot_table(%s) failed: %s", table_name, exc)

        return result

    def save_stage_snapshot(
        self,
        pipeline_run_id: int,
        stage_name: str,
        book_id: str,
        db_writes: list[str],
    ) -> None:
        """在 stage 执行前，快照该 stage 声明要写入的表。

        Args:
            pipeline_run_id: 当前 run 的 ID
            stage_name: stage 名称
            book_id: 书籍 ID
            db_writes: 该 stage 声明要写入的表名列表
        """
        if not self.is_available or pipeline_run_id is None:
            return
        import json

        # 只快照 db_writes 中声明的表
        tables_to_snapshot = [
            t for t in db_writes if t in self._SNAPSHOT_TABLES
        ]
        if not tables_to_snapshot:
            return

        snapshots: dict[str, list[dict[str, Any]]] = {}
        for table_name in tables_to_snapshot:
            table = _t(self._schema, table_name)
            try:
                rows = self._execute(
                    f"SELECT * FROM {table} WHERE book_id = %s ORDER BY 1",
                    (book_id,),
                )
                if rows:
                    snapshots[table_name] = [dict(r) for r in rows]
                else:
                    snapshots[table_name] = []
            except Exception as exc:
                logger.warning(
                    "save_stage_snapshot(%s/%s) snapshot failed: %s",
                    stage_name, table_name, exc,
                )

        snapshot_table = _t(self._schema, "stage_snapshots")
        self._execute(
            f"""INSERT INTO {snapshot_table}
                (pipeline_run_id, stage_name, book_id, snapshots)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (pipeline_run_id, stage_name)
                DO UPDATE SET snapshots = EXCLUDED.snapshots,
                              created_at = now()""",
            (
                pipeline_run_id,
                stage_name,
                book_id,
                json.dumps(snapshots, ensure_ascii=False, default=str),
            ),
        )

    def rollback_to_run(
        self,
        target_run_id: int,
        stage_name: str | None = None,
    ) -> dict[str, int]:
        """回退到指定 run 的快照状态。

        Args:
            target_run_id: 目标 run 的 ID
            stage_name: 指定回退到哪个 stage 的快照；
                        None 时回退到该 run 最后一个 stage 的快照

        Returns:
            {"table_name": restored_count, ...}
        """
        if not self.is_available:
            return {}
        import json

        snapshot_table = _t(self._schema, "stage_snapshots")

        # 加载快照
        if stage_name:
            rows = self._execute(
                f"SELECT * FROM {snapshot_table} WHERE pipeline_run_id = %s AND stage_name = %s",
                (target_run_id, stage_name),
            )
        else:
            rows = self._execute(
                f"""SELECT * FROM {snapshot_table}
                    WHERE pipeline_run_id = %s
                    ORDER BY id DESC LIMIT 1""",
                (target_run_id,),
            )

        if not rows:
            logger.warning("No snapshot found for run_id=%s stage=%s", target_run_id, stage_name)
            return {}

        snapshot_row = rows[0]
        book_id = snapshot_row["book_id"]
        snapshots = snapshot_row["snapshots"]
        if isinstance(snapshots, str):
            snapshots = json.loads(snapshots)

        result: dict[str, int] = {}

        # JSONB 列映射：这些列需要 psycopg2.extras.Json 包装
        _JSONB_COLUMNS = {
            "books": {"tags", "script_parsed"},
            "boundaries": {"subjects"},
            "episodes": {"sources_evidence"},
            "relationships": {"sources_evidence"},
            "shots": {"subjects"},
            "subjects": {"aliases", "personality", "sources_evidence"},
        }

        conn = self._ensure_connection()
        if conn is None:
            return {}

        try:
            # 快照里有哪些表
            snapshot_tables = set(snapshots.keys())
            
            # FK 反向依赖：如果快照里有 subjects，需要同时删除 subject_episodes
            _CHILD_TABLES = {
                "books": ["episodes", "subjects", "relationships", "boundaries"],
                "episodes": ["shots", "subtitles", "scenes", "subject_episodes", "speaker_mappings"],
                "subjects": ["relationships", "subject_episodes"],
            }
            
            # 计算需要删除的表：快照表 + 所有引用它们的子表
            tables_to_delete = set(snapshot_tables)
            for tname in snapshot_tables:
                for child in _CHILD_TABLES.get(tname, []):
                    tables_to_delete.add(child)
            
            # Step 1: DELETE（按子表→父表顺序）
            _DELETE_ORDER = [
                "speaker_mappings", "subject_episodes", "subtitles",
                "scenes", "boundaries", "shots",
                "relationships", "subjects", "episodes", "books",
            ]
            for tname in _DELETE_ORDER:
                if tname not in tables_to_delete:
                    continue
                table = _t(self._schema, tname)
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {table} WHERE book_id = %s",
                        (book_id,),
                    )

            # Step 2: INSERT 快照数据（按依赖顺序：先父表后子表）
            _INSERT_ORDER = [
                "books", "episodes", "subjects", "relationships",
                "shots", "boundaries", "subject_episodes",
                "subtitles", "scenes", "speaker_mappings",
            ]
            
            # FK 依赖映射：子表 -> 必需的父表
            _FK_DEPS = {
                "episodes": ["books"],
                "subjects": ["books"],
                "relationships": ["books", "subjects"],
                "shots": ["episodes"],
                "boundaries": ["books"],
                "subject_episodes": ["episodes", "subjects"],
                "subtitles": ["episodes"],
                "scenes": ["episodes"],
                "speaker_mappings": ["episodes"],
            }
            
            for tname in _INSERT_ORDER:
                if tname not in snapshots:
                    continue
                
                # 检查 FK 依赖是否满足
                required_parents = _FK_DEPS.get(tname, [])
                missing_parents = [p for p in required_parents if p not in snapshots]
                if missing_parents:
                    logger.warning(
                        "Skipping rollback of %s: missing parent tables %s in snapshot",
                        tname, missing_parents,
                    )
                    continue
                
                table = _t(self._schema, tname)
                old_rows = snapshots[tname]
                if not old_rows:
                    result[tname] = 0
                    continue

                columns = list(old_rows[0].keys())
                col_list = ", ".join(columns)
                placeholders = ", ".join(["%s"] * len(columns))
                insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"

                jsonb_cols = _JSONB_COLUMNS.get(tname, set())
                with conn.cursor() as cur:
                    for row in old_rows:
                        values = []
                        for c in columns:
                            val = row[c]
                            if c in jsonb_cols and val is not None and _HAS_PSYCOPG2:
                                values.append(psycopg2.extras.Json(val))
                            else:
                                values.append(val)
                        cur.execute(insert_sql, tuple(values))
                result[tname] = len(old_rows)

            conn.commit()
            logger.info(
                "Rollback to run_id=%s stage=%s: %s",
                target_run_id, stage_name, result,
            )
            return result
        except Exception as exc:
            logger.warning("Rollback failed [%s]: %s", exc.__class__.__name__, exc)
            try:
                conn.rollback()
            except Exception:
                pass
            return {}

    def list_pipeline_runs(self, book_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """列出某本书的 pipeline 运行记录。"""
        if not self.is_available:
            return []
        table = _t(self._schema, "pipeline_runs")
        return self._execute(
            f"SELECT * FROM {table} WHERE book_id = %s ORDER BY id DESC LIMIT %s",
            (book_id, limit),
        )

    def list_snapshots(self, pipeline_run_id: int) -> list[dict[str, Any]]:
        """列出某个 run 的所有 stage 快照（不含快照数据本身）。"""
        if not self.is_available:
            return []
        table = _t(self._schema, "stage_snapshots")
        return self._execute(
            f"""SELECT id, pipeline_run_id, stage_name, book_id,
                       created_at, jsonb_object_keys(snapshots) as tables
                FROM {table}
                WHERE pipeline_run_id = %s
                ORDER BY id""",
            (pipeline_run_id,),
        )

    # ── VLM-first architecture: auto-migration ──────────────────────────

    def auto_migrate(self) -> None:
        """Run all idempotent in-code migrations for VLM-first architecture.

        Called by _auto_init_schema after SQL file migrations. All migrations
        use IF NOT EXISTS / IF EXISTS for safe re-runs.
        """
        self._ensure_global_context_table()
        self._ensure_vlm_confidence_log_table()
        self._ensure_highlight_skill_evolution_table()
        self._ensure_highlight_skill_versions_table()
        self._ensure_highlight_candidates_table()
        self._ensure_span_candidates_table()
        self._ensure_final_clips_table()
        self._ensure_story_events_table()
        self._ensure_story_threads_table()
        self._ensure_story_thread_beats_table()
        self._ensure_story_facts_table()
        self._migrate_episodes_source_default()
        self._migrate_shots_highlight_fields()
        self._migrate_subtitles_vlm_fields()
        self._migrate_subjects_vlm_fields()
        self._migrate_story_beats_event_fields()

    def _ensure_global_context_table(self) -> None:
        """Create global_context table if not exists.

        Schema: book_id TEXT PK, synopsis TEXT, themes TEXT[], relationships
        JSONB, source TEXT, fetched_at TIMESTAMPTZ.
        """
        if self._conn is None:
            return
        try:
            table = _t(self._schema, "global_context")
            with self._conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        book_id         TEXT PRIMARY KEY,
                        synopsis        TEXT,
                        themes          TEXT[],
                        relationships   JSONB,
                        source          TEXT,
                        fetched_at      TIMESTAMPTZ DEFAULT now()
                    )
                """)
                self._conn.commit()
            logger.info("Ensured table: %s", table)
        except Exception as exc:
            logger.warning("_ensure_global_context_table failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass

    def query_global_context(self, book_id: str) -> dict[str, Any] | None:
        """Query global_context for a book. Returns None if not found."""
        if not self.is_available:
            return None
        table = _t(self._schema, "global_context")
        rows = self._execute(
            f"SELECT * FROM {table} WHERE book_id = %s", (book_id,)
        )
        return dict(rows[0]) if rows else None

    def upsert_global_context(
        self,
        book_id: str,
        synopsis: str | None = None,
        themes: list[str] | None = None,
        relationships: list[dict[str, Any]] | None = None,
        source: str = "api",
    ) -> bool:
        """UPSERT global_context for a book. Returns True on success."""
        if not self.is_available:
            return False
        import json

        table = _t(self._schema, "global_context")
        relationships_json = (
            json.dumps(relationships, ensure_ascii=False) if relationships else None
        )
        sql = f"""
            INSERT INTO {table} (book_id, synopsis, themes, relationships, source)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (book_id) DO UPDATE SET
                synopsis = COALESCE(EXCLUDED.synopsis, global_context.synopsis),
                themes = COALESCE(EXCLUDED.themes, global_context.themes),
                relationships = COALESCE(EXCLUDED.relationships, global_context.relationships),
                source = EXCLUDED.source,
                fetched_at = now()
        """
        self._execute(
            sql,
            (
                book_id,
                synopsis,
                themes,
                relationships_json,
                source,
            ),
        )
        return True

    def write_confidence_log(
        self,
        book_id: str,
        window_id: str,
        total_dialogue: int,
        high_conf: int,
        low_conf: int,
        characters_seen: list[str],
        has_hard_subtitles: bool,
        enrichment_triggered: bool,
    ) -> bool:
        """Write a single window confidence assessment to vlm_confidence_log.

        Returns True on success, False when DB is unavailable.
        """
        if not self.is_available:
            return False

        table = _t(self._schema, "vlm_confidence_log")
        sql = f"""
            INSERT INTO {table} (
                book_id, window_id, total_dialogue, high_conf,
                low_conf, characters_seen, has_hard_subtitles,
                enrichment_triggered
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        self._execute(
            sql,
            (
                book_id,
                window_id,
                total_dialogue,
                high_conf,
                low_conf,
                characters_seen,
                has_hard_subtitles,
                enrichment_triggered,
            ),
        )
        return True

    def _ensure_vlm_confidence_log_table(self) -> None:
        """Create vlm_confidence_log table if not exists.

        Schema: id SERIAL PK, book_id TEXT, window_id TEXT, total_dialogue INT,
        high_conf INT, low_conf INT, characters_seen TEXT[], has_hard_subtitles
        BOOLEAN, enrichment_triggered BOOLEAN, created_at TIMESTAMPTZ.
        """
        if self._conn is None:
            return
        try:
            table = _t(self._schema, "vlm_confidence_log")
            with self._conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id                     SERIAL PRIMARY KEY,
                        book_id                TEXT NOT NULL,
                        window_id              TEXT NOT NULL,
                        total_dialogue         INT,
                        high_conf              INT,
                        low_conf               INT,
                        characters_seen        TEXT[],
                        has_hard_subtitles     BOOLEAN DEFAULT false,
                        enrichment_triggered   BOOLEAN DEFAULT false,
                        created_at             TIMESTAMPTZ DEFAULT now()
                    )
                """)
                self._conn.commit()
            logger.info("Ensured table: %s", table)
        except Exception as exc:
            logger.warning("_ensure_vlm_confidence_log_table failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass

    def _ensure_highlight_skill_evolution_table(self) -> None:
        """Create highlight_skill_evolution table if not exists.

        Schema: id SERIAL PK, skill_version TEXT, window_id TEXT,
        api_highlight JSONB, vlm_miss_reason TEXT, skill_update TEXT,
        created_at TIMESTAMPTZ.
        """
        if self._conn is None:
            return
        try:
            table = _t(self._schema, "highlight_skill_evolution")
            with self._conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id               SERIAL PRIMARY KEY,
                        skill_version    TEXT NOT NULL,
                        window_id        TEXT NOT NULL,
                        api_highlight    JSONB DEFAULT '{{}}',
                        vlm_miss_reason  TEXT,
                        skill_update     TEXT,
                        created_at       TIMESTAMPTZ DEFAULT now()
                    )
                """)
                self._conn.commit()
            logger.info("Ensured table: %s", table)
        except Exception as exc:
            logger.warning("_ensure_highlight_skill_evolution_table failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass

    def _ensure_highlight_skill_versions_table(self) -> None:
        """Create highlight_skill_versions table if not exists.

        Tracks version history for highlight recognition skill evolution.
        Each evolution creates a new version entry with the full skill content.
        """
        try:
            self._ensure_connection()
            table = _t(self._schema, "highlight_skill_versions")
            self._conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    id              SERIAL PRIMARY KEY,
                    book_id         TEXT,
                    version         TEXT NOT NULL,
                    skill_content   TEXT NOT NULL,
                    parent_version  TEXT,
                    changes_summary JSONB DEFAULT '{{}}',
                    is_active       BOOLEAN DEFAULT false,
                    created_at      TIMESTAMPTZ DEFAULT now()
                )
            """)
            self._conn.execute(f"""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_skill_version
                ON {table} (COALESCE(book_id, ''))
                WHERE is_active = true
            """)
            self._conn.commit()
            logger.info("Ensured table: %s", table)
        except Exception as exc:
            logger.warning("_ensure_highlight_skill_versions_table failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass

    def record_highlight_evolution(
        self,
        skill_version: str,
        window_id: str,
        api_highlight: dict[str, Any],
        vlm_miss_reason: str,
        skill_update: str,
    ) -> bool:
        """Record a single highlight evolution event to highlight_skill_evolution.

        Returns True on success, False when DB is unavailable.
        """
        if not self.is_available:
            return False
        import json

        table = _t(self._schema, "highlight_skill_evolution")
        sql = f"""
            INSERT INTO {table} (
                skill_version, window_id, api_highlight,
                vlm_miss_reason, skill_update, created_at
            ) VALUES (%s, %s, %s, %s, %s, now())
        """
        self._execute(
            sql,
            (
                skill_version,
                window_id,
                json.dumps(api_highlight, ensure_ascii=False),
                vlm_miss_reason,
                skill_update,
            ),
        )
        return True

    def query_highlight_evolution_empty_window(
        self,
    ) -> list[dict[str, Any]]:
        """Query highlight_skill_evolution records where window_id is NULL or empty.

        Returns a list of dicts with columns: id, skill_version, window_id,
        api_highlight, vlm_miss_reason, skill_update, created_at.
        Returns empty list when DB is unavailable.
        """
        if not self.is_available:
            return []
        table = _t(self._schema, "highlight_skill_evolution")
        sql = f"""
            SELECT * FROM {table}
            WHERE window_id IS NULL OR window_id = ''
            ORDER BY id
        """
        rows = self._execute(sql)
        return [dict(r) for r in rows]

    def update_highlight_evolution_window_id(
        self,
        record_id: int,
        window_id: str,
    ) -> bool:
        """Update the window_id for a highlight_skill_evolution record.

        Returns True on success, False when DB is unavailable.
        """
        if not self.is_available:
            return False
        table = _t(self._schema, "highlight_skill_evolution")
        sql = f"""
            UPDATE {table} SET window_id = %s WHERE id = %s
        """
        self._execute(sql, (window_id, record_id))
        return True

    def insert_highlight_skill_version(
        self,
        book_id: str | None,
        version: str,
        skill_content: str,
        parent_version: str | None,
        changes_summary: dict[str, Any],
    ) -> int | None:
        """Insert a new highlight skill version and set it as active.

        Deactivates any existing active version for the same book_id first.
        Returns the new record's id, or None if DB is unavailable.
        """
        if not self.is_available:
            return None
        import json

        table = _t(self._schema, "highlight_skill_versions")
        
        # Deactivate existing active versions
        self._execute(
            f"UPDATE {table} SET is_active = false WHERE book_id = %s",
            (book_id,),
        )
        
        # Insert new version
        sql = f"""
            INSERT INTO {table} (
                book_id, version, skill_content, parent_version,
                changes_summary, is_active, created_at
            ) VALUES (%s, %s, %s, %s, %s, true, now())
            RETURNING id
        """
        result = self._execute(
            sql,
            (
                book_id,
                version,
                skill_content,
                parent_version,
                json.dumps(changes_summary, ensure_ascii=False),
            ),
        )
        return result[0][0] if result else None

    def get_active_highlight_skill_version(
        self,
        book_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get the active highlight skill version for a book_id.

        Returns a dict with columns: id, version, skill_content, parent_version,
        changes_summary, created_at.
        Returns None if no active version exists or DB is unavailable.
        """
        if not self.is_available:
            return None
        table = _t(self._schema, "highlight_skill_versions")
        sql = f"""
            SELECT id, version, skill_content, parent_version,
                   changes_summary, created_at
            FROM {table}
            WHERE book_id = %s AND is_active = true
            LIMIT 1
        """
        rows = self._execute(sql, (book_id,))
        return dict(rows[0]) if rows else None

    def get_highlight_skill_version_history(
        self,
        book_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get version history for a book_id, ordered by created_at desc.

        Returns list of dicts with columns: version, parent_version,
        changes_summary, is_active, created_at.
        Returns empty list if DB is unavailable.
        """
        if not self.is_available:
            return []
        table = _t(self._schema, "highlight_skill_versions")
        sql = f"""
            SELECT version, parent_version, changes_summary,
                   is_active, created_at
            FROM {table}
            WHERE book_id = %s
            ORDER BY created_at DESC
            LIMIT %s
        """
        rows = self._execute(sql, (book_id, limit))
        return [dict(r) for r in rows]


    def _migrate_episodes_source_default(self) -> None:
        """Remove DEFAULT 'vlm' from episodes.source column.

        Changes the default to NULL so that episodes from API are correctly
        marked as 'api' when explicitly set, and inadvertent NULL inserts
        signal missing source data rather than silently defaulting to 'vlm'.
        """
        if self._conn is None:
            return
        table = _t(self._schema, "episodes")
        try:
            with self._conn.cursor() as cur:
                cur.execute(f"""
                    ALTER TABLE {table} ALTER COLUMN source DROP DEFAULT
                """)
                self._conn.commit()
            logger.info("Migrated episodes source default: %s", table)
        except Exception as exc:
            logger.warning("_migrate_episodes_source_default failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass

    def _migrate_subtitles_vlm_fields(self) -> None:
        """Add VLM context columns to subtitles table if not present.

        New columns: language TEXT, kind TEXT, text_zh TEXT, position TEXT.
        """
        if self._conn is None:
            return
        table = _t(self._schema, "subtitles")
        columns = [
            ("language", "TEXT"),
            ("kind", "TEXT"),
            ("text_zh", "TEXT"),
            ("position", "TEXT"),
        ]
        try:
            with self._conn.cursor() as cur:
                for col_name, col_type in columns:
                    cur.execute(f"""
                        ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_type}
                    """)
                self._conn.commit()
            logger.info("Migrated subtitles VLM fields: %s", table)
        except Exception as exc:
            logger.warning("_migrate_subtitles_vlm_fields failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass

    def _migrate_subjects_vlm_fields(self) -> None:
        """Migrate subjects table for VLM-first architecture.

        - Drops NOT NULL constraint on visual_features (makes it optional)
        - Adds first_seen_window TEXT column
        """
        if self._conn is None:
            return
        table = _t(self._schema, "subjects")
        try:
            with self._conn.cursor() as cur:
                # Make visual_features nullable
                cur.execute(f"""
                    ALTER TABLE {table} ALTER COLUMN visual_features DROP NOT NULL
                """)
                # Add first_seen_window column
                cur.execute(f"""
                    ALTER TABLE {table} ADD COLUMN IF NOT EXISTS first_seen_window TEXT
                """)
                self._conn.commit()
            logger.info("Migrated subjects VLM fields: %s", table)
        except Exception as exc:
            logger.warning("_migrate_subjects_vlm_fields failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass

    def _migrate_shots_highlight_fields(self) -> None:
        """Add highlight annotation columns to shots table if not present.

        New columns: precise_start FLOAT, precise_end FLOAT, global_rank INT,
        rank_score FLOAT, rank_criteria JSONB.
        """
        if self._conn is None:
            return
        table = _t(self._schema, "shots")
        columns = [
            ("precise_start", "FLOAT"),
            ("precise_end", "FLOAT"),
            ("global_rank", "INT"),
            ("rank_score", "FLOAT"),
            ("rank_criteria", "JSONB"),
        ]
        try:
            with self._conn.cursor() as cur:
                for col_name, col_type in columns:
                    cur.execute(f"""
                        ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_type}
                    """)
                self._conn.commit()
            logger.info("Migrated shots highlight fields: %s", table)
        except Exception as exc:
            logger.warning("_migrate_shots_highlight_fields failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass

    def _migrate_story_beats_event_fields(self) -> None:
        """Add event linkage columns to story_beats table if not present.

        New columns: event_id TEXT, function TEXT, temporal_mode TEXT,
        merged_into_event TEXT.
        """
        if self._conn is None:
            return
        table = _t(self._schema, "story_beats")
        columns = [
            ("event_id", "TEXT"),
            ("function", "TEXT"),
            ("temporal_mode", "TEXT"),
            ("merged_into_event", "TEXT"),
        ]
        try:
            with self._conn.cursor() as cur:
                for col_name, col_type in columns:
                    cur.execute(f"""
                        ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_type}
                    """)
                self._conn.commit()
            logger.info("Migrated story_beats event fields: %s", table)
        except Exception as exc:
            logger.warning("_migrate_story_beats_event_fields failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass

    def _ensure_highlight_candidates_table(self) -> None:
        """Create highlight_candidates table if not exists.

        Schema: id SERIAL PK, book_id TEXT NOT NULL, episode_id INT NOT NULL,
        start_time FLOAT NOT NULL, end_time FLOAT NOT NULL, score FLOAT,
        reason TEXT, candidate_type TEXT, source TEXT NOT NULL,
        metadata JSONB, created_at TIMESTAMPTZ DEFAULT now().
        """
        if self._conn is None:
            return
        try:
            table = _t(self._schema, "highlight_candidates")
            with self._conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id             SERIAL PRIMARY KEY,
                        book_id        TEXT NOT NULL,
                        episode_id     INT NOT NULL,
                        start_time     FLOAT NOT NULL,
                        end_time       FLOAT NOT NULL,
                        score          FLOAT,
                        reason         TEXT,
                        candidate_type TEXT,
                        source         TEXT NOT NULL,
                        metadata       JSONB DEFAULT '{{}}',
                        created_at     TIMESTAMPTZ DEFAULT now()
                    )
                """)
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_highlight_candidates_book_ep
                    ON {table} (book_id, episode_id, source)
                """)
                self._conn.commit()
            logger.info("Ensured table: %s", table)
        except Exception as exc:
            logger.warning("_ensure_highlight_candidates_table failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass

    def _ensure_span_candidates_table(self) -> None:
        """Create span_candidates table if not exists.

        Schema: id SERIAL PK, book_id TEXT NOT NULL, episode_id INT NOT NULL,
        candidate_id TEXT NOT NULL, start_time FLOAT NOT NULL,
        end_time FLOAT NOT NULL, span_type TEXT, confidence FLOAT,
        features JSONB, source TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now().
        """
        if self._conn is None:
            return
        try:
            table = _t(self._schema, "span_candidates")
            with self._conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id             SERIAL PRIMARY KEY,
                        book_id        TEXT NOT NULL,
                        episode_id     INT NOT NULL,
                        candidate_id   TEXT NOT NULL,
                        start_time     FLOAT NOT NULL,
                        end_time       FLOAT NOT NULL,
                        span_type      TEXT,
                        confidence     FLOAT,
                        features       JSONB DEFAULT '{{}}',
                        source         TEXT NOT NULL,
                        created_at     TIMESTAMPTZ DEFAULT now()
                    )
                """)
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_span_candidates_book_ep
                    ON {table} (book_id, episode_id, source)
                """)
                self._conn.commit()
            logger.info("Ensured table: %s", table)
        except Exception as exc:
            logger.warning("_ensure_span_candidates_table failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass

    def _ensure_final_clips_table(self) -> None:
        """Create final_clips table if not exists.

        Schema: id SERIAL PK, book_id TEXT NOT NULL, episode_id INT NOT NULL,
        clip_id TEXT NOT NULL, start_time FLOAT NOT NULL,
        end_time FLOAT NOT NULL, title TEXT, description TEXT,
        tags TEXT[], score FLOAT, clip_order INT, source TEXT NOT NULL,
        metadata JSONB DEFAULT '{{}}', created_at TIMESTAMPTZ DEFAULT now().
        """
        if self._conn is None:
            return
        try:
            table = _t(self._schema, "final_clips")
            with self._conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id             SERIAL PRIMARY KEY,
                        book_id        TEXT NOT NULL,
                        episode_id     INT NOT NULL,
                        clip_id        TEXT NOT NULL,
                        start_time     FLOAT NOT NULL,
                        end_time       FLOAT NOT NULL,
                        title          TEXT,
                        description    TEXT,
                        tags           TEXT[],
                        score          FLOAT,
                        clip_order     INT,
                        source         TEXT NOT NULL,
                        metadata       JSONB DEFAULT '{{}}',
                        created_at     TIMESTAMPTZ DEFAULT now()
                    )
                """)
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_final_clips_book_ep
                    ON {table} (book_id, episode_id, source)
                """)
                self._conn.commit()
            logger.info("Ensured table: %s", table)
        except Exception as exc:
            logger.warning("_ensure_final_clips_table failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass

    def _ensure_story_events_table(self) -> None:
        """Create story_events table if not exists.

        Schema: id SERIAL PK, book_id TEXT NOT NULL, episode_id INT NOT NULL,
        event_id TEXT NOT NULL, event_type TEXT, summary TEXT,
        participants TEXT[], cause TEXT, effect TEXT, start_time FLOAT,
        end_time FLOAT, temporal_mode TEXT, importance FLOAT,
        source TEXT NOT NULL, metadata JSONB DEFAULT '{{}}',
        created_at TIMESTAMPTZ DEFAULT now().
        """
        if self._conn is None:
            return
        try:
            table = _t(self._schema, "story_events")
            with self._conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id             SERIAL PRIMARY KEY,
                        book_id        TEXT NOT NULL,
                        episode_id     INT NOT NULL,
                        event_id       TEXT NOT NULL,
                        event_type     TEXT,
                        summary        TEXT,
                        participants   TEXT[],
                        cause          TEXT,
                        effect         TEXT,
                        start_time     FLOAT,
                        end_time       FLOAT,
                        temporal_mode  TEXT,
                        importance     FLOAT,
                        source         TEXT NOT NULL,
                        metadata       JSONB DEFAULT '{{}}',
                        created_at     TIMESTAMPTZ DEFAULT now()
                    )
                """)
                cur.execute(f"""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_story_events_unique
                    ON {table} (book_id, episode_id, event_id, source)
                """)
                self._conn.commit()
            logger.info("Ensured table: %s", table)
        except Exception as exc:
            logger.warning("_ensure_story_events_table failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass

    def _ensure_story_threads_table(self) -> None:
        """Create story_threads table if not exists.

        Schema: id SERIAL PK, book_id TEXT NOT NULL, thread_id TEXT NOT NULL,
        thread_name TEXT NOT NULL, description TEXT, thread_type TEXT,
        importance FLOAT, status TEXT, first_episode INT, last_episode INT,
        source TEXT NOT NULL, metadata JSONB DEFAULT '{{}}',
        created_at TIMESTAMPTZ DEFAULT now().
        """
        if self._conn is None:
            return
        try:
            table = _t(self._schema, "story_threads")
            with self._conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id             SERIAL PRIMARY KEY,
                        book_id        TEXT NOT NULL,
                        thread_id      TEXT NOT NULL,
                        thread_name    TEXT NOT NULL,
                        description    TEXT,
                        thread_type    TEXT,
                        importance     FLOAT,
                        status         TEXT,
                        first_episode  INT,
                        last_episode   INT,
                        source         TEXT NOT NULL,
                        metadata       JSONB DEFAULT '{{}}',
                        created_at     TIMESTAMPTZ DEFAULT now()
                    )
                """)
                cur.execute(f"""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_story_threads_unique
                    ON {table} (book_id, thread_id, source)
                """)
                self._conn.commit()
            logger.info("Ensured table: %s", table)
        except Exception as exc:
            logger.warning("_ensure_story_threads_table failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass

    def _ensure_story_thread_beats_table(self) -> None:
        """Create story_thread_beats table if not exists.

        Schema: id SERIAL PK, book_id TEXT NOT NULL, episode_id INT NOT NULL,
        thread_id TEXT NOT NULL, beat_id TEXT NOT NULL, event_id TEXT,
        beat_summary TEXT, beat_order INT, start_time FLOAT, end_time FLOAT,
        source TEXT NOT NULL, metadata JSONB DEFAULT '{{}}',
        created_at TIMESTAMPTZ DEFAULT now().
        """
        if self._conn is None:
            return
        try:
            table = _t(self._schema, "story_thread_beats")
            with self._conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id             SERIAL PRIMARY KEY,
                        book_id        TEXT NOT NULL,
                        episode_id     INT NOT NULL,
                        thread_id      TEXT NOT NULL,
                        beat_id        TEXT NOT NULL,
                        event_id       TEXT,
                        beat_summary   TEXT,
                        beat_order     INT,
                        start_time     FLOAT,
                        end_time       FLOAT,
                        source         TEXT NOT NULL,
                        metadata       JSONB DEFAULT '{{}}',
                        created_at     TIMESTAMPTZ DEFAULT now()
                    )
                """)
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_story_thread_beats_book_ep
                    ON {table} (book_id, episode_id, thread_id, source)
                """)
                self._conn.commit()
            logger.info("Ensured table: %s", table)
        except Exception as exc:
            logger.warning("_ensure_story_thread_beats_table failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass

    def _ensure_story_facts_table(self) -> None:
        """Create story_facts table if not exists.

        Schema: id SERIAL PK, book_id TEXT NOT NULL, fact_id TEXT NOT NULL,
        fact_type TEXT, subject TEXT, fact_text TEXT NOT NULL,
        confidence FLOAT, first_episode INT, last_episode INT,
        related_events TEXT[], source TEXT NOT NULL,
        metadata JSONB DEFAULT '{{}}', created_at TIMESTAMPTZ DEFAULT now().
        """
        if self._conn is None:
            return
        try:
            table = _t(self._schema, "story_facts")
            with self._conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id             SERIAL PRIMARY KEY,
                        book_id        TEXT NOT NULL,
                        fact_id        TEXT NOT NULL,
                        fact_type      TEXT,
                        subject        TEXT,
                        fact_text      TEXT NOT NULL,
                        confidence     FLOAT,
                        first_episode  INT,
                        last_episode   INT,
                        related_events TEXT[],
                        source         TEXT NOT NULL,
                        metadata       JSONB DEFAULT '{{}}',
                        created_at     TIMESTAMPTZ DEFAULT now()
                    )
                """)
                cur.execute(f"""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_story_facts_unique
                    ON {table} (book_id, fact_id, source)
                """)
                self._conn.commit()
            logger.info("Ensured table: %s", table)
        except Exception as exc:
            logger.warning("_ensure_story_facts_table failed: %s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass


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
