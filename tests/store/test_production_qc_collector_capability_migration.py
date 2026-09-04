"""Static closure checks for the QC collector capability migration."""

from __future__ import annotations

import re
from pathlib import Path

MIGRATION = Path("packages/autocut-kernel/migrations/0059_production_qc_collector_capability.sql")


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_refuses_preexisting_protected_rows() -> None:
    sql = _sql()
    assert "0059 refuses pre-existing protected QC collector capability artifacts" in sql
    assert "0059 refuses a pre-existing QC collector capability relation" in sql
    assert "namespace = 'autocut_authority'" in sql
    assert "scope_kind = 'production_qc_collector_capability'" in sql


def test_migration_defines_immutable_capability_relation() -> None:
    sql = _sql()
    assert "CREATE TABLE runtime.production_qc_collector_capabilities (" in sql
    for column in (
        "profile_id",
        "qc_runner_identity_sha256",
        "policy_source_sha256",
        "registry_snapshot_sha256",
        "collector_registry_sha256",
        "required_check_set_version",
        "runner_schema_version",
        "fixed_environment_sha256",
        "ffmpeg_executable_sha256",
        "ffmpeg_executable_byte_length",
        "ffmpeg_version_output_sha256",
        "ffprobe_executable_sha256",
        "ffprobe_executable_byte_length",
        "ffprobe_version_output_sha256",
        "capability_request_json",
        "capability_request_sha256",
        "measurement_member_sha256",
        "capability_member_sha256",
        "decision",
        "receipt_id",
        "artifact_set_id",
        "command_slot_id",
        "authority_revision",
        "authority_bundle_sha256",
        "source_commit",
        "inventory_commit",
        "lock_commit",
        "accepted_at",
    ):
        assert re.search(rf"^\s+{column} ", sql, re.MULTILINE), f"missing column {column}"


def test_migration_logical_identity_is_the_complete_tuple() -> None:
    sql = _sql()
    assert (
        "UNIQUE (profile_id, qc_runner_identity_sha256, "
        "policy_source_sha256, registry_snapshot_sha256)" in sql
    )
    assert "PRIMARY KEY (namespace, scope_kind, scope_key)" in sql
    # Each authority reference is unique so one accepted decision owns exactly
    # one receipt, set and command slot.
    for unique_column in ("receipt_id uuid NOT NULL UNIQUE", "artifact_set_id uuid NOT NULL UNIQUE", "command_slot_id uuid NOT NULL UNIQUE"):
        assert unique_column in sql


def test_migration_check_set_and_runner_schema_are_registered_values() -> None:
    sql = _sql()
    assert "required_check_set_version = 'production-av-qc-v1'" in sql
    assert "runner_schema_version = 'production-qc-runner-v1'" in sql
    assert "decision = 'accepted'" in sql


def test_migration_trigger_is_insert_only_and_deterministic() -> None:
    sql = _sql()
    assert "BEFORE INSERT OR UPDATE OR DELETE ON runtime.production_qc_collector_capabilities" in sql
    assert "production QC collector capabilities are insert-only" in sql
    assert "production QC collector capabilities are durable and cannot be deleted" in sql
    assert "production QC collector capability scope key is not deterministic" in sql
    assert "capability request digest does not match its stored JSON" in sql
    # The scope key must be the deterministic projection of the identity tuple.
    assert "'production_qc_collector_capability:'" in sql
    assert "substring(NEW.policy_source_sha256 from 8)" in sql
    assert "substring(NEW.registry_snapshot_sha256 from 8)" in sql
    assert "substring(NEW.qc_runner_identity_sha256 from 8)" in sql


def test_migration_artifact_guard_binds_exact_validator_provenance() -> None:
    sql = _sql()
    assert "BEFORE INSERT OR UPDATE OR DELETE ON runtime.artifacts" in sql
    assert "slot_name <> 'AcceptProductionRenderQcCollectorCapability@1'" in sql
    assert "writer_profile <> 'authority'" in sql
    assert "production QC collector capability artifacts are immutable" in sql
    assert "require the exact validator provenance" in sql
    assert "NEW.artifact_type = 'production_qc_collector_measurement'" in sql
    assert "NEW.artifact_type = 'production_qc_collector_capability'" in sql
    assert "NEW.logical_id = 'measurement'" in sql
    assert "NEW.logical_id = 'decision'" in sql


def test_migration_members_and_hashes_cannot_coincide() -> None:
    sql = _sql()
    assert "CHECK (measurement_member_sha256 <> capability_member_sha256)" in sql
    assert sql.count("repeat('0', 64)") >= 10


def test_migration_runs_inside_one_transaction() -> None:
    sql = _sql()
    assert sql.startswith("--")
    assert "BEGIN;" in sql
    assert sql.rstrip().endswith("COMMIT;")
