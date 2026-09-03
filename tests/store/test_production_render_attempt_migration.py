"""Static closure checks for production Render attempt recovery migration."""

from pathlib import Path

MIGRATION = Path("packages/autocut-kernel/migrations/0055_production_render_attempt_recovery.sql")


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_render_attempt_binds_exact_slot_recipe_and_render_identities() -> None:
    sql = _sql()

    assert "CREATE TABLE runtime.production_render_attempts" in sql
    assert "command_slot_id uuid NOT NULL UNIQUE" in sql
    assert "RenderProductionRecipeCommand@1" in sql
    assert "CompileProductionRecipeCommand@1" in sql
    for column in (
        "recipe_receipt_id",
        "recipe_artifact_set_id",
        "recipe_member_ordinal",
        "recipe_namespace",
        "recipe_scope_kind",
        "recipe_scope_key",
        "recipe_artifact_type",
        "recipe_logical_id",
        "recipe_revision",
        "recipe_content_hash",
        "render_plan_sha256",
        "render_profile_sha256",
        "renderer_identity_sha256",
        "max_output_bytes",
    ):
        assert column in sql
    assert "recipe_artifact.artifact_id = recipe_member.artifact_id" in sql
    assert "recipe_receipt.outcome = 'succeeded'" in sql
    assert "render_job.profile IN ('shadow', 'production')" in sql
    assert "recipe_job.profile IN ('shadow', 'production')" in sql
    assert "recipe_artifact.scope_key = recipe_job.job_key" in sql
    assert "render_slot.state = CASE" in sql
    assert "recipe_member.ordinal < recipe_set.member_count - 1" in sql
    assert "report_artifact.artifact_type = 'physical_edit_compilation_report'" in sql
    assert "admission_artifact.artifact_type = 'physical_edit_admission'" in sql
    assert "NOT EXISTS (" in sql


def test_render_attempt_state_and_lease_are_closed_and_fenced() -> None:
    sql = _sql()

    assert "state IN ('reserved', 'rendering', 'rendered', 'committed', 'denied', 'failed')" in sql
    assert "NEW.state <> 'reserved' OR NEW.version <> 0" in sql
    assert "NEW.version <> OLD.version + 1" in sql
    assert "production render attempt identity is immutable" in sql
    assert "OLD.lease_expires_at > clock_timestamp()" in sql
    assert "transaction_timestamp()" not in sql
    assert "NEW.lease_expires_at <= OLD.lease_expires_at" in sql
    assert "OLD.lease_expires_at <= clock_timestamp()" in sql
    assert "active production render lease cannot be taken over" in sql
    assert "expired production render lease cannot resolve an attempt" in sql
    assert "active pre-render rejection cannot claim production output bytes" in sql
    assert "reserved production render rejection cannot claim output bytes" in sql
    assert "BEFORE INSERT OR UPDATE OR DELETE" in sql
    assert "production render attempts are durable and cannot be deleted" in sql
    assert "terminal production render attempts are immutable" in sql


def test_rendered_and_terminal_shapes_require_exact_blob_and_receipt_closure() -> None:
    sql = _sql()

    assert "output_object_id uuid REFERENCES storage.blob_objects" in sql
    assert "output_blob.storage_kind = 's3_compatible'" in sql
    assert "output_blob.byte_length <= NEW.max_output_bytes" in sql
    assert "output_blob.media_type = 'video/mp4'" in sql
    assert "output_claim.job_id = NEW.job_id" in sql
    assert "terminal_receipt.command_slot_id = NEW.command_slot_id" in sql
    assert "terminal_receipt.outcome = 'succeeded'" in sql
    assert "terminal_receipt.outcome = NEW.state" in sql
    assert "terminal_receipt.failure_detail = NEW.failure_detail" in sql
    assert "NEW.state IN ('committed', 'denied', 'failed')" in sql
    assert "(output_object_id IS NOT NULL AND rendered_at IS NOT NULL)" in sql
    assert "jsonb_typeof(failure_detail) = 'object'" in sql
    assert "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW" in sql


def test_render_attempt_migration_grants_no_visibility_or_publication() -> None:
    sql = _sql().lower()

    for forbidden in (
        "publish_decision",
        "local_visibility",
        "current.json",
        "publication_allow",
    ):
        assert forbidden not in sql
    assert "not local visibility or publication authority" in sql
