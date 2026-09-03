"""Static closure checks for production Render attempt recovery migration."""

from pathlib import Path

RECOVERY_MIGRATION = Path(
    "packages/autocut-kernel/migrations/0055_production_render_attempt_recovery.sql"
)
FACTS_MIGRATION = Path(
    "packages/autocut-kernel/migrations/0056_production_render_facts.sql"
)


def _sql() -> str:
    return "\n".join(
        (
            RECOVERY_MIGRATION.read_text(encoding="utf-8"),
            FACTS_MIGRATION.read_text(encoding="utf-8"),
        )
    )


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
        "execution_limits_sha256",
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


def test_render_facts_are_closed_exact_and_immutable() -> None:
    sql = FACTS_MIGRATION.read_text(encoding="utf-8")

    assert "requires an empty production render attempt journal" in sql
    assert "pre-facts attempts must be quarantined or reset" in sql
    assert "render_facts_json text" in sql
    assert "render_facts_sha256 text" in sql
    assert "production_render_attempt_facts_shape" in sql
    assert "output_object_id IS NULL" in sql
    assert "render_facts_json IS NULL" in sql
    assert "output_object_id IS NOT NULL" in sql
    assert "render_facts_json IS NOT NULL" in sql
    assert "NEW.render_facts_json IS NULL" in sql
    assert "production render output facts are immutable once known" in sql
    assert "jsonb_object_keys(facts)" in sql
    assert "CREATE OR REPLACE FUNCTION runtime.canonical_json_ascii" in sql
    assert "codepoint < 32 OR codepoint > 126" in sql
    assert "NEW.render_facts_json IS DISTINCT FROM runtime.canonical_json_ascii(facts)" in sql
    assert "production render facts must use canonical JSON serialization" in sql
    assert "production-render-attempt-v1" in sql
    assert "production-ffmpeg-execution-v1" in sql
    assert "facts->>'attempt_id' <> NEW.attempt_id::text" in sql
    assert "facts->>'recipe_sha256' <> NEW.recipe_content_hash" in sql
    assert "facts->>'plan_sha256' <> NEW.render_plan_sha256" in sql
    assert "facts->>'profile_sha256' <> NEW.render_profile_sha256" in sql
    assert "facts->>'execution_limits_sha256' <> NEW.execution_limits_sha256" in sql
    assert "facts->'output'->>'content_hash'" in sql
    assert "facts->'output'->>'byte_length'" in sql
    assert "facts->'output'->>'media_type'" in sql
    assert "sha256(convert_to(NEW.render_facts_json, 'UTF8'))" in sql
    assert "production render facts hash does not bind" in sql
    assert "production renderer identity does not bind" in sql
    assert "Canonical SHA-256 identity" in sql


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
