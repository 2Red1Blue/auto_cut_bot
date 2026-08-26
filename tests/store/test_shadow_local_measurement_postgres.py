"""Static/fake-only coverage for the first local measurement journal tranche.

These tests intentionally do not claim PostgreSQL transaction acceptance.  They
pin the Store's public method surface and the SQL ownership/CAS predicates; a
real PostgreSQL race run remains required before this journal can be activated.
"""

from __future__ import annotations

import inspect

import pytest
from autocut_kernel.store import PostgresRuntimeStore
from autocut_kernel.store.errors import StoreValidationError
from autocut_kernel.store.postgres import _canonical_media_db_json


def _source(name: str) -> str:
    return inspect.getsource(getattr(PostgresRuntimeStore, name))


def test_local_journal_public_surface_matches_the_pipeline_protocol() -> None:
    expected = {
        "claim_or_read_shadow_local_measurement_attempt": ("self", "plan"),
        "read_shadow_local_measurement_attempt": ("self", "attempt_id"),
        "materialize_shadow_local_measurement_source": ("self", "attempt_id", "case_sha256", "limits"),
        "acquire_shadow_local_measurement_member_lease": ("self", "attempt_id", "case_sha256", "expected_version"),
        "stage_shadow_local_measurement_member_response": (
            "self", "attempt_id", "case_sha256", "expected_version", "lease_token", "staged"
        ),
        "stage_shadow_local_measurement_not_started": (
            "self", "attempt_id", "case_sha256", "expected_version", "lease_token", "proof"
        ),
        "acquire_shadow_local_measurement_recovery_lease": ("self", "attempt_id", "expected_version"),
        "mark_shadow_local_measurement_member_indeterminate": (
            "self", "attempt_id", "case_sha256", "expected_version", "recovery_lease_token"
        ),
        "reserve_shadow_local_measurement_successor": ("self", "previous_attempt_id", "authorization"),
    }
    for name, parameter_names in expected.items():
        assert tuple(inspect.signature(getattr(PostgresRuntimeStore, name)).parameters) == parameter_names


def test_claim_verifies_source_before_creating_the_local_command_slot() -> None:
    source = _source("claim_or_read_shadow_local_measurement_attempt")

    source_verification = source.index("self._verify_shadow_local_source_owner")
    slot_insert = source.index("INSERT INTO runtime.command_slots")
    journal_insert = source.index("INSERT INTO runtime.shadow_local_calibration_measurement_attempts")
    assert source_verification < slot_insert < journal_insert
    assert "shadow-local source Job must already be succeeded" in _source(
        "_verify_shadow_local_source_owner_values"
    )
    assert "storage.blob_claims AS claim" in _source("_verify_shadow_local_source_owner_values")


def test_local_member_dispatch_and_staging_are_exact_cas_operations() -> None:
    lease = _source("acquire_shadow_local_measurement_member_lease")
    raw_stage = _source("stage_shadow_local_measurement_member_response")
    busy_stage = _source("stage_shadow_local_measurement_not_started")

    assert "state = 'pending' AND version = %s" in lease
    assert "state = 'invoking' AND version = %s AND lease_token = %s" in raw_stage
    assert "lease_expires_at > transaction_timestamp()" in raw_stage
    assert "state = 'not_started'" in busy_stage
    assert "busy_proof_blob_object_id" in busy_stage
    assert "shadow-local BUSY proof does not bind the leased member" in busy_stage
    assert 'self._transition_shadow_local_attempt(cursor, attempt, "indeterminate")' in busy_stage
    assert "raw_blob_object_id" in raw_stage
    assert "_put_shadow_local_blob" in raw_stage


def test_materialization_is_locked_to_the_pending_member_before_dispatch() -> None:
    source = _source("materialize_shadow_local_measurement_source")

    assert "member.state != \"pending\"" in source
    assert "_verify_shadow_local_source_owner_from_member" in source
    assert "materialize_immutable_blob(source_job, source_blob, limits)" in source
    assert "lease_token" not in inspect.signature(
        PostgresRuntimeStore.materialize_shadow_local_measurement_source
    ).parameters


def test_recovery_marks_unknown_and_successor_keeps_the_same_slot() -> None:
    recovery = _source("mark_shadow_local_measurement_member_indeterminate")
    successor = _source("reserve_shadow_local_measurement_successor")

    assert "state = 'indeterminate'" in recovery
    assert "lease_expires_at <= transaction_timestamp()" in recovery
    assert "previous.command_slot_id" in successor
    assert "INSERT INTO runtime.command_slots" not in successor
    assert "retry_member_case_sha256" in successor
    assert 'state="staged" if member.state == "staged" else "pending"' in successor


def test_successor_enforces_frozen_budget_and_exact_authorization_replay() -> None:
    source = _source("reserve_shadow_local_measurement_successor")

    assert "_shadow_local_max_attempt_count" in source
    assert "shadow-local retry exceeds the frozen max_attempt_count" in source
    assert source.index("_shadow_local_max_attempt_count") < source.index(
        "SELECT attempt_id FROM runtime.shadow_local_calibration_measurement_attempts"
    )
    for field in (
        "previous_attempt_id",
        "retry_decision_reference_sha256",
        "retry_member_case_sha256",
        "retry_predecessor_version",
        "retry_reason_code",
    ):
        assert field in source
    assert "IdempotencyConflictError" in source
    assert "different authorization" in source


def test_frozen_max_attempt_budget_rejects_bool_and_missing_value() -> None:
    method = PostgresRuntimeStore._shadow_local_max_attempt_count

    assert method('{"shadow_local_inputs":{"max_attempt_count":2}}') == 2
    for payload in (
        '{"shadow_local_inputs":{"max_attempt_count":true}}',
        '{"shadow_local_inputs":{}}',
    ):
        with pytest.raises(StoreValidationError, match="max_attempt_count"):
            method(payload)


def test_claim_replays_the_current_same_slot_successor_not_ordinal_one() -> None:
    source = _source("_read_shadow_local_attempt_by_slot")

    assert "ORDER BY attempt_ordinal DESC" in source
    assert "LIMIT 1" in source
    assert "attempt_ordinal = 1" not in source


def test_local_media_jsonb_values_restore_ascii_media_canonical_bytes() -> None:
    assert _canonical_media_db_json('{"text":"中文"}') == '{"text":"\\u4e2d\\u6587"}'

    decoder = _source("_read_shadow_local_attempt_by_id")
    assert decoder.count("_canonical_media_db_json") == 4
    assert "_canonical_db_json(_text(plan_json))" in decoder


def test_member_lease_locks_the_serial_prefix_and_sets_expiry_after_locks() -> None:
    source = _source("acquire_shadow_local_measurement_member_lease")

    assert "member_ordinal < %s" in source
    assert "ORDER BY member_ordinal FOR UPDATE" in source
    assert '_text(prefix[1]) != "staged"' in source
    assert "attempt_ordinal > %s FOR KEY SHARE" in source
    assert source.index("expires_at = datetime.now") > source.index("ORDER BY member_ordinal FOR UPDATE")


def test_recovery_lease_expiry_is_calculated_after_the_attempt_lock() -> None:
    source = _source("acquire_shadow_local_measurement_recovery_lease")

    assert source.index("attempt = self._locked_shadow_local_attempt") < source.index(
        "expires_at = datetime.now"
    )


def test_attempt_and_member_decoders_read_all_local_raw_and_busy_fields() -> None:
    source = _source("_read_shadow_local_attempt_by_id")

    for column in (
        "source_blob_object_id",
        "raw_blob_object_id",
        "evidence_json::text",
        "busy_proof_blob_object_id",
        "busy_proof_json::text",
        "lease_expires_at",
    ):
        assert column in source
    assert "ShadowLocalMeasurementMember(" in source
    assert "ShadowLocalMeasurementAttempt(" in source
