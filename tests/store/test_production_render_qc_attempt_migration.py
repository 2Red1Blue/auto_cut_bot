"""Static and model closure checks for the production Render QC attempt journal."""

from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from autocut_kernel.store import (
    BlobRef,
    ProductionRenderQcAttempt,
    ProductionRenderQcLease,
    StoreValidationError,
)

MIGRATION = Path(
    "packages/autocut-kernel/migrations/0057_production_render_qc_attempts.sql"
)
SHA256 = "sha256:" + "a" * 64


def _attempt(**changes: object) -> ProductionRenderQcAttempt:
    attempt = ProductionRenderQcAttempt(
        qc_attempt_id=uuid4(),
        render_attempt_id=uuid4(),
        job_id=uuid4(),
        command_slot_id=uuid4(),
        rendered_version=2,
        output_blob=BlobRef(uuid4(), SHA256, 1, "video/mp4"),
        render_facts_sha256=SHA256,
        qc_policy_sha256=SHA256,
        required_check_set_version="production-av-qc-v1",
        qc_runner_identity_sha256=SHA256,
        state="reserved",
        version=0,
        reserved_at=datetime.now(timezone.utc),
    )
    return replace(attempt, **changes)


def test_qc_attempt_public_model_is_strict_and_hides_lease_token() -> None:
    reserved = _attempt(is_fresh_reservation=True)

    assert reserved == replace(reserved, is_fresh_reservation=False)
    assert {"token", "lease_token"}.isdisjoint(field.name for field in fields(reserved))
    scanning = replace(
        reserved,
        state="scanning",
        version=1,
        lease_expires_at=datetime.now(timezone.utc),
    )
    assert scanning.state == "scanning"

    invalid_changes = (
        {"qc_attempt_id": str(uuid4())},
        {"render_attempt_id": str(uuid4())},
        {"job_id": str(uuid4())},
        {"command_slot_id": str(uuid4())},
        {"rendered_version": 0},
        {"rendered_version": 1},
        {"output_blob": object()},
        {"output_blob": BlobRef(uuid4(), SHA256, 0, "video/mp4")},
        {"output_blob": BlobRef(uuid4(), SHA256, 1, "video/webm")},
        {"render_facts_sha256": "sha256:" + "A" * 64},
        {"qc_policy_sha256": "not-a-hash"},
        {"required_check_set_version": 1},
        {"required_check_set_version": ""},
        {"required_check_set_version": "Production-av-qc-v1"},
        {"required_check_set_version": "production/av/qc/v1"},
        {"required_check_set_version": "a" * 129},
        {"qc_runner_identity_sha256": ""},
        {"state": "evidence_ready"},
        {"version": -1},
        {"reserved_at": datetime.now()},
        {"lease_expires_at": datetime.now(timezone.utc)},
        {"is_fresh_reservation": 1},
    )
    for changes in invalid_changes:
        with pytest.raises(StoreValidationError):
            _attempt(**changes)

    with pytest.raises(StoreValidationError):
        _attempt(state="reserved", version=1)
    with pytest.raises(StoreValidationError):
        _attempt(state="scanning", version=1)
    with pytest.raises(StoreValidationError):
        _attempt(
            state="scanning",
            version=1,
            lease_expires_at=datetime.now(),
        )


def test_qc_lease_is_an_exact_positive_private_capability() -> None:
    lease = ProductionRenderQcLease(
        qc_attempt_id=uuid4(),
        render_attempt_id=uuid4(),
        job_id=uuid4(),
        command_slot_id=uuid4(),
        token=uuid4(),
        expires_at=datetime.now(timezone.utc),
        version=1,
    )
    assert lease.version == 1

    for field_name in (
        "qc_attempt_id",
        "render_attempt_id",
        "job_id",
        "command_slot_id",
        "token",
    ):
        with pytest.raises(StoreValidationError):
            replace(lease, **{field_name: str(uuid4())})
    with pytest.raises(StoreValidationError):
        replace(lease, expires_at=datetime.now())
    with pytest.raises(StoreValidationError):
        replace(lease, version=0)


def test_qc_attempt_schema_binds_one_exact_render_output_and_policy() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TABLE runtime.production_render_qc_attempts" in sql
    assert "qc_attempt_id uuid PRIMARY KEY" in sql
    assert "render_attempt_id uuid NOT NULL UNIQUE" in sql
    assert "command_slot_id uuid NOT NULL UNIQUE" in sql
    assert "REFERENCES runtime.production_render_attempts (attempt_id)" in sql
    for column in (
        "rendered_version",
        "output_object_id",
        "render_facts_sha256",
        "qc_policy_sha256",
        "required_check_set_version",
        "qc_runner_identity_sha256",
    ):
        assert column in sql
    assert "required_check_set_version text NOT NULL" in sql
    assert "rendered_version bigint NOT NULL CHECK (rendered_version >= 2)" in sql
    assert "length(required_check_set_version) <= 128" in sql
    assert "required_check_set_version ~ '^[a-z0-9][a-z0-9._-]*$'" in sql
    assert "state IN ('reserved', 'scanning')" in sql
    assert "state = 'reserved' AND version = 0" in sql
    assert "state = 'scanning' AND version >= 1" in sql


def test_qc_attempt_transition_guard_is_closed_durable_and_db_timed() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "BEFORE INSERT OR UPDATE OR DELETE" in sql
    assert "production render QC attempts are durable and cannot be deleted" in sql
    assert "production render QC attempt identity is immutable" in sql
    assert "NEW.version <> OLD.version + 1" in sql
    assert "OLD.state = 'reserved' AND NEW.state = 'scanning'" in sql
    assert "OLD.state = 'scanning' AND NEW.state = 'scanning'" in sql
    assert "NEW.lease_expires_at <= OLD.lease_expires_at" in sql
    assert "OLD.lease_expires_at <= clock_timestamp()" in sql
    assert "OLD.lease_expires_at > clock_timestamp()" in sql
    assert "active production render QC lease cannot be taken over" in sql
    assert "transaction_timestamp()" not in sql


def test_qc_attempt_integrity_is_deferred_and_recomputes_parent_authority() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW" in sql
    assert "parent_render.state = 'rendered'" in sql
    assert "parent_render.version = NEW.rendered_version" in sql
    assert "parent_render.output_object_id = NEW.output_object_id" in sql
    assert "parent_render.render_facts_sha256 = NEW.render_facts_sha256" in sql
    assert "parent_render.job_id = NEW.job_id" in sql
    assert "parent_render.command_slot_id = NEW.command_slot_id" in sql
    assert "render_slot.state = 'running'" in sql
    assert "render_slot.command_name = 'RenderProductionRecipeCommand@1'" in sql
    assert "render_slot.execution_kind = 'deterministic'" in sql
    assert "render_job.profile IN ('shadow', 'production')" in sql
    assert "output_blob.storage_kind = 's3_compatible'" in sql
    assert "output_blob.byte_length > 0" in sql
    assert "output_blob.media_type = 'video/mp4'" in sql
    assert "output_claim.job_id = NEW.job_id" in sql


def test_parent_render_cannot_terminalize_around_a_qc_journal() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE OR REPLACE FUNCTION runtime.guard_production_render_attempt_transition()" in sql
    assert "OLD.state = 'rendered'" in sql
    assert "NEW.state IN ('committed', 'denied', 'failed')" in sql
    assert "FROM runtime.production_render_qc_attempts" in sql
    assert "production render with an active QC journal cannot become terminal" in sql


def test_qc_attempt_migration_grants_no_release_or_visibility_authority() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    for forbidden in ("publish_decision", "local_visibility"):
        assert forbidden not in sql
    assert "not local visibility or publication authority" in sql
