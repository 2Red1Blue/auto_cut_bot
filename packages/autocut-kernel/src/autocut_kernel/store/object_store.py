"""Verified large-object persistence for production render outputs.

The PostgreSQL Store owns authority and claims.  This adapter owns only the
effect of putting already-rendered immutable bytes into an S3-compatible
workspace and proving the resulting object metadata.  Object existence never
grants visibility or publication authority.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Final, Literal, Protocol, cast
from uuid import RFC_4122, UUID

from .models import BlobRef

try:  # Native Windows may import write-only store code; reads remain POSIX-only.
    import fcntl as _fcntl
except ModuleNotFoundError:  # pragma: no cover - exercised by native Windows.
    _fcntl = None

OBJECT_STORE_WRITE_SCHEMA_VERSION: Final = "object-store-write-v1"
OBJECT_STORE_WRITE_STRATEGY: Final = "s3-single-put-v1"
_SHA256_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_ID_PATTERN: Final = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_SAFE_PREFIX_PATTERN: Final = re.compile(
    r"^(?:[a-zA-Z0-9][a-zA-Z0-9._-]*/)*[a-zA-Z0-9][a-zA-Z0-9._-]*$"
)
_OBJECT_ID_METADATA_KEY: Final = "autocut-object-id"
_CONTENT_HASH_METADATA_KEY: Final = "autocut-content-sha256"
_RESERVATION_ATTESTATION: Final = object()
_READ_GRANT_ATTESTATION: Final = object()

ObjectStoreWriteOutcome = Literal["denied", "failed", "indeterminate"]
ObjectStoreReadOutcome = Literal["denied", "failed", "indeterminate"]


class S3ObjectClient(Protocol):
    """Small synchronous client surface implemented by boto3 S3 clients."""

    def put_object(self, **kwargs: object) -> Mapping[str, object]: ...

    def head_object(self, **kwargs: object) -> Mapping[str, object]: ...


class ObjectStoreWriteError(RuntimeError):
    """Closed, path-free result from the external object-write boundary."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        outcome: ObjectStoreWriteOutcome,
    ) -> None:
        if code not in {
            "OBJECT_STORE_REQUEST_INVALID",
            "OBJECT_STORE_SOURCE_UNSAFE",
            "OBJECT_STORE_SOURCE_LIMIT_EXCEEDED",
            "OBJECT_STORE_SOURCE_INTEGRITY_FAILED",
            "OBJECT_STORE_REMOTE_CONFLICT",
            "OBJECT_STORE_REMOTE_INTEGRITY_FAILED",
            "OBJECT_STORE_RESULT_INDETERMINATE",
        }:
            raise ValueError("object-store failure code is unsupported")
        self.code = code
        self.detail = detail
        self.outcome = outcome
        super().__init__(detail)


class ObjectStoreReadError(RuntimeError):
    """Closed, locator-free result from the external object-read boundary."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        outcome: ObjectStoreReadOutcome,
    ) -> None:
        if code not in {
            "OBJECT_STORE_READ_REQUEST_INVALID",
            "OBJECT_STORE_READ_TARGET_UNSAFE",
            "OBJECT_STORE_READ_LIMIT_EXCEEDED",
            "OBJECT_STORE_REMOTE_INTEGRITY_FAILED",
            "OBJECT_STORE_RESULT_INDETERMINATE",
        }:
            raise ValueError("object-store read failure code is unsupported")
        self.code = code
        self.detail = detail
        self.outcome = outcome
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class PendingObjectIntent:
    """Durable expected identity for one not-yet-claimed large object."""

    object_id: UUID
    content_hash: str
    byte_length: int
    media_type: str

    def __post_init__(self) -> None:
        if type(self.object_id) is not UUID:  # noqa: E721
            raise ValueError("pending object_id must be an exact UUID")
        if self.object_id.version != 4 or self.object_id.variant != RFC_4122:
            raise ValueError("pending object_id must use the random UUIDv4 scheme")
        if not _SHA256_PATTERN.fullmatch(self.content_hash):
            raise ValueError("pending content_hash must be a lowercase SHA-256")
        if type(self.byte_length) is not int or self.byte_length <= 0:  # noqa: E721
            raise ValueError("pending byte_length must be a positive integer")
        if type(self.media_type) is not str or not self.media_type.strip():  # noqa: E721
            raise ValueError("pending media_type must be non-empty text")

    @property
    def reference(self) -> BlobRef:
        return BlobRef(
            self.object_id,
            self.content_hash,
            self.byte_length,
            self.media_type,
        )


@dataclass(frozen=True, slots=True)
class ObjectStoreWriteLimits:
    """Explicit resource ceilings for one object-store effect."""

    max_object_bytes: int
    verification_chunk_bytes: int

    def __post_init__(self) -> None:
        if type(self.max_object_bytes) is not int or self.max_object_bytes <= 0:  # noqa: E721
            raise ValueError("max_object_bytes must be a positive integer")
        if (  # noqa: E721
            type(self.verification_chunk_bytes) is not int
            or self.verification_chunk_bytes <= 0
            or self.verification_chunk_bytes > self.max_object_bytes
        ):
            raise ValueError(
                "verification_chunk_bytes must be positive and not exceed max_object_bytes"
            )


@dataclass(frozen=True, slots=True)
class ObjectStoreReadLimits:
    """Explicit byte and chunk ceilings for one object-store materialization."""

    max_object_bytes: int
    transfer_chunk_bytes: int

    def __post_init__(self) -> None:
        if type(self.max_object_bytes) is not int or self.max_object_bytes <= 0:  # noqa: E721
            raise ValueError("max_object_bytes must be a positive integer")
        if (  # noqa: E721
            type(self.transfer_chunk_bytes) is not int
            or self.transfer_chunk_bytes <= 0
            or self.transfer_chunk_bytes > self.max_object_bytes
        ):
            raise ValueError(
                "transfer_chunk_bytes must be positive and not exceed max_object_bytes"
            )


@dataclass(frozen=True, slots=True)
class S3ObjectStoreConfig:
    """Private composition settings; none are copied into Kernel ``BlobRef`` values."""

    backend_id: str
    storage_region: str
    bucket: str
    key_prefix: str

    def __post_init__(self) -> None:
        if type(self.backend_id) is not str or not _SAFE_ID_PATTERN.fullmatch(  # noqa: E721
            self.backend_id
        ):
            raise ValueError("backend_id must be a safe stable identifier")
        if type(self.storage_region) is not str or not _SAFE_ID_PATTERN.fullmatch(  # noqa: E721
            self.storage_region
        ):
            raise ValueError("storage_region must be a safe stable identifier")
        if type(self.bucket) is not str or not self.bucket.strip():  # noqa: E721
            raise ValueError("bucket must be non-empty text")
        if (
            type(self.key_prefix) is not str  # noqa: E721
            or not _SAFE_PREFIX_PATTERN.fullmatch(self.key_prefix)
            or ".." in self.key_prefix.split("/")
        ):
            raise ValueError("key_prefix must be a safe relative object prefix")


@dataclass(frozen=True, slots=True)
class _PendingObjectTarget:
    """Internal exact destination persisted before an external write begins."""

    backend_id: str
    storage_region: str
    storage_locator: str
    strategy: str = OBJECT_STORE_WRITE_STRATEGY

    def __post_init__(self) -> None:
        if not _SAFE_ID_PATTERN.fullmatch(self.backend_id):
            raise ValueError("pending object backend_id is invalid")
        if not _SAFE_ID_PATTERN.fullmatch(self.storage_region):
            raise ValueError("pending object storage_region is invalid")
        if (
            not self.storage_locator
            or len(self.storage_locator) > 1024
            or self.storage_locator.startswith("/")
            or ".." in self.storage_locator.split("/")
        ):
            raise ValueError("pending object storage_locator is invalid")
        if self.strategy != OBJECT_STORE_WRITE_STRATEGY:
            raise ValueError("pending object write strategy is unsupported")


@dataclass(frozen=True, slots=True)
class _PendingObjectReservation:  # pyright: ignore[reportUnusedClass]
    """Database-issued CAS capability for one exact pending object intent."""

    intent: PendingObjectIntent
    target: _PendingObjectTarget
    job_id: UUID
    reservation_token: UUID
    expected_version: int
    _attestation: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.intent) is not PendingObjectIntent:  # noqa: E721
            raise ValueError("pending object reservation intent is invalid")
        if type(self.target) is not _PendingObjectTarget:  # noqa: E721
            raise ValueError("pending object reservation target is invalid")
        for field_name in ("job_id", "reservation_token"):
            if type(getattr(self, field_name)) is not UUID:  # noqa: E721
                raise ValueError(f"pending object reservation {field_name} is invalid")
        if type(self.expected_version) is not int or self.expected_version < 0:  # noqa: E721
            raise ValueError("pending object reservation version is invalid")
        if self._attestation is not _RESERVATION_ATTESTATION:
            raise ValueError("pending object reservation was not issued by the Store")

    @property
    def reference(self) -> BlobRef:
        return self.intent.reference

    @property
    def object_id(self) -> UUID:
        return self.intent.object_id

    @property
    def content_hash(self) -> str:
        return self.intent.content_hash

    @property
    def byte_length(self) -> int:
        return self.intent.byte_length

    @property
    def media_type(self) -> str:
        return self.intent.media_type


def _issue_pending_object_reservation(  # pyright: ignore[reportUnusedFunction]
    *,
    intent: PendingObjectIntent,
    target: _PendingObjectTarget,
    job_id: UUID,
    reservation_token: UUID,
    expected_version: int,
) -> _PendingObjectReservation:
    """Issue the package-private capability consumed by the object adapter."""

    return _PendingObjectReservation(
        intent=intent,
        target=target,
        job_id=job_id,
        reservation_token=reservation_token,
        expected_version=expected_version,
        _attestation=_RESERVATION_ATTESTATION,
    )


@dataclass(frozen=True, slots=True, repr=False)
class _S3ReadGrant:  # pyright: ignore[reportUnusedClass]
    """Store-issued authority to read one exact durable S3-compatible object.

    Its representation intentionally omits every private storage field.  Public
    callers must never receive this package-private capability.
    """

    reference: BlobRef
    backend_id: str
    storage_region: str
    storage_locator: str
    etag: str
    version_id: str | None
    write_strategy: str
    _attestation: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.reference) is not BlobRef:  # noqa: E721
            raise ValueError("S3 read grant requires an exact BlobRef")
        if not _SAFE_ID_PATTERN.fullmatch(self.backend_id):
            raise ValueError("S3 read grant backend_id is invalid")
        if not _SAFE_ID_PATTERN.fullmatch(self.storage_region):
            raise ValueError("S3 read grant storage_region is invalid")
        if (
            not self.storage_locator
            or len(self.storage_locator) > 1024
            or self.storage_locator.startswith("/")
            or ".." in self.storage_locator.split("/")
        ):
            raise ValueError("S3 read grant storage_locator is invalid")
        if type(self.etag) is not str or not self.etag.strip():  # noqa: E721
            raise ValueError("S3 read grant etag is invalid")
        if self.version_id is not None and (
            type(self.version_id) is not str or not self.version_id.strip()  # noqa: E721
        ):
            raise ValueError("S3 read grant version_id is invalid")
        if self.write_strategy != OBJECT_STORE_WRITE_STRATEGY:
            raise ValueError("S3 read grant write strategy is unsupported")
        if self._attestation is not _READ_GRANT_ATTESTATION:
            raise ValueError("S3 read grant was not issued by the Store")


def _issue_s3_read_grant(  # pyright: ignore[reportUnusedFunction]
    *,
    reference: BlobRef,
    backend_id: str,
    storage_region: str,
    storage_locator: str,
    etag: str,
    version_id: str | None,
    write_strategy: str,
) -> _S3ReadGrant:
    """Issue the package-private capability consumed by the read adapter."""

    return _S3ReadGrant(
        reference=reference,
        backend_id=backend_id,
        storage_region=storage_region,
        storage_locator=storage_locator,
        etag=etag,
        version_id=version_id,
        write_strategy=write_strategy,
        _attestation=_READ_GRANT_ATTESTATION,
    )


@dataclass(frozen=True, slots=True)
class _VerifiedPendingObject:
    """Internal metadata proven by an exact remote HEAD reconciliation.

    The storage locator is an opaque database input.  Runtime and public DTOs
    continue to receive only ``reference``.
    """

    reference: BlobRef
    backend_id: str
    storage_region: str
    storage_locator: str
    etag: str
    version_id: str | None
    reconciled_after_put_error: bool
    reservation_token: UUID
    reservation_version: int
    _verification_mac: bytes = field(repr=False, compare=False)
    strategy: str = OBJECT_STORE_WRITE_STRATEGY

    def __post_init__(self) -> None:
        if type(self.reference) is not BlobRef:  # noqa: E721
            raise ValueError("verified object requires an exact BlobRef")
        for name in ("backend_id", "storage_region", "storage_locator", "etag"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():  # noqa: E721
                raise ValueError(f"verified object {name} must be non-empty text")
        if self.version_id is not None and (
            type(self.version_id) is not str or not self.version_id.strip()  # noqa: E721
        ):
            raise ValueError("verified object version_id must be non-empty text when present")
        if type(self.reconciled_after_put_error) is not bool:  # noqa: E721
            raise ValueError("reconciled_after_put_error must be boolean")
        if type(self.reservation_token) is not UUID:  # noqa: E721
            raise ValueError("verified object reservation_token must be a UUID")
        if type(self.reservation_version) is not int or self.reservation_version < 0:  # noqa: E721
            raise ValueError("verified object reservation_version must be non-negative")
        if type(self._verification_mac) is not bytes or len(self._verification_mac) != 32:  # noqa: E721
            raise ValueError("verified object signature is malformed")
        if self.strategy != OBJECT_STORE_WRITE_STRATEGY:
            raise ValueError("verified object strategy is unsupported")


@dataclass(frozen=True, slots=True)
class _VerifiedObjectRead:
    """Adapter-sealed proof that exact bytes reached one private descriptor."""

    reference: BlobRef
    backend_id: str
    storage_region: str
    etag: str
    version_id: str | None
    write_strategy: str
    destination_identity: tuple[int, int, int, int, int, int, int, int]
    _verification_mac: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.reference) is not BlobRef:  # noqa: E721
            raise ValueError("verified read requires an exact BlobRef")
        if not _SAFE_ID_PATTERN.fullmatch(self.backend_id):
            raise ValueError("verified read backend_id is invalid")
        if not _SAFE_ID_PATTERN.fullmatch(self.storage_region):
            raise ValueError("verified read storage_region is invalid")
        if type(self.etag) is not str or not self.etag.strip():  # noqa: E721
            raise ValueError("verified read etag is invalid")
        if self.version_id is not None and (
            type(self.version_id) is not str or not self.version_id.strip()  # noqa: E721
        ):
            raise ValueError("verified read version_id is invalid")
        if self.write_strategy != OBJECT_STORE_WRITE_STRATEGY:
            raise ValueError("verified read write strategy is unsupported")
        if (
            type(self.destination_identity) is not tuple  # noqa: E721
            or len(self.destination_identity) != 8
            or any(type(value) is not int for value in self.destination_identity)  # noqa: E721
        ):
            raise ValueError("verified read destination identity is malformed")
        if type(self._verification_mac) is not bytes or len(self._verification_mac) != 32:  # noqa: E721
            raise ValueError("verified read signature is malformed")


@dataclass(frozen=True, slots=True)
class _OpenedSource:
    descriptor: int
    identity: tuple[int, int, int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class _HeadResult:
    state: Literal["exact", "missing", "mismatch", "unknown"]
    etag: str | None = None
    version_id: str | None = None


class S3PendingObjectStore:
    """Conditionally stream and independently verify one pending render object."""

    def __init__(self, client: S3ObjectClient, config: S3ObjectStoreConfig) -> None:
        if not callable(getattr(client, "put_object", None)) or not callable(
            getattr(client, "head_object", None)
        ):
            raise ValueError("object-store client does not implement the required S3 surface")
        if type(config) is not S3ObjectStoreConfig:  # noqa: E721
            raise ValueError("object-store config must be exact S3ObjectStoreConfig")
        self._client = client
        self._config = config
        self._verification_key = secrets.token_bytes(32)

    def target_for(self, intent: object) -> _PendingObjectTarget:
        """Describe the exact target that must be reserved before ``put_path``."""

        if type(intent) is not PendingObjectIntent:  # noqa: E721
            raise ObjectStoreWriteError(
                "OBJECT_STORE_REQUEST_INVALID",
                "object target requires an exact pending intent",
                outcome="denied",
            )
        return _PendingObjectTarget(
            backend_id=self._config.backend_id,
            storage_region=self._config.storage_region,
            storage_locator=self._object_key(intent.object_id),
        )

    def put_path(
        self,
        reservation: object,
        *,
        source_path: object,
        attempt_directory: object,
        limits: object,
    ) -> _VerifiedPendingObject:
        """Put one sealed attempt output or reconcile the same durable intent."""

        if (  # noqa: E721
            type(reservation) is not _PendingObjectReservation
            or not isinstance(source_path, Path)
            or not isinstance(attempt_directory, Path)
            or type(limits) is not ObjectStoreWriteLimits
        ):
            raise ObjectStoreWriteError(
                "OBJECT_STORE_REQUEST_INVALID",
                "large-object write requires exact intent, paths, and limits",
                outcome="denied",
            )
        intent = reservation.intent
        if reservation.target != self.target_for(intent):
            raise ObjectStoreWriteError(
                "OBJECT_STORE_REQUEST_INVALID",
                "object reservation does not belong to this configured adapter",
                outcome="denied",
            )
        if intent.byte_length > limits.max_object_bytes:
            raise ObjectStoreWriteError(
                "OBJECT_STORE_SOURCE_LIMIT_EXCEEDED",
                "render output exceeds the configured object byte limit",
                outcome="denied",
            )

        opened = _open_sealed_attempt_output(
            source_path,
            attempt_directory,
            intent,
        )
        descriptor = opened.descriptor
        key = reservation.target.storage_locator
        checksum = _checksum_header(intent.content_hash)
        try:
            initial_hash, initial_length = _hash_descriptor(
                descriptor,
                limits.verification_chunk_bytes,
            )
            if initial_hash != intent.content_hash or initial_length != intent.byte_length:
                raise ObjectStoreWriteError(
                    "OBJECT_STORE_SOURCE_INTEGRITY_FAILED",
                    "render output does not match its durable pending-object intent",
                    outcome="denied",
                )

            put_error: Exception | None = None
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                with os.fdopen(os.dup(descriptor), "rb", buffering=0) as body:
                    self._client.put_object(
                        Bucket=self._config.bucket,
                        Key=key,
                        Body=body,
                        ContentLength=intent.byte_length,
                        ContentType=intent.media_type,
                        ChecksumAlgorithm="SHA256",
                        ChecksumSHA256=checksum,
                        IfNoneMatch="*",
                        Metadata={
                            _OBJECT_ID_METADATA_KEY: str(intent.object_id),
                            _CONTENT_HASH_METADATA_KEY: intent.content_hash,
                        },
                    )
            except Exception as error:  # provider errors are reconciled, never exposed.
                put_error = error

            head = self._head_exact(intent, key, checksum)
            _revalidate_source(
                descriptor,
                opened.identity,
                intent,
                limits.verification_chunk_bytes,
            )
            if head.state == "exact":
                result = _VerifiedPendingObject(
                    reference=intent.reference,
                    backend_id=self._config.backend_id,
                    storage_region=self._config.storage_region,
                    storage_locator=key,
                    etag=cast(str, head.etag),
                    version_id=head.version_id,
                    reconciled_after_put_error=put_error is not None,
                    reservation_token=reservation.reservation_token,
                    reservation_version=reservation.expected_version,
                    _verification_mac=b"\x00" * 32,
                )
                return _seal_verified_object(self._verification_key, result)
            if head.state == "mismatch":
                code = (
                    "OBJECT_STORE_REMOTE_CONFLICT"
                    if put_error is not None
                    and _provider_error_code(put_error) in {"412", "PreconditionFailed"}
                    else "OBJECT_STORE_REMOTE_INTEGRITY_FAILED"
                )
                raise ObjectStoreWriteError(
                    code,
                    "the durable object locator is occupied by different or unverifiable bytes",
                    outcome="failed",
                )
            raise ObjectStoreWriteError(
                "OBJECT_STORE_RESULT_INDETERMINATE",
                "the object write cannot be proven committed or absent",
                outcome="indeterminate",
            )
        finally:
            os.close(descriptor)

    def materialize_to_descriptor(
        self,
        grant: object,
        *,
        destination_descriptor: object,
        limits: object,
    ) -> _VerifiedObjectRead:
        """Boundedly GET one exact granted object into a caller-owned descriptor.

        The caller retains ownership of the descriptor and any partial bytes.
        The adapter never accepts or returns a filesystem path.
        """

        if (  # noqa: E721
            type(grant) is not _S3ReadGrant
            or type(destination_descriptor) is not int
            or type(limits) is not ObjectStoreReadLimits
        ):
            raise ObjectStoreReadError(
                "OBJECT_STORE_READ_REQUEST_INVALID",
                "object read requires an exact Store grant, descriptor, and limits",
                outcome="denied",
            )
        if (
            grant.backend_id != self._config.backend_id
            or grant.storage_region != self._config.storage_region
            or grant.storage_locator != self._object_key(grant.reference.object_id)
            or grant.write_strategy != OBJECT_STORE_WRITE_STRATEGY
        ):
            raise ObjectStoreReadError(
                "OBJECT_STORE_READ_REQUEST_INVALID",
                "object read grant does not belong to this configured adapter",
                outcome="denied",
            )
        if grant.reference.byte_length > limits.max_object_bytes:
            raise ObjectStoreReadError(
                "OBJECT_STORE_READ_LIMIT_EXCEEDED",
                "object exceeds the configured read byte limit",
                outcome="denied",
            )
        descriptor = destination_descriptor
        initial_identity = _validate_read_destination(descriptor)
        head = self._head_exact_read(grant)
        if head == "missing" or head == "mismatch":
            raise ObjectStoreReadError(
                "OBJECT_STORE_REMOTE_INTEGRITY_FAILED",
                "the granted object is missing or does not match durable metadata",
                outcome="failed",
            )
        if head == "unknown":
            raise ObjectStoreReadError(
                "OBJECT_STORE_RESULT_INDETERMINATE",
                "the granted object cannot be reconciled with its provider",
                outcome="indeterminate",
            )

        get_object = getattr(self._client, "get_object", None)
        if not callable(get_object):
            raise ObjectStoreReadError(
                "OBJECT_STORE_RESULT_INDETERMINATE",
                "the configured provider cannot perform an exact object read",
                outcome="indeterminate",
            )
        request: dict[str, object] = {
            "Bucket": self._config.bucket,
            "Key": grant.storage_locator,
            "ChecksumMode": "ENABLED",
            "IfMatch": grant.etag,
        }
        if grant.version_id is not None:
            request["VersionId"] = grant.version_id
        try:
            response = get_object(**request)
        except Exception as error:
            if _provider_error_code(error) in {
                "404",
                "NoSuchKey",
                "NoSuchVersion",
                "NotFound",
                "PreconditionFailed",
                "412",
            }:
                raise ObjectStoreReadError(
                    "OBJECT_STORE_REMOTE_INTEGRITY_FAILED",
                    "the granted object disappeared or changed before it could be read",
                    outcome="failed",
                ) from None
            raise ObjectStoreReadError(
                "OBJECT_STORE_RESULT_INDETERMINATE",
                "the exact object read did not produce a known provider result",
                outcome="indeterminate",
            ) from None
        # Third-party clients can violate their type stub at runtime; keep this
        # trust-boundary check even though the local Protocol promises Mapping.
        if not isinstance(response, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
            _close_provider_response_body(response)
            raise ObjectStoreReadError(
                "OBJECT_STORE_RESULT_INDETERMINATE",
                "the exact object read returned an unknown provider result",
                outcome="indeterminate",
            )
        response_mapping = cast(Mapping[object, object], response)
        try:
            body = response_mapping.get("Body")
        except Exception:
            _close_provider_response_body(response_mapping)
            raise ObjectStoreReadError(
                "OBJECT_STORE_RESULT_INDETERMINATE",
                "the exact object read returned an unknown provider result",
                outcome="indeterminate",
            ) from None
        read = getattr(body, "read", None)
        close = getattr(body, "close", None)
        if not callable(read) or not callable(close):
            if callable(close):
                _close_provider_body(close)
            raise ObjectStoreReadError(
                "OBJECT_STORE_RESULT_INDETERMINATE",
                "the exact object read did not return a bounded response stream",
                outcome="indeterminate",
            )
        try:
            metadata_exact = _read_metadata_exact(response_mapping, grant)
        except Exception:
            _close_provider_body(close)
            raise ObjectStoreReadError(
                "OBJECT_STORE_RESULT_INDETERMINATE",
                "the exact object response metadata could not be inspected",
                outcome="indeterminate",
            ) from None
        if not metadata_exact:
            _close_provider_body(close)
            raise ObjectStoreReadError(
                "OBJECT_STORE_REMOTE_INTEGRITY_FAILED",
                "the object response does not match durable metadata",
                outcome="failed",
            )

        try:
            digest, byte_length = _stream_provider_body(
                read,
                descriptor,
                grant.reference.byte_length,
                limits.transfer_chunk_bytes,
            )
        except ObjectStoreReadError:
            _close_provider_body(close)
            raise
        except Exception:
            _close_provider_body(close)
            raise ObjectStoreReadError(
                "OBJECT_STORE_RESULT_INDETERMINATE",
                "the exact object response could not be completely consumed",
                outcome="indeterminate",
            ) from None
        try:
            close()
        except Exception:
            raise ObjectStoreReadError(
                "OBJECT_STORE_RESULT_INDETERMINATE",
                "the exact object response could not be cleanly released",
                outcome="indeterminate",
            ) from None
        if byte_length != grant.reference.byte_length or digest != grant.reference.content_hash:
            raise ObjectStoreReadError(
                "OBJECT_STORE_REMOTE_INTEGRITY_FAILED",
                "the object response bytes do not match the granted BlobRef",
                outcome="failed",
            )
        final_identity = _verify_read_destination(
            descriptor,
            initial_identity,
            grant.reference,
            limits.transfer_chunk_bytes,
        )
        verified = _VerifiedObjectRead(
            reference=grant.reference,
            backend_id=grant.backend_id,
            storage_region=grant.storage_region,
            etag=grant.etag,
            version_id=grant.version_id,
            write_strategy=grant.write_strategy,
            destination_identity=final_identity,
            _verification_mac=b"\x00" * 32,
        )
        return _seal_verified_read(self._verification_key, grant, verified)

    def _verify_materialized_read(self, grant: object, verified: object) -> bool:
        """Verify this adapter sealed every grant and result identity field."""

        if (  # noqa: E721
            type(grant) is not _S3ReadGrant
            or type(verified) is not _VerifiedObjectRead
            or verified.reference != grant.reference
            or verified.backend_id != grant.backend_id
            or verified.storage_region != grant.storage_region
            or verified.etag != grant.etag
            or verified.version_id != grant.version_id
            or verified.write_strategy != grant.write_strategy
        ):
            return False
        expected = hmac.digest(
            self._verification_key,
            _verified_read_payload(grant, verified),
            "sha256",
        )
        return hmac.compare_digest(
            verified._verification_mac,  # pyright: ignore[reportPrivateUsage]
            expected,
        )

    def _verify_pending_object(
        self,
        reservation: object,
        verified: object,
    ) -> bool:
        """Verify that this exact adapter signed every persisted result field."""

        if (  # noqa: E721
            type(reservation) is not _PendingObjectReservation
            or type(verified) is not _VerifiedPendingObject
            or verified.reference != reservation.reference
            or verified.backend_id != reservation.target.backend_id
            or verified.storage_region != reservation.target.storage_region
            or verified.storage_locator != reservation.target.storage_locator
            or verified.strategy != reservation.target.strategy
            or verified.reservation_token != reservation.reservation_token
            or verified.reservation_version != reservation.expected_version
        ):
            return False
        expected = hmac.digest(
            self._verification_key,
            _verified_object_payload(verified),
            "sha256",
        )
        return hmac.compare_digest(
            verified._verification_mac,  # pyright: ignore[reportPrivateUsage]
            expected,
        )

    def _head_exact_read(
        self,
        grant: _S3ReadGrant,
    ) -> Literal["exact", "missing", "mismatch", "unknown"]:
        request: dict[str, object] = {
            "Bucket": self._config.bucket,
            "Key": grant.storage_locator,
            "ChecksumMode": "ENABLED",
        }
        if grant.version_id is not None:
            request["VersionId"] = grant.version_id
        response: object
        try:
            response = self._client.head_object(**request)
        except Exception as error:
            if _provider_error_code(error) in {
                "404",
                "NoSuchKey",
                "NoSuchVersion",
                "NotFound",
            }:
                return "missing"
            return "unknown"
        # Provider responses remain untrusted even when the SDK type stub says
        # Mapping; malformed response objects must fail closed.
        if not isinstance(response, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
            return "unknown"
        try:
            metadata_exact = _read_metadata_exact(
                cast(Mapping[object, object], response),
                grant,
            )
        except Exception:
            return "unknown"
        if not metadata_exact:
            return "mismatch"
        return "exact"

    def _object_key(self, object_id: UUID) -> str:
        hexadecimal = object_id.hex
        return f"{self._config.key_prefix}/{hexadecimal[:2]}/{hexadecimal}"

    def _head_exact(
        self,
        intent: PendingObjectIntent,
        key: str,
        checksum: str,
    ) -> _HeadResult:
        try:
            response = self._client.head_object(
                Bucket=self._config.bucket,
                Key=key,
                ChecksumMode="ENABLED",
            )
        except Exception as error:
            if _provider_error_code(error) in {"404", "NoSuchKey", "NotFound"}:
                return _HeadResult("missing")
            return _HeadResult("unknown")
        metadata = response.get("Metadata")
        if not isinstance(metadata, Mapping):
            return _HeadResult("mismatch")
        metadata_mapping = cast(Mapping[object, object], metadata)
        normalized_metadata: dict[str, object] = {
            str(key).lower(): value for key, value in metadata_mapping.items()
        }
        etag = response.get("ETag")
        version_id = response.get("VersionId")
        if (
            type(response.get("ContentLength")) is not int  # noqa: E721
            or response.get("ContentLength") != intent.byte_length
            or response.get("ContentType") != intent.media_type
            or response.get("ChecksumSHA256") != checksum
            or normalized_metadata.get(_OBJECT_ID_METADATA_KEY) != str(intent.object_id)
            or normalized_metadata.get(_CONTENT_HASH_METADATA_KEY) != intent.content_hash
            or type(etag) is not str  # noqa: E721
            or not etag.strip()
            or (
                version_id is not None and (type(version_id) is not str or not version_id.strip())  # noqa: E721
            )
        ):
            return _HeadResult("mismatch")
        return _HeadResult("exact", etag, version_id)


def _read_metadata_exact(
    response: Mapping[object, object],
    grant: _S3ReadGrant,
) -> bool:
    metadata = response.get("Metadata")
    if not isinstance(metadata, Mapping):
        return False
    metadata_mapping = cast(Mapping[object, object], metadata)
    normalized_metadata = {str(key).lower(): value for key, value in metadata_mapping.items()}
    checksum = response.get("ChecksumSHA256")
    delete_marker = response.get("DeleteMarker")
    return (
        type(response.get("ContentLength")) is int  # noqa: E721
        and response.get("ContentLength") == grant.reference.byte_length
        and response.get("ContentType") == grant.reference.media_type
        and response.get("ETag") == grant.etag
        and response.get("VersionId") == grant.version_id
        and (delete_marker is None or delete_marker is False)
        and (checksum is None or checksum == _checksum_header(grant.reference.content_hash))
        and normalized_metadata.get(_OBJECT_ID_METADATA_KEY) == str(grant.reference.object_id)
        and normalized_metadata.get(_CONTENT_HASH_METADATA_KEY) == grant.reference.content_hash
    )


def _validate_read_destination(
    descriptor: int,
) -> tuple[int, int, int, int, int, int, int, int]:
    if _fcntl is None:
        raise ObjectStoreReadError(
            "OBJECT_STORE_READ_TARGET_UNSAFE",
            "object reads require POSIX descriptor integrity controls",
            outcome="denied",
        )
    try:
        status = os.fstat(descriptor)
        descriptor_flags = _fcntl.fcntl(descriptor, _fcntl.F_GETFL)
    except (OSError, ValueError):
        raise ObjectStoreReadError(
            "OBJECT_STORE_READ_TARGET_UNSAFE",
            "object read destination is not an available private descriptor",
            outcome="denied",
        ) from None
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or status.st_nlink != 1
        or stat.S_IMODE(status.st_mode) != 0o600
        or status.st_size != 0
        or descriptor_flags & os.O_ACCMODE != os.O_RDWR
        or descriptor_flags & os.O_APPEND
    ):
        raise ObjectStoreReadError(
            "OBJECT_STORE_READ_TARGET_UNSAFE",
            "object read destination is not an empty caller-owned private file",
            outcome="denied",
        )
    return _file_identity(status)


def _stream_provider_body(
    read: Callable[[int], object],
    descriptor: int,
    expected_byte_length: int,
    chunk_bytes: int,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    offset = 0
    while offset < expected_byte_length:
        requested = min(chunk_bytes, expected_byte_length - offset)
        try:
            chunk = read(requested)
        except Exception:
            raise ObjectStoreReadError(
                "OBJECT_STORE_RESULT_INDETERMINATE",
                "the exact object response stream failed during bounded reading",
                outcome="indeterminate",
            ) from None
        if type(chunk) is not bytes:  # noqa: E721
            raise ObjectStoreReadError(
                "OBJECT_STORE_RESULT_INDETERMINATE",
                "the exact object response returned an invalid stream chunk",
                outcome="indeterminate",
            )
        if not chunk:
            raise ObjectStoreReadError(
                "OBJECT_STORE_REMOTE_INTEGRITY_FAILED",
                "the exact object response ended before its declared byte length",
                outcome="failed",
            )
        if len(chunk) > requested:
            raise ObjectStoreReadError(
                "OBJECT_STORE_REMOTE_INTEGRITY_FAILED",
                "the exact object response violated its bounded chunk contract",
                outcome="failed",
            )
        digest.update(chunk)
        _write_destination_chunk(descriptor, chunk, offset)
        offset += len(chunk)
    try:
        trailing = read(1)
    except Exception:
        raise ObjectStoreReadError(
            "OBJECT_STORE_RESULT_INDETERMINATE",
            "the exact object response stream failed at its declared boundary",
            outcome="indeterminate",
        ) from None
    if type(trailing) is not bytes:  # noqa: E721
        raise ObjectStoreReadError(
            "OBJECT_STORE_RESULT_INDETERMINATE",
            "the exact object response returned an invalid terminal chunk",
            outcome="indeterminate",
        )
    if trailing:
        raise ObjectStoreReadError(
            "OBJECT_STORE_REMOTE_INTEGRITY_FAILED",
            "the exact object response exceeded its declared byte length",
            outcome="failed",
        )
    try:
        os.fsync(descriptor)
    except OSError:
        raise ObjectStoreReadError(
            "OBJECT_STORE_READ_TARGET_UNSAFE",
            "object read destination could not be durably synchronized",
            outcome="failed",
        ) from None
    return f"sha256:{digest.hexdigest()}", offset


def _write_destination_chunk(descriptor: int, chunk: bytes, offset: int) -> None:
    written = 0
    try:
        while written < len(chunk):
            count = os.pwrite(descriptor, chunk[written:], offset + written)
            if count <= 0:
                raise OSError("destination write made no progress")
            written += count
    except OSError:
        raise ObjectStoreReadError(
            "OBJECT_STORE_READ_TARGET_UNSAFE",
            "object read destination could not accept exact bytes",
            outcome="failed",
        ) from None


def _verify_read_destination(
    descriptor: int,
    initial_identity: tuple[int, int, int, int, int, int, int, int],
    reference: BlobRef,
    chunk_bytes: int,
) -> tuple[int, int, int, int, int, int, int, int]:
    try:
        before_hash = _file_identity(os.fstat(descriptor))
        digest = hashlib.sha256()
        offset = 0
        while offset < reference.byte_length:
            chunk = os.pread(
                descriptor,
                min(chunk_bytes, reference.byte_length - offset),
                offset,
            )
            if not chunk:
                raise OSError("destination ended before its declared length")
            digest.update(chunk)
            offset += len(chunk)
        trailing = os.pread(descriptor, 1, offset)
        after_hash = _file_identity(os.fstat(descriptor))
    except OSError:
        raise ObjectStoreReadError(
            "OBJECT_STORE_READ_TARGET_UNSAFE",
            "object read destination could not be independently verified",
            outcome="failed",
        ) from None
    same_private_file = (
        before_hash[0] == initial_identity[0]
        and before_hash[1] == initial_identity[1]
        and before_hash[5:] == initial_identity[5:]
        and before_hash[2] == reference.byte_length
    )
    if (
        not same_private_file
        or before_hash != after_hash
        or trailing
        or offset != reference.byte_length
        or f"sha256:{digest.hexdigest()}" != reference.content_hash
    ):
        raise ObjectStoreReadError(
            "OBJECT_STORE_READ_TARGET_UNSAFE",
            "object read destination changed or failed exact verification",
            outcome="failed",
        )
    return after_hash


def _close_provider_body(close: Callable[[], object]) -> None:
    try:
        close()
    except Exception:
        pass


def _close_provider_response_body(response: object) -> None:
    """Best-effort close for a malformed provider envelope.

    SDK type declarations are not an authority boundary.  A malformed runtime
    response may expose its body as an attribute or a mapping member; neither
    inspection nor close failure may escape the closed read result.
    """

    body: object | None = None
    try:
        body = getattr(response, "Body", None)
    except Exception:
        pass
    if body is None and isinstance(response, Mapping):
        response_mapping = cast(Mapping[object, object], response)
        try:
            body = response_mapping.get("Body")
        except Exception:
            pass
    try:
        close = getattr(body, "close", None)
    except Exception:
        return
    if callable(close):
        _close_provider_body(close)


def _open_sealed_attempt_output(
    source_path: Path,
    attempt_directory: Path,
    intent: PendingObjectIntent,
) -> _OpenedSource:
    if not source_path.is_absolute() or not attempt_directory.is_absolute():
        raise ObjectStoreWriteError(
            "OBJECT_STORE_SOURCE_UNSAFE",
            "render output and attempt directory must be absolute",
            outcome="denied",
        )
    if any(
        not hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "geteuid")
    ):
        raise ObjectStoreWriteError(
            "OBJECT_STORE_SOURCE_UNSAFE",
            "this host cannot provide the required no-follow owner checks",
            outcome="denied",
        )
    directory_descriptor: int | None = None
    source_descriptor: int | None = None
    unsafe = False
    try:
        resolved_directory = attempt_directory.resolve(strict=True)
        directory_status = attempt_directory.lstat()
        if (
            resolved_directory != attempt_directory
            or not stat.S_ISDIR(directory_status.st_mode)
            or stat.S_ISLNK(directory_status.st_mode)
            or directory_status.st_uid != os.geteuid()
            or directory_status.st_mode & 0o077
            or source_path.parent != attempt_directory
            or source_path.name in {"", ".", ".."}
        ):
            raise OSError("unsafe attempt output hierarchy")
        directory_descriptor = os.open(
            attempt_directory,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        pinned_directory_status = os.fstat(directory_descriptor)
        if (
            (pinned_directory_status.st_dev, pinned_directory_status.st_ino)
            != (directory_status.st_dev, directory_status.st_ino)
            or not stat.S_ISDIR(pinned_directory_status.st_mode)
            or pinned_directory_status.st_uid != os.geteuid()
            or pinned_directory_status.st_mode & 0o077
        ):
            raise OSError("attempt directory changed while it was pinned")
        source_descriptor = os.open(
            source_path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_descriptor,
        )
    except (OSError, RuntimeError, ValueError):
        unsafe = True
    finally:
        if directory_descriptor is not None:
            try:
                os.close(directory_descriptor)
            except OSError:
                unsafe = True
    if unsafe or source_descriptor is None:
        if source_descriptor is not None:
            try:
                os.close(source_descriptor)
            except OSError:
                pass
        raise ObjectStoreWriteError(
            "OBJECT_STORE_SOURCE_UNSAFE",
            "render output is not a safe lease-private file",
            outcome="denied",
        )
    source_status: os.stat_result | None = None
    try:
        source_status = os.fstat(source_descriptor)
    except OSError:
        pass
    if (
        source_status is None
        or not stat.S_ISREG(source_status.st_mode)
        or source_status.st_uid != os.geteuid()
        or source_status.st_nlink != 1
        or source_status.st_mode & 0o077
        or source_status.st_mode & 0o222
        or source_status.st_size != intent.byte_length
    ):
        try:
            os.close(source_descriptor)
        except OSError:
            pass
        raise ObjectStoreWriteError(
            "OBJECT_STORE_SOURCE_UNSAFE",
            "render output is not a sealed single-link regular file",
            outcome="denied",
        )
    return _OpenedSource(source_descriptor, _file_identity(source_status))


def _file_identity(
    status: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
        status.st_uid,
        status.st_nlink,
        stat.S_IMODE(status.st_mode),
    )


def _hash_descriptor(descriptor: int, chunk_bytes: int) -> tuple[str, int]:
    failure = False
    digest = hashlib.sha256()
    length = 0
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while chunk := os.read(descriptor, chunk_bytes):
            digest.update(chunk)
            length += len(chunk)
    except OSError:
        failure = True
    if failure:
        raise ObjectStoreWriteError(
            "OBJECT_STORE_SOURCE_INTEGRITY_FAILED",
            "render output could not be read for integrity verification",
            outcome="failed",
        )
    return f"sha256:{digest.hexdigest()}", length


def _revalidate_source(
    descriptor: int,
    initial_identity: tuple[int, int, int, int, int, int, int, int],
    intent: PendingObjectIntent,
    chunk_bytes: int,
) -> None:
    status_failure = False
    final_identity: tuple[int, int, int, int, int, int, int, int] | None = None
    try:
        final_identity = _file_identity(os.fstat(descriptor))
    except OSError:
        status_failure = True
    if status_failure or final_identity is None:
        raise ObjectStoreWriteError(
            "OBJECT_STORE_SOURCE_INTEGRITY_FAILED",
            "render output identity became unavailable during persistence",
            outcome="failed",
        )
    final_hash, final_length = _hash_descriptor(descriptor, chunk_bytes)
    if (
        final_identity != initial_identity
        or final_hash != intent.content_hash
        or final_length != intent.byte_length
    ):
        raise ObjectStoreWriteError(
            "OBJECT_STORE_SOURCE_INTEGRITY_FAILED",
            "render output changed during object-store persistence",
            outcome="failed",
        )


def _checksum_header(content_hash: str) -> str:
    return base64.b64encode(bytes.fromhex(content_hash.removeprefix("sha256:"))).decode("ascii")


def _provider_error_code(error: Exception) -> str | None:
    response = cast(object, getattr(error, "response", None))
    if not isinstance(response, Mapping):
        return None
    response_mapping = cast(Mapping[object, object], response)
    provider_error = response_mapping.get("Error")
    if not isinstance(provider_error, Mapping):
        return None
    provider_error_mapping = cast(Mapping[object, object], provider_error)
    code = provider_error_mapping.get("Code")
    if isinstance(code, (str, int)):
        return str(code)
    return None


def _verified_object_payload(verified: _VerifiedPendingObject) -> bytes:
    return json.dumps(
        [
            str(verified.reference.object_id),
            verified.reference.content_hash,
            verified.reference.byte_length,
            verified.reference.media_type,
            verified.backend_id,
            verified.storage_region,
            verified.storage_locator,
            verified.etag,
            verified.version_id,
            verified.reconciled_after_put_error,
            str(verified.reservation_token),
            verified.reservation_version,
            verified.strategy,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _seal_verified_object(
    key: bytes,
    verified: _VerifiedPendingObject,
) -> _VerifiedPendingObject:
    signature = hmac.digest(key, _verified_object_payload(verified), "sha256")
    return dataclass_replace(verified, _verification_mac=signature)


def _verified_read_payload(
    grant: _S3ReadGrant,
    verified: _VerifiedObjectRead,
) -> bytes:
    return json.dumps(
        [
            str(verified.reference.object_id),
            verified.reference.content_hash,
            verified.reference.byte_length,
            verified.reference.media_type,
            verified.backend_id,
            verified.storage_region,
            grant.storage_locator,
            verified.etag,
            verified.version_id,
            verified.write_strategy,
            list(verified.destination_identity),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _seal_verified_read(
    key: bytes,
    grant: _S3ReadGrant,
    verified: _VerifiedObjectRead,
) -> _VerifiedObjectRead:
    signature = hmac.digest(key, _verified_read_payload(grant, verified), "sha256")
    return dataclass_replace(verified, _verification_mac=signature)


__all__ = [
    "OBJECT_STORE_WRITE_SCHEMA_VERSION",
    "OBJECT_STORE_WRITE_STRATEGY",
    "ObjectStoreReadError",
    "ObjectStoreReadLimits",
    "ObjectStoreWriteError",
    "ObjectStoreWriteLimits",
    "PendingObjectIntent",
    "S3ObjectClient",
    "S3ObjectStoreConfig",
    "S3PendingObjectStore",
]
