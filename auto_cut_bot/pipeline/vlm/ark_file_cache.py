"""Durable, tenant-scoped Ark Files API cache with leased recovery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    provider_scope_fingerprint: str
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
    lease_token: str | None
    lease_expires_at: datetime | None
    audit_expires_at: datetime | None


class ArkFileCachePort(Protocol):
    def claim(
        self,
        *,
        provider_id: str,
        provider_scope_fingerprint: str,
        content_hash: str,
        byte_length: int,
        media_type: str,
        preprocess_policy_hash: str,
        lease_seconds: int,
        unknown_outcome_quarantine_seconds: int,
    ) -> tuple[ArkFileCacheRecord, bool]: ...

    def record_processing(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        expected_lease_token: str | None,
        provider_file_id: str,
        provider_status: str,
    ) -> ArkFileCacheRecord: ...

    def record_available(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        expected_lease_token: str | None,
        provider_status: str,
        expires_at: datetime,
    ) -> ArkFileCacheRecord: ...

    def record_indeterminate(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        expected_lease_token: str | None,
        provider_status: str,
        audit_expires_at: datetime,
    ) -> ArkFileCacheRecord: ...

    def record_failed(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        expected_lease_token: str | None,
        provider_status: str,
        failure_code: str,
    ) -> ArkFileCacheRecord: ...

    def release_processing(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        expected_lease_token: str | None,
        provider_status: str,
    ) -> ArkFileCacheRecord: ...

    def mark_expired(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        provider_status: str,
    ) -> ArkFileCacheRecord: ...


class PostgresArkFileCache:
    """CAS owner for scoped provider files, recovery leases, and audit expiry."""

    def __init__(
        self,
        connection_factory: Callable[[], DbConnection],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._connection_factory = connection_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def claim(
        self,
        *,
        provider_id: str,
        provider_scope_fingerprint: str,
        content_hash: str,
        byte_length: int,
        media_type: str,
        preprocess_policy_hash: str,
        lease_seconds: int,
        unknown_outcome_quarantine_seconds: int,
    ) -> tuple[ArkFileCacheRecord, bool]:
        _nonempty(provider_id, "provider_id")
        _sha256(provider_scope_fingerprint, "provider_scope_fingerprint")
        _sha256(content_hash, "content_hash")
        _sha256(preprocess_policy_hash, "preprocess_policy_hash")
        _nonempty(media_type, "media_type")
        if type(byte_length) is not int or byte_length < 1:  # noqa: E721
            raise ValueError("byte_length must be a positive integer")
        _positive_seconds(lease_seconds, "lease_seconds")
        _positive_seconds(
            unknown_outcome_quarantine_seconds,
            "unknown_outcome_quarantine_seconds",
        )
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        audit_expires_at = now + timedelta(seconds=unknown_outcome_quarantine_seconds)

        def operation(cursor: DbCursor) -> tuple[ArkFileCacheRecord, bool]:
            created = self._insert_reservation(
                cursor,
                provider_id=provider_id,
                provider_scope_fingerprint=provider_scope_fingerprint,
                content_hash=content_hash,
                byte_length=byte_length,
                media_type=media_type,
                preprocess_policy_hash=preprocess_policy_hash,
                reserved_at=now,
                lease_expires_at=lease_expires_at,
            )
            record = self._select_live(
                cursor,
                provider_id,
                provider_scope_fingerprint,
                content_hash,
                preprocess_policy_hash,
            )
            if record.byte_length != byte_length or record.media_type != media_type:
                raise ArkFileCacheError(
                    "provider media identity collided with different metadata"
                )
            if created:
                return record, True

            if record.state == "reserved" and _lease_expired(record, now):
                cursor.execute(
                    """
                    UPDATE runtime.provider_media_objects
                       SET state = 'indeterminate', version = version + 1,
                           provider_status = 'upload_outcome_unknown_after_lease',
                           completed_at = %s, audit_expires_at = %s,
                           lease_token = NULL, lease_expires_at = NULL
                     WHERE media_object_id = %s AND version = %s AND state = 'reserved'
                       AND lease_token = %s AND lease_expires_at <= %s
                    """,
                    (
                        now,
                        audit_expires_at,
                        record.media_object_id,
                        record.version,
                        record.lease_token,
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ArkFileCacheError("stale reserved provider-media lease")
                return self._select_by_id(cursor, record.media_object_id), False

            if record.state == "processing" and _lease_expired(record, now):
                next_token = str(uuid4())
                cursor.execute(
                    """
                    UPDATE runtime.provider_media_objects
                       SET version = version + 1,
                           provider_status = 'processing_recovery_claimed',
                           lease_token = %s, lease_expires_at = %s
                     WHERE media_object_id = %s AND version = %s AND state = 'processing'
                       AND lease_token = %s AND lease_expires_at <= %s
                    """,
                    (
                        next_token,
                        lease_expires_at,
                        record.media_object_id,
                        record.version,
                        record.lease_token,
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ArkFileCacheError("stale processing provider-media lease")
                return self._select_by_id(cursor, record.media_object_id), True

            if (
                record.state == "indeterminate"
                and record.provider_file_id is None
                and record.audit_expires_at is not None
                and record.audit_expires_at <= now
            ):
                cursor.execute(
                    """
                    UPDATE runtime.provider_media_objects
                       SET state = 'expired', version = version + 1,
                           provider_status = 'unknown_upload_audit_expired',
                           audit_expires_at = NULL
                     WHERE media_object_id = %s AND version = %s
                       AND state = 'indeterminate' AND provider_file_id IS NULL
                       AND audit_expires_at <= %s
                    """,
                    (record.media_object_id, record.version, now),
                )
                if cursor.rowcount != 1:
                    raise ArkFileCacheError("stale provider-media audit expiry")
                if not self._insert_reservation(
                    cursor,
                    provider_id=provider_id,
                    provider_scope_fingerprint=provider_scope_fingerprint,
                    content_hash=content_hash,
                    byte_length=byte_length,
                    media_type=media_type,
                    preprocess_policy_hash=preprocess_policy_hash,
                    reserved_at=now,
                    lease_expires_at=lease_expires_at,
                ):
                    raise ArkFileCacheError("provider-media reclamation lost its reservation")
                return (
                    self._select_live(
                        cursor,
                        provider_id,
                        provider_scope_fingerprint,
                        content_hash,
                        preprocess_policy_hash,
                    ),
                    True,
                )
            return record, False

        return self._transaction(operation)

    def record_processing(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        expected_lease_token: str | None,
        provider_file_id: str,
        provider_status: str,
    ) -> ArkFileCacheRecord:
        return self._leased_transition(
            media_object_id,
            expected_version=expected_version,
            expected_lease_token=expected_lease_token,
            source_states=("reserved",),
            target_state="processing",
            assignments=(
                "provider_file_id = %s, provider_status = %s, "
                "uploaded_at = transaction_timestamp()"
            ),
            values=(provider_file_id, provider_status),
        )

    def record_available(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        expected_lease_token: str | None,
        provider_status: str,
        expires_at: datetime,
    ) -> ArkFileCacheRecord:
        return self._leased_transition(
            media_object_id,
            expected_version=expected_version,
            expected_lease_token=expected_lease_token,
            source_states=("processing",),
            target_state="available",
            assignments=(
                "provider_status = %s, available_at = transaction_timestamp(), "
                "expires_at = %s, lease_token = NULL, lease_expires_at = NULL"
            ),
            values=(provider_status, expires_at),
        )

    def record_indeterminate(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        expected_lease_token: str | None,
        provider_status: str,
        audit_expires_at: datetime,
    ) -> ArkFileCacheRecord:
        return self._leased_transition(
            media_object_id,
            expected_version=expected_version,
            expected_lease_token=expected_lease_token,
            source_states=("reserved",),
            target_state="indeterminate",
            assignments=(
                "provider_status = %s, completed_at = transaction_timestamp(), "
                "audit_expires_at = %s, lease_token = NULL, lease_expires_at = NULL"
            ),
            values=(provider_status, audit_expires_at),
        )

    def record_failed(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        expected_lease_token: str | None,
        provider_status: str,
        failure_code: str,
    ) -> ArkFileCacheRecord:
        return self._leased_transition(
            media_object_id,
            expected_version=expected_version,
            expected_lease_token=expected_lease_token,
            source_states=("reserved", "processing"),
            target_state="failed",
            assignments=(
                "provider_status = %s, failure_code = %s, "
                "completed_at = transaction_timestamp(), "
                "lease_token = NULL, lease_expires_at = NULL"
            ),
            values=(provider_status, failure_code),
        )

    def release_processing(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        expected_lease_token: str | None,
        provider_status: str,
    ) -> ArkFileCacheRecord:
        return self._leased_transition(
            media_object_id,
            expected_version=expected_version,
            expected_lease_token=expected_lease_token,
            source_states=("processing",),
            target_state="processing",
            assignments="provider_status = %s, lease_expires_at = %s",
            values=(provider_status, self._clock()),
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
            source_states=("available",),
            target_state="expired",
            assignments="provider_status = %s, completed_at = transaction_timestamp()",
            values=(provider_status,),
        )

    @staticmethod
    def _insert_reservation(
        cursor: DbCursor,
        *,
        provider_id: str,
        provider_scope_fingerprint: str,
        content_hash: str,
        byte_length: int,
        media_type: str,
        preprocess_policy_hash: str,
        reserved_at: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        cursor.execute(
            """
            INSERT INTO runtime.provider_media_objects
                (media_object_id, provider_id, provider_scope_fingerprint,
                 content_hash, byte_length, media_type, preprocess_policy_hash,
                 generation, state, version, reserved_at, lease_token, lease_expires_at)
            SELECT %s, %s, %s, %s, %s, %s, %s,
                   COALESCE(max(generation), 0) + 1, 'reserved', 0, %s, %s, %s
              FROM runtime.provider_media_objects
             WHERE provider_id = %s AND provider_scope_fingerprint = %s
               AND content_hash = %s AND preprocess_policy_hash = %s
            ON CONFLICT DO NOTHING
            RETURNING media_object_id
            """,
            (
                uuid4(),
                provider_id,
                provider_scope_fingerprint,
                content_hash,
                byte_length,
                media_type,
                preprocess_policy_hash,
                reserved_at,
                str(uuid4()),
                lease_expires_at,
                provider_id,
                provider_scope_fingerprint,
                content_hash,
                preprocess_policy_hash,
            ),
        )
        return cursor.fetchone() is not None

    @staticmethod
    def _select_live(
        cursor: DbCursor,
        provider_id: str,
        provider_scope_fingerprint: str,
        content_hash: str,
        preprocess_policy_hash: str,
    ) -> ArkFileCacheRecord:
        cursor.execute(
            _SELECT_RECORD
            + """
             WHERE provider_id = %s AND provider_scope_fingerprint = %s
               AND content_hash = %s AND preprocess_policy_hash = %s
               AND state <> 'expired'
             ORDER BY generation DESC LIMIT 1 FOR UPDATE
            """,
            (
                provider_id,
                provider_scope_fingerprint,
                content_hash,
                preprocess_policy_hash,
            ),
        )
        return _record(cursor.fetchone())

    @staticmethod
    def _select_by_id(cursor: DbCursor, media_object_id: UUID) -> ArkFileCacheRecord:
        cursor.execute(
            _SELECT_RECORD + " WHERE media_object_id = %s",
            (media_object_id,),
        )
        return _record(cursor.fetchone())

    def _leased_transition(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        expected_lease_token: str | None,
        source_states: tuple[str, ...],
        target_state: str,
        assignments: str,
        values: tuple[object, ...],
    ) -> ArkFileCacheRecord:
        _nonempty(expected_lease_token, "expected_lease_token")
        return self._transition(
            media_object_id,
            expected_version=expected_version,
            source_states=source_states,
            target_state=target_state,
            assignments=assignments,
            values=values,
            expected_lease_token=expected_lease_token,
        )

    def _transition(
        self,
        media_object_id: UUID,
        *,
        expected_version: int,
        source_states: tuple[str, ...],
        target_state: str,
        assignments: str,
        values: tuple[object, ...],
        expected_lease_token: str | None = None,
    ) -> ArkFileCacheRecord:
        if not source_states:
            raise ValueError("a transition must declare at least one source state")

        def operation(cursor: DbCursor) -> ArkFileCacheRecord:
            placeholders = ",".join("%s" for _ in source_states)
            lease_clause = " AND lease_token = %s" if expected_lease_token is not None else ""
            cursor.execute(
                f"""
                UPDATE runtime.provider_media_objects
                   SET state = %s, version = version + 1, {assignments}
                 WHERE media_object_id = %s AND version = %s
                   AND state IN ({placeholders}){lease_clause}
                """,  # noqa: S608 - SQL fragments derive only from closed internal values
                (
                    target_state,
                    *values,
                    media_object_id,
                    expected_version,
                    *source_states,
                    *((expected_lease_token,) if expected_lease_token is not None else ()),
                ),
            )
            if cursor.rowcount != 1:
                raise ArkFileCacheError("stale, unleased, or invalid provider media transition")
            return self._select_by_id(cursor, media_object_id)

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


_SELECT_RECORD = """
    SELECT media_object_id, provider_id, provider_scope_fingerprint,
           content_hash, byte_length, media_type, preprocess_policy_hash,
           generation, state, version, provider_file_id, provider_status,
           failure_code, reserved_at, uploaded_at, available_at, expires_at,
           completed_at, lease_token, lease_expires_at, audit_expires_at
      FROM runtime.provider_media_objects
"""


def _record(row: tuple[object, ...] | None) -> ArkFileCacheRecord:
    if row is None:
        raise ArkFileCacheError("provider media object was not found")
    return ArkFileCacheRecord(
        media_object_id=UUID(str(row[0])),
        provider_id=str(row[1]),
        provider_scope_fingerprint=str(row[2]),
        content_hash=str(row[3]),
        byte_length=int(str(row[4])),
        media_type=str(row[5]),
        preprocess_policy_hash=str(row[6]),
        generation=int(str(row[7])),
        state=str(row[8]),
        version=int(str(row[9])),
        provider_file_id=None if row[10] is None else str(row[10]),
        provider_status=None if row[11] is None else str(row[11]),
        failure_code=None if row[12] is None else str(row[12]),
        reserved_at=row[13],  # type: ignore[arg-type]
        uploaded_at=row[14],  # type: ignore[arg-type]
        available_at=row[15],  # type: ignore[arg-type]
        expires_at=row[16],  # type: ignore[arg-type]
        completed_at=row[17],  # type: ignore[arg-type]
        lease_token=None if row[18] is None else str(row[18]),
        lease_expires_at=row[19],  # type: ignore[arg-type]
        audit_expires_at=row[20],  # type: ignore[arg-type]
    )


def _lease_expired(record: ArkFileCacheRecord, now: datetime) -> bool:
    return record.lease_expires_at is not None and record.lease_expires_at <= now


def _nonempty(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise ValueError(f"{field_name} must be non-empty text")


def _sha256(value: object, field_name: str) -> None:
    _nonempty(value, field_name)
    assert isinstance(value, str)
    if (
        len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{field_name} must be a lowercase sha256 digest")


def _positive_seconds(value: object, field_name: str) -> None:
    if type(value) is not int or value < 1:  # noqa: E721
        raise ValueError(f"{field_name} must be a positive integer")


__all__ = [
    "ArkFileCacheError",
    "ArkFileCachePort",
    "ArkFileCacheRecord",
    "PostgresArkFileCache",
]
