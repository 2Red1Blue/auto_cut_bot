from pathlib import Path

MIGRATION = Path("packages/autocut-kernel/migrations/0005_pipeline_http_run_control.sql")
WORKER_MIGRATION = Path("packages/autocut-kernel/migrations/0007_pipeline_stage_worker.sql")


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


def test_pipeline_worker_migration_closes_ordered_blocking_and_heartbeat_states() -> None:
    sql = WORKER_MIGRATION.read_text(encoding="utf-8")

    assert "blocking_command_id uuid" in sql
    assert "'indeterminate', 'blocked'" in sql
    assert "OLD.state = 'pending' AND NEW.state IN ('running', 'blocked')" in sql
    assert "OLD.state = 'running' AND NEW.state IN" in sql
    assert "'running', 'succeeded', 'denied', 'failed', 'indeterminate'" in sql
    assert "non-Receipt pipeline command cannot have a Receipt" in sql
    assert "OLD.state = 'leased' AND NEW.state IN ('leased', 'pending', 'consumed')" in sql
    assert "md5(run.run_id || ':vlm')::uuid" in sql
    assert "ON CONFLICT DO NOTHING" in sql
    assert "runtime.assert_pipeline_blocker_relation" in sql
    assert "blocker.run_id = NEW.run_id" in sql
    assert "blocker.ordinal < NEW.ordinal" in sql
    assert "blocker.state IN ('denied', 'failed')" in sql
    assert "DEFERRABLE INITIALLY DEFERRED" in sql
