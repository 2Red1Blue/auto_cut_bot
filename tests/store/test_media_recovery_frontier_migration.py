from pathlib import Path

MIGRATION = Path(
    "packages/autocut-kernel/migrations/0053_media_preflight_recovery_frontier.sql"
)


def test_media_recovery_frontier_migration_is_minimal_and_append_only() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TABLE runtime.media_preflight_recovery_frontiers" in sql
    assert "CREATE TABLE runtime.media_preflight_recovery_entries" in sql
    assert sql.count("CREATE TABLE") == 2
    assert "PRIMARY KEY (frontier_id, episode_index)" in sql
    assert "media recovery entries are append-only" in sql
    assert "PrepareRuntimeTimedMediaEvidence@1.0.0" in sql
    assert "runtime_timed_speech_capability_admission" in sql
    assert "media recovery frontier cannot close with partial coverage" in sql
    assert "media recovery final batch is not an exact succeeded closure" in sql
    # Deterministic member-layout checks may order by the immutable ordinal;
    # the migration must not discover a predecessor through a mutable
    # latest/head query.
    assert "ORDER BY member.ordinal" in sql
    assert "ORDER BY created_at DESC" not in sql
    assert "latest" not in sql.lower()
    assert "logical_heads" not in sql
