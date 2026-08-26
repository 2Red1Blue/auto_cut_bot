"""Static contract checks for the isolated shadow-local recovery migration.

PostgreSQL transition races require a disposable database acceptance run in the
next Store slice.  These checks keep the immutable SQL grammar visible without
pretending that collection-only tests prove database behavior.
"""

from pathlib import Path

MIGRATION = Path(
    "packages/autocut-kernel/migrations/0023_shadow_local_calibration_measurement_recovery.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_local_recovery_uses_isolated_tables_protocol_and_non_authority_grammar() -> None:
    sql = _sql()

    assert "runtime.shadow_local_calibration_measurement_attempts" in sql
    assert "runtime.shadow_local_calibration_measurement_members" in sql
    assert "MeasureShadowLocalCalibrationCommand@1" in sql
    assert "shadow-local-calibration-measurement-v1" in sql
    assert "calibration_record" not in sql
    assert "registry_anchor" not in sql
    assert "installed_profile" not in sql
    assert "shadow_calibration_measurement_attempts" not in sql.replace(
        "shadow_local_calibration_measurement_attempts", ""
    )


def test_local_recovery_sql_closes_hashes_types_and_member_states() -> None:
    sql = _sql()

    for field in (
        "plan_hash text NOT NULL CHECK (plan_hash ~ '^sha256:[0-9a-f]{64}$')",
        "case_sha256 text NOT NULL CHECK (case_sha256 ~ '^sha256:[0-9a-f]{64}$')",
        "request_sha256 text NOT NULL CHECK (request_sha256 ~ '^sha256:[0-9a-f]{64}$')",
        "source_blob_reference_sha256 text NOT NULL CHECK",
        "binding_sha256 text NOT NULL CHECK",
        "service_profile_sha256 text NOT NULL CHECK",
        "max_response_bytes bigint NOT NULL CHECK (max_response_bytes > 0)",
    ):
        assert field in sql
    assert "('prepared', 'collecting', 'ready', 'indeterminate', 'committed', 'denied')" in sql
    assert "('pending', 'invoking', 'not_started', 'staged', 'indeterminate', 'rejected')" in sql
    assert "(OLD.state = 'pending' AND NEW.state = 'invoking')" in sql
    assert "(OLD.state = 'invoking' AND NEW.state IN ('not_started', 'staged', 'indeterminate', 'rejected'))" in sql
    assert "(OLD.state = 'collecting' AND NEW.state IN ('collecting', 'ready', 'indeterminate', 'denied'))" in sql
    assert "NEW.plan_json->>'command' IS DISTINCT FROM 'MeasureShadowLocalCalibrationCommand@1'" in sql
    assert "NEW.plan_json->>'measurement_protocol' IS DISTINCT FROM 'shadow-local-calibration-measurement-v1'" in sql
    assert "shadow-local plan must have exact closed local input fields" in sql
    assert "jsonb_object_keys(NEW.plan_json)" in sql
    assert "jsonb_object_keys(NEW.plan_json->'shadow_local_inputs')" in sql
    assert "exact version increment" in sql


def test_local_recovery_sql_binds_successors_and_staged_blob_ownership() -> None:
    sql = _sql()

    assert "UNIQUE (job_id, plan_hash, attempt_ordinal)" in sql
    assert "predecessor.version <> NEW.retry_predecessor_version" in sql
    assert "REQUEST_NOT_STARTED" in sql
    assert "shadow-local successor staged member must inherit exact prior evidence" in sql
    assert "storage.blob_claims AS claim" in sql
    assert "claim.job_id = attempt_job" in sql
    assert "busy_proof_blob_object_id" in sql
    assert "not-started shadow-local BUSY proof must be exactly claimed by its shadow Job" in sql
    assert "command_slot_id uuid NOT NULL REFERENCES runtime.command_slots" in sql
    assert "command_slot_id uuid NOT NULL UNIQUE" not in sql
    assert "predecessor.command_slot_id <> NEW.command_slot_id" in sql
    assert "shadow-local successor member must preserve complete immutable identity" in sql
    assert "NEW.case_sha256 = authorized_retry_case" in sql
    assert "attempt_number integer" in sql
    assert "INTO attempt_job, attempt_number, predecessor_id, authorized_retry_case" in sql
    assert "DEFERRABLE INITIALLY DEFERRED" in sql
