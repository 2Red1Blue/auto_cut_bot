"""Public Store lifecycle probes; scripted DB I/O is not PostgreSQL acceptance."""

import json
from collections import deque
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from autocut_kernel.store import (
    ArtifactMember,
    ArtifactScope,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandStateError,
    CommandSuccess,
    GenerationAttempt,
    IdempotencyConflictError,
    Job,
)
from autocut_kernel.store.models import canonical_payload_hash
from autocut_kernel.store.postgres import (
    BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND,
    CALIBRATION_VALIDATOR_COMMAND,
    VLM_BATCH_FINALIZER_COMMAND_NAME,
    VLM_BATCH_IDEMPOTENCY_PREFIX,
    PostgresRuntimeStore,
)

JOB_ID, SLOT_ID, RECEIPT_ID, SET_ID = (UUID(int=n) for n in range(1, 5))
NAME = "BuildNarrativeGraph@2.1.3"
HASH = canonical_payload_hash('{"synthetic":"request"}')
BLOB = BlobRef(UUID(int=5), HASH, 23, "application/json")
JOB = Job("execution-kind-lifecycle-unit", "test")


class _Cursor:
    """Only supplies DB rows: no command-name/kind/state decision is simulated."""

    rowcount = 1

    def __init__(self, rows=()):
        self.rows = deque(rows)
        self.calls = []

    def execute(self, query, params=()):
        self.calls.append((" ".join(query.split()), params))

    def fetchone(self):
        assert self.rows, "unexpected database read"
        return self.rows.popleft()

    def close(self):
        pass


def _store(rows=()):
    cursor = _Cursor(rows)
    connection = SimpleNamespace(cursor=lambda: cursor, commit=Mock(), rollback=Mock(), close=Mock())
    return PostgresRuntimeStore(lambda: connection), cursor, connection


def _slot_rows(kind, state="running"):
    # Real _locked_job_then_slot -> _locked_job_state -> _locked_slot -> kind helper.
    return [(JOB_ID,), ("running",), (JOB_ID, state, NAME, HASH), (kind,)]


def _success():
    scope = ArtifactScope("pipeline", "job", JOB.job_key)
    member = ArtifactMember("unit_draft", "draft", 1, scope, canonical_payload_hash("{}"), "{}")
    wire = [{"artifact_type": member.artifact_type, "logical_id": member.logical_id,
             "revision": 1, "scope": {"namespace": scope.namespace, "kind": scope.kind, "key": scope.key},
             "content_hash": member.content_hash, "payload_json": {}}]
    return CommandSuccess(SLOT_ID, canonical_payload_hash(json.dumps(wire)), (member,))


def _reserve(store):
    return store.reserve_generation_attempt(
        SLOT_ID, HASH, provider_id="synthetic-provider", provider_idempotency_key="synthetic-key", request_payload=BLOB,
    )


def test_new_command_claim_persists_explicit_generation_kind():
    store, cursor, connection = _store([(JOB_ID, "test"), ("pending",), None])
    outcome = store.claim_command(CommandClaim(JOB, "new-command", NAME, HASH, execution_kind="generation"))
    assert outcome.state == "running" and outcome.is_fresh_claim
    insert = next(params for sql, params in cursor.calls if "INSERT INTO runtime.command_slots" in sql)
    assert insert[3:] == (NAME, HASH, "generation")
    assert not cursor.rows
    connection.commit.assert_called_once()


@pytest.mark.parametrize("terminal", ["success", "rejection"])
def test_new_generation_reserves_commits_and_replays_through_real_kind_and_lock_checks(monkeypatch, terminal):
    store, cursor, connection = _store([*_slot_rows("generation"), None])
    monkeypatch.setattr(store, "_claimed_blob_ref", Mock(return_value=BLOB))
    # Reservation generates its real UUID; only the persisted-attempt read is an I/O seam.
    reader = Mock(side_effect=lambda _cursor, attempt_id, **_kwargs: GenerationAttempt(
        attempt_id, JOB_ID, SLOT_ID, HASH, "synthetic-provider", "synthetic-key", BLOB, "reserved", 0,
    ))
    monkeypatch.setattr(store, "_read_generation_attempt_by_id", reader)
    reserved = _reserve(store)
    assert reserved.is_fresh_reservation and reserved.state == "reserved"
    assert any("INSERT INTO runtime.generation_attempts" in sql for sql, _ in cursor.calls)
    cursor.rows.extend([(JOB_ID, SLOT_ID), *_slot_rows("generation")])
    success, rejection = _success(), CommandRejection(SLOT_ID, "invalid_draft", "{}")
    if terminal == "success":
        responded = replace(reserved, state="responded", version=1, raw_response=BLOB)
        committed = replace(responded, state="committed", version=2, receipt_id=RECEIPT_ID, artifact_set_id=SET_ID)
        reader.side_effect = [responded, committed]
        writer = Mock(return_value=CommandOutcome(SLOT_ID, "succeeded", receipt_id=RECEIPT_ID, artifact_set_id=SET_ID))
        monkeypatch.setattr(store, "_write_success", writer)
        result = store.commit_generation_success(reserved.attempt_id, expected_version=1, success=success)
        assert result == committed
        writer.assert_called_once_with(cursor, success, JOB_ID)
        cursor.rows.extend([(JOB_ID, SLOT_ID), *_slot_rows("generation", "succeeded"), (success.set_hash,)])
        reader.side_effect = [committed]
        assert store.commit_generation_success(reserved.attempt_id, expected_version=1, success=success) == committed
    else:
        failed = replace(reserved, state="failed", version=1, failure_code="invalid_draft", failure_detail_json="{}", failure_disposition="nonretryable")
        reader.side_effect = [failed]
        cursor.rows.append((reserved.attempt_id,))
        writer = Mock(return_value=CommandOutcome(SLOT_ID, "denied", receipt_id=RECEIPT_ID))
        monkeypatch.setattr(store, "_write_rejection", writer)
        assert store.commit_generation_rejection(reserved.attempt_id, expected_version=1, rejection=rejection).receipt_id == RECEIPT_ID
        writer.assert_called_once_with(cursor, rejection, JOB_ID)
        cursor.rows.extend([(JOB_ID, SLOT_ID), *_slot_rows("generation", "denied"), ("denied", RECEIPT_ID, None, "invalid_draft", "{}")])
        reader.side_effect = [failed]
        assert store.commit_generation_rejection(reserved.attempt_id, expected_version=1, rejection=rejection).receipt_id == RECEIPT_ID
    writer.assert_called_once()  # Terminal replay never writes a second result.
    assert sum("SELECT execution_kind" in sql for sql, _ in cursor.calls) == 3
    assert sum("INSERT INTO runtime.generation_receipt_attempts" in sql for sql, _ in cursor.calls) == 1
    assert not cursor.rows and connection.commit.call_count == 3


@pytest.mark.parametrize("state", ["running", "succeeded", "denied"])
def test_deterministic_slot_cannot_reserve_even_for_same_new_command(state):
    store, cursor, connection = _store(_slot_rows("deterministic", state))
    with pytest.raises(CommandStateError, match="execution kind must be generation"):
        _reserve(store)
    assert not cursor.rows
    assert all(not sql.startswith(("INSERT", "UPDATE")) for sql, _ in cursor.calls)
    connection.rollback.assert_called_once()


@pytest.mark.parametrize("operation", ["success", "rejection"])
@pytest.mark.parametrize("state", ["running", "succeeded", "denied"])
def test_ordinary_commit_cannot_complete_or_replay_generation_slot(monkeypatch, operation, state):
    store, cursor, connection = _store(_slot_rows("generation", state))
    writer = Mock(side_effect=AssertionError("generic writer must be unreachable"))
    monkeypatch.setattr(store, "_write_success", writer)
    monkeypatch.setattr(store, "_write_rejection", writer)
    with pytest.raises(CommandStateError, match="execution kind must be deterministic"):
        if operation == "success":
            store.commit_command_success(_success())
        else:
            store.commit_command_rejection(CommandRejection(SLOT_ID, "invalid_draft", "{}"))
    writer.assert_not_called()
    assert not cursor.rows
    assert all("command_receipts" not in sql for sql, _ in cursor.calls)
    connection.rollback.assert_called_once()


@pytest.mark.parametrize("existing,requested", [("generation", "deterministic"), ("deterministic", "generation")])
def test_same_key_name_and_request_with_different_kind_conflicts(existing, requested):
    store, cursor, connection = _store([None, (JOB_ID, "test"), ("running",), (SLOT_ID, NAME, HASH, existing)])
    with pytest.raises(IdempotencyConflictError):
        store.claim_command(CommandClaim(JOB, "same-key", NAME, HASH, execution_kind=requested))
    assert not cursor.rows
    assert all("INSERT INTO runtime.command_slots" not in sql for sql, _ in cursor.calls)
    connection.rollback.assert_called_once()


@pytest.mark.parametrize("name", [CALIBRATION_VALIDATOR_COMMAND, BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND,
                                 "FinalizeRunOutcome", VLM_BATCH_FINALIZER_COMMAND_NAME])
def test_protected_command_cannot_acquire_generation_kind_through_generic_claim(name):
    factory = Mock(side_effect=AssertionError("protected claim must reject before I/O"))
    store = PostgresRuntimeStore(factory)
    with pytest.raises(CommandStateError):
        store.claim_command(CommandClaim(JOB, "protected", name, HASH, execution_kind="generation"))
    factory.assert_not_called()


def test_batch_owner_api_also_rejects_generation_kind_before_io():
    factory = Mock(side_effect=AssertionError("batch claim must reject before I/O"))
    store = PostgresRuntimeStore(factory)
    with pytest.raises(CommandStateError, match="deterministic"):
        store.claim_vlm_batch_command(CommandClaim(JOB, VLM_BATCH_IDEMPOTENCY_PREFIX + "unit",
                                                  VLM_BATCH_FINALIZER_COMMAND_NAME, HASH, execution_kind="generation"))
    factory.assert_not_called()
