"""Real reader/value checks over scripted SQL I/O, not PostgreSQL acceptance."""

import json
from collections import deque
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from autocut_kernel.store.errors import (
    JobProfileMismatchError,
    SemanticInputIntegrityError,
    SemanticInputUnavailableError,
    StoreValidationError,
)
from autocut_kernel.store.models import (
    ArtifactMember,
    ArtifactScope,
    BlobRef,
    CommandSuccess,
    CommittedArtifactMemberReference,
    GenerationAttempt,
    Job,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
    canonical_payload_hash,
)
from autocut_kernel.store.postgres import PostgresRuntimeStore

JOB_ID, SLOT_ID, RECEIPT_ID, SET_ID = (UUID(int=value) for value in range(1, 5))
JOB = Job("synthetic-exact-set", "test")
NAME = "BuildNarrativeGraph@2.1.3"
REQUEST_HASH = canonical_payload_hash('{"request":"synthetic"}')
SCOPE = ArtifactScope("pipeline", "job", JOB.job_key)
MEMBERS = tuple(
    ArtifactMember(kind, kind, 1, SCOPE, canonical_payload_hash(raw), raw)
    for kind, raw in (("unit_first", '{"value":"原始"}'), ("unit_second", '{"value":2}'))
)
SET_HASH = canonical_payload_hash(json.dumps([
    {"artifact_type": member.artifact_type, "logical_id": member.logical_id,
     "revision": member.revision, "scope": {"namespace": SCOPE.namespace, "kind": SCOPE.kind, "key": SCOPE.key},
     "content_hash": member.content_hash, "payload_json": json.loads(member.payload_json)}
    for member in MEMBERS
]))


def _header(*, kind="generation"):
    return (SLOT_ID, NAME, SET_HASH, len(MEMBERS), REQUEST_HASH, kind)


def _member_rows():
    return [
        (ordinal, member.artifact_type, member.logical_id, member.revision,
         member.scope.namespace, member.scope.kind, member.scope.key,
         member.content_hash, member.payload_json)
        for ordinal, member in enumerate(MEMBERS)
    ]


class _Cursor:
    rowcount = 1

    def __init__(self, rows):
        self.rows = deque(rows)
        self.calls = []
        self.closed = False

    def execute(self, query, params=()):
        self.calls.append((" ".join(query.split()), params))

    def fetchone(self):
        assert self.rows, "reader issued an unexpected database read"
        return self.rows.popleft()

    def close(self):
        self.closed = True


def _store(rows=None):
    cursor = _Cursor(
        [(JOB_ID, JOB.profile), _header(), None, *_member_rows(), None]
        if rows is None else rows
    )
    connection = SimpleNamespace(cursor=lambda: cursor, commit=Mock(), rollback=Mock(), close=Mock())
    return PostgresRuntimeStore(lambda: connection), cursor, connection


def _read(store, **changes):
    arguments = dict(job=JOB, command_slot_id=SLOT_ID, receipt_id=RECEIPT_ID,
                     artifact_set_id=SET_ID, expected_request_hash=REQUEST_HASH,
                     expected_command_name=NAME, expected_execution_kind="generation")
    arguments.update(changes)
    return store.read_committed_artifact_set(**arguments)


@pytest.mark.parametrize("kind", ["generation", "deterministic"])
def test_public_reader_returns_actual_ordered_full_references_in_one_transaction(kind):
    store, cursor, connection = _store([(JOB_ID, JOB.profile), _header(kind=kind), None, *_member_rows(), None])
    result = _read(store, expected_execution_kind=kind)
    assert type(result) is PersistedCommittedArtifactSet
    assert (result.job, result.job_id, result.command_slot_id, result.receipt_id, result.artifact_set_id) == (
        JOB, JOB_ID, SLOT_ID, RECEIPT_ID, SET_ID,
    )
    assert (result.request_hash, result.command_name, result.execution_kind, result.set_hash) == (
        REQUEST_HASH, NAME, kind, SET_HASH,
    )
    assert result.artifacts == MEMBERS
    assert result.references == tuple(
        CommittedArtifactMemberReference(RECEIPT_ID, SET_ID, ordinal, member.scope,
                                         member.artifact_type, member.logical_id, member.revision, member.content_hash)
        for ordinal, member in enumerate(MEMBERS)
    )
    assert all(member.command_slot_id == SLOT_ID for member in result.members)
    assert not hasattr(result, "accepted") and not hasattr(result, "admission")
    assert [params for _sql, params in cursor.calls] == [
        (JOB.job_key,), (JOB_ID, RECEIPT_ID, SET_ID), (JOB_ID, SET_ID),
    ]
    header_sql, member_sql = cursor.calls[1][0], cursor.calls[2][0]
    for required in (
        "slot.command_slot_id = receipt.command_slot_id", "slot.job_id = %s",
        "artifact_set.command_slot_id = slot.command_slot_id", "artifact_set.job_id = slot.job_id",
        "receipt.receipt_id = %s", "receipt.result_artifact_set_id = %s",
        "receipt.outcome = 'succeeded'", "slot.state = 'succeeded'",
        "slot.request_hash", "slot.execution_kind",
    ):
        assert required in header_sql
    for required in ("artifact.artifact_set_id = member.artifact_set_id", "artifact.job_id = %s",
                     "member.artifact_set_id = %s", "ORDER BY member.ordinal"):
        assert required in member_sql
    assert all(sql.startswith("SELECT") and "logical_heads" not in sql and "LIMIT" not in sql for sql, _ in cursor.calls)
    assert not cursor.rows and cursor.closed
    connection.commit.assert_called_once()
    connection.rollback.assert_not_called()
    connection.close.assert_called_once()


@pytest.mark.parametrize("field,value", [
    ("command_slot_id", UUID(int=90)), ("expected_command_name", "OtherCommand"),
    ("expected_execution_kind", "deterministic"), ("expected_request_hash", "sha256:" + "a" * 64),
])
def test_valid_but_foreign_expected_producer_identity_is_rejected(field, value):
    store, _cursor, connection = _store()
    with pytest.raises(SemanticInputIntegrityError, match="producer/request identity differs"):
        _read(store, **{field: value})
    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()


@pytest.mark.parametrize("field", ["receipt_id", "artifact_set_id"])
def test_missing_exact_receipt_or_set_is_not_replaced_by_another_result(field):
    store, cursor, connection = _store([(JOB_ID, JOB.profile), None])
    foreign = UUID(int=99)
    with pytest.raises(SemanticInputUnavailableError):
        _read(store, **{field: foreign})
    assert cursor.calls[-1][1] == (JOB_ID, foreign if field == "receipt_id" else RECEIPT_ID,
                                  foreign if field == "artifact_set_id" else SET_ID)
    assert len(cursor.calls) == 2
    connection.rollback.assert_called_once()


@pytest.mark.parametrize("rows,error", [
    ([None], SemanticInputUnavailableError),
    ([(JOB_ID, "shadow")], JobProfileMismatchError),
    ([(JOB_ID, JOB.profile), None], SemanticInputUnavailableError),
    ([(JOB_ID, JOB.profile), _header(), _header(), None], SemanticInputUnavailableError),
])
def test_job_profile_and_unique_succeeded_set_join_fail_closed(rows, error):
    store, cursor, connection = _store(rows)
    with pytest.raises(error):
        _read(store)
    assert len(cursor.calls) <= 2 and not cursor.rows
    connection.rollback.assert_called_once()


@pytest.mark.parametrize("mutation", [
    "missing_member", "duplicate_ordinal", "ordinal_gap", "reversed_order", "count",
    "set_hash", "payload_hash", "scope", "revision", "empty", "duplicate_chain",
])
def test_set_closure_recomputes_membership_payload_and_ordered_hash(mutation):
    header, rows = list(_header()), _member_rows()
    if mutation == "missing_member":
        rows.pop()
    elif mutation == "duplicate_ordinal":
        rows[1] = (0, *rows[1][1:])
    elif mutation == "ordinal_gap":
        rows[1] = (2, *rows[1][1:])
    elif mutation == "reversed_order":
        rows = [(index, *row[1:]) for index, row in enumerate(reversed(rows))]
    elif mutation == "count":
        header[3] = 3
    elif mutation == "set_hash":
        header[2] = "sha256:" + "b" * 64
    elif mutation == "payload_hash":
        rows[0] = (*rows[0][:-1], '{"value":"rewritten"}')
    elif mutation == "scope":
        row = list(rows[0])
        row[6] = "foreign-job"
        rows[0] = tuple(row)
    elif mutation == "revision":
        row = list(rows[0])
        row[3] = 2
        rows[0] = tuple(row)
    elif mutation == "empty":
        header[3], rows = 0, []
    else:
        rows[1] = (1, *rows[0][1:])
    store, cursor, connection = _store([(JOB_ID, JOB.profile), tuple(header), None, *rows, None])
    with pytest.raises(SemanticInputIntegrityError):
        _read(store)
    assert cursor.closed
    connection.rollback.assert_called_once()


@pytest.mark.parametrize("field,value", [
    ("job", "synthetic"), ("job", Job("synthetic", "unknown")),
    ("command_slot_id", str(SLOT_ID)), ("receipt_id", True), ("artifact_set_id", None),
    ("expected_request_hash", "SHA256:" + "A" * 64), ("expected_request_hash", 1),
    ("expected_command_name", ""), ("expected_command_name", True),
    ("expected_execution_kind", "unknown"), ("expected_execution_kind", True),
])
def test_invalid_selector_rejected_before_any_io(field, value):
    factory = Mock(side_effect=AssertionError("invalid input opened a connection"))
    with pytest.raises(StoreValidationError):
        _read(PostgresRuntimeStore(factory), **{field: value})
    factory.assert_not_called()


def test_existing_full_anchor_wrapper_uses_same_set_checks_and_still_checks_exact_member():
    reference = CommittedArtifactMemberReference(RECEIPT_ID, SET_ID, 1, SCOPE,
                                                 MEMBERS[1].artifact_type, MEMBERS[1].logical_id,
                                                 1, MEMBERS[1].content_hash)
    store, cursor, _connection = _store()
    result = store._read_exact_committed_set(cursor, JOB, reference)
    assert result.members == tuple(enumerate(MEMBERS))
    for changed in (replace(reference, content_hash="sha256:" + "a" * 64),
                    replace(reference, member_ordinal=2)):
        store, cursor, _connection = _store()
        with pytest.raises(SemanticInputUnavailableError, match="member identity"):
            store._read_exact_committed_set(cursor, JOB, changed)


def test_full_reference_mapping_round_trip_freshness_and_strict_scope():
    result = _read(_store()[0])
    reference = result.references[0]
    assert CommittedArtifactMemberReference.from_mapping(reference.to_mapping()) == reference
    wire = reference.to_mapping()
    wire["scope"]["key"] = "modified-copy"
    assert reference.scope == SCOPE
    with pytest.raises(FrozenInstanceError):
        result.set_hash = "sha256:" + "b" * 64


@pytest.mark.parametrize("field", [
    "receipt_id", "artifact_set_id", "member_ordinal", "scope", "artifact_type", "logical_id", "revision", "content_hash",
])
@pytest.mark.parametrize("change", ["missing", "unknown"])
def test_reference_decoder_requires_exact_root_fields(field, change):
    wire = _read(_store()[0]).references[0].to_mapping()
    value = wire.pop(field)
    if change == "unknown":
        wire["unknown_" + field] = value
    with pytest.raises(StoreValidationError):
        CommittedArtifactMemberReference.from_mapping(wire)


@pytest.mark.parametrize("field,value", [
    ("receipt_id", RECEIPT_ID), ("receipt_id", "bad-uuid"), ("artifact_set_id", 3),
    ("member_ordinal", True), ("member_ordinal", 1.0), ("member_ordinal", -1),
    ("revision", False), ("revision", 1.0), ("revision", 0),
    ("artifact_type", 1), ("artifact_type", ""), ("logical_id", False),
    ("content_hash", "sha256:" + "A" * 64), ("content_hash", "bad"),
    ("scope", []), ("scope", {"namespace": "x", "kind": "y"}),
    ("scope", {"namespace": "x", "kind": "y", "key": "z", "extra": True}),
    ("scope", {"namespace": "x", "kind": "y", "key": False}),
])
def test_reference_decoder_rejects_wrong_primitive_hash_and_scope(field, value):
    wire = _read(_store()[0]).references[0].to_mapping()
    wire[field] = value
    with pytest.raises(StoreValidationError):
        CommittedArtifactMemberReference.from_mapping(wire)


@pytest.mark.parametrize("change", ["slot", "receipt", "set", "ordinal", "set_hash", "members"])
def test_persisted_set_value_cannot_misbind_its_members(change):
    result = _read(_store()[0])
    fields = {
        "slot": {"command_slot_id": UUID(int=90)}, "receipt": {"receipt_id": UUID(int=90)},
        "set": {"artifact_set_id": UUID(int=90)}, "ordinal": {"members": tuple(reversed(result.members))},
        "set_hash": {"set_hash": "sha256:" + "b" * 64}, "members": {"members": list(result.members)},
    }
    with pytest.raises(StoreValidationError):
        replace(result, **fields[change])


def test_public_set_hash_is_the_existing_ordered_store_hash():
    assert artifact_set_hash(MEMBERS) == SET_HASH
    assert CommandSuccess(SLOT_ID, SET_HASH, MEMBERS).expected_set_hash == SET_HASH
    assert artifact_set_hash(tuple(reversed(MEMBERS))) != SET_HASH
    reformatted = replace(MEMBERS[0], payload_json=json.dumps(json.loads(MEMBERS[0].payload_json), indent=2))
    assert artifact_set_hash((reformatted, MEMBERS[1])) == SET_HASH


def _attempts(count=2):
    request = BlobRef(UUID(int=10), REQUEST_HASH, 23, "application/json")
    response = BlobRef(UUID(int=11), canonical_payload_hash('{"draft":true}'), 14, "application/json")
    return tuple(
        GenerationAttempt(
            UUID(int=20 + index), JOB_ID, SLOT_ID, REQUEST_HASH,
            "synthetic-provider", f"synthetic-key-{index + 1}", request,
            "committed" if index == count - 1 else "failed", 3,
            raw_response=response if index == count - 1 else None,
            receipt_id=RECEIPT_ID if index == count - 1 else None,
            artifact_set_id=SET_ID if index == count - 1 else None,
            failure_code=None if index == count - 1 else "transient",
            failure_detail_json=None if index == count - 1 else "{}",
            failure_disposition=None if index == count - 1 else "retryable",
            attempt_ordinal=index + 1,
            previous_attempt_id=None if index == 0 else UUID(int=19 + index),
            max_attempts=count,
        )
        for index in range(count)
    )


def _attempt_row(attempt):
    request, response = attempt.request_payload, attempt.raw_response
    return (
        attempt.job_id, attempt.command_slot_id, attempt.request_hash, attempt.provider_id,
        attempt.provider_idempotency_key, request.object_id, request.content_hash,
        request.byte_length, request.media_type, attempt.state, attempt.version,
        attempt.provider_request_id,
        *(tuple(getattr(response, field) for field in ("object_id", "content_hash", "byte_length", "media_type"))
          if response is not None else (None,) * 4),
        attempt.receipt_id, attempt.artifact_set_id, attempt.failure_code,
        attempt.failure_detail_json, attempt.attempt_ordinal, attempt.previous_attempt_id,
        attempt.retry_policy_hash, attempt.max_attempts, attempt.failure_disposition,
        attempt.dispatch_lease_token, attempt.dispatch_lease_expires_at, attempt.not_before_at,
        attempt.retry_backoff_seconds,
    )


def _links(attempts):
    return [(RECEIPT_ID, item.attempt_id, item.attempt_ordinal, JOB_ID, SLOT_ID) for item in attempts]


def _chain_store(attempts=None, *, links=None, header=None):
    attempts = _attempts() if attempts is None else attempts
    return _store([
        (JOB_ID, JOB.profile), _header() if header is None else header, None, *_member_rows(), None,
        *((item.attempt_id,) for item in attempts), None,
        *(_attempt_row(item) for item in attempts),
        *(_links(attempts) if links is None else links), None,
    ])


def _read_chain(store, **changes):
    fields = dict(command_slot_id=SLOT_ID, receipt_id=RECEIPT_ID,
                  artifact_set_id=SET_ID, expected_request_hash=REQUEST_HASH)
    fields.update(changes)
    return store.read_committed_generation_attempt_chain(JOB, **fields)


@pytest.mark.parametrize("count", [1, 2, 3])
def test_committed_attempt_chain_reads_actual_attempts_and_all_receipt_links_once(count):
    attempts = _attempts(count)
    store, cursor, connection = _chain_store(attempts)
    assert _read_chain(store) == attempts
    assert not cursor.rows
    sql, params = cursor.calls[-1]
    assert "FULL JOIN runtime.generation_attempts" in sql
    assert "link.receipt_id = %s OR attempt.command_slot_id = %s" in sql
    assert params == (RECEIPT_ID, SLOT_ID)
    attempt_queries = [params for sql, params in cursor.calls if "WHERE attempt.attempt_id = %s" in sql]
    assert attempt_queries == [(item.attempt_id,) for item in attempts]
    assert all(sql.startswith("SELECT") for sql, _ in cursor.calls)
    connection.commit.assert_called_once()
    # Replay rereads exactly the same persisted values; no dispatch/write helper exists here.
    replay, _cursor, _connection = _chain_store(attempts)
    assert _read_chain(replay) == attempts


@pytest.mark.parametrize("change", [
    "empty", "duplicate", "job", "slot", "request", "ordinal", "previous",
    "provider", "request_blob", "retry_policy", "budget", "nonretryable", "repairable",
    "predecessor_committed", "final_responded", "receipt", "set",
])
def test_committed_attempt_chain_rejects_broken_identity_or_lifecycle(change):
    first, final = _attempts()
    if change == "empty":
        attempts = ()
    elif change == "duplicate":
        attempts = (first, first)
    elif change == "predecessor_committed":
        attempts = (replace(first, state="committed", raw_response=final.raw_response,
                            receipt_id=RECEIPT_ID, artifact_set_id=SET_ID,
                            failure_code=None, failure_detail_json=None, failure_disposition=None), final)
    elif change in ("nonretryable", "repairable"):
        attempts = (replace(first, failure_disposition=change), final)
    else:
        changes = {
            "job": {"job_id": UUID(int=90)}, "slot": {"command_slot_id": UUID(int=90)},
            "request": {"request_hash": "sha256:" + "a" * 64},
            "ordinal": {"attempt_ordinal": 3, "max_attempts": 3},
            "previous": {"previous_attempt_id": UUID(int=90)}, "provider": {"provider_id": "foreign"},
            "request_blob": {"request_payload": replace(final.request_payload, object_id=UUID(int=90))},
            "retry_policy": {"retry_policy_hash": "sha256:" + "a" * 64}, "budget": {"max_attempts": 3},
            "final_responded": {"state": "responded", "receipt_id": None, "artifact_set_id": None},
            "receipt": {"receipt_id": UUID(int=90)}, "set": {"artifact_set_id": UUID(int=90)},
        }
        attempts = (first, replace(final, **changes[change]))
    store, _cursor, connection = _chain_store(attempts)
    with pytest.raises(SemanticInputIntegrityError):
        _read_chain(store)
    connection.rollback.assert_called_once()


@pytest.mark.parametrize("change", [
    "missing", "duplicate", "extra", "foreign_receipt", "foreign_attempt", "wrong_ordinal",
    "foreign_job", "foreign_slot", "unlinked_foreign_attempt", "boolean_ordinal", "float_ordinal",
])
def test_receipt_attempt_links_must_exactly_cover_whole_chain(change):
    attempts = _attempts()
    links = _links(attempts)
    if change == "missing":
        links.pop()
    elif change == "duplicate":
        links.append(links[0])
    elif change in ("extra", "unlinked_foreign_attempt"):
        links.append((RECEIPT_ID if change == "extra" else None, UUID(int=99), 3, UUID(int=98), SLOT_ID))
    else:
        field, value = {
            "foreign_receipt": (0, UUID(int=99)), "foreign_attempt": (1, UUID(int=99)),
            "wrong_ordinal": (2, 3), "foreign_job": (3, UUID(int=99)), "foreign_slot": (4, UUID(int=99)),
            "boolean_ordinal": (2, True), "float_ordinal": (2, 1.0),
        }[change]
        row = list(links[0])
        row[field] = value
        links[0] = tuple(row)
    store, _cursor, connection = _chain_store(attempts, links=links)
    with pytest.raises(SemanticInputIntegrityError, match="Receipt links"):
        _read_chain(store)
    connection.rollback.assert_called_once()


@pytest.mark.parametrize("change", ["slot", "request", "kind"])
def test_committed_chain_checks_terminal_producer_before_attempt_reads(change):
    header = list(_header())
    index, value = {"slot": (0, UUID(int=90)), "request": (4, "sha256:" + "a" * 64), "kind": (5, "deterministic")}[change]
    header[index] = value
    store, cursor, _connection = _chain_store(header=tuple(header))
    with pytest.raises(SemanticInputIntegrityError, match="slot/request"):
        _read_chain(store)
    assert all("generation_attempts" not in sql for sql, _ in cursor.calls)


def test_old_nonterminal_chain_reader_keeps_existing_behavior_after_extraction():
    attempts = _attempts()
    store, cursor, _connection = _store([
        (JOB_ID, JOB.profile), *((item.attempt_id,) for item in attempts), None,
        *(_attempt_row(item) for item in attempts),
    ])
    assert store.read_generation_attempt_chain(JOB, SLOT_ID) == attempts
    assert not cursor.rows
    assert all("generation_receipt_attempts" not in sql for sql, _ in cursor.calls)


def test_receipt_links_accept_uuid_driver_text_without_weakening_identity():
    attempts = _attempts()
    links = [tuple(str(value) if isinstance(value, UUID) else value for value in row) for row in _links(attempts)]
    store, _cursor, _connection = _chain_store(attempts, links=links)
    assert _read_chain(store) == attempts


@pytest.mark.parametrize("field,value", [
    ("command_slot_id", str(SLOT_ID)), ("receipt_id", None), ("artifact_set_id", True),
    ("expected_request_hash", "bad"),
])
def test_invalid_committed_chain_selector_rejected_before_io(field, value):
    factory = Mock(side_effect=AssertionError("invalid chain selector opened a connection"))
    with pytest.raises(StoreValidationError):
        _read_chain(PostgresRuntimeStore(factory), **{field: value})
    factory.assert_not_called()
