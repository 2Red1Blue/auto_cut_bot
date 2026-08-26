"""PostgreSQL behavior for durable generic command execution kinds.

These tests require the existing disposable ``ac_autocut_verify`` PostgreSQL
fixture.  They are deliberately not run by this implementation task.
"""

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from autocut_kernel.store import (
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandRejection,
    CommandStateError,
    CommandSuccess,
    IdempotencyConflictError,
    Job,
)
from autocut_kernel.store.postgres import PostgresRuntimeStore

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    DSN is None, reason="AUTOCUT_TEST_POSTGRES_DSN is required for disposable PostgreSQL tests"
)

_MIGRATIONS = (
    "0001_runtime_core.sql",
    "0002_runtime_core_constraints.sql",
    "0003_vlm_generation_and_run_finalization.sql",
    "0004_provider_media_objects.sql",
    "0006_ark_provider_recovery.sql",
    "0009_vlm_bounded_retry.sql",
    "0011_generation_retry_schedule.sql",
    "0018_command_execution_kind.sql",
)


def _digest(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _member(job: Job) -> ArtifactMember:
    payload = '{"complete":true}'
    return ArtifactMember(
        artifact_type="narrative_graph",
        logical_id="narrative_graph",
        revision=1,
        scope=ArtifactScope("pipeline", "job", job.job_key),
        content_hash=_digest(payload),
        payload_json=payload,
    )


def _set_hash(members: tuple[ArtifactMember, ...]) -> str:
    canonical = [
        {
            "artifact_type": member.artifact_type,
            "content_hash": member.content_hash,
            "logical_id": member.logical_id,
            "payload_json": json.loads(member.payload_json),
            "revision": member.revision,
            "scope": {
                "key": member.scope.key,
                "kind": member.scope.kind,
                "namespace": member.scope.namespace,
            },
        }
        for member in members
    ]
    return _digest(json.dumps(canonical, separators=(",", ":"), sort_keys=True))


@pytest.fixture(autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        if connection.info.dbname != "ac_autocut_verify":
            pytest.fail(
                "AUTOCUT_TEST_POSTGRES_DSN must name disposable ac_autocut_verify, never ac_db"
            )
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for name in _MIGRATIONS:
                cursor.execute((Path("packages/autocut-kernel/migrations") / name).read_text())


def _store() -> PostgresRuntimeStore:
    assert DSN is not None
    return PostgresRuntimeStore(lambda: psycopg.connect(DSN))


def test_arbitrary_generation_command_owns_attempt_and_terminal_rejection() -> None:
    store = _store()
    job = Job("execution-kind-generic-generation", "test")
    request_hash = _digest("BuildNarrativeGraph request")
    claimed = store.claim_command(
        CommandClaim(
            job,
            "build-narrative",
            "BuildNarrativeGraph@2.1.3",
            request_hash,
            execution_kind="generation",
        )
    )
    with pytest.raises(CommandStateError, match="execution kind must be deterministic"):
        store.commit_command_rejection(
            CommandRejection(
                claimed.command_slot_id,
                "FIXTURE_FAILURE",
                '{"reason":"fixture"}',
                "failed",
            )
        )
    request = b'{"source":"fixture"}'
    request_ref = store.put_immutable_blob(
        job,
        content=request,
        content_hash=_digest(request),
        media_type="application/json",
    )
    attempt = store.reserve_generation_attempt(
        claimed.command_slot_id,
        request_hash,
        provider_id="fixture-provider",
        provider_idempotency_key="execution-kind-generic-generation",
        request_payload=request_ref,
    )
    failed = store.fail_generation_attempt(
        attempt.attempt_id,
        expected_version=attempt.version,
        failure_code="FIXTURE_FAILURE",
        failure_detail_json='{"reason":"fixture"}',
    )
    outcome = store.commit_generation_rejection(
        failed.attempt_id,
        expected_version=failed.version,
        rejection=CommandRejection(
            claimed.command_slot_id,
            "FIXTURE_FAILURE",
            '{"reason":"fixture"}',
            "failed",
        ),
    )

    assert outcome.state == "failed"
    assert outcome.receipt_id is not None


def test_deterministic_slot_cannot_start_provider_generation_or_replay_as_generation() -> None:
    store = _store()
    job = Job("execution-kind-deterministic", "test")
    request_hash = _digest("deterministic request")
    claimed = store.claim_command(
        CommandClaim(
            job,
            "deterministic",
            "BuildNarrativeGraph@2.1.3",
            request_hash,
            execution_kind="deterministic",
        )
    )
    request = b"{}"
    request_ref = store.put_immutable_blob(
        job,
        content=request,
        content_hash=_digest(request),
        media_type="application/json",
    )
    with pytest.raises(CommandStateError, match="execution kind must be generation"):
        store.reserve_generation_attempt(
            claimed.command_slot_id,
            request_hash,
            provider_id="fixture-provider",
            provider_idempotency_key="execution-kind-deterministic",
            request_payload=request_ref,
        )
    with pytest.raises(IdempotencyConflictError):
        store.claim_command(
            CommandClaim(
                job,
                "deterministic",
                "BuildNarrativeGraph@2.1.3",
                request_hash,
                execution_kind="generation",
            )
        )


def test_arbitrary_generation_command_commits_success_with_full_attempt_chain() -> None:
    store = _store()
    job = Job("execution-kind-generic-success", "test")
    request_hash = _digest("BuildNarrativeGraph success request")
    claimed = store.claim_command(
        CommandClaim(
            job,
            "build-narrative-success",
            "BuildNarrativeGraph@2.1.3",
            request_hash,
            execution_kind="generation",
        )
    )
    request = b'{"source":"fixture"}'
    request_ref = store.put_immutable_blob(
        job,
        content=request,
        content_hash=_digest(request),
        media_type="application/json",
    )
    reserved = store.reserve_generation_attempt(
        claimed.command_slot_id,
        request_hash,
        provider_id="fixture-provider",
        provider_idempotency_key="execution-kind-generic-success",
        request_payload=request_ref,
    )
    dispatched = store.dispatch_generation_attempt(
        reserved.attempt_id,
        expected_version=reserved.version,
        provider_request_id="execution-kind-generic-success-request",
    )
    assert dispatched is not None
    raw = b'{"response":"fixture"}'
    raw_ref = store.put_immutable_blob(
        job,
        content=raw,
        content_hash=_digest(raw),
        media_type="application/json",
    )
    responded = store.record_generation_response(
        reserved.attempt_id,
        expected_version=dispatched.version,
        raw_response=raw_ref,
        dispatch_lease_token=dispatched.dispatch_lease_token or "",
    )
    member = _member(job)
    committed = store.commit_generation_success(
        reserved.attempt_id,
        expected_version=responded.version,
        success=CommandSuccess(claimed.command_slot_id, _set_hash((member,)), (member,)),
    )

    assert committed.state == "committed"
    assert committed.receipt_id is not None
    assert DSN is not None
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM runtime.generation_receipt_attempts WHERE receipt_id = %s",
                (committed.receipt_id,),
            )
            assert cursor.fetchone() == (1,)


def test_0018_backfills_historical_terminal_slots_without_rewriting_receipts_or_sets() -> None:
    """Exercise the populated-schema upgrade path before the autouse final schema."""

    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for name in _MIGRATIONS[:-1]:
                cursor.execute((Path("packages/autocut-kernel/migrations") / name).read_text())

        job_id = uuid4()
        success_slot, denied_slot, failed_slot, running_slot, generation_slot = (
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
        )
        set_id, artifact_id, success_receipt, denied_receipt, failed_receipt = (
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
        )
        success_set_hash = _digest("historical-set")
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO runtime.jobs (job_id, job_key, profile, state) VALUES (%s, %s, 'test', 'running')",
                    (job_id, "execution-kind-historical"),
                )
                for slot_id, key, command_name, request_hash, state in (
                    (success_slot, "old-success", "OldDeterministicCommand", _digest("old-success"), "succeeded"),
                    (denied_slot, "old-denied", "OldDeterministicCommand", _digest("old-denied"), "denied"),
                    (failed_slot, "old-failed", "OldDeterministicCommand", _digest("old-failed"), "failed"),
                    (running_slot, "old-running", "OldDeterministicCommand", _digest("old-running"), "running"),
                    (generation_slot, "old-generation", "GenerateVlmEvidenceCommand", _digest("old-generation"), "running"),
                ):
                    cursor.execute(
                        "INSERT INTO runtime.command_slots "
                        "(command_slot_id, job_id, idempotency_key, command_name, request_hash, state, completed_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, "
                        "CASE WHEN %s IN ('succeeded', 'denied', 'failed') "
                        "THEN transaction_timestamp() ELSE NULL END)",
                        (slot_id, job_id, key, command_name, request_hash, state, state),
                    )
                cursor.execute(
                    "INSERT INTO runtime.artifact_sets "
                    "(artifact_set_id, command_slot_id, job_id, set_hash, member_count) "
                    "VALUES (%s, %s, %s, %s, 1)",
                    (set_id, success_slot, job_id, success_set_hash),
                )
                cursor.execute(
                    "INSERT INTO runtime.artifacts "
                    "(artifact_id, artifact_set_id, artifact_type, logical_id, revision, namespace, "
                    "scope_kind, scope_key, content_hash, payload_json, job_id) "
                    "VALUES (%s, %s, 'fixture', 'fixture', 1, 'pipeline', 'job', %s, %s, '{}'::jsonb, %s)",
                    (artifact_id, set_id, "execution-kind-historical", _digest("{}"), job_id),
                )
                cursor.execute(
                    "INSERT INTO runtime.artifact_set_members (artifact_set_id, ordinal, artifact_id) "
                    "VALUES (%s, 0, %s)",
                    (set_id, artifact_id),
                )
                cursor.execute(
                    "INSERT INTO runtime.command_receipts "
                    "(receipt_id, command_slot_id, outcome, result_artifact_set_id) "
                    "VALUES (%s, %s, 'succeeded', %s)",
                    (success_receipt, success_slot, set_id),
                )
                for receipt_id, slot_id, outcome in (
                    (denied_receipt, denied_slot, "denied"),
                    (failed_receipt, failed_slot, "failed"),
                ):
                    cursor.execute(
                        "INSERT INTO runtime.command_receipts "
                        "(receipt_id, command_slot_id, outcome, failure_code, failure_detail) "
                        "VALUES (%s, %s, %s, 'HISTORICAL', '{}'::jsonb)",
                        (receipt_id, slot_id, outcome),
                    )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT receipt_id, outcome, result_artifact_set_id FROM runtime.command_receipts ORDER BY receipt_id"
            )
            receipts_before = cursor.fetchall()
            cursor.execute("SELECT artifact_set_id, set_hash FROM runtime.artifact_sets")
            sets_before = cursor.fetchall()
            cursor.execute((Path("packages/autocut-kernel/migrations") / _MIGRATIONS[-1]).read_text())
            cursor.execute(
                "SELECT command_slot_id, execution_kind FROM runtime.command_slots ORDER BY command_slot_id"
            )
            kinds = dict(cursor.fetchall())
            assert kinds[generation_slot] == "generation"
            assert kinds[success_slot] == kinds[denied_slot] == kinds[failed_slot] == kinds[running_slot] == "deterministic"
            cursor.execute(
                "SELECT receipt_id, outcome, result_artifact_set_id FROM runtime.command_receipts ORDER BY receipt_id"
            )
            assert cursor.fetchall() == receipts_before
            cursor.execute("SELECT artifact_set_id, set_hash FROM runtime.artifact_sets")
            assert cursor.fetchall() == sets_before
            with pytest.raises(psycopg.Error, match="execution kind is immutable"):
                cursor.execute(
                    "UPDATE runtime.command_slots SET execution_kind = 'deterministic' "
                    "WHERE command_slot_id = %s",
                    (generation_slot,),
                )
