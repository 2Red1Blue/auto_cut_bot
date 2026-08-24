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


def test_vlm_generation_migration_closes_blob_attempt_and_finalizer_relations() -> None:
    sql = (MIGRATIONS / "0003_vlm_generation_and_run_finalization.sql").read_text()

    for relation in (
        "storage.blob_objects",
        "storage.blob_claims",
        "runtime.generation_attempts",
    ):
        assert f"CREATE TABLE {relation}" in sql
    assert "content_hash = 'sha256:' || encode(sha256(content_bytes), 'hex')" in sql
    assert "immutable blob objects and claims cannot be mutated" in sql
    assert "generation attempts must begin as a clean reservation" in sql
    assert "UNIQUE (provider_id, provider_idempotency_key)" in sql
    assert "request_payload_object_id uuid NOT NULL" in sql
    assert "generation request payload must be an immutable blob claimed by its Job" in sql
    assert "invalid generation attempt state transition" in sql
    assert "generation raw response must be an immutable blob claimed by its Job" in sql
    assert "committed generation must bind its exact command Receipt and ArtifactSet" in sql
    assert "runtime_one_run_finalizer_per_job" in sql
    assert "terminal Job requires exactly one matching FinalizeRunOutcome receipt" in sql
    assert "successful FinalizeRunOutcome requires exactly one run_outcome member" in sql
    assert "terminal Job cannot accept a fresh command slot" in sql


def test_provider_media_migration_binds_file_id_to_content_and_policy() -> None:
    sql = (MIGRATIONS / "0004_provider_media_objects.sql").read_text()

    assert "runtime.provider_media_objects" in sql
    assert "UNIQUE (provider_id, content_hash, preprocess_policy_hash, generation)" in sql
    assert "runtime_one_live_provider_media_identity" in sql
    assert "provider file identity is immutable once known" in sql
    assert "provider media objects must begin as a clean reservation" in sql


def test_ark_recovery_migration_adds_scope_leases_and_immediate_request_id_cas() -> None:
    sql = (MIGRATIONS / "0006_ark_provider_recovery.sql").read_text()

    assert "provider_scope_fingerprint" in sql
    assert "lease_token" in sql
    assert "lease_expires_at" in sql
    assert "audit_expires_at" in sql
    assert "runtime_one_live_provider_media_identity" in sql
    assert "provider_media_scoped_generation_unique" in sql
    assert "provider_media_scoped_file_id_unique" in sql
    assert "generation provider request identity is immutable once known" in sql
    assert "OLD.state = 'dispatched' AND NEW.state = 'dispatched'" in sql


def test_vlm_bounded_retry_migration_adds_chain_lease_and_receipt_proof() -> None:
    sql = (MIGRATIONS / "0009_vlm_bounded_retry.sql").read_text()

    assert "attempt_ordinal" in sql
    assert "previous_attempt_id" in sql
    assert "retry_policy_hash" in sql
    assert "max_attempts" in sql
    assert "failure_disposition" in sql
    assert "dispatch_lease_token" in sql
    assert "dispatch_lease_expires_at" in sql
    assert "not_before_at" in sql
    assert "generation_attempt_budget_bounded" in sql
    assert "generation_attempt_slot_ordinal_unique" in sql
    assert "generation_attempt_previous_unique" in sql
    assert "runtime.generation_receipt_attempts" in sql
    assert "generation Receipt must bind the complete contiguous Attempt chain" in sql
    assert "generation retry predecessor must be the exact retryable prior attempt" in sql


def test_pipeline_retry_profile_migration_closes_v2_and_preserves_v1() -> None:
    sql = (MIGRATIONS / "0010_pipeline_retry_profile.sql").read_text()

    assert "pipeline-execution-profile-v1" in sql
    assert "pipeline-execution-profile-v2" in sql
    assert "generation_retry_policy" in sql
    assert "generation-retry-v1" in sql
    assert "jsonb_array_length" in sql
    assert "max_attempts') ~ '^[1-3]$'" in sql
    assert "v1 runs remain frozen at one attempt" in sql


def test_generation_retry_schedule_migration_makes_backoff_a_durable_identity() -> None:
    sql = (MIGRATIONS / "0011_generation_retry_schedule.sql").read_text()

    assert "retry_backoff_seconds" in sql
    assert "generation_attempt_retry_schedule_exact" in sql
    assert "generation retry schedule is immutable" in sql
