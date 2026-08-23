"""Structural proof that the MVP migration owns every runtime table it uses."""

from pathlib import Path

MIGRATIONS = Path("packages/autocut-kernel/migrations")


def test_runtime_core_migration_declares_closed_durable_relations() -> None:
    sql = (MIGRATIONS / "0001_runtime_core.sql").read_text()
    for relation in (
        "runtime.jobs",
        "runtime.command_slots",
        "runtime.command_receipts",
        "runtime.artifact_sets",
        "runtime.artifacts",
        "runtime.artifact_set_members",
        "runtime.logical_heads",
    ):
        assert f"CREATE TABLE {relation}" in sql
    assert "UNIQUE (job_id, idempotency_key)" in sql
    assert "state IN ('pending', 'running', 'succeeded', 'denied', 'failed')" in sql
    assert "runtime_artifacts_scope_revision_key" in sql
    assert "successful receipt must reference its command slot artifact set" in sql
    assert "artifact set members are incomplete" in sql


def test_follow_up_migration_binds_head_to_its_exact_scoped_revision() -> None:
    sql = (MIGRATIONS / "0002_runtime_core_constraints.sql").read_text()
    assert "runtime.assert_head_matches_artifact" in sql
    assert "runtime_logical_head_exact_target_check" in sql
