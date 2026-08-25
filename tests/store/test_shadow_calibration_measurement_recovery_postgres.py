"""Disposable PostgreSQL coverage for the shadow-only native recovery owner."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import autocut_kernel.store.postgres as postgres_module
import pytest
from autocut_kernel.store import (
    SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME,
    SHADOW_CALIBRATION_MEASUREMENT_PROTOCOL,
    CommandClaim,
    CommandStateError,
    Job,
    PostgresRuntimeStore,
    ShadowMeasurementMemberPlan,
    ShadowMeasurementPlan,
    ShadowMeasurementRetryAuthorization,
    ShadowMeasurementStagedResponse,
    ShadowMeasurementTerminalDenialRequest,
    StoreValidationError,
)
from autocut_kernel.store.models import canonical_payload_hash

psycopg = pytest.importorskip("psycopg")

VERIFY_POSTGRES_DSN = "postgresql://ac_user:ac_password_2026@127.0.0.1:5433/ac_autocut_verify"
MIGRATIONS = Path("packages/autocut-kernel/migrations")


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _plan() -> ShadowMeasurementPlan:
    members = []
    member_plans = []
    for ordinal, name in enumerate(("first", "second")):
        reference = _hash(f"member:{name}")
        anchor = _hash(f"anchor:{name}")
        invocation = {"member": name, "native": "locked"}
        context = {"anchor": anchor, "member": name}
        members.append(
            {
                "corpus_member_reference_sha256": reference,
                "expected_anchor_reference_sha256": anchor,
                "native_invocation": invocation,
                "raw_context": context,
            }
        )
        member_plans.append(
            ShadowMeasurementMemberPlan(reference, ordinal, _canonical(invocation), _canonical(context), anchor)
        )
    payload = {
        "command": SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME,
        "corpus_members": members,
        "measurement_protocol": SHADOW_CALIBRATION_MEASUREMENT_PROTOCOL,
        "shadow_inputs": {
            "acceptance_policy_sha256": _hash("acceptance"),
            "alignment_policy_sha256": _hash("alignment"),
            "calibration_corpus_set_sha256": _hash("corpus"),
            "native_port_identity_sha256": _hash("native"),
            "profile_source_sha256": _hash("profile"),
            "registry_snapshot_sha256": _hash("registry"),
            "vad_merge_policy_sha256": _hash("vad"),
            "word_gap_policy_sha256": _hash("gap"),
        },
    }
    plan_json = _canonical(payload)
    plan_hash = canonical_payload_hash(plan_json)
    claim = CommandClaim(
        Job(plan_hash.removeprefix("sha256:"), "shadow"),
        f"shadow-calibration:{plan_hash.removeprefix('sha256:')}",
        SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME,
        plan_hash,
    )
    return ShadowMeasurementPlan(claim, plan_json, tuple(member_plans))


@pytest.fixture(autouse=True)
def migrated_database() -> None:
    try:
        connection = psycopg.connect(VERIFY_POSTGRES_DSN, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("disposable authority PostgreSQL is unavailable")
    with connection:
        if connection.info.dbname != "ac_autocut_verify":
            pytest.fail("shadow recovery acceptance may run only against ac_autocut_verify")
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for migration in sorted(MIGRATIONS.glob("*.sql")):
                cursor.execute(migration.read_text(encoding="utf-8"))


def _store() -> PostgresRuntimeStore:
    return PostgresRuntimeStore(lambda: psycopg.connect(VERIFY_POSTGRES_DSN))


def _stage(store: PostgresRuntimeStore, attempt: object, index: int) -> object:
    members = attempt.members  # type: ignore[union-attr]
    lease = store.acquire_shadow_measurement_member_lease(
        attempt.attempt_id, members[index].corpus_member_reference_sha256, expected_version=members[index].version  # type: ignore[union-attr]
    )
    assert lease is not None
    raw = f'{{"member":{index}}}'.encode()
    projection = {
        "member": index,
        "summary": {
            producer: {
                "absolute_maximum_tick": index + 1,
                "clock_id": "clock",
                "early_maximum_tick": 0,
                "inference_kind": f"{producer}-direct",
                "late_maximum_tick": index + 1,
                "matches": [],
                "producer": producer,
                "producer_id": f"{producer}-producer",
                "time_base": {"denominator": 1, "numerator": 1},
            }
            for producer in ("asr", "vad")
        },
    }
    return store.stage_shadow_measurement_member_response(
        attempt.attempt_id,  # type: ignore[union-attr]
        members[index].corpus_member_reference_sha256,
        expected_version=lease.member.version,
        lease_token=lease.lease_token,
        staged=ShadowMeasurementStagedResponse(
            raw,
            "sha256:" + hashlib.sha256(raw).hexdigest(),
            "application/json",
            _canonical(projection),
        ),
    )


def _terminal_denial_request(
    store: PostgresRuntimeStore, attempt: object, index: int = 0
) -> ShadowMeasurementTerminalDenialRequest:
    members = attempt.members  # type: ignore[union-attr]
    lease = store.acquire_shadow_measurement_member_lease(
        attempt.attempt_id,  # type: ignore[union-attr]
        members[index].corpus_member_reference_sha256,
        expected_version=members[index].version,
    )
    assert lease is not None
    return ShadowMeasurementTerminalDenialRequest(
        attempt_id=attempt.attempt_id,  # type: ignore[union-attr]
        command_slot_id=attempt.command_slot_id,  # type: ignore[union-attr]
        job=attempt.job,  # type: ignore[union-attr]
        plan_hash=attempt.plan_hash,  # type: ignore[union-attr]
        member_reference_sha256=lease.member.corpus_member_reference_sha256,
        expected_attempt_version=lease.attempt_version,
        expected_member_version=lease.member.version,
        member_lease_token=lease.lease_token,
        failure_code="SHADOW_CALIBRATION_INVALID",
        failure_detail_json=_canonical(
            {"reason": "decoder rejected raw evidence", "stage": "shadow_calibration"}
        ),
    )


def test_normal_plan_stage_and_atomic_two_member_finalization() -> None:
    store, plan = _store(), _plan()
    attempt = store.claim_or_read_shadow_measurement_attempt(plan.claim, plan)
    attempt = _stage(store, attempt, 0)
    attempt = _stage(store, attempt, 1)

    outcome = store.finalize_shadow_measurement_success(attempt.attempt_id, expected_version=attempt.version)
    replay = store.claim_or_read_shadow_measurement_attempt(plan.claim, plan)

    assert outcome.state == "succeeded"
    assert replay.state == "committed"
    assert replay.outcome.artifact_set_id == outcome.artifact_set_id
    with psycopg.connect(VERIFY_POSTGRES_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT member_count FROM runtime.artifact_sets WHERE artifact_set_id = %s", (outcome.artifact_set_id,))
        assert cursor.fetchone() == (2,)


def test_staged_recovery_never_requires_a_second_native_call() -> None:
    store, plan = _store(), _plan()
    attempt = store.claim_or_read_shadow_measurement_attempt(plan.claim, plan)
    staged = _stage(store, attempt, 0)

    replay = _store().claim_or_read_shadow_measurement_attempt(plan.claim, plan)

    assert replay.attempt_id == staged.attempt_id
    assert replay.members[0].state == "staged"
    assert replay.members[0].raw_blob == staged.members[0].raw_blob
    assert replay.members[1].state == "pending"


def test_expired_unknown_member_is_indeterminate_and_successor_is_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    store, plan = _store(), _plan()
    attempt = store.claim_or_read_shadow_measurement_attempt(plan.claim, plan)
    attempt = _stage(store, attempt, 0)
    monkeypatch.setattr(postgres_module, "SHADOW_MEASUREMENT_LEASE_SECONDS", 0)
    lease = store.acquire_shadow_measurement_member_lease(
        attempt.attempt_id, attempt.members[1].corpus_member_reference_sha256, expected_version=attempt.members[1].version
    )
    assert lease is not None
    recovery = store.acquire_shadow_measurement_recovery_lease(attempt.attempt_id, expected_version=lease.attempt_version)
    assert recovery is not None
    unknown = store.mark_shadow_measurement_member_indeterminate(
        attempt.attempt_id,
        lease.member.corpus_member_reference_sha256,
        expected_version=lease.member.version,
        recovery_lease_token=recovery.lease_token,
    )

    assert unknown.state == "indeterminate"
    assert unknown.outcome.receipt_id is None
    with pytest.raises(StoreValidationError):
        store.reserve_shadow_measurement_successor(attempt.attempt_id, object())  # type: ignore[arg-type]
    successor = store.reserve_shadow_measurement_successor(
        attempt.attempt_id, ShadowMeasurementRetryAuthorization(_hash("decision"), plan.claim.request_hash)
    )

    assert successor.attempt_ordinal == 2
    assert successor.previous_attempt_id == attempt.attempt_id
    assert successor.canonical_plan_json == attempt.canonical_plan_json
    assert all(member.state == "pending" for member in successor.members)
    # The predecessor remains audit evidence and cannot be replaced.
    assert unknown.previous_attempt_id is None
    with psycopg.connect(VERIFY_POSTGRES_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM runtime.artifact_sets")
        assert cursor.fetchone() == (0,)


def test_recovery_lease_is_compare_and_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postgres_module, "SHADOW_MEASUREMENT_LEASE_SECONDS", 0)
    store, plan = _store(), _plan()
    attempt = store.claim_or_read_shadow_measurement_attempt(plan.claim, plan)
    first = store.acquire_shadow_measurement_recovery_lease(attempt.attempt_id, expected_version=attempt.version)
    assert first is not None
    assert store.acquire_shadow_measurement_recovery_lease(attempt.attempt_id, expected_version=attempt.version) is None


def test_decoder_proven_invalid_response_terminally_denies_without_artifacts() -> None:
    store, plan = _store(), _plan()
    attempt = store.claim_or_read_shadow_measurement_attempt(plan.claim, plan)
    denial = _terminal_denial_request(store, attempt)

    result = store.commit_shadow_measurement_terminal_denial(denial)
    replay = store.commit_shadow_measurement_terminal_denial(denial)

    assert result.outcome.state == replay.outcome.state == "denied"
    assert result.outcome.receipt_id == replay.outcome.receipt_id
    assert result.attempt.state == "indeterminate"
    assert result.attempt.outcome == result.outcome
    assert result.attempt.members[0].state == "indeterminate"
    assert result.attempt.members[1].state == "pending"
    with psycopg.connect(VERIFY_POSTGRES_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM runtime.artifact_sets")
        assert cursor.fetchone() == (0,)
        cursor.execute(
            "SELECT outcome, failure_code FROM runtime.command_receipts WHERE receipt_id = %s",
            (result.outcome.receipt_id,),
        )
        assert cursor.fetchone() == ("denied", "SHADOW_CALIBRATION_INVALID")


def test_terminal_denial_rejects_substitution_and_preserves_unknown_invocation() -> None:
    store, plan = _store(), _plan()
    attempt = store.claim_or_read_shadow_measurement_attempt(plan.claim, plan)
    denial = _terminal_denial_request(store, attempt)

    with pytest.raises(CommandStateError):
        store.commit_shadow_measurement_terminal_denial(
            replace(denial, job=Job("another-shadow-job", "shadow"))
        )
    with pytest.raises(StoreValidationError):
        replace(denial, command_name="AnotherShadowCommand")
    with pytest.raises(CommandStateError):
        store.commit_shadow_measurement_terminal_denial(
            replace(denial, attempt_id=uuid4())
        )
    with pytest.raises(CommandStateError):
        store.commit_shadow_measurement_terminal_denial(
            replace(denial, plan_hash=_hash("another-plan"))
        )
    with pytest.raises(CommandStateError):
        store.commit_shadow_measurement_terminal_denial(
            replace(denial, expected_member_version=denial.expected_member_version + 1)
        )
    with pytest.raises(StoreValidationError):
        replace(denial, failure_code="SHADOW_CALIBRATION_NATIVE_UNAVAILABLE")

    still_running = store.claim_or_read_shadow_measurement_attempt(plan.claim, plan)
    assert still_running.outcome.state == "running"
    assert still_running.state == "collecting"
    assert still_running.members[0].state == "invoking"


def test_terminal_denial_refuses_to_overwrite_staged_or_concurrent_evidence() -> None:
    store, plan = _store(), _plan()
    attempt = store.claim_or_read_shadow_measurement_attempt(plan.claim, plan)
    attempt = _stage(store, attempt, 0)
    denial = _terminal_denial_request(store, attempt, 1)

    with pytest.raises(CommandStateError, match="staged or outcome-unknown"):
        store.commit_shadow_measurement_terminal_denial(denial)

    preserved = store.claim_or_read_shadow_measurement_attempt(plan.claim, plan)
    assert preserved.outcome.state == "running"
    assert preserved.members[0].state == "staged"
    assert preserved.members[1].state == "invoking"


def test_wrong_job_command_plan_and_generic_bypass_are_denied() -> None:
    plan = _plan()
    store = _store()
    with pytest.raises(CommandStateError, match="explicit shadow owner"):
        store.claim_command(plan.claim)
    wrong_claim = CommandClaim(plan.claim.job, plan.claim.idempotency_key, "another-command", plan.claim.request_hash)
    with pytest.raises(StoreValidationError):
        store.claim_or_read_shadow_measurement_attempt(wrong_claim, plan)
    wrong_job = CommandClaim(Job(plan.claim.job.job_key, "test"), plan.claim.idempotency_key, plan.claim.command_name, plan.claim.request_hash)
    with pytest.raises(StoreValidationError):
        ShadowMeasurementPlan(wrong_job, plan.canonical_plan_json, plan.members)
    with pytest.raises(StoreValidationError):
        ShadowMeasurementPlan(plan.claim, plan.canonical_plan_json + " ", plan.members)
