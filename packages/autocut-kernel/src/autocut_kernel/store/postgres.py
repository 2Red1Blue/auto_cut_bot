"""Closed PostgreSQL persistence adapter for the local Pipeline MVP.

The adapter accepts only semantic command operations.  It does not expose a
cursor, generic row writer, legacy ArtifactBus object, or an execution escape
hatch to callers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar
from uuid import UUID, uuid4

from .errors import CommandStateError, StaleHeadError, StoreValidationError
from .models import (
    ArtifactMember,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
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


_Result = TypeVar("_Result")


class PostgresRuntimeStore:
    """Persist one Job's idempotent commands, receipts and immutable artifacts."""

    def __init__(self, connection_factory: Callable[[], DbConnection]) -> None:
        if not callable(connection_factory):
            raise StoreValidationError("connection_factory must be callable")
        self._connection_factory = connection_factory

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        """Create or replay a canonical command claim for one durable Job."""

        def operation(cursor: DbCursor) -> CommandOutcome:
            job_id = self._ensure_job(cursor, claim.job)
            cursor.execute(
                """
                SELECT command_slot_id, command_name, request_hash, state
                  FROM runtime.command_slots
                 WHERE job_id = %s AND idempotency_key = %s FOR UPDATE
                """,
                (job_id, claim.idempotency_key),
            )
            existing = cursor.fetchone()
            if existing is not None:
                slot_id, command_name, request_hash, _state = existing
                if command_name != claim.command_name or request_hash != claim.request_hash:
                    raise StoreValidationError(
                        "idempotency key was already claimed by a different command"
                    )
                return self._read_outcome_by_slot(cursor, UUID(str(slot_id)), job_id)
            slot_id = uuid4()
            cursor.execute(
                """
                INSERT INTO runtime.command_slots
                    (command_slot_id, job_id, idempotency_key, command_name, request_hash, state)
                VALUES (%s, %s, %s, %s, %s, 'running')
                """,
                (slot_id, job_id, claim.idempotency_key, claim.command_name, claim.request_hash),
            )
            cursor.execute("UPDATE runtime.jobs SET state = 'running' WHERE job_id = %s", (job_id,))
            return CommandOutcome(command_slot_id=slot_id, state="running", job_id=job_id)

        return self._transaction(operation)

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        """Atomically persist one non-empty immutable result set and success Receipt."""

        def operation(cursor: DbCursor) -> CommandOutcome:
            job_id, state = self._locked_slot(cursor, success.command_slot_id)
            if state != "running":
                return self._replay_or_raise(
                    cursor, success.command_slot_id, job_id, "succeeded", success.set_hash
                )

            artifact_set_id = uuid4()
            cursor.execute(
                """
                INSERT INTO runtime.artifact_sets (artifact_set_id, command_slot_id, job_id, set_hash, member_count)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    artifact_set_id,
                    success.command_slot_id,
                    job_id,
                    success.set_hash,
                    len(success.artifacts),
                ),
            )
            inserted: list[tuple[UUID, ArtifactMember]] = []
            for ordinal, artifact in enumerate(success.artifacts):
                self._assert_next_revision(cursor, job_id, artifact)
                artifact_id = uuid4()
                cursor.execute(
                    """
                    INSERT INTO runtime.artifacts
                        (artifact_id, artifact_set_id, job_id, artifact_type, logical_id, revision,
                         namespace, scope_kind, scope_key, content_hash, payload_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        artifact_id,
                        artifact_set_id,
                        job_id,
                        artifact.artifact_type,
                        artifact.logical_id,
                        artifact.revision,
                        artifact.scope.namespace,
                        artifact.scope.kind,
                        artifact.scope.key,
                        artifact.content_hash,
                        artifact.payload_json,
                    ),
                )
                cursor.execute(
                    "INSERT INTO runtime.artifact_set_members (artifact_set_id, ordinal, artifact_id) VALUES (%s, %s, %s)",
                    (artifact_set_id, ordinal, artifact_id),
                )
                inserted.append((artifact_id, artifact))
            for artifact_id, artifact in inserted:
                cursor.execute(
                    """
                    INSERT INTO runtime.logical_heads
                        (job_id, namespace, scope_kind, scope_key, artifact_type, logical_id, artifact_id, revision)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (job_id, namespace, scope_kind, scope_key, artifact_type, logical_id)
                    DO UPDATE SET artifact_id = EXCLUDED.artifact_id, revision = EXCLUDED.revision
                    """,
                    (
                        job_id,
                        artifact.scope.namespace,
                        artifact.scope.kind,
                        artifact.scope.key,
                        artifact.artifact_type,
                        artifact.logical_id,
                        artifact_id,
                        artifact.revision,
                    ),
                )
            receipt_id = uuid4()
            cursor.execute(
                """
                INSERT INTO runtime.command_receipts
                    (receipt_id, command_slot_id, outcome, result_artifact_set_id)
                VALUES (%s, %s, 'succeeded', %s)
                """,
                (receipt_id, success.command_slot_id, artifact_set_id),
            )
            self._complete(cursor, success.command_slot_id, job_id, "succeeded")
            return CommandOutcome(
                command_slot_id=success.command_slot_id,
                state="succeeded",
                receipt_id=receipt_id,
                artifact_set_id=artifact_set_id,
                job_id=job_id,
            )

        return self._transaction(operation)

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome:
        """Atomically persist a terminal deny/fail Receipt without inventing an ArtifactSet."""

        def operation(cursor: DbCursor) -> CommandOutcome:
            job_id, state = self._locked_slot(cursor, rejection.command_slot_id)
            if state != "running":
                return self._replay_or_raise(
                    cursor, rejection.command_slot_id, job_id, rejection.outcome, None
                )
            receipt_id = uuid4()
            cursor.execute(
                """
                INSERT INTO runtime.command_receipts
                    (receipt_id, command_slot_id, outcome, failure_code, failure_detail)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                """,
                (
                    receipt_id,
                    rejection.command_slot_id,
                    rejection.outcome,
                    rejection.failure_code,
                    rejection.failure_detail_json,
                ),
            )
            self._complete(cursor, rejection.command_slot_id, job_id, rejection.outcome)
            return CommandOutcome(
                command_slot_id=rejection.command_slot_id,
                state=rejection.outcome,
                receipt_id=receipt_id,
                failure_code=rejection.failure_code,
                failure_detail_json=rejection.failure_detail_json,
                job_id=job_id,
            )

        return self._transaction(operation)

    def read_outcome(self, job: Job, idempotency_key: str) -> CommandOutcome | None:
        """Read the durable state of a previously claimed semantic command."""

        if type(idempotency_key) is not str or not idempotency_key:  # noqa: E721
            raise StoreValidationError("idempotency_key must be a non-empty string")

        def operation(cursor: DbCursor) -> CommandOutcome | None:
            cursor.execute("SELECT job_id FROM runtime.jobs WHERE job_key = %s", (job.job_key,))
            row = cursor.fetchone()
            if row is None:
                return None
            job_id = UUID(str(row[0]))
            cursor.execute(
                "SELECT command_slot_id FROM runtime.command_slots WHERE job_id = %s AND idempotency_key = %s",
                (job_id, idempotency_key),
            )
            command = cursor.fetchone()
            return (
                None
                if command is None
                else self._read_outcome_by_slot(cursor, UUID(str(command[0])), job_id)
            )

        return self._transaction(operation)

    def _ensure_job(self, cursor: DbCursor, job: Job) -> UUID:
        cursor.execute(
            "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s FOR UPDATE", (job.job_key,)
        )
        existing = cursor.fetchone()
        if existing is not None:
            job_id, profile = existing
            if profile != job.profile:
                raise StoreValidationError("job_key cannot be reused with a different profile")
            return UUID(str(job_id))
        job_id = uuid4()
        cursor.execute(
            "INSERT INTO runtime.jobs (job_id, job_key, profile, state) VALUES (%s, %s, %s, 'pending')",
            (job_id, job.job_key, job.profile),
        )
        return job_id

    @staticmethod
    def _locked_slot(cursor: DbCursor, slot_id: UUID) -> tuple[UUID, str]:
        cursor.execute(
            "SELECT job_id, state FROM runtime.command_slots WHERE command_slot_id = %s FOR UPDATE",
            (slot_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise StoreValidationError("command_slot_id is unknown")
        return UUID(str(row[0])), str(row[1])

    @staticmethod
    def _complete(cursor: DbCursor, slot_id: UUID, job_id: UUID, outcome: str) -> None:
        cursor.execute(
            "UPDATE runtime.command_slots SET state = %s, completed_at = transaction_timestamp() WHERE command_slot_id = %s",
            (outcome, slot_id),
        )
        cursor.execute("UPDATE runtime.jobs SET state = %s WHERE job_id = %s", (outcome, job_id))

    def _assert_next_revision(self, cursor: DbCursor, job_id: UUID, member: ArtifactMember) -> None:
        cursor.execute(
            """
            SELECT revision FROM runtime.logical_heads
             WHERE job_id = %s AND namespace = %s AND scope_kind = %s AND scope_key = %s
               AND artifact_type = %s AND logical_id = %s FOR UPDATE
            """,
            (
                job_id,
                member.scope.namespace,
                member.scope.kind,
                member.scope.key,
                member.artifact_type,
                member.logical_id,
            ),
        )
        current = cursor.fetchone()
        expected = 1 if current is None else int(str(current[0])) + 1
        if member.revision != expected:
            raise StaleHeadError(f"expected revision {expected}, received {member.revision}")

    def _replay_or_raise(
        self, cursor: DbCursor, slot_id: UUID, job_id: UUID, intended: str, set_hash: str | None
    ) -> CommandOutcome:
        outcome = self._read_outcome_by_slot(cursor, slot_id, job_id)
        if outcome.state != intended:
            raise CommandStateError(f"command already completed as {outcome.state}")
        if set_hash is not None and outcome.artifact_set_id is not None:
            cursor.execute(
                "SELECT set_hash FROM runtime.artifact_sets WHERE artifact_set_id = %s",
                (outcome.artifact_set_id,),
            )
            row = cursor.fetchone()
            if row is None or row[0] != set_hash:
                raise CommandStateError("command already completed with a different artifact set")
        return outcome

    @staticmethod
    def _read_outcome_by_slot(cursor: DbCursor, slot_id: UUID, job_id: UUID) -> CommandOutcome:
        cursor.execute(
            """
            SELECT slot.state, receipt.receipt_id, receipt.result_artifact_set_id,
                   receipt.failure_code, receipt.failure_detail::text
              FROM runtime.command_slots AS slot
              LEFT JOIN runtime.command_receipts AS receipt ON receipt.command_slot_id = slot.command_slot_id
             WHERE slot.command_slot_id = %s
            """,
            (slot_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise StoreValidationError("command_slot_id is unknown")
        state, receipt_id, set_id, failure_code, failure_detail = row
        return CommandOutcome(
            command_slot_id=slot_id,
            state=str(state),  # type: ignore[arg-type]
            receipt_id=None if receipt_id is None else UUID(str(receipt_id)),
            artifact_set_id=None if set_id is None else UUID(str(set_id)),
            failure_code=None if failure_code is None else str(failure_code),
            failure_detail_json=None if failure_detail is None else str(failure_detail),
            job_id=job_id,
        )

    def _transaction(self, operation: Callable[[DbCursor], _Result]) -> _Result:
        connection = self._connection_factory()
        cursor = connection.cursor()
        try:
            result = operation(cursor)
            connection.commit()
            return result
        except Exception as error:
            connection.rollback()
            if self._is_first_head_race(error):
                raise StaleHeadError(
                    "a concurrent command created the same logical head"
                ) from error
            raise
        finally:
            cursor.close()
            connection.close()

    @staticmethod
    def _is_first_head_race(error: Exception) -> bool:
        return getattr(
            error, "sqlstate", None
        ) == "23505" and "runtime_artifacts_scope_revision_key" in str(error)
