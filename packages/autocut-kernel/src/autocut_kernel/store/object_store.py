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
from collections.abc import Mapping
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Final, Literal, Protocol, cast
from uuid import RFC_4122, UUID

from .models import BlobRef

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

ObjectStoreWriteOutcome = Literal["denied", "failed", "indeterminate"]


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


__all__ = [
    "OBJECT_STORE_WRITE_SCHEMA_VERSION",
    "OBJECT_STORE_WRITE_STRATEGY",
    "ObjectStoreWriteError",
    "ObjectStoreWriteLimits",
    "PendingObjectIntent",
    "S3ObjectClient",
    "S3ObjectStoreConfig",
    "S3PendingObjectStore",
]
