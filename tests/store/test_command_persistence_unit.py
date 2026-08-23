"""Unit coverage for closed semantic persistence request objects."""

import hashlib
import json
from uuid import uuid4

import pytest
from autocut_kernel.store import (
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
    PersistenceConflictError,
    RuntimeStoreError,
    StaleHeadError,
    StoreValidationError,
)
from autocut_kernel.store.postgres import PostgresRuntimeStore
from psycopg import ProgrammingError


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def test_command_claim_requires_canonical_digest_and_identity() -> None:
    with pytest.raises(StoreValidationError, match="request_hash"):
        CommandClaim(Job("fixture-job", "test"), "run-1", "preflight", "not-a-hash")


def test_command_outcome_defaults_to_a_replay_claim() -> None:
    outcome = CommandOutcome(command_slot_id=uuid4(), state="running")

    assert outcome.is_fresh_claim is False


def test_success_requires_a_non_empty_set_with_bound_member_hash() -> None:
    member = ArtifactMember(
        artifact_type="media_evidence",
        logical_id="preflight",
        revision=1,
        scope=ArtifactScope("pipeline", "job", "fixture-job"),
        content_hash=digest("evidence"),
        payload_json='{"ready":true}',
    )
    with pytest.raises(StoreValidationError, match="set_hash must bind"):
        CommandSuccess(command_slot_id=uuid4(), set_hash=digest("wrong"), artifacts=(member,))


def test_success_accepts_exact_canonical_member_set_hash() -> None:
    member = ArtifactMember(
        artifact_type="media_evidence",
        logical_id="preflight",
        revision=1,
        scope=ArtifactScope("pipeline", "job", "fixture-job"),
        content_hash=digest("evidence"),
        payload_json=json.dumps({"ready": True}),
    )
    canonical = [
        {
            "artifact_type": member.artifact_type,
            "content_hash": member.content_hash,
            "logical_id": member.logical_id,
            "payload_json": {"ready": True},
            "revision": 1,
            "scope": {"key": "fixture-job", "kind": "job", "namespace": "pipeline"},
        }
    ]
    set_hash = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode()
        ).hexdigest()
    )
    success = CommandSuccess(command_slot_id=uuid4(), set_hash=set_hash, artifacts=(member,))
    assert success.expected_set_hash == set_hash


def test_terminal_rejection_requires_structured_failure_detail() -> None:
    with pytest.raises(StoreValidationError, match="failure_detail_json"):
        CommandRejection(uuid4(), "PRECHECK_DENY", "")


def test_terminal_rejection_requires_valid_json_failure_detail() -> None:
    with pytest.raises(StoreValidationError, match="failure_detail_json must contain JSON"):
        CommandRejection(uuid4(), "PRECHECK_DENY", "not json")


@pytest.mark.parametrize("outcome", ("success", "running", "", None))
def test_rejection_rejects_non_terminal_outcomes(outcome: object) -> None:
    with pytest.raises(StoreValidationError, match="outcome must be 'denied' or 'failed'"):
        CommandRejection(uuid4(), "PRECHECK_DENY", "{}", outcome=outcome)  # type: ignore[arg-type]


@pytest.mark.parametrize("slot_id", ("not-a-uuid", 1, None))
def test_rejection_requires_a_uuid_slot(slot_id: object) -> None:
    with pytest.raises(StoreValidationError, match="command_slot_id must be a UUID"):
        CommandRejection(slot_id, "PRECHECK_DENY", "{}")  # type: ignore[arg-type]


@pytest.mark.parametrize("detail", ('{"value": NaN}', '{"value": Infinity}', "[]", '"text"', "null"))
def test_rejection_requires_finite_json_object(detail: str) -> None:
    with pytest.raises(StoreValidationError):
        CommandRejection(uuid4(), "PRECHECK_DENY", detail)


def test_rejection_supports_failed_as_well_as_denied_outcome() -> None:
    rej = CommandRejection(uuid4(), "RUNTIME_CRASH", '{"reason":"unexpected"}', outcome="failed")
    assert rej.outcome == "failed"
    assert rej.failure_code == "RUNTIME_CRASH"


class _UniqueViolationError(Exception):
    sqlstate = "23505"

    def __init__(self, constraint_name: str) -> None:
        self.diag = type("Diagnostic", (), {"constraint_name": constraint_name})()


class _UniqueViolationCursor:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        raise self._error

    def fetchone(self) -> None:
        return None

    def close(self) -> None:
        pass


class _UniqueViolationConnection:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def cursor(self) -> _UniqueViolationCursor:
        return _UniqueViolationCursor(self._error)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def _store_with_unique_violation(constraint_name: str) -> PostgresRuntimeStore:
    error = _UniqueViolationError(constraint_name)
    return PostgresRuntimeStore(lambda: _UniqueViolationConnection(error))


def test_first_head_unique_violation_maps_to_stale_head_error() -> None:
    store = _store_with_unique_violation("runtime_artifacts_scope_revision_key")

    with pytest.raises(StaleHeadError, match="logical artifact head"):
        store.claim_command(CommandClaim(Job("unique-head", "test"), "cmd", "x", digest("x")))


@pytest.mark.parametrize(
    "constraint_name",
    ("runtime_command_slots_job_id_idempotency_key_key", "runtime_other_unique_key"),
)
def test_other_unique_violations_map_to_persistence_conflict_error(constraint_name: str) -> None:
    store = _store_with_unique_violation(constraint_name)

    with pytest.raises(PersistenceConflictError, match="uniqueness constraint"):
        store.claim_command(CommandClaim(Job("unique-other", "test"), "cmd", "x", digest("x")))


def test_programming_database_errors_are_mapped_to_runtime_store_error() -> None:
    class Cursor:
        def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
            raise ProgrammingError("broken SQL")

        def fetchone(self) -> None:
            return None

        def close(self) -> None:
            pass

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            pass

    store = PostgresRuntimeStore(Connection)
    with pytest.raises(RuntimeStoreError, match="database operation failed"):
        store.claim_command(CommandClaim(Job("programming-error", "test"), "cmd", "x", digest("x")))
