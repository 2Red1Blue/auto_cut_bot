from pathlib import Path

MIGRATION = Path("packages/autocut-kernel/migrations/0005_pipeline_http_run_control.sql")


def test_pipeline_http_run_migration_owns_durable_control_plane() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    for relation in (
        "runtime.pipeline_runs",
        "runtime.pipeline_commands",
        "runtime.pipeline_run_receipts",
        "runtime.pipeline_run_outbox",
    ):
        assert f"CREATE TABLE {relation}" in sql
    assert "UNIQUE (idempotency_key)" in sql
    assert "pipeline command must begin pending at version zero" in sql
    assert "terminal pipeline command requires one matching Receipt" in sql
    assert "pipeline outbox transition requires exact version increment" in sql
    assert "pipeline command transition requires exact version increment" in sql
    assert "REVOKE ALL ON runtime.pipeline_run_outbox FROM PUBLIC" in sql
