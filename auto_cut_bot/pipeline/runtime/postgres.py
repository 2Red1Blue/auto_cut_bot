"""PostgreSQL authority for HTTP pipeline runs and their durable outbox."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol, TypeVar
from uuid import UUID, uuid4

from psycopg import DatabaseError, InterfaceError

from .errors import (
    IdempotencyConflictError,
    PipelineRunError,
    PipelineRunNotFoundError,
    PipelineRunValidationError,
    ResumeNotAllowedError,
    StaleRunVersionError,
)
from .models import (
    PipelineCommand,
    PipelineRunRequest,
    PipelineRunSnapshot,
    PipelineStageResult,
    RunClaim,
)


class DbCursor(Protocol):
    def execute(self, query: str, params: tuple[object, ...] = ()) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...

    def close(self) -> None: ...


class DbConnection(Protocol):
    def cursor(self) -> DbCursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[], DbConnection]
_Result = TypeVar("_Result")


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _ensure_pending_outbox(cursor: DbCursor, run_id: str) -> None:
    cursor.execute(
        """
        INSERT INTO runtime.pipeline_run_outbox
            (outbox_id, run_id, state, version)
        VALUES (%s, %s, 'pending', 0)
        ON CONFLICT (run_id) DO UPDATE
           SET state = 'pending', version = runtime.pipeline_run_outbox.version + 1,
               lease_id = NULL, lease_expires_at = NULL,
               updated_at = transaction_timestamp()
         WHERE runtime.pipeline_run_outbox.state = 'consumed'
            OR (runtime.pipeline_run_outbox.state = 'leased'
                AND runtime.pipeline_run_outbox.lease_expires_at <= transaction_timestamp())
        """,
        (uuid4(), run_id),
    )


class _PostgresTransactions:
    def __init__(self, connection_factory: ConnectionFactory) -> None:
        if not callable(connection_factory):
            raise PipelineRunValidationError("connection_factory must be callable")
        self._connection_factory = connection_factory

    @property
    def connection_factory(self) -> ConnectionFactory:
        return self._connection_factory

    def _transaction(self, operation: Callable[[DbCursor], _Result]) -> _Result:
        connection: DbConnection | None = None
        cursor: DbCursor | None = None
        try:
            connection = self._connection_factory()
            cursor = connection.cursor()
            result = operation(cursor)
            connection.commit()
            return result
        except Exception as error:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            if isinstance(error, PipelineRunError):
                raise
            if isinstance(error, (DatabaseError, InterfaceError)):
                raise PipelineRunValidationError("pipeline PostgreSQL operation failed") from error
            raise
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()


class PostgresPipelineRunStore(_PostgresTransactions):
    """Transactional RunStore with command leases and Receipt projection."""

    def __init__(self, connection_factory: ConnectionFactory, *, lease_seconds: int = 300) -> None:
        super().__init__(connection_factory)
        if type(lease_seconds) is not int or lease_seconds < 1:  # noqa: E721
            raise PipelineRunValidationError("lease_seconds must be a positive integer")
        self._lease_seconds = lease_seconds

    async def claim_run(
        self,
        *,
        run_id: str,
        idempotency_key: str,
        request: PipelineRunRequest,
        request_hash: str,
    ) -> RunClaim:
        return await asyncio.to_thread(
            self._claim_run_sync,
            run_id,
            idempotency_key,
            request,
            request_hash,
        )

    def _claim_run_sync(
        self,
        run_id: str,
        idempotency_key: str,
        request: PipelineRunRequest,
        request_hash: str,
    ) -> RunClaim:
        source_kind = "root" if request.source_root is not None else "reference"
        source_value = request.source_root or request.source_reference
        if source_value is None:
            raise PipelineRunValidationError("pipeline request source is missing")

        def operation(cursor: DbCursor) -> RunClaim:
            cursor.execute(
                """
                INSERT INTO runtime.pipeline_runs
                    (run_id, idempotency_key, request_hash, profile, source_kind,
                     source_value, state, version)
                VALUES (%s, %s, %s, %s, %s, %s, 'accepted', 0)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING run_id
                """,
                (
                    run_id,
                    idempotency_key,
                    request_hash,
                    request.profile,
                    source_kind,
                    source_value,
                ),
            )
            inserted = cursor.fetchone()
            replayed = inserted is None
            effective_run_id = run_id
            if replayed:
                cursor.execute(
                    """
                    SELECT run_id, request_hash, profile, source_kind, source_value
                      FROM runtime.pipeline_runs
                     WHERE idempotency_key = %s FOR UPDATE
                    """,
                    (idempotency_key,),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise PipelineRunValidationError(
                        "idempotent run vanished after uniqueness conflict"
                    )
                effective_run_id = _text(existing[0])
                if (
                    _text(existing[1]) != request_hash
                    or _text(existing[2]) != request.profile
                    or _text(existing[3]) != source_kind
                    or _text(existing[4]) != source_value
                ):
                    raise IdempotencyConflictError(
                        "idempotency key already binds another pipeline request"
                    )
            else:
                cursor.execute(
                    """
                    INSERT INTO runtime.pipeline_commands
                        (command_id, run_id, ordinal, stage, state, version)
                    VALUES (%s, %s, 0, 'source_prep', 'pending', 0)
                    """,
                    (uuid4(), run_id),
                )
                self._insert_outbox(cursor, run_id)
            return RunClaim(self._read_snapshot(cursor, effective_run_id), replayed)

        return self._transaction(operation)

    async def read_run(self, run_id: str) -> PipelineRunSnapshot | None:
        return await asyncio.to_thread(self._read_run_sync, run_id)

    def _read_run_sync(self, run_id: str) -> PipelineRunSnapshot | None:
        def operation(cursor: DbCursor) -> PipelineRunSnapshot | None:
            cursor.execute(
                "SELECT 1 FROM runtime.pipeline_runs WHERE run_id = %s",
                (run_id,),
            )
            return None if cursor.fetchone() is None else self._read_snapshot(cursor, run_id)

        return self._transaction(operation)

    async def claim_resume(
        self,
        run_id: str,
        *,
        expected_version: int,
    ) -> PipelineRunSnapshot:
        return await asyncio.to_thread(self._claim_resume_sync, run_id, expected_version)

    def _claim_resume_sync(self, run_id: str, expected_version: int) -> PipelineRunSnapshot:
        def operation(cursor: DbCursor) -> PipelineRunSnapshot:
            cursor.execute(
                "SELECT state, version FROM runtime.pipeline_runs WHERE run_id = %s FOR UPDATE",
                (run_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise PipelineRunNotFoundError(run_id)
            state, version = _text(row[0]), int(_text(row[1]))
            if version != expected_version:
                raise StaleRunVersionError(run_id)
            if state not in ("accepted", "running"):
                raise ResumeNotAllowedError(run_id)
            cursor.execute(
                """
                SELECT 1 FROM runtime.pipeline_commands
                 WHERE run_id = %s AND state IN ('pending', 'indeterminate')
                 LIMIT 1 FOR UPDATE
                """,
                (run_id,),
            )
            if cursor.fetchone() is None:
                raise ResumeNotAllowedError(run_id)
            cursor.execute(
                """
                UPDATE runtime.pipeline_runs
                   SET version = version + 1, updated_at = transaction_timestamp()
                 WHERE run_id = %s AND version = %s
                """,
                (run_id, expected_version),
            )
            _ensure_pending_outbox(cursor, run_id)
            return self._read_snapshot(cursor, run_id)

        return self._transaction(operation)

    async def list_reconstructible_runs(self) -> tuple[PipelineRunSnapshot, ...]:
        return await asyncio.to_thread(self._list_reconstructible_runs_sync)

    def _list_reconstructible_runs_sync(self) -> tuple[PipelineRunSnapshot, ...]:
        def operation(cursor: DbCursor) -> tuple[PipelineRunSnapshot, ...]:
            cursor.execute(
                """
                SELECT DISTINCT run.run_id
                  FROM runtime.pipeline_runs AS run
                  JOIN runtime.pipeline_commands AS command ON command.run_id = run.run_id
                 WHERE run.state IN ('accepted', 'running')
                   AND command.state IN ('pending', 'running', 'indeterminate')
                 ORDER BY run.run_id
                """
            )
            run_ids: list[str] = []
            while (row := cursor.fetchone()) is not None:
                run_ids.append(_text(row[0]))
            return tuple(self._read_snapshot(cursor, run_id) for run_id in run_ids)

        return self._transaction(operation)

    async def expire_running_lease(
        self,
        run_id: str,
        *,
        expected_version: int,
        lease_id: str,
    ) -> PipelineCommand:
        return await asyncio.to_thread(
            self._expire_running_lease_sync,
            run_id,
            expected_version,
            lease_id,
        )

    def _expire_running_lease_sync(
        self,
        run_id: str,
        expected_version: int,
        lease_id: str,
    ) -> PipelineCommand:
        def operation(cursor: DbCursor) -> PipelineCommand:
            cursor.execute(
                """
                SELECT command.command_id, command.stage, command.state,
                       command.version, command.lease_id,
                       command.lease_expires_at <= transaction_timestamp(), run.state
                  FROM runtime.pipeline_commands AS command
                  JOIN runtime.pipeline_runs AS run ON run.run_id = command.run_id
                 WHERE command.run_id = %s
                 ORDER BY command.ordinal LIMIT 1
                 FOR UPDATE OF command, run
                """,
                (run_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise PipelineRunNotFoundError(run_id)
            command_id, stage, state, version, current_lease, expired, run_state = row
            if (
                _text(state) != "running"
                or int(_text(version)) != expected_version
                or _text(current_lease) != lease_id
            ):
                raise StaleRunVersionError(run_id)
            if expired is not True:
                raise ResumeNotAllowedError(run_id)
            if _text(run_state) not in ("accepted", "running"):
                raise ResumeNotAllowedError(run_id)
            cursor.execute(
                """
                UPDATE runtime.pipeline_commands
                   SET state = 'indeterminate', version = version + 1,
                       lease_id = NULL, lease_expires_at = NULL,
                       updated_at = transaction_timestamp()
                 WHERE command_id = %s AND state = 'running'
                   AND version = %s AND lease_id = %s
                """,
                (command_id, expected_version, lease_id),
            )
            cursor.execute(
                """
                UPDATE runtime.pipeline_runs
                   SET state = 'running', version = version + 1,
                       updated_at = transaction_timestamp()
                 WHERE run_id = %s
                """,
                (run_id,),
            )
            return PipelineCommand(
                _text(command_id),
                _text(stage),
                "indeterminate",
                None,
                expected_version + 1,
            )

        return self._transaction(operation)

    async def claim_next_pending(
        self,
        run_id: str,
        *,
        expected_version: int,
        lease_id: str,
    ) -> PipelineCommand | None:
        return await asyncio.to_thread(
            self._claim_next_pending_sync,
            run_id,
            expected_version,
            lease_id,
        )

    def _claim_next_pending_sync(
        self,
        run_id: str,
        expected_version: int,
        lease_id: str,
    ) -> PipelineCommand | None:
        def operation(cursor: DbCursor) -> PipelineCommand | None:
            cursor.execute(
                "SELECT state, version FROM runtime.pipeline_runs WHERE run_id = %s FOR UPDATE",
                (run_id,),
            )
            run_row = cursor.fetchone()
            if run_row is None:
                raise PipelineRunNotFoundError(run_id)
            if _text(run_row[0]) not in ("accepted", "running"):
                return None
            cursor.execute(
                """
                SELECT command_id, stage, version
                  FROM runtime.pipeline_commands
                 WHERE run_id = %s AND state = 'pending' AND version = %s
                 ORDER BY ordinal
                 LIMIT 1 FOR UPDATE SKIP LOCKED
                """,
                (run_id, expected_version),
            )
            command = cursor.fetchone()
            if command is None:
                return None
            command_id, stage, version = command
            cursor.execute(
                """
                UPDATE runtime.pipeline_commands
                   SET state = 'running', version = version + 1, lease_id = %s,
                       lease_expires_at = transaction_timestamp()
                           + (%s * interval '1 second'),
                       updated_at = transaction_timestamp()
                 WHERE command_id = %s AND state = 'pending' AND version = %s
                """,
                (lease_id, self._lease_seconds, command_id, expected_version),
            )
            if _text(run_row[0]) == "accepted":
                cursor.execute(
                    """
                    UPDATE runtime.pipeline_runs
                       SET state = 'running', version = version + 1,
                           updated_at = transaction_timestamp()
                     WHERE run_id = %s
                    """,
                    (run_id,),
                )
            return PipelineCommand(
                _text(command_id),
                _text(stage),
                "running",
                None,
                int(_text(version)) + 1,
                lease_id,
            )

        return self._transaction(operation)

    async def record_result(
        self,
        run_id: str,
        *,
        result: PipelineStageResult,
        expected_version: int,
        lease_id: str,
    ) -> None:
        await asyncio.to_thread(
            self._record_result_sync,
            run_id,
            result,
            expected_version,
            lease_id,
        )

    def _record_result_sync(
        self,
        run_id: str,
        result: PipelineStageResult,
        expected_version: int,
        lease_id: str,
    ) -> None:
        def operation(cursor: DbCursor) -> None:
            cursor.execute(
                """
                SELECT command.state, command.version, command.lease_id, run.state,
                       command.lease_expires_at > transaction_timestamp()
                  FROM runtime.pipeline_commands AS command
                  JOIN runtime.pipeline_runs AS run ON run.run_id = command.run_id
                 WHERE command.run_id = %s AND command.command_id = %s
                 FOR UPDATE OF command, run
                """,
                (run_id, result.command_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise PipelineRunNotFoundError(run_id)
            command_state, command_version, command_lease, run_state = map(_text, row[:4])
            if command_state != "running" or int(command_version) != expected_version:
                raise StaleRunVersionError(run_id)
            if row[4] is not True:
                raise StaleRunVersionError(run_id)
            if command_lease != lease_id:
                raise PipelineRunValidationError("pipeline result does not bind the command lease")
            if run_state not in ("accepted", "running"):
                raise ResumeNotAllowedError(run_id)

            if result.receipt_id is not None:
                cursor.execute(
                    """
                    INSERT INTO runtime.pipeline_run_receipts
                        (receipt_id, command_id, outcome)
                    VALUES (%s, %s, %s)
                    """,
                    (result.receipt_id, result.command_id, result.outcome),
                )
            cursor.execute(
                """
                UPDATE runtime.pipeline_commands
                   SET state = %s, version = version + 1,
                       lease_id = NULL, lease_expires_at = NULL,
                       completed_at = CASE
                           WHEN %s = 'indeterminate' THEN NULL
                           ELSE transaction_timestamp()
                       END,
                       updated_at = transaction_timestamp()
                 WHERE command_id = %s AND state = 'running'
                   AND version = %s AND lease_id = %s
                   AND lease_expires_at > transaction_timestamp()
                """,
                (
                    result.outcome,
                    result.outcome,
                    result.command_id,
                    expected_version,
                    lease_id,
                ),
            )
            cursor.execute(
                "SELECT state FROM runtime.pipeline_commands WHERE run_id = %s",
                (run_id,),
            )
            command_states: list[str] = []
            while (command_row := cursor.fetchone()) is not None:
                command_states.append(_text(command_row[0]))
            terminal = {"succeeded", "denied", "failed"}
            if command_states and all(state in terminal for state in command_states):
                next_run_state = (
                    "failed"
                    if "failed" in command_states
                    else "denied"
                    if "denied" in command_states
                    else "succeeded"
                )
            else:
                next_run_state = "running"
            cursor.execute(
                """
                UPDATE runtime.pipeline_runs
                   SET state = %s, version = version + 1,
                       updated_at = transaction_timestamp()
                 WHERE run_id = %s
                """,
                (next_run_state, run_id),
            )

        self._transaction(operation)

    async def read_indeterminate(
        self,
        run_id: str,
        *,
        expected_version: int,
    ) -> PipelineCommand | None:
        return await asyncio.to_thread(
            self._read_indeterminate_sync,
            run_id,
            expected_version,
        )

    def _read_indeterminate_sync(
        self,
        run_id: str,
        expected_version: int,
    ) -> PipelineCommand | None:
        def operation(cursor: DbCursor) -> PipelineCommand | None:
            cursor.execute(
                """
                SELECT command_id, stage, version
                  FROM runtime.pipeline_commands
                 WHERE run_id = %s AND state = 'indeterminate' AND version = %s
                 ORDER BY ordinal LIMIT 1
                """,
                (run_id, expected_version),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return PipelineCommand(
                _text(row[0]),
                _text(row[1]),
                "indeterminate",
                None,
                int(_text(row[2])),
            )

        return self._transaction(operation)

    async def record_reconciled_result(
        self,
        run_id: str,
        *,
        result: PipelineStageResult,
        expected_version: int,
    ) -> None:
        await asyncio.to_thread(
            self._record_reconciled_result_sync,
            run_id,
            result,
            expected_version,
        )

    def _record_reconciled_result_sync(
        self,
        run_id: str,
        result: PipelineStageResult,
        expected_version: int,
    ) -> None:
        if result.outcome == "indeterminate" or result.receipt_id is None:
            raise PipelineRunValidationError(
                "reconciled result must contain a terminal Receipt"
            )

        def operation(cursor: DbCursor) -> None:
            cursor.execute(
                """
                SELECT command.state, command.version, run.state
                  FROM runtime.pipeline_commands AS command
                  JOIN runtime.pipeline_runs AS run ON run.run_id = command.run_id
                 WHERE command.run_id = %s AND command.command_id = %s
                 FOR UPDATE OF command, run
                """,
                (run_id, result.command_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise PipelineRunNotFoundError(run_id)
            if _text(row[0]) != "indeterminate" or int(_text(row[1])) != expected_version:
                raise StaleRunVersionError(run_id)
            if _text(row[2]) not in ("accepted", "running"):
                raise ResumeNotAllowedError(run_id)
            cursor.execute(
                """
                INSERT INTO runtime.pipeline_run_receipts
                    (receipt_id, command_id, outcome)
                VALUES (%s, %s, %s)
                """,
                (result.receipt_id, result.command_id, result.outcome),
            )
            cursor.execute(
                """
                UPDATE runtime.pipeline_commands
                   SET state = %s, version = version + 1,
                       completed_at = transaction_timestamp(),
                       updated_at = transaction_timestamp()
                 WHERE command_id = %s AND state = 'indeterminate' AND version = %s
                """,
                (result.outcome, result.command_id, expected_version),
            )
            cursor.execute(
                "SELECT state FROM runtime.pipeline_commands WHERE run_id = %s",
                (run_id,),
            )
            command_states: list[str] = []
            while (command_row := cursor.fetchone()) is not None:
                command_states.append(_text(command_row[0]))
            terminal = {"succeeded", "denied", "failed"}
            if command_states and all(state in terminal for state in command_states):
                next_run_state = (
                    "failed"
                    if "failed" in command_states
                    else "denied"
                    if "denied" in command_states
                    else "succeeded"
                )
            else:
                next_run_state = "running"
            cursor.execute(
                """
                UPDATE runtime.pipeline_runs
                   SET state = %s, version = version + 1,
                       updated_at = transaction_timestamp()
                 WHERE run_id = %s
                """,
                (next_run_state, run_id),
            )

        self._transaction(operation)

    @staticmethod
    def _insert_outbox(cursor: DbCursor, run_id: str) -> None:
        cursor.execute(
            """
            INSERT INTO runtime.pipeline_run_outbox
                (outbox_id, run_id, state, version)
            VALUES (%s, %s, 'pending', 0)
            """,
            (uuid4(), run_id),
        )

    @staticmethod
    def _read_snapshot(cursor: DbCursor, run_id: str) -> PipelineRunSnapshot:
        cursor.execute(
            """
            SELECT request_hash, profile, source_kind, source_value, state, version
              FROM runtime.pipeline_runs WHERE run_id = %s
            """,
            (run_id,),
        )
        run = cursor.fetchone()
        if run is None:
            raise PipelineRunNotFoundError(run_id)
        request_hash, profile, source_kind, source_value, state, version = run
        request_mapping: dict[str, object] = {"profile": _text(profile)}
        request_mapping[
            "source_root" if _text(source_kind) == "root" else "source_reference"
        ] = _text(source_value)
        request = PipelineRunRequest.from_mapping(request_mapping)
        cursor.execute(
            """
            SELECT command.command_id, command.stage, command.state, receipt.receipt_id,
                   command.version, command.lease_id
              FROM runtime.pipeline_commands AS command
              LEFT JOIN runtime.pipeline_run_receipts AS receipt
                ON receipt.command_id = command.command_id
             WHERE command.run_id = %s ORDER BY command.ordinal
            """,
            (run_id,),
        )
        commands: list[PipelineCommand] = []
        while (row := cursor.fetchone()) is not None:
            commands.append(
                PipelineCommand(
                    _text(row[0]),
                    _text(row[1]),
                    _text(row[2]),  # type: ignore[arg-type]
                    None if row[3] is None else UUID(str(row[3])),
                    int(_text(row[4])),
                    None if row[5] is None else _text(row[5]),
                )
            )
        return PipelineRunSnapshot(
            run_id,
            request,
            _text(request_hash),
            _text(state),  # type: ignore[arg-type]
            tuple(commands),
            int(_text(version)),
        )


class PostgresPipelineScheduler(_PostgresTransactions):
    """Idempotent durable scheduler backed by the transactional run outbox."""

    async def enqueue(self, run_id: str) -> None:
        await asyncio.to_thread(self._enqueue_sync, run_id)

    def _enqueue_sync(self, run_id: str) -> None:
        self._transaction(lambda cursor: _ensure_pending_outbox(cursor, run_id))

    async def pending_run_ids(self) -> tuple[str, ...]:
        return await asyncio.to_thread(self._pending_run_ids_sync)

    def _pending_run_ids_sync(self) -> tuple[str, ...]:
        def operation(cursor: DbCursor) -> tuple[str, ...]:
            cursor.execute(
                """
                SELECT run_id FROM runtime.pipeline_run_outbox
                 WHERE state = 'pending'
                    OR (state = 'leased' AND lease_expires_at <= transaction_timestamp())
                 ORDER BY run_id
                """
            )
            run_ids: list[str] = []
            while (row := cursor.fetchone()) is not None:
                run_ids.append(_text(row[0]))
            return tuple(run_ids)

        return self._transaction(operation)
