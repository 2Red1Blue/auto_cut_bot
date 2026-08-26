"""Public Store reader over strict fake SQL I/O; not PostgreSQL acceptance."""

from collections import deque
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from autocut_kernel.store.errors import (
    SemanticInputIntegrityError,
    SemanticInputUnavailableError,
    StoreValidationError,
)
from autocut_kernel.store.models import Job
from autocut_kernel.store.postgres import PostgresRuntimeStore
from autocut_kernel.store.terminal_receipts import PersistedTerminalCommandReceipt

JOB = Job("synthetic-terminal-reader", "test")
JOB_ID, SLOT_ID, RECEIPT_ID = (UUID(int=value) for value in range(1, 4))
REQUEST_HASH = "sha256:" + "a" * 64
NAME = "SyntheticWindowChild@2.1.3"
DETAIL = '{"reason": "队列忙", "proof": {"ordinal": 1}}'
LIMIT = 1024


def _row(*, kind="deterministic", outcome="failed", detail=DETAIL):
    return (JOB.job_key, JOB.profile, JOB_ID, SLOT_ID, RECEIPT_ID,
            REQUEST_HASH, NAME, kind, outcome, outcome, None, "BUSY", detail,
            len(detail.encode("utf-8")))


class _Cursor:
    rowcount = 1

    def __init__(self, rows, expected_params):
        self.rows = deque(rows)
        self.expected_params = expected_params
        self.calls = []
        self.closed = False

    def execute(self, query, params=()):
        sql = " ".join(query.split())
        assert not self.calls, "reader must use one snapshot SELECT"
        assert params == self.expected_params
        for required in (
            "FROM runtime.jobs AS job",
            "JOIN runtime.command_slots AS slot ON slot.job_id = job.job_id",
            "JOIN runtime.command_receipts AS receipt ON receipt.command_slot_id = slot.command_slot_id",
            "WHERE job.job_key = %s AND job.profile = %s",
            "slot.command_slot_id = %s AND receipt.receipt_id = %s",
            "slot.request_hash = %s AND slot.command_name = %s",
            "slot.execution_kind = %s",
            "receipt.outcome IN ('failed', 'denied')",
            "slot.state = receipt.outcome",
            "receipt.result_artifact_set_id IS NULL",
            "NOT EXISTS ( SELECT 1 FROM runtime.artifact_sets AS owned_set "
            "WHERE owned_set.command_slot_id = slot.command_slot_id )",
            "CASE WHEN octet_length(convert_to(receipt.failure_detail::text, 'UTF8')) <= %s "
            "THEN receipt.failure_detail::text ELSE NULL END",
            "AND octet_length(convert_to(receipt.failure_detail::text, 'UTF8')) <= %s",
        ):
            assert required in sql
        assert sql.startswith("SELECT")
        assert all(word not in sql for word in ("FOR UPDATE", "logical_heads", "LIMIT", "INSERT", "UPDATE", "DELETE"))
        self.calls.append((sql, params))

    def fetchone(self):
        assert self.rows, "unexpected read"
        return self.rows.popleft()

    def close(self):
        self.closed = True


def _arguments(**changes):
    arguments = dict(job=JOB, command_slot_id=SLOT_ID, receipt_id=RECEIPT_ID,
                     expected_request_hash=REQUEST_HASH, expected_command_name=NAME,
                     expected_execution_kind="deterministic", max_failure_detail_bytes=LIMIT)
    arguments.update(changes)
    return arguments


def _store(rows=None, **changes):
    args = _arguments(**changes)
    cursor = _Cursor([_row(), None] if rows is None else rows, (
        args["max_failure_detail_bytes"], args["job"].job_key, args["job"].profile,
        args["command_slot_id"], args["receipt_id"], args["expected_request_hash"],
        args["expected_command_name"], args["expected_execution_kind"], args["max_failure_detail_bytes"],
    ))
    connection = SimpleNamespace(cursor=lambda: cursor, commit=Mock(), rollback=Mock(), close=Mock())
    return PostgresRuntimeStore(lambda: connection), cursor, connection


def _read(store, **changes):
    return store.read_terminal_command_receipt(**_arguments(**changes))


@pytest.mark.parametrize("outcome", ["failed", "denied"])
@pytest.mark.parametrize("kind", ["deterministic", "generation"])
def test_exact_terminal_read_preserves_identities_and_logical_json_without_writes(outcome, kind):
    store, cursor, connection = _store([_row(kind=kind, outcome=outcome), None], expected_execution_kind=kind)
    result = _read(store, expected_execution_kind=kind)
    assert result == PersistedTerminalCommandReceipt(
        JOB, JOB_ID, SLOT_ID, RECEIPT_ID, REQUEST_HASH, NAME, kind, outcome, "BUSY", DETAIL,
    )
    assert result.failure_detail_json is DETAIL
    assert not hasattr(result, "retry_authorized") and not hasattr(result, "raw_response")
    with pytest.raises(FrozenInstanceError):
        result.failure_code = "OTHER"
    assert len(cursor.calls) == 1 and cursor.closed and not cursor.rows
    connection.commit.assert_called_once()
    connection.rollback.assert_not_called()
    connection.close.assert_called_once()


@pytest.mark.parametrize("field,value", [
    ("job", None), ("job", Job("valid", "unknown")), ("job", Job("\ud800", "test")),
    ("command_slot_id", str(SLOT_ID)), ("command_slot_id", True), ("receipt_id", None),
    ("expected_request_hash", "sha256:" + "A" * 64), ("expected_request_hash", 1),
    ("expected_command_name", " "), ("expected_command_name", b"name"),
    ("expected_command_name", "\ud800"), ("expected_execution_kind", "other"),
    ("expected_execution_kind", True), ("max_failure_detail_bytes", 0),
    ("max_failure_detail_bytes", -1), ("max_failure_detail_bytes", True),
    ("max_failure_detail_bytes", 1.0), ("max_failure_detail_bytes", "100"),
])
def test_invalid_request_is_rejected_before_connection(field, value):
    factory = Mock(side_effect=AssertionError("must not connect"))
    with pytest.raises(StoreValidationError):
        _read(PostgresRuntimeStore(factory), **{field: value})
    factory.assert_not_called()


@pytest.mark.parametrize("index,value", [
    (0, "other-job"), (0, b"synthetic-terminal-reader"), (1, "shadow"), (2, str(JOB_ID)),
    (3, UUID(int=40)), (4, UUID(int=41)), (5, "sha256:" + "b" * 64),
    (6, "OtherCommand"), (7, "generation"), (8, "running"), (8, "denied"),
    (9, "succeeded"), (10, UUID(int=44)), (11, None), (11, ""), (11, " "),
    (11, b"BUSY"), (11, "\ud800"), (12, None), (12, b"{}"),
    (13, True), (13, 1.0), (13, -1), (13, LIMIT + 1), (13, 1),
])
def test_defensive_returned_row_validation_does_not_trust_scripted_sql(index, value):
    row = list(_row())
    row[index] = value
    store, cursor, connection = _store([tuple(row), None])
    with pytest.raises(SemanticInputIntegrityError):
        _read(store)
    assert cursor.closed
    connection.commit.assert_not_called()
    connection.rollback.assert_called_once()
    connection.close.assert_called_once()


@pytest.mark.parametrize("rows,error", [
    ([None], SemanticInputUnavailableError),
    ([_row(), _row()], SemanticInputIntegrityError),
    ([_row()[:-1], None], SemanticInputIntegrityError),
    ([_row() + ("extra",), None], SemanticInputIntegrityError),
])
def test_no_arbitrary_or_incomplete_terminal_row(rows, error):
    store, cursor, _connection = _store(rows)
    with pytest.raises(error):
        _read(store)
    assert not cursor.rows


@pytest.mark.parametrize("field,value", [
    ("job", Job("another", "shadow")), ("command_slot_id", UUID(int=50)),
    ("receipt_id", UUID(int=51)), ("expected_request_hash", "sha256:" + "c" * 64),
    ("expected_command_name", "OtherCommand"), ("expected_execution_kind", "generation"),
])
def test_all_expected_identity_selectors_are_in_sql_parameters(field, value):
    # Missing join rows model no exact match, not a DB constraint acceptance test.
    store, _cursor, _connection = _store([None], **{field: value})
    with pytest.raises(SemanticInputUnavailableError):
        _read(store, **{field: value})


@pytest.mark.parametrize("raw", [
    "null", "[]", "1", '"text"', "{}{}", '{"x":}',
    '{"x":1,"x":2}', '{"nested":{"x":1,"x":2}}',
    '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}',
    '{"x":"\\ud800"}', '{"\\udfff":1}',
])
def test_invalid_logical_json_rejected_by_reader_and_direct_value(raw):
    store, _cursor, _connection = _store([_row(detail=raw), None])
    with pytest.raises(SemanticInputIntegrityError):
        _read(store)
    valid = PersistedTerminalCommandReceipt(
        JOB, JOB_ID, SLOT_ID, RECEIPT_ID, REQUEST_HASH, NAME, "deterministic", "failed", "BUSY", DETAIL,
    )
    with pytest.raises(StoreValidationError):
        replace(valid, failure_detail_json=raw)


@pytest.mark.parametrize("raw", [
    '{ "precise": 1.123456789012345678901234567890123456789, "large": 1e400 }',
    '{"large_integer":' + "9" * 5000 + '}',
    '{"z":true,"a":[null,{"中文":"值"}]}',
])
def test_logical_json_is_not_float_reserialized_or_canonicalized(raw):
    limit = len(raw.encode("utf-8"))
    store, _cursor, _connection = _store([_row(detail=raw), None], max_failure_detail_bytes=limit)
    result = _read(store, max_failure_detail_bytes=limit)
    assert result.failure_detail_json == raw


def test_utf8_bound_applies_in_sql_and_defensively_before_json_parsing():
    limit = len(DETAIL.encode("utf-8"))
    store, _cursor, _connection = _store(max_failure_detail_bytes=limit)
    assert _read(store, max_failure_detail_bytes=limit).failure_detail_json == DETAIL
    # A scripted row ignoring the SQL byte cap must still fail before JSON parse.
    for raw in (DETAIL, "坏" * limit):
        store, _cursor, _connection = _store([_row(detail=raw), None], max_failure_detail_bytes=limit - 1)
        with pytest.raises(SemanticInputIntegrityError, match="UTF-8 byte bound"):
            _read(store, max_failure_detail_bytes=limit - 1)


@pytest.mark.parametrize("field,value", [
    ("job", None), ("job", Job("valid", "other")), ("job_id", str(JOB_ID)),
    ("command_slot_id", 1), ("receipt_id", True), ("request_hash", "sha256:" + "A" * 64),
    ("command_name", ""), ("execution_kind", "other"), ("outcome", "succeeded"),
    ("outcome", "running"), ("failure_code", 1), ("failure_detail_json", None),
])
def test_direct_value_is_closed_and_typed_but_not_store_authority(field, value):
    valid = PersistedTerminalCommandReceipt(
        JOB, JOB_ID, SLOT_ID, RECEIPT_ID, REQUEST_HASH, NAME, "deterministic", "denied", "BUSY", DETAIL,
    )
    with pytest.raises(StoreValidationError):
        replace(valid, **{field: value})
