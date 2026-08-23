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
    # set_hash is scoped per job, not globally unique
    assert "UNIQUE (job_id, set_hash)" in sql


def test_follow_up_migration_binds_head_to_its_exact_scoped_revision() -> None:
    sql = (MIGRATIONS / "0002_runtime_core_constraints.sql").read_text()
    assert "runtime.assert_head_matches_artifact" in sql
    assert "runtime_logical_head_exact_target_check" in sql
    # Immutability guards
    assert "committed receipts are immutable" in sql
    assert "committed artifact sets are immutable" in sql
    assert "committed artifacts are immutable" in sql
    assert "committed artifact set members are immutable" in sql
    # Cross-table integrity
    assert "artifact job must match its artifact set job" in sql
    assert "runtime.assert_artifact_job_matches_set" in sql
    assert "runtime_artifact_job_matches_set_check" in sql
    # Deferred command lifecycle and monotonic terminal state guards.
    assert "runtime.assert_command_slot_receipt_lifecycle" in sql
    assert "terminal command slot must have exactly one matching receipt" in sql
    assert "pending or running command slot must not have a receipt" in sql
    assert "terminal jobs are immutable" in sql
    assert "terminal command slots are immutable" in sql
