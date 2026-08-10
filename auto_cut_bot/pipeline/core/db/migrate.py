"""DB auto-migrator — ensures the ``autocut`` schema is present and up-to-date.

Applied automatically at pipeline startup (via ``StageDBClient`` or
``ensure_schema``). Idempotent and safe to run on every startup:

  1. Connects to PostgreSQL (best-effort; no-ops when DB unavailable).
  2. Ensures the ``schema_migrations`` tracking table exists.
  3. Scans ``migrations/*.sql``, applies each pending migration file in
     filename order (000_base_schema.sql, 001_provenance.sql, ...), records
     each applied version.
  4. Records applied versions to avoid re-running on next startup.

Schema drift (e.g. missing columns that newer client code expects) is handled
by per-migration ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` statements.

Usage::

    from auto_cut_bot.pipeline.core.db.migrate import ensure_schema
    ensure_schema(db_url="postgresql://...", schema="autocut")
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autocut_core.logging import get_logger

logger = get_logger(__name__)

# Optional driver — mirror client.py behavior. If psycopg2 missing, no-op.
try:
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

    _HAS_PSYCOPG2 = True
except ImportError:  # pragma: no cover — optional dependency
    _HAS_PSYCOPG2 = False

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_TABLE_MIGRATIONS = "schema_migrations"


def _migration_files(migrations_dir: Path | None = None) -> list[Path]:
    """Return migration .sql files sorted by filename (version order)."""
    base = migrations_dir or _MIGRATIONS_DIR
    if not base.is_dir():
        return []
    return sorted(base.glob("*.sql"))


def _ensure_migrations_table(conn: Any, schema: str) -> None:
    """Create the schema_migrations tracking table if missing."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {schema}.{_TABLE_MIGRATIONS} (
                version     text PRIMARY KEY,
                applied_at  timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    conn.commit()


def _applied_versions(conn: Any, schema: str) -> set[str]:
    """Return the set of already-applied migration versions."""
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT version FROM {schema}.{_TABLE_MIGRATIONS}")
            return {row[0] for row in cur.fetchall()}
    except Exception as exc:
        logger.warning("Could not read applied migrations: %s", exc)
        return set()


def _apply_file(conn: Any, schema: str, path: Path) -> None:
    """Execute a single migration .sql file against the schema.

    The schema name is injected into the SQL by replacing the literal
    ``autocut.`` prefix placeholder with the configured schema, so the same
    migration files work for any schema.
    """
    sql = path.read_text(encoding="utf-8")
    # Migrations reference the default schema name `autocut`; rewrite to the
    # configured schema when it differs.
    if schema != "autocut":
        sql = sql.replace("autocut.", f"{schema}.")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _record_version(conn: Any, schema: str, version: str) -> None:
    """Record an applied migration version."""
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema}.{_TABLE_MIGRATIONS} (version) VALUES (%s)",
            (version,),
        )
    conn.commit()


def ensure_schema(
    db_url: str | None,
    schema: str = "autocut",
    migrations_dir: Path | None = None,
) -> dict[str, Any]:
    """Apply pending migrations to ensure the schema is present and up-to-date.

    Best-effort: returns a summary dict and never raises on DB unavailability
    (so pipeline startup is not blocked when DB is down).

    Returns::
        {"applied": [version, ...], "already_applied": n, "available": [...],
         "error": None | str}
    """
    result: dict[str, Any] = {
        "applied": [],
        "already_applied": 0,
        "available": [],
        "error": None,
    }
    if not db_url or not _HAS_PSYCOPG2:
        return result

    files = _migration_files(migrations_dir)
    result["available"] = [f.stem for f in files]

    try:
        conn = psycopg2.connect(db_url)
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        try:
            _ensure_migrations_table(conn, schema)
            applied = _applied_versions(conn, schema)
            for f in files:
                version = f.stem
                if version in applied:
                    result["already_applied"] += 1
                    continue
                try:
                    _apply_file(conn, schema, f)
                    _record_version(conn, schema, version)
                    result["applied"].append(version)
                    logger.info("Applied migration %s to schema %s", version, schema)
                except Exception as exc:
                    logger.warning("Migration %s failed: %s", version, exc)
                    result["error"] = f"{version}: {exc}"
                    break
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("ensure_schema failed: %s", exc)
        result["error"] = str(exc)

    return result


__all__ = ["ensure_schema"]
