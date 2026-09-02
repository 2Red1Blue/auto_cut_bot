"""PostgreSQL authority for HTTP pipeline runs and their durable outbox."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Protocol, TypeVar, cast
from uuid import UUID, uuid4

from psycopg import DatabaseError, InterfaceError

from .errors import (
    IdempotencyConflictError,
    PipelineRunError,
    PipelineRunNotFoundError,
    PipelineRunValidationError,
    PipelineStageIsolationError,
    ResumeNotAllowedError,
    StaleRunVersionError,
)
from .models import (
    MediaPreflightRecomputeRequest,
    OutboxLease,
    PipelineCommand,
    PipelineExecutionProfile,
    PipelineRecomputeRequest,
    PipelineRunRequest,
    PipelineRunSnapshot,
    PipelineStageResult,
    RunClaim,
    VlmFullStageRecomputeRequest,
    parse_recompute_request,
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

# Stage 1 is wired; Stage 2/3 and the final Render/QC path are still incomplete.
# The bootstrap stages must never manufacture a fully successful Pipeline run.
_PIPELINE_SUCCESS_TERMINAL_STAGE: str | None = None


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _execution_profile(value: object, expected_hash: object) -> PipelineExecutionProfile:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[str, object], value)
    else:
        try:
            decoded = cast(object, json.loads(_text(value)))
        except (json.JSONDecodeError, UnicodeError) as error:
            raise PipelineRunValidationError("persisted execution profile is not JSON") from error
        if not isinstance(decoded, Mapping):
            raise PipelineRunValidationError("persisted execution profile must be an object")
        mapping = cast(Mapping[str, object], decoded)
    profile = PipelineExecutionProfile.from_mapping(mapping)
    if profile.canonical_hash != _text(expected_hash):
        raise PipelineRunValidationError(
            "persisted execution profile hash does not bind its canonical JSON"
        )
    return profile


def _terminal_run_state(command_rows: list[tuple[str, str]]) -> str:
    states = [state for _stage, state in command_rows]
    if "recompute_needed" in states:
        if any(
            state in ("pending", "running", "indeterminate", "awaiting_calibration")
            for state in states
        ):
            return "running"
        return "recompute_needed"
    if "awaiting_calibration" in states:
        if any(state in ("pending", "running", "indeterminate") for state in states):
            return "running"
        return "awaiting_calibration"
    terminal_states = {"succeeded", "denied", "failed", "blocked"}
    if not states or any(state not in terminal_states for state in states):
        return "running"
    if "failed" in states:
        return "failed"
    if "denied" in states:
        return "denied"
    if "blocked" in states:
        return "failed"
    if tuple(stage for stage, _state in command_rows) in {
        ("media_preflight",),
        ("source_prep", "vlm"),
        ("source_prep", "context_prepare", "vlm"),
        (
            "source_prep",
            "context_prepare",
            "vlm",
            "stage1_narrative",
            "stage2_portfolio",
            "stage3_blueprint",
        ),
    }:
        return "succeeded"
    if _PIPELINE_SUCCESS_TERMINAL_STAGE is not None and any(
        stage == _PIPELINE_SUCCESS_TERMINAL_STAGE and state == "succeeded"
        for stage, state in command_rows
    ):
        return "succeeded"
    return "failed"


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
        execution_profile: PipelineExecutionProfile,
        recompute_request: PipelineRecomputeRequest | None = None,
    ) -> RunClaim:
        return await asyncio.to_thread(
            self._claim_run_sync,
            run_id,
            idempotency_key,
            request,
            request_hash,
            execution_profile,
            recompute_request,
        )

    def _claim_run_sync(
        self,
        run_id: str,
        idempotency_key: str,
        request: PipelineRunRequest,
        request_hash: str,
        execution_profile: PipelineExecutionProfile,
        recompute_request: PipelineRecomputeRequest | None = None,
    ) -> RunClaim:
        if type(execution_profile) is not PipelineExecutionProfile:  # noqa: E721
            raise PipelineRunValidationError("claim_run requires a PipelineExecutionProfile")
        if request_hash != request.request_hash:
            raise PipelineRunValidationError(
                "claim_run request_hash does not bind the canonical request"
            )
        if recompute_request is not None and type(recompute_request) not in (  # noqa: E721
            VlmFullStageRecomputeRequest,
            MediaPreflightRecomputeRequest,
        ):
            raise PipelineRunValidationError("claim_run recompute_request must be canonical")
        if (
            type(recompute_request) is MediaPreflightRecomputeRequest  # noqa: E721
            and not execution_profile.has_media_preflight_policy
        ):
            raise PipelineRunValidationError(
                "media-preflight recompute requires an execution profile with a media-preflight policy"
            )
        if execution_profile.has_media_preflight_policy:
            execution_profile.build_stage1_command_policy()
            execution_profile.build_stage2_command_policy()
            execution_profile.build_stage3_command_policy()
            execution_profile.to_evidence_read_limits()
        elif execution_profile.is_semantic_only:
            execution_profile.to_doubao_policy()
            execution_profile.to_generation_retry_policy()
        elif execution_profile.is_semantic_story:
            execution_profile.to_doubao_policy()
            execution_profile.to_generation_retry_policy()
            execution_profile.build_stage1_command_policy()
            execution_profile.build_stage2_command_policy()
            execution_profile.build_stage3_command_policy()
        else:
            raise PipelineRunValidationError("execution profile has no executable run plan")
        source_kind = "root" if request.source_root is not None else "reference"
        source_value = request.source_root or request.source_reference
        if source_value is None:
            raise PipelineRunValidationError("pipeline request source is missing")
        recompute_json = (
            None
            if recompute_request is None
            else json.dumps(
                recompute_request.to_mapping(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        recompute_hash = None if recompute_request is None else recompute_request.request_hash

        def operation(cursor: DbCursor) -> RunClaim:
            cursor.execute(
                """
                INSERT INTO runtime.pipeline_runs
                    (run_id, idempotency_key, request_hash, profile, source_kind,
                     source_value, execution_profile, execution_profile_hash,
                     recompute_request, recompute_request_hash, state, version)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s,
                        %s::jsonb, %s, 'accepted', 0)
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
                    execution_profile.canonical_json,
                    execution_profile.canonical_hash,
                    recompute_json,
                    recompute_hash,
                ),
            )
            inserted = cursor.fetchone()
            replayed = inserted is None
            effective_run_id = run_id
            if replayed:
                cursor.execute(
                    """
                    SELECT run_id, request_hash, profile, source_kind, source_value,
                           recompute_request_hash
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
                    or (None if existing[5] is None else _text(existing[5])) != recompute_hash
                ):
                    raise IdempotencyConflictError(
                        "idempotency key already binds another pipeline request"
                    )
            else:
                if type(recompute_request) is MediaPreflightRecomputeRequest:  # noqa: E721
                    cursor.execute(
                        """
                        INSERT INTO runtime.pipeline_commands
                            (command_id, run_id, ordinal, stage, state, version)
                        VALUES (%s, %s, 0, 'media_preflight', 'pending', 0)
                        """,
                        (uuid4(), run_id),
                    )
                elif execution_profile.is_semantic_only:
                    cursor.execute(
                        """
                        INSERT INTO runtime.pipeline_commands
                            (command_id, run_id, ordinal, stage, state, version)
                        VALUES
                            (%s, %s, 0, 'source_prep', 'pending', 0),
                            (%s, %s, 1, 'context_prepare', 'pending', 0),
                            (%s, %s, 2, 'vlm', 'pending', 0)
                        """,
                        (uuid4(), run_id, uuid4(), run_id, uuid4(), run_id),
                    )
                elif execution_profile.is_semantic_story:
                    cursor.execute(
                        """
                        INSERT INTO runtime.pipeline_commands
                            (command_id, run_id, ordinal, stage, state, version)
                        VALUES
                            (%s, %s, 0, 'source_prep', 'pending', 0),
                            (%s, %s, 1, 'context_prepare', 'pending', 0),
                            (%s, %s, 2, 'vlm', 'pending', 0),
                            (%s, %s, 3, 'stage1_narrative', 'pending', 0),
                            (%s, %s, 4, 'stage2_portfolio', 'pending', 0),
                            (%s, %s, 5, 'stage3_blueprint', 'pending', 0)
                        """,
                        (
                            uuid4(),
                            run_id,
                            uuid4(),
                            run_id,
                            uuid4(),
                            run_id,
                            uuid4(),
                            run_id,
                            uuid4(),
                            run_id,
                            uuid4(),
                            run_id,
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO runtime.pipeline_commands
                            (command_id, run_id, ordinal, stage, state, version)
                        VALUES
                            (%s, %s, 0, 'source_prep', 'pending', 0),
                            (%s, %s, 1, 'vlm', 'pending', 0),
                            (%s, %s, 2, 'stage1_narrative', 'pending', 0),
                            (%s, %s, 3, 'stage2_portfolio', 'pending', 0),
                            (%s, %s, 4, 'stage3_blueprint', 'pending', 0),
                            (%s, %s, 5, 'media_preflight', 'pending', 0)
                        """,
                        (
                            uuid4(),
                            run_id,
                            uuid4(),
                            run_id,
                            uuid4(),
                            run_id,
                            uuid4(),
                            run_id,
                            uuid4(),
                            run_id,
                            uuid4(),
                            run_id,
                        ),
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
            if state not in ("accepted", "running", "awaiting_calibration"):
                raise ResumeNotAllowedError(run_id)
            command_states = (
                ("pending", "indeterminate")
                if state in ("accepted", "running")
                else ("awaiting_calibration",)
            )
            cursor.execute(
                """
                SELECT 1 FROM runtime.pipeline_commands
                 WHERE run_id = %s AND state = ANY(%s)
                   AND (%s OR stage = 'media_preflight')
                 LIMIT 1 FOR UPDATE
                """,
                (run_id, list(command_states), state in ("accepted", "running")),
            )
            if cursor.fetchone() is None:
                raise ResumeNotAllowedError(run_id)
            if state == "awaiting_calibration":
                cursor.execute(
                    """
                    UPDATE runtime.pipeline_commands
                       SET state = 'pending', version = version + 1,
                           updated_at = transaction_timestamp()
                     WHERE run_id = %s AND state = 'awaiting_calibration'
                       AND stage = 'media_preflight'
                 RETURNING command_id
                    """,
                    (run_id,),
                )
                if cursor.fetchone() is None or cursor.fetchone() is not None:
                    raise ResumeNotAllowedError(run_id)
            cursor.execute(
                """
                UPDATE runtime.pipeline_runs
                   SET state = CASE WHEN state = 'awaiting_calibration' THEN 'accepted' ELSE state END,
                       version = version + 1, updated_at = transaction_timestamp()
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
                 WHERE command.run_id = %s AND command.state = 'running'
                   AND command.version = %s AND command.lease_id = %s
                 ORDER BY command.ordinal LIMIT 1
                 FOR UPDATE OF command, run
                """,
                (run_id, expected_version, lease_id),
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

    async def renew_running_lease(
        self,
        run_id: str,
        *,
        command_id: str,
        expected_version: int,
        lease_id: str,
    ) -> PipelineCommand:
        return await asyncio.to_thread(
            self._renew_running_lease_sync,
            run_id,
            command_id,
            expected_version,
            lease_id,
        )

    def _renew_running_lease_sync(
        self,
        run_id: str,
        command_id: str,
        expected_version: int,
        lease_id: str,
    ) -> PipelineCommand:
        def operation(cursor: DbCursor) -> PipelineCommand:
            cursor.execute(
                """
                UPDATE runtime.pipeline_commands
                   SET version = version + 1,
                       lease_expires_at = transaction_timestamp()
                           + (%s * interval '1 second'),
                       updated_at = transaction_timestamp()
                 WHERE run_id = %s AND command_id = %s AND state = 'running'
                   AND version = %s AND lease_id = %s
                   AND lease_expires_at > transaction_timestamp()
                RETURNING command_id, stage, version, lease_id
                """,
                (self._lease_seconds, run_id, command_id, expected_version, lease_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise StaleRunVersionError(run_id)
            return PipelineCommand(
                _text(row[0]),
                _text(row[1]),
                "running",
                None,
                int(_text(row[2])),
                _text(row[3]),
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
                  FROM runtime.pipeline_commands AS candidate
                 WHERE candidate.run_id = %s AND candidate.state = 'pending'
                   AND candidate.version = %s
                   AND (
                       candidate.stage NOT IN ('vlm', 'stage1_narrative', 'stage2_portfolio', 'stage3_blueprint')
                       OR (
                           candidate.stage = 'vlm'
                           AND EXISTS (
                               SELECT 1 FROM runtime.pipeline_runs AS profile_run
                                WHERE profile_run.run_id = candidate.run_id
                                  AND profile_run.execution_profile ->> 'kind' = 'doubao_vlm'
                                  AND profile_run.execution_profile ->> 'schema_version'
                                      IN ('pipeline-execution-profile-v9', 'pipeline-execution-profile-v10',
                                          'pipeline-execution-profile-v11')
                           )
                       )
                       OR EXISTS (
                           SELECT 1 FROM runtime.pipeline_runs AS profile_run
                            WHERE profile_run.run_id = candidate.run_id
                              AND candidate.stage IN ('stage1_narrative', 'stage2_portfolio', 'stage3_blueprint')
                              AND profile_run.execution_profile ->> 'kind' = 'doubao_vlm'
                              AND profile_run.execution_profile
                                  ->> 'schema_version' IN (
                                      'pipeline-execution-profile-v9',
                                      'pipeline-execution-profile-v11'
                                  )
                              AND profile_run.execution_profile ? 'stage1_command_policy'
                              AND profile_run.execution_profile ? 'stage2_command_policy'
                              AND profile_run.execution_profile ? 'stage3_command_policy'
                              AND (
                                  profile_run.execution_profile ->> 'schema_version'
                                      = 'pipeline-execution-profile-v11'
                                  OR profile_run.execution_profile ? 'evidence_read_limits'
                              )
                       )
                   )
                   AND (
                       candidate.stage <> 'media_preflight'
                       OR EXISTS (
                           SELECT 1 FROM runtime.pipeline_runs AS profile_run
                            WHERE profile_run.run_id = candidate.run_id
                              AND profile_run.execution_profile
                                  ->> 'schema_version' = 'pipeline-execution-profile-v9'
                              AND profile_run.execution_profile
                                  ? 'media_preflight_policy'
                              AND profile_run.execution_profile
                                  ? 'media_preflight_policy_hash'
                              AND profile_run.execution_profile
                                  ? 'materialization_limits'
                              AND profile_run.execution_profile
                                  ? 'stage1_command_policy'
                              AND profile_run.execution_profile
                                  ? 'stage2_command_policy'
                              AND profile_run.execution_profile
                                  ? 'stage3_command_policy'
                              AND profile_run.execution_profile
                                  ? 'evidence_read_limits'
                       )
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM runtime.pipeline_commands AS predecessor
                        WHERE predecessor.run_id = candidate.run_id
                          AND predecessor.ordinal < candidate.ordinal
                          AND predecessor.state <> 'succeeded'
                   )
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
            if result.outcome in ("denied", "failed"):
                cursor.execute(
                    """
                    UPDATE runtime.pipeline_commands
                       SET state = 'blocked', version = version + 1,
                           blocking_command_id = %s,
                           completed_at = transaction_timestamp(),
                           updated_at = transaction_timestamp()
                     WHERE run_id = %s AND state = 'pending'
                       AND ordinal > (
                           SELECT ordinal FROM runtime.pipeline_commands
                            WHERE command_id = %s
                       )
                    """,
                    (result.command_id, run_id, result.command_id),
                )
            cursor.execute(
                """
                UPDATE runtime.pipeline_commands
                   SET state = %s, version = version + 1,
                       lease_id = NULL, lease_expires_at = NULL,
                       completed_at = CASE
                           WHEN %s IN ('indeterminate', 'awaiting_calibration', 'recompute_needed') THEN NULL
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
                """SELECT stage, state FROM runtime.pipeline_commands
                    WHERE run_id = %s ORDER BY ordinal""",
                (run_id,),
            )
            command_rows: list[tuple[str, str]] = []
            while (command_row := cursor.fetchone()) is not None:
                command_rows.append((_text(command_row[0]), _text(command_row[1])))
            next_run_state = _terminal_run_state(command_rows)
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
        if result.outcome == "indeterminate":
            raise PipelineRunValidationError("reconciled result cannot remain indeterminate")
        if result.outcome in ("succeeded", "denied", "failed") and result.receipt_id is None:
            raise PipelineRunValidationError("terminal reconciled result must contain a Receipt")
        if (
            result.outcome in ("awaiting_calibration", "recompute_needed")
            and result.receipt_id is not None
        ):
            raise PipelineRunValidationError(
                "calibration reconciliation result cannot contain a Receipt"
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
                       completed_at = CASE
                           WHEN %s IN ('awaiting_calibration', 'recompute_needed') THEN NULL
                           ELSE transaction_timestamp()
                       END,
                       updated_at = transaction_timestamp()
                 WHERE command_id = %s AND state = 'indeterminate' AND version = %s
                """,
                (result.outcome, result.outcome, result.command_id, expected_version),
            )
            if result.outcome in ("denied", "failed"):
                cursor.execute(
                    """
                    UPDATE runtime.pipeline_commands
                       SET state = 'blocked', version = version + 1,
                           blocking_command_id = %s,
                           completed_at = transaction_timestamp(),
                           updated_at = transaction_timestamp()
                     WHERE run_id = %s AND state = 'pending'
                       AND ordinal > (
                           SELECT ordinal FROM runtime.pipeline_commands
                            WHERE command_id = %s
                       )
                    """,
                    (result.command_id, run_id, result.command_id),
                )
            cursor.execute(
                """SELECT stage, state FROM runtime.pipeline_commands
                    WHERE run_id = %s ORDER BY ordinal""",
                (run_id,),
            )
            command_rows: list[tuple[str, str]] = []
            while (command_row := cursor.fetchone()) is not None:
                command_rows.append((_text(command_row[0]), _text(command_row[1])))
            next_run_state = _terminal_run_state(command_rows)
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

    async def record_isolated_failure(
        self,
        run_id: str,
        *,
        failure: PipelineStageIsolationError,
    ) -> PipelineStageResult:
        if type(failure) is not PipelineStageIsolationError:  # noqa: E721
            raise PipelineRunValidationError("isolated failure must use the closed type")
        return await asyncio.to_thread(self._record_isolated_failure_sync, run_id, failure)

    def _record_isolated_failure_sync(
        self,
        run_id: str,
        failure: PipelineStageIsolationError,
    ) -> PipelineStageResult:
        def operation(cursor: DbCursor) -> PipelineStageResult:
            cursor.execute(
                """
                SELECT command.state, command.version, command.stage, run.state
                  FROM runtime.pipeline_commands AS command
                  JOIN runtime.pipeline_runs AS run ON run.run_id = command.run_id
                 WHERE command.run_id = %s AND command.command_id = %s
                 FOR UPDATE OF command, run
                """,
                (run_id, failure.command_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise PipelineRunNotFoundError(run_id)
            if _text(row[0]) != "indeterminate" or int(_text(row[1])) != failure.command_version:
                raise StaleRunVersionError(run_id)
            if _text(row[2]) != failure.stage or failure.stage != "vlm":
                raise PipelineRunValidationError("isolated failure does not bind the VLM command")
            if _text(row[3]) not in ("accepted", "running"):
                raise ResumeNotAllowedError(run_id)

            receipt_id = uuid4()
            cursor.execute(
                """
                INSERT INTO runtime.pipeline_run_receipts
                    (receipt_id, command_id, outcome, failure_code, failure_detail)
                VALUES (%s, %s, 'failed', %s, %s::jsonb)
                """,
                (
                    receipt_id,
                    failure.command_id,
                    failure.failure_code,
                    failure.failure_detail_json,
                ),
            )
            cursor.execute(
                """
                UPDATE runtime.pipeline_commands
                   SET state = 'failed', version = version + 1,
                       completed_at = transaction_timestamp(),
                       updated_at = transaction_timestamp()
                 WHERE command_id = %s AND state = 'indeterminate' AND version = %s
                RETURNING command_id
                """,
                (failure.command_id, failure.command_version),
            )
            if cursor.fetchone() is None:
                raise StaleRunVersionError(run_id)
            cursor.execute(
                """
                UPDATE runtime.pipeline_commands
                   SET state = 'blocked', version = version + 1,
                       blocking_command_id = %s,
                       completed_at = transaction_timestamp(),
                       updated_at = transaction_timestamp()
                 WHERE run_id = %s AND state = 'pending'
                   AND ordinal > (
                       SELECT ordinal FROM runtime.pipeline_commands
                        WHERE command_id = %s
                   )
                """,
                (failure.command_id, run_id, failure.command_id),
            )
            cursor.execute(
                """
                UPDATE runtime.pipeline_runs
                   SET state = 'failed', version = version + 1,
                       updated_at = transaction_timestamp()
                 WHERE run_id = %s AND state IN ('accepted', 'running')
                RETURNING run_id
                """,
                (run_id,),
            )
            if cursor.fetchone() is None:
                raise StaleRunVersionError(run_id)
            return PipelineStageResult(failure.command_id, "failed", receipt_id)

        return self._transaction(operation)

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
            SELECT request_hash, profile, source_kind, source_value, state, version,
                   execution_profile, execution_profile_hash,
                   recompute_request, recompute_request_hash
              FROM runtime.pipeline_runs WHERE run_id = %s
            """,
            (run_id,),
        )
        run = cursor.fetchone()
        if run is None:
            raise PipelineRunNotFoundError(run_id)
        (
            request_hash,
            profile,
            source_kind,
            source_value,
            state,
            version,
            execution_profile_json,
            execution_profile_hash,
            recompute_request_json,
            recompute_request_hash,
        ) = run
        request_mapping: dict[str, object] = {"profile": _text(profile)}
        request_mapping["source_root" if _text(source_kind) == "root" else "source_reference"] = (
            _text(source_value)
        )
        request = PipelineRunRequest.from_mapping(request_mapping)
        frozen_execution_profile = _execution_profile(
            execution_profile_json,
            execution_profile_hash,
        )
        frozen_recompute_request: PipelineRecomputeRequest | None = None
        if recompute_request_json is not None:
            if isinstance(recompute_request_json, Mapping):
                recompute_mapping = cast(Mapping[str, object], recompute_request_json)
            else:
                decoded_recompute = cast(object, json.loads(_text(recompute_request_json)))
                if not isinstance(decoded_recompute, Mapping):
                    raise PipelineRunValidationError(
                        "persisted recompute request must be an object"
                    )
                recompute_mapping = cast(Mapping[str, object], decoded_recompute)
            frozen_recompute_request = parse_recompute_request(recompute_mapping)
            if recompute_request_hash is None or frozen_recompute_request.request_hash != _text(
                recompute_request_hash
            ):
                raise PipelineRunValidationError(
                    "persisted recompute request hash does not bind its canonical request"
                )
        elif recompute_request_hash is not None:
            raise PipelineRunValidationError("persisted recompute identity is incomplete")
        cursor.execute(
            """
            SELECT command.command_id, command.stage, command.state, receipt.receipt_id,
                   command.version, command.lease_id, command.blocking_command_id
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
                    None if row[6] is None else _text(row[6]),
                )
            )
        return PipelineRunSnapshot(
            run_id,
            request,
            _text(request_hash),
            _text(state),  # type: ignore[arg-type]
            tuple(commands),
            int(_text(version)),
            frozen_execution_profile,
            frozen_recompute_request,
        )


class PostgresPipelineScheduler(_PostgresTransactions):
    """Idempotent durable scheduler backed by the transactional run outbox."""

    def __init__(self, connection_factory: ConnectionFactory, *, lease_seconds: int = 60) -> None:
        super().__init__(connection_factory)
        if type(lease_seconds) is not int or lease_seconds < 1:  # noqa: E721
            raise PipelineRunValidationError("outbox lease_seconds must be positive")
        self._lease_seconds = lease_seconds

    async def enqueue(self, run_id: str) -> None:
        await asyncio.to_thread(self._enqueue_sync, run_id)

    def _enqueue_sync(self, run_id: str) -> None:
        self._transaction(lambda cursor: _ensure_pending_outbox(cursor, run_id))

    async def claim_next(self, *, lease_id: str) -> OutboxLease | None:
        return await asyncio.to_thread(self._claim_next_sync, lease_id)

    def _claim_next_sync(self, lease_id: str) -> OutboxLease | None:
        if type(lease_id) is not str or not lease_id.strip():  # noqa: E721
            raise PipelineRunValidationError("outbox lease_id must be non-empty")

        def operation(cursor: DbCursor) -> OutboxLease | None:
            cursor.execute(
                """
                UPDATE runtime.pipeline_run_outbox
                   SET state = 'pending', version = version + 1,
                       lease_id = NULL, lease_expires_at = NULL,
                       updated_at = transaction_timestamp()
                 WHERE state = 'leased'
                   AND lease_expires_at <= transaction_timestamp()
                """
            )
            cursor.execute(
                """
                SELECT outbox_id FROM runtime.pipeline_run_outbox
                 WHERE state = 'pending'
                 ORDER BY updated_at, run_id
                 LIMIT 1 FOR UPDATE SKIP LOCKED
                """
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                """
                UPDATE runtime.pipeline_run_outbox
                   SET state = 'leased', version = version + 1, lease_id = %s,
                       lease_expires_at = transaction_timestamp()
                           + (%s * interval '1 second'),
                       updated_at = transaction_timestamp()
                 WHERE outbox_id = %s AND state = 'pending'
                RETURNING outbox_id, run_id, version, lease_id
                """,
                (lease_id, self._lease_seconds, row[0]),
            )
            claimed = cursor.fetchone()
            if claimed is None:
                return None
            return OutboxLease(
                UUID(str(claimed[0])),
                _text(claimed[1]),
                int(_text(claimed[2])),
                _text(claimed[3]),
            )

        return self._transaction(operation)

    async def acknowledge(self, lease: OutboxLease) -> None:
        await asyncio.to_thread(self._finish_lease_sync, lease, "consumed")

    async def requeue(self, lease: OutboxLease) -> None:
        await asyncio.to_thread(self._finish_lease_sync, lease, "pending")

    async def renew(self, lease: OutboxLease) -> OutboxLease:
        return await asyncio.to_thread(self._renew_lease_sync, lease)

    def _renew_lease_sync(self, lease: OutboxLease) -> OutboxLease:
        if type(lease) is not OutboxLease:  # noqa: E721
            raise PipelineRunValidationError("outbox renewal requires an exact lease")

        def operation(cursor: DbCursor) -> OutboxLease:
            cursor.execute(
                """
                UPDATE runtime.pipeline_run_outbox
                   SET version = version + 1,
                       lease_expires_at = transaction_timestamp()
                           + (%s * interval '1 second'),
                       updated_at = transaction_timestamp()
                 WHERE outbox_id = %s AND run_id = %s AND state = 'leased'
                   AND version = %s AND lease_id = %s
                   AND lease_expires_at > transaction_timestamp()
                RETURNING outbox_id, run_id, version, lease_id
                """,
                (
                    self._lease_seconds,
                    lease.outbox_id,
                    lease.run_id,
                    lease.version,
                    lease.lease_id,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise StaleRunVersionError(lease.run_id)
            return OutboxLease(
                UUID(str(row[0])),
                _text(row[1]),
                int(_text(row[2])),
                _text(row[3]),
            )

        return self._transaction(operation)

    def _finish_lease_sync(self, lease: OutboxLease, state: str) -> None:
        if type(lease) is not OutboxLease:  # noqa: E721
            raise PipelineRunValidationError("outbox transition requires an exact lease")

        def operation(cursor: DbCursor) -> None:
            cursor.execute(
                """
                UPDATE runtime.pipeline_run_outbox
                   SET state = %s, version = version + 1,
                       lease_id = NULL, lease_expires_at = NULL,
                       updated_at = transaction_timestamp()
                 WHERE outbox_id = %s AND run_id = %s AND state = 'leased'
                   AND version = %s AND lease_id = %s
                   AND lease_expires_at > transaction_timestamp()
                RETURNING outbox_id
                """,
                (state, lease.outbox_id, lease.run_id, lease.version, lease.lease_id),
            )
            if cursor.fetchone() is None:
                raise StaleRunVersionError(lease.run_id)

        self._transaction(operation)

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
