"""Durable Ark Files API identity cache backed by the Kernel PostgreSQL database."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeVar
from uuid import UUID, uuid4


class ArkFileCacheError(RuntimeError):
    """Raised when a provider-media lifecycle transition cannot be committed."""


class DbCursor(Protocol):
    rowcount: int

    def execute(self, query: str, params: tuple[object, ...] = ()) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...


class DbConnection(Protocol):
    def cursor(self) -> DbCursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


_Result = TypeVar("_Result")


@dataclass(frozen=True, slots=True)
class ArkFileCacheRecord:
    media_object_id: UUID
    provider_id: str
    content_hash: str
    byte_length: int
    media_type: str
    preprocess_policy_hash: str
    generation: int
    state: str
    version: int
    provider_file_id: str | None
    provider_status: str | None
    failure_code: str | None
    reserved_at: datetime
    uploaded_at: datetime | None
    available_at: datetime | None
    expires_at: datetime | None
    completed_at: datetime | None


class ArkFileCachePort(Protocol):
    def claim(
        self,
        *,
        provider_id: str,
        content_hash: str,
        byte_length: int,
        media_type: str,
        preprocess_policy_hash: str,
    ) -> tuple[ArkFileCacheRecord, bool]: ...

    def record_processing(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        provider_file_id: str,
        provider_status: str,
    ) -> ArkFileCacheRecord: ...

    def record_available(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        provider_status: str,
        expires_at: datetime,
    ) -> ArkFileCacheRecord: ...

    def record_indeterminate(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        provider_status: str,
    ) -> ArkFileCacheRecord: ...

    def record_failed(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        provider_status: str,
        failure_code: str,
    ) -> ArkFileCacheRecord: ...

    def mark_expired(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        provider_status: str,
    ) -> ArkFileCacheRecord: ...


class PostgresArkFileCache:
    """CAS owner for provider-side media objects; never stores local paths or secrets."""

    def __init__(self, connection_factory: Callable[[], DbConnection]) -> None:
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._connection_factory = connection_factory

    def claim(
        self,
        *,
        provider_id: str,
        content_hash: str,
        byte_length: int,
        media_type: str,
        preprocess_policy_hash: str,
    ) -> tuple[ArkFileCacheRecord, bool]:
        object_id = uuid4()

        def operation(cursor: DbCursor) -> tuple[ArkFileCacheRecord, bool]:
            cursor.execute(
                """
                INSERT INTO runtime.provider_media_objects
                    (media_object_id, provider_id, content_hash, byte_length, media_type,
                     preprocess_policy_hash, generation, state, version)
                SELECT %s, %s, %s, %s, %s, %s,
                       COALESCE(max(generation), 0) + 1, 'reserved', 0
                  FROM runtime.provider_media_objects
                 WHERE provider_id = %s AND content_hash = %s
                   AND preprocess_policy_hash = %s
                ON CONFLICT DO NOTHING
                RETURNING media_object_id
                """,
                (
                    object_id,
                    provider_id,
                    content_hash,
                    byte_length,
                    media_type,
                    preprocess_policy_hash,
                    provider_id,
                    content_hash,
                    preprocess_policy_hash,
                ),
            )
            created = cursor.fetchone() is not None
            cursor.execute(
                """
                SELECT media_object_id, provider_id, content_hash, byte_length, media_type,
                       preprocess_policy_hash, generation, state, version, provider_file_id,
                       provider_status, failure_code, reserved_at, uploaded_at,
                       available_at, expires_at, completed_at
                  FROM runtime.provider_media_objects
                 WHERE provider_id = %s AND content_hash = %s
                   AND preprocess_policy_hash = %s AND state <> 'expired'
                 ORDER BY generation DESC
                 LIMIT 1
                 FOR UPDATE
                """,
                (provider_id, content_hash, preprocess_policy_hash),
            )
            record = _record(cursor.fetchone())
            if record.byte_length != byte_length or record.media_type != media_type:
                raise ArkFileCacheError("provider media identity collided with different metadata")
            return record, created

        return self._transaction(operation)

    def record_processing(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        provider_file_id: str,
        provider_status: str,
    ) -> ArkFileCacheRecord:
        return self._transition(
            media_object_id,
            expected_version=expected_version,
            source_state="reserved",
            target_state="processing",
            assignments="provider_file_id = %s, provider_status = %s, uploaded_at = transaction_timestamp()",
            values=(provider_file_id, provider_status),
        )

    def record_available(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        provider_status: str,
        expires_at: datetime,
    ) -> ArkFileCacheRecord:
        return self._transition(
            media_object_id,
            expected_version=expected_version,
            source_state="processing",
            target_state="available",
            assignments=(
                "provider_status = %s, available_at = transaction_timestamp(), expires_at = %s"
            ),
            values=(provider_status, expires_at),
        )

    def record_indeterminate(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        provider_status: str,
    ) -> ArkFileCacheRecord:
        return self._transition(
            media_object_id,
            expected_version=expected_version,
            source_state=None,
            target_state="indeterminate",
            assignments="provider_status = %s, completed_at = transaction_timestamp()",
            values=(provider_status,),
            allowed_sources=("reserved", "processing"),
        )

    def record_failed(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        provider_status: str,
        failure_code: str,
    ) -> ArkFileCacheRecord:
        return self._transition(
            media_object_id,
            expected_version=expected_version,
            source_state=None,
            target_state="failed",
            assignments=(
                "provider_status = %s, failure_code = %s, completed_at = transaction_timestamp()"
            ),
            values=(provider_status, failure_code),
            allowed_sources=("reserved", "processing"),
        )

    def mark_expired(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        provider_status: str,
    ) -> ArkFileCacheRecord:
        return self._transition(
            media_object_id,
            expected_version=expected_version,
            source_state="available",
            target_state="expired",
            assignments="provider_status = %s, completed_at = transaction_timestamp()",
            values=(provider_status,),
        )

    def _transition(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        source_state: str | None,
        target_state: str,
        assignments: str,
        values: tuple[object, ...],
        allowed_sources: tuple[str, ...] = (),
    ) -> ArkFileCacheRecord:
        states = allowed_sources or ((source_state,) if source_state is not None else ())
        if not states:
            raise ValueError("a transition must declare at least one source state")

        def operation(cursor: DbCursor) -> ArkFileCacheRecord:
            placeholders = ",".join("%s" for _ in states)
            cursor.execute(
                f"""
                UPDATE runtime.provider_media_objects
                   SET state = %s, version = version + 1, {assignments}
                 WHERE media_object_id = %s AND version = %s
                   AND state IN ({placeholders})
                """,  # noqa: S608 - placeholders are derived only from internal state count
                (target_state, *values, media_object_id, expected_version, *states),
            )
            if cursor.rowcount != 1:
                raise ArkFileCacheError("stale or invalid provider media transition")
            cursor.execute(
                """
                SELECT media_object_id, provider_id, content_hash, byte_length, media_type,
                       preprocess_policy_hash, generation, state, version, provider_file_id,
                       provider_status, failure_code, reserved_at, uploaded_at,
                       available_at, expires_at, completed_at
                  FROM runtime.provider_media_objects WHERE media_object_id = %s
                """,
                (media_object_id,),
            )
            return _record(cursor.fetchone())

        return self._transaction(operation)

    def _transaction(self, operation: Callable[[DbCursor], _Result]) -> _Result:
        connection = self._connection_factory()
        try:
            cursor = connection.cursor()
            result = operation(cursor)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _record(row: tuple[object, ...] | None) -> ArkFileCacheRecord:
    if row is None:
        raise ArkFileCacheError("provider media object was not found")
    return ArkFileCacheRecord(
        media_object_id=UUID(str(row[0])),
        provider_id=str(row[1]),
        content_hash=str(row[2]),
        byte_length=int(str(row[3])),
        media_type=str(row[4]),
        preprocess_policy_hash=str(row[5]),
        generation=int(str(row[6])),
        state=str(row[7]),
        version=int(str(row[8])),
        provider_file_id=None if row[9] is None else str(row[9]),
        provider_status=None if row[10] is None else str(row[10]),
        failure_code=None if row[11] is None else str(row[11]),
        reserved_at=row[12],  # type: ignore[arg-type]
        uploaded_at=row[13],  # type: ignore[arg-type]
        available_at=row[14],  # type: ignore[arg-type]
        expires_at=row[15],  # type: ignore[arg-type]
        completed_at=row[16],  # type: ignore[arg-type]
    )


__all__ = [
    "ArkFileCacheError",
    "ArkFileCachePort",
    "ArkFileCacheRecord",
    "PostgresArkFileCache",
]
