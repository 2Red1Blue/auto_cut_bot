"""Closed PostgreSQL persistence adapter for the local Pipeline MVP.

The adapter accepts only semantic command operations.  It does not expose a
cursor, generic row writer, legacy ArtifactBus object, or an execution escape
hatch to callers.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar, cast
from uuid import UUID, uuid4

from psycopg import DatabaseError, InterfaceError

try:  # ``fcntl`` is absent on Windows, where semantic-only execution is valid.
    import fcntl as _fcntl
except ModuleNotFoundError:  # pragma: no cover - exercised by Windows runtime.
    _fcntl = None

from ..contracts.compiler.canonical import canonical_json_bytes
from ..media import (
    CalibrationRecordError,
    Stage4PredecessorError,
    decode_timed_speech_profile_registry_entry,
)
from ..media.calibration_record import (
    CALIBRATION_VALIDATOR_COMMAND,
    CalibrationRecordArtifactMember,
    CalibrationRecordArtifactSet,
    CalibrationRecordScope,
    decode_calibration_record_member_payload,
    decode_calibration_record_payload,
    decode_calibration_validation_receipt_payload,
    verify_calibration_record_artifact_set,
)
from ..media.runtime_measurement_identity import RuntimeMeasurementIdentity
from ..media.timing_compatibility import decode_timing_compatibility_profile
from ..registry.timed_speech import (
    AUTHORITY_BOOTSTRAP_JOB,
    BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND,
    TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE,
    TIMED_SPEECH_PROFILE_REGISTRY_SCOPE,
    AuthorityRegistrySnapshot,
    BootstrappedTimedSpeechProfile,
    TimedSpeechProfileKey,
    TimedSpeechRegistryError,
)
from ..source_manifest import (
    SourceManifestDecodeError,
    SourceOperationGrant,
    decode_source_manifest,
)
from ..vlm import (
    GENERATION_PROVIDER_LEASE_SECONDS,
    VlmParsePolicy,
    VlmRequestIdentity,
    VlmValidationError,
    WindowManifest,
    WindowManifestSet,
    decode_vlm_semantic_pack,
    parse_vlm_response,
)
from .errors import (
    BlobIntegrityError,
    BlobUnavailableError,
    CommandStateError,
    GenerationAttemptStateError,
    IdempotencyConflictError,
    JobProfileMismatchError,
    MediaEvidenceIntegrityError,
    MediaEvidenceUnavailableError,
    MediaOutputsIntegrityError,
    MediaOutputsUnavailableError,
    PersistenceConflictError,
    RecipeIntegrityError,
    RecipeUnavailableError,
    RuntimeCalibrationIdentityMismatchError,
    RuntimeStoreError,
    SemanticInputIntegrityError,
    SemanticInputUnavailableError,
    StaleHeadError,
    StoreConcurrencyError,
    StoreValidationError,
)
from .media_recovery_frontier import (
    MediaRecoveryEntry,
    MediaRecoveryFrontier,
    MediaRecoveryFrontierError,
    MediaRecoveryPlan,
)
from .models import (
    PRODUCTION_RECIPE_COMMAND_NAME,
    PRODUCTION_RENDER_COMMAND_NAME,
    SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME,
    SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME,
    VLM_BATCH_FINALIZER_COMMAND_NAME,
    VLM_BATCH_FINALIZER_STRATEGY_VERSION,
    VLM_BATCH_IDEMPOTENCY_PREFIX,
    VLM_REQUEST_IDENTITY_FIELDS,
    ArtifactMember,
    ArtifactScope,
    BlobRef,
    CalibrationValidationBinding,
    CommandClaim,
    CommandExecutionKind,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    CommittedArtifactMemberReference,
    CommittedSemanticInputs,
    CommittedSemanticInputsRequest,
    CommittedV4InspectionInput,
    CommittedV4SemanticChildInspection,
    CommittedVlmInputReference,
    CommittedVlmSemanticInput,
    GenerationAttempt,
    Job,
    JobProfile,
    MaterializationError,
    MaterializationLimits,
    MediaEvidenceReference,
    PersistedCalibrationRecordAnchor,
    PersistedCommittedArtifactMember,
    PersistedCommittedArtifactSet,
    PersistedMediaEvidence,
    PersistedMediaOutputs,
    PersistedRecipe,
    PersistedRuntimeCalibrationCapability,
    PersistedShadowCalibrationMeasurement,
    PersistedVlmGenerationChild,
    PersistedVlmSemanticPack,
    PersistedVlmSemanticPackV4,
    PersistedWholeSeriesSourceManifest,
    ProductionRenderAttempt,
    ProductionRenderLease,
    ProductionRenderQcAttempt,
    ProductionRenderQcLease,
    RecipeReference,
    ShadowLocalMeasurementAttempt,
    ShadowLocalMeasurementAttemptState,
    ShadowLocalMeasurementMember,
    ShadowLocalMeasurementMemberLease,
    ShadowLocalMeasurementMemberPlan,
    ShadowLocalMeasurementMemberState,
    ShadowLocalMeasurementNotStartedProof,
    ShadowLocalMeasurementPlan,
    ShadowLocalMeasurementRecoveryLease,
    ShadowLocalMeasurementRetryAuthorization,
    ShadowLocalMeasurementStagedResponse,
    ShadowMeasurementAttempt,
    ShadowMeasurementAttemptState,
    ShadowMeasurementMember,
    ShadowMeasurementMemberLease,
    ShadowMeasurementMemberState,
    ShadowMeasurementPlan,
    ShadowMeasurementRecoveryLease,
    ShadowMeasurementRetryAuthorization,
    ShadowMeasurementStagedResponse,
    ShadowMeasurementTerminalDenialRequest,
    ShadowMeasurementTerminalDenialResult,
    SourceWindowIdentity,
    VerifiedMaterializedBlob,
    VlmBatchRequestPolicy,
    VlmRequestRecordReference,
    VlmSemanticPackReference,
    VlmSemanticPackSetChild,
    WholeSeriesSourceManifestReference,
    artifact_set_hash,
    canonical_payload_hash,
    canonical_recipe_scope,
)
from .object_store import (
    ObjectStoreReadError,
    ObjectStoreReadLimits,
    PendingObjectIntent,
    S3PendingObjectStore,
    _issue_pending_object_reservation,  # pyright: ignore[reportPrivateUsage]
    _issue_s3_read_grant,  # pyright: ignore[reportPrivateUsage]
    _PendingObjectReservation,  # pyright: ignore[reportPrivateUsage]
    _PendingObjectTarget,  # pyright: ignore[reportPrivateUsage]
    _VerifiedPendingObject,  # pyright: ignore[reportPrivateUsage]
)
from .shadow_local_measurement_artifacts import (
    CommittedShadowLocalMeasurement,
    compile_shadow_local_measurement_artifacts,
    validate_shadow_local_measurement_artifact_metadata,
)

if TYPE_CHECKING:
    from ..rendering.production_ffmpeg_renderer import ProductionRenderAttemptFacts
from .source_reuse import (
    SOURCE_REUSE_BINDING_ARTIFACT_TYPE,
    SOURCE_REUSE_BINDING_LOGICAL_ID,
    SOURCE_REUSE_BINDING_SCHEMA_VERSION,
    SOURCE_REUSE_COMMAND_NAME,
    SourceReuseBinding,
)
from .terminal_receipts import PersistedTerminalCommandReceipt
from .vlm_v4 import (
    VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4,
    batch_version_fields,
    generation_semantic_version,
    require_batch_child_version,
    verify_v4_semantic_pack,
)


class DbCursor(Protocol):
    rowcount: int

    def execute(self, query: str, params: tuple[object, ...] = ()) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...

    def close(self) -> None: ...


class DbConnection(Protocol):
    def cursor(self) -> DbCursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


_Result = TypeVar("_Result")

_MATERIALIZATION_RESERVATION_DIRECTORY = ".autocut-media-reservations"
_MATERIALIZATION_RESERVATION_LOCK = ".autocut-media-reservations.lock"
_MATERIALIZATION_QUOTA_CONFIGURATION = ".autocut-media-quota-bytes"
_MATERIALIZATION_DIRECTORY_PREFIX = ".autocut-media-"
SHADOW_MEASUREMENT_LEASE_SECONDS = 60


def _acquire_materialization_ledger_lock(lock_fd: int) -> None:
    """Take the cross-process POSIX lock required by physical media staging.

    A process-local fallback would make quota accounting unsafe whenever two
    workers share a staging root.  Windows can still run the semantic-only
    VLM pipeline because it never materializes physical media; a request that
    does require physical materialization is rejected clearly and before any
    reservation is written.
    """

    if _fcntl is None:
        raise MaterializationError(
            "MEDIA_MATERIALIZATION_INFRASTRUCTURE_FAILED",
            "physical media materialization requires POSIX advisory file locks",
            outcome="failed",
        )
    _fcntl.flock(lock_fd, _fcntl.LOCK_EX)


def _release_materialization_ledger_lock(lock_fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(lock_fd, _fcntl.LOCK_UN)


def validate_materialization_staging_root(root: Path) -> Path:
    """Create and verify the one private root used for verified media leases.

    Composition calls this before registering any paid provider or worker.  The
    store repeats it at use time so a later permission or symlink change cannot
    turn a previously admitted root into an unsafe materialization target.
    """

    if not root.is_absolute():
        raise StoreValidationError("materialization_staging_root must be absolute")
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_stat = os.stat(root, follow_symlinks=False)
    except OSError as error:
        raise StoreValidationError("materialization_staging_root is unavailable") from error
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or stat.S_IMODE(root_stat.st_mode) != 0o700
        or root_stat.st_uid != os.geteuid()
    ):
        raise StoreValidationError("materialization_staging_root must be a private 0700 directory")
    return root


@dataclass(slots=True)
class _MaterializationQuotaLease:
    """One filesystem-backed reservation, visible to every local worker process."""

    root: Path
    reservation_id: str
    directory: Path
    byte_length: int
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            root_fd, lock_fd, reservations_fd = _open_materialization_ledger(self.root)
            try:
                _acquire_materialization_ledger_lock(lock_fd)
                try:
                    os.unlink(f"{self.reservation_id}.lease", dir_fd=reservations_fd)
                except FileNotFoundError:
                    # Another worker may have safely reclaimed this entry only
                    # after observing that our private directory was removed.
                    pass
                os.fsync(reservations_fd)
            finally:
                _release_materialization_ledger_lock(lock_fd)
                os.close(reservations_fd)
                os.close(lock_fd)
                os.close(root_fd)
        except OSError as error:
            self._released = False
            raise MaterializationError(
                "MEDIA_MATERIALIZATION_INFRASTRUCTURE_FAILED",
                "private media staging cleanup failed",
                outcome="failed",
            ) from error


def _open_materialization_ledger(root: Path) -> tuple[int, int, int]:
    """Open only no-follow descriptors for the root lock and reservation directory."""

    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.mkdir(_MATERIALIZATION_RESERVATION_DIRECTORY, 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        lock_fd = os.open(
            _MATERIALIZATION_RESERVATION_LOCK,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
        try:
            reservations_fd = os.open(
                _MATERIALIZATION_RESERVATION_DIRECTORY,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        except Exception:
            os.close(lock_fd)
            raise
    except Exception:
        os.close(root_fd)
        raise
    return root_fd, lock_fd, reservations_fd


def _reservation_bytes(reservations_fd: int, name: str) -> int:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=reservations_fd)
    try:
        raw = os.read(descriptor, 64)
        if os.read(descriptor, 1):
            raise ValueError("reservation record is oversized")
    finally:
        os.close(descriptor)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("reservation record is not ASCII") from error
    if not text.endswith("\n") or not text[:-1].isdecimal():
        raise ValueError("reservation record is malformed")
    value = int(text[:-1])
    if value <= 0:
        raise ValueError("reservation record has an invalid byte length")
    return value


def _pin_materialization_quota(root_fd: int, limit: int) -> None:
    """Bind one root to one quota under the same lock as reservations.

    The capacity ledger is shared by all local workers.  Letting each worker
    supply a different quota would make its arithmetic non-composable, so the
    first safely configured worker writes an immutable root-local record and
    every subsequent worker must match it exactly.
    """

    expected = f"{limit}\n".encode("ascii")
    created = False
    try:
        descriptor = os.open(
            _MATERIALIZATION_QUOTA_CONFIGURATION,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
    except FileNotFoundError:
        descriptor = os.open(
            _MATERIALIZATION_QUOTA_CONFIGURATION,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
        created = True
    try:
        record_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(record_stat.st_mode)
            or record_stat.st_nlink != 1
            or stat.S_IMODE(record_stat.st_mode) != 0o600
            or record_stat.st_uid != os.geteuid()
        ):
            raise OSError("materialization quota configuration is unsafe")
        if created:
            os.write(descriptor, expected)
            os.fsync(descriptor)
            return
        actual = os.read(descriptor, len(expected) + 1)
        if os.read(descriptor, 1) or actual != expected:
            raise MaterializationError(
                "MEDIA_MATERIALIZATION_QUOTA_CONFIGURATION_MISMATCH",
                "private media staging quota does not match the root configuration",
                outcome="failed",
            )
    finally:
        os.close(descriptor)


def _reserve_materialization_quota(
    root: Path,
    byte_length: int,
    limit: int,
) -> _MaterializationQuotaLease:
    """Atomically reserve capacity and make a private owner directory.

    Entries whose private directory is already absent are safe stale orphans:
    they are removed while holding the cross-process lock.  Any surviving
    entry remains charged, so a crashed process can never lead to overcommit.
    """

    reservation_id = uuid4().hex
    directory_name = _MATERIALIZATION_DIRECTORY_PREFIX + reservation_id
    root_fd, lock_fd, reservations_fd = _open_materialization_ledger(root)
    directory_created = False
    reservation_created = False
    try:
        _acquire_materialization_ledger_lock(lock_fd)
        _pin_materialization_quota(root_fd, limit)
        used = 0
        for name in os.listdir(reservations_fd):
            if not name.endswith(".lease"):
                raise OSError("materialization reservation directory has an unexpected entry")
            entry_id = name.removesuffix(".lease")
            if len(entry_id) != 32 or any(character not in "0123456789abcdef" for character in entry_id):
                raise OSError("materialization reservation identifier is malformed")
            try:
                directory_stat = os.stat(
                    _MATERIALIZATION_DIRECTORY_PREFIX + entry_id,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                os.unlink(name, dir_fd=reservations_fd)
                continue
            if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_mode & 0o077:
                raise OSError("materialization reservation directory is unsafe")
            used += _reservation_bytes(reservations_fd, name)
        if byte_length > limit or used + byte_length > limit:
            raise MaterializationError(
                "MEDIA_MATERIALIZATION_CAPACITY_BUSY",
                "private media staging capacity is unavailable",
                outcome="failed",
            )
        os.mkdir(directory_name, 0o700, dir_fd=root_fd)
        directory_created = True
        descriptor = os.open(
            f"{reservation_id}.lease",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=reservations_fd,
        )
        reservation_created = True
        try:
            os.write(descriptor, f"{byte_length}\n".encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(reservations_fd)
    except Exception:
        if reservation_created:
            try:
                os.unlink(f"{reservation_id}.lease", dir_fd=reservations_fd)
            except OSError:
                pass
        if directory_created:
            try:
                os.rmdir(directory_name, dir_fd=root_fd)
            except OSError:
                pass
        raise
    finally:
        _release_materialization_ledger_lock(lock_fd)
        os.close(reservations_fd)
        os.close(lock_fd)
        os.close(root_fd)
    return _MaterializationQuotaLease(
        root=root,
        reservation_id=reservation_id,
        directory=root / directory_name,
        byte_length=byte_length,
    )


@dataclass(slots=True)
class _VerifiedMaterializedBlob:
    reference: BlobRef
    path: Path
    _directory: Path
    _quota_lease: _MaterializationQuotaLease
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            directory_fd = os.open(
                self._directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                os.unlink("source.mp4", dir_fd=directory_fd)
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            os.rmdir(self._directory)
        except OSError as error:
            raise MaterializationError(
                "MEDIA_MATERIALIZATION_INFRASTRUCTURE_FAILED",
                "private media staging cleanup failed",
                outcome="failed",
            ) from error
        self._quota_lease.release()


@dataclass(frozen=True, slots=True)
class _ClaimedBlobMaterializationSource:
    """Exact durable storage identity behind one same-Job ``BlobRef`` claim."""

    reference: BlobRef
    storage_kind: Literal["postgres_inline", "s3_compatible"]
    backend_id: str | None
    storage_region: str | None
    storage_locator: str | None
    etag: str | None
    version_id: str | None
    write_strategy: str | None


@dataclass(frozen=True, slots=True)
class _ProductionRenderAttemptRecord:
    """Private persisted render state including the current fencing secret."""

    attempt: ProductionRenderAttempt
    lease_token: UUID | None

    def __post_init__(self) -> None:
        if type(self.attempt) is not ProductionRenderAttempt:  # noqa: E721
            raise RuntimeStoreError("production render record requires an exact attempt")
        if self.attempt.state == "rendering":
            if type(self.lease_token) is not UUID:  # noqa: E721
                raise RuntimeStoreError("rendering production record lost its lease token")
        elif self.lease_token is not None:
            raise RuntimeStoreError("non-rendering production record retained a lease token")


@dataclass(frozen=True, slots=True)
class _ProductionRenderQcAttemptRecord:
    """Private persisted QC state including the current fencing secret."""

    attempt: ProductionRenderQcAttempt
    lease_token: UUID | None

    def __post_init__(self) -> None:
        if type(self.attempt) is not ProductionRenderQcAttempt:  # noqa: E721
            raise RuntimeStoreError("production render QC record requires an exact attempt")
        if self.attempt.state == "scanning":
            if type(self.lease_token) is not UUID:  # noqa: E721
                raise RuntimeStoreError("scanning production render QC record lost its lease token")
        elif self.lease_token is not None:
            raise RuntimeStoreError(
                "non-scanning production render QC record retained a lease token"
            )


def _text(value: object) -> str:
    """Normalize PostgreSQL text values returned in either wire format."""
    return value.decode() if isinstance(value, bytes) else str(value)


def _strict_json_object(value: str, field_name: str) -> dict[str, object]:
    """Parse one finite JSON object and reject duplicate keys at every depth."""

    def closed_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, member in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = member
        return result

    try:
        parsed: object = json.loads(
            value,
            object_pairs_hook=closed_pairs,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {constant}")
            ),
        )
    except (TypeError, ValueError) as error:
        raise StoreValidationError(f"{field_name} must contain strict JSON") from error
    if type(parsed) is not dict:  # noqa: E721
        raise StoreValidationError(f"{field_name} must contain a JSON object")
    return cast(dict[str, object], parsed)


def _canonical_db_json(value: str) -> str:
    """Normalize PostgreSQL jsonb text before handing it to strict model records."""

    return json.dumps(
        _strict_json_object(value, "PostgreSQL JSON"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_production_render_facts_json(
    facts: ProductionRenderAttemptFacts,
) -> str:
    """Encode render facts exactly as their domain canonical hash expects."""

    return json.dumps(
        facts.to_mapping(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_production_render_facts(
    value: str,
    expected_sha256: str,
) -> ProductionRenderAttemptFacts:
    """Rebuild one closed facts record and reject non-canonical durable text."""

    from ..rendering.production_ffmpeg_renderer import (
        PRODUCTION_RENDER_ATTEMPT_SCHEMA_VERSION,
        PRODUCTION_RENDER_EXECUTION_SCHEMA_VERSION,
        ProductionFfmpegIdentity,
        ProductionRenderAttemptFacts,
    )

    def exact_mapping(
        candidate: object,
        keys: frozenset[str],
        field_name: str,
    ) -> dict[str, object]:
        if type(candidate) is not dict:  # noqa: E721
            raise ValueError(f"{field_name} does not use its closed schema")
        unknown_mapping = cast(dict[object, object], candidate)
        if frozenset(unknown_mapping) != keys:
            raise ValueError(f"{field_name} does not use its closed schema")
        return cast(dict[str, object], candidate)

    try:
        mapping = exact_mapping(
            _strict_json_object(value, "production render facts"),
            frozenset(
                {
                    "schema_version",
                    "execution_schema_version",
                    "attempt_id",
                    "job",
                    "story_id",
                    "recipe_sha256",
                    "plan_sha256",
                    "profile_sha256",
                    "execution_limits_sha256",
                    "input_authority_sha256",
                    "input_count",
                    "segment_count",
                    "ffmpeg",
                    "stderr_sha256",
                    "output",
                }
            ),
            "production render facts",
        )
        if (
            mapping["schema_version"] != PRODUCTION_RENDER_ATTEMPT_SCHEMA_VERSION
            or mapping["execution_schema_version"]
            != PRODUCTION_RENDER_EXECUTION_SCHEMA_VERSION
        ):
            raise ValueError("production render facts schema version is unsupported")
        job = exact_mapping(
            mapping["job"],
            frozenset({"job_key", "profile"}),
            "production render facts job",
        )
        profile = job["profile"]
        if profile not in ("test", "shadow", "production", "authority"):
            raise ValueError("production render facts Job profile is unsupported")
        ffmpeg = exact_mapping(
            mapping["ffmpeg"],
            frozenset(
                {
                    "executable_sha256",
                    "executable_byte_length",
                    "version_output_sha256",
                }
            ),
            "production render facts ffmpeg",
        )
        output = exact_mapping(
            mapping["output"],
            frozenset({"content_hash", "byte_length", "media_type"}),
            "production render facts output",
        )
        facts = ProductionRenderAttemptFacts(
            attempt_id=UUID(str(mapping["attempt_id"])),
            job=Job(str(job["job_key"]), profile),
            story_id=str(mapping["story_id"]),
            recipe_sha256=str(mapping["recipe_sha256"]),
            plan_sha256=str(mapping["plan_sha256"]),
            profile_sha256=str(mapping["profile_sha256"]),
            execution_limits_sha256=str(mapping["execution_limits_sha256"]),
            input_authority_sha256=str(mapping["input_authority_sha256"]),
            input_count=cast(int, mapping["input_count"]),
            segment_count=cast(int, mapping["segment_count"]),
            ffmpeg=ProductionFfmpegIdentity(
                executable_sha256=str(ffmpeg["executable_sha256"]),
                executable_byte_length=cast(int, ffmpeg["executable_byte_length"]),
                version_output_sha256=str(ffmpeg["version_output_sha256"]),
            ),
            stderr_sha256=str(mapping["stderr_sha256"]),
            output_sha256=str(output["content_hash"]),
            output_byte_length=cast(int, output["byte_length"]),
            output_media_type=str(output["media_type"]),
        )
        if (
            _canonical_production_render_facts_json(facts) != value
            or facts.canonical_hash != expected_sha256
        ):
            raise ValueError("production render facts JSON/hash is not canonical")
        return facts
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeStoreError("persisted production render facts are invalid") from error


def _canonical_media_db_json(value: str) -> str:
    """Restore JSONB text to the media-domain canonical representation.

    PostgreSQL JSONB intentionally erases escaping choices.  Local cases,
    requests, projected evidence, and BUSY proofs are media-domain values, so
    their frozen representation uses ASCII escapes rather than Store payload
    canonicalization's Unicode-preserving form.
    """

    return json.dumps(
        _strict_json_object(value, "PostgreSQL media JSON"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _shadow_blob_mapping(reference: BlobRef) -> dict[str, object]:
    return {
        "byte_length": reference.byte_length,
        "content_hash": reference.content_hash,
        "media_type": reference.media_type,
        "object_id": str(reference.object_id),
    }


def _shadow_artifact(
    scope: ArtifactScope, artifact_type: str, logical_id: str, payload: Mapping[str, object]
) -> ArtifactMember:
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return ArtifactMember(
        artifact_type=artifact_type,
        logical_id=logical_id,
        revision=1,
        scope=scope,
        content_hash=canonical_payload_hash(payload_json),
        payload_json=payload_json,
    )


def _shadow_artifact_set_hash(artifacts: tuple[ArtifactMember, ...]) -> str:
    canonical_members = [
        {
            "artifact_type": artifact.artifact_type,
            "content_hash": artifact.content_hash,
            "logical_id": artifact.logical_id,
            "payload_json": json.loads(artifact.payload_json),
            "revision": artifact.revision,
            "scope": {
                "key": artifact.scope.key,
                "kind": artifact.scope.kind,
                "namespace": artifact.scope.namespace,
            },
        }
        for artifact in artifacts
    ]
    encoded = json.dumps(
        canonical_members, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _shadow_member_bound_aggregate(
    members: tuple[ShadowMeasurementMember, ...], producer: str
) -> dict[str, object]:
    """Rebuild the command's member-bound summary from the staged projections."""

    measurements: list[dict[str, object]] = []
    for member in members:
        if member.projection_json is None:
            raise CommandStateError("shadow aggregate requires staged projections")
        projection = _strict_json_object(member.projection_json, "shadow staged projection")
        summary = projection.get("summary")
        if not isinstance(summary, dict):
            raise StoreValidationError("shadow staged projection summary is invalid")
        summary_mapping = cast(dict[str, object], summary)
        measurement = summary_mapping.get(producer)
        if not isinstance(measurement, dict):
            raise StoreValidationError("shadow staged projection producer summary is invalid")
        measurements.append(cast(dict[str, object], measurement))
    first = measurements[0]
    required = {
        "absolute_maximum_tick",
        "clock_id",
        "early_maximum_tick",
        "inference_kind",
        "late_maximum_tick",
        "matches",
        "producer",
        "producer_id",
        "time_base",
    }
    if not required.issubset(first):
        raise StoreValidationError("shadow staged producer summary is incomplete")
    for measurement in measurements:
        if not required.issubset(measurement) or any(
            measurement[name] != first[name]
            for name in ("clock_id", "inference_kind", "producer", "producer_id", "time_base")
        ):
            raise StoreValidationError("shadow staged producer summaries drift")
        if not isinstance(measurement["matches"], list) or any(
            type(measurement[name]) is not int
            for name in ("absolute_maximum_tick", "early_maximum_tick", "late_maximum_tick")
        ):
            raise StoreValidationError("shadow staged producer measurements are invalid")
    return {
        "aggregation": "member-bound-calibration-statistics-v1",
        "absolute_maximum_tick": max(cast(int, item["absolute_maximum_tick"]) for item in measurements),
        "clock_id": first["clock_id"],
        "corpus_member_count": len(members),
        "corpus_member_references": [member.corpus_member_reference_sha256 for member in members],
        "early_maximum_tick": max(cast(int, item["early_maximum_tick"]) for item in measurements),
        "eligible_anchor_count": sum(len(cast(list[object], item["matches"])) for item in measurements),
        "inference_kind": first["inference_kind"],
        "invalid_or_indeterminate_member_count": 0,
        "late_maximum_tick": max(cast(int, item["late_maximum_tick"]) for item in measurements),
        "matched_anchor_count": sum(len(cast(list[object], item["matches"])) for item in measurements),
        "producer": first["producer"],
        "producer_id": first["producer_id"],
        "time_base": first["time_base"],
    }


def _source_manifest_blob_refs(payload_json: str) -> tuple[BlobRef, ...]:
    """Extract the closed proxy-BlobRef surface from a source manifest payload."""

    try:
        payload: object = json.loads(
            payload_json,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {value}")
            ),
        )
        if not isinstance(payload, dict):
            raise ValueError("source manifest must be an object")
        root = cast(dict[str, object], payload)
        episodes = root.get("episodes")
        if not isinstance(episodes, list) or not episodes:
            raise ValueError("source manifest episodes must be a non-empty array")
        references: list[BlobRef] = []
        for episode_value in cast(list[object], episodes):
            if not isinstance(episode_value, dict):
                raise ValueError("source manifest episode must be an object")
            episode = cast(dict[str, object], episode_value)
            proxy = episode.get("proxy_blob")
            manifest = episode.get("window_manifest")
            if not isinstance(proxy, dict) or not isinstance(manifest, dict):
                raise ValueError("source manifest proxy BlobRef is malformed or unbound")
            proxy_mapping = cast(dict[str, object], proxy)
            manifest_mapping = cast(dict[str, object], manifest)
            if set(proxy_mapping) != {
                "object_id",
                "content_hash",
                "byte_length",
                "media_type",
            } or manifest_mapping.get("proxy_blob_ref") != proxy_mapping:
                raise ValueError("source manifest proxy BlobRef is malformed or unbound")
            references.append(
                BlobRef(
                    UUID(str(proxy_mapping["object_id"])),
                    str(proxy_mapping["content_hash"]),
                    cast(int, proxy_mapping["byte_length"]),
                    str(proxy_mapping["media_type"]),
                )
            )
    except (KeyError, TypeError, ValueError, StoreValidationError) as error:
        raise StoreValidationError(
            "source manifest contains invalid proxy BlobRefs"
        ) from error
    return tuple(references)


def _decode_committed_member_reference(
    value: object,
    field_name: str,
) -> CommittedArtifactMemberReference:
    try:
        return CommittedArtifactMemberReference.from_mapping(value)
    except StoreValidationError as error:
        raise StoreValidationError(f"{field_name} is not an exact committed member reference") from error


@dataclass(frozen=True, slots=True)
class _DecodedVlmSemanticPackSet:
    source_manifest_sha256: str
    source_provenance_sha256: str
    request_policy: VlmBatchRequestPolicy
    children: tuple[VlmSemanticPackSetChild, ...]
    declared_episode_count: int
    strategy_version: str

    def verified_payload_mapping(self) -> dict[str, object]:
        return {
            **batch_version_fields(self.strategy_version),
            "children": [child.to_mapping() for child in self.children],
            "declared_episode_count": self.declared_episode_count,
            "request_policy": self.request_policy.to_mapping(),
            "source_manifest_sha256": self.source_manifest_sha256,
            "source_provenance_sha256": self.source_provenance_sha256,
            "strategy_version": self.strategy_version,
        }


def _vlm_batch_request_hash(
    job: Job,
    artifact: ArtifactMember,
    decoded: _DecodedVlmSemanticPackSet,
) -> str:
    return canonical_payload_hash(
        json.dumps(
            {
                "artifact_revision": artifact.revision,
                "artifact_scope": {
                    "key": artifact.scope.key,
                    "kind": artifact.scope.kind,
                    "namespace": artifact.scope.namespace,
                },
                **decoded.verified_payload_mapping(),
                "job": {"job_key": job.job_key, "profile": job.profile},
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _decode_vlm_semantic_pack_set(
    payload_json: str,
) -> _DecodedVlmSemanticPackSet:
    payload = _closed_mapping(
        _strict_json_object(payload_json, "VLM Semantic Pack set"),
        frozenset(
            {
                "children",
                "completion_policy",
                "declared_episode_count",
                "request_policy",
                "source_manifest_sha256",
                "source_provenance_sha256",
                "strategy_version",
            }
        ),
        "VLM Semantic Pack set",
    )
    if payload["completion_policy"] != "all_committed_episodes":
        raise StoreValidationError("VLM Semantic Pack set completion policy is invalid")
    if payload["strategy_version"] != VLM_BATCH_FINALIZER_STRATEGY_VERSION:
        raise StoreValidationError("VLM Semantic Pack set strategy version is invalid")
    policy_mapping = _closed_mapping(
        payload["request_policy"],
        frozenset(
            {
                "model_id",
                "parse_policy_sha256",
                "preprocess_policy_sha256",
                "prompt_template_sha256",
                "prompt_version",
                "provider_id",
                "request_parameters_sha256",
                "response_schema_sha256",
                "window_sampling_policy_sha256",
            }
        ),
        "VLM Semantic Pack set request_policy",
    )
    policy = VlmBatchRequestPolicy(
        **{key: _text(value) for key, value in policy_mapping.items()}
    )
    child_values = payload["children"]
    if type(child_values) is not list:  # noqa: E721
        raise StoreValidationError("VLM Semantic Pack set children must be an array")
    children: list[VlmSemanticPackSetChild] = []
    for index, child_value in enumerate(cast(list[object], child_values)):
        child = _closed_mapping(
            child_value,
            frozenset(
                {
                    "episode_index",
                    "idempotency_key",
                    "request_hash",
                    "request_record",
                    "response_record",
                    "semantic_pack",
                }
            ),
            f"VLM Semantic Pack set child[{index}]",
        )
        children.append(
            VlmSemanticPackSetChild(
                episode_index=cast(int, child["episode_index"]),
                idempotency_key=_text(child["idempotency_key"]),
                request_hash=_text(child["request_hash"]),
                request_record=_decode_committed_member_reference(
                    child["request_record"], f"child[{index}].request_record"
                ),
                response_record=_decode_committed_member_reference(
                    child["response_record"], f"child[{index}].response_record"
                ),
                semantic_pack=_decode_committed_member_reference(
                    child["semantic_pack"], f"child[{index}].semantic_pack"
                ),
            )
        )
    declared_count = payload["declared_episode_count"]
    if (
        type(declared_count) is not int  # noqa: E721
        or declared_count != len(children)
        or tuple(item.episode_index for item in children) != tuple(range(len(children)))
        or not children
    ):
        raise StoreValidationError("VLM Semantic Pack set coverage is invalid")
    for field_name, values in (
        ("idempotency_key", tuple(item.idempotency_key for item in children)),
        ("request_hash", tuple(item.request_hash for item in children)),
        (
            "artifact_set_id",
            tuple(item.request_record.artifact_set_id for item in children),
        ),
        ("receipt_id", tuple(item.request_record.receipt_id for item in children)),
    ):
        if len(values) != len(set(values)):
            raise StoreValidationError(
                f"VLM Semantic Pack set contains duplicate child {field_name}"
            )
    return _DecodedVlmSemanticPackSet(
        source_manifest_sha256=_text(payload["source_manifest_sha256"]),
        source_provenance_sha256=_text(payload["source_provenance_sha256"]),
        request_policy=policy,
        children=tuple(children),
        declared_episode_count=declared_count,
        strategy_version=VLM_BATCH_FINALIZER_STRATEGY_VERSION,
    )
def _decode_registered_vlm_semantic_pack_set(payload_json: str) -> _DecodedVlmSemanticPackSet:
    payload = _strict_json_object(payload_json, "VLM Semantic Pack set")
    if payload.get("strategy_version") != VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4:
        return _decode_vlm_semantic_pack_set(payload_json)
    version = batch_version_fields(VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4)
    if (
        type(payload.get("schema_version")) is not int  # noqa: E721
        or any(payload.get(key) != value for key, value in version.items())
    ):
        raise StoreValidationError("V4 VLM batch parser/schema version binding is invalid")
    # Reuse only the unchanged structural checks, not v3 semantic decoding.
    legacy_shape = {key: value for key, value in payload.items() if key not in version}
    legacy_shape["strategy_version"] = VLM_BATCH_FINALIZER_STRATEGY_VERSION
    decoded = _decode_vlm_semantic_pack_set(json.dumps(legacy_shape))
    return replace(decoded, strategy_version=VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4)


def _vlm_request_record_projection(
    payload_json: str,
) -> tuple[int, str, str, str, str, str]:
    """Extract typed fields; the persisted model performs full closed validation."""

    try:
        payload: object = json.loads(
            payload_json,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {value}")
            ),
        )
        if type(payload) is not dict:  # noqa: E721
            raise ValueError("VLM request record must be an object")
        mapping = cast(dict[str, object], payload)
        episode_index = mapping["episode_index"]
        window_manifest_sha256 = mapping["window_manifest_sha256"]
        window_manifest_set_sha256 = mapping["window_manifest_set_sha256"]
        source_manifest_sha256 = mapping["source_manifest_sha256"]
        source_provenance_sha256 = mapping["source_provenance_sha256"]
        request_identity_sha256 = mapping["request_identity_sha256"]
        if (
            type(episode_index) is not int  # noqa: E721
            or type(window_manifest_sha256) is not str  # noqa: E721
            or type(window_manifest_set_sha256) is not str  # noqa: E721
            or type(source_manifest_sha256) is not str  # noqa: E721
            or type(source_provenance_sha256) is not str  # noqa: E721
            or type(request_identity_sha256) is not str  # noqa: E721
        ):
            raise ValueError("VLM request record projection types are invalid")
        return (
            episode_index,
            window_manifest_sha256,
            window_manifest_set_sha256,
            source_manifest_sha256,
            source_provenance_sha256,
            request_identity_sha256,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise StoreValidationError("VLM request record projection is invalid") from error


@dataclass(frozen=True, slots=True)
class _CommittedSet:
    job_id: UUID
    command_slot_id: UUID
    command_name: str
    set_hash: str
    members: tuple[tuple[int, ArtifactMember], ...]
    request_hash: str
    execution_kind: CommandExecutionKind


def _closed_mapping(
    value: object,
    fields: frozenset[str],
    field_name: str,
) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise StoreValidationError(f"{field_name} must be an object")
    result = cast(dict[str, object], value)
    if frozenset(result) != fields:
        raise StoreValidationError(f"{field_name} does not match its closed schema")
    return result


def _required_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise StoreValidationError(f"{field_name} must be non-empty text")
    return value


def _blob_ref(value: object, field_name: str) -> BlobRef:
    mapping = _closed_mapping(
        value,
        frozenset({"object_id", "content_hash", "byte_length", "media_type"}),
        field_name,
    )
    try:
        content_hash = _required_text(mapping["content_hash"], f"{field_name}.content_hash")
        byte_length = mapping["byte_length"]
        media_type = _required_text(mapping["media_type"], f"{field_name}.media_type")
        if type(byte_length) is not int:  # noqa: E721
            raise StoreValidationError(f"{field_name}.byte_length must be an integer")
        return BlobRef(
            UUID(_required_text(mapping["object_id"], f"{field_name}.object_id")),
            content_hash,
            byte_length,
            media_type,
        )
    except (TypeError, ValueError) as error:
        raise StoreValidationError(f"{field_name} is invalid") from error


def _exact_blob_bytes(
    cursor: DbCursor,
    reference: BlobRef,
    field_name: str,
) -> bytes:
    cursor.execute(
        "SELECT content_bytes FROM storage.blob_objects WHERE object_id = %s",
        (reference.object_id,),
    )
    row = cursor.fetchone()
    if row is None or not isinstance(row[0], (bytes, bytearray, memoryview)):
        raise StoreValidationError(f"{field_name} bytes are unavailable")
    content = row[0].tobytes() if isinstance(row[0], memoryview) else bytes(row[0])
    if (
        len(content) != reference.byte_length
        or "sha256:" + hashlib.sha256(content).hexdigest() != reference.content_hash
    ):
        raise StoreValidationError(f"{field_name} bytes fail exact integrity validation")
    return content


def _decode_request_identity(value: object) -> VlmRequestIdentity:
    mapping = _closed_mapping(value, VLM_REQUEST_IDENTITY_FIELDS, "request_identity")
    try:
        return VlmRequestIdentity(
            window_manifest_sha256=_required_text(
                mapping["window_manifest_sha256"],
                "request_identity.window_manifest_sha256",
            ),
            source_id=_required_text(mapping["source_id"], "request_identity.source_id"),
            source_clock_id=_required_text(
                mapping["source_clock_id"], "request_identity.source_clock_id"
            ),
            source_sha256=_required_text(
                mapping["source_sha256"], "request_identity.source_sha256"
            ),
            frame_samples_sha256=_required_text(
                mapping["frame_samples_sha256"],
                "request_identity.frame_samples_sha256",
            ),
            frame_pts_index_set_sha256=_required_text(
                mapping["frame_pts_index_set_sha256"],
                "request_identity.frame_pts_index_set_sha256",
            ),
            window_manifest_set_sha256=_required_text(
                mapping["window_manifest_set_sha256"],
                "request_identity.window_manifest_set_sha256",
            ),
            proxy_blob_ref_sha256=_required_text(
                mapping["proxy_blob_ref_sha256"],
                "request_identity.proxy_blob_ref_sha256",
            ),
            preprocess_policy_sha256=_required_text(
                mapping["preprocess_policy_sha256"],
                "request_identity.preprocess_policy_sha256",
            ),
            window_sampling_policy_sha256=_required_text(
                mapping["window_sampling_policy_sha256"],
                "request_identity.window_sampling_policy_sha256",
            ),
            prompt_template_sha256=_required_text(
                mapping["prompt_template_sha256"],
                "request_identity.prompt_template_sha256",
            ),
            prompt_version=_required_text(
                mapping["prompt_version"], "request_identity.prompt_version"
            ),
            response_schema_sha256=_required_text(
                mapping["response_schema_sha256"],
                "request_identity.response_schema_sha256",
            ),
            model_id=_required_text(mapping["model_id"], "request_identity.model_id"),
            provider_id=_required_text(
                mapping["provider_id"], "request_identity.provider_id"
            ),
            request_parameters_sha256=_required_text(
                mapping["request_parameters_sha256"],
                "request_identity.request_parameters_sha256",
            ),
            request_payload_sha256=_required_text(
                mapping["request_payload_sha256"],
                "request_identity.request_payload_sha256",
            ),
            parse_policy_sha256=_required_text(
                mapping["parse_policy_sha256"],
                "request_identity.parse_policy_sha256",
            ),
        )
    except VlmValidationError as error:
        raise StoreValidationError("request_identity is invalid") from error


def _decode_parse_policy(value: object) -> VlmParsePolicy:
    if type(value) is not dict:  # noqa: E721
        raise StoreValidationError("parse_policy must be an object")
    try:
        policy = VlmParsePolicy(**cast(dict[str, int], value))
    except (TypeError, VlmValidationError) as error:
        raise StoreValidationError("parse_policy is invalid") from error
    if policy.to_mapping() != value:
        raise StoreValidationError("parse_policy is not in canonical persisted form")
    return policy


@dataclass(frozen=True, slots=True)
class _DecodedSourceWindow:
    identity: SourceWindowIdentity
    manifest: WindowManifest
    manifest_set: WindowManifestSet

    @property
    def canonical_order_key(self) -> tuple[object, ...]:
        manifest = self.manifest
        return (
            self.identity.episode_index,
            manifest.stream_index,
            manifest.core_range.start_pts,
            manifest.core_range.end_pts,
            manifest.canonical_hash,
        )


@dataclass(frozen=True, slots=True)
class _VerifiedVlmGenerationChild:
    """Internal exact child closure retained for typed downstream projections."""

    child: PersistedVlmGenerationChild
    semantic_pack: PersistedVlmSemanticPack | PersistedVlmSemanticPackV4
    request_identity: VlmRequestIdentity | None
    response_record: CommittedArtifactMemberReference
    response_payload_json: str
    provider_request_id: str | None
    raw_response: BlobRef
    source_manifest: PersistedWholeSeriesSourceManifest | None


def _strict_source_windows(
    persisted: PersistedWholeSeriesSourceManifest,
) -> tuple[SourceOperationGrant, tuple[_DecodedSourceWindow, ...]]:
    """Use the Kernel-owned canonical decoder; never project loose mappings."""

    try:
        prepared = decode_source_manifest(
            persisted.payload_json,
            persisted.proxy_blobs,
        )
        results = tuple(
            _DecodedSourceWindow(
                identity=SourceWindowIdentity(
                    episode_index=episode_index,
                    source_id=episode.manifest.source_id,
                    source_sha256=episode.manifest.source_sha256,
                    source_clock_id=episode.manifest.source_clock_id,
                    window_manifest_sha256=episode.manifest.canonical_hash,
                    window_manifest_set_sha256=episode.manifest_set.canonical_hash,
                    proxy_blob=proxy_blob,
                    stream_index=episode.manifest.stream_index,
                    core_start_pts=episode.manifest.core_range.start_pts,
                    core_end_pts=episode.manifest.core_range.end_pts,
                ),
                manifest=episode.manifest,
                manifest_set=episode.manifest_set,
            )
            for episode_index, (episode, proxy_blob) in enumerate(
                zip(prepared.episodes, persisted.proxy_blobs, strict=True)
            )
        )
    except (SourceManifestDecodeError, TypeError, ValueError) as error:
        raise StoreValidationError(
            "committed SourceManifest failed canonical source-prep decoding"
        ) from error
    if not results or len({item.manifest.canonical_hash for item in results}) != len(results):
        raise StoreValidationError("source windows must have unique immutable identities")
    return prepared.census, tuple(
        sorted(results, key=lambda item: item.canonical_order_key)
    )


class PostgresRuntimeStore:
    """Persist one Job's idempotent commands, receipts and immutable artifacts."""

    def __init__(
        self,
        connection_factory: Callable[[], DbConnection],
        *,
        materialization_staging_root: Path | None = None,
        object_store_verifier: S3PendingObjectStore | None = None,
    ) -> None:
        if not callable(connection_factory):
            raise StoreValidationError("connection_factory must be callable")
        if materialization_staging_root is not None:
            validate_materialization_staging_root(materialization_staging_root)
        if object_store_verifier is not None and type(  # noqa: E721
            object_store_verifier
        ) is not S3PendingObjectStore:
            raise StoreValidationError(
                "object_store_verifier must be an exact S3PendingObjectStore"
            )
        self._connection_factory = connection_factory
        self._materialization_staging_root = materialization_staging_root
        self._object_store_verifier = object_store_verifier

    # ------------------------------------------------------------------
    # claim_command
    # ------------------------------------------------------------------

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        """Claim a non-reserved command through the generic command boundary."""

        if (
            claim.command_name == "GenerateVlmEvidenceCommand"
            and claim.execution_kind != "generation"
        ):
            raise CommandStateError(
                "GenerateVlmEvidenceCommand requires a generation execution kind"
            )
        if (
            claim.command_name
            in (
                CALIBRATION_VALIDATOR_COMMAND,
                BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND,
                "FinalizeRunOutcome",
            )
            and claim.execution_kind != "deterministic"
        ):
            raise CommandStateError("protected command requires a deterministic execution kind")
        if claim.command_name == SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME:
            raise CommandStateError(
                "MeasureShadowCalibrationCommand@2.1.3 requires the explicit shadow owner API"
            )
        if claim.command_name == SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME:
            raise CommandStateError(
                "MeasureShadowLocalCalibrationCommand@1 requires the explicit local shadow owner API"
            )
        if claim.idempotency_key.startswith("shadow-local-measurement:"):
            raise CommandStateError(
                "shadow-local-measurement idempotency keys require the explicit local shadow owner API"
            )
        if (
            claim.command_name == VLM_BATCH_FINALIZER_COMMAND_NAME
            or claim.idempotency_key.startswith(VLM_BATCH_IDEMPOTENCY_PREFIX)
        ):
            raise CommandStateError(
                "FinalizeVlmBatchCommand requires the explicit VLM batch owner API"
            )
        return self._claim_command(claim)

    def claim_vlm_batch_command(self, claim: CommandClaim) -> CommandOutcome:
        """Claim the reserved VLM batch-finalizer identity."""

        if claim.command_name != VLM_BATCH_FINALIZER_COMMAND_NAME:
            raise CommandStateError(
                "VLM batch owner API accepts only FinalizeVlmBatchCommand"
            )
        if not claim.idempotency_key.startswith(VLM_BATCH_IDEMPOTENCY_PREFIX):
            raise CommandStateError(
                "VLM batch owner API requires the reserved vlm-batch identity"
            )
        if claim.execution_kind != "deterministic":
            raise CommandStateError("VLM batch owner API requires a deterministic execution kind")
        return self._claim_command(claim)

    def _claim_command(self, claim: CommandClaim) -> CommandOutcome:
        """Create or replay a canonical command claim for one durable Job.

        Uses INSERT … ON CONFLICT DO NOTHING RETURNING so that two
        concurrent callers with the same (job, idempotency_key) pair
        never leak a raw unique-violation to the application.
        """

        def operation(cursor: DbCursor) -> CommandOutcome:
            job_id = self._ensure_job(cursor, claim.job)
            # Every mutation of this aggregate uses the same lock order:
            # Job first, then command slot.  In particular, take the Job lock
            # before deciding whether a key is fresh, so a terminal transition
            # and a fresh claim are serialized.
            job_state = self._locked_job_state(cursor, job_id)
            cursor.execute(
                """
                SELECT command_slot_id, command_name, request_hash, execution_kind
                  FROM runtime.command_slots
                 WHERE job_id = %s AND idempotency_key = %s
                   FOR UPDATE
                """,
                (job_id, claim.idempotency_key),
            )
            existing = cursor.fetchone()
            if existing is not None:
                slot_id_existing, command_name, request_hash, execution_kind = existing
                if (
                    _text(command_name) != claim.command_name
                    or _text(request_hash) != claim.request_hash
                    or _text(execution_kind) != claim.execution_kind
                ):
                    raise IdempotencyConflictError(
                        "idempotency key was already claimed by a different command"
                    )
                return self._read_outcome_by_slot(cursor, UUID(str(slot_id_existing)), job_id)

            if job_state not in ("pending", "running"):
                raise CommandStateError("job is already terminal; new commands are closed")
            slot_id = uuid4()
            cursor.execute(
                """
                INSERT INTO runtime.command_slots
                    (command_slot_id, job_id, idempotency_key, command_name, request_hash,
                     execution_kind, state)
                VALUES (%s, %s, %s, %s, %s, %s, 'running')
                """,
                (
                    slot_id,
                    job_id,
                    claim.idempotency_key,
                    claim.command_name,
                    claim.request_hash,
                    claim.execution_kind,
                ),
            )
            if job_state == "pending":
                cursor.execute(
                    "UPDATE runtime.jobs SET state = 'running' WHERE job_id = %s",
                    (job_id,),
                )
            # This is the sole path that creates a command slot.  The result
            # therefore records durable ownership from this same transaction;
            # a read of an existing running or terminal slot is always replay.
            return CommandOutcome(
                command_slot_id=slot_id,
                state="running",
                is_fresh_claim=True,
                job_id=job_id,
            )

        return self._transaction(operation)

    def _read_source_reuse_origin(
        self,
        cursor: DbCursor,
        binding: SourceReuseBinding,
    ) -> PersistedWholeSeriesSourceManifest:
        """Independently reconstruct the immutable origin named by a binding."""

        declared = binding.origin
        origin_job = declared.source_job
        if origin_job is None:
            raise StoreValidationError("source reuse origin has no source Job")
        cursor.execute(
            "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s",
            (origin_job.job_key,),
        )
        job_row = cursor.fetchone()
        if job_row is None or (
            UUID(str(job_row[0])) != declared.job_id
            or _text(job_row[1]) != origin_job.profile
        ):
            raise StoreValidationError("source reuse origin Job is unavailable or changed")
        cursor.execute(
            """
            SELECT artifact.artifact_type, artifact.logical_id, artifact.revision, artifact.namespace,
                   artifact.scope_kind, artifact.scope_key, artifact.content_hash,
                   artifact.payload_json::text, artifact_set.set_hash,
                   artifact_set.member_count, receipt.receipt_id, slot.command_slot_id
              FROM runtime.artifact_sets AS artifact_set
              JOIN runtime.command_slots AS slot
                ON slot.command_slot_id = artifact_set.command_slot_id
               AND slot.job_id = artifact_set.job_id
              JOIN runtime.command_receipts AS receipt
                ON receipt.command_slot_id = slot.command_slot_id
               AND receipt.result_artifact_set_id = artifact_set.artifact_set_id
              JOIN runtime.artifact_set_members AS member
                ON member.artifact_set_id = artifact_set.artifact_set_id
               AND member.ordinal = 0
              JOIN runtime.artifacts AS artifact
                ON artifact.artifact_id = member.artifact_id
               AND artifact.artifact_set_id = artifact_set.artifact_set_id
               AND artifact.job_id = artifact_set.job_id
             WHERE artifact_set.artifact_set_id = %s
               AND artifact_set.job_id = %s
               AND receipt.receipt_id = %s
               AND slot.command_slot_id = %s
               AND slot.command_name = 'PrepareWholeSeriesSourcesCommand'
               AND slot.state = 'succeeded'
               AND receipt.outcome = 'succeeded'
               AND artifact_set.member_count = 1
               AND artifact.artifact_type = 'whole_series_source_manifest'
               AND artifact.logical_id = 'whole_series_source_manifest'
            """,
            (
                declared.artifact_set_id,
                declared.job_id,
                declared.receipt_id,
                declared.command_slot_id,
            ),
        )
        rows: list[tuple[object, ...]] = []
        while (row := cursor.fetchone()) is not None:
            rows.append(row)
        if len(rows) != 1:
            raise StoreValidationError("source reuse origin is not an exact SourcePrep result")
        (
            artifact_type,
            logical_id,
            revision,
            namespace,
            scope_kind,
            scope_key,
            content_hash,
            payload_json,
            set_hash,
            member_count,
            receipt_id,
            command_slot_id,
        ) = rows[0]
        if int(_text(member_count)) != 1:
            raise StoreValidationError("source reuse origin set is not singleton")
        scope = ArtifactScope(_text(namespace), _text(scope_kind), _text(scope_key))
        if scope != canonical_recipe_scope(origin_job):
            raise StoreValidationError("source reuse origin scope is not canonical")
        reference = WholeSeriesSourceManifestReference(
            scope, _text(logical_id), int(_text(revision)), _text(content_hash)
        )
        serialized = _text(payload_json)
        artifact = ArtifactMember(
            _text(artifact_type),
            reference.logical_id,
            reference.revision,
            reference.scope,
            reference.content_hash,
            serialized,
        )
        if artifact.artifact_type != "whole_series_source_manifest":
            raise StoreValidationError("source reuse origin artifact type is invalid")
        CommandSuccess(UUID(str(command_slot_id)), _text(set_hash), (artifact,))
        blobs = tuple(
            self._claimed_blob_ref(
                cursor,
                declared.job_id,
                blob,
                field_name=f"source reuse origin proxy[{position}]",
            )
            for position, blob in enumerate(_source_manifest_blob_refs(serialized))
        )
        actual = PersistedWholeSeriesSourceManifest(
            reference,
            serialized,
            blobs,
            declared.job_id,
            UUID(str(receipt_id)),
            declared.artifact_set_id,
            UUID(str(command_slot_id)),
            origin_job,
        )
        _strict_source_windows(actual)
        if (
            actual.provenance_mapping() != declared.provenance_mapping()
            or actual.canonical_hash != declared.canonical_hash
        ):
            raise StoreValidationError("source reuse origin provenance differs from its binding")
        return actual

    def _validate_source_reuse_binding_member(
        self,
        cursor: DbCursor,
        artifact_set_id: UUID,
        target_job: Job,
        source: PersistedWholeSeriesSourceManifest,
    ) -> ArtifactMember:
        """Verify a target-scoped binding before exposing the reused source."""

        cursor.execute(
            """
            SELECT artifact.artifact_type, artifact.logical_id, artifact.revision, artifact.namespace,
                   artifact.scope_kind, artifact.scope_key, artifact.content_hash,
                   artifact.payload_json::text
              FROM runtime.artifact_set_members AS member
              JOIN runtime.artifacts AS artifact
                ON artifact.artifact_id = member.artifact_id
               AND artifact.artifact_set_id = member.artifact_set_id
             WHERE member.artifact_set_id = %s AND member.ordinal = 1
            """,
            (artifact_set_id,),
        )
        row = cursor.fetchone()
        if row is None or cursor.fetchone() is not None:
            raise StoreValidationError("source reuse binding member is unavailable")
        (
            artifact_type,
            logical_id,
            revision,
            namespace,
            scope_kind,
            scope_key,
            content_hash,
            payload_json,
        ) = row
        artifact = ArtifactMember(
            _text(artifact_type),
            _text(logical_id),
            int(_text(revision)),
            ArtifactScope(_text(namespace), _text(scope_kind), _text(scope_key)),
            _text(content_hash),
            _text(payload_json),
        )
        if (
            artifact.artifact_type != SOURCE_REUSE_BINDING_ARTIFACT_TYPE
            or artifact.logical_id != SOURCE_REUSE_BINDING_LOGICAL_ID
            or artifact.scope != canonical_recipe_scope(target_job)
            or canonical_payload_hash(artifact.payload_json) != artifact.content_hash
        ):
            raise StoreValidationError("source reuse binding member identity is invalid")
        try:
            raw_value = json.loads(artifact.payload_json)
        except (TypeError, ValueError) as error:
            raise StoreValidationError("source reuse binding payload is invalid JSON") from error
        if type(raw_value) is not dict:  # noqa: E721
            raise StoreValidationError("source reuse binding payload is not closed")
        raw: dict[str, object] = {}
        for key, value in cast(dict[object, object], raw_value).items():
            if type(key) is not str:  # noqa: E721
                raise StoreValidationError("source reuse binding payload keys are invalid")
            raw[key] = value
        if set(raw) != {
            "origin",
            "origin_source_manifest_sha256",
            "origin_source_provenance_sha256",
            "schema_version",
            "target_job",
            "target_policy",
            "target_policy_sha256",
        }:
            raise StoreValidationError("source reuse binding payload is not closed")
        if raw["schema_version"] != SOURCE_REUSE_BINDING_SCHEMA_VERSION:
            raise StoreValidationError("source reuse binding schema version is unsupported")
        if raw["target_job"] != {"job_key": target_job.job_key, "profile": target_job.profile}:
            raise StoreValidationError("source reuse binding target Job is invalid")
        if raw["origin_source_manifest_sha256"] != source.reference.content_hash:
            raise StoreValidationError("source reuse binding source hash is invalid")
        try:
            decoded = decode_source_manifest(source.payload_json, source.proxy_blobs)
        except SourceManifestDecodeError as error:
            raise StoreValidationError("source reuse source manifest is invalid") from error
        if raw["target_policy"] != decoded.census.policy.to_mapping():
            raise StoreValidationError("source reuse binding policy differs from source manifest")
        if raw["target_policy_sha256"] != decoded.census.policy.policy_sha256:
            raise StoreValidationError("source reuse binding policy hash is invalid")
        return artifact

    # ------------------------------------------------------------------
    # shadow calibration measurement recovery owner
    # ------------------------------------------------------------------

    def claim_or_read_shadow_measurement_attempt(
        self, claim: CommandClaim, plan: ShadowMeasurementPlan
    ) -> ShadowMeasurementAttempt:
        """Reserve or reread the one closed native-measurement recovery aggregate."""

        self._require_shadow_plan_claim(claim, plan)
        outcome = self._claim_command(claim)

        def operation(cursor: DbCursor) -> ShadowMeasurementAttempt:
            job_id, slot_state, command_name, request_hash = self._locked_job_then_slot(
                cursor, outcome.command_slot_id
            )
            self._require_slot_execution_kind(cursor, outcome.command_slot_id, "deterministic")
            if (
                command_name != SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME
                or request_hash != plan.claim.request_hash
                or slot_state not in ("running", "succeeded")
            ):
                raise CommandStateError("shadow measurement slot is not owned by the recovery protocol")
            existing = self._read_shadow_attempt_by_slot(cursor, outcome.command_slot_id)
            if existing is not None:
                return existing
            if slot_state != "running":
                raise CommandStateError("succeeded shadow command is missing its immutable attempt")
            attempt_id = uuid4()
            cursor.execute(
                """
                INSERT INTO runtime.shadow_calibration_measurement_attempts
                    (attempt_id, command_slot_id, job_id, plan_hash, attempt_ordinal,
                     state, version, plan_json)
                VALUES (%s, %s, %s, %s, 1, 'prepared', 0, %s::jsonb)
                """,
                (attempt_id, outcome.command_slot_id, job_id, plan.claim.request_hash, plan.canonical_plan_json),
            )
            for member in plan.members:
                cursor.execute(
                    """
                    INSERT INTO runtime.shadow_calibration_measurement_members
                        (attempt_id, corpus_member_reference_sha256, member_ordinal,
                         expected_anchor_reference_sha256, invocation_json, context_json, state, version)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, 'pending', 0)
                    """,
                    (
                        attempt_id,
                        member.corpus_member_reference_sha256,
                        member.member_ordinal,
                        member.expected_anchor_reference_sha256,
                        member.invocation_json,
                        member.context_json,
                    ),
                )
            created = self._read_shadow_attempt_by_id(cursor, attempt_id)
            if created is None:
                raise RuntimeStoreError("shadow measurement attempt vanished after reservation")
            return created

        return self._transaction(operation)

    def acquire_shadow_measurement_member_lease(
        self, attempt_id: UUID, member_reference_sha256: str, *, expected_version: int
    ) -> ShadowMeasurementMemberLease | None:
        """CAS-acquire the short lease that records native invocation may begin."""

        self._validate_uuid(attempt_id, "shadow attempt_id")
        self._validate_sha256(member_reference_sha256, "shadow member reference")
        self._validate_nonnegative_version(expected_version, "shadow member expected_version")
        token = str(uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=SHADOW_MEASUREMENT_LEASE_SECONDS)

        def operation(cursor: DbCursor) -> ShadowMeasurementMemberLease | None:
            attempt = self._locked_shadow_attempt(cursor, attempt_id)
            if attempt.state not in ("prepared", "collecting") or attempt.outcome.state != "running":
                return None
            member = self._locked_shadow_member(cursor, attempt_id, member_reference_sha256)
            if member is None or member.version != expected_version or member.state != "pending":
                return None
            cursor.execute(
                """
                UPDATE runtime.shadow_calibration_measurement_members
                   SET state = 'invoking', lease_token = %s, lease_expires_at = %s, version = version + 1
                 WHERE attempt_id = %s AND corpus_member_reference_sha256 = %s
                   AND state = 'pending' AND version = %s
                """,
                (token, expires_at, attempt_id, member_reference_sha256, expected_version),
            )
            if cursor.rowcount != 1:
                return None
            self._transition_shadow_attempt(cursor, attempt, "collecting")
            current = self._read_shadow_attempt_by_id(cursor, attempt_id)
            if current is None:
                raise RuntimeStoreError("shadow attempt vanished after member lease")
            leased = next(item for item in current.members if item.corpus_member_reference_sha256 == member_reference_sha256)
            return ShadowMeasurementMemberLease(leased, current.version, token)

        return self._transaction(operation)

    def stage_shadow_measurement_member_response(
        self,
        attempt_id: UUID,
        member_reference_sha256: str,
        *,
        expected_version: int,
        lease_token: str,
        staged: ShadowMeasurementStagedResponse,
    ) -> ShadowMeasurementAttempt:
        """Atomically claim exact raw bytes and attach them to the leased member."""

        self._validate_uuid(attempt_id, "shadow attempt_id")
        self._validate_sha256(member_reference_sha256, "shadow member reference")
        self._validate_nonnegative_version(expected_version, "shadow member expected_version")
        if type(staged) is not ShadowMeasurementStagedResponse:  # noqa: E721
            raise StoreValidationError("shadow stage requires exact staged response")
        if type(lease_token) is not str or not lease_token.strip():  # noqa: E721
            raise StoreValidationError("shadow stage lease_token is required")

        def operation(cursor: DbCursor) -> ShadowMeasurementAttempt:
            attempt = self._locked_shadow_attempt(cursor, attempt_id)
            member = self._locked_shadow_member(cursor, attempt_id, member_reference_sha256)
            if member is None:
                raise StoreValidationError("shadow stage member is unknown")
            if member.state == "staged":
                if (
                    member.raw_blob is None
                    or member.raw_blob.content_hash != staged.content_hash
                    or member.raw_blob.byte_length != len(staged.raw_bytes)
                    or member.raw_blob.media_type != staged.media_type
                    or member.projection_json != staged.projection_json
                ):
                    raise IdempotencyConflictError("shadow staged member cannot be substituted")
                return attempt
            if member.state != "invoking" or member.version != expected_version:
                raise CommandStateError("shadow stage member is stale or not invoking")
            self._require_shadow_member_lease(member, lease_token, "stage")
            reference = self._put_shadow_blob(cursor, attempt.job, staged)
            cursor.execute(
                """
                UPDATE runtime.shadow_calibration_measurement_members
                   SET state = 'staged', lease_token = NULL, lease_expires_at = NULL,
                       raw_blob_object_id = %s, raw_content_hash = %s, raw_byte_length = %s,
                       raw_media_type = %s, projection_json = %s::jsonb, version = version + 1
                 WHERE attempt_id = %s AND corpus_member_reference_sha256 = %s
                   AND state = 'invoking' AND version = %s AND lease_token = %s
                   AND lease_expires_at > transaction_timestamp()
                """,
                (
                    reference.object_id, reference.content_hash, reference.byte_length, reference.media_type,
                    staged.projection_json, attempt_id, member_reference_sha256, expected_version, lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise CommandStateError("shadow stage lease was lost")
            cursor.execute(
                "SELECT count(*) FROM runtime.shadow_calibration_measurement_members WHERE attempt_id = %s AND state = 'staged'",
                (attempt_id,),
            )
            all_staged = int(_text(cursor.fetchone()[0])) == len(attempt.members)  # type: ignore[index]
            self._transition_shadow_attempt(cursor, attempt, "ready" if all_staged else "collecting")
            current = self._read_shadow_attempt_by_id(cursor, attempt_id)
            if current is None:
                raise RuntimeStoreError("shadow attempt vanished after staging")
            return current

        return self._transaction(operation)

    def acquire_shadow_measurement_recovery_lease(
        self, attempt_id: UUID, *, expected_version: int
    ) -> ShadowMeasurementRecoveryLease | None:
        """CAS-acquire recovery only after the prior recovery lease expires."""

        self._validate_uuid(attempt_id, "shadow attempt_id")
        self._validate_nonnegative_version(expected_version, "shadow recovery expected_version")
        token = str(uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=SHADOW_MEASUREMENT_LEASE_SECONDS)

        def operation(cursor: DbCursor) -> ShadowMeasurementRecoveryLease | None:
            attempt = self._locked_shadow_attempt(cursor, attempt_id)
            if attempt.version != expected_version or attempt.outcome.state != "running":
                return None
            cursor.execute(
                """
                UPDATE runtime.shadow_calibration_measurement_attempts
                   SET recovery_lease_token = %s, recovery_lease_expires_at = %s, version = version + 1
                 WHERE attempt_id = %s AND version = %s
                   AND (recovery_lease_expires_at IS NULL
                        OR recovery_lease_expires_at <= transaction_timestamp())
                """,
                (token, expires_at, attempt_id, expected_version),
            )
            if cursor.rowcount != 1:
                return None
            current = self._read_shadow_attempt_by_id(cursor, attempt_id)
            if current is None:
                raise RuntimeStoreError("shadow attempt vanished after recovery lease")
            return ShadowMeasurementRecoveryLease(current, token)

        return self._transaction(operation)

    def mark_shadow_measurement_member_indeterminate(
        self,
        attempt_id: UUID,
        member_reference_sha256: str,
        *,
        expected_version: int,
        recovery_lease_token: str,
        code: str = "NATIVE_OUTCOME_UNKNOWN",
    ) -> ShadowMeasurementAttempt:
        """Record an expired unstageable native invocation without terminalizing its command."""

        self._validate_uuid(attempt_id, "shadow attempt_id")
        self._validate_sha256(member_reference_sha256, "shadow member reference")
        self._validate_nonnegative_version(expected_version, "shadow member expected_version")
        if code != "NATIVE_OUTCOME_UNKNOWN":
            raise StoreValidationError("shadow indeterminate code is unsupported")
        if type(recovery_lease_token) is not str or not recovery_lease_token.strip():  # noqa: E721
            raise StoreValidationError("shadow recovery lease_token is required")

        def operation(cursor: DbCursor) -> ShadowMeasurementAttempt:
            attempt = self._locked_shadow_attempt(cursor, attempt_id)
            self._require_shadow_recovery_lease(attempt, recovery_lease_token, "mark indeterminate")
            member = self._locked_shadow_member(cursor, attempt_id, member_reference_sha256)
            if member is None or member.version != expected_version or member.state != "invoking":
                raise CommandStateError("shadow indeterminate member is stale or not invoking")
            if member.lease_expires_at is None or member.lease_expires_at > datetime.now(timezone.utc):
                raise CommandStateError("shadow member invocation lease has not expired")
            cursor.execute(
                """
                UPDATE runtime.shadow_calibration_measurement_members
                   SET state = 'indeterminate', lease_token = NULL, lease_expires_at = NULL, version = version + 1
                 WHERE attempt_id = %s AND corpus_member_reference_sha256 = %s
                   AND state = 'invoking' AND version = %s AND lease_expires_at <= transaction_timestamp()
                """,
                (attempt_id, member_reference_sha256, expected_version),
            )
            if cursor.rowcount != 1:
                raise CommandStateError("shadow indeterminate CAS was lost")
            cursor.execute(
                """
                UPDATE runtime.shadow_calibration_measurement_attempts
                   SET state = 'indeterminate', version = version + 1
                 WHERE attempt_id = %s AND version = %s AND recovery_lease_token = %s
                   AND recovery_lease_expires_at > transaction_timestamp()
                """,
                (attempt_id, attempt.version, recovery_lease_token),
            )
            if cursor.rowcount != 1:
                raise CommandStateError("shadow indeterminate recovery lease was lost")
            current = self._read_shadow_attempt_by_id(cursor, attempt_id)
            if current is None:
                raise RuntimeStoreError("shadow attempt vanished after indeterminate transition")
            return current

        return self._transaction(operation)

    def reserve_shadow_measurement_successor(
        self,
        previous_attempt_id: UUID,
        authorization: ShadowMeasurementRetryAuthorization,
    ) -> ShadowMeasurementAttempt:
        """Record the sole bounded successor after an authority retry decision."""

        self._validate_uuid(previous_attempt_id, "shadow previous_attempt_id")
        if type(authorization) is not ShadowMeasurementRetryAuthorization:  # noqa: E721
            raise StoreValidationError("shadow successor requires exact retry authorization")

        def operation(cursor: DbCursor) -> ShadowMeasurementAttempt:
            previous = self._locked_shadow_attempt(cursor, previous_attempt_id)
            if previous.state != "indeterminate" or previous.outcome.state != "running":
                raise CommandStateError("only a running indeterminate shadow attempt may have a successor")
            if previous.plan_hash != authorization.predecessor_plan_hash:
                raise StoreValidationError("shadow retry authorization plan hash does not match predecessor")
            if any(member.state not in ("staged", "indeterminate") for member in previous.members):
                raise CommandStateError("shadow successor requires every predecessor member to be staged or indeterminate")
            if previous.recovery_lease_expires_at is not None and previous.recovery_lease_expires_at > datetime.now(timezone.utc):
                raise CommandStateError("shadow successor is blocked by an active recovery lease")
            successor_ordinal = previous.attempt_ordinal + 1
            cursor.execute(
                "SELECT attempt_id FROM runtime.shadow_calibration_measurement_attempts WHERE job_id = %s AND plan_hash = %s AND attempt_ordinal = %s FOR UPDATE",
                (previous.outcome.job_id, previous.plan_hash, successor_ordinal),
            )
            existing = cursor.fetchone()
            if existing is not None:
                found = self._read_shadow_attempt_by_id(cursor, UUID(str(existing[0])))
                if found is None:
                    raise RuntimeStoreError("shadow successor vanished after replay read")
                return found
            slot_id = uuid4()
            successor_key = f"shadow-calibration-successor:{previous.plan_hash.removeprefix('sha256:')}:{successor_ordinal}"
            cursor.execute(
                """
                INSERT INTO runtime.command_slots
                    (command_slot_id, job_id, idempotency_key, command_name, request_hash,
                     execution_kind, state)
                VALUES (%s, %s, %s, %s, %s, 'deterministic', 'running')
                """,
                (slot_id, previous.outcome.job_id, successor_key, SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME, previous.plan_hash),
            )
            attempt_id = uuid4()
            cursor.execute(
                """
                INSERT INTO runtime.shadow_calibration_measurement_attempts
                    (attempt_id, command_slot_id, job_id, plan_hash, attempt_ordinal, previous_attempt_id,
                     state, version, plan_json, retry_decision_reference_sha256, retry_reason_code)
                VALUES (%s, %s, %s, %s, %s, %s, 'prepared', 0, %s::jsonb, %s, 'NATIVE_OUTCOME_UNKNOWN')
                """,
                (
                    attempt_id, slot_id, previous.outcome.job_id, previous.plan_hash, successor_ordinal,
                    previous.attempt_id, previous.canonical_plan_json, authorization.decision_reference_sha256,
                ),
            )
            for member in previous.members:
                cursor.execute(
                    """
                    INSERT INTO runtime.shadow_calibration_measurement_members
                        (attempt_id, corpus_member_reference_sha256, member_ordinal,
                         expected_anchor_reference_sha256, invocation_json, context_json, state, version)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, 'pending', 0)
                    """,
                    (attempt_id, member.corpus_member_reference_sha256, member.member_ordinal,
                     member.expected_anchor_reference_sha256, member.invocation_json, member.context_json),
                )
            successor = self._read_shadow_attempt_by_id(cursor, attempt_id)
            if successor is None:
                raise RuntimeStoreError("shadow successor vanished after reservation")
            return successor

        return self._transaction(operation)

    # ------------------------------------------------------------------
    # shadow-local calibration measurement recovery owner
    # ------------------------------------------------------------------

    def claim_or_read_shadow_local_measurement_attempt(
        self, plan: ShadowLocalMeasurementPlan
    ) -> ShadowLocalMeasurementAttempt:
        """Reserve or reread the closed local-only measurement journal."""

        claim = plan.claim
        self._require_shadow_local_plan_claim(claim, plan)

        def operation(cursor: DbCursor) -> ShadowLocalMeasurementAttempt:
            job_id = self._ensure_job(cursor, claim.job)
            job_state = self._locked_job_state(cursor, job_id)
            cursor.execute(
                """
                SELECT command_slot_id, command_name, request_hash, execution_kind
                  FROM runtime.command_slots
                 WHERE job_id = %s AND idempotency_key = %s FOR UPDATE
                """,
                (job_id, claim.idempotency_key),
            )
            existing_slot = cursor.fetchone()
            if existing_slot is not None:
                slot_id, command_name, request_hash, execution_kind = existing_slot
                if (
                    _text(command_name) != claim.command_name
                    or _text(request_hash) != claim.request_hash
                    or _text(execution_kind) != claim.execution_kind
                ):
                    raise IdempotencyConflictError(
                        "idempotency key was already claimed by a different command"
                    )
                existing = self._read_shadow_local_attempt_by_slot(cursor, UUID(str(slot_id)))
                if existing is None:
                    raise CommandStateError("shadow-local command slot is missing its immutable journal")
                return existing
            if job_state not in ("pending", "running"):
                raise CommandStateError("job is already terminal; new commands are closed")
            for member in plan.members:
                self._verify_shadow_local_source_owner(cursor, member)
            slot_id = uuid4()
            cursor.execute(
                """
                INSERT INTO runtime.command_slots
                    (command_slot_id, job_id, idempotency_key, command_name, request_hash,
                     execution_kind, state)
                VALUES (%s, %s, %s, %s, %s, %s, 'running')
                """,
                (
                    slot_id, job_id, claim.idempotency_key, claim.command_name,
                    claim.request_hash, claim.execution_kind,
                ),
            )
            if job_state == "pending":
                cursor.execute("UPDATE runtime.jobs SET state = 'running' WHERE job_id = %s", (job_id,))
            attempt_id = uuid4()
            cursor.execute(
                """
                INSERT INTO runtime.shadow_local_calibration_measurement_attempts
                    (attempt_id, command_slot_id, job_id, plan_hash, attempt_ordinal,
                     state, version, plan_json)
                VALUES (%s, %s, %s, %s, 1, 'prepared', 0, %s::jsonb)
                """,
                (attempt_id, slot_id, job_id, plan.claim.request_hash, plan.canonical_plan_json),
            )
            for member in plan.members:
                self._insert_shadow_local_member(cursor, attempt_id, member, state="pending")
            created = self._read_shadow_local_attempt_by_id(cursor, attempt_id)
            if created is None:
                raise RuntimeStoreError("shadow-local attempt vanished after reservation")
            return created

        return self._transaction(operation)

    def read_shadow_local_measurement_attempt(
        self, attempt_id: UUID
    ) -> ShadowLocalMeasurementAttempt:
        """Read one exact durable local attempt without changing its lease state."""

        self._validate_uuid(attempt_id, "shadow-local attempt_id")

        def operation(cursor: DbCursor) -> ShadowLocalMeasurementAttempt:
            attempt = self._read_shadow_local_attempt_by_id(cursor, attempt_id)
            if attempt is None:
                raise StoreValidationError("shadow-local attempt_id is unknown")
            return attempt

        return self._transaction(operation)

    def acquire_shadow_local_measurement_member_lease(
        self, attempt_id: UUID, case_sha256: str, *, expected_version: int
    ) -> ShadowLocalMeasurementMemberLease | None:
        """CAS-record an invocation lease before any local service dispatch."""

        self._validate_uuid(attempt_id, "shadow-local attempt_id")
        self._validate_sha256(case_sha256, "shadow-local case")
        self._validate_nonnegative_version(expected_version, "shadow-local member expected_version")
        token = str(uuid4())

        def operation(cursor: DbCursor) -> ShadowLocalMeasurementMemberLease | None:
            attempt = self._locked_shadow_local_attempt(cursor, attempt_id)
            if attempt.outcome.state != "running" or attempt.state not in ("prepared", "collecting"):
                return None
            cursor.execute(
                """
                SELECT attempt_id FROM runtime.shadow_local_calibration_measurement_attempts
                 WHERE command_slot_id = %s AND attempt_ordinal > %s FOR KEY SHARE
                """,
                (attempt.command_slot_id, attempt.attempt_ordinal),
            )
            if cursor.fetchone() is not None:
                return None
            member = self._locked_shadow_local_member(cursor, attempt_id, case_sha256)
            if member is None or member.state != "pending" or member.version != expected_version:
                return None
            cursor.execute(
                """
                SELECT member_ordinal, state
                  FROM runtime.shadow_local_calibration_measurement_members
                 WHERE attempt_id = %s AND member_ordinal < %s
                 ORDER BY member_ordinal FOR UPDATE
                """,
                (attempt_id, member.member_ordinal),
            )
            while (prefix := cursor.fetchone()) is not None:
                if _text(prefix[1]) != "staged":
                    return None
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=SHADOW_MEASUREMENT_LEASE_SECONDS)
            cursor.execute(
                """
                UPDATE runtime.shadow_local_calibration_measurement_members
                   SET state = 'invoking', lease_token = %s, lease_expires_at = %s,
                       version = version + 1
                 WHERE attempt_id = %s AND case_sha256 = %s
                   AND state = 'pending' AND version = %s
                """,
                (token, expires_at, attempt_id, case_sha256, expected_version),
            )
            if cursor.rowcount != 1:
                return None
            self._transition_shadow_local_attempt(cursor, attempt, "collecting")
            current = self._read_shadow_local_attempt_by_id(cursor, attempt_id)
            if current is None:
                raise RuntimeStoreError("shadow-local attempt vanished after member lease")
            leased = next(item for item in current.members if item.case_sha256 == case_sha256)
            return ShadowLocalMeasurementMemberLease(leased, current.version, token)

        return self._transaction(operation)

    def materialize_shadow_local_measurement_source(
        self,
        attempt_id: UUID,
        case_sha256: str,
        *,
        limits: MaterializationLimits,
    ) -> VerifiedMaterializedBlob:
        """Materialize only the exact source bound to a pending local member.

        The command calls this before its durable invocation lease.  The locked
        pending row closes the source owner without claiming that any provider
        dispatch has begun.
        """

        self._validate_uuid(attempt_id, "shadow-local attempt_id")
        self._validate_sha256(case_sha256, "shadow-local case")
        if type(limits) is not MaterializationLimits:  # noqa: E721
            raise StoreValidationError("shadow-local source materialization requires exact limits")

        def operation(cursor: DbCursor) -> tuple[Job, BlobRef]:
            attempt = self._locked_shadow_local_attempt(cursor, attempt_id)
            if attempt.outcome.state != "running":
                raise CommandStateError("shadow-local source materialization requires a running command")
            member = self._locked_shadow_local_member(cursor, attempt_id, case_sha256)
            if member is None or member.state != "pending":
                raise CommandStateError("shadow-local source materialization requires a pending member")
            return self._verify_shadow_local_source_owner_from_member(cursor, member)

        source_job, source_blob = self._transaction(operation)
        return self.materialize_immutable_blob(source_job, source_blob, limits)

    def stage_shadow_local_measurement_member_response(
        self,
        attempt_id: UUID,
        case_sha256: str,
        *,
        expected_version: int,
        lease_token: str,
        staged: ShadowLocalMeasurementStagedResponse,
    ) -> ShadowLocalMeasurementAttempt:
        """Atomically attach raw local bytes and independently replayed evidence."""

        self._validate_uuid(attempt_id, "shadow-local attempt_id")
        self._validate_sha256(case_sha256, "shadow-local case")
        self._validate_nonnegative_version(expected_version, "shadow-local member expected_version")
        if type(staged) is not ShadowLocalMeasurementStagedResponse:  # noqa: E721
            raise StoreValidationError("shadow-local stage requires an exact staged response")

        def operation(cursor: DbCursor) -> ShadowLocalMeasurementAttempt:
            attempt = self._locked_shadow_local_attempt(cursor, attempt_id)
            member = self._locked_shadow_local_member(cursor, attempt_id, case_sha256)
            if member is None:
                raise StoreValidationError("shadow-local stage member is unknown")
            if member.state == "staged":
                if (
                    member.raw_blob is None
                    or member.raw_blob.content_hash != staged.content_hash
                    or member.raw_blob.byte_length != len(staged.raw_bytes)
                    or member.raw_blob.media_type != staged.media_type
                    or member.evidence_json != staged.evidence_json
                ):
                    raise IdempotencyConflictError("shadow-local staged member cannot be substituted")
                return attempt
            if member.state != "invoking" or member.version != expected_version:
                raise CommandStateError("shadow-local stage member is stale or not invoking")
            self._require_shadow_local_member_lease(cursor, member, lease_token, "stage")
            reference = self._put_shadow_local_blob(
                cursor, attempt.job, staged.raw_bytes, staged.content_hash, staged.media_type
            )
            cursor.execute(
                """
                UPDATE runtime.shadow_local_calibration_measurement_members
                   SET state = 'staged', lease_token = NULL, lease_expires_at = NULL,
                       raw_blob_object_id = %s, raw_content_hash = %s, raw_byte_length = %s,
                       raw_media_type = %s, evidence_json = %s::jsonb, version = version + 1
                 WHERE attempt_id = %s AND case_sha256 = %s
                   AND state = 'invoking' AND version = %s AND lease_token = %s
                   AND lease_expires_at > transaction_timestamp()
                """,
                (
                    reference.object_id, reference.content_hash, reference.byte_length, reference.media_type,
                    staged.evidence_json, attempt_id, case_sha256, expected_version, lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise CommandStateError("shadow-local stage lease was lost")
            cursor.execute(
                "SELECT count(*) FROM runtime.shadow_local_calibration_measurement_members "
                "WHERE attempt_id = %s AND state = 'staged'",
                (attempt_id,),
            )
            all_staged = int(_text(cursor.fetchone()[0])) == len(attempt.members)  # type: ignore[index]
            self._transition_shadow_local_attempt(cursor, attempt, "ready" if all_staged else "collecting")
            current = self._read_shadow_local_attempt_by_id(cursor, attempt_id)
            if current is None:
                raise RuntimeStoreError("shadow-local attempt vanished after staging")
            return current

        return self._transaction(operation)

    def stage_shadow_local_measurement_not_started(
        self,
        attempt_id: UUID,
        case_sha256: str,
        *,
        expected_version: int,
        lease_token: str,
        proof: ShadowLocalMeasurementNotStartedProof,
    ) -> ShadowLocalMeasurementAttempt:
        """Durably prove BUSY-before-dispatch without inventing an unknown result."""

        self._validate_uuid(attempt_id, "shadow-local attempt_id")
        self._validate_sha256(case_sha256, "shadow-local case")
        self._validate_nonnegative_version(expected_version, "shadow-local member expected_version")
        if type(proof) is not ShadowLocalMeasurementNotStartedProof:  # noqa: E721
            raise StoreValidationError("shadow-local BUSY stage requires an exact proof")

        def operation(cursor: DbCursor) -> ShadowLocalMeasurementAttempt:
            attempt = self._locked_shadow_local_attempt(cursor, attempt_id)
            member = self._locked_shadow_local_member(cursor, attempt_id, case_sha256)
            if member is None:
                raise StoreValidationError("shadow-local BUSY member is unknown")
            if member.state == "not_started":
                if (
                    member.busy_proof_blob is None
                    or member.busy_proof_blob.content_hash != proof.content_hash
                    or member.busy_proof_blob.byte_length != len(proof.raw_bytes)
                    or member.busy_proof_blob.media_type != proof.media_type
                    or member.busy_proof_json != proof.proof_json
                ):
                    raise IdempotencyConflictError("shadow-local BUSY proof cannot be substituted")
                return attempt
            if member.state != "invoking" or member.version != expected_version:
                raise CommandStateError("shadow-local BUSY member is stale or not invoking")
            self._require_shadow_local_member_lease(cursor, member, lease_token, "stage BUSY proof")
            payload = _strict_json_object(proof.proof_json, "shadow-local BUSY proof")
            if (
                payload.get("request_sha256") != member.request_sha256
                or payload.get("binding_sha256") != member.binding_sha256
                or payload.get("service_profile_sha256") != member.service_profile_sha256
            ):
                raise StoreValidationError("shadow-local BUSY proof does not bind the leased member")
            reference = self._put_shadow_local_blob(
                cursor, attempt.job, proof.raw_bytes, proof.content_hash, proof.media_type
            )
            cursor.execute(
                """
                UPDATE runtime.shadow_local_calibration_measurement_members
                   SET state = 'not_started', lease_token = NULL, lease_expires_at = NULL,
                       busy_proof_blob_object_id = %s, busy_proof_content_hash = %s,
                       busy_proof_byte_length = %s, busy_proof_media_type = %s,
                       busy_proof_json = %s::jsonb, version = version + 1
                 WHERE attempt_id = %s AND case_sha256 = %s
                   AND state = 'invoking' AND version = %s AND lease_token = %s
                   AND lease_expires_at > transaction_timestamp()
                """,
                (
                    reference.object_id, reference.content_hash, reference.byte_length, reference.media_type,
                    proof.proof_json, attempt_id, case_sha256, expected_version, lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise CommandStateError("shadow-local BUSY proof lease was lost")
            # BUSY is a definitive pre-dispatch proof, not a native unknown.
            # The attempt becomes recoverable only so an explicitly authorized
            # REQUEST_NOT_STARTED successor can resume this exact member.
            self._transition_shadow_local_attempt(cursor, attempt, "indeterminate")
            current = self._read_shadow_local_attempt_by_id(cursor, attempt_id)
            if current is None:
                raise RuntimeStoreError("shadow-local attempt vanished after BUSY proof")
            return current

        return self._transaction(operation)

    def acquire_shadow_local_measurement_recovery_lease(
        self, attempt_id: UUID, *, expected_version: int
    ) -> ShadowLocalMeasurementRecoveryLease | None:
        """CAS-acquire recovery for an active local collection attempt."""

        self._validate_uuid(attempt_id, "shadow-local attempt_id")
        self._validate_nonnegative_version(expected_version, "shadow-local recovery expected_version")
        token = str(uuid4())

        def operation(cursor: DbCursor) -> ShadowLocalMeasurementRecoveryLease | None:
            attempt = self._locked_shadow_local_attempt(cursor, attempt_id)
            if (
                attempt.version != expected_version
                or attempt.outcome.state != "running"
                or attempt.state != "collecting"
            ):
                return None
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=SHADOW_MEASUREMENT_LEASE_SECONDS)
            cursor.execute(
                """
                UPDATE runtime.shadow_local_calibration_measurement_attempts
                   SET recovery_lease_token = %s, recovery_lease_expires_at = %s, version = version + 1
                 WHERE attempt_id = %s AND version = %s AND state = 'collecting'
                   AND (recovery_lease_expires_at IS NULL
                        OR recovery_lease_expires_at <= transaction_timestamp())
                """,
                (token, expires_at, attempt_id, expected_version),
            )
            if cursor.rowcount != 1:
                return None
            current = self._read_shadow_local_attempt_by_id(cursor, attempt_id)
            if current is None:
                raise RuntimeStoreError("shadow-local attempt vanished after recovery lease")
            return ShadowLocalMeasurementRecoveryLease(current, token)

        return self._transaction(operation)

    def mark_shadow_local_measurement_member_indeterminate(
        self,
        attempt_id: UUID,
        case_sha256: str,
        *,
        expected_version: int,
        recovery_lease_token: str,
    ) -> ShadowLocalMeasurementAttempt:
        """Record one expired invocation as unknown; never silently re-dispatch it."""

        self._validate_uuid(attempt_id, "shadow-local attempt_id")
        self._validate_sha256(case_sha256, "shadow-local case")
        self._validate_nonnegative_version(expected_version, "shadow-local member expected_version")
        if type(recovery_lease_token) is not str or not recovery_lease_token.strip():  # noqa: E721
            raise StoreValidationError("shadow-local recovery lease_token is required")

        def operation(cursor: DbCursor) -> ShadowLocalMeasurementAttempt:
            attempt = self._locked_shadow_local_attempt(cursor, attempt_id)
            self._require_shadow_local_recovery_lease(cursor, attempt, recovery_lease_token, "mark indeterminate")
            member = self._locked_shadow_local_member(cursor, attempt_id, case_sha256)
            if member is None or member.state != "invoking" or member.version != expected_version:
                raise CommandStateError("shadow-local indeterminate member is stale or not invoking")
            if member.lease_expires_at is None or member.lease_expires_at > datetime.now(timezone.utc):
                raise CommandStateError("shadow-local member invocation lease has not expired")
            cursor.execute(
                """
                UPDATE runtime.shadow_local_calibration_measurement_members
                   SET state = 'indeterminate', lease_token = NULL, lease_expires_at = NULL,
                       version = version + 1
                 WHERE attempt_id = %s AND case_sha256 = %s
                   AND state = 'invoking' AND version = %s
                   AND lease_expires_at <= transaction_timestamp()
                """,
                (attempt_id, case_sha256, expected_version),
            )
            if cursor.rowcount != 1:
                raise CommandStateError("shadow-local indeterminate member CAS was lost")
            cursor.execute(
                """
                UPDATE runtime.shadow_local_calibration_measurement_attempts
                   SET state = 'indeterminate', version = version + 1
                 WHERE attempt_id = %s AND version = %s AND state = 'collecting'
                   AND recovery_lease_token = %s
                   AND recovery_lease_expires_at > transaction_timestamp()
                """,
                (attempt_id, attempt.version, recovery_lease_token),
            )
            if cursor.rowcount != 1:
                raise CommandStateError("shadow-local indeterminate recovery lease was lost")
            current = self._read_shadow_local_attempt_by_id(cursor, attempt_id)
            if current is None:
                raise RuntimeStoreError("shadow-local attempt vanished after indeterminate transition")
            return current

        return self._transaction(operation)

    def reserve_shadow_local_measurement_successor(
        self,
        previous_attempt_id: UUID,
        authorization: ShadowLocalMeasurementRetryAuthorization,
    ) -> ShadowLocalMeasurementAttempt:
        """Reserve the one authorized same-slot successor of an unknown local invocation."""

        self._validate_uuid(previous_attempt_id, "shadow-local previous_attempt_id")
        if type(authorization) is not ShadowLocalMeasurementRetryAuthorization:  # noqa: E721
            raise StoreValidationError("shadow-local successor requires exact retry authorization")

        def operation(cursor: DbCursor) -> ShadowLocalMeasurementAttempt:
            previous = self._locked_shadow_local_attempt(cursor, previous_attempt_id)
            if previous.state != "indeterminate" or previous.outcome.state != "running":
                raise CommandStateError("only a running indeterminate shadow-local attempt may have a successor")
            if (
                authorization.predecessor_attempt_id != previous.attempt_id
                or authorization.predecessor_plan_hash != previous.plan_hash
                or authorization.predecessor_version != previous.version
                or authorization.next_attempt_ordinal != previous.attempt_ordinal + 1
            ):
                raise StoreValidationError("shadow-local retry authorization does not bind the predecessor")
            if authorization.next_attempt_ordinal > self._shadow_local_max_attempt_count(
                previous.canonical_plan_json
            ):
                raise CommandStateError("shadow-local retry exceeds the frozen max_attempt_count")
            if previous.recovery_lease_expires_at is not None and previous.recovery_lease_expires_at > datetime.now(timezone.utc):
                raise CommandStateError("shadow-local successor is blocked by an active recovery lease")
            authorized = next(
                (member for member in previous.members if member.case_sha256 == authorization.member_case_sha256),
                None,
            )
            if authorized is None or (
                authorization.reason_code == "NATIVE_OUTCOME_UNKNOWN" and authorized.state != "indeterminate"
            ) or (
                authorization.reason_code == "REQUEST_NOT_STARTED" and authorized.state != "not_started"
            ):
                raise StoreValidationError("shadow-local retry authorization target is not recoverable")
            if any(member.state not in ("staged", "pending", "indeterminate", "not_started") for member in previous.members):
                raise CommandStateError("shadow-local successor requires no concurrent invocation")
            cursor.execute(
                """
                SELECT attempt_id FROM runtime.shadow_local_calibration_measurement_attempts
                 WHERE job_id = %s AND plan_hash = %s AND attempt_ordinal = %s FOR UPDATE
                """,
                (previous.outcome.job_id, previous.plan_hash, authorization.next_attempt_ordinal),
            )
            existing = cursor.fetchone()
            if existing is not None:
                existing_id = UUID(str(existing[0]))
                cursor.execute(
                    """
                    SELECT command_slot_id, job_id, plan_hash, attempt_ordinal, previous_attempt_id,
                           retry_decision_reference_sha256, retry_member_case_sha256,
                           retry_predecessor_version, retry_reason_code
                      FROM runtime.shadow_local_calibration_measurement_attempts
                     WHERE attempt_id = %s FOR KEY SHARE
                    """,
                    (existing_id,),
                )
                replay = cursor.fetchone()
                if replay is None:
                    raise RuntimeStoreError("shadow-local successor vanished during replay validation")
                (
                    replay_slot, replay_job, replay_plan, replay_ordinal, replay_previous,
                    replay_decision, replay_case, replay_version, replay_reason,
                ) = replay
                if (
                    UUID(str(replay_slot)) != previous.command_slot_id
                    or UUID(str(replay_job)) != previous.outcome.job_id
                    or _text(replay_plan) != previous.plan_hash
                    or int(_text(replay_ordinal)) != authorization.next_attempt_ordinal
                    or UUID(str(replay_previous)) != previous.attempt_id
                    or _text(replay_decision) != authorization.decision_reference_sha256
                    or _text(replay_case) != authorization.member_case_sha256
                    or int(_text(replay_version)) != authorization.predecessor_version
                    or _text(replay_reason) != authorization.reason_code
                ):
                    raise IdempotencyConflictError(
                        "shadow-local successor ordinal was reserved by a different authorization"
                    )
                found = self._read_shadow_local_attempt_by_id(cursor, existing_id)
                if found is None:
                    raise RuntimeStoreError("shadow-local successor vanished after replay read")
                return found
            attempt_id = uuid4()
            cursor.execute(
                """
                INSERT INTO runtime.shadow_local_calibration_measurement_attempts
                    (attempt_id, command_slot_id, job_id, plan_hash, attempt_ordinal, previous_attempt_id,
                     state, version, plan_json, retry_decision_reference_sha256,
                     retry_member_case_sha256, retry_predecessor_version, retry_reason_code)
                VALUES (%s, %s, %s, %s, %s, %s, 'prepared', 0, %s::jsonb, %s, %s, %s, %s)
                """,
                (
                    attempt_id, previous.command_slot_id, previous.outcome.job_id, previous.plan_hash,
                    authorization.next_attempt_ordinal, previous.attempt_id, previous.canonical_plan_json,
                    authorization.decision_reference_sha256, authorization.member_case_sha256,
                    authorization.predecessor_version, authorization.reason_code,
                ),
            )
            for member in previous.members:
                self._insert_shadow_local_member(
                    cursor,
                    attempt_id,
                    ShadowLocalMeasurementMemberPlan(
                        member.member_ordinal, member.case_sha256, member.request_sha256,
                        member.canonical_case_json, member.canonical_request_json, member.source_job_id,
                        member.source_blob, member.source_blob_reference_sha256, member.binding_sha256,
                        member.service_profile_sha256, member.max_response_bytes,
                    ),
                    state="staged" if member.state == "staged" else "pending",
                    staged=member if member.state == "staged" else None,
                )
            successor = self._read_shadow_local_attempt_by_id(cursor, attempt_id)
            if successor is None:
                raise RuntimeStoreError("shadow-local successor vanished after reservation")
            return successor

        return self._transaction(operation)

    def finalize_shadow_local_measurement_success(
        self, attempt_id: UUID, *, expected_version: int
    ) -> CommandOutcome:
        """Atomically publish the local journal's exact two-member evidence set.

        This owner intentionally does not share the complete-source shadow
        finalizer: local evidence has a different plan grammar, raw-response
        claims and pure replay contract.
        """

        self._validate_uuid(attempt_id, "shadow-local attempt_id")
        self._validate_nonnegative_version(expected_version, "shadow-local finalizer expected_version")

        def operation(cursor: DbCursor) -> CommandOutcome:
            attempt = self._locked_shadow_local_attempt(cursor, attempt_id)
            job_id, slot_state, command_name, request_hash = self._locked_job_then_slot(
                cursor, attempt.command_slot_id
            )
            self._require_slot_execution_kind(cursor, attempt.command_slot_id, "deterministic")
            if (
                command_name != SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME
                or request_hash != attempt.plan_hash
                or attempt.job.profile != "shadow"
                or attempt.outcome.job_id != job_id
            ):
                raise CommandStateError("shadow-local finalizer lost its exact command identity")
            self._validate_shadow_local_attempt_chain(cursor, attempt)
            if slot_state != "running":
                if attempt.state != "committed":
                    raise CommandStateError("terminal local command is not backed by a committed journal")
                return self._replay_or_raise(
                    cursor, attempt.command_slot_id, job_id, "succeeded", None  # type: ignore[arg-type]
                )
            if (
                attempt.state != "ready"
                or attempt.version != expected_version
                or attempt.outcome.state != "running"
                or any(member.state != "staged" for member in attempt.members)
            ):
                raise CommandStateError("shadow-local finalizer requires the exact ready staged attempt")
            raw_responses = self._read_shadow_local_staged_responses(cursor, attempt, job_id)
            compiled = compile_shadow_local_measurement_artifacts(attempt, raw_responses)
            cursor.execute(
                """
                UPDATE runtime.shadow_local_calibration_measurement_attempts
                   SET state = 'committed', completed_at = transaction_timestamp(),
                       recovery_lease_token = NULL, recovery_lease_expires_at = NULL,
                       version = version + 1
                 WHERE attempt_id = %s AND state = 'ready' AND version = %s
                """,
                (attempt_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise CommandStateError("shadow-local finalizer CAS was lost")
            return self._write_success(
                cursor,
                CommandSuccess(
                    attempt.command_slot_id,
                    artifact_set_hash(compiled.artifacts),
                    compiled.artifacts,
                ),
                job_id,
            )

        return self._transaction(operation)

    def read_committed_shadow_local_measurement(
        self,
        job: Job,
        command_slot_id: UUID,
        receipt_id: UUID,
        artifact_set_id: UUID,
        *,
        expected_request_sha256: str,
    ) -> CommittedShadowLocalMeasurement:
        """Read one exact unaccepted local measurement after full durable closure."""

        if type(job) is not Job or job.profile != "shadow":  # noqa: E721
            raise StoreValidationError("local measurement reader requires its exact shadow Job")
        for value, field_name in (
            (command_slot_id, "local measurement command_slot_id"),
            (receipt_id, "local measurement receipt_id"),
            (artifact_set_id, "local measurement artifact_set_id"),
        ):
            self._validate_uuid(value, field_name)
        self._validate_sha256(expected_request_sha256, "local measurement expected request hash")
        if job.job_key != f"shadow-local:{expected_request_sha256.removeprefix('sha256:')}":
            raise StoreValidationError("local measurement Job does not match its expected request hash")

        def operation(cursor: DbCursor) -> CommittedShadowLocalMeasurement:
            actual_job, _job_state, command_name, request_hash, members = self._read_succeeded_set_members(
                cursor, receipt_id, artifact_set_id
            )
            summary = expected_request_sha256.removeprefix("sha256:")
            scope = ArtifactScope("autocut_calibration", "shadow_local_run", summary)
            if (
                actual_job != job
                or command_name != SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME
                or request_hash != expected_request_sha256
                or len(members) != 2
                or any(
                    member.command_slot_id != command_slot_id
                    or member.reference.scope != scope
                    or (member.reference.member_ordinal, member.reference.artifact_type,
                        member.reference.logical_id, member.reference.revision)
                    != expected
                    for member, expected in zip(
                        members,
                        (
                            (0, "shadow_local_measurement_manifest", f"shadow-local-measurement:{summary}:manifest", 1),
                            (1, "shadow_local_measurement_results", f"shadow-local-measurement:{summary}:results", 1),
                        ),
                        strict=True,
                    )
                )
            ):
                raise StoreValidationError("local measurement Receipt/Set does not name the exact artifact pair")
            cursor.execute(
                """
                SELECT attempt_id FROM runtime.shadow_local_calibration_measurement_attempts
                 WHERE command_slot_id = %s AND state = 'committed'
                """,
                (command_slot_id,),
            )
            row = cursor.fetchone()
            if row is None or cursor.fetchone() is not None:
                raise StoreValidationError("local measurement has no unique committed journal attempt")
            attempt = self._read_shadow_local_attempt_by_id(cursor, UUID(str(row[0])))
            if attempt is None or attempt.job != job or attempt.plan_hash != expected_request_sha256:
                raise StoreValidationError("local measurement committed journal identity does not close")
            self._validate_shadow_local_attempt_chain(cursor, attempt)
            validate_shadow_local_measurement_artifact_metadata(
                attempt, members[0].payload_json, members[1].payload_json
            )
            raw_responses = self._read_shadow_local_staged_responses(cursor, attempt, attempt.outcome.job_id)
            compiled = compile_shadow_local_measurement_artifacts(attempt, raw_responses)
            if (
                tuple(member.reference.content_hash for member in members)
                != (compiled.manifest_artifact.content_hash, compiled.results_artifact.content_hash)
                or tuple(member.payload_json for member in members)
                != (compiled.manifest_artifact.payload_json, compiled.results_artifact.payload_json)
            ):
                raise StoreValidationError("local measurement artifacts do not match independent raw replay")
            if attempt.outcome.job_id is None:
                raise RuntimeStoreError("local measurement journal lost its durable Job UUID")
            return CommittedShadowLocalMeasurement(
                attempt.attempt_id,
                attempt.outcome.job_id,
                command_slot_id,
                receipt_id,
                artifact_set_id,
                expected_request_sha256,
                compiled.manifest,
                compiled.results,
                compiled.report,
            )

        return self._transaction(operation)

    def finalize_shadow_measurement_success(
        self, attempt_id: UUID, *, expected_version: int
    ) -> CommandOutcome:
        """Write the only two calibration artifacts/Receipt from durably staged rows."""

        self._validate_uuid(attempt_id, "shadow attempt_id")
        self._validate_nonnegative_version(expected_version, "shadow finalizer expected_version")

        def operation(cursor: DbCursor) -> CommandOutcome:
            attempt = self._locked_shadow_attempt(cursor, attempt_id)
            if attempt.outcome.state != "running":
                if attempt.outcome.job_id is None:
                    raise RuntimeStoreError(
                        "persisted shadow measurement outcome is missing its Job identity"
                    )
                return self._replay_or_raise(
                    cursor, attempt.command_slot_id, attempt.outcome.job_id, "succeeded", None
                )
            if attempt.version != expected_version or attempt.state != "ready":
                raise CommandStateError("shadow measurement finalizer requires the exact ready attempt")
            if any(member.state != "staged" for member in attempt.members):
                raise CommandStateError("shadow measurement finalizer requires every member staged")
            artifacts = self._shadow_measurement_artifacts(attempt)
            cursor.execute(
                """
                UPDATE runtime.shadow_calibration_measurement_attempts
                   SET state = 'committed', completed_at = transaction_timestamp(),
                       recovery_lease_token = NULL, recovery_lease_expires_at = NULL, version = version + 1
                 WHERE attempt_id = %s AND version = %s AND state = 'ready'
                """,
                (attempt_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise CommandStateError("shadow measurement finalizer CAS was lost")
            job_id = attempt.outcome.job_id
            if job_id is None:
                raise RuntimeStoreError("shadow finalizer lost its durable Job identity")
            return self._write_success(
                cursor,
                CommandSuccess(attempt.command_slot_id, _shadow_artifact_set_hash(artifacts), artifacts),
                job_id,
            )

        return self._transaction(operation)

    def commit_shadow_measurement_terminal_denial(
        self, request: ShadowMeasurementTerminalDenialRequest
    ) -> ShadowMeasurementTerminalDenialResult:
        """Terminally deny one decoder-rejected native result without staging it.

        The caller is responsible for obtaining definitive decoder/domain proof.
        This Store boundary only accepts the two proof-bearing failure codes and
        refuses to close an aggregate once any durable evidence exists or an
        additional native invocation could still be outcome-unknown.
        """

        if type(request) is not ShadowMeasurementTerminalDenialRequest:  # noqa: E721
            raise StoreValidationError("shadow terminal denial requires an exact request")

        def operation(cursor: DbCursor) -> ShadowMeasurementTerminalDenialResult:
            attempt = self._locked_shadow_attempt(cursor, request.attempt_id)
            job_id, slot_state, command_name, request_hash = self._locked_job_then_slot(
                cursor, attempt.command_slot_id
            )
            self._require_slot_execution_kind(cursor, attempt.command_slot_id, "deterministic")
            if (
                attempt.command_slot_id != request.command_slot_id
                or attempt.job != request.job
                or attempt.plan_hash != request.plan_hash
                or request.command_name != SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME
                or command_name != SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME
                or request_hash != request.plan_hash
                or attempt.job.profile != "shadow"
            ):
                raise CommandStateError("shadow terminal denial identity does not match its attempt")
            if slot_state != "running":
                outcome = self._replay_or_raise(
                    cursor, attempt.command_slot_id, job_id, "denied", None
                )
                replay = self._read_shadow_attempt_by_id(cursor, attempt.attempt_id)
                if replay is None:
                    raise RuntimeStoreError("shadow terminal denial attempt vanished on replay")
                return ShadowMeasurementTerminalDenialResult(replay, outcome)
            if (
                attempt.state != "collecting"
                or attempt.version != request.expected_attempt_version
                or attempt.outcome.state != "running"
            ):
                raise CommandStateError("shadow terminal denial requires the exact collecting attempt")
            member = self._locked_shadow_member(
                cursor, attempt.attempt_id, request.member_reference_sha256
            )
            if (
                member is None
                or member.state != "invoking"
                or member.version != request.expected_member_version
            ):
                raise CommandStateError("shadow terminal denial requires the exact invoking member")
            self._require_shadow_member_lease(member, request.member_lease_token, "terminal denial")
            if any(
                candidate.state != "pending"
                for candidate in attempt.members
                if candidate.corpus_member_reference_sha256 != member.corpus_member_reference_sha256
            ):
                raise CommandStateError(
                    "shadow terminal denial is blocked by staged or outcome-unknown member evidence"
                )
            cursor.execute(
                """
                UPDATE runtime.shadow_calibration_measurement_members
                   SET state = 'indeterminate', lease_token = NULL, lease_expires_at = NULL,
                       version = version + 1
                 WHERE attempt_id = %s AND corpus_member_reference_sha256 = %s
                   AND state = 'invoking' AND version = %s AND lease_token = %s
                   AND lease_expires_at > transaction_timestamp()
                """,
                (
                    attempt.attempt_id,
                    member.corpus_member_reference_sha256,
                    request.expected_member_version,
                    request.member_lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise CommandStateError("shadow terminal denial member lease was lost")
            cursor.execute(
                """
                UPDATE runtime.shadow_calibration_measurement_attempts
                   SET state = 'indeterminate', recovery_lease_token = NULL,
                       recovery_lease_expires_at = NULL, version = version + 1
                 WHERE attempt_id = %s AND state = 'collecting' AND version = %s
                """,
                (attempt.attempt_id, request.expected_attempt_version),
            )
            if cursor.rowcount != 1:
                raise CommandStateError("shadow terminal denial attempt CAS was lost")
            self._write_rejection(
                cursor,
                CommandRejection(
                    attempt.command_slot_id,
                    request.failure_code,
                    request.failure_detail_json,
                    "denied",
                ),
                job_id,
            )
            denied = self._read_shadow_attempt_by_id(cursor, attempt.attempt_id)
            if denied is None:
                raise RuntimeStoreError("shadow terminal denial attempt vanished after closure")
            # Use the same durable Receipt representation as replay: JSONB text
            # need not preserve the submitted canonical JSON whitespace.
            return ShadowMeasurementTerminalDenialResult(denied, denied.outcome)

        return self._transaction(operation)

    # ------------------------------------------------------------------
    # commit_command_success
    # ------------------------------------------------------------------

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        """Atomically persist one non-empty immutable result set and success Receipt."""

        def operation(cursor: DbCursor) -> CommandOutcome:
            job_id, state, command_name, _ = self._locked_job_then_slot(
                cursor, success.command_slot_id
            )
            if command_name == CALIBRATION_VALIDATOR_COMMAND:
                raise CommandStateError(
                    "ValidateCalibrationRecord success requires the protected validator writer"
                )
            if command_name == "FinalizeRunOutcome":
                raise CommandStateError("FinalizeRunOutcome requires the explicit run finalizer API")
            if command_name == "GenerateVlmEvidenceCommand":
                raise CommandStateError(
                    "GenerateVlmEvidenceCommand success requires a committed generation attempt"
                )
            if command_name == VLM_BATCH_FINALIZER_COMMAND_NAME:
                raise CommandStateError(
                    "FinalizeVlmBatchCommand success requires the explicit VLM batch owner API"
                )
            if command_name == SOURCE_REUSE_COMMAND_NAME:
                raise CommandStateError(
                    "BindWholeSeriesSourcesCommand requires the explicit source reuse writer"
                )
            if command_name == SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME:
                raise CommandStateError(
                    "MeasureShadowCalibrationCommand@2.1.3 success requires the shadow owner API"
                )
            if command_name == SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME:
                raise CommandStateError(
                    "MeasureShadowLocalCalibrationCommand@1 success requires the local shadow owner API"
                )
            if command_name == PRODUCTION_RENDER_COMMAND_NAME:
                raise CommandStateError(
                    "RenderProductionRecipeCommand@1 success requires the render-attempt owner API"
                )
            if command_name == PRODUCTION_RECIPE_COMMAND_NAME:
                raise CommandStateError(
                    "CompileProductionRecipeCommand@1 success requires the Stage 4 owner API"
                )
            self._require_slot_execution_kind(cursor, success.command_slot_id, "deterministic")
            if state != "running":
                return self._replay_or_raise(
                    cursor, success.command_slot_id, job_id, "succeeded", success.set_hash
                )
            return self._write_success(cursor, success, job_id)

        return self._transaction(operation)

    def commit_production_recipe_success(
        self,
        verified: object,
    ) -> CommandOutcome:
        """Commit only a command-owned, independently verified Stage 4 closure."""

        # Lazy import preserves the Store/model import boundary while the
        # package-private opener verifies the process-local HMAC capability.
        from ..pipeline.compile_production_recipe_command import (
            CompileProductionRecipeError,
            _open_verified_production_recipe_commit,  # pyright: ignore[reportPrivateUsage]
        )

        try:
            success = _open_verified_production_recipe_commit(verified)
        except CompileProductionRecipeError as error:
            raise StoreValidationError(
                "Stage 4 success requires its verified command capability"
            ) from error

        def operation(cursor: DbCursor) -> CommandOutcome:
            job_id, state, command_name, _ = self._locked_job_then_slot(
                cursor,
                success.command_slot_id,
            )
            self._require_slot_execution_kind(
                cursor,
                success.command_slot_id,
                "deterministic",
            )
            if command_name != PRODUCTION_RECIPE_COMMAND_NAME:
                raise CommandStateError(
                    "only CompileProductionRecipeCommand@1 may commit Stage 4 output"
                )
            cursor.execute(
                "SELECT job_key, profile FROM runtime.jobs WHERE job_id = %s",
                (job_id,),
            )
            job_row = cursor.fetchone()
            if job_row is None or cursor.fetchone() is not None:
                raise RuntimeStoreError("Stage 4 command lost its durable Job")
            job = Job(_text(job_row[0]), cast(JobProfile, _text(job_row[1])))
            if success.artifacts[0].scope != canonical_recipe_scope(job):
                raise StoreValidationError(
                    "Stage 4 output must use the canonical Job scope"
                )
            if state != "running":
                return self._replay_or_raise(
                    cursor,
                    success.command_slot_id,
                    job_id,
                    "succeeded",
                    success.set_hash,
                )
            return self._write_success(cursor, success, job_id)

        return self._transaction(operation)

    def commit_source_reuse_success(
        self,
        success: CommandSuccess,
        *,
        binding: SourceReuseBinding,
    ) -> CommandOutcome:
        """Atomically grant target-Job claims for one exact SourcePrep result.

        Generic successful commands may not grant a Job access to immutable
        objects claimed by another Job. This dedicated writer is the only
        capability bridge and leaves the origin claim intact.
        """

        if type(binding) is not SourceReuseBinding:  # noqa: E721
            raise StoreValidationError("source reuse success requires an exact binding")
        if len(success.artifacts) != 2:
            raise StoreValidationError("source reuse success requires source and binding members")
        source, binding_member = success.artifacts
        expected_binding = binding.artifact(binding_member.revision)
        if (
            source.artifact_type != "whole_series_source_manifest"
            or source.logical_id != "whole_series_source_manifest"
            or source.scope != binding.target_scope
            or binding_member != expected_binding
        ):
            raise StoreValidationError("source reuse success members do not match its binding")
        if (
            source.payload_json != binding.origin.payload_json
            or source.content_hash != binding.origin.reference.content_hash
        ):
            raise StoreValidationError("source reuse cannot alter the origin source manifest")

        def operation(cursor: DbCursor) -> CommandOutcome:
            target_job_id, state, command_name, _ = self._locked_job_then_slot(
                cursor, success.command_slot_id
            )
            self._require_slot_execution_kind(cursor, success.command_slot_id, "deterministic")
            if command_name != SOURCE_REUSE_COMMAND_NAME:
                raise CommandStateError(
                    "only BindWholeSeriesSourcesCommand may grant source Blob claims"
                )
            cursor.execute(
                "SELECT job_key, profile FROM runtime.jobs WHERE job_id = %s",
                (target_job_id,),
            )
            target_row = cursor.fetchone()
            if target_row is None or (
                _text(target_row[0]) != binding.target_job.job_key
                or _text(target_row[1]) != binding.target_job.profile
            ):
                raise StoreValidationError("source reuse target Job does not own the command")
            if state != "running":
                return self._replay_or_raise(
                    cursor, success.command_slot_id, target_job_id, "succeeded", success.set_hash
                )
            origin = self._read_source_reuse_origin(cursor, binding)
            try:
                decoded = decode_source_manifest(origin.payload_json, origin.proxy_blobs)
            except SourceManifestDecodeError as error:
                raise StoreValidationError("source reuse origin manifest is invalid") from error
            if decoded.census.policy != binding.target_policy:
                raise StoreValidationError("source reuse target policy differs from origin policy")
            for position, blob in enumerate(origin.proxy_blobs):
                self._claimed_blob_ref(
                    cursor,
                    origin.job_id,
                    blob,
                    field_name=f"source reuse origin proxy[{position}]",
                )
                cursor.execute(
                    """
                    INSERT INTO storage.blob_claims (blob_claim_id, object_id, job_id)
                    VALUES (%s, %s, %s) ON CONFLICT (job_id, object_id) DO NOTHING
                    """,
                    (uuid4(), blob.object_id, target_job_id),
                )
            return self._write_success(cursor, success, target_job_id)

        return self._transaction(operation)

    def commit_calibration_record_validation_success(
        self,
        success: CommandSuccess,
        binding: CalibrationValidationBinding,
        record: CalibrationRecordArtifactSet,
    ) -> CommandOutcome:
        """Close the accepted record, Receipt, anchor and authority Job atomically."""

        self._validate_calibration_record_binding(binding, record)
        expected_members = tuple(
            ArtifactMember(
                member.artifact_type, member.logical_id, member.revision,
                ArtifactScope(member.scope.namespace, member.scope.kind, member.scope.key),
                member.content_hash, member.payload_json,
            )
            for member in record.members
        )
        if type(success) is not CommandSuccess or success.artifacts != expected_members:  # noqa: E721
            raise StoreValidationError("validator success must contain the exact typed record members")

        def operation(cursor: DbCursor) -> CommandOutcome:
            job_id, state, command_name, request_hash = self._locked_job_then_slot(
                cursor, success.command_slot_id
            )
            self._require_slot_execution_kind(cursor, success.command_slot_id, "deterministic")
            cursor.execute(
                "SELECT job.job_key, job.profile, slot.idempotency_key FROM runtime.jobs AS job "
                "JOIN runtime.command_slots AS slot ON slot.job_id = job.job_id "
                "WHERE job.job_id = %s AND slot.command_slot_id = %s",
                (job_id, success.command_slot_id),
            )
            row = cursor.fetchone()
            if (
                command_name != CALIBRATION_VALIDATOR_COMMAND
                or row is None
                or Job(_text(row[0]), cast(JobProfile, _text(row[1]))) != binding.job
            ):
                raise CommandStateError("validator writer requires its exact dedicated authority Job")
            if request_hash != binding.request_hash or _text(row[2]) != binding.attempt_idempotency_key:
                raise IdempotencyConflictError("validator slot does not bind the exact validation inputs")
            self._read_shadow_calibration_measurement(cursor, binding)
            if state != "running":
                outcome = self._replay_or_raise(
                    cursor, success.command_slot_id, job_id, "succeeded", success.set_hash
                )
                anchor = self._read_calibration_record_anchor(cursor, binding)
                if (
                    anchor.command_slot_id != success.command_slot_id
                    or anchor.aggregate.reference.receipt_id != outcome.receipt_id
                    or anchor.aggregate.reference.artifact_set_id != outcome.artifact_set_id
                    or anchor.record != record
                ):
                    raise IdempotencyConflictError("validator replay does not match its immutable anchor")
                return outcome
            self._assert_no_other_open_slots(cursor, job_id, success.command_slot_id)
            cursor.execute(
                "SELECT command_slot_id FROM runtime.calibration_record_anchors "
                "WHERE namespace = 'autocut_authority' AND scope_kind = 'calibration' AND scope_key = %s",
                (binding.profile_key,),
            )
            if cursor.fetchone() is not None:
                raise IdempotencyConflictError("calibration profile already has an immutable anchor")
            outcome = self._write_success(cursor, success, job_id)
            cursor.execute(
                """
                INSERT INTO runtime.calibration_record_anchors (
                    namespace, scope_kind, scope_key, record_sha256,
                    profile_source_sha256, registry_snapshot_sha256,
                    measurement_manifest_sha256, measurement_results_sha256,
                    asr_member_sha256, vad_member_sha256, validation_receipt_sha256,
                    receipt_id, artifact_set_id, aggregate_member_ordinal,
                    validation_member_ordinal, command_slot_id
                ) VALUES ('autocut_authority', 'calibration', %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, 0, 3, %s)
                """,
                (
                    binding.profile_key, record.members[0].content_hash,
                    binding.profile_source_sha256, binding.registry_snapshot_sha256,
                    binding.manifest_reference.content_hash, binding.results_reference.content_hash,
                    record.members[1].content_hash, record.members[2].content_hash,
                    record.members[3].content_hash, outcome.receipt_id, outcome.artifact_set_id,
                    success.command_slot_id,
                ),
            )
            if binding.runtime_measurement_identity is not None:
                identity = binding.runtime_measurement_identity
                cursor.execute(
                    """
                    INSERT INTO runtime.runtime_calibration_capabilities (
                        runtime_capability_id, timing_compatibility_sha256,
                        runtime_measurement_identity_sha256, build_audit_sha256,
                        measurement_identity_json, profile_source_sha256,
                        registry_snapshot_sha256, calibration_scope_key,
                        record_sha256, validation_receipt_sha256, receipt_id,
                        artifact_set_id, command_slot_id
                    ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        identity.runtime_capability_id,
                        identity.timing_compatibility_sha256,
                        identity.canonical_sha256,
                        identity.build_audit_sha256,
                        canonical_json_bytes(identity.to_mapping()).decode("utf-8"),
                        binding.profile_source_sha256,
                        binding.registry_snapshot_sha256,
                        binding.profile_key,
                        record.members[0].content_hash,
                        record.members[3].content_hash,
                        outcome.receipt_id,
                        outcome.artifact_set_id,
                        success.command_slot_id,
                    ),
                )
            cursor.execute(
                "UPDATE runtime.jobs SET state = 'succeeded' WHERE job_id = %s AND state = 'running'",
                (job_id,),
            )
            if cursor.rowcount != 1:
                raise CommandStateError("validator authority Job is not running")
            return outcome

        return self._transaction(operation)

    def read_runtime_calibration_capability(
        self,
        *,
        profile_source_sha256: str,
        registry_snapshot_sha256: str,
        measurement_identity: RuntimeMeasurementIdentity,
    ) -> PersistedRuntimeCalibrationCapability:
        """Read one exact v2 capability; v1 anchors are never an admission fallback."""
        self._validate_sha256(profile_source_sha256, "expected profile source hash")
        self._validate_sha256(registry_snapshot_sha256, "expected registry snapshot hash")
        if type(measurement_identity) is not RuntimeMeasurementIdentity:  # noqa: E721
            raise StoreValidationError("runtime capability reader requires exact measured identity")

        def operation(cursor: DbCursor) -> PersistedRuntimeCalibrationCapability:
            cursor.execute(
                """
                SELECT calibration_scope_key, record_sha256, validation_receipt_sha256,
                       receipt_id, artifact_set_id, command_slot_id,
                       measurement_identity_json::text
                  FROM runtime.runtime_calibration_capabilities
                 WHERE runtime_capability_id = %s
                   AND timing_compatibility_sha256 = %s
                   AND runtime_measurement_identity_sha256 = %s
                   AND profile_source_sha256 = %s
                   AND registry_snapshot_sha256 = %s
                """,
                (
                    measurement_identity.runtime_capability_id,
                    measurement_identity.timing_compatibility_sha256,
                    measurement_identity.canonical_sha256,
                    profile_source_sha256,
                    registry_snapshot_sha256,
                ),
            )
            rows: list[tuple[object, ...]] = []
            while (row := cursor.fetchone()) is not None:
                rows.append(row)
            if len(rows) != 1:
                cursor.execute(
                    """
                    SELECT 1
                      FROM runtime.runtime_calibration_capabilities
                     WHERE runtime_capability_id = %s
                     LIMIT 1
                    """,
                    (measurement_identity.runtime_capability_id,),
                )
                if cursor.fetchone() is not None:
                    raise RuntimeCalibrationIdentityMismatchError(
                        "accepted runtime capability differs from the current timing identity or authority lineage"
                    )
                raise MediaEvidenceUnavailableError("exact v2 runtime calibration capability is unavailable")
            row = rows[0]
            scope_key = _text(row[0])
            anchor = self._read_calibration_record_anchor_closure(
                cursor, scope_key, profile_source_sha256, registry_snapshot_sha256
            )
            if (
                anchor.record_sha256 != _text(row[1])
                or anchor.validation_receipt_sha256 != _text(row[2])
                or anchor.aggregate.reference.receipt_id != UUID(str(row[3]))
                or anchor.aggregate.reference.artifact_set_id != UUID(str(row[4]))
                or anchor.command_slot_id != UUID(str(row[5]))
            ):
                raise StoreValidationError("runtime capability does not close over its exact accepted record")
            try:
                decoded_identity_payload: object = json.loads(_text(row[6]))
                if type(decoded_identity_payload) is not dict:  # noqa: E721
                    raise StoreValidationError("runtime capability identity payload is not an object")
                identity_payload = cast(dict[str, object], decoded_identity_payload)
                runtime_capability_id = identity_payload.get("runtime_capability_id")
                timing_compatibility = identity_payload.get("timing_compatibility")
                if type(runtime_capability_id) is not str:  # noqa: E721
                    raise StoreValidationError(
                        "runtime capability identity runtime_capability_id is invalid"
                    )
                if type(timing_compatibility) is not dict:  # noqa: E721
                    raise StoreValidationError(
                        "runtime capability identity timing_compatibility is invalid"
                    )
                typed_timing_compatibility = cast(dict[str, object], timing_compatibility)
                persisted_identity = RuntimeMeasurementIdentity(
                    runtime_capability_id,
                    decode_timing_compatibility_profile(typed_timing_compatibility),
                )
                if (
                    persisted_identity.runtime_capability_id
                    != measurement_identity.runtime_capability_id
                    or persisted_identity.timing_compatibility_sha256
                    != measurement_identity.timing_compatibility_sha256
                    or persisted_identity.canonical_sha256 != measurement_identity.canonical_sha256
                ):
                    raise StoreValidationError("runtime capability identity payload differs")
            except (KeyError, TypeError, ValueError) as error:
                raise StoreValidationError("runtime capability identity payload is invalid") from error
            return PersistedRuntimeCalibrationCapability(persisted_identity, anchor)

        return self._transaction(operation)

    @staticmethod
    def _validate_calibration_record_binding(
        binding: CalibrationValidationBinding, record: CalibrationRecordArtifactSet
    ) -> None:
        if type(binding) is not CalibrationValidationBinding or type(record) is not CalibrationRecordArtifactSet:  # noqa: E721
            raise StoreValidationError("validator writer requires typed binding and accepted record")
        verify_calibration_record_artifact_set(record.members)
        if (
            record.members[0].scope.key != binding.profile_key
            or record.aggregate.identity.profile_source_sha256 != binding.profile_source_sha256
            or record.aggregate.identity.registry_snapshot_sha256 != binding.registry_snapshot_sha256
            or record.aggregate.measurement_manifest_sha256 != binding.manifest_reference.content_hash
            or record.aggregate.measurement_results_sha256 != binding.results_reference.content_hash
            or any(canonical_payload_hash(member.payload_json) != member.content_hash for member in record.members)
        ):
            raise StoreValidationError("accepted record does not match the exact validation binding")

    def commit_timed_speech_profile_bootstrap(
        self,
        success: CommandSuccess,
        snapshot: AuthorityRegistrySnapshot,
    ) -> CommandOutcome:
        """Commit the only authority registry writer and its anchor together."""

        if type(snapshot) is not AuthorityRegistrySnapshot:  # noqa: E721
            raise StoreValidationError("timed speech bootstrap requires an authority snapshot")
        if (
            len(success.artifacts) != 1
            or success.artifacts[0].scope != TIMED_SPEECH_PROFILE_REGISTRY_SCOPE
            or success.artifacts[0].artifact_type != TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE
            or success.artifacts[0].logical_id != snapshot.enabled_profile.logical_id
            or success.artifacts[0].revision != 1
        ):
            raise StoreValidationError("timed speech bootstrap has an invalid registry artifact")

        def operation(cursor: DbCursor) -> CommandOutcome:
            job_id, state, command_name, request_hash = self._locked_job_then_slot(
                cursor, success.command_slot_id
            )
            self._require_slot_execution_kind(cursor, success.command_slot_id, "deterministic")
            if command_name != BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND:
                raise CommandStateError("only the authority bootstrap command may commit a profile")
            cursor.execute("SELECT job_key, profile FROM runtime.jobs WHERE job_id = %s", (job_id,))
            job_row = cursor.fetchone()
            if job_row is None or Job(_text(job_row[0]), cast(JobProfile, _text(job_row[1]))) != AUTHORITY_BOOTSTRAP_JOB:
                raise CommandStateError("timed speech bootstrap lost its dedicated authority Job")
            artifact = success.artifacts[0]
            try:
                entry = decode_timed_speech_profile_registry_entry(
                    _strict_json_object(artifact.payload_json, "timed speech bootstrap payload")
                )
                key = TimedSpeechProfileKey(entry.profile_id, entry.profile_version)
            except (Stage4PredecessorError, TimedSpeechRegistryError) as error:
                raise StoreValidationError("timed speech bootstrap payload is invalid") from error
            if (
                key != snapshot.enabled_profile
                or artifact.content_hash != entry.canonical_hash
                or artifact.logical_id != key.logical_id
            ):
                raise StoreValidationError("timed speech bootstrap payload does not close")
            expected_request_hash = canonical_payload_hash(
                json.dumps(
                    {
                        "command": BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND,
                        "profile_key": key.value,
                        "profile_payload_sha256": entry.canonical_hash,
                        "registry_set_sha256": snapshot.registry_set_sha256,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            if request_hash != expected_request_hash:
                raise IdempotencyConflictError("bootstrap slot is not bound to the registry snapshot")
            if state != "running":
                replay = self._replay_or_raise(
                    cursor, success.command_slot_id, job_id, "succeeded", success.set_hash
                )
                self._read_bootstrapped_timed_speech_profile(cursor, snapshot)
                return replay
            cursor.execute(
                """
                SELECT registry_set_sha256, content_hash
                  FROM runtime.timed_speech_profile_anchors
                 WHERE profile_key = %s
                 FOR UPDATE
                """,
                (key.value,),
            )
            if cursor.fetchone() is not None:
                raise IdempotencyConflictError(
                    "timed speech profile is already anchored to another authority snapshot"
                )
            outcome = self._write_success(cursor, success, job_id)
            if outcome.receipt_id is None or outcome.artifact_set_id is None:
                raise StoreValidationError("bootstrap success lost its immutable result identity")
            cursor.execute(
                """
                INSERT INTO runtime.timed_speech_profile_anchors
                    (profile_key, registry_set_sha256, receipt_id, artifact_set_id,
                     member_ordinal, content_hash, command_slot_id)
                VALUES (%s, %s, %s, %s, 0, %s, %s)
                """,
                (
                    key.value,
                    snapshot.registry_set_sha256,
                    outcome.receipt_id,
                    outcome.artifact_set_id,
                    artifact.content_hash,
                    success.command_slot_id,
                ),
            )
            return outcome

        return self._transaction(operation)

    def commit_vlm_batch_success(self, success: CommandSuccess) -> CommandOutcome:
        """Commit the reserved VLM aggregate only after Store-owned closure checks."""

        if (
            len(success.artifacts) != 1
            or success.artifacts[0].artifact_type != "vlm_semantic_pack_set"
            or success.artifacts[0].logical_id != "vlm_semantic_pack_set"
        ):
            raise StoreValidationError(
                "VLM batch success requires exactly one vlm_semantic_pack_set member"
            )

        def operation(cursor: DbCursor) -> CommandOutcome:
            job_id, state, command_name, request_hash = self._locked_job_then_slot(
                cursor, success.command_slot_id
            )
            self._require_slot_execution_kind(cursor, success.command_slot_id, "deterministic")
            if command_name != VLM_BATCH_FINALIZER_COMMAND_NAME:
                raise CommandStateError(
                    "only FinalizeVlmBatchCommand may commit a VLM SemanticPackSet"
                )
            if state != "running":
                return self._replay_or_raise(
                    cursor,
                    success.command_slot_id,
                    job_id,
                    "succeeded",
                    success.set_hash,
                )
            cursor.execute(
                "SELECT job_key, profile FROM runtime.jobs WHERE job_id = %s",
                (job_id,),
            )
            job_row = cursor.fetchone()
            if job_row is None or cursor.fetchone() is not None:
                raise StoreValidationError("VLM batch command lost its durable Job")
            job = Job(_text(job_row[0]), cast(JobProfile, _text(job_row[1])))
            artifact = success.artifacts[0]
            if artifact.scope != canonical_recipe_scope(job):
                raise StoreValidationError("VLM SemanticPackSet has a non-canonical Job scope")
            decoded = _decode_registered_vlm_semantic_pack_set(artifact.payload_json)
            if _vlm_batch_request_hash(job, artifact, decoded) != request_hash:
                raise IdempotencyConflictError(
                    "VLM batch request hash does not bind its exact aggregate payload"
                )
            self._assert_vlm_batch_child_closure(cursor, job, decoded)
            return self._write_success(cursor, success, job_id)

        return self._transaction(operation)

    # ------------------------------------------------------------------
    # commit_command_rejection
    # ------------------------------------------------------------------

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome:
        """Atomically persist a terminal deny/fail Receipt without inventing an ArtifactSet."""

        def operation(cursor: DbCursor) -> CommandOutcome:
            job_id, state, command_name, _ = self._locked_job_then_slot(
                cursor, rejection.command_slot_id
            )
            if command_name == "FinalizeRunOutcome":
                raise CommandStateError("FinalizeRunOutcome requires the explicit run finalizer API")
            if command_name == "GenerateVlmEvidenceCommand":
                raise CommandStateError(
                    "GenerateVlmEvidenceCommand rejection requires the explicit generation API"
                )
            if command_name == VLM_BATCH_FINALIZER_COMMAND_NAME:
                raise CommandStateError(
                    "FinalizeVlmBatchCommand cannot use generic rejection"
                )
            if command_name == SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME:
                raise CommandStateError(
                    "MeasureShadowCalibrationCommand@2.1.3 cannot use generic rejection"
                )
            if command_name == SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME:
                raise CommandStateError(
                    "MeasureShadowLocalCalibrationCommand@1 cannot use generic rejection"
                )
            if command_name == PRODUCTION_RENDER_COMMAND_NAME:
                raise CommandStateError(
                    "RenderProductionRecipeCommand@1 rejection requires the render-attempt owner API"
                )
            self._require_slot_execution_kind(cursor, rejection.command_slot_id, "deterministic")
            if state != "running":
                return self._replay_or_raise(
                    cursor, rejection.command_slot_id, job_id, rejection.outcome, None
                )
            return self._write_rejection(cursor, rejection, job_id)

        return self._transaction(operation)

    # ------------------------------------------------------------------
    # explicit Job finalization
    # ------------------------------------------------------------------

    def finalize_run_success(self, success: CommandSuccess) -> CommandOutcome:
        """Commit the sole run_outcome artifact and terminalize its Job atomically."""

        if len(success.artifacts) != 1 or success.artifacts[0].artifact_type != "run_outcome":
            raise StoreValidationError(
                "successful run finalization requires exactly one run_outcome member"
            )

        def operation(cursor: DbCursor) -> CommandOutcome:
            job_id, state, command_name, _ = self._locked_job_then_slot(
                cursor, success.command_slot_id
            )
            self._require_slot_execution_kind(cursor, success.command_slot_id, "deterministic")
            if command_name != "FinalizeRunOutcome":
                raise CommandStateError("only FinalizeRunOutcome may terminalize a Job")
            if state != "running":
                return self._replay_or_raise(
                    cursor, success.command_slot_id, job_id, "succeeded", success.set_hash
                )
            self._assert_no_other_open_slots(cursor, job_id, success.command_slot_id)
            outcome = self._write_success(cursor, success, job_id)
            cursor.execute(
                "UPDATE runtime.jobs SET state = 'succeeded' WHERE job_id = %s AND state = 'running'",
                (job_id,),
            )
            return outcome

        return self._transaction(operation)

    def finalize_run_rejection(self, rejection: CommandRejection) -> CommandOutcome:
        """Commit a failed/denied finalizer receipt and terminalize its Job atomically."""

        def operation(cursor: DbCursor) -> CommandOutcome:
            job_id, state, command_name, _ = self._locked_job_then_slot(
                cursor, rejection.command_slot_id
            )
            self._require_slot_execution_kind(cursor, rejection.command_slot_id, "deterministic")
            if command_name != "FinalizeRunOutcome":
                raise CommandStateError("only FinalizeRunOutcome may terminalize a Job")
            if state != "running":
                return self._replay_or_raise(
                    cursor, rejection.command_slot_id, job_id, rejection.outcome, None
                )
            self._assert_no_other_open_slots(cursor, job_id, rejection.command_slot_id)
            outcome = self._write_rejection(cursor, rejection, job_id)
            cursor.execute(
                "UPDATE runtime.jobs SET state = %s WHERE job_id = %s AND state = 'running'",
                (rejection.outcome, job_id),
            )
            return outcome

        return self._transaction(operation)

    # ------------------------------------------------------------------
    # immutable provider blobs
    # ------------------------------------------------------------------

    def put_immutable_blob(
        self,
        job: Job,
        *,
        content: bytes,
        content_hash: str,
        media_type: str,
    ) -> BlobRef:
        """Verify immutable bytes, deduplicate the object, and claim it for ``job``."""

        if type(content) is not bytes:  # noqa: E721
            raise StoreValidationError("blob content must be immutable bytes")
        self._validate_sha256(content_hash, "blob.content_hash")
        if type(media_type) is not str or not media_type.strip():  # noqa: E721
            raise StoreValidationError("blob.media_type must be a non-empty string")
        actual_hash = "sha256:" + hashlib.sha256(content).hexdigest()
        if actual_hash != content_hash:
            raise BlobIntegrityError("blob bytes do not match the declared SHA-256")

        def operation(cursor: DbCursor) -> BlobRef:
            job_id = self._ensure_job(cursor, job)
            if self._locked_job_state(cursor, job_id) not in ("pending", "running"):
                raise CommandStateError("terminal jobs cannot claim new blob objects")
            object_id = uuid4()
            cursor.execute(
                """
                INSERT INTO storage.blob_objects
                    (object_id, content_hash, byte_length, media_type, content_bytes)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (content_hash) DO NOTHING
                RETURNING object_id, content_hash, byte_length, media_type
                """,
                (object_id, content_hash, len(content), media_type, content),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                cursor.execute(
                    """
                    SELECT object_id, content_hash, byte_length, media_type, content_bytes
                      FROM storage.blob_objects WHERE content_hash = %s FOR UPDATE
                    """,
                    (content_hash,),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise RuntimeStoreError("blob object vanished after deduplication conflict")
                existing_id, existing_hash, byte_length, existing_type, existing_bytes = existing
                if not isinstance(existing_bytes, (bytes, bytearray, memoryview)):
                    raise BlobIntegrityError("existing blob object returned invalid bytes")
                durable_bytes = (
                    existing_bytes.tobytes()
                    if isinstance(existing_bytes, memoryview)
                    else bytes(existing_bytes)
                )
                if (
                    _text(existing_hash) != content_hash
                    or int(_text(byte_length)) != len(content)
                    or _text(existing_type) != media_type
                    or durable_bytes != content
                ):
                    raise BlobIntegrityError("existing blob object does not match the exact bytes")
                reference = BlobRef(
                    UUID(str(existing_id)), content_hash, len(content), media_type
                )
            else:
                reference = BlobRef(
                    UUID(str(inserted[0])),
                    _text(inserted[1]),
                    int(_text(inserted[2])),
                    _text(inserted[3]),
                )
            cursor.execute(
                """
                INSERT INTO storage.blob_claims (blob_claim_id, object_id, job_id)
                VALUES (%s, %s, %s) ON CONFLICT (job_id, object_id) DO NOTHING
                """,
                (uuid4(), reference.object_id, job_id),
            )
            return reference

        return self._transaction(operation)

    def reserve_object_write(
        self,
        job: Job,
        intent: object,
        target: object,
    ) -> object:
        """Persist exact external-write expectations before any provider effect."""

        if type(intent) is not PendingObjectIntent:  # noqa: E721
            raise StoreValidationError("intent must be an exact PendingObjectIntent")
        if type(target) is not _PendingObjectTarget:  # noqa: E721
            raise StoreValidationError("target must be an exact Store object target")

        def operation(cursor: DbCursor) -> object:
            job_id = self._ensure_job(cursor, job)
            if self._locked_job_state(cursor, job_id) not in ("pending", "running"):
                raise CommandStateError("terminal jobs cannot reserve object writes")
            reservation_token = uuid4()
            cursor.execute(
                """
                INSERT INTO storage.object_write_intents (
                    object_id, job_id, content_hash, byte_length, media_type,
                    storage_backend_id, storage_region, storage_locator,
                    write_strategy, reservation_token, state, version
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'reserved', 0
                )
                ON CONFLICT (object_id) DO NOTHING
                RETURNING reservation_token, version
                """,
                (
                    intent.object_id,
                    job_id,
                    intent.content_hash,
                    intent.byte_length,
                    intent.media_type,
                    target.backend_id,
                    target.storage_region,
                    target.storage_locator,
                    target.strategy,
                    reservation_token,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                cursor.execute(
                    """
                    SELECT job_id, content_hash, byte_length, media_type,
                           storage_backend_id, storage_region, storage_locator,
                           write_strategy, reservation_token, state, version
                      FROM storage.object_write_intents
                     WHERE object_id = %s
                     FOR UPDATE
                    """,
                    (intent.object_id,),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise RuntimeStoreError(
                        "object write intent vanished after reservation conflict"
                    )
                (
                    existing_job_id,
                    content_hash,
                    byte_length,
                    media_type,
                    backend_id,
                    storage_region,
                    storage_locator,
                    write_strategy,
                    existing_token,
                    state,
                    version,
                ) = existing
                if (
                    UUID(str(existing_job_id)) != job_id
                    or _text(content_hash) != intent.content_hash
                    or int(_text(byte_length)) != intent.byte_length
                    or _text(media_type) != intent.media_type
                    or _text(backend_id) != target.backend_id
                    or _text(storage_region) != target.storage_region
                    or _text(storage_locator) != target.storage_locator
                    or _text(write_strategy) != target.strategy
                ):
                    raise PersistenceConflictError(
                        "object_id belongs to a different durable write intent"
                    )
                durable_state = _text(state)
                durable_version = int(_text(version))
                if (durable_state, durable_version) not in {
                    ("reserved", 0),
                    ("resolved", 1),
                }:
                    raise BlobIntegrityError(
                        "persisted object write intent has an invalid lifecycle"
                    )
                reservation_token = UUID(str(existing_token))
                version = 0
            else:
                reservation_token = UUID(str(inserted[0]))
                version = int(_text(inserted[1]))
            return _issue_pending_object_reservation(
                intent=intent,
                target=target,
                job_id=job_id,
                reservation_token=reservation_token,
                expected_version=int(_text(version)),
            )

        return self._transaction(operation)

    def claim_verified_object(
        self,
        job: Job,
        reservation: object,
        verified: object,
    ) -> BlobRef:
        """Atomically claim one independently verified external object for ``job``.

        The locator-bearing value is intentionally private to the Store package.
        Callers receive only the locator-free ``BlobRef`` selected by immutable
        content identity.
        """

        if type(reservation) is not _PendingObjectReservation:  # noqa: E721
            raise StoreValidationError(
                "reservation must be an exact Store-owned pending object reservation"
            )
        if type(verified) is not _VerifiedPendingObject:  # noqa: E721
            raise StoreValidationError(
                "verified must be an exact Store-owned verified pending object"
            )
        verifier = self._object_store_verifier
        if verifier is None:
            raise StoreValidationError(
                "external object claims require a configured object-store verifier"
            )
        if not verifier._verify_pending_object(  # pyright: ignore[reportPrivateUsage]
            reservation,
            verified,
        ):
            raise BlobIntegrityError(
                "verified object signature does not match the configured object adapter"
            )
        reference = verified.reference
        if (
            reference != reservation.intent.reference
            or verified.backend_id != reservation.target.backend_id
            or verified.storage_region != reservation.target.storage_region
            or verified.storage_locator != reservation.target.storage_locator
            or verified.strategy != reservation.target.strategy
            or verified.reservation_token != reservation.reservation_token
            or verified.reservation_version != reservation.expected_version
        ):
            raise BlobIntegrityError(
                "verified object does not match its durable pre-write reservation"
            )

        def operation(cursor: DbCursor) -> BlobRef:
            job_id = self._ensure_job(cursor, job)
            job_state = self._locked_job_state(cursor, job_id)
            cursor.execute(
                """
                SELECT job_id, content_hash, byte_length, media_type,
                       storage_backend_id, storage_region, storage_locator,
                       write_strategy, reservation_token, state, version,
                       resolved_object_id
                  FROM storage.object_write_intents
                 WHERE object_id = %s
                 FOR UPDATE
                """,
                (reference.object_id,),
            )
            persisted = cursor.fetchone()
            if persisted is None:
                raise BlobIntegrityError("verified object has no durable pre-write reservation")
            (
                persisted_job_id,
                content_hash,
                byte_length,
                media_type,
                backend_id,
                storage_region,
                storage_locator,
                write_strategy,
                reservation_token,
                state,
                version,
                resolved_object_id,
            ) = persisted
            if (
                UUID(str(persisted_job_id)) != job_id
                or reservation.job_id != job_id
                or UUID(str(reservation_token)) != reservation.reservation_token
                or _text(content_hash) != reference.content_hash
                or int(_text(byte_length)) != reference.byte_length
                or _text(media_type) != reference.media_type
                or _text(backend_id) != verified.backend_id
                or _text(storage_region) != verified.storage_region
                or _text(storage_locator) != verified.storage_locator
                or _text(write_strategy) != verified.strategy
            ):
                raise BlobIntegrityError(
                    "verified object does not match the persisted write reservation"
                )
            durable_state = _text(state)
            durable_version = int(_text(version))
            if durable_state == "resolved":
                if durable_version != reservation.expected_version + 1:
                    raise BlobIntegrityError(
                        "resolved object write intent has an invalid version"
                    )
                durable = self._locked_exact_blob_by_content_hash(cursor, reference)
                if resolved_object_id is None or durable.object_id != UUID(
                    str(resolved_object_id)
                ):
                    raise BlobIntegrityError(
                        "resolved object write intent points at a different blob"
                    )
                cursor.execute(
                    """
                    SELECT 1 FROM storage.blob_claims
                     WHERE job_id = %s AND object_id = %s
                    """,
                    (job_id, durable.object_id),
                )
                if cursor.fetchone() is None:
                    raise BlobIntegrityError(
                        "resolved object write intent is missing its durable Job claim"
                    )
                return durable
            if (
                durable_state != "reserved"
                or durable_version != reservation.expected_version
            ):
                raise CommandStateError("object write reservation is stale")
            if job_state not in ("pending", "running"):
                raise CommandStateError("terminal jobs cannot claim new blob objects")
            cursor.execute(
                """
                INSERT INTO storage.blob_objects (
                    object_id, content_hash, byte_length, media_type, content_bytes,
                    storage_kind, storage_backend_id, storage_region,
                    storage_locator, storage_etag, storage_version_id,
                    write_strategy, verified_at
                ) VALUES (
                    %s, %s, %s, %s, NULL,
                    's3_compatible', %s, %s, %s, %s, %s, %s,
                    transaction_timestamp()
                )
                ON CONFLICT (content_hash) DO NOTHING
                RETURNING object_id, content_hash, byte_length, media_type
                """,
                (
                    reference.object_id,
                    reference.content_hash,
                    reference.byte_length,
                    reference.media_type,
                    verified.backend_id,
                    verified.storage_region,
                    verified.storage_locator,
                    verified.etag,
                    verified.version_id,
                    verified.strategy,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                durable = self._locked_exact_blob_by_content_hash(cursor, reference)
            else:
                durable = BlobRef(
                    UUID(str(inserted[0])),
                    _text(inserted[1]),
                    int(_text(inserted[2])),
                    _text(inserted[3]),
                )
                if durable != reference:
                    raise BlobIntegrityError(
                        "inserted external blob metadata does not match the verified object"
                    )
            cursor.execute(
                """
                INSERT INTO storage.blob_claims (blob_claim_id, object_id, job_id)
                VALUES (%s, %s, %s) ON CONFLICT (job_id, object_id) DO NOTHING
                """,
                (uuid4(), durable.object_id, job_id),
            )
            cursor.execute(
                """
                UPDATE storage.object_write_intents
                   SET state = 'resolved', version = version + 1,
                       resolved_object_id = %s,
                       resolved_at = transaction_timestamp()
                 WHERE object_id = %s AND state = 'reserved' AND version = %s
                   AND reservation_token = %s
                """,
                (
                    durable.object_id,
                    reference.object_id,
                    reservation.expected_version,
                    reservation.reservation_token,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreConcurrencyError("object write reservation CAS was lost")
            return durable

        return self._transaction(operation)

    @staticmethod
    def _locked_exact_blob_by_content_hash(
        cursor: DbCursor,
        expected: BlobRef,
    ) -> BlobRef:
        cursor.execute(
            """
            SELECT object_id, content_hash, byte_length, media_type, content_bytes,
                   storage_kind, storage_backend_id, storage_region,
                   storage_locator, storage_etag, write_strategy, verified_at
              FROM storage.blob_objects
             WHERE content_hash = %s
             FOR UPDATE
            """,
            (expected.content_hash,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeStoreError("blob object vanished after deduplication conflict")
        (
            object_id,
            content_hash,
            byte_length,
            media_type,
            content_bytes,
            storage_kind,
            backend_id,
            storage_region,
            storage_locator,
            storage_etag,
            write_strategy,
            verified_at,
        ) = row
        durable = BlobRef(
            UUID(str(object_id)),
            _text(content_hash),
            int(_text(byte_length)),
            _text(media_type),
        )
        if (
            durable.content_hash != expected.content_hash
            or durable.byte_length != expected.byte_length
            or durable.media_type != expected.media_type
        ):
            raise BlobIntegrityError(
                "existing blob object does not match the verified content identity"
            )
        kind = _text(storage_kind)
        if kind == "postgres_inline":
            raise BlobIntegrityError(
                "external render content conflicts with an inline blob object"
            )
        elif kind == "s3_compatible":
            if content_bytes is not None or any(
                value is None
                for value in (
                    backend_id,
                    storage_region,
                    storage_locator,
                    storage_etag,
                    write_strategy,
                    verified_at,
                )
            ):
                raise BlobIntegrityError("existing external blob metadata has an invalid shape")
        else:
            raise BlobIntegrityError("existing blob object uses an unsupported storage kind")
        return durable

    # ------------------------------------------------------------------
    # durable production render attempts
    # ------------------------------------------------------------------

    def reserve_production_render_attempt(
        self,
        command_slot_id: UUID,
        request_hash: str,
        *,
        recipe: CommittedArtifactMemberReference,
        render_plan_sha256: str,
        render_profile_sha256: str,
        renderer_identity_sha256: str,
        execution_limits_sha256: str,
        max_output_bytes: int,
    ) -> ProductionRenderAttempt:
        """Reserve one exact Recipe and immutable render/execution identities.

        ``renderer_identity_sha256`` is retained for API compatibility and has
        one closed meaning: the canonical hash of
        ``ProductionFfmpegIdentity.to_mapping()`` supplied in the final facts.
        """

        self._validate_uuid(command_slot_id, "command_slot_id")
        self._validate_sha256(request_hash, "production render request_hash")
        if type(recipe) is not CommittedArtifactMemberReference:  # noqa: E721
            raise StoreValidationError(
                "production render recipe must be an exact committed member reference"
            )
        for field_name, value in (
            ("render_plan_sha256", render_plan_sha256),
            ("render_profile_sha256", render_profile_sha256),
            ("renderer_identity_sha256", renderer_identity_sha256),
            ("execution_limits_sha256", execution_limits_sha256),
        ):
            self._validate_sha256(value, f"production render {field_name}")
        if type(max_output_bytes) is not int or max_output_bytes <= 0:  # noqa: E721
            raise StoreValidationError(
                "production render max_output_bytes must be a positive integer"
            )

        def operation(cursor: DbCursor) -> ProductionRenderAttempt:
            job_id, slot_state, command_name, slot_request_hash = (
                self._locked_job_then_slot(cursor, command_slot_id)
            )
            self._require_slot_execution_kind(cursor, command_slot_id, "deterministic")
            if command_name != PRODUCTION_RENDER_COMMAND_NAME:
                raise CommandStateError(
                    "production render attempt requires its reserved render command"
                )
            if slot_state != "running":
                raise CommandStateError(
                    "production render command slot is already terminal"
                )
            if slot_request_hash != request_hash:
                raise IdempotencyConflictError(
                    "production render request hash differs from its command claim"
                )
            cursor.execute(
                "SELECT job_key, profile FROM runtime.jobs WHERE job_id = %s",
                (job_id,),
            )
            job_row = cursor.fetchone()
            if job_row is None or cursor.fetchone() is not None:
                raise RuntimeStoreError("production render Job identity is unavailable")
            if _text(job_row[1]) not in ("shadow", "production"):
                raise StoreValidationError(
                    "production render attempts require a shadow or production Job"
                )
            job = Job(
                _text(job_row[0]),
                cast(Literal["shadow", "production"], _text(job_row[1])),
            )
            try:
                committed = self._read_exact_committed_set(cursor, job, recipe)
            except (SemanticInputUnavailableError, SemanticInputIntegrityError) as error:
                raise StoreValidationError(
                    "production render Recipe authority is unavailable"
                ) from error
            if (
                committed.command_name != PRODUCTION_RECIPE_COMMAND_NAME
                or committed.execution_kind != "deterministic"
                or recipe.scope != canonical_recipe_scope(job)
                or recipe.artifact_type != "recipe"
                or not recipe.logical_id.startswith("production_recipe@")
                or recipe.member_ordinal <= 0
                or recipe.member_ordinal >= len(committed.members) - 1
                or committed.members[0][1].artifact_type
                != "physical_edit_compilation_report"
                or committed.members[-1][1].artifact_type
                != "physical_edit_admission"
                or any(
                    member.artifact_type != "recipe"
                    for _, member in committed.members[1:-1]
                )
            ):
                raise StoreValidationError(
                    "production render Recipe is not an exact admitted Stage 4 member"
                )
            cursor.execute(
                """
                SELECT attempt_id
                  FROM runtime.production_render_attempts
                 WHERE command_slot_id = %s
                 FOR UPDATE
                """,
                (command_slot_id,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                attempt = self._read_production_render_attempt_by_id(
                    cursor,
                    UUID(str(existing[0])),
                    for_update=False,
                ).attempt
                if (
                    attempt.request_hash != request_hash
                    or attempt.recipe != recipe
                    or attempt.render_plan_sha256 != render_plan_sha256
                    or attempt.render_profile_sha256 != render_profile_sha256
                    or attempt.renderer_identity_sha256
                    != renderer_identity_sha256
                    or attempt.execution_limits_sha256
                    != execution_limits_sha256
                    or attempt.max_output_bytes != max_output_bytes
                ):
                    raise IdempotencyConflictError(
                        "production render slot was reserved with a different identity"
                    )
                return attempt
            attempt_id = uuid4()
            cursor.execute(
                """
                INSERT INTO runtime.production_render_attempts (
                    attempt_id, job_id, command_slot_id, request_hash,
                    recipe_receipt_id, recipe_artifact_set_id,
                    recipe_member_ordinal, recipe_namespace,
                    recipe_scope_kind, recipe_scope_key, recipe_artifact_type,
                    recipe_logical_id, recipe_revision, recipe_content_hash,
                    render_plan_sha256, render_profile_sha256,
                    renderer_identity_sha256, execution_limits_sha256,
                    max_output_bytes,
                    state, version
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    'reserved', 0
                )
                """,
                (
                    attempt_id,
                    job_id,
                    command_slot_id,
                    request_hash,
                    recipe.receipt_id,
                    recipe.artifact_set_id,
                    recipe.member_ordinal,
                    recipe.scope.namespace,
                    recipe.scope.kind,
                    recipe.scope.key,
                    recipe.artifact_type,
                    recipe.logical_id,
                    recipe.revision,
                    recipe.content_hash,
                    render_plan_sha256,
                    render_profile_sha256,
                    renderer_identity_sha256,
                    execution_limits_sha256,
                    max_output_bytes,
                ),
            )
            return replace(
                self._read_production_render_attempt_by_id(
                    cursor,
                    attempt_id,
                    for_update=False,
                ).attempt,
                is_fresh_reservation=True,
            )

        return self._transaction(operation)

    def acquire_production_render_lease(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        lease_seconds: int,
    ) -> ProductionRenderLease | None:
        """Acquire a reservation or take over only an expired render lease."""

        self._validate_uuid(attempt_id, "attempt_id")
        self._validate_nonnegative_version(expected_version, "expected_version")
        self._validate_render_lease_seconds(lease_seconds)
        token = uuid4()

        def operation(cursor: DbCursor) -> ProductionRenderLease | None:
            record, _, slot_state = self._locked_production_render_attempt_aggregate(
                cursor,
                attempt_id,
            )
            attempt = record.attempt
            if attempt.version != expected_version:
                raise CommandStateError("production render lease version is stale")
            if slot_state != "running":
                raise CommandStateError("production render command cannot acquire a lease")
            if attempt.state not in ("reserved", "rendering"):
                raise CommandStateError(
                    f"production render attempt in {attempt.state} cannot acquire a lease"
                )
            cursor.execute(
                """
                UPDATE runtime.production_render_attempts
                   SET state = 'rendering', lease_token = %s,
                       lease_expires_at = clock_timestamp()
                           + make_interval(secs => %s),
                       version = version + 1
                 WHERE attempt_id = %s AND version = %s
                   AND (
                       state = 'reserved'
                       OR (state = 'rendering'
                           AND lease_expires_at <= clock_timestamp())
                   )
                """,
                (token, lease_seconds, attempt_id, expected_version),
            )
            if cursor.rowcount != 1:
                return None
            acquired = self._read_production_render_attempt_by_id(
                cursor,
                attempt_id,
                for_update=False,
            )
            return self._production_render_lease(acquired)

        return self._transaction(operation)

    def renew_production_render_lease(
        self,
        lease: ProductionRenderLease,
        *,
        lease_seconds: int,
    ) -> ProductionRenderLease:
        """Renew one active exact lease using database time and version fencing."""

        if type(lease) is not ProductionRenderLease:  # noqa: E721
            raise StoreValidationError(
                "production render renewal requires an exact lease"
            )
        self._validate_render_lease_seconds(lease_seconds)

        def operation(cursor: DbCursor) -> ProductionRenderLease:
            record, _, slot_state = self._locked_production_render_attempt_aggregate(
                cursor,
                lease.attempt_id,
            )
            attempt = record.attempt
            if (
                slot_state != "running"
                or attempt.state != "rendering"
                or attempt.version != lease.version
                or attempt.job_id != lease.job_id
                or attempt.command_slot_id != lease.command_slot_id
                or record.lease_token != lease.token
            ):
                raise CommandStateError(
                    "production render lease is stale or owned elsewhere"
                )
            cursor.execute(
                """
                UPDATE runtime.production_render_attempts
                   SET lease_expires_at = clock_timestamp()
                           + make_interval(secs => %s),
                       version = version + 1
                 WHERE attempt_id = %s AND state = 'rendering'
                   AND version = %s AND lease_token = %s
                   AND lease_expires_at > clock_timestamp()
                   AND clock_timestamp() + make_interval(secs => %s)
                       > lease_expires_at
                """,
                (
                    lease_seconds,
                    lease.attempt_id,
                    lease.version,
                    lease.token,
                    lease_seconds,
                ),
            )
            if cursor.rowcount != 1:
                raise CommandStateError(
                    "production render lease renewal CAS was lost or would not extend expiry"
                )
            renewed = self._read_production_render_attempt_by_id(
                cursor,
                lease.attempt_id,
                for_update=False,
            )
            return self._production_render_lease(renewed)

        return self._transaction(operation)

    def record_production_render_output(
        self,
        lease: ProductionRenderLease,
        *,
        output_blob: BlobRef,
        facts: ProductionRenderAttemptFacts,
    ) -> ProductionRenderAttempt:
        """Bind one exact same-Job external MP4 while the lease is active."""

        if type(lease) is not ProductionRenderLease:  # noqa: E721
            raise StoreValidationError(
                "production render output requires an exact lease"
            )
        if type(output_blob) is not BlobRef:  # noqa: E721
            raise StoreValidationError(
                "production render output must be an exact BlobRef"
            )
        from ..rendering.production_ffmpeg_renderer import (
            ProductionRenderAttemptFacts,
        )

        if type(facts) is not ProductionRenderAttemptFacts:  # noqa: E721
            raise StoreValidationError(
                "production render output requires exact ProductionRenderAttemptFacts"
            )

        def operation(cursor: DbCursor) -> ProductionRenderAttempt:
            record, job_id, slot_state = (
                self._locked_production_render_attempt_aggregate(
                    cursor,
                    lease.attempt_id,
                )
            )
            attempt = record.attempt
            if (
                slot_state != "running"
                or attempt.state != "rendering"
                or attempt.version != lease.version
                or attempt.job_id != lease.job_id
                or attempt.command_slot_id != lease.command_slot_id
                or record.lease_token != lease.token
            ):
                raise CommandStateError(
                    "production render output lease is stale or owned elsewhere"
                )
            durable = self._claimed_blob_ref(
                cursor,
                job_id,
                output_blob,
                field_name="production-render-output",
            )
            cursor.execute(
                """
                SELECT storage_kind
                  FROM storage.blob_objects
                 WHERE object_id = %s
                """,
                (durable.object_id,),
            )
            kind_row = cursor.fetchone()
            if (
                kind_row is None
                or _text(kind_row[0]) != "s3_compatible"
                or durable.media_type != "video/mp4"
                or durable.byte_length <= 0
                or durable.byte_length > attempt.max_output_bytes
            ):
                raise BlobIntegrityError(
                    "production render output is not an allowed external MP4"
                )
            cursor.execute(
                "SELECT job_key, profile FROM runtime.jobs WHERE job_id = %s",
                (job_id,),
            )
            job_row = cursor.fetchone()
            if job_row is None or cursor.fetchone() is not None:
                raise RuntimeStoreError("production render Job identity is unavailable")
            job = Job(
                _text(job_row[0]),
                cast(JobProfile, _text(job_row[1])),
            )
            ffmpeg_identity_json = json.dumps(
                facts.ffmpeg.to_mapping(),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            ffmpeg_identity_sha256 = (
                "sha256:" + hashlib.sha256(ffmpeg_identity_json).hexdigest()
            )
            if (
                facts.attempt_id != attempt.attempt_id
                or facts.job != job
                or facts.story_id
                != attempt.recipe.logical_id.removeprefix("production_recipe@")
                or facts.recipe_sha256 != attempt.recipe.content_hash
                or facts.plan_sha256 != attempt.render_plan_sha256
                or facts.profile_sha256 != attempt.render_profile_sha256
                or facts.execution_limits_sha256
                != attempt.execution_limits_sha256
                or ffmpeg_identity_sha256 != attempt.renderer_identity_sha256
                or facts.output_sha256 != durable.content_hash
                or facts.output_byte_length != durable.byte_length
                or facts.output_media_type != durable.media_type
            ):
                raise StoreValidationError(
                    "production render facts disagree with reserved authority or output Blob"
                )
            facts_json = _canonical_production_render_facts_json(facts)
            facts_json_sha256 = (
                "sha256:" + hashlib.sha256(facts_json.encode("utf-8")).hexdigest()
            )
            if facts_json_sha256 != facts.canonical_hash:
                raise StoreValidationError(
                    "production render facts canonical JSON/hash identity diverged"
                )
            cursor.execute(
                """
                UPDATE runtime.production_render_attempts
                   SET state = 'rendered', version = version + 1,
                       lease_token = NULL, lease_expires_at = NULL,
                       output_object_id = %s,
                       render_facts_json = %s,
                       render_facts_sha256 = %s,
                       rendered_at = clock_timestamp()
                 WHERE attempt_id = %s AND state = 'rendering'
                   AND version = %s AND lease_token = %s
                   AND lease_expires_at > clock_timestamp()
                """,
                (
                    durable.object_id,
                    facts_json,
                    facts.canonical_hash,
                    lease.attempt_id,
                    lease.version,
                    lease.token,
                ),
            )
            if cursor.rowcount != 1:
                raise CommandStateError(
                    "production render output lease expired or lost its CAS"
                )
            return self._read_production_render_attempt_by_id(
                cursor,
                lease.attempt_id,
                for_update=False,
            ).attempt

        return self._transaction(operation)

    def commit_production_render_rejection(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        rejection: CommandRejection,
        lease: ProductionRenderLease | None = None,
    ) -> CommandOutcome:
        """Atomically reject a reserved, actively rendering, or rendered attempt."""

        self._validate_uuid(attempt_id, "attempt_id")
        self._validate_nonnegative_version(expected_version, "expected_version")
        if type(rejection) is not CommandRejection:  # noqa: E721
            raise StoreValidationError(
                "production render rejection must be an exact CommandRejection"
            )
        if lease is not None and type(lease) is not ProductionRenderLease:  # noqa: E721
            raise StoreValidationError(
                "production render rejection lease must be exact when supplied"
            )

        def operation(cursor: DbCursor) -> CommandOutcome:
            record, job_id, slot_state = (
                self._locked_production_render_attempt_aggregate(
                    cursor,
                    attempt_id,
                )
            )
            attempt = record.attempt
            if attempt.command_slot_id != rejection.command_slot_id:
                raise StoreValidationError(
                    "production render rejection belongs to another command slot"
                )
            if slot_state != "running":
                outcome = self._replay_or_raise(
                    cursor,
                    rejection.command_slot_id,
                    job_id,
                    rejection.outcome,
                    None,
                )
                if (
                    attempt.state != rejection.outcome
                    or attempt.receipt_id != outcome.receipt_id
                    or attempt.failure_code != rejection.failure_code
                    or attempt.failure_detail_json
                    != _canonical_db_json(rejection.failure_detail_json)
                ):
                    raise CommandStateError(
                        "terminal production render rejection replay differs"
                    )
                return replace(
                    outcome,
                    failure_detail_json=attempt.failure_detail_json,
                )
            if attempt.version != expected_version:
                raise CommandStateError(
                    "production render rejection version is stale"
                )
            if attempt.state == "rendering":
                if (
                    lease is None
                    or lease.attempt_id != attempt.attempt_id
                    or lease.job_id != attempt.job_id
                    or lease.command_slot_id != attempt.command_slot_id
                    or lease.version != attempt.version
                    or lease.token != record.lease_token
                ):
                    raise CommandStateError(
                        "production render rejection lease is stale or owned elsewhere"
                    )
                lease_predicate = (
                    " AND lease_token = %s"
                    " AND lease_expires_at > clock_timestamp()"
                )
                lease_params: tuple[object, ...] = (lease.token,)
            elif attempt.state in ("reserved", "rendered"):
                if lease is not None:
                    raise StoreValidationError(
                        "non-rendering production rejection must not supply a lease"
                    )
                lease_predicate = ""
                lease_params = ()
            else:
                raise CommandStateError(
                    f"production render attempt in {attempt.state} cannot be rejected"
                )
            outcome = self._write_rejection(cursor, rejection, job_id)
            cursor.execute(
                """
                UPDATE runtime.production_render_attempts
                   SET state = %s, version = version + 1,
                       lease_token = NULL, lease_expires_at = NULL,
                       receipt_id = %s, failure_code = %s,
                       failure_detail = %s::jsonb,
                       completed_at = clock_timestamp()
                 WHERE attempt_id = %s AND state = %s AND version = %s
                """
                + lease_predicate,
                (
                    rejection.outcome,
                    outcome.receipt_id,
                    rejection.failure_code,
                    rejection.failure_detail_json,
                    attempt_id,
                    attempt.state,
                    expected_version,
                    *lease_params,
                ),
            )
            if cursor.rowcount != 1:
                raise CommandStateError(
                    "production render rejection lease expired or lost its CAS"
                )
            terminal = self._read_production_render_attempt_by_id(
                cursor,
                attempt_id,
                for_update=False,
            ).attempt
            if terminal.state != rejection.outcome or terminal.receipt_id != outcome.receipt_id:
                raise RuntimeStoreError(
                    "production render rejection did not close its exact attempt"
                )
            return outcome

        return self._transaction(operation)

    def read_production_render_attempt(
        self,
        attempt_id: UUID,
    ) -> ProductionRenderAttempt:
        """Read one exact production render attempt by immutable identity."""

        self._validate_uuid(attempt_id, "attempt_id")
        return self._transaction(
            lambda cursor: self._read_production_render_attempt_by_id(
                cursor,
                attempt_id,
                for_update=False,
            ).attempt
        )

    def read_production_render_attempt_for_slot(
        self,
        job: Job,
        command_slot_id: UUID,
    ) -> ProductionRenderAttempt | None:
        """Resolve the sole attempt through an exact Job and command slot."""

        if type(job) is not Job:  # noqa: E721
            raise StoreValidationError("job must be an exact Job")
        self._validate_uuid(command_slot_id, "command_slot_id")

        def operation(cursor: DbCursor) -> ProductionRenderAttempt | None:
            cursor.execute(
                "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s",
                (job.job_key,),
            )
            job_row = cursor.fetchone()
            if job_row is None:
                return None
            if _text(job_row[1]) != job.profile:
                raise JobProfileMismatchError(
                    "job_key belongs to a different profile"
                )
            cursor.execute(
                """
                SELECT attempt_id
                  FROM runtime.production_render_attempts
                 WHERE job_id = %s AND command_slot_id = %s
                """,
                (UUID(str(job_row[0])), command_slot_id),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            if cursor.fetchone() is not None:
                raise RuntimeStoreError(
                    "production render slot owns multiple attempts"
                )
            return self._read_production_render_attempt_by_id(
                cursor,
                UUID(str(row[0])),
                for_update=False,
            ).attempt

        return self._transaction(operation)

    # ------------------------------------------------------------------
    # durable production render QC attempts
    # ------------------------------------------------------------------

    def reserve_production_render_qc_attempt(
        self,
        render_attempt_id: UUID,
        *,
        expected_render_version: int,
        qc_policy_sha256: str,
        required_check_set_version: str,
        qc_runner_identity_sha256: str,
    ) -> ProductionRenderQcAttempt:
        """Reserve the sole exact QC identity for one rendered output."""

        self._validate_uuid(render_attempt_id, "render_attempt_id")
        self._validate_nonnegative_version(
            expected_render_version,
            "expected_render_version",
        )
        self._validate_sha256(
            qc_policy_sha256,
            "production render QC qc_policy_sha256",
        )
        self._validate_required_check_set_version(required_check_set_version)
        self._validate_sha256(
            qc_runner_identity_sha256,
            "production render QC qc_runner_identity_sha256",
        )

        def operation(cursor: DbCursor) -> ProductionRenderQcAttempt:
            render_record, job_id, slot_state = self._locked_production_render_attempt_aggregate(
                cursor,
                render_attempt_id,
            )
            render_attempt = render_record.attempt
            if slot_state != "running":
                raise CommandStateError("production render command cannot reserve a QC attempt")
            if render_attempt.version != expected_render_version:
                raise CommandStateError("production render QC parent version is stale")
            if render_attempt.state != "rendered":
                raise CommandStateError("production render QC requires a rendered parent attempt")
            if (
                render_attempt.output_blob is None
                or render_attempt.render_facts is None
                or render_attempt.render_facts_sha256 is None
            ):
                raise RuntimeStoreError("rendered production attempt lacks exact output and facts")

            cursor.execute(
                """
                SELECT qc_attempt_id
                  FROM runtime.production_render_qc_attempts
                 WHERE render_attempt_id = %s
                 FOR UPDATE
                """,
                (render_attempt_id,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                attempt = self._read_production_render_qc_attempt_by_id(
                    cursor,
                    UUID(str(existing[0])),
                    for_update=False,
                ).attempt
                if (
                    attempt.render_attempt_id != render_attempt_id
                    or attempt.job_id != job_id
                    or attempt.command_slot_id != render_attempt.command_slot_id
                    or attempt.rendered_version != expected_render_version
                    or attempt.output_blob != render_attempt.output_blob
                    or attempt.render_facts_sha256 != render_attempt.render_facts_sha256
                    or attempt.qc_policy_sha256 != qc_policy_sha256
                    or attempt.required_check_set_version != required_check_set_version
                    or attempt.qc_runner_identity_sha256 != qc_runner_identity_sha256
                ):
                    raise IdempotencyConflictError(
                        "production render was reserved for QC with a different identity"
                    )
                return attempt

            qc_attempt_id = uuid4()
            output = render_attempt.output_blob
            cursor.execute(
                """
                INSERT INTO runtime.production_render_qc_attempts (
                    qc_attempt_id, render_attempt_id, job_id, command_slot_id,
                    rendered_version, output_object_id, render_facts_sha256,
                    qc_policy_sha256, required_check_set_version,
                    qc_runner_identity_sha256, state, version
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'reserved', 0
                )
                """,
                (
                    qc_attempt_id,
                    render_attempt_id,
                    job_id,
                    render_attempt.command_slot_id,
                    expected_render_version,
                    output.object_id,
                    render_attempt.render_facts_sha256,
                    qc_policy_sha256,
                    required_check_set_version,
                    qc_runner_identity_sha256,
                ),
            )
            return replace(
                self._read_production_render_qc_attempt_by_id(
                    cursor,
                    qc_attempt_id,
                    for_update=False,
                ).attempt,
                is_fresh_reservation=True,
            )

        return self._transaction(operation)

    def read_production_render_qc_attempt(
        self,
        qc_attempt_id: UUID,
    ) -> ProductionRenderQcAttempt:
        """Read one public QC attempt without exposing its lease token."""

        self._validate_uuid(qc_attempt_id, "qc_attempt_id")
        return self._transaction(
            lambda cursor: (
                self._read_production_render_qc_attempt_by_id(
                    cursor,
                    qc_attempt_id,
                    for_update=False,
                ).attempt
            )
        )

    def read_production_render_qc_attempt_for_render(
        self,
        render_attempt_id: UUID,
    ) -> ProductionRenderQcAttempt | None:
        """Resolve the sole QC attempt through its exact rendered parent."""

        self._validate_uuid(render_attempt_id, "render_attempt_id")

        def operation(cursor: DbCursor) -> ProductionRenderQcAttempt | None:
            cursor.execute(
                """
                SELECT qc_attempt_id
                  FROM runtime.production_render_qc_attempts
                 WHERE render_attempt_id = %s
                """,
                (render_attempt_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            if cursor.fetchone() is not None:
                raise RuntimeStoreError("production render owns multiple QC attempts")
            return self._read_production_render_qc_attempt_by_id(
                cursor,
                UUID(str(row[0])),
                for_update=False,
            ).attempt

        return self._transaction(operation)

    def acquire_production_render_qc_lease(
        self,
        qc_attempt_id: UUID,
        *,
        expected_version: int,
        lease_seconds: int,
    ) -> ProductionRenderQcLease | None:
        """Acquire a reserved QC attempt or take over only an expired scan."""

        self._validate_uuid(qc_attempt_id, "qc_attempt_id")
        self._validate_nonnegative_version(expected_version, "expected_version")
        self._validate_production_render_qc_lease_seconds(lease_seconds)
        token = uuid4()

        def operation(cursor: DbCursor) -> ProductionRenderQcLease | None:
            record, render_attempt, slot_state = (
                self._locked_production_render_qc_attempt_aggregate(
                    cursor,
                    qc_attempt_id,
                )
            )
            attempt = record.attempt
            if attempt.version != expected_version:
                raise CommandStateError("production render QC lease version is stale")
            if slot_state != "running" or render_attempt.state != "rendered":
                raise CommandStateError("production render QC parent cannot acquire a lease")
            if attempt.state not in ("reserved", "scanning"):
                raise CommandStateError(
                    f"production render QC attempt in {attempt.state} cannot acquire a lease"
                )
            cursor.execute(
                """
                UPDATE runtime.production_render_qc_attempts
                   SET state = 'scanning', lease_token = %s,
                       lease_expires_at = clock_timestamp()
                           + make_interval(secs => %s),
                       version = version + 1
                 WHERE qc_attempt_id = %s AND version = %s
                   AND (
                       state = 'reserved'
                       OR (state = 'scanning'
                           AND lease_expires_at <= clock_timestamp())
                   )
                """,
                (token, lease_seconds, qc_attempt_id, expected_version),
            )
            if cursor.rowcount != 1:
                return None
            acquired = self._read_production_render_qc_attempt_by_id(
                cursor,
                qc_attempt_id,
                for_update=False,
            )
            return self._production_render_qc_lease(acquired)

        return self._transaction(operation)

    def renew_production_render_qc_lease(
        self,
        lease: ProductionRenderQcLease,
        *,
        lease_seconds: int,
    ) -> ProductionRenderQcLease:
        """Renew one exact active QC lease using database time."""

        if type(lease) is not ProductionRenderQcLease:  # noqa: E721
            raise StoreValidationError("production render QC renewal requires an exact lease")
        self._validate_production_render_qc_lease_seconds(lease_seconds)

        def operation(cursor: DbCursor) -> ProductionRenderQcLease:
            record, render_attempt, slot_state = (
                self._locked_production_render_qc_attempt_aggregate(
                    cursor,
                    lease.qc_attempt_id,
                )
            )
            attempt = record.attempt
            if (
                slot_state != "running"
                or render_attempt.state != "rendered"
                or attempt.state != "scanning"
                or attempt.version != lease.version
                or attempt.render_attempt_id != lease.render_attempt_id
                or attempt.job_id != lease.job_id
                or attempt.command_slot_id != lease.command_slot_id
                or record.lease_token != lease.token
            ):
                raise CommandStateError("production render QC lease is stale or owned elsewhere")
            cursor.execute(
                """
                UPDATE runtime.production_render_qc_attempts
                   SET lease_expires_at = clock_timestamp()
                           + make_interval(secs => %s),
                       version = version + 1
                 WHERE qc_attempt_id = %s AND state = 'scanning'
                   AND version = %s AND lease_token = %s
                   AND lease_expires_at > clock_timestamp()
                   AND clock_timestamp() + make_interval(secs => %s)
                       > lease_expires_at
                """,
                (
                    lease_seconds,
                    lease.qc_attempt_id,
                    lease.version,
                    lease.token,
                    lease_seconds,
                ),
            )
            if cursor.rowcount != 1:
                raise CommandStateError(
                    "production render QC lease renewal CAS was lost or would not extend expiry"
                )
            renewed = self._read_production_render_qc_attempt_by_id(
                cursor,
                lease.qc_attempt_id,
                for_update=False,
            )
            return self._production_render_qc_lease(renewed)

        return self._transaction(operation)

    @staticmethod
    def _validate_required_check_set_version(value: object) -> None:
        valid_characters = "abcdefghijklmnopqrstuvwxyz0123456789._-"
        valid_initial_characters = "abcdefghijklmnopqrstuvwxyz0123456789"
        if (
            type(value) is not str  # noqa: E721
            or not 1 <= len(value) <= 128
            or value[0] not in valid_initial_characters
            or any(character not in valid_characters for character in value)
        ):
            raise StoreValidationError(
                "production render QC required_check_set_version must be a "
                "safe lowercase version identifier"
            )

    @staticmethod
    def _validate_production_render_qc_lease_seconds(value: int) -> None:
        if type(value) is not int or not 1 <= value <= 3600:  # noqa: E721
            raise StoreValidationError(
                "production render QC lease_seconds must be between 1 and 3600"
            )

    @staticmethod
    def _production_render_qc_lease(
        record: _ProductionRenderQcAttemptRecord,
    ) -> ProductionRenderQcLease:
        attempt = record.attempt
        if (
            attempt.state != "scanning"
            or record.lease_token is None
            or attempt.lease_expires_at is None
        ):
            raise RuntimeStoreError(
                "production render QC lease transition returned an invalid attempt"
            )
        return ProductionRenderQcLease(
            attempt.qc_attempt_id,
            attempt.render_attempt_id,
            attempt.job_id,
            attempt.command_slot_id,
            record.lease_token,
            attempt.lease_expires_at,
            attempt.version,
        )

    def _locked_production_render_qc_attempt_aggregate(
        self,
        cursor: DbCursor,
        qc_attempt_id: UUID,
    ) -> tuple[_ProductionRenderQcAttemptRecord, ProductionRenderAttempt, str]:
        self._validate_uuid(qc_attempt_id, "qc_attempt_id")
        cursor.execute(
            """
            SELECT render_attempt_id
              FROM runtime.production_render_qc_attempts
             WHERE qc_attempt_id = %s
            """,
            (qc_attempt_id,),
        )
        identity = cursor.fetchone()
        if identity is None:
            raise StoreValidationError("production render QC qc_attempt_id is unknown")
        render_attempt_id = UUID(str(identity[0]))
        render_record, _, slot_state = self._locked_production_render_attempt_aggregate(
            cursor,
            render_attempt_id,
        )
        record = self._read_production_render_qc_attempt_by_id(
            cursor,
            qc_attempt_id,
            for_update=True,
        )
        attempt = record.attempt
        render_attempt = render_record.attempt
        if (
            attempt.render_attempt_id != render_attempt.attempt_id
            or attempt.job_id != render_attempt.job_id
            or attempt.command_slot_id != render_attempt.command_slot_id
            or attempt.output_blob != render_attempt.output_blob
            or attempt.render_facts_sha256 != render_attempt.render_facts_sha256
        ):
            raise RuntimeStoreError(
                "production render QC identity disagrees with its locked parent"
            )
        return record, render_attempt, slot_state

    def _read_production_render_qc_attempt_by_id(
        self,
        cursor: DbCursor,
        qc_attempt_id: UUID,
        *,
        for_update: bool,
    ) -> _ProductionRenderQcAttemptRecord:
        suffix = " FOR UPDATE OF qc" if for_update else ""
        cursor.execute(
            """
            SELECT qc.render_attempt_id, qc.job_id, qc.command_slot_id,
                   qc.rendered_version, qc.output_object_id,
                   output.content_hash, output.byte_length, output.media_type,
                   output.storage_kind, output_claim.job_id,
                   qc.render_facts_sha256, qc.qc_policy_sha256,
                   qc.required_check_set_version,
                   qc.qc_runner_identity_sha256, qc.state, qc.version,
                   qc.reserved_at, qc.lease_token, qc.lease_expires_at,
                   parent.job_id, parent.command_slot_id,
                   parent.output_object_id, parent.render_facts_sha256,
                   parent.state, parent.version,
                   render_slot.job_id, render_slot.state,
                   render_slot.command_name, render_slot.execution_kind,
                   render_job.profile
              FROM runtime.production_render_qc_attempts AS qc
              JOIN runtime.production_render_attempts AS parent
                ON parent.attempt_id = qc.render_attempt_id
              JOIN runtime.command_slots AS render_slot
                ON render_slot.command_slot_id = parent.command_slot_id
              JOIN runtime.jobs AS render_job
                ON render_job.job_id = parent.job_id
              JOIN storage.blob_objects AS output
                ON output.object_id = qc.output_object_id
              LEFT JOIN storage.blob_claims AS output_claim
                ON output_claim.object_id = output.object_id
               AND output_claim.job_id = qc.job_id
             WHERE qc.qc_attempt_id = %s
            """
            + suffix,
            (qc_attempt_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise StoreValidationError("production render QC qc_attempt_id is unknown")
        if cursor.fetchone() is not None:
            raise RuntimeStoreError("production render QC identity resolved multiple rows")
        (
            render_attempt_id,
            job_id,
            command_slot_id,
            rendered_version,
            output_object_id,
            output_content_hash,
            output_byte_length,
            output_media_type,
            output_storage_kind,
            output_claim_job_id,
            render_facts_sha256,
            qc_policy_sha256,
            required_check_set_version,
            qc_runner_identity_sha256,
            state,
            version,
            reserved_at,
            lease_token,
            lease_expires_at,
            parent_job_id,
            parent_command_slot_id,
            parent_output_object_id,
            parent_render_facts_sha256,
            parent_state,
            parent_version,
            slot_job_id,
            slot_state,
            slot_command_name,
            slot_execution_kind,
            render_job_profile,
        ) = row
        if (
            output_claim_job_id is None
            or UUID(str(output_claim_job_id)) != UUID(str(job_id))
            or _text(output_storage_kind) != "s3_compatible"
            or int(_text(output_byte_length)) <= 0
            or _text(output_media_type) != "video/mp4"
        ):
            raise RuntimeStoreError(
                "persisted production render QC output storage authority is invalid"
            )
        if (
            UUID(str(job_id)) != UUID(str(parent_job_id))
            or UUID(str(command_slot_id)) != UUID(str(parent_command_slot_id))
            or UUID(str(output_object_id)) != UUID(str(parent_output_object_id))
            or _text(render_facts_sha256) != _text(parent_render_facts_sha256)
            or _text(parent_state) != "rendered"
            or int(_text(rendered_version)) != int(_text(parent_version))
            or UUID(str(slot_job_id)) != UUID(str(job_id))
            or _text(slot_state) != "running"
            or _text(slot_command_name) != PRODUCTION_RENDER_COMMAND_NAME
            or _text(slot_execution_kind) != "deterministic"
            or _text(render_job_profile) not in ("shadow", "production")
        ):
            raise RuntimeStoreError(
                "persisted production render QC identity disagrees with its parent"
            )
        attempt = ProductionRenderQcAttempt(
            qc_attempt_id=qc_attempt_id,
            render_attempt_id=UUID(str(render_attempt_id)),
            job_id=UUID(str(job_id)),
            command_slot_id=UUID(str(command_slot_id)),
            rendered_version=int(_text(rendered_version)),
            output_blob=BlobRef(
                UUID(str(output_object_id)),
                _text(output_content_hash),
                int(_text(output_byte_length)),
                _text(output_media_type),
            ),
            render_facts_sha256=_text(render_facts_sha256),
            qc_policy_sha256=_text(qc_policy_sha256),
            required_check_set_version=_text(required_check_set_version),
            qc_runner_identity_sha256=_text(qc_runner_identity_sha256),
            state=cast(Literal["reserved", "scanning"], _text(state)),
            version=int(_text(version)),
            reserved_at=cast(datetime, reserved_at),
            lease_expires_at=cast(datetime | None, lease_expires_at),
        )
        return _ProductionRenderQcAttemptRecord(
            attempt,
            None if lease_token is None else UUID(str(lease_token)),
        )

    @staticmethod
    def _validate_render_lease_seconds(value: int) -> None:
        if type(value) is not int or not 1 <= value <= 3600:  # noqa: E721
            raise StoreValidationError(
                "production render lease_seconds must be between 1 and 3600"
            )

    @staticmethod
    def _production_render_lease(
        record: _ProductionRenderAttemptRecord,
    ) -> ProductionRenderLease:
        attempt = record.attempt
        if (
            attempt.state != "rendering"
            or record.lease_token is None
            or attempt.lease_expires_at is None
        ):
            raise RuntimeStoreError(
                "production render lease transition returned an invalid attempt"
            )
        return ProductionRenderLease(
            attempt.attempt_id,
            attempt.job_id,
            attempt.command_slot_id,
            record.lease_token,
            attempt.lease_expires_at,
            attempt.version,
        )

    def _locked_production_render_attempt_aggregate(
        self,
        cursor: DbCursor,
        attempt_id: UUID,
    ) -> tuple[_ProductionRenderAttemptRecord, UUID, str]:
        self._validate_uuid(attempt_id, "attempt_id")
        cursor.execute(
            """
            SELECT job_id, command_slot_id
              FROM runtime.production_render_attempts
             WHERE attempt_id = %s
            """,
            (attempt_id,),
        )
        identity = cursor.fetchone()
        if identity is None:
            raise StoreValidationError("production render attempt_id is unknown")
        expected_job_id = UUID(str(identity[0]))
        slot_id = UUID(str(identity[1]))
        job_id, slot_state, command_name, _ = self._locked_job_then_slot(
            cursor,
            slot_id,
        )
        self._require_slot_execution_kind(cursor, slot_id, "deterministic")
        if command_name != PRODUCTION_RENDER_COMMAND_NAME:
            raise CommandStateError(
                "production render attempt belongs to another command"
            )
        if job_id != expected_job_id:
            raise RuntimeStoreError(
                "production render attempt changed Jobs while being locked"
            )
        record = self._read_production_render_attempt_by_id(
            cursor,
            attempt_id,
            for_update=True,
        )
        attempt = record.attempt
        if attempt.job_id != job_id or attempt.command_slot_id != slot_id:
            raise RuntimeStoreError(
                "production render attempt identity changed while being locked"
            )
        return record, job_id, slot_state

    def _read_production_render_attempt_by_id(
        self,
        cursor: DbCursor,
        attempt_id: UUID,
        *,
        for_update: bool,
    ) -> _ProductionRenderAttemptRecord:
        suffix = " FOR UPDATE OF attempt" if for_update else ""
        cursor.execute(
            """
            SELECT attempt.job_id, attempt.command_slot_id,
                   attempt.request_hash,
                   attempt.recipe_receipt_id, attempt.recipe_artifact_set_id,
                   attempt.recipe_member_ordinal, attempt.recipe_namespace,
                   attempt.recipe_scope_kind, attempt.recipe_scope_key,
                   attempt.recipe_artifact_type, attempt.recipe_logical_id,
                   attempt.recipe_revision, attempt.recipe_content_hash,
                   attempt.render_plan_sha256, attempt.render_profile_sha256,
                   attempt.renderer_identity_sha256,
                   attempt.execution_limits_sha256, attempt.max_output_bytes,
                   attempt.state, attempt.version, attempt.reserved_at,
                   attempt.lease_token, attempt.lease_expires_at,
                   output.object_id, output.content_hash,
                   output.byte_length, output.media_type,
                   attempt.render_facts_json, attempt.render_facts_sha256,
                   attempt.receipt_id, attempt.artifact_set_id,
                   attempt.failure_code, attempt.failure_detail::text,
                   attempt.rendered_at, attempt.completed_at,
                   render_job.job_key, render_job.profile
              FROM runtime.production_render_attempts AS attempt
              JOIN runtime.jobs AS render_job ON render_job.job_id = attempt.job_id
              LEFT JOIN storage.blob_objects AS output
                ON output.object_id = attempt.output_object_id
             WHERE attempt.attempt_id = %s
            """
            + suffix,
            (attempt_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise StoreValidationError("production render attempt_id is unknown")
        (
            job_id,
            command_slot_id,
            request_hash,
            recipe_receipt_id,
            recipe_artifact_set_id,
            recipe_member_ordinal,
            recipe_namespace,
            recipe_scope_kind,
            recipe_scope_key,
            recipe_artifact_type,
            recipe_logical_id,
            recipe_revision,
            recipe_content_hash,
            render_plan_sha256,
            render_profile_sha256,
            renderer_identity_sha256,
            execution_limits_sha256,
            max_output_bytes,
            state,
            version,
            reserved_at,
            lease_token,
            lease_expires_at,
            output_object_id,
            output_content_hash,
            output_byte_length,
            output_media_type,
            render_facts_json,
            render_facts_sha256,
            receipt_id,
            artifact_set_id,
            failure_code,
            failure_detail,
            rendered_at,
            completed_at,
            render_job_key,
            render_job_profile,
        ) = row
        output_blob = None
        if output_object_id is not None:
            output_blob = BlobRef(
                UUID(str(output_object_id)),
                _text(output_content_hash),
                int(_text(output_byte_length)),
                _text(output_media_type),
            )
        render_facts = None
        if render_facts_json is not None and render_facts_sha256 is not None:
            render_facts = _decode_production_render_facts(
                _text(render_facts_json),
                _text(render_facts_sha256),
            )
            if render_facts.job != Job(
                _text(render_job_key),
                cast(JobProfile, _text(render_job_profile)),
            ):
                raise RuntimeStoreError(
                    "persisted production render facts disagree with their Job"
                )
        elif render_facts_json is not None or render_facts_sha256 is not None:
            raise RuntimeStoreError("persisted production render facts are incomplete")
        attempt = ProductionRenderAttempt(
            attempt_id=UUID(str(attempt_id)),
            job_id=UUID(str(job_id)),
            command_slot_id=UUID(str(command_slot_id)),
            request_hash=_text(request_hash),
            recipe=CommittedArtifactMemberReference(
                UUID(str(recipe_receipt_id)),
                UUID(str(recipe_artifact_set_id)),
                int(_text(recipe_member_ordinal)),
                ArtifactScope(
                    _text(recipe_namespace),
                    _text(recipe_scope_kind),
                    _text(recipe_scope_key),
                ),
                _text(recipe_artifact_type),
                _text(recipe_logical_id),
                int(_text(recipe_revision)),
                _text(recipe_content_hash),
            ),
            render_plan_sha256=_text(render_plan_sha256),
            render_profile_sha256=_text(render_profile_sha256),
            renderer_identity_sha256=_text(renderer_identity_sha256),
            execution_limits_sha256=_text(execution_limits_sha256),
            max_output_bytes=int(_text(max_output_bytes)),
            state=cast(
                Literal[
                    "reserved",
                    "rendering",
                    "rendered",
                    "committed",
                    "denied",
                    "failed",
                ],
                _text(state),
            ),
            version=int(_text(version)),
            reserved_at=cast(datetime, reserved_at),
            lease_expires_at=cast(datetime | None, lease_expires_at),
            output_blob=output_blob,
            render_facts=render_facts,
            render_facts_sha256=(
                None
                if render_facts_sha256 is None
                else _text(render_facts_sha256)
            ),
            receipt_id=None if receipt_id is None else UUID(str(receipt_id)),
            artifact_set_id=(
                None if artifact_set_id is None else UUID(str(artifact_set_id))
            ),
            failure_code=None if failure_code is None else _text(failure_code),
            failure_detail_json=(
                None
                if failure_detail is None
                else _canonical_db_json(_text(failure_detail))
            ),
            rendered_at=cast(datetime | None, rendered_at),
            completed_at=cast(datetime | None, completed_at),
        )
        return _ProductionRenderAttemptRecord(
            attempt,
            None if lease_token is None else UUID(str(lease_token)),
        )

    def read_immutable_blob(self, job: Job, reference: BlobRef) -> bytes:
        """Read exact bytes only through a matching per-Job immutable claim."""

        if type(reference) is not BlobRef:  # noqa: E721
            raise StoreValidationError("reference must be an exact BlobRef")

        def operation(cursor: DbCursor) -> bytes:
            cursor.execute(
                "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s",
                (job.job_key,),
            )
            job_row = cursor.fetchone()
            if job_row is None:
                raise BlobIntegrityError("blob Job does not exist")
            job_id, profile = job_row
            if _text(profile) != job.profile:
                raise JobProfileMismatchError("job_key belongs to a different profile")
            durable = self._claimed_blob_ref(
                cursor,
                UUID(str(job_id)),
                reference,
                field_name="immutable",
            )
            cursor.execute(
                "SELECT content_bytes FROM storage.blob_objects WHERE object_id = %s",
                (durable.object_id,),
            )
            row = cursor.fetchone()
            if row is None or not isinstance(row[0], (bytes, bytearray, memoryview)):
                raise BlobUnavailableError("immutable blob bytes are unavailable")
            content = row[0].tobytes() if isinstance(row[0], memoryview) else bytes(row[0])
            if (
                len(content) != durable.byte_length
                or "sha256:" + hashlib.sha256(content).hexdigest() != durable.content_hash
            ):
                raise BlobIntegrityError("immutable blob bytes fail exact integrity validation")
            return content

        return self._transaction(operation)

    def materialize_immutable_blob(
        self,
        job: Job,
        reference: BlobRef,
        limits: MaterializationLimits,
    ) -> VerifiedMaterializedBlob:
        """Boundedly stream an exact Job-owned BlobRef into a sealed private file."""

        if type(reference) is not BlobRef:  # noqa: E721
            raise StoreValidationError("reference must be an exact BlobRef")
        if type(limits) is not MaterializationLimits:  # noqa: E721
            raise StoreValidationError("limits must be exact MaterializationLimits")
        if reference.byte_length > limits.effective_max_source_bytes:
            raise MaterializationError(
                "MEDIA_SOURCE_BYTE_LIMIT_EXCEEDED",
                "committed source exceeds the frozen source-byte limit",
                outcome="denied",
            )
        root = self._materialization_root()
        try:
            source = self._verified_claimed_blob_materialization_source(job, reference)
            durable = source.reference
        except BlobIntegrityError as error:
            raise MaterializationError(
                "COMMITTED_SOURCE_BLOB_INTEGRITY_FAILED",
                "committed source BlobRef integrity verification failed",
                outcome="failed",
            ) from error
        except RuntimeStoreError as error:
            raise MaterializationError(
                "MEDIA_MATERIALIZATION_INFRASTRUCTURE_FAILED",
                "private media staging is unavailable",
                outcome="failed",
            ) from error
        if source.storage_kind == "s3_compatible" and self._object_store_verifier is None:
            raise MaterializationError(
                "MEDIA_MATERIALIZATION_INFRASTRUCTURE_FAILED",
                "external immutable media storage is not configured",
                outcome="failed",
            )

        quota_lease = _reserve_materialization_quota(
            root, durable.byte_length, limits.staging_quota_bytes
        )
        directory: Path | None = quota_lease.directory
        descriptor: int | None = None
        try:
            directory_stat = os.stat(directory, follow_symlinks=False)
            if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_mode & 0o077:
                raise OSError("private staging directory is unsafe")
            directory_fd = os.open(
                directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                descriptor = os.open(
                    "source.mp4",
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
            finally:
                os.close(directory_fd)

            if source.storage_kind == "postgres_inline":
                digest = hashlib.sha256()
                offset = 0
                while offset < durable.byte_length:
                    expected = min(limits.copy_chunk_bytes, durable.byte_length - offset)
                    chunk = self._read_immutable_blob_chunk(
                        durable.object_id, offset, expected
                    )
                    if len(chunk) != expected:
                        raise BlobIntegrityError(
                            "immutable blob stream ended before its declared length"
                        )
                    digest.update(chunk)
                    written = 0
                    while written < len(chunk):
                        written += os.write(descriptor, chunk[written:])
                    offset += len(chunk)
                if self._read_immutable_blob_chunk(durable.object_id, offset, 1):
                    raise BlobIntegrityError(
                        "immutable blob stream exceeded its declared length"
                    )
                if "sha256:" + digest.hexdigest() != durable.content_hash:
                    raise BlobIntegrityError(
                        "immutable blob stream failed exact digest verification"
                    )
            else:
                verifier = cast(S3PendingObjectStore, self._object_store_verifier)
                if any(
                    value is None
                    for value in (
                        source.backend_id,
                        source.storage_region,
                        source.storage_locator,
                        source.etag,
                        source.write_strategy,
                    )
                ):
                    raise BlobIntegrityError(
                        "external immutable blob metadata is incomplete"
                    )
                grant = _issue_s3_read_grant(
                    reference=durable,
                    backend_id=cast(str, source.backend_id),
                    storage_region=cast(str, source.storage_region),
                    storage_locator=cast(str, source.storage_locator),
                    etag=cast(str, source.etag),
                    version_id=source.version_id,
                    write_strategy=cast(str, source.write_strategy),
                )
                verified = verifier.materialize_to_descriptor(
                    grant,
                    destination_descriptor=descriptor,
                    limits=ObjectStoreReadLimits(
                        max_object_bytes=limits.effective_max_source_bytes,
                        transfer_chunk_bytes=min(
                            limits.copy_chunk_bytes,
                            limits.effective_max_source_bytes,
                        ),
                    ),
                )
                if not verifier._verify_materialized_read(  # pyright: ignore[reportPrivateUsage]
                    grant, verified
                ):
                    raise BlobIntegrityError(
                        "external immutable blob verification signature is invalid"
                    )
            os.fsync(descriptor)
            source_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(source_stat.st_mode)
                or source_stat.st_nlink != 1
                or source_stat.st_size != durable.byte_length
            ):
                raise BlobIntegrityError("private materialization is not one sealed regular file")
            os.fchmod(descriptor, 0o400)
            os.close(descriptor)
            descriptor = None
            return _VerifiedMaterializedBlob(
                durable,
                directory / "source.mp4",
                directory,
                quota_lease,
            )
        except (BlobIntegrityError, ObjectStoreReadError) as error:
            if self._discard_partial_materialization(directory, descriptor):
                quota_lease.release()
            if isinstance(error, ObjectStoreReadError) and error.code in {
                "OBJECT_STORE_READ_REQUEST_INVALID",
                "OBJECT_STORE_READ_TARGET_UNSAFE",
                "OBJECT_STORE_RESULT_INDETERMINATE",
            }:
                raise MaterializationError(
                    "MEDIA_MATERIALIZATION_INFRASTRUCTURE_FAILED",
                    "external immutable media could not be materialized exactly",
                    outcome="failed",
                ) from error
            raise MaterializationError(
                "COMMITTED_SOURCE_BLOB_INTEGRITY_FAILED",
                "committed source BlobRef integrity verification failed",
                outcome="failed",
            ) from error
        except Exception as error:
            if self._discard_partial_materialization(directory, descriptor):
                quota_lease.release()
            if isinstance(error, MaterializationError):
                raise
            raise MaterializationError(
                "MEDIA_MATERIALIZATION_INFRASTRUCTURE_FAILED",
                "private media staging is unavailable",
                outcome="failed",
            ) from error

    def _materialization_root(self) -> Path:
        root = self._materialization_staging_root
        if root is None:
            raise MaterializationError(
                "MEDIA_MATERIALIZATION_INFRASTRUCTURE_FAILED",
                "private media staging is not configured",
                outcome="failed",
            )
        try:
            return validate_materialization_staging_root(root)
        except StoreValidationError as error:
            raise MaterializationError(
                "MEDIA_MATERIALIZATION_INFRASTRUCTURE_FAILED",
                "private media staging is unavailable or unsafe",
                outcome="failed",
            ) from error

    def _verified_claimed_blob_materialization_source(
        self,
        job: Job,
        reference: BlobRef,
    ) -> _ClaimedBlobMaterializationSource:
        def operation(cursor: DbCursor) -> _ClaimedBlobMaterializationSource:
            cursor.execute(
                "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s",
                (job.job_key,),
            )
            job_row = cursor.fetchone()
            if job_row is None:
                raise BlobIntegrityError("blob Job does not exist")
            job_id, profile = job_row
            if _text(profile) != job.profile:
                raise JobProfileMismatchError("job_key belongs to a different profile")
            cursor.execute(
                """
                SELECT object.object_id, object.content_hash,
                       object.byte_length, object.media_type,
                       object.content_bytes IS NOT NULL,
                       object.storage_kind, object.storage_backend_id,
                       object.storage_region, object.storage_locator,
                       object.storage_etag, object.storage_version_id,
                       object.write_strategy, object.verified_at IS NOT NULL
                  FROM storage.blob_objects AS object
                  JOIN storage.blob_claims AS claim
                    ON claim.object_id = object.object_id
                 WHERE claim.job_id = %s AND object.object_id = %s
                """,
                (UUID(str(job_id)), reference.object_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise BlobIntegrityError(
                    "immutable BlobRef is not claimed by the attempt Job"
                )
            durable = BlobRef(
                UUID(str(row[0])),
                _text(row[1]),
                int(_text(row[2])),
                _text(row[3]),
            )
            if durable != reference:
                raise BlobIntegrityError(
                    "immutable BlobRef does not match durable blob metadata"
                )
            has_inline_bytes = bool(row[4])
            storage_kind = _text(row[5])
            backend_id = None if row[6] is None else _text(row[6])
            storage_region = None if row[7] is None else _text(row[7])
            storage_locator = None if row[8] is None else _text(row[8])
            etag = None if row[9] is None else _text(row[9])
            version_id = None if row[10] is None else _text(row[10])
            write_strategy = None if row[11] is None else _text(row[11])
            is_verified = bool(row[12])
            external_metadata = (
                backend_id,
                storage_region,
                storage_locator,
                etag,
                version_id,
                write_strategy,
            )
            if storage_kind == "postgres_inline":
                if not has_inline_bytes or is_verified or any(
                    value is not None for value in external_metadata
                ):
                    raise BlobIntegrityError(
                        "inline immutable blob storage metadata is invalid"
                    )
            elif storage_kind == "s3_compatible":
                required_external_metadata = (
                    backend_id,
                    storage_region,
                    storage_locator,
                    etag,
                    write_strategy,
                )
                if has_inline_bytes or not is_verified or any(
                    value is None for value in required_external_metadata
                ):
                    raise BlobIntegrityError(
                        "external immutable blob storage metadata is invalid"
                    )
            else:
                raise BlobIntegrityError(
                    "immutable blob uses an unsupported storage kind"
                )
            return _ClaimedBlobMaterializationSource(
                reference=durable,
                storage_kind=storage_kind,
                backend_id=backend_id,
                storage_region=storage_region,
                storage_locator=storage_locator,
                etag=etag,
                version_id=version_id,
                write_strategy=write_strategy,
            )

        return self._transaction(operation)

    def _read_immutable_blob_chunk(self, object_id: UUID, offset: int, size: int) -> bytes:
        def operation(cursor: DbCursor) -> bytes:
            cursor.execute(
                """
                SELECT substring(content_bytes FROM %s FOR %s)
                  FROM storage.blob_objects
                 WHERE object_id = %s
                """,
                (offset + 1, size, object_id),
            )
            row = cursor.fetchone()
            if row is None or not isinstance(row[0], (bytes, bytearray, memoryview)):
                raise BlobIntegrityError("immutable blob bytes are unavailable")
            if isinstance(row[0], memoryview):
                return row[0].tobytes()
            return bytes(row[0])

        return self._transaction(operation)

    @staticmethod
    def _discard_partial_materialization(directory: Path | None, descriptor: int | None) -> bool:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if directory is None:
            return True
        try:
            source = directory / "source.mp4"
            if source.exists() or source.is_symlink():
                source.unlink()
            directory.rmdir()
            return True
        except OSError:
            # Retain the logical quota reservation when private cleanup fails.
            return False

    # ------------------------------------------------------------------
    # durable provider generation attempts
    # ------------------------------------------------------------------

    def reserve_generation_attempt(
        self,
        command_slot_id: UUID,
        request_hash: str,
        *,
        provider_id: str,
        provider_idempotency_key: str,
        request_payload: BlobRef,
        retry_policy_hash: str = (
            "sha256:70f279a4b886d1aaf1498b432af937495e431113db3f38728a635ed24a6fbe39"
        ),
        max_attempts: int = 1,
    ) -> GenerationAttempt:
        """Reserve exactly one provider invocation identity for a generation slot."""

        self._validate_uuid(command_slot_id, "command_slot_id")
        self._validate_sha256(request_hash, "generation.request_hash")
        self._validate_sha256(retry_policy_hash, "generation.retry_policy_hash")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:  # noqa: E721
            raise StoreValidationError(
                "generation.max_attempts must be between one and three"
            )
        if type(provider_id) is not str or not provider_id.strip():  # noqa: E721
            raise StoreValidationError("generation.provider_id must be non-empty")
        if (
            type(provider_idempotency_key) is not str  # noqa: E721
            or not provider_idempotency_key.strip()
        ):
            raise StoreValidationError(
                "generation.provider_idempotency_key must be non-empty"
            )
        if type(request_payload) is not BlobRef:  # noqa: E721
            raise StoreValidationError(
                "generation.request_payload must be an exact BlobRef"
            )

        def operation(cursor: DbCursor) -> GenerationAttempt:
            job_id, state, _command_name, slot_request_hash = self._locked_job_then_slot(
                cursor, command_slot_id
            )
            self._require_slot_execution_kind(cursor, command_slot_id, "generation")
            if state != "running":
                raise GenerationAttemptStateError("generation command slot is already terminal")
            if slot_request_hash != request_hash:
                raise IdempotencyConflictError(
                    "generation request hash must match its exact command claim"
                )
            verified_request_payload = self._claimed_blob_ref(
                cursor,
                job_id,
                request_payload,
                field_name="request-payload",
            )
            cursor.execute(
                """
                SELECT attempt_id FROM runtime.generation_attempts
                 WHERE command_slot_id = %s AND attempt_ordinal = 1 FOR UPDATE
                """,
                (command_slot_id,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                attempt = self._read_generation_attempt_by_id(
                    cursor, UUID(str(existing[0])), for_update=False
                )
                if attempt.request_hash != request_hash:
                    raise IdempotencyConflictError(
                        "generation slot was reserved for another request"
                    )
                if (
                    attempt.provider_id != provider_id
                    or attempt.provider_idempotency_key != provider_idempotency_key
                    or attempt.request_payload != verified_request_payload
                    or attempt.retry_policy_hash != retry_policy_hash
                    or attempt.max_attempts != max_attempts
                ):
                    raise IdempotencyConflictError(
                        "generation slot was reserved with different provider request identity"
                    )
                return attempt
            attempt_id = uuid4()
            cursor.execute(
                """
                INSERT INTO runtime.generation_attempts
                    (attempt_id, job_id, command_slot_id, request_hash,
                     provider_id, provider_idempotency_key, request_payload_object_id,
                     attempt_ordinal, previous_attempt_id, retry_policy_hash, max_attempts,
                     not_before_at, retry_backoff_seconds, state, version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 1, NULL, %s, %s,
                        transaction_timestamp(), 0, 'reserved', 0)
                """,
                (
                    attempt_id,
                    job_id,
                    command_slot_id,
                    request_hash,
                    provider_id,
                    provider_idempotency_key,
                    verified_request_payload.object_id,
                    retry_policy_hash,
                    max_attempts,
                ),
            )
            persisted = self._read_generation_attempt_by_id(
                cursor, attempt_id, for_update=False
            )
            return GenerationAttempt(
                attempt_id,
                job_id,
                command_slot_id,
                request_hash,
                provider_id,
                provider_idempotency_key,
                verified_request_payload,
                "reserved",
                0,
                attempt_ordinal=1,
                retry_policy_hash=retry_policy_hash,
                max_attempts=max_attempts,
                not_before_at=persisted.not_before_at,
                is_fresh_reservation=True,
            )

        return self._transaction(operation)

    def reserve_next_generation_attempt(
        self,
        previous_attempt_id: UUID,
        *,
        expected_version: int,
        provider_idempotency_key: str,
    ) -> GenerationAttempt:
        """Atomically reserve the sole next ordinal after a retryable failure."""

        if type(provider_idempotency_key) is not str or not provider_idempotency_key.strip():  # noqa: E721
            raise StoreValidationError(
                "generation.provider_idempotency_key must be non-empty"
            )
        def operation(cursor: DbCursor) -> GenerationAttempt:
            previous, job_id, slot_state, _command_name = self._locked_attempt_aggregate(
                cursor, previous_attempt_id
            )
            self._require_attempt_transition(
                previous, expected_version, ("failed",), "reserve retry"
            )
            if slot_state != "running":
                raise GenerationAttemptStateError("generation retry slot is not running")
            if previous.failure_disposition != "retryable":
                raise GenerationAttemptStateError(
                    "only an explicitly retryable failure may reserve another attempt"
                )
            if previous.attempt_ordinal >= previous.max_attempts:
                raise GenerationAttemptStateError("generation retry budget is exhausted")
            backoff_seconds = self._generation_retry_backoff_seconds(
                cursor,
                previous,
            )
            next_ordinal = previous.attempt_ordinal + 1
            cursor.execute(
                """
                SELECT attempt_id
                  FROM runtime.generation_attempts
                 WHERE command_slot_id = %s AND attempt_ordinal = %s
                   FOR UPDATE
                """,
                (previous.command_slot_id, next_ordinal),
            )
            existing = cursor.fetchone()
            if existing is not None:
                attempt = self._read_generation_attempt_by_id(
                    cursor, UUID(str(existing[0])), for_update=False
                )
                if (
                    attempt.previous_attempt_id != previous.attempt_id
                    or attempt.provider_idempotency_key != provider_idempotency_key
                ):
                    raise IdempotencyConflictError(
                        "generation retry ordinal has a different request identity"
                    )
                return attempt
            attempt_id = uuid4()
            cursor.execute(
                """
                INSERT INTO runtime.generation_attempts
                    (attempt_id, job_id, command_slot_id, request_hash,
                     provider_id, provider_idempotency_key, request_payload_object_id,
                     attempt_ordinal, previous_attempt_id, retry_policy_hash, max_attempts,
                     not_before_at, retry_backoff_seconds, state, version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        transaction_timestamp() + make_interval(secs => %s),
                        %s, 'reserved', 0)
                """,
                (
                    attempt_id,
                    job_id,
                    previous.command_slot_id,
                    previous.request_hash,
                    previous.provider_id,
                    provider_idempotency_key,
                    previous.request_payload.object_id,
                    next_ordinal,
                    previous.attempt_id,
                    previous.retry_policy_hash,
                    previous.max_attempts,
                    backoff_seconds,
                    backoff_seconds,
                ),
            )
            return GenerationAttempt(
                attempt_id,
                job_id,
                previous.command_slot_id,
                previous.request_hash,
                previous.provider_id,
                provider_idempotency_key,
                previous.request_payload,
                "reserved",
                0,
                attempt_ordinal=next_ordinal,
                previous_attempt_id=previous.attempt_id,
                retry_policy_hash=previous.retry_policy_hash,
                max_attempts=previous.max_attempts,
                not_before_at=self._read_generation_attempt_by_id(
                    cursor, attempt_id, for_update=False
                ).not_before_at,
                retry_backoff_seconds=backoff_seconds,
                is_fresh_reservation=True,
            )

        return self._transaction(operation)

    def dispatch_generation_attempt(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        provider_request_id: str | None = None,
    ) -> GenerationAttempt | None:
        """Move one fresh reservation to dispatched exactly once."""

        if provider_request_id is not None and (
            type(provider_request_id) is not str or not provider_request_id.strip()  # noqa: E721
        ):
            raise StoreValidationError("provider_request_id must be non-empty when present")

        lease_token = str(uuid4())
        lease_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=GENERATION_PROVIDER_LEASE_SECONDS
        )

        def operation(cursor: DbCursor) -> GenerationAttempt | None:
            attempt, _, slot_state, _command_name = self._locked_attempt_aggregate(cursor, attempt_id)
            self._require_attempt_transition(
                attempt, expected_version, ("reserved",), "dispatch"
            )
            if slot_state != "running":
                raise GenerationAttemptStateError("generation slot cannot be dispatched")
            cursor.execute(
                "SELECT transaction_timestamp() >= %s",
                (attempt.not_before_at,),
            )
            ready = cursor.fetchone()
            if ready is None or ready[0] is not True:
                return None
            cursor.execute(
                """
                UPDATE runtime.generation_attempts
                   SET state = 'dispatched', provider_request_id = %s,
                       dispatch_lease_token = %s, dispatch_lease_expires_at = %s,
                       version = version + 1,
                       dispatched_at = transaction_timestamp()
                 WHERE attempt_id = %s AND version = %s
                """,
                (
                    provider_request_id,
                    lease_token,
                    lease_expires_at,
                    attempt_id,
                    expected_version,
                ),
            )
            return self._read_generation_attempt_by_id(cursor, attempt_id, for_update=False)

        return self._transaction(operation)

    def acquire_generation_reconcile_lease(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
    ) -> GenerationAttempt | None:
        """Acquire expired dispatch/indeterminate ownership without racing an active owner."""

        lease_token = str(uuid4())
        lease_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=GENERATION_PROVIDER_LEASE_SECONDS
        )

        def operation(cursor: DbCursor) -> GenerationAttempt | None:
            attempt, _, slot_state, _command_name = self._locked_attempt_aggregate(
                cursor, attempt_id
            )
            self._require_attempt_transition(
                attempt,
                expected_version,
                ("dispatched", "indeterminate"),
                "acquire reconcile lease",
            )
            if slot_state != "running":
                raise GenerationAttemptStateError("generation slot cannot be reconciled")
            if attempt.dispatch_lease_is_active():
                return None
            cursor.execute(
                """
                UPDATE runtime.generation_attempts
                   SET dispatch_lease_token = %s, dispatch_lease_expires_at = %s,
                       version = version + 1
                 WHERE attempt_id = %s AND version = %s
                   AND (dispatch_lease_expires_at IS NULL
                        OR dispatch_lease_expires_at <= transaction_timestamp())
                """,
                (lease_token, lease_expires_at, attempt_id, expected_version),
            )
            if cursor.rowcount != 1:
                return None
            return self._read_generation_attempt_by_id(
                cursor, attempt_id, for_update=False
            )

        return self._transaction(operation)

    def record_generation_response(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        raw_response: BlobRef,
        dispatch_lease_token: str,
        provider_request_id: str | None = None,
    ) -> GenerationAttempt:
        """Bind the exact immutable provider response to a dispatched attempt."""

        return self._record_generation_blob_transition(
            attempt_id,
            expected_version=expected_version,
            raw_response=raw_response,
            provider_request_id=provider_request_id,
            dispatch_lease_token=dispatch_lease_token,
            source_state="dispatched",
            target_state="responded",
        )

    def record_generation_provider_request_id(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        provider_request_id: str,
        dispatch_lease_token: str,
    ) -> GenerationAttempt:
        """CAS-bind ``response.created`` identity while the stream is still open."""

        if type(provider_request_id) is not str or not provider_request_id.strip():  # noqa: E721
            raise StoreValidationError("provider_request_id must be non-empty")

        def operation(cursor: DbCursor) -> GenerationAttempt:
            attempt, _, slot_state, _command_name = self._locked_attempt_aggregate(
                cursor, attempt_id
            )
            if attempt.provider_request_id is not None:
                if attempt.provider_request_id != provider_request_id:
                    raise IdempotencyConflictError(
                        "provider_request_id cannot change for one attempt"
                    )
                return attempt
            self._require_attempt_transition(
                attempt,
                expected_version,
                ("dispatched",),
                "bind provider request id",
            )
            self._require_attempt_lease(
                attempt, dispatch_lease_token, "bind provider request id"
            )
            if slot_state != "running":
                raise GenerationAttemptStateError(
                    "generation slot cannot bind a provider request identity"
                )
            cursor.execute(
                """
                UPDATE runtime.generation_attempts
                   SET provider_request_id = %s, version = version + 1
                 WHERE attempt_id = %s AND version = %s
                   AND state = 'dispatched' AND provider_request_id IS NULL
                   AND dispatch_lease_token = %s
                   AND dispatch_lease_expires_at > transaction_timestamp()
                """,
                (
                    provider_request_id,
                    attempt_id,
                    expected_version,
                    dispatch_lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise GenerationAttemptStateError(
                    "stale generation provider request identity CAS"
                )
            return self._read_generation_attempt_by_id(
                cursor, attempt_id, for_update=False
            )

        return self._transaction(operation)

    def mark_generation_indeterminate(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        dispatch_lease_token: str,
        provider_request_id: str | None = None,
    ) -> GenerationAttempt:
        """Record ambiguous timeout without a receipt or permission to blind retry."""

        if provider_request_id is not None and (
            type(provider_request_id) is not str or not provider_request_id.strip()  # noqa: E721
        ):
            raise StoreValidationError("provider_request_id must be non-empty when present")

        def operation(cursor: DbCursor) -> GenerationAttempt:
            attempt, _, _, _ = self._locked_attempt_aggregate(cursor, attempt_id)
            self._require_attempt_transition(
                attempt,
                expected_version,
                ("dispatched", "indeterminate"),
                "mark indeterminate",
            )
            self._require_attempt_lease(
                attempt, dispatch_lease_token, "mark indeterminate"
            )
            effective_request_id = self._exact_provider_request_id(
                attempt.provider_request_id, provider_request_id
            )
            cursor.execute(
                """
                UPDATE runtime.generation_attempts
                   SET state = 'indeterminate', provider_request_id = %s,
                       dispatch_lease_token = NULL, dispatch_lease_expires_at = NULL,
                       version = version + 1
                 WHERE attempt_id = %s AND version = %s
                   AND dispatch_lease_token = %s
                   AND dispatch_lease_expires_at > transaction_timestamp()
                """,
                (
                    effective_request_id,
                    attempt_id,
                    expected_version,
                    dispatch_lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise GenerationAttemptStateError("generation dispatch lease was lost")
            return self._read_generation_attempt_by_id(cursor, attempt_id, for_update=False)

        return self._transaction(operation)

    def reconcile_generation_response(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        raw_response: BlobRef,
        dispatch_lease_token: str,
        provider_request_id: str | None = None,
    ) -> GenerationAttempt:
        """Continue an indeterminate attempt only with exact reconciliation evidence."""

        return self._record_generation_blob_transition(
            attempt_id,
            expected_version=expected_version,
            raw_response=raw_response,
            provider_request_id=provider_request_id,
            dispatch_lease_token=dispatch_lease_token,
            source_state="indeterminate",
            target_state="reconciled",
        )

    def fail_generation_attempt(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        failure_code: str,
        failure_detail_json: str,
        provider_request_id: str | None = None,
        failure_disposition: str = "nonretryable",
        dispatch_lease_token: str | None = None,
    ) -> GenerationAttempt:
        """Persist an exact terminal provider failure without creating a command receipt."""

        if type(failure_code) is not str or not failure_code.strip():  # noqa: E721
            raise StoreValidationError("generation failure_code must be non-empty")
        try:
            detail = json.loads(failure_detail_json)
        except (TypeError, ValueError) as error:
            raise StoreValidationError("generation failure_detail_json must contain JSON") from error
        if not isinstance(detail, dict):
            raise StoreValidationError("generation failure_detail_json must contain a JSON object")
        if provider_request_id is not None and (
            type(provider_request_id) is not str  # noqa: E721
            or not provider_request_id.strip()
        ):
            raise StoreValidationError("provider_request_id must be non-empty when present")
        if failure_disposition not in ("retryable", "nonretryable", "repairable"):
            raise StoreValidationError("generation failure_disposition is unsupported")

        def operation(cursor: DbCursor) -> GenerationAttempt:
            attempt, _, _, _ = self._locked_attempt_aggregate(cursor, attempt_id)
            self._require_attempt_transition(
                attempt,
                expected_version,
                ("reserved", "dispatched", "responded", "indeterminate", "reconciled"),
                "fail",
            )
            effective_request_id = self._exact_provider_request_id(
                attempt.provider_request_id,
                provider_request_id,
            )
            leased_source = attempt.state in ("dispatched", "indeterminate")
            if leased_source:
                self._require_attempt_lease(
                    attempt, dispatch_lease_token, "fail generation"
                )
            lease_predicate = ""
            params: tuple[object, ...]
            if leased_source:
                lease_predicate = (
                    " AND dispatch_lease_token = %s"
                    " AND dispatch_lease_expires_at > transaction_timestamp()"
                )
                params = (
                    failure_code,
                    failure_detail_json,
                    failure_disposition,
                    effective_request_id,
                    attempt_id,
                    expected_version,
                    dispatch_lease_token,
                )
            else:
                params = (
                    failure_code,
                    failure_detail_json,
                    failure_disposition,
                    effective_request_id,
                    attempt_id,
                    expected_version,
                )
            cursor.execute(
                """
                UPDATE runtime.generation_attempts
                   SET state = 'failed', failure_code = %s, failure_detail = %s::jsonb,
                       failure_disposition = %s, provider_request_id = %s,
                       dispatch_lease_token = NULL, dispatch_lease_expires_at = NULL,
                       version = version + 1,
                       completed_at = transaction_timestamp()
                 WHERE attempt_id = %s AND version = %s
                """
                + lease_predicate,
                params,
            )
            if cursor.rowcount != 1:
                raise GenerationAttemptStateError("generation failure CAS or lease was lost")
            return self._read_generation_attempt_by_id(cursor, attempt_id, for_update=False)

        return self._transaction(operation)

    def commit_generation_success(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        success: CommandSuccess,
    ) -> GenerationAttempt:
        """Atomically bind a reconciled response to its command set, receipt, and attempt."""

        def operation(cursor: DbCursor) -> GenerationAttempt:
            attempt, job_id, slot_state, _command_name = self._locked_attempt_aggregate(
                cursor, attempt_id
            )
            if attempt.command_slot_id != success.command_slot_id:
                raise StoreValidationError("generation success belongs to another command slot")
            if attempt.state == "committed":
                if attempt.artifact_set_id is None:
                    raise BlobIntegrityError("committed generation lost its artifact set binding")
                cursor.execute(
                    "SELECT set_hash FROM runtime.artifact_sets WHERE artifact_set_id = %s",
                    (attempt.artifact_set_id,),
                )
                row = cursor.fetchone()
                if row is None or _text(row[0]) != success.set_hash:
                    raise CommandStateError(
                        "generation was already committed with a different artifact set"
                    )
                return attempt
            self._require_attempt_transition(
                attempt, expected_version, ("responded", "reconciled"), "commit"
            )
            if slot_state != "running":
                raise GenerationAttemptStateError("generation command cannot be committed")
            outcome = self._write_success(cursor, success, job_id)
            cursor.execute(
                """
                UPDATE runtime.generation_attempts
                   SET state = 'committed', version = version + 1,
                       receipt_id = %s, artifact_set_id = %s,
                       completed_at = transaction_timestamp()
                 WHERE attempt_id = %s AND version = %s
                """,
                (
                    outcome.receipt_id,
                    outcome.artifact_set_id,
                    attempt_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise GenerationAttemptStateError("generation commit CAS was lost")
            self._bind_generation_receipt_chain(
                cursor, success.command_slot_id, outcome.receipt_id
            )
            return self._read_generation_attempt_by_id(cursor, attempt_id, for_update=False)

        return self._transaction(operation)

    def commit_generation_rejection(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        rejection: CommandRejection,
    ) -> CommandOutcome:
        """Commit one final generation Receipt and its complete durable Attempt chain."""

        def operation(cursor: DbCursor) -> CommandOutcome:
            attempt, job_id, slot_state, _command_name = self._locked_attempt_aggregate(
                cursor, attempt_id
            )
            if attempt.command_slot_id != rejection.command_slot_id:
                raise StoreValidationError("generation rejection belongs to another slot")
            if slot_state != "running":
                return self._replay_or_raise(
                    cursor,
                    rejection.command_slot_id,
                    job_id,
                    rejection.outcome,
                    None,
                )
            self._require_attempt_transition(
                attempt, expected_version, ("failed",), "commit rejection"
            )
            if (
                attempt.failure_disposition == "retryable"
                and attempt.attempt_ordinal < attempt.max_attempts
            ):
                raise GenerationAttemptStateError(
                    "retryable generation failure still has remaining budget"
                )
            cursor.execute(
                """
                SELECT attempt_id
                  FROM runtime.generation_attempts
                 WHERE command_slot_id = %s
                 ORDER BY attempt_ordinal DESC LIMIT 1
                """,
                (attempt.command_slot_id,),
            )
            latest = cursor.fetchone()
            if latest is None or UUID(str(latest[0])) != attempt.attempt_id:
                raise GenerationAttemptStateError(
                    "only the final generation attempt may reject its command"
                )
            outcome = self._write_rejection(cursor, rejection, job_id)
            self._bind_generation_receipt_chain(
                cursor, rejection.command_slot_id, outcome.receipt_id
            )
            return outcome

        return self._transaction(operation)

    def read_generation_attempt(self, attempt_id: UUID) -> GenerationAttempt:
        """Read one exact attempt identity without a latest/by-provider escape hatch."""

        self._validate_uuid(attempt_id, "attempt_id")
        return self._transaction(
            lambda cursor: self._read_generation_attempt_by_id(
                cursor, attempt_id, for_update=False
            )
        )

    def read_generation_attempt_for_slot(
        self,
        job: Job,
        command_slot_id: UUID,
    ) -> GenerationAttempt | None:
        """Resolve the latest attempt through the exact Job and command slot."""

        self._validate_uuid(command_slot_id, "command_slot_id")

        def operation(cursor: DbCursor) -> GenerationAttempt | None:
            cursor.execute(
                "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s",
                (job.job_key,),
            )
            job_row = cursor.fetchone()
            if job_row is None:
                return None
            job_id, profile = job_row
            if _text(profile) != job.profile:
                raise JobProfileMismatchError("job_key belongs to a different profile")
            cursor.execute(
                """
                SELECT attempt.attempt_id
                  FROM runtime.generation_attempts AS attempt
                  JOIN runtime.command_slots AS slot
                    ON slot.command_slot_id = attempt.command_slot_id
                   AND slot.job_id = attempt.job_id
                 WHERE attempt.job_id = %s AND attempt.command_slot_id = %s
                 ORDER BY attempt.attempt_ordinal DESC
                 LIMIT 1
                """,
                (UUID(str(job_id)), command_slot_id),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._read_generation_attempt_by_id(
                cursor,
                UUID(str(row[0])),
                for_update=False,
            )

        return self._transaction(operation)

    def read_generation_attempt_chain(
        self,
        job: Job,
        command_slot_id: UUID,
    ) -> tuple[GenerationAttempt, ...]:
        """Read the complete ordered Attempt chain for one exact command slot."""

        self._validate_uuid(command_slot_id, "command_slot_id")

        def operation(cursor: DbCursor) -> tuple[GenerationAttempt, ...]:
            cursor.execute(
                "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s",
                (job.job_key,),
            )
            job_row = cursor.fetchone()
            if job_row is None:
                return ()
            job_id, profile = job_row
            if _text(profile) != job.profile:
                raise JobProfileMismatchError("job_key belongs to a different profile")
            return self._read_generation_attempt_chain_by_slot(
                cursor, UUID(str(job_id)), command_slot_id,
            )

        return self._transaction(operation)

    def _read_generation_attempt_chain_by_slot(
        self, cursor: DbCursor, job_id: UUID, command_slot_id: UUID,
    ) -> tuple[GenerationAttempt, ...]:
        cursor.execute(
            """
            SELECT attempt_id
              FROM runtime.generation_attempts
             WHERE job_id = %s AND command_slot_id = %s
             ORDER BY attempt_ordinal
            """,
            (job_id, command_slot_id),
        )
        attempt_ids: list[UUID] = []
        while (row := cursor.fetchone()) is not None:
            attempt_ids.append(UUID(str(row[0])))
        return tuple(
            self._read_generation_attempt_by_id(cursor, item, for_update=False)
            for item in attempt_ids
        )

    def read_committed_generation_attempt_chain(
        self,
        job: Job,
        *,
        command_slot_id: UUID,
        receipt_id: UUID,
        artifact_set_id: UUID,
        expected_request_hash: str,
    ) -> tuple[GenerationAttempt, ...]:
        """Verify the complete durable Attempt/Receipt chain of one succeeded set.

        Request/response Blob bytes and domain semantics remain the caller's
        exact-reader responsibility; this checks durable invocation ownership.
        """
        if type(job) is not Job or job.profile not in ("test", "shadow", "production", "authority"):  # noqa: E721
            raise StoreValidationError("committed generation reader requires an exact Job and profile")
        for name, value in (
            ("command_slot_id", command_slot_id), ("receipt_id", receipt_id),
            ("artifact_set_id", artifact_set_id),
        ):
            self._validate_uuid(value, name)
        self._validate_sha256(expected_request_hash, "expected_request_hash")

        def operation(cursor: DbCursor) -> tuple[GenerationAttempt, ...]:
            committed = self._read_exact_committed_set_by_ids(
                cursor, job, receipt_id=receipt_id, artifact_set_id=artifact_set_id,
            )
            if (
                committed.command_slot_id != command_slot_id
                or committed.request_hash != expected_request_hash
                or committed.execution_kind != "generation"
            ):
                raise SemanticInputIntegrityError("committed generation slot/request identity differs")
            attempts = self._read_generation_attempt_chain_by_slot(
                cursor, committed.job_id, command_slot_id,
            )
            if not attempts or len({item.attempt_id for item in attempts}) != len(attempts):
                raise SemanticInputIntegrityError("committed generation Attempt chain is missing or duplicated")
            first = attempts[0]
            for index, attempt in enumerate(attempts):
                if (
                    attempt.job_id != committed.job_id
                    or attempt.command_slot_id != command_slot_id
                    or attempt.request_hash != expected_request_hash
                    or attempt.attempt_ordinal != index + 1
                    or attempt.previous_attempt_id != (None if index == 0 else attempts[index - 1].attempt_id)
                    or (attempt.provider_id, attempt.request_payload, attempt.retry_policy_hash, attempt.max_attempts)
                    != (first.provider_id, first.request_payload, first.retry_policy_hash, first.max_attempts)
                ):
                    raise SemanticInputIntegrityError("committed generation Attempt chain identity differs")
                if index < len(attempts) - 1:
                    if attempt.state != "failed" or attempt.failure_disposition != "retryable":
                        raise SemanticInputIntegrityError("committed generation predecessor is not retryable failed")
                elif (
                    attempt.state != "committed" or attempt.receipt_id != receipt_id
                    or attempt.artifact_set_id != artifact_set_id
                ):
                    raise SemanticInputIntegrityError("final generation Attempt does not bind the exact succeeded set")
            cursor.execute(
                """
                SELECT link.receipt_id, link.attempt_id, link.attempt_ordinal,
                       attempt.job_id, attempt.command_slot_id
                  FROM runtime.generation_receipt_attempts AS link
                  FULL JOIN runtime.generation_attempts AS attempt
                    ON attempt.attempt_id = link.attempt_id
                 WHERE link.receipt_id = %s OR attempt.command_slot_id = %s
                 ORDER BY link.attempt_ordinal, link.attempt_id
                """,
                (receipt_id, command_slot_id),
            )
            # FULL JOIN also exposes an unlinked extra Attempt under this slot,
            # including a corrupt foreign Job owner filtered by the chain read.
            links: list[tuple[UUID, UUID, int, UUID, UUID]] = []
            while (row := cursor.fetchone()) is not None:
                try:
                    link_receipt, link_attempt, ordinal, owner_job, owner_slot = row
                    links.append((
                        UUID(str(link_receipt)), UUID(str(link_attempt)), int(_text(ordinal)),
                        UUID(str(owner_job)), UUID(str(owner_slot)),
                    ))
                except (TypeError, ValueError) as error:
                    raise SemanticInputIntegrityError("generation Receipt links have invalid identities") from error
            expected = tuple(
                (receipt_id, item.attempt_id, item.attempt_ordinal, committed.job_id, command_slot_id)
                for item in attempts
            )
            if tuple(links) != expected:
                raise SemanticInputIntegrityError("generation Receipt links do not exactly cover the Attempt chain")
            return attempts

        return self._transaction(operation)

    # ------------------------------------------------------------------
    # read_outcome
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Media Preflight recovery frontier
    # ------------------------------------------------------------------

    def claim_media_recovery_frontier(
        self, plan: MediaRecoveryPlan
    ) -> MediaRecoveryFrontier:
        """Create or exact-read one fixed episode recovery census."""

        if type(plan) is not MediaRecoveryPlan:  # noqa: E721
            raise StoreValidationError("media recovery claim requires an exact plan")
        plan_json = json.dumps(
            plan.to_mapping(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )

        def operation(cursor: DbCursor) -> MediaRecoveryFrontier:
            base_job_id = self._require_existing_job_id(cursor, plan.base_job)
            frontier_id = uuid4()
            cursor.execute(
                """
                INSERT INTO runtime.media_preflight_recovery_frontiers
                    (frontier_id, plan_sha256, base_job_id, plan_json, episode_count)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (plan_sha256) DO NOTHING
                """,
                (
                    frontier_id,
                    plan.plan_sha256,
                    base_job_id,
                    plan_json,
                    len(plan.requirement_sha256s),
                ),
            )
            persisted = self._read_media_recovery_frontier(cursor, plan.plan_sha256)
            if persisted is None or persisted.plan != plan:
                raise StoreValidationError(
                    "media recovery plan hash does not bind the persisted plan"
                )
            return persisted

        return self._transaction(operation)

    def merge_media_recovery_successes(
        self,
        plan: MediaRecoveryPlan,
        participant_job: Job,
        entries: tuple[MediaRecoveryEntry, ...],
    ) -> MediaRecoveryFrontier:
        """Fill previously empty episode slots and elect one finalizer owner."""

        if type(plan) is not MediaRecoveryPlan or type(participant_job) is not Job:  # noqa: E721
            raise StoreValidationError("media recovery merge requires exact plan and Job")
        if type(entries) is not tuple or any(  # noqa: E721
            type(entry) is not MediaRecoveryEntry for entry in entries
        ):
            raise StoreValidationError("media recovery entries must be an exact tuple")
        if tuple(entry.episode_index for entry in entries) != tuple(
            sorted({entry.episode_index for entry in entries})
        ):
            raise StoreValidationError("media recovery merge entries must be unique and ordered")

        def operation(cursor: DbCursor) -> MediaRecoveryFrontier:
            cursor.execute(
                """
                SELECT frontier_id
                  FROM runtime.media_preflight_recovery_frontiers
                 WHERE plan_sha256 = %s
                 FOR UPDATE
                """,
                (plan.plan_sha256,),
            )
            frontier_row = cursor.fetchone()
            if frontier_row is None:
                raise StoreValidationError("media recovery frontier is not claimed")
            frontier_id = UUID(str(frontier_row[0]))
            persisted = self._read_media_recovery_frontier(cursor, plan.plan_sha256)
            if persisted is None or persisted.plan != plan:
                raise StoreValidationError("media recovery frontier plan changed")
            if persisted.state == "finalized":
                if entries and any(entry not in persisted.entries for entry in entries):
                    raise StoreValidationError("finalized recovery frontier cannot accept new entries")
                return persisted
            participant_job_id = self._require_existing_job_id(cursor, participant_job)
            changed = False
            for entry in entries:
                if (
                    entry.origin_job not in (plan.base_job, participant_job)
                    or entry.episode_index >= len(plan.requirement_sha256s)
                    or entry.requirement_sha256
                    != plan.requirement_sha256s[entry.episode_index]
                    or entry.origin_job.profile != plan.base_job.profile
                ):
                    raise StoreValidationError(
                        "media recovery entry must belong to the base or participant Job and satisfy its plan"
                    )
                origin_job_id = self._require_existing_job_id(cursor, entry.origin_job)
                cursor.execute(
                    """
                    INSERT INTO runtime.media_preflight_recovery_entries
                        (frontier_id, episode_index, requirement_sha256, origin_job_id,
                         idempotency_key, request_hash, transient_retry_budget,
                         command_slot_id, receipt_id, artifact_set_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (frontier_id, episode_index) DO NOTHING
                    """,
                    (
                        frontier_id,
                        entry.episode_index,
                        entry.requirement_sha256,
                        origin_job_id,
                        entry.idempotency_key,
                        entry.request_hash,
                        entry.transient_retry_budget,
                        entry.command_slot_id,
                        entry.receipt_id,
                        entry.artifact_set_id,
                    ),
                )
                inserted = cursor.rowcount == 1
                cursor.execute(
                    """
                    SELECT requirement_sha256, job.job_key, job.profile,
                           entry.idempotency_key, entry.request_hash,
                           entry.transient_retry_budget, entry.command_slot_id,
                           entry.receipt_id, entry.artifact_set_id
                      FROM runtime.media_preflight_recovery_entries entry
                      JOIN runtime.jobs job ON job.job_id = entry.origin_job_id
                     WHERE entry.frontier_id = %s AND entry.episode_index = %s
                    """,
                    (frontier_id, entry.episode_index),
                )
                exact = cursor.fetchone()
                if exact is None or self._decode_media_recovery_entry(
                    entry.episode_index, exact
                ) != entry:
                    raise StoreValidationError(
                        "media recovery episode slot is already sealed by another closure"
                    )
                changed = changed or inserted
            cursor.execute(
                "SELECT count(*) FROM runtime.media_preflight_recovery_entries WHERE frontier_id = %s",
                (frontier_id,),
            )
            count_row = cursor.fetchone()
            count = 0 if count_row is None else int(str(count_row[0]))
            if count > len(plan.requirement_sha256s):
                raise StoreValidationError("media recovery coverage exceeds its census")
            if persisted.state == "open" and count == len(plan.requirement_sha256s):
                cursor.execute(
                    """
                    UPDATE runtime.media_preflight_recovery_frontiers
                       SET state = 'complete', finalizer_job_id = %s,
                           version = version + 1, updated_at = transaction_timestamp()
                     WHERE frontier_id = %s AND state = 'open'
                    """,
                    (participant_job_id, frontier_id),
                )
            elif changed:
                cursor.execute(
                    """
                    UPDATE runtime.media_preflight_recovery_frontiers
                       SET version = version + 1, updated_at = transaction_timestamp()
                     WHERE frontier_id = %s AND state = 'open'
                    """,
                    (frontier_id,),
                )
            refreshed = self._read_media_recovery_frontier(cursor, plan.plan_sha256)
            if refreshed is None:
                raise StoreValidationError("media recovery frontier vanished after merge")
            return refreshed

        return self._transaction(operation)

    def mark_media_recovery_finalized(
        self,
        plan: MediaRecoveryPlan,
        finalizer_job: Job,
        outcome: CommandOutcome,
    ) -> MediaRecoveryFrontier:
        """CAS a complete frontier to the exact already-committed batch."""

        if (
            type(plan) is not MediaRecoveryPlan  # noqa: E721
            or type(finalizer_job) is not Job  # noqa: E721
            or type(outcome) is not CommandOutcome  # noqa: E721
            or outcome.state != "succeeded"
            or outcome.receipt_id is None
            or outcome.artifact_set_id is None
        ):
            raise StoreValidationError("media recovery finalization requires exact success")

        def operation(cursor: DbCursor) -> MediaRecoveryFrontier:
            cursor.execute(
                """
                SELECT frontier_id, state, finalizer_job_id, final_receipt_id,
                       final_artifact_set_id
                  FROM runtime.media_preflight_recovery_frontiers
                 WHERE plan_sha256 = %s
                 FOR UPDATE
                """,
                (plan.plan_sha256,),
            )
            row = cursor.fetchone()
            if row is None:
                raise StoreValidationError("media recovery frontier is unavailable")
            frontier_id = UUID(str(row[0]))
            job_id = self._require_existing_job_id(cursor, finalizer_job)
            if UUID(str(row[2])) != job_id:
                raise StoreValidationError("only the elected Job may finalize recovery")
            if outcome.job_id != job_id:
                raise StoreValidationError("final batch outcome belongs to another Job")
            if str(row[1]) == "finalized":
                if UUID(str(row[3])) != outcome.receipt_id or UUID(
                    str(row[4])
                ) != outcome.artifact_set_id:
                    raise StoreValidationError("replayed final batch handles changed")
            elif str(row[1]) == "complete":
                cursor.execute(
                    """
                    UPDATE runtime.media_preflight_recovery_frontiers
                       SET state = 'finalized', final_receipt_id = %s,
                           final_artifact_set_id = %s, version = version + 1,
                           updated_at = transaction_timestamp()
                     WHERE frontier_id = %s AND state = 'complete'
                    """,
                    (outcome.receipt_id, outcome.artifact_set_id, frontier_id),
                )
            else:
                raise StoreValidationError("incomplete recovery frontier cannot be finalized")
            refreshed = self._read_media_recovery_frontier(cursor, plan.plan_sha256)
            if refreshed is None:
                raise StoreValidationError("media recovery frontier vanished after finalization")
            return refreshed

        return self._transaction(operation)

    @staticmethod
    def _require_existing_job_id(cursor: DbCursor, job: Job) -> UUID:
        cursor.execute(
            "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s",
            (job.job_key,),
        )
        row = cursor.fetchone()
        if row is None or str(row[1]) != job.profile:
            raise StoreValidationError("media recovery Job is unavailable or changed profile")
        return UUID(str(row[0]))

    @staticmethod
    def _decode_media_recovery_entry(
        episode_index: int, row: tuple[object, ...]
    ) -> MediaRecoveryEntry:
        try:
            return MediaRecoveryEntry(
                episode_index,
                str(row[0]),
                Job(str(row[1]), cast(JobProfile, str(row[2]))),
                str(row[3]),
                str(row[4]),
                int(str(row[5])),
                UUID(str(row[6])),
                UUID(str(row[7])),
                UUID(str(row[8])),
            )
        except (MediaRecoveryFrontierError, TypeError, ValueError) as error:
            raise StoreValidationError("persisted media recovery entry is invalid") from error

    def _read_media_recovery_frontier(
        self, cursor: DbCursor, plan_sha256: str
    ) -> MediaRecoveryFrontier | None:
        cursor.execute(
            """
            SELECT frontier.frontier_id, frontier.plan_sha256, frontier.base_job_id,
                   frontier.plan_json, frontier.state,
                   frontier.version, final_job.job_key, final_job.profile,
                   frontier.final_receipt_id, frontier.final_artifact_set_id
              FROM runtime.media_preflight_recovery_frontiers frontier
              LEFT JOIN runtime.jobs final_job
                ON final_job.job_id = frontier.finalizer_job_id
             WHERE frontier.plan_sha256 = %s
            """,
            (plan_sha256,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        persisted_plan_sha256 = _required_text(row[1], "persisted media recovery plan hash")
        if persisted_plan_sha256 != plan_sha256:
            raise StoreValidationError("persisted media recovery plan hash differs from lookup")
        raw_plan = row[3]
        if isinstance(raw_plan, Mapping):
            plan_mapping = cast(Mapping[str, object], raw_plan)
        else:
            decoded = json.loads(str(raw_plan))
            if not isinstance(decoded, Mapping):
                raise StoreValidationError("persisted media recovery plan is not an object")
            plan_mapping = cast(Mapping[str, object], decoded)
        try:
            plan = MediaRecoveryPlan.from_mapping(plan_mapping)
        except MediaRecoveryFrontierError as error:
            raise StoreValidationError("persisted media recovery plan is invalid") from error
        if plan.plan_sha256 != persisted_plan_sha256:
            raise StoreValidationError("persisted media recovery plan bytes do not match its hash")
        base_job_id = UUID(str(row[2]))
        cursor.execute(
            "SELECT job_key, profile FROM runtime.jobs WHERE job_id = %s",
            (base_job_id,),
        )
        base_job_row = cursor.fetchone()
        if base_job_row is None or Job(str(base_job_row[0]), cast(JobProfile, str(base_job_row[1]))) != plan.base_job:
            raise StoreValidationError("persisted media recovery base Job differs from its plan")
        cursor.execute(
            """
            SELECT entry.episode_index, entry.requirement_sha256,
                   job.job_key, job.profile, entry.idempotency_key,
                   entry.request_hash, entry.transient_retry_budget,
                   entry.command_slot_id, entry.receipt_id, entry.artifact_set_id
              FROM runtime.media_preflight_recovery_entries entry
              JOIN runtime.jobs job ON job.job_id = entry.origin_job_id
             WHERE entry.frontier_id = %s
             ORDER BY entry.episode_index
            """,
                    (row[0],),
        )
        entries: list[MediaRecoveryEntry] = []
        while True:
            entry_row = cursor.fetchone()
            if entry_row is None:
                break
            entries.append(
                self._decode_media_recovery_entry(
                    int(str(entry_row[0])), entry_row[1:]
                )
            )
        finalizer_job = (
            None
            if row[6] is None
            else Job(str(row[6]), cast(JobProfile, str(row[7])))
        )
        try:
            return MediaRecoveryFrontier(
                UUID(str(row[0])),
                plan,
                cast(Literal["open", "complete", "finalized"], str(row[4])),
                int(str(row[5])),
                tuple(entries),
                finalizer_job,
                None if row[8] is None else UUID(str(row[8])),
                None if row[9] is None else UUID(str(row[9])),
            )
        except (MediaRecoveryFrontierError, TypeError, ValueError) as error:
            raise StoreValidationError("persisted media recovery frontier is invalid") from error

    def read_terminal_command_receipt(
        self,
        job: Job,
        *,
        command_slot_id: UUID,
        receipt_id: UUID,
        expected_request_hash: str,
        expected_command_name: str,
        expected_execution_kind: str,
        max_failure_detail_bytes: int,
    ) -> PersistedTerminalCommandReceipt:
        """Read an exact failed/denied Receipt, without a claim or retry decision.

        Detail is bounded logical JSONB text, never the original HTTP bytes.
        No slot-owned ArtifactSet is allowed, even if its Receipt pointer is null.
        """
        if type(job) is not Job or type(job.profile) is not str or job.profile not in (
            "test", "shadow", "production", "authority",
        ):
            raise StoreValidationError("terminal reader requires an exact Job and profile")
        for name, value in (("command_slot_id", command_slot_id), ("receipt_id", receipt_id)):
            if type(value) is not UUID:
                raise StoreValidationError(f"{name} must be an exact UUID")
        self._validate_sha256(expected_request_hash, "expected_request_hash")
        for name, value in (("job_key", job.job_key), ("expected_command_name", expected_command_name)):
            _required_text(value, name)
            try:
                value.encode("utf-8", "strict")
            except UnicodeError as error:
                raise StoreValidationError(f"{name} must be UTF-8 text") from error
        if type(expected_execution_kind) is not str or expected_execution_kind not in ("deterministic", "generation"):
            raise StoreValidationError("expected execution kind is unsupported")
        if type(max_failure_detail_bytes) is not int or max_failure_detail_bytes <= 0:
            raise StoreValidationError("max_failure_detail_bytes must be a positive integer")

        def operation(cursor: DbCursor) -> PersistedTerminalCommandReceipt:
            cursor.execute(
                """
                SELECT job.job_key, job.profile, job.job_id, slot.command_slot_id,
                       receipt.receipt_id, slot.request_hash, slot.command_name,
                       slot.execution_kind, slot.state, receipt.outcome,
                       receipt.result_artifact_set_id, receipt.failure_code,
                       CASE WHEN octet_length(convert_to(receipt.failure_detail::text, 'UTF8')) <= %s
                            THEN receipt.failure_detail::text ELSE NULL END,
                       octet_length(convert_to(receipt.failure_detail::text, 'UTF8'))
                  FROM runtime.jobs AS job
                  JOIN runtime.command_slots AS slot ON slot.job_id = job.job_id
                  JOIN runtime.command_receipts AS receipt
                    ON receipt.command_slot_id = slot.command_slot_id
                 WHERE job.job_key = %s AND job.profile = %s
                   AND slot.command_slot_id = %s AND receipt.receipt_id = %s
                   AND slot.request_hash = %s AND slot.command_name = %s
                   AND slot.execution_kind = %s
                   AND receipt.outcome IN ('failed', 'denied')
                   AND slot.state = receipt.outcome
                   AND receipt.result_artifact_set_id IS NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM runtime.artifact_sets AS owned_set
                        WHERE owned_set.command_slot_id = slot.command_slot_id
                   )
                   AND octet_length(convert_to(receipt.failure_detail::text, 'UTF8')) <= %s
                """,
                (max_failure_detail_bytes, job.job_key, job.profile, command_slot_id,
                 receipt_id, expected_request_hash, expected_command_name,
                 expected_execution_kind, max_failure_detail_bytes),
            )
            row = cursor.fetchone()
            if row is None:
                raise SemanticInputUnavailableError("exact terminal command Receipt is unavailable")
            if cursor.fetchone() is not None or len(row) != 14:
                raise SemanticInputIntegrityError("terminal command Receipt is not a unique complete row")
            (
                job_key, profile, job_id, slot_id, actual_receipt_id, request_hash,
                command_name, kind, state, outcome, set_id, code, detail, byte_count,
            ) = row
            if (
                type(job_key) is not str or type(profile) is not str
                or (job_key, profile, slot_id, actual_receipt_id, request_hash, command_name, kind)
                != (job.job_key, job.profile, command_slot_id, receipt_id, expected_request_hash,
                    expected_command_name, expected_execution_kind)
                or type(state) is not str or state != outcome or set_id is not None
            ):
                raise SemanticInputIntegrityError("terminal command Receipt ownership/state differs")
            if type(detail) is not str or type(byte_count) is not int or not 0 < byte_count <= max_failure_detail_bytes:
                raise SemanticInputIntegrityError("terminal failure detail exceeds its UTF-8 byte bound or is missing")
            try:
                if len(detail.encode("utf-8", "strict")) != byte_count:
                    raise StoreValidationError("terminal failure detail UTF-8 byte count differs")
                return PersistedTerminalCommandReceipt(
                    job, cast(UUID, job_id), cast(UUID, slot_id), cast(UUID, actual_receipt_id),
                    cast(str, request_hash), cast(str, command_name), cast(CommandExecutionKind, kind),
                    cast(Literal["failed", "denied"], outcome), cast(str, code), detail,
                )
            except (StoreValidationError, UnicodeError) as error:
                raise SemanticInputIntegrityError("terminal command Receipt payload is invalid") from error

        return self._transaction(operation)

    def read_committed_shadow_calibration_measurement(
        self, binding: CalibrationValidationBinding
    ) -> PersistedShadowCalibrationMeasurement:
        """Read the exact two-member shadow predecessor with command provenance."""
        if type(binding) is not CalibrationValidationBinding:  # noqa: E721
            raise StoreValidationError("measurement reader requires an exact validation binding")
        return self._transaction(lambda cursor: self._read_shadow_calibration_measurement(cursor, binding))

    def read_shadow_calibration_measurement_outcome(
        self,
        job: Job,
        outcome: CommandOutcome,
        *,
        expected_request_sha256: str,
        expected_profile_source_sha256: str,
        expected_registry_snapshot_sha256: str,
    ) -> PersistedShadowCalibrationMeasurement:
        """Resolve exact durable measurement refs, never a reconstructed result hash."""
        if (
            type(job) is not Job or type(outcome) is not CommandOutcome  # noqa: E721
            or outcome.state != "succeeded" or outcome.is_fresh_claim is not False
            or any(type(value) is not UUID for value in (
                outcome.command_slot_id, outcome.receipt_id, outcome.artifact_set_id
            ))
            or outcome.failure_code is not None or outcome.failure_detail_json is not None
            or (outcome.job_id is not None and type(outcome.job_id) is not UUID)
        ):
            raise StoreValidationError("measurement outcome must be exact succeeded Receipt/Set/slot")
        for name, value in (
            ("expected request hash", expected_request_sha256),
            ("expected profile source hash", expected_profile_source_sha256),
            ("expected registry snapshot hash", expected_registry_snapshot_sha256),
        ):
            self._validate_sha256(value, name)
            if value == "sha256:" + "0" * 64:
                raise StoreValidationError(f"{name} must be non-zero")
        if job != Job(expected_request_sha256.removeprefix("sha256:"), "shadow"):
            raise StoreValidationError("measurement Job does not match the expected shadow request")

        def operation(cursor: DbCursor) -> PersistedShadowCalibrationMeasurement:
            actual_job, _, command, request_hash, members = self._read_succeeded_set_members(
                cursor, cast(UUID, outcome.receipt_id), cast(UUID, outcome.artifact_set_id)
            )
            if (
                actual_job != job or request_hash != expected_request_sha256
                or members[0].command_slot_id != outcome.command_slot_id
            ):
                raise StoreValidationError("measurement outcome does not name the expected Job/request/slot")
            if outcome.job_id is not None:
                cursor.execute(
                    "SELECT job_id FROM runtime.command_slots WHERE command_slot_id = %s",
                    (outcome.command_slot_id,),
                )
                row = cursor.fetchone()
                if row is None or UUID(str(row[0])) != outcome.job_id:
                    raise StoreValidationError("measurement outcome Job UUID does not match its owner")
            return self._decode_shadow_calibration_measurement(
                actual_job, command, request_hash, members,
                expected_profile_source_sha256=expected_profile_source_sha256,
                expected_registry_snapshot_sha256=expected_registry_snapshot_sha256,
            )

        return self._transaction(operation)

    @staticmethod
    def _read_succeeded_set_members(
        cursor: DbCursor, receipt_id: UUID, artifact_set_id: UUID
    ) -> tuple[Job, str, str, str, tuple[PersistedCommittedArtifactMember, ...]]:
        cursor.execute(
            """
            SELECT job.job_key, job.profile, job.state, slot.command_slot_id,
                   slot.command_name, slot.request_hash, artifact_set.member_count,
                   member.ordinal, artifact.namespace, artifact.scope_kind,
                   artifact.scope_key, artifact.artifact_type, artifact.logical_id,
                   artifact.revision, artifact.content_hash, artifact.payload_json::text
              FROM runtime.command_receipts AS receipt
              JOIN runtime.command_slots AS slot ON slot.command_slot_id = receipt.command_slot_id
              JOIN runtime.jobs AS job ON job.job_id = slot.job_id
              JOIN runtime.artifact_sets AS artifact_set
                ON artifact_set.artifact_set_id = receipt.result_artifact_set_id
               AND artifact_set.command_slot_id = slot.command_slot_id
               AND artifact_set.job_id = job.job_id
              JOIN runtime.artifact_set_members AS member
                ON member.artifact_set_id = artifact_set.artifact_set_id
              JOIN runtime.artifacts AS artifact
                ON artifact.artifact_id = member.artifact_id
               AND artifact.artifact_set_id = artifact_set.artifact_set_id
               AND artifact.job_id = job.job_id
             WHERE receipt.receipt_id = %s AND artifact_set.artifact_set_id = %s
               AND receipt.outcome = 'succeeded' AND slot.state = 'succeeded'
             ORDER BY member.ordinal
            """,
            (receipt_id, artifact_set_id),
        )
        rows: list[tuple[object, ...]] = []
        while (row := cursor.fetchone()) is not None:
            rows.append(row)
        if not rows:
            raise MediaEvidenceUnavailableError("exact committed artifact set is unavailable")
        first = rows[0]
        if int(_text(first[6])) != len(rows) or any(
            int(_text(row[7])) != ordinal or row[:7] != first[:7]
            for ordinal, row in enumerate(rows)
        ):
            raise StoreValidationError("committed artifact set has incomplete member provenance")
        members = tuple(
            PersistedCommittedArtifactMember(
                CommittedArtifactMemberReference(
                    receipt_id, artifact_set_id, int(_text(row[7])),
                    ArtifactScope(_text(row[8]), _text(row[9]), _text(row[10])),
                    _text(row[11]), _text(row[12]), int(_text(row[13])), _text(row[14]),
                ),
                _canonical_db_json(_text(row[15])), UUID(str(row[3])),
            )
            for row in rows
        )
        return (
            Job(_text(first[0]), cast(JobProfile, _text(first[1]))),
            _text(first[2]), _text(first[4]), _text(first[5]), members,
        )

    def _read_shadow_calibration_measurement(
        self, cursor: DbCursor, binding: CalibrationValidationBinding
    ) -> PersistedShadowCalibrationMeasurement:
        manifest_ref, results_ref = binding.manifest_reference, binding.results_reference
        job, _, command, request_hash, members = self._read_succeeded_set_members(
            cursor, manifest_ref.receipt_id, manifest_ref.artifact_set_id
        )
        if tuple(member.reference for member in members) != (manifest_ref, results_ref):
            raise StoreValidationError("measurement pair does not match its exact validation references")
        return self._decode_shadow_calibration_measurement(
            job, command, request_hash, members,
            expected_profile_source_sha256=binding.profile_source_sha256,
            expected_registry_snapshot_sha256=binding.registry_snapshot_sha256,
        )

    @staticmethod
    def _decode_shadow_calibration_measurement(
        job: Job,
        command: str,
        request_hash: str,
        members: tuple[PersistedCommittedArtifactMember, ...],
        *,
        expected_profile_source_sha256: str,
        expected_registry_snapshot_sha256: str,
    ) -> PersistedShadowCalibrationMeasurement:
        """One closure shared by outcome consumers and exact validation bindings."""
        if (
            len(members) != 2
            or command != SHADOW_CALIBRATION_MEASUREMENT_COMMAND_NAME
            or job != Job(request_hash.removeprefix("sha256:"), "shadow")
            or any(
                member.reference.scope != ArtifactScope("autocut_calibration", "shadow_run", job.job_key)
                or (member.reference.member_ordinal, member.reference.artifact_type,
                    member.reference.logical_id, member.reference.revision)
                != (ordinal, artifact_type, logical_id, 1)
                or member.reference.content_hash == "sha256:" + "0" * 64
                for member, ordinal, artifact_type, logical_id in zip(
                    members, (0, 1),
                    ("calibration_measurement_manifest", "calibration_measurement_results"),
                    ("measurement-manifest", "measurement-results"), strict=True,
                )
            )
            or members[0].reference.content_hash == members[1].reference.content_hash
        ):
            raise StoreValidationError("measurement pair does not have exact shadow command provenance")
        manifest_ref = members[0].reference
        manifest = _strict_json_object(members[0].payload_json, "measurement manifest")
        results = _strict_json_object(members[1].payload_json, "measurement results")
        if (
            set(manifest) != {
                "alignment_policy_sha256", "acceptance_policy_sha256", "calibration_corpus_set_sha256",
                "measurement_request_sha256", "native_invocations", "native_port_identity_sha256",
                "registry_snapshot_sha256", "schema_version", "shadow_profile_source_sha256",
                "vad_merge_policy_sha256", "word_gap_policy_sha256",
            }
            or set(results) != {
                "measurement_manifest_sha256", "members", "per_producer_measurements", "schema_version"
            }
            or manifest.get("schema_version") != "shadow-calibration-measurement-manifest-v3"
            or results.get("schema_version") != "shadow-calibration-measurement-results-v2"
            or manifest.get("measurement_request_sha256") != request_hash
            or manifest.get("shadow_profile_source_sha256") != expected_profile_source_sha256
            or manifest.get("registry_snapshot_sha256") != expected_registry_snapshot_sha256
            or results.get("measurement_manifest_sha256") != manifest_ref.content_hash
        ):
            raise StoreValidationError("measurement v3 manifest/results do not close over the validation binding")
        invocation_values, projection_values = manifest["native_invocations"], results["members"]
        if not isinstance(invocation_values, list) or not isinstance(projection_values, list):
            raise StoreValidationError("measurement manifest/results member coverage is incomplete")
        invocations = cast(list[object], invocation_values)
        projections = cast(list[object], projection_values)
        if not invocations or len(invocations) != len(projections):
            raise StoreValidationError("measurement manifest/results member coverage is incomplete")
        seen: set[str] = set()
        for raw_invocation, raw_projection in zip(invocations, projections, strict=True):
            if not isinstance(raw_invocation, dict) or not isinstance(raw_projection, dict):
                raise StoreValidationError("measurement member must be an object")
            invocation = cast(dict[str, object], raw_invocation)
            projection = cast(dict[str, object], raw_projection)
            if (
                set(invocation) != {"corpus_member_reference_sha256", "expected_anchor_reference_sha256", "native_invocation", "native_response_blob", "raw_context"}
                or set(projection) != {"corpus_member_reference_sha256", "expected_anchor_reference_sha256", "native_invocation", "native_response_blob", "native_response_sha256", "projection"}
                or any(invocation[key] != projection[key] for key in (
                    "corpus_member_reference_sha256", "expected_anchor_reference_sha256", "native_invocation", "native_response_blob"
                ))
                or not isinstance(invocation["raw_context"], dict)
                or not isinstance(invocation["native_response_blob"], dict)
            ):
                raise StoreValidationError("measurement member provenance does not close")
            blob = cast(dict[str, object], invocation["native_response_blob"])
            corpus_ref = invocation["corpus_member_reference_sha256"]
            if not isinstance(corpus_ref, str) or corpus_ref in seen or projection["native_response_sha256"] != blob.get("content_hash"):
                raise StoreValidationError("measurement member identity is duplicated or inconsistent")
            seen.add(corpus_ref)
        return PersistedShadowCalibrationMeasurement(job, request_hash, members[0].command_slot_id, members[0], members[1])

    def read_calibration_record_anchor(
        self,
        aggregate_reference: CommittedArtifactMemberReference,
        validation_reference: CommittedArtifactMemberReference,
        *,
        expected_profile_source_sha256: str,
        expected_registry_snapshot_sha256: str,
    ) -> PersistedCalibrationRecordAnchor:
        """Resolve a consumer's exact accepted refs without requiring writer inputs."""
        if any(type(ref) is not CommittedArtifactMemberReference for ref in (aggregate_reference, validation_reference)):  # noqa: E721
            raise StoreValidationError("anchor reader requires exact committed member references")
        scope = aggregate_reference.scope
        try:
            CalibrationRecordScope(scope.namespace, scope.kind, scope.key)
        except CalibrationRecordError as error:
            raise StoreValidationError("anchor references require the protected calibration scope") from error
        if (
            aggregate_reference.receipt_id != validation_reference.receipt_id
            or aggregate_reference.artifact_set_id != validation_reference.artifact_set_id
            or scope != validation_reference.scope
            or any(
                (ref.member_ordinal, ref.artifact_type, ref.logical_id, ref.revision)
                != (ordinal, artifact_type, f"calibration-record/{role}/{scope.key}/1", 1)
                for ref, ordinal, artifact_type, role in (
                    (aggregate_reference, 0, "calibration_record", "aggregate"),
                    (validation_reference, 3, "calibration_validation_receipt", "validation"),
                )
            )
        ):
            raise StoreValidationError("anchor references must name the exact aggregate/validation pair")
        self._validate_sha256(expected_profile_source_sha256, "expected profile source hash")
        self._validate_sha256(expected_registry_snapshot_sha256, "expected registry snapshot hash")

        def operation(cursor: DbCursor) -> PersistedCalibrationRecordAnchor:
            anchor = self._read_calibration_record_anchor_closure(
                cursor, scope.key, expected_profile_source_sha256, expected_registry_snapshot_sha256
            )
            if anchor.aggregate.reference != aggregate_reference or anchor.validation.reference != validation_reference:
                raise StoreValidationError("anchor does not name the expected exact accepted members")
            return anchor

        return self._transaction(operation)

    def _read_calibration_record_anchor(
        self, cursor: DbCursor, binding: CalibrationValidationBinding
    ) -> PersistedCalibrationRecordAnchor:
        """Writer replay additionally proves the complete immutable request binding."""
        anchor = self._read_calibration_record_anchor_closure(
            cursor, binding.profile_key, binding.profile_source_sha256,
            binding.registry_snapshot_sha256, expected_request_hash=binding.request_hash,
        )
        self._validate_calibration_record_binding(binding, anchor.record)
        return anchor

    def _read_calibration_record_anchor_closure(
        self,
        cursor: DbCursor,
        profile_key: str,
        expected_profile_source_sha256: str,
        expected_registry_snapshot_sha256: str,
        *,
        expected_request_hash: str | None = None,
    ) -> PersistedCalibrationRecordAnchor:
        """Shared accepted-set decoding, immutable anchor and authority closure."""
        cursor.execute(
            """
            SELECT record_sha256, profile_source_sha256, registry_snapshot_sha256,
                   measurement_manifest_sha256, measurement_results_sha256,
                   asr_member_sha256, vad_member_sha256, validation_receipt_sha256,
                   receipt_id, artifact_set_id, command_slot_id,
                   aggregate_member_ordinal, validation_member_ordinal
              FROM runtime.calibration_record_anchors
             WHERE namespace = 'autocut_authority' AND scope_kind = 'calibration' AND scope_key = %s
            """,
            (profile_key,),
        )
        row = cursor.fetchone()
        if row is None:
            raise MediaEvidenceUnavailableError("exact calibration record anchor is unavailable")
        job, job_state, command, request_hash, members = self._read_succeeded_set_members(
            cursor, UUID(str(row[8])), UUID(str(row[9]))
        )
        if (
            job != Job(f"autocut_calibration_validator:{profile_key}", "authority")
            or job_state != "succeeded" or command != CALIBRATION_VALIDATOR_COMMAND
            or (expected_request_hash is not None and request_hash != expected_request_hash)
            or len(members) != 4
            or members[0].command_slot_id != UUID(str(row[10]))
            or (int(_text(row[11])), int(_text(row[12]))) != (0, 3)
        ):
            raise StoreValidationError("calibration anchor does not close over its succeeded authority command")
        payloads = (
            decode_calibration_record_payload(members[0].payload_json.encode("utf-8")),
            decode_calibration_record_member_payload(members[1].payload_json.encode("utf-8")),
            decode_calibration_record_member_payload(members[2].payload_json.encode("utf-8")),
            decode_calibration_validation_receipt_payload(members[3].payload_json.encode("utf-8")),
        )
        record = CalibrationRecordArtifactSet(tuple(
            CalibrationRecordArtifactMember(
                member.reference.member_ordinal, member.reference.artifact_type,
                member.reference.logical_id, member.reference.revision,
                CalibrationRecordScope(member.reference.scope.namespace, member.reference.scope.kind, member.reference.scope.key),
                member.reference.content_hash, payload,
            )
            for member, payload in zip(members, payloads, strict=True)
        ))
        if (
            record.members[0].scope.key != profile_key
            or record.aggregate.identity.profile_source_sha256 != expected_profile_source_sha256
            or record.aggregate.identity.registry_snapshot_sha256 != expected_registry_snapshot_sha256
        ):
            raise StoreValidationError("calibration anchor does not match the expected profile/registry identity")
        expected_hashes = (
            record.members[0].content_hash, expected_profile_source_sha256, expected_registry_snapshot_sha256,
            record.aggregate.measurement_manifest_sha256, record.aggregate.measurement_results_sha256,
            record.members[1].content_hash, record.members[2].content_hash, record.members[3].content_hash,
        )
        if tuple(_text(value) for value in row[:8]) != expected_hashes:
            raise StoreValidationError("calibration anchor hashes do not bind its exact accepted members")
        return PersistedCalibrationRecordAnchor(record, members[0], members[3])

    def read_committed_artifact_set(
        self,
        job: Job,
        *,
        command_slot_id: UUID,
        receipt_id: UUID,
        artifact_set_id: UUID,
        expected_request_hash: str,
        expected_command_name: str,
        expected_execution_kind: str,
    ) -> PersistedCommittedArtifactSet:
        """Read one exact succeeded set without guessing a member hash or a head.

        This generic persistence check establishes neither generation Attempt
        provenance nor domain admission. Domain readers must verify both.
        """
        if type(job) is not Job or job.profile not in ("test", "shadow", "production", "authority"):  # noqa: E721
            raise StoreValidationError("committed set reader requires an exact Job and profile")
        for name, value in (
            ("command_slot_id", command_slot_id), ("receipt_id", receipt_id),
            ("artifact_set_id", artifact_set_id),
        ):
            self._validate_uuid(value, name)
        self._validate_sha256(expected_request_hash, "expected_request_hash")
        _required_text(expected_command_name, "expected_command_name")
        if type(expected_execution_kind) is not str or expected_execution_kind not in ("deterministic", "generation"):  # noqa: E721
            raise StoreValidationError("expected execution kind is unsupported")

        def operation(cursor: DbCursor) -> PersistedCommittedArtifactSet:
            committed = self._read_exact_committed_set_by_ids(
                cursor, job, receipt_id=receipt_id, artifact_set_id=artifact_set_id,
            )
            if (
                committed.command_slot_id != command_slot_id
                or committed.request_hash != expected_request_hash
                or committed.command_name != expected_command_name
                or committed.execution_kind != expected_execution_kind
            ):
                raise SemanticInputIntegrityError("committed set producer/request identity differs")
            return PersistedCommittedArtifactSet(
                job, committed.job_id, committed.command_slot_id, receipt_id,
                artifact_set_id, committed.request_hash, committed.command_name,
                committed.execution_kind, committed.set_hash,
                tuple(
                    PersistedCommittedArtifactMember(
                        CommittedArtifactMemberReference(
                            receipt_id, artifact_set_id, ordinal, member.scope,
                            member.artifact_type, member.logical_id, member.revision,
                            member.content_hash,
                        ),
                        member.payload_json, committed.command_slot_id,
                    )
                    for ordinal, member in committed.members
                ),
            )

        return self._transaction(operation)

    def find_committed_context_pack_set(
        self,
        job: Job,
        *,
        artifact_scope: ArtifactScope,
        artifact_revision: int,
    ) -> PersistedCommittedArtifactSet | None:
        """Locate the sole committed Context Prepare output for durable replay.

        This intentionally does *not* read a logical head.  The lookup is
        bound to the exact job scope, registered producer and immutable member
        identity, so a new host can resume a run without an API credential or
        a process-local episode-map configuration.
        """
        if type(job) is not Job or type(artifact_scope) is not ArtifactScope:  # noqa: E721
            raise StoreValidationError("context replay lookup requires exact Job and ArtifactScope")
        if type(artifact_revision) is not int or artifact_revision < 1:  # noqa: E721
            raise StoreValidationError("context replay lookup requires a positive revision")

        def operation(cursor: DbCursor) -> PersistedCommittedArtifactSet | None:
            cursor.execute(
                "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s",
                (job.job_key,),
            )
            job_row = cursor.fetchone()
            if job_row is None:
                return None
            job_id, profile = job_row
            if _text(profile) != job.profile:
                raise JobProfileMismatchError("job_key belongs to a different profile")
            cursor.execute(
                """
                SELECT receipt.receipt_id, receipt.result_artifact_set_id
                  FROM runtime.command_receipts AS receipt
                  JOIN runtime.command_slots AS slot
                    ON slot.command_slot_id = receipt.command_slot_id
                  JOIN runtime.artifact_sets AS artifact_set
                    ON artifact_set.artifact_set_id = receipt.result_artifact_set_id
                   AND artifact_set.command_slot_id = slot.command_slot_id
                   AND artifact_set.job_id = slot.job_id
                  JOIN runtime.artifact_set_members AS member
                    ON member.artifact_set_id = artifact_set.artifact_set_id
                  JOIN runtime.artifacts AS artifact
                    ON artifact.artifact_id = member.artifact_id
                   AND artifact.artifact_set_id = member.artifact_set_id
                   AND artifact.job_id = slot.job_id
                 WHERE slot.job_id = %s
                   AND slot.command_name = 'PrepareWindowContextCommand'
                   AND slot.execution_kind = 'deterministic'
                   AND slot.state = 'succeeded'
                   AND receipt.outcome = 'succeeded'
                   AND artifact.namespace = %s
                   AND artifact.scope_kind = %s
                   AND artifact.scope_key = %s
                   AND artifact.artifact_type = 'window_context_pack_set'
                   AND artifact.logical_id = 'window_context_pack_set'
                   AND artifact.revision = %s
                 ORDER BY receipt.receipt_id
                """,
                (
                    UUID(str(job_id)), artifact_scope.namespace, artifact_scope.kind,
                    artifact_scope.key, artifact_revision,
                ),
            )
            rows: list[tuple[object, ...]] = []
            while (row := cursor.fetchone()) is not None:
                rows.append(row)
            if not rows:
                return None
            if len(rows) != 1:
                raise SemanticInputIntegrityError("context replay lookup is ambiguous")
            receipt_id, artifact_set_id = rows[0]
            committed = self._read_exact_committed_set_by_ids(
                cursor,
                job,
                receipt_id=UUID(str(receipt_id)),
                artifact_set_id=UUID(str(artifact_set_id)),
            )
            if committed.command_name != "PrepareWindowContextCommand" or committed.execution_kind != "deterministic":
                raise SemanticInputIntegrityError("context replay producer identity differs")
            return PersistedCommittedArtifactSet(
                job, committed.job_id, committed.command_slot_id, UUID(str(receipt_id)),
                UUID(str(artifact_set_id)), committed.request_hash, committed.command_name,
                committed.execution_kind, committed.set_hash,
                tuple(
                    PersistedCommittedArtifactMember(
                        CommittedArtifactMemberReference(
                            UUID(str(receipt_id)), UUID(str(artifact_set_id)), ordinal,
                            member.scope, member.artifact_type, member.logical_id,
                            member.revision, member.content_hash,
                        ),
                        member.payload_json,
                        committed.command_slot_id,
                    )
                    for ordinal, member in committed.members
                ),
            )

        return self._transaction(operation)

    def read_committed_artifact_member(
        self,
        reference: CommittedArtifactMemberReference,
    ) -> PersistedCommittedArtifactMember:
        """Reread one authority/predecessor member by full immutable identity.

        This deliberately has no logical-head or caller-selected Job lookup.
        A receipt, ArtifactSet, ordinal, scope and content hash must all name
        the same succeeded durable member.
        """

        if type(reference) is not CommittedArtifactMemberReference:  # noqa: E721
            raise StoreValidationError("committed member reader requires an exact reference")

        def operation(cursor: DbCursor) -> PersistedCommittedArtifactMember:
            cursor.execute(
                """
                SELECT slot.command_slot_id, artifact.payload_json::text
                  FROM runtime.command_receipts AS receipt
                  JOIN runtime.command_slots AS slot
                    ON slot.command_slot_id = receipt.command_slot_id
                  JOIN runtime.artifact_sets AS artifact_set
                    ON artifact_set.artifact_set_id = receipt.result_artifact_set_id
                   AND artifact_set.command_slot_id = slot.command_slot_id
                  JOIN runtime.artifact_set_members AS member
                    ON member.artifact_set_id = artifact_set.artifact_set_id
                  JOIN runtime.artifacts AS artifact
                    ON artifact.artifact_id = member.artifact_id
                   AND artifact.artifact_set_id = member.artifact_set_id
                 WHERE receipt.receipt_id = %s
                   AND receipt.result_artifact_set_id = %s
                   AND receipt.outcome = 'succeeded'
                   AND slot.state = 'succeeded'
                   AND member.ordinal = %s
                   AND artifact.namespace = %s
                   AND artifact.scope_kind = %s
                   AND artifact.scope_key = %s
                   AND artifact.artifact_type = %s
                   AND artifact.logical_id = %s
                   AND artifact.revision = %s
                   AND artifact.content_hash = %s
                """,
                (
                    reference.receipt_id,
                    reference.artifact_set_id,
                    reference.member_ordinal,
                    reference.scope.namespace,
                    reference.scope.kind,
                    reference.scope.key,
                    reference.artifact_type,
                    reference.logical_id,
                    reference.revision,
                    reference.content_hash,
                ),
            )
            rows: list[tuple[object, ...]] = []
            while (row := cursor.fetchone()) is not None:
                rows.append(row)
            if len(rows) != 1:
                raise StoreValidationError("exact committed artifact member is unavailable")
            slot_id, payload_json = rows[0]
            return PersistedCommittedArtifactMember(
                reference=reference,
                payload_json=_text(payload_json),
                command_slot_id=UUID(str(slot_id)),
            )

        return self._transaction(operation)

    def read_bootstrapped_timed_speech_profile(
        self,
        snapshot: AuthorityRegistrySnapshot,
    ) -> BootstrappedTimedSpeechProfile:
        """Read one profile only through its authority anchor, never a head lookup."""

        if type(snapshot) is not AuthorityRegistrySnapshot:  # noqa: E721
            raise StoreValidationError("timed speech profile reader requires an authority snapshot")

        def operation(cursor: DbCursor) -> BootstrappedTimedSpeechProfile:
            return self._read_bootstrapped_timed_speech_profile(cursor, snapshot)

        return self._transaction(operation)

    @staticmethod
    def _read_bootstrapped_timed_speech_profile(
        cursor: DbCursor,
        snapshot: AuthorityRegistrySnapshot,
    ) -> BootstrappedTimedSpeechProfile:
        key = snapshot.enabled_profile
        cursor.execute(
            """
            SELECT anchor.receipt_id, anchor.artifact_set_id, anchor.member_ordinal,
                   anchor.content_hash, artifact.payload_json::text
              FROM runtime.timed_speech_profile_anchors AS anchor
              JOIN runtime.command_receipts AS receipt
                ON receipt.receipt_id = anchor.receipt_id
               AND receipt.result_artifact_set_id = anchor.artifact_set_id
               AND receipt.outcome = 'succeeded'
              JOIN runtime.command_slots AS slot
                ON slot.command_slot_id = receipt.command_slot_id
               AND slot.command_slot_id = anchor.command_slot_id
               AND slot.state = 'succeeded'
               AND slot.command_name = %s
              JOIN runtime.jobs AS job
                ON job.job_id = slot.job_id
               AND job.job_key = %s
               AND job.profile = %s
              JOIN runtime.artifact_sets AS artifact_set
                ON artifact_set.artifact_set_id = anchor.artifact_set_id
               AND artifact_set.command_slot_id = slot.command_slot_id
              JOIN runtime.artifact_set_members AS member
                ON member.artifact_set_id = artifact_set.artifact_set_id
               AND member.ordinal = anchor.member_ordinal
              JOIN runtime.artifacts AS artifact
                ON artifact.artifact_id = member.artifact_id
               AND artifact.artifact_set_id = artifact_set.artifact_set_id
             WHERE anchor.profile_key = %s
               AND anchor.registry_set_sha256 = %s
               AND artifact.namespace = %s
               AND artifact.scope_kind = %s
               AND artifact.scope_key = %s
               AND artifact.artifact_type = %s
               AND artifact.logical_id = %s
               AND artifact.revision = 1
               AND artifact.content_hash = anchor.content_hash
            """,
            (
                BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND,
                AUTHORITY_BOOTSTRAP_JOB.job_key,
                AUTHORITY_BOOTSTRAP_JOB.profile,
                key.value,
                snapshot.registry_set_sha256,
                TIMED_SPEECH_PROFILE_REGISTRY_SCOPE.namespace,
                TIMED_SPEECH_PROFILE_REGISTRY_SCOPE.kind,
                TIMED_SPEECH_PROFILE_REGISTRY_SCOPE.key,
                TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE,
                key.logical_id,
            ),
        )
        rows: list[tuple[object, ...]] = []
        while (row := cursor.fetchone()) is not None:
            rows.append(row)
        if len(rows) != 1:
            raise StoreValidationError("authority anchored timed speech profile is unavailable")
        receipt_id, artifact_set_id, ordinal, content_hash, payload_json = rows[0]
        reference = CommittedArtifactMemberReference(
            receipt_id=UUID(str(receipt_id)),
            artifact_set_id=UUID(str(artifact_set_id)),
            member_ordinal=cast(int, ordinal),
            scope=TIMED_SPEECH_PROFILE_REGISTRY_SCOPE,
            artifact_type=TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE,
            logical_id=key.logical_id,
            revision=1,
            content_hash=_text(content_hash),
        )
        try:
            entry = decode_timed_speech_profile_registry_entry(
                _strict_json_object(_text(payload_json), "timed speech profile payload")
            )
            resolved = BootstrappedTimedSpeechProfile(snapshot, reference, entry)
        except (Stage4PredecessorError, TimedSpeechRegistryError) as error:
            raise StoreValidationError("authority anchored timed speech profile is malformed") from error
        if canonical_payload_hash(_text(payload_json)) != reference.content_hash:
            raise StoreValidationError("authority anchored timed speech profile hash does not close")
        return resolved

    def read_committed_semantic_inputs(
        self,
        request: CommittedSemanticInputsRequest,
    ) -> CommittedSemanticInputs:
        """Read exact committed Source/Window/VLM inputs without head lookup.

        Every member is resolved through its Job, profile, Receipt, ArtifactSet,
        ordinal, full artifact identity, and exact BlobRefs.  Only the Source
        owner projection and VLM Semantic Packs are returned; no
        Transcript or VAD artifact is queried by this reader.
        """

        if type(request) is not CommittedSemanticInputsRequest:  # noqa: E721
            raise StoreValidationError(
                "request must be a CommittedSemanticInputsRequest"
            )

        def operation(cursor: DbCursor) -> CommittedSemanticInputs:
            source_set = self._read_exact_committed_set(
                cursor,
                request.job,
                request.source_manifest,
            )
            if (
                source_set.command_name != "PrepareWholeSeriesSourcesCommand"
                or len(source_set.members) != 1
                or request.source_manifest.member_ordinal != 0
                or request.source_manifest.artifact_type
                != "whole_series_source_manifest"
                or request.source_manifest.logical_id
                != "whole_series_source_manifest"
                or request.source_manifest.scope != canonical_recipe_scope(request.job)
            ):
                raise SemanticInputUnavailableError(
                    "exact committed whole-series source member is unavailable"
                )
            source_artifact = source_set.members[0][1]
            try:
                declared_blobs = _source_manifest_blob_refs(source_artifact.payload_json)
                durable_blobs = tuple(
                    self._claimed_blob_ref(
                        cursor,
                        source_set.job_id,
                        blob,
                        field_name=f"semantic source proxy[{position}]",
                    )
                    for position, blob in enumerate(declared_blobs)
                )
                source_manifest = PersistedWholeSeriesSourceManifest(
                    reference=WholeSeriesSourceManifestReference(
                        source_artifact.scope,
                        source_artifact.logical_id,
                        source_artifact.revision,
                        source_artifact.content_hash,
                    ),
                    payload_json=source_artifact.payload_json,
                    proxy_blobs=durable_blobs,
                    job_id=source_set.job_id,
                    receipt_id=request.source_manifest.receipt_id,
                    artifact_set_id=request.source_manifest.artifact_set_id,
                    command_slot_id=source_set.command_slot_id,
                    source_job=request.job,
                )
                source_grant, source_windows = _strict_source_windows(source_manifest)
                source_grant.require_purpose("semantic_analysis")
            except (
                BlobIntegrityError,
                StoreValidationError,
            ) as error:
                raise SemanticInputIntegrityError(
                    "committed Source/Window input failed exact verification"
                ) from error

            aggregate_set = self._read_exact_committed_set(
                cursor,
                request.job,
                request.vlm_semantic_pack_set,
            )
            if (
                aggregate_set.command_name != VLM_BATCH_FINALIZER_COMMAND_NAME
                or len(aggregate_set.members) != 1
                or request.vlm_semantic_pack_set.scope
                != canonical_recipe_scope(request.job)
                or not self._member_matches_reference(
                    aggregate_set.members[0], request.vlm_semantic_pack_set
                )
            ):
                raise SemanticInputUnavailableError(
                    "exact committed VLM Semantic Pack set is unavailable"
                )
            try:
                aggregate = _decode_registered_vlm_semantic_pack_set(
                    aggregate_set.members[0][1].payload_json
                )
            except (StoreValidationError, TypeError, ValueError) as error:
                raise SemanticInputIntegrityError(
                    "committed VLM Semantic Pack set failed exact verification"
                ) from error
            if (
                aggregate.source_manifest_sha256
                != source_manifest.reference.content_hash
                or aggregate.source_provenance_sha256
                != source_manifest.canonical_hash
            ):
                raise SemanticInputIntegrityError(
                    "committed VLM Semantic Pack set does not bind the committed Source owner"
                )
            aggregate_policy = aggregate.request_policy
            aggregate_children = aggregate.children

            semantic_inputs: list[CommittedVlmSemanticInput] = []
            source_windows_by_episode = {
                item.identity.episode_index: item for item in source_windows
            }
            for batch_child in aggregate_children:
                member_refs = (
                    batch_child.request_record,
                    batch_child.response_record,
                    batch_child.semantic_pack,
                )
                set_ids = {item.artifact_set_id for item in member_refs}
                receipt_ids = {item.receipt_id for item in member_refs}
                if len(set_ids) != 1 or len(receipt_ids) != 1:
                    raise SemanticInputIntegrityError(
                        "committed VLM batch child members do not share one Receipt/ArtifactSet"
                    )
                try:
                    vlm_set = self._read_exact_committed_set(
                        cursor,
                        request.job,
                        batch_child.request_record,
                    )
                except SemanticInputUnavailableError as error:
                    raise SemanticInputIntegrityError(
                        "committed VLM batch child member/blob/owner closure is broken"
                    ) from error
                expected_refs = (
                    (0, "vlm_request_record", batch_child.request_record),
                    (1, "vlm_response_record", batch_child.response_record),
                    (2, "vlm_semantic_pack", batch_child.semantic_pack),
                )
                if vlm_set.command_name != "GenerateVlmEvidenceCommand" or len(
                    vlm_set.members
                ) != 3:
                    raise SemanticInputIntegrityError(
                        "committed VLM batch child ArtifactSet shape is invalid"
                    )
                if any(
                    member.scope != canonical_recipe_scope(request.job)
                    or member.revision != vlm_set.members[0][1].revision
                    for _ordinal, member in vlm_set.members
                ):
                    raise SemanticInputIntegrityError(
                        "committed VLM members do not share one Job scope/revision"
                    )
                for ordinal, artifact_type, reference in expected_refs:
                    if (
                        reference.member_ordinal != ordinal
                        or reference.artifact_type != artifact_type
                        or not self._member_matches_reference(
                            vlm_set.members[ordinal], reference
                        )
                    ):
                        raise SemanticInputIntegrityError(
                            "committed VLM batch child member/blob/owner identity is broken"
                        )
                request_artifact = vlm_set.members[0][1]
                response_artifact = vlm_set.members[1][1]
                semantic_pack_artifact = vlm_set.members[2][1]
                try:
                    cursor.execute(
                        """
                        SELECT attempt.attempt_id, slot.idempotency_key,
                               slot.request_hash, attempt.provider_id,
                               attempt.provider_idempotency_key,
                               request_blob.object_id, request_blob.content_hash,
                               request_blob.byte_length, request_blob.media_type,
                               response_blob.object_id, response_blob.content_hash,
                               response_blob.byte_length, response_blob.media_type,
                               attempt.provider_request_id, attempt.retry_policy_hash
                          FROM runtime.generation_attempts AS attempt
                          JOIN runtime.command_slots AS slot
                            ON slot.command_slot_id = attempt.command_slot_id
                           AND slot.job_id = attempt.job_id
                          JOIN storage.blob_objects AS request_blob
                            ON request_blob.object_id = attempt.request_payload_object_id
                          JOIN storage.blob_objects AS response_blob
                            ON response_blob.object_id = attempt.raw_response_object_id
                         WHERE attempt.job_id = %s
                           AND attempt.command_slot_id = %s
                           AND attempt.state = 'committed'
                           AND attempt.receipt_id = %s
                           AND attempt.artifact_set_id = %s
                        """,
                        (
                            vlm_set.job_id,
                            vlm_set.command_slot_id,
                            batch_child.request_record.receipt_id,
                            batch_child.request_record.artifact_set_id,
                        ),
                    )
                    attempt_rows: list[tuple[object, ...]] = []
                    while (attempt_row := cursor.fetchone()) is not None:
                        attempt_rows.append(attempt_row)
                    if len(attempt_rows) != 1:
                        raise StoreValidationError(
                            "VLM ArtifactSet does not bind one committed attempt"
                        )
                    (
                        attempt_id,
                        idempotency_key,
                        request_hash,
                        attempt_provider_id,
                        provider_idempotency_key,
                        request_object_id,
                        request_blob_hash,
                        request_byte_length,
                        request_media_type,
                        response_object_id,
                        response_blob_hash,
                        response_byte_length,
                        response_media_type,
                        provider_request_id,
                        attempt_retry_policy_hash,
                    ) = attempt_rows[0]
                    request_payload = BlobRef(
                        UUID(str(request_object_id)),
                        _text(request_blob_hash),
                        int(_text(request_byte_length)),
                        _text(request_media_type),
                    )
                    raw_response = BlobRef(
                        UUID(str(response_object_id)),
                        _text(response_blob_hash),
                        int(_text(response_byte_length)),
                        _text(response_media_type),
                    )
                    self._claimed_blob_ref(
                        cursor,
                        vlm_set.job_id,
                        request_payload,
                        field_name="semantic VLM request payload",
                    )
                    self._claimed_blob_ref(
                        cursor,
                        vlm_set.job_id,
                        raw_response,
                        field_name="semantic VLM raw response",
                    )
                    request_payload_json = _strict_json_object(
                        request_artifact.payload_json,
                        "VLM request record",
                    )
                    response_payload = _strict_json_object(
                        response_artifact.payload_json,
                        "VLM response record",
                    )
                    request_identity = _decode_request_identity(
                        request_payload_json["request_identity"]
                    )
                    provider_request_payload = _strict_json_object(
                        _exact_blob_bytes(
                            cursor,
                            request_payload,
                            "VLM request payload",
                        ).decode("utf-8", "strict"),
                        "VLM provider request payload",
                    )
                    pack_payload = _strict_json_object(
                        semantic_pack_artifact.payload_json,
                        "VLM Semantic Pack",
                    )
                    parser_version, semantic_version = generation_semantic_version(
                        provider_request_payload,
                        pack_payload,
                    )
                    require_batch_child_version(
                        aggregate.strategy_version,
                        parser_version,
                        semantic_version,
                    )
                    parse_policy = _decode_parse_policy(
                        provider_request_payload["parse_policy"]
                    )
                    (
                        episode_index,
                        window_manifest_sha256,
                        window_manifest_set_sha256,
                        source_manifest_sha256,
                        source_provenance_sha256,
                        request_identity_sha256,
                    ) = _vlm_request_record_projection(request_artifact.payload_json)
                    child = PersistedVlmGenerationChild(
                        reference=VlmRequestRecordReference(
                            request_artifact.scope,
                            request_artifact.logical_id,
                            request_artifact.revision,
                            request_artifact.content_hash,
                        ),
                        payload_json=request_artifact.payload_json,
                        source_job=request.job,
                        kernel_job_id=vlm_set.job_id,
                        command_slot_id=vlm_set.command_slot_id,
                        idempotency_key=_text(idempotency_key),
                        request_hash=_text(request_hash),
                        attempt_id=UUID(str(attempt_id)),
                        provider_idempotency_key=_text(provider_idempotency_key),
                        request_payload=request_payload,
                        receipt_id=batch_child.request_record.receipt_id,
                        artifact_set_id=batch_child.request_record.artifact_set_id,
                        episode_index=episode_index,
                        window_manifest_sha256=window_manifest_sha256,
                        window_manifest_set_sha256=window_manifest_set_sha256,
                        source_manifest_sha256=source_manifest_sha256,
                        source_provenance_sha256=source_provenance_sha256,
                        request_identity_sha256=request_identity_sha256,
                        parser_strategy_version=parser_version,
                        semantic_schema_version=semantic_version,
                    )
                    if (
                        child.episode_index != batch_child.episode_index
                        or child.idempotency_key != batch_child.idempotency_key
                        or child.request_hash != batch_child.request_hash
                        or child.request_policy != aggregate_policy
                    ):
                        raise StoreValidationError(
                            "VLM child does not match its aggregate identity/policy"
                        )
                    raw_bytes = _exact_blob_bytes(
                        cursor,
                        raw_response,
                        "VLM raw response",
                    )
                    if semantic_version == 4:
                        if (
                            _text(attempt_provider_id) != provider_request_payload.get("provider_id")
                            or _text(attempt_retry_policy_hash)
                            != provider_request_payload.get("retry_policy_sha256")
                        ):
                            raise StoreValidationError(
                                "V4 generation attempt differs from its frozen provider/retry policy"
                            )
                        semantic_pack = verify_v4_semantic_pack(
                            child=child,
                            artifact=semantic_pack_artifact,
                            request_record=request_payload_json,
                            request_payload=provider_request_payload,
                            pack_payload=pack_payload,
                            raw_response=raw_bytes,
                            source=source_manifest,
                        )
                        decoded = semantic_pack.semantic_pack
                    else:
                        decoded = decode_vlm_semantic_pack(pack_payload)
                        semantic_pack = PersistedVlmSemanticPack(
                            reference=VlmSemanticPackReference(
                                semantic_pack_artifact.scope,
                                semantic_pack_artifact.logical_id,
                                semantic_pack_artifact.revision,
                                semantic_pack_artifact.content_hash,
                            ),
                            payload_json=semantic_pack_artifact.payload_json,
                            semantic_pack=decoded,
                            source_child=child,
                        )
                    response = _closed_mapping(
                        response_payload,
                        frozenset(
                            {
                                "attempt_id",
                                "provider_request_id",
                                "raw_response_blob",
                                "raw_response_sha256",
                            }
                        ),
                        "VLM response record",
                    )
                    proxy_blob = _blob_ref(
                        request_payload_json["proxy_blob"],
                        "VLM request proxy_blob",
                    )
                    if (
                        response["attempt_id"] != str(child.attempt_id)
                        or response["provider_request_id"]
                        != (
                            None
                            if provider_request_id is None
                            else _text(provider_request_id)
                        )
                        or _blob_ref(
                            response["raw_response_blob"],
                            "VLM response raw_response_blob",
                        )
                        != raw_response
                        or response["raw_response_sha256"]
                        != decoded.raw_response_sha256
                        or raw_response.content_hash != decoded.raw_response_sha256
                    ):
                        raise StoreValidationError(
                            "VLM response/request blobs are internally inconsistent"
                        )
                    self._claimed_blob_ref(
                        cursor,
                        vlm_set.job_id,
                        proxy_blob,
                        field_name="semantic VLM proxy",
                    )
                    if episode_index not in source_windows_by_episode:
                        raise StoreValidationError(
                            "VLM episode_index has no committed Source owner"
                        )
                    decoded_source_window = source_windows_by_episode[episode_index]
                    source_window = decoded_source_window.identity
                    if (
                        child.source_manifest_sha256
                        != source_manifest.reference.content_hash
                        or child.source_provenance_sha256
                        != source_manifest.canonical_hash
                        or child.window_manifest_sha256
                        != source_window.window_manifest_sha256
                        or child.window_manifest_set_sha256
                        != source_window.window_manifest_set_sha256
                        or proxy_blob != source_window.proxy_blob
                        or request_identity.source_id != source_window.source_id
                        or request_identity.source_sha256
                        != source_window.source_sha256
                        or request_identity.source_clock_id
                        != source_window.source_clock_id
                        or request_identity.window_manifest_sha256
                        != source_window.window_manifest_sha256
                        or request_identity.window_manifest_set_sha256
                        != source_window.window_manifest_set_sha256
                        or request_identity.request_payload_sha256
                        != request_payload.content_hash
                        or request_identity.provider_id
                        != _text(attempt_provider_id)
                        or request_identity.proxy_blob_ref_sha256
                        != canonical_payload_hash(
                            json.dumps(
                                {
                                    "byte_length": source_window.proxy_blob.byte_length,
                                    "content_hash": source_window.proxy_blob.content_hash,
                                    "media_type": source_window.proxy_blob.media_type,
                                    "object_id": str(source_window.proxy_blob.object_id),
                                }
                            )
                        )
                    ):
                        raise StoreValidationError(
                            "VLM input does not match its committed Source/Window owner"
                        )
                    if semantic_version == 3:
                        reparsed = parse_vlm_response(
                            raw_bytes,
                            manifest=decoded_source_window.manifest,
                            manifest_set=decoded_source_window.manifest_set,
                            request_identity=request_identity,
                            policy=parse_policy,
                        )
                        if (
                            reparsed.to_mapping() != decoded.to_mapping()
                            or reparsed.canonical_hash != decoded.canonical_hash
                        ):
                            raise StoreValidationError(
                                "persisted VLM Semantic Pack does not match exact raw-response reparse"
                            )
                    semantic_inputs.append(
                        CommittedVlmSemanticInput(
                            source_window=source_window,
                            request_identity=request_identity,
                            semantic_pack=semantic_pack,
                            response_record=batch_child.response_record,
                            raw_response=raw_response,
                        )
                    )
                except (
                    BlobIntegrityError,
                    StoreValidationError,
                    TypeError,
                    ValueError,
                    VlmValidationError,
                ) as error:
                    raise SemanticInputIntegrityError(
                        "committed VLM input failed member/blob/owner verification"
                    ) from error
            source_order = {
                item.identity.window_manifest_sha256: item.canonical_order_key
                for item in source_windows
            }
            semantic_inputs.sort(
                key=lambda item: source_order[
                    item.source_window.window_manifest_sha256
                ]
            )
            if {
                item.source_window.window_manifest_sha256
                for item in semantic_inputs
            } != set(source_order):
                raise SemanticInputIntegrityError(
                    "committed Source windows and VLM inputs are not one-to-one"
                )
            return CommittedSemanticInputs(
                source_manifest,
                source_grant,
                request.vlm_semantic_pack_set,
                aggregate_policy,
                tuple(semantic_inputs),
                aggregate.strategy_version,
            )

        return self._transaction(operation)

    def read_committed_vlm_generation_child(
        self,
        job: Job,
        idempotency_key: str,
    ) -> PersistedVlmGenerationChild:
        """Reconstruct one committed VLM child from its immutable Store evidence."""

        if type(idempotency_key) is not str or not idempotency_key.strip():  # noqa: E721
            raise StoreValidationError("idempotency_key must be a non-empty string")

        verified = self._transaction(
            lambda cursor: self._read_committed_vlm_generation_child(cursor, job, idempotency_key)
        )
        return verified.child

    def read_committed_v4_semantic_child_inspection(
        self,
        job: Job,
        idempotency_key: str,
    ) -> CommittedV4SemanticChildInspection:
        """Reread one V4 child without conferring complete-batch authority."""

        if type(job) is not Job:  # noqa: E721
            raise StoreValidationError("V4 inspection job must be an exact Job")
        if type(idempotency_key) is not str or not idempotency_key.strip():  # noqa: E721
            raise StoreValidationError(
                "V4 inspection idempotency_key must be non-empty text"
            )

        def operation(cursor: DbCursor) -> CommittedV4SemanticChildInspection:
            verified = self._read_committed_vlm_generation_child(
                cursor,
                job,
                idempotency_key,
            )
            if (
                type(verified.semantic_pack) is not PersistedVlmSemanticPackV4  # noqa: E721
                or verified.source_manifest is None
                or verified.request_identity is None
            ):
                raise StoreValidationError(
                    "V4 inspection requires an exact committed V4 generation child"
                )
            source_grant, source_windows = _strict_source_windows(
                verified.source_manifest
            )
            source_grant.require_purpose("semantic_analysis")
            matches = tuple(
                item
                for item in source_windows
                if item.identity.episode_index == verified.child.episode_index
                and item.identity.window_manifest_sha256
                == verified.child.window_manifest_sha256
            )
            if len(matches) != 1:
                raise StoreValidationError(
                    "V4 inspection child has no unique committed Source window"
                )
            semantic_input = CommittedV4InspectionInput(
                source_window=matches[0].identity,
                request_identity=verified.request_identity,
                semantic_pack=verified.semantic_pack,
                response_record=verified.response_record,
                response_payload_json=verified.response_payload_json,
                provider_request_id=verified.provider_request_id,
                raw_response=verified.raw_response,
            )
            return CommittedV4SemanticChildInspection(
                source_manifest=verified.source_manifest,
                source_grant=source_grant,
                semantic_input=semantic_input,
                child_idempotency_key=idempotency_key,
            )

        return self._transaction(operation)

    def _read_committed_vlm_generation_child(
        self, cursor: DbCursor, job: Job, idempotency_key: str,
    ) -> _VerifiedVlmGenerationChild:
        cursor.execute(
            "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s",
            (job.job_key,),
        )
        job_row = cursor.fetchone()
        if job_row is None:
            raise StoreValidationError("VLM generation Job is unavailable")
        job_id, profile = job_row
        durable_job_id = UUID(str(job_id))
        if _text(profile) != job.profile:
            raise JobProfileMismatchError("job_key belongs to a different profile")
        cursor.execute(
            """
            SELECT slot.command_slot_id, slot.command_name, slot.request_hash,
                   attempt.attempt_id, attempt.request_hash,
                   attempt.provider_idempotency_key, attempt.state,
                   request_blob.object_id, request_blob.content_hash,
                   request_blob.byte_length, request_blob.media_type,
                   response_blob.object_id, response_blob.content_hash,
                   response_blob.byte_length, response_blob.media_type,
                   attempt.provider_request_id,
                   attempt.receipt_id, attempt.artifact_set_id,
                   receipt.receipt_id, receipt.outcome,
                   receipt.result_artifact_set_id, artifact_set.set_hash,
                   artifact_set.member_count
              FROM runtime.command_slots AS slot
              JOIN runtime.generation_attempts AS attempt
                ON attempt.command_slot_id = slot.command_slot_id
               AND attempt.job_id = slot.job_id
              JOIN storage.blob_objects AS request_blob
                ON request_blob.object_id = attempt.request_payload_object_id
              JOIN storage.blob_objects AS response_blob
                ON response_blob.object_id = attempt.raw_response_object_id
              JOIN runtime.command_receipts AS receipt
                ON receipt.command_slot_id = slot.command_slot_id
               AND receipt.receipt_id = attempt.receipt_id
              JOIN runtime.artifact_sets AS artifact_set
                ON artifact_set.artifact_set_id = attempt.artifact_set_id
               AND artifact_set.artifact_set_id = receipt.result_artifact_set_id
               AND artifact_set.command_slot_id = slot.command_slot_id
               AND artifact_set.job_id = slot.job_id
             WHERE slot.job_id = %s
               AND slot.idempotency_key = %s
               AND slot.command_name = 'GenerateVlmEvidenceCommand'
               AND slot.state = 'succeeded'
               AND attempt.state = 'committed'
               AND receipt.outcome = 'succeeded'
            """,
            (durable_job_id, idempotency_key),
        )
        row = cursor.fetchone()
        if row is None or cursor.fetchone() is not None:
            raise StoreValidationError(
                "exact committed VLM generation child is unavailable"
            )
        (
            command_slot_id,
            command_name,
            slot_request_hash,
            attempt_id,
            attempt_request_hash,
            provider_idempotency_key,
            attempt_state,
            request_object_id,
            request_blob_hash,
            request_byte_length,
            request_media_type,
            response_object_id,
            response_blob_hash,
            response_byte_length,
            response_media_type,
            provider_request_id,
            attempt_receipt_id,
            attempt_artifact_set_id,
            receipt_id,
            receipt_outcome,
            receipt_artifact_set_id,
            set_hash,
            member_count,
        ) = row
        durable_provider_request_id = (
            None
            if provider_request_id is None
            else _text(provider_request_id)
        )
        slot_id = UUID(str(command_slot_id))
        durable_attempt_id = UUID(str(attempt_id))
        durable_receipt_id = UUID(str(receipt_id))
        durable_artifact_set_id = UUID(str(attempt_artifact_set_id))
        if (
            _text(command_name) != "GenerateVlmEvidenceCommand"
            or _text(slot_request_hash) != _text(attempt_request_hash)
            or _text(attempt_state) != "committed"
            or UUID(str(attempt_receipt_id)) != durable_receipt_id
            or UUID(str(receipt_artifact_set_id)) != durable_artifact_set_id
            or _text(receipt_outcome) != "succeeded"
            or int(_text(member_count)) != 3
        ):
            raise StoreValidationError(
                "committed VLM generation identity is internally inconsistent"
            )
        request_payload = BlobRef(
            UUID(str(request_object_id)),
            _text(request_blob_hash),
            int(_text(request_byte_length)),
            _text(request_media_type),
        )
        self._claimed_blob_ref(
            cursor,
            durable_job_id,
            request_payload,
            field_name="VLM request payload",
        )
        raw_response = BlobRef(
            UUID(str(response_object_id)),
            _text(response_blob_hash),
            int(_text(response_byte_length)),
            _text(response_media_type),
        )
        self._claimed_blob_ref(
            cursor,
            durable_job_id,
            raw_response,
            field_name="VLM raw response",
        )
        cursor.execute(
            """
            SELECT member.ordinal, artifact.artifact_type, artifact.logical_id,
                   artifact.revision, artifact.namespace,
                   artifact.scope_kind, artifact.scope_key,
                   artifact.content_hash, artifact.payload_json::text
              FROM runtime.artifact_set_members AS member
              JOIN runtime.artifacts AS artifact
                ON artifact.artifact_id = member.artifact_id
               AND artifact.artifact_set_id = member.artifact_set_id
             WHERE member.artifact_set_id = %s
             ORDER BY member.ordinal
            """,
            (durable_artifact_set_id,),
        )
        artifacts: list[tuple[int, ArtifactMember]] = []
        while (artifact_row := cursor.fetchone()) is not None:
            (
                member_ordinal,
                artifact_type,
                logical_id,
                revision,
                namespace,
                scope_kind,
                scope_key,
                content_hash,
                payload_json,
            ) = artifact_row
            serialized = _text(payload_json)
            if canonical_payload_hash(serialized) != _text(content_hash):
                raise StoreValidationError(
                    "VLM ArtifactSet member payload hash is invalid"
                )
            artifacts.append(
                (
                    int(_text(member_ordinal)),
                    ArtifactMember(
                        _text(artifact_type),
                        _text(logical_id),
                        int(_text(revision)),
                        ArtifactScope(
                            _text(namespace),
                            _text(scope_kind),
                            _text(scope_key),
                        ),
                        _text(content_hash),
                        serialized,
                    ),
                )
            )
        ordered_artifacts = tuple(artifacts)
        if tuple(
            (ordinal, artifact.artifact_type)
            for ordinal, artifact in ordered_artifacts
        ) != (
            (0, "vlm_request_record"),
            (1, "vlm_response_record"),
            (2, "vlm_semantic_pack"),
        ):
            raise StoreValidationError(
                "VLM ArtifactSet must exact-bind request, response, and Semantic Pack"
            )
        artifact_tuple = tuple(artifact for _ordinal, artifact in ordered_artifacts)
        if any(
            item.scope != canonical_recipe_scope(job)
            or item.revision != artifact_tuple[0].revision
            for item in artifact_tuple
        ):
            raise StoreValidationError(
                "VLM ArtifactSet members do not share one Job scope/revision"
            )
        CommandSuccess(slot_id, _text(set_hash), artifact_tuple)
        record, response_record, semantic_pack_record = artifact_tuple
        response_ordinal = ordered_artifacts[1][0]
        frozen_request = _strict_json_object(
            _exact_blob_bytes(cursor, request_payload, "VLM request payload").decode("utf-8", "strict"),
            "VLM provider request payload",
        )
        pack_payload = _strict_json_object(semantic_pack_record.payload_json, "VLM Semantic Pack")
        parser_version, semantic_version = generation_semantic_version(frozen_request, pack_payload)
        (
            episode_index,
            window_manifest_sha256,
            window_manifest_set_sha256,
            source_manifest_sha256,
            source_provenance_sha256,
            request_identity_sha256,
        ) = _vlm_request_record_projection(record.payload_json)
        reference = VlmRequestRecordReference(
            record.scope,
            record.logical_id,
            record.revision,
            record.content_hash,
        )
        child = PersistedVlmGenerationChild(
            reference=reference,
            payload_json=record.payload_json,
            source_job=job,
            kernel_job_id=durable_job_id,
            command_slot_id=slot_id,
            idempotency_key=idempotency_key,
            request_hash=_text(slot_request_hash),
            attempt_id=durable_attempt_id,
            provider_idempotency_key=_text(provider_idempotency_key),
            request_payload=request_payload,
            receipt_id=durable_receipt_id,
            artifact_set_id=durable_artifact_set_id,
            episode_index=episode_index,
            window_manifest_sha256=window_manifest_sha256,
            window_manifest_set_sha256=window_manifest_set_sha256,
            source_manifest_sha256=source_manifest_sha256,
            source_provenance_sha256=source_provenance_sha256,
            request_identity_sha256=request_identity_sha256,
            parser_strategy_version=parser_version,
            semantic_schema_version=semantic_version,
        )
        try:
            source_manifest: PersistedWholeSeriesSourceManifest | None = None
            request_identity: VlmRequestIdentity | None = None
            if semantic_version == 4:
                request_record_payload = _strict_json_object(
                    record.payload_json,
                    "VLM request record",
                )
                if "request_identity" not in request_record_payload:
                    raise StoreValidationError(
                        "V4 request record is missing request_identity"
                    )
                request_identity = _decode_request_identity(
                    request_record_payload["request_identity"]
                )
                cursor.execute(
                    """
                    SELECT provider_id, retry_policy_hash, provider_request_id
                      FROM runtime.generation_attempts
                     WHERE attempt_id = %s
                    """,
                    (child.attempt_id,),
                )
                policy_row = cursor.fetchone()
                if policy_row is None or (
                    _text(policy_row[0]) != frozen_request.get("provider_id")
                    or _text(policy_row[1]) != frozen_request.get("retry_policy_sha256")
                    or (
                        None if policy_row[2] is None else _text(policy_row[2])
                    )
                    != durable_provider_request_id
                ):
                    raise StoreValidationError(
                        "V4 generation attempt differs from its frozen provider/retry identity"
                    )
                source_manifest = self._read_v4_source_owner(cursor, child)
                semantic_pack: PersistedVlmSemanticPack | PersistedVlmSemanticPackV4
                semantic_pack = verify_v4_semantic_pack(
                    child=child, artifact=semantic_pack_record,
                    request_record=request_record_payload,
                    request_payload=frozen_request, pack_payload=pack_payload,
                    raw_response=_exact_blob_bytes(cursor, raw_response, "VLM raw response"),
                    source=source_manifest,
                )
                decoded_raw_hash = semantic_pack.semantic_pack.raw_response_sha256
            else:
                decoded = decode_vlm_semantic_pack(pack_payload)
                semantic_pack = PersistedVlmSemanticPack(
                    reference=VlmSemanticPackReference(
                        semantic_pack_record.scope,
                        semantic_pack_record.logical_id,
                        semantic_pack_record.revision,
                        semantic_pack_record.content_hash,
                    ),
                    payload_json=semantic_pack_record.payload_json,
                    semantic_pack=decoded,
                    source_child=child,
                )
                decoded_raw_hash = decoded.raw_response_sha256
            response = _closed_mapping(
                _strict_json_object(
                    response_record.payload_json,
                    "VLM response record",
                ),
                frozenset(
                    {
                        "attempt_id",
                        "provider_request_id",
                        "raw_response_blob",
                        "raw_response_sha256",
                    }
                ),
                "VLM response record",
            )
            if (
                response["attempt_id"] != str(child.attempt_id)
                or response["provider_request_id"]
                != (
                    None
                    if provider_request_id is None
                    else _text(provider_request_id)
                )
                or _blob_ref(
                    response["raw_response_blob"],
                    "VLM response raw_response_blob",
                )
                != raw_response
                or response["raw_response_sha256"]
                != decoded_raw_hash
                or raw_response.content_hash != decoded_raw_hash
            ):
                raise StoreValidationError(
                    "VLM response and Semantic Pack provenance do not match"
                )
        except (StoreValidationError, VlmValidationError, TypeError, ValueError) as error:
            raise StoreValidationError(
                f"committed VLM ArtifactSet failed exact v{semantic_version} verification"
            ) from error
        return _VerifiedVlmGenerationChild(
            child=child,
            semantic_pack=semantic_pack,
            request_identity=request_identity,
            response_record=CommittedArtifactMemberReference(
                receipt_id=durable_receipt_id,
                artifact_set_id=durable_artifact_set_id,
                member_ordinal=response_ordinal,
                scope=response_record.scope,
                artifact_type=response_record.artifact_type,
                logical_id=response_record.logical_id,
                revision=response_record.revision,
                content_hash=response_record.content_hash,
            ),
            response_payload_json=response_record.payload_json,
            provider_request_id=durable_provider_request_id,
            raw_response=raw_response,
            source_manifest=source_manifest,
        )

    def read_committed_vlm_input_reference(
        self,
        job: Job,
        idempotency_key: str,
    ) -> CommittedVlmInputReference:
        """Resolve exact child references for the Kernel batch finalizer.

        Semantic consumers submit only the resulting aggregate reference; the
        authoritative reader reconstructs these child members itself.
        """

        child = self.read_committed_vlm_generation_child(job, idempotency_key)

        def operation(cursor: DbCursor) -> CommittedVlmInputReference:
            try:
                cursor.execute(
                    """
                    SELECT member.ordinal, artifact.artifact_type,
                           artifact.logical_id, artifact.revision,
                           artifact.namespace, artifact.scope_kind,
                           artifact.scope_key, artifact.content_hash,
                           artifact.payload_json::text
                      FROM runtime.artifact_set_members AS member
                      JOIN runtime.artifacts AS artifact
                        ON artifact.artifact_id = member.artifact_id
                       AND artifact.artifact_set_id = member.artifact_set_id
                     WHERE member.artifact_set_id = %s
                     ORDER BY member.ordinal
                    """,
                    (child.artifact_set_id,),
                )
                rows: list[tuple[object, ...]] = []
                while (row := cursor.fetchone()) is not None:
                    rows.append(row)
                if len(rows) != 3:
                    raise SemanticInputUnavailableError(
                        "committed VLM input does not contain three exact members"
                    )
                members: list[tuple[int, ArtifactMember]] = []
                for row in rows:
                    (
                        ordinal, artifact_type, logical_id, revision,
                        namespace, scope_kind, scope_key, content_hash, payload_json,
                    ) = row
                    serialized = _text(payload_json)
                    if canonical_payload_hash(serialized) != _text(content_hash):
                        raise StoreValidationError(
                            "committed VLM member payload hash is invalid"
                        )
                    members.append(
                        (
                            int(_text(ordinal)),
                            ArtifactMember(
                                _text(artifact_type),
                                _text(logical_id),
                                int(_text(revision)),
                                ArtifactScope(
                                    _text(namespace),
                                    _text(scope_kind),
                                    _text(scope_key),
                                ),
                                _text(content_hash),
                                serialized,
                            ),
                        )
                    )
                member_tuple = tuple(members)
                if tuple((ordinal, member.artifact_type) for ordinal, member in member_tuple) != (
                    (0, "vlm_request_record"),
                    (1, "vlm_response_record"),
                    (2, "vlm_semantic_pack"),
                ):
                    raise StoreValidationError(
                        "committed VLM member ordering is not canonical"
                    )
                cursor.execute(
                    """
                    SELECT response.object_id, response.content_hash,
                           response.byte_length, response.media_type
                      FROM runtime.generation_attempts AS attempt
                      JOIN storage.blob_objects AS response
                        ON response.object_id = attempt.raw_response_object_id
                     WHERE attempt.attempt_id = %s
                       AND attempt.state = 'committed'
                       AND attempt.receipt_id = %s
                       AND attempt.artifact_set_id = %s
                    """,
                    (child.attempt_id, child.receipt_id, child.artifact_set_id),
                )
                raw_row = cursor.fetchone()
                if raw_row is None or cursor.fetchone() is not None:
                    raise SemanticInputUnavailableError(
                        "committed VLM input lost its exact raw response"
                    )
                raw_response = BlobRef(
                    UUID(str(raw_row[0])),
                    _text(raw_row[1]),
                    int(_text(raw_row[2])),
                    _text(raw_row[3]),
                )
                request_member = member_tuple[0][1]
                request_payload = _strict_json_object(
                    request_member.payload_json,
                    "VLM request record",
                )
                proxy_blob = _blob_ref(
                    request_payload["proxy_blob"],
                    "VLM request proxy_blob",
                )
                self._claimed_blob_ref(
                    cursor,
                    child.kernel_job_id,
                    proxy_blob,
                    field_name="committed VLM proxy",
                )
                self._claimed_blob_ref(
                    cursor,
                    child.kernel_job_id,
                    child.request_payload,
                    field_name="committed VLM request payload",
                )
                self._claimed_blob_ref(
                    cursor,
                    child.kernel_job_id,
                    raw_response,
                    field_name="committed VLM raw response",
                )
                references = tuple(
                    CommittedArtifactMemberReference(
                        receipt_id=child.receipt_id,
                        artifact_set_id=child.artifact_set_id,
                        member_ordinal=ordinal,
                        scope=member.scope,
                        artifact_type=member.artifact_type,
                        logical_id=member.logical_id,
                        revision=member.revision,
                        content_hash=member.content_hash,
                    )
                    for ordinal, member in member_tuple
                )
                return CommittedVlmInputReference(
                    request_record=references[0],
                    response_record=references[1],
                    semantic_pack=references[2],
                    proxy_blob=proxy_blob,
                    request_payload=child.request_payload,
                    raw_response=raw_response,
                )
            except SemanticInputUnavailableError:
                raise
            except (BlobIntegrityError, StoreValidationError, TypeError, ValueError) as error:
                raise SemanticInputIntegrityError(
                    "committed VLM input references failed exact resolution"
                ) from error

        return self._transaction(operation)

    def read_committed_vlm_semantic_pack_set_reference(
        self,
        job: Job,
        idempotency_key: str,
    ) -> CommittedArtifactMemberReference:
        """Resolve the only committed member of one complete VLM batch.

        This is the batch-owner boundary used by downstream semantic consumers.
        Callers identify the batch command; they never assemble child references.
        """

        if type(job) is not Job:  # noqa: E721
            raise StoreValidationError("job must be a Job")
        if type(idempotency_key) is not str or not idempotency_key:  # noqa: E721
            raise StoreValidationError("idempotency_key must be a non-empty string")
        if not idempotency_key.startswith(VLM_BATCH_IDEMPOTENCY_PREFIX):
            raise StoreValidationError(
                "VLM SemanticPackSet lookup requires the reserved batch identity"
            )

        def operation(cursor: DbCursor) -> CommittedArtifactMemberReference:
            cursor.execute(
                """
                SELECT job.job_id, job.profile, slot.command_slot_id,
                       slot.request_hash,
                       receipt.receipt_id, receipt.result_artifact_set_id,
                       artifact_set.set_hash, artifact_set.member_count,
                       member.ordinal, artifact.artifact_type,
                       artifact.logical_id, artifact.revision,
                       artifact.namespace, artifact.scope_kind,
                       artifact.scope_key, artifact.content_hash,
                       artifact.payload_json::text
                  FROM runtime.jobs AS job
                  JOIN runtime.command_slots AS slot
                    ON slot.job_id = job.job_id
                   AND slot.idempotency_key = %s
                   AND slot.command_name = %s
                   AND slot.state = 'succeeded'
                  JOIN runtime.command_receipts AS receipt
                    ON receipt.command_slot_id = slot.command_slot_id
                   AND receipt.outcome = 'succeeded'
                  JOIN runtime.artifact_sets AS artifact_set
                    ON artifact_set.artifact_set_id = receipt.result_artifact_set_id
                   AND artifact_set.command_slot_id = slot.command_slot_id
                   AND artifact_set.job_id = job.job_id
                  JOIN runtime.artifact_set_members AS member
                    ON member.artifact_set_id = artifact_set.artifact_set_id
                  JOIN runtime.artifacts AS artifact
                    ON artifact.artifact_id = member.artifact_id
                   AND artifact.artifact_set_id = member.artifact_set_id
                   AND artifact.job_id = job.job_id
                 WHERE job.job_key = %s
                """,
                (
                    idempotency_key,
                    VLM_BATCH_FINALIZER_COMMAND_NAME,
                    job.job_key,
                ),
            )
            rows: list[tuple[object, ...]] = []
            while (row := cursor.fetchone()) is not None:
                rows.append(row)
            if len(rows) != 1:
                raise SemanticInputUnavailableError(
                    "committed VLM SemanticPackSet is unavailable"
                )
            (
                _job_id,
                profile,
                command_slot_id,
                request_hash,
                receipt_id,
                artifact_set_id,
                set_hash,
                member_count,
                ordinal,
                artifact_type,
                logical_id,
                revision,
                namespace,
                scope_kind,
                scope_key,
                content_hash,
                payload_json,
            ) = rows[0]
            if _text(profile) != job.profile:
                raise JobProfileMismatchError("job_key belongs to a different profile")
            serialized = _text(payload_json)
            artifact = ArtifactMember(
                _text(artifact_type),
                _text(logical_id),
                int(_text(revision)),
                ArtifactScope(
                    _text(namespace),
                    _text(scope_kind),
                    _text(scope_key),
                ),
                _text(content_hash),
                serialized,
            )
            try:
                if (
                    int(_text(member_count)) != 1
                    or int(_text(ordinal)) != 0
                    or artifact.artifact_type != "vlm_semantic_pack_set"
                    or artifact.logical_id != "vlm_semantic_pack_set"
                    or artifact.scope != canonical_recipe_scope(job)
                    or canonical_payload_hash(serialized) != artifact.content_hash
                ):
                    raise StoreValidationError(
                        "VLM SemanticPackSet member identity is invalid"
                    )
                decoded = _decode_registered_vlm_semantic_pack_set(serialized)
                if _vlm_batch_request_hash(job, artifact, decoded) != _text(request_hash):
                    raise StoreValidationError(
                        "VLM SemanticPackSet does not match its command request hash"
                    )
                CommandSuccess(
                    UUID(str(command_slot_id)),
                    _text(set_hash),
                    (artifact,),
                )
                if decoded.strategy_version == VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4:
                    self._assert_vlm_batch_child_closure(cursor, job, decoded)
            except (StoreValidationError, TypeError, ValueError) as error:
                raise SemanticInputIntegrityError(
                    "committed VLM SemanticPackSet failed immutable verification "
                    "or request hash binding"
                ) from error
            return CommittedArtifactMemberReference(
                receipt_id=UUID(str(receipt_id)),
                artifact_set_id=UUID(str(artifact_set_id)),
                member_ordinal=0,
                scope=artifact.scope,
                artifact_type=artifact.artifact_type,
                logical_id=artifact.logical_id,
                revision=artifact.revision,
                content_hash=artifact.content_hash,
            )

        return self._transaction(operation)

    # ------------------------------------------------------------------
    # read_whole_series_source_manifest
    # ------------------------------------------------------------------

    def read_whole_series_source_manifest(
        self,
        job: Job,
        artifact_set_id: UUID,
    ) -> PersistedWholeSeriesSourceManifest:
        """Read one exact succeeded source manifest and its claimed proxy blobs."""

        self._validate_uuid(artifact_set_id, "artifact_set_id")

        def operation(cursor: DbCursor) -> PersistedWholeSeriesSourceManifest:
            cursor.execute(
                "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s",
                (job.job_key,),
            )
            job_row = cursor.fetchone()
            if job_row is None:
                raise StoreValidationError("job has no whole-series source manifest")
            job_id, profile = job_row
            durable_job_id = UUID(str(job_id))
            if _text(profile) != job.profile:
                raise JobProfileMismatchError("job_key belongs to a different profile")
            cursor.execute(
                """
                SELECT slot.command_name, artifact.logical_id, artifact.revision, artifact.namespace,
                       artifact.scope_kind, artifact.scope_key, artifact.content_hash,
                       artifact.payload_json::text, artifact_set.set_hash,
                       artifact_set.member_count, receipt.receipt_id, slot.command_slot_id
                  FROM runtime.artifact_sets AS artifact_set
                  JOIN runtime.command_slots AS slot
                    ON slot.command_slot_id = artifact_set.command_slot_id
                   AND slot.job_id = artifact_set.job_id
                  JOIN runtime.command_receipts AS receipt
                    ON receipt.command_slot_id = slot.command_slot_id
                   AND receipt.result_artifact_set_id = artifact_set.artifact_set_id
                  JOIN runtime.artifact_set_members AS member
                    ON member.artifact_set_id = artifact_set.artifact_set_id
                   AND member.ordinal = 0
                  JOIN runtime.artifacts AS artifact
                    ON artifact.artifact_id = member.artifact_id
                   AND artifact.artifact_set_id = artifact_set.artifact_set_id
                   AND artifact.job_id = artifact_set.job_id
                 WHERE artifact_set.artifact_set_id = %s
                   AND artifact_set.job_id = %s
                   AND slot.command_name IN (
                       'PrepareWholeSeriesSourcesCommand',
                       'BindWholeSeriesSourcesCommand'
                   )
                   AND slot.state = 'succeeded'
                   AND receipt.outcome = 'succeeded'
                   AND artifact.artifact_type = 'whole_series_source_manifest'
                   AND artifact.logical_id = 'whole_series_source_manifest'
                """,
                (artifact_set_id, durable_job_id),
            )
            rows: list[tuple[object, ...]] = []
            while (row := cursor.fetchone()) is not None:
                rows.append(row)
            if len(rows) != 1:
                raise StoreValidationError(
                    "exact succeeded whole-series source manifest is unavailable"
                )
            (
                command_name,
                logical_id,
                revision,
                namespace,
                scope_kind,
                scope_key,
                content_hash,
                payload_json,
                set_hash,
                member_count,
                receipt_id,
                command_slot_id,
            ) = rows[0]
            command = _text(command_name)
            count = int(_text(member_count))
            if command == "PrepareWholeSeriesSourcesCommand" and count != 1:
                raise StoreValidationError("SourcePrep manifest ArtifactSet is not singleton")
            if command == SOURCE_REUSE_COMMAND_NAME and count != 2:
                raise StoreValidationError("source reuse ArtifactSet must contain source and binding")
            if command not in ("PrepareWholeSeriesSourcesCommand", SOURCE_REUSE_COMMAND_NAME):
                raise StoreValidationError("source manifest command is not registered")
            scope = ArtifactScope(_text(namespace), _text(scope_kind), _text(scope_key))
            if scope != canonical_recipe_scope(job):
                raise StoreValidationError("source manifest has a non-canonical Job scope")
            reference = WholeSeriesSourceManifestReference(
                scope,
                _text(logical_id),
                int(_text(revision)),
                _text(content_hash),
            )
            serialized = _text(payload_json)
            artifact = ArtifactMember(
                reference.artifact_type,
                reference.logical_id,
                reference.revision,
                reference.scope,
                reference.content_hash,
                serialized,
            )
            slot_id = UUID(str(command_slot_id))
            declared_blobs = _source_manifest_blob_refs(serialized)
            durable_blobs = tuple(
                self._claimed_blob_ref(
                    cursor,
                    durable_job_id,
                    blob,
                    field_name=f"source manifest proxy[{position}]",
                )
                for position, blob in enumerate(declared_blobs)
            )
            persisted = PersistedWholeSeriesSourceManifest(
                reference,
                serialized,
                durable_blobs,
                durable_job_id,
                UUID(str(receipt_id)),
                artifact_set_id,
                slot_id,
                job,
            )
            _strict_source_windows(persisted)
            if command == SOURCE_REUSE_COMMAND_NAME:
                binding_member = self._validate_source_reuse_binding_member(
                    cursor, artifact_set_id, job, persisted
                )
                CommandSuccess(slot_id, _text(set_hash), (artifact, binding_member))
            else:
                CommandSuccess(slot_id, _text(set_hash), (artifact,))
            return persisted

        return self._transaction(operation)

    def is_source_reuse_binding(self, job: Job, outcome: CommandOutcome) -> bool:
        """Return whether one exact terminal outcome is a protected source binding.

        The Pipeline uses this narrow query only to decide whether its
        ``source_prep`` projection can be replayed without resolving the
        original host path.  It is not a generic provenance lookup.
        """

        if type(job) is not Job or type(outcome) is not CommandOutcome:  # noqa: E721
            raise StoreValidationError("source reuse binding lookup requires exact values")
        if (
            outcome.state != "succeeded"
            or outcome.receipt_id is None
            or outcome.artifact_set_id is None
        ):
            return False

        def operation(cursor: DbCursor) -> bool:
            cursor.execute(
                """
                SELECT 1
                  FROM runtime.jobs AS job
                  JOIN runtime.command_slots AS slot
                    ON slot.job_id = job.job_id
                  JOIN runtime.command_receipts AS receipt
                    ON receipt.command_slot_id = slot.command_slot_id
                 WHERE job.job_key = %s
                   AND job.profile = %s
                   AND slot.command_slot_id = %s
                   AND slot.command_name = %s
                   AND slot.state = 'succeeded'
                   AND receipt.outcome = 'succeeded'
                   AND receipt.receipt_id = %s
                   AND receipt.result_artifact_set_id = %s
                """,
                (
                    job.job_key,
                    job.profile,
                    outcome.command_slot_id,
                    SOURCE_REUSE_COMMAND_NAME,
                    outcome.receipt_id,
                    outcome.artifact_set_id,
                ),
            )
            row = cursor.fetchone()
            return row is not None and cursor.fetchone() is None

        return self._transaction(operation)

    # ------------------------------------------------------------------
    # read_recipe
    # ------------------------------------------------------------------

    def read_recipe(self, job: Job, reference: RecipeReference) -> PersistedRecipe:
        """Read one exact, succeeded immutable Recipe artifact for ``job``.

        This deliberately has no "latest" mode: callers must supply the full
        scope/type/logical/revision/content identity.  In particular, it never
        consults ``runtime.logical_heads``.
        """

        def operation(cursor: DbCursor) -> PersistedRecipe:
            cursor.execute(
                "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s",
                (job.job_key,),
            )
            job_row = cursor.fetchone()
            if job_row is None:
                raise RecipeUnavailableError("job has no persisted recipe")
            job_id, profile = job_row
            if _text(profile) != job.profile:
                raise JobProfileMismatchError("job_key belongs to a different profile")

            cursor.execute(
                """
                SELECT artifact.payload_json::text, artifact.job_id, receipt.receipt_id,
                       artifact_set.artifact_set_id, slot.command_slot_id
                  FROM runtime.artifacts AS artifact
                  JOIN runtime.artifact_set_members AS member
                    ON member.artifact_set_id = artifact.artifact_set_id
                   AND member.artifact_id = artifact.artifact_id
                  JOIN runtime.artifact_sets AS artifact_set
                    ON artifact_set.artifact_set_id = artifact.artifact_set_id
                   AND artifact_set.job_id = artifact.job_id
                  JOIN runtime.command_slots AS slot
                    ON slot.command_slot_id = artifact_set.command_slot_id
                   AND slot.job_id = artifact.job_id
                  JOIN runtime.command_receipts AS receipt
                    ON receipt.command_slot_id = slot.command_slot_id
                   AND receipt.result_artifact_set_id = artifact_set.artifact_set_id
                 WHERE artifact.job_id = %s
                   AND artifact.namespace = %s
                   AND artifact.scope_kind = %s
                   AND artifact.scope_key = %s
                   AND artifact.artifact_type = %s
                   AND artifact.logical_id = %s
                   AND artifact.revision = %s
                   AND artifact.content_hash = %s
                   AND slot.state = 'succeeded'
                   AND receipt.outcome = 'succeeded'
                """,
                (
                    UUID(str(job_id)),
                    reference.scope.namespace,
                    reference.scope.kind,
                    reference.scope.key,
                    reference.artifact_type,
                    reference.logical_id,
                    reference.revision,
                    reference.content_hash,
                ),
            )
            rows: list[tuple[object, ...]] = []
            while (row := cursor.fetchone()) is not None:
                rows.append(row)
            if not rows:
                raise RecipeUnavailableError("exact recipe artifact is unavailable")
            if len(rows) != 1:
                raise RecipeIntegrityError("exact recipe identity resolved to multiple durable rows")
            payload_json, result_job_id, receipt_id, artifact_set_id, command_slot_id = rows[0]
            try:
                return PersistedRecipe(
                    reference=reference,
                    payload_json=_text(payload_json),
                    job_id=UUID(str(result_job_id)),
                    receipt_id=UUID(str(receipt_id)),
                    artifact_set_id=UUID(str(artifact_set_id)),
                    command_slot_id=UUID(str(command_slot_id)),
                )
            except (StoreValidationError, TypeError, ValueError) as error:
                raise RecipeIntegrityError("persisted recipe payload failed immutable hash validation") from error

        return self._transaction(operation)

    # ------------------------------------------------------------------
    # read_media_evidence
    # ------------------------------------------------------------------

    def read_media_evidence(
        self, job: Job, reference: MediaEvidenceReference
    ) -> PersistedMediaEvidence:
        """Read one exact, succeeded immutable MediaEvidence artifact for ``job``.

        Callers must provide the full scope/type/logical/revision/content
        identity. This query intentionally does not use ``runtime.logical_heads``.
        """

        def operation(cursor: DbCursor) -> PersistedMediaEvidence:
            cursor.execute(
                "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s",
                (job.job_key,),
            )
            job_row = cursor.fetchone()
            if job_row is None:
                raise MediaEvidenceUnavailableError("job has no persisted media evidence")
            job_id, profile = job_row
            if _text(profile) != job.profile:
                raise JobProfileMismatchError("job_key belongs to a different profile")

            cursor.execute(
                """
                SELECT artifact.payload_json::text, artifact.job_id, receipt.receipt_id,
                       artifact_set.artifact_set_id, slot.command_slot_id
                  FROM runtime.artifacts AS artifact
                  JOIN runtime.artifact_set_members AS member
                    ON member.artifact_set_id = artifact.artifact_set_id
                   AND member.artifact_id = artifact.artifact_id
                  JOIN runtime.artifact_sets AS artifact_set
                    ON artifact_set.artifact_set_id = artifact.artifact_set_id
                   AND artifact_set.job_id = artifact.job_id
                  JOIN runtime.command_slots AS slot
                    ON slot.command_slot_id = artifact_set.command_slot_id
                   AND slot.job_id = artifact.job_id
                  JOIN runtime.command_receipts AS receipt
                    ON receipt.command_slot_id = slot.command_slot_id
                   AND receipt.result_artifact_set_id = artifact_set.artifact_set_id
                 WHERE artifact.job_id = %s
                   AND artifact.namespace = %s
                   AND artifact.scope_kind = %s
                   AND artifact.scope_key = %s
                   AND artifact.artifact_type = %s
                   AND artifact.logical_id = %s
                   AND artifact.revision = %s
                   AND artifact.content_hash = %s
                   AND slot.state = 'succeeded'
                   AND receipt.outcome = 'succeeded'
                """,
                (
                    UUID(str(job_id)),
                    reference.scope.namespace,
                    reference.scope.kind,
                    reference.scope.key,
                    reference.artifact_type,
                    reference.logical_id,
                    reference.revision,
                    reference.content_hash,
                ),
            )
            rows: list[tuple[object, ...]] = []
            while (row := cursor.fetchone()) is not None:
                rows.append(row)
            if not rows:
                raise MediaEvidenceUnavailableError("exact media evidence artifact is unavailable")
            if len(rows) != 1:
                raise MediaEvidenceIntegrityError(
                    "exact media evidence identity resolved to multiple durable rows"
                )
            payload_json, result_job_id, receipt_id, artifact_set_id, command_slot_id = rows[0]
            try:
                return PersistedMediaEvidence(
                    reference=reference,
                    payload_json=_text(payload_json),
                    job_id=UUID(str(result_job_id)),
                    receipt_id=UUID(str(receipt_id)),
                    artifact_set_id=UUID(str(artifact_set_id)),
                    command_slot_id=UUID(str(command_slot_id)),
                )
            except (StoreValidationError, TypeError, ValueError) as error:
                raise MediaEvidenceIntegrityError(
                    "persisted media evidence payload failed immutable hash validation"
                ) from error

        return self._transaction(operation)

    # ------------------------------------------------------------------
    # read_succeeded_media_outputs
    # ------------------------------------------------------------------

    def read_succeeded_media_outputs(self, job: Job) -> PersistedMediaOutputs:
        """Read the exact paired output from one succeeded LocalMediaCommand.

        This is deliberately not a generic artifact or logical-head lookup.
        One query binds the Job, command slot, success receipt, ArtifactSet,
        and both members before canonical payload hashes are verified.
        """

        def operation(cursor: DbCursor) -> PersistedMediaOutputs:
            cursor.execute(
                "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s",
                (job.job_key,),
            )
            job_row = cursor.fetchone()
            if job_row is None:
                raise MediaOutputsUnavailableError("job has no succeeded local media outputs")
            job_id, profile = job_row
            if _text(profile) != job.profile:
                raise JobProfileMismatchError("job_key belongs to a different profile")

            cursor.execute(
                """
                SELECT evidence.logical_id, evidence.revision, evidence.content_hash,
                       evidence.payload_json::text, recipe.logical_id, recipe.revision,
                       recipe.content_hash, recipe.payload_json::text, artifact_set.artifact_set_id,
                       receipt.receipt_id, slot.command_slot_id
                  FROM runtime.command_slots AS slot
                  JOIN runtime.artifact_sets AS artifact_set
                    ON artifact_set.command_slot_id = slot.command_slot_id
                   AND artifact_set.job_id = slot.job_id
                  JOIN runtime.command_receipts AS receipt
                    ON receipt.command_slot_id = slot.command_slot_id
                   AND receipt.result_artifact_set_id = artifact_set.artifact_set_id
                  JOIN runtime.artifacts AS evidence
                    ON evidence.artifact_set_id = artifact_set.artifact_set_id
                   AND evidence.job_id = artifact_set.job_id
                   AND evidence.artifact_type = 'media_evidence'
                   AND evidence.logical_id = 'media_evidence'
                   AND evidence.namespace = 'pipeline'
                   AND evidence.scope_kind = 'job'
                   AND evidence.scope_key = %s
                  JOIN runtime.artifact_set_members AS evidence_member
                    ON evidence_member.artifact_set_id = artifact_set.artifact_set_id
                   AND evidence_member.artifact_id = evidence.artifact_id
                  JOIN runtime.artifacts AS recipe
                    ON recipe.artifact_set_id = artifact_set.artifact_set_id
                   AND recipe.job_id = artifact_set.job_id
                   AND recipe.artifact_type = 'recipe'
                   AND recipe.logical_id = 'recipe'
                   AND recipe.namespace = 'pipeline'
                   AND recipe.scope_kind = 'job'
                   AND recipe.scope_key = %s
                  JOIN runtime.artifact_set_members AS recipe_member
                    ON recipe_member.artifact_set_id = artifact_set.artifact_set_id
                   AND recipe_member.artifact_id = recipe.artifact_id
                 WHERE slot.job_id = %s
                   AND slot.command_name = 'local_media_command'
                   AND slot.state = 'succeeded'
                   AND receipt.outcome = 'succeeded'
                   AND artifact_set.member_count = 2
                """,
                (job.job_key, job.job_key, UUID(str(job_id))),
            )
            rows: list[tuple[object, ...]] = []
            while (row := cursor.fetchone()) is not None:
                rows.append(row)
            if not rows:
                raise MediaOutputsUnavailableError("no succeeded LocalMediaCommand output pair is available")
            if len(rows) != 1:
                raise MediaOutputsIntegrityError("succeeded media output pair resolved to multiple durable rows")
            (
                evidence_logical_id,
                evidence_revision,
                evidence_hash,
                evidence_payload,
                recipe_logical_id,
                recipe_revision,
                recipe_hash,
                recipe_payload,
                artifact_set_id,
                receipt_id,
                command_slot_id,
            ) = rows[0]
            try:
                evidence_reference = MediaEvidenceReference(
                    canonical_recipe_scope(job),
                    _text(evidence_logical_id),
                    int(_text(evidence_revision)),
                    _text(evidence_hash),
                )
                recipe_reference = RecipeReference(
                    canonical_recipe_scope(job),
                    _text(recipe_logical_id),
                    int(_text(recipe_revision)),
                    _text(recipe_hash),
                )
                if canonical_payload_hash(_text(evidence_payload)) != evidence_reference.content_hash:
                    raise StoreValidationError("media evidence payload hash does not match artifact identity")
                if canonical_payload_hash(_text(recipe_payload)) != recipe_reference.content_hash:
                    raise StoreValidationError("recipe payload hash does not match artifact identity")
                return PersistedMediaOutputs(
                    evidence_reference,
                    recipe_reference,
                    UUID(str(job_id)),
                    UUID(str(receipt_id)),
                    UUID(str(artifact_set_id)),
                    UUID(str(command_slot_id)),
                )
            except (StoreValidationError, TypeError, ValueError) as error:
                raise MediaOutputsIntegrityError(
                    "succeeded media output pair failed immutable provenance validation"
                ) from error

        return self._transaction(operation)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _member_matches_reference(
        member: tuple[int, ArtifactMember],
        reference: CommittedArtifactMemberReference,
    ) -> bool:
        ordinal, artifact = member
        return (
            ordinal == reference.member_ordinal
            and artifact.scope == reference.scope
            and artifact.artifact_type == reference.artifact_type
            and artifact.logical_id == reference.logical_id
            and artifact.revision == reference.revision
            and artifact.content_hash == reference.content_hash
        )

    def _read_v4_source_owner(
        self, cursor: DbCursor, child: PersistedVlmGenerationChild,
    ) -> PersistedWholeSeriesSourceManifest:
        """Resolve the frozen same-Job owner, never a logical head or another Job."""
        cursor.execute(
            """
            SELECT receipt.receipt_id, artifact.artifact_set_id
              FROM runtime.artifacts AS artifact
              JOIN runtime.artifact_sets AS artifact_set
                ON artifact_set.artifact_set_id = artifact.artifact_set_id
               AND artifact_set.job_id = artifact.job_id
              JOIN runtime.command_receipts AS receipt
                ON receipt.result_artifact_set_id = artifact_set.artifact_set_id
               AND receipt.command_slot_id = artifact_set.command_slot_id
               AND receipt.outcome = 'succeeded'
              JOIN runtime.command_slots AS slot
                ON slot.command_slot_id = artifact_set.command_slot_id
               AND slot.job_id = artifact.job_id
               AND slot.command_name = 'PrepareWholeSeriesSourcesCommand'
               AND slot.state = 'succeeded'
             WHERE artifact.job_id = %s AND artifact.content_hash = %s
               AND artifact.artifact_type = 'whole_series_source_manifest'
               AND artifact.logical_id = 'whole_series_source_manifest'
            """,
            (child.kernel_job_id, child.source_manifest_sha256),
        )
        owners: list[tuple[UUID, UUID]] = []
        while (row := cursor.fetchone()) is not None:
            owners.append((UUID(str(row[0])), UUID(str(row[1]))))
        matches: list[PersistedWholeSeriesSourceManifest] = []
        for receipt_id, artifact_set_id in owners:
            committed = self._read_exact_committed_set_by_ids(
                cursor, child.source_job, receipt_id=receipt_id, artifact_set_id=artifact_set_id,
            )
            if committed.command_name != "PrepareWholeSeriesSourcesCommand" or len(committed.members) != 1:
                raise StoreValidationError("V4 source owner is not an exact singleton SourcePrep set")
            ordinal, artifact = committed.members[0]
            if ordinal != 0 or artifact.scope != canonical_recipe_scope(child.source_job):
                raise StoreValidationError("V4 source owner scope or ordinal is invalid")
            source = PersistedWholeSeriesSourceManifest(
                reference=WholeSeriesSourceManifestReference(
                    artifact.scope, artifact.logical_id, artifact.revision, artifact.content_hash,
                ),
                payload_json=artifact.payload_json,
                proxy_blobs=tuple(
                    self._claimed_blob_ref(cursor, committed.job_id, blob, field_name="V4 source proxy")
                    for blob in _source_manifest_blob_refs(artifact.payload_json)
                ),
                job_id=committed.job_id, receipt_id=receipt_id, artifact_set_id=artifact_set_id,
                command_slot_id=committed.command_slot_id, source_job=child.source_job,
            )
            if source.canonical_hash == child.source_provenance_sha256:
                matches.append(source)
        if len(matches) != 1:
            raise StoreValidationError("V4 exact same-Job committed Source owner is unavailable or ambiguous")
        return matches[0]

    def _assert_vlm_batch_child_closure(
        self,
        cursor: DbCursor,
        job: Job,
        decoded: _DecodedVlmSemanticPackSet,
    ) -> None:
        for child in decoded.children:
            try:
                committed = self._read_exact_committed_set(
                    cursor,
                    job,
                    child.request_record,
                )
            except SemanticInputUnavailableError as error:
                raise StoreValidationError(
                    "VLM SemanticPackSet child closure is unavailable"
                ) from error
            references = (
                child.request_record,
                child.response_record,
                child.semantic_pack,
            )
            if (
                committed.command_name != "GenerateVlmEvidenceCommand"
                or len(committed.members) != 3
                or any(
                    not self._member_matches_reference(
                        committed.members[ordinal],
                        reference,
                    )
                    for ordinal, reference in enumerate(references)
                )
            ):
                raise StoreValidationError(
                    "VLM SemanticPackSet child member closure is invalid"
                )
            cursor.execute(
                """
                SELECT idempotency_key, request_hash
                  FROM runtime.command_slots
                 WHERE command_slot_id = %s
                   AND job_id = %s
                """,
                (committed.command_slot_id, committed.job_id),
            )
            slot_row = cursor.fetchone()
            if slot_row is None or cursor.fetchone() is not None:
                raise StoreValidationError(
                    "VLM SemanticPackSet child command identity is unavailable"
                )
            request_artifact = committed.members[0][1]
            request_payload = _strict_json_object(
                request_artifact.payload_json,
                "VLM request record",
            )
            request_identity = _decode_request_identity(
                request_payload["request_identity"]
            )
            request_blob = _blob_ref(request_payload["request_payload_blob"], "VLM request payload")
            self._claimed_blob_ref(cursor, committed.job_id, request_blob, field_name="VLM batch request payload")
            frozen_request = _strict_json_object(
                _exact_blob_bytes(cursor, request_blob, "VLM batch request payload").decode("utf-8", "strict"),
                "VLM batch frozen request",
            )
            parser_version, schema_version = generation_semantic_version(
                frozen_request, _strict_json_object(committed.members[2][1].payload_json, "VLM batch pack"),
            )
            require_batch_child_version(decoded.strategy_version, parser_version, schema_version)
            if schema_version == 4:
                verified = self._read_committed_vlm_generation_child(
                    cursor,
                    job,
                    child.idempotency_key,
                ).child
                if (
                    verified.receipt_id != child.request_record.receipt_id
                    or verified.artifact_set_id != child.request_record.artifact_set_id
                ):
                    raise StoreValidationError("V4 batch child differs from its verified generation")
            (
                episode_index,
                _window_manifest_sha256,
                _window_manifest_set_sha256,
                source_manifest_sha256,
                source_provenance_sha256,
                _request_identity_sha256,
            ) = _vlm_request_record_projection(request_artifact.payload_json)
            request_policy = VlmBatchRequestPolicy(
                prompt_template_sha256=request_identity.prompt_template_sha256,
                prompt_version=request_identity.prompt_version,
                response_schema_sha256=request_identity.response_schema_sha256,
                preprocess_policy_sha256=request_identity.preprocess_policy_sha256,
                window_sampling_policy_sha256=(
                    request_identity.window_sampling_policy_sha256
                ),
                model_id=request_identity.model_id,
                provider_id=request_identity.provider_id,
                request_parameters_sha256=(
                    request_identity.request_parameters_sha256
                ),
                parse_policy_sha256=request_identity.parse_policy_sha256,
            )
            if (
                _text(slot_row[0]) != child.idempotency_key
                or _text(slot_row[1]) != child.request_hash
                or episode_index != child.episode_index
                or source_manifest_sha256 != decoded.source_manifest_sha256
                or source_provenance_sha256 != decoded.source_provenance_sha256
                or request_policy != decoded.request_policy
            ):
                raise StoreValidationError(
                    "VLM SemanticPackSet child identity/policy/source binding is invalid"
                )

    def _read_exact_committed_set(
        self,
        cursor: DbCursor,
        job: Job,
        reference: CommittedArtifactMemberReference,
    ) -> _CommittedSet:
        committed = self._read_exact_committed_set_by_ids(
            cursor, job, receipt_id=reference.receipt_id,
            artifact_set_id=reference.artifact_set_id,
        )
        if reference.member_ordinal >= len(committed.members) or not self._member_matches_reference(
            committed.members[reference.member_ordinal], reference
        ):
            raise SemanticInputUnavailableError(
                "exact committed semantic member identity is unavailable"
            )
        return committed

    def _read_exact_committed_set_by_ids(
        self,
        cursor: DbCursor,
        job: Job,
        *,
        receipt_id: UUID,
        artifact_set_id: UUID,
    ) -> _CommittedSet:
        cursor.execute(
            "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s",
            (job.job_key,),
        )
        job_row = cursor.fetchone()
        if job_row is None:
            raise SemanticInputUnavailableError(
                "exact committed semantic input Job is unavailable"
            )
        job_id, profile = job_row
        durable_job_id = UUID(str(job_id))
        if _text(profile) != job.profile:
            raise JobProfileMismatchError("job_key belongs to a different profile")
        cursor.execute(
            """
            SELECT slot.command_slot_id, slot.command_name, artifact_set.set_hash,
                   artifact_set.member_count, slot.request_hash, slot.execution_kind
              FROM runtime.command_receipts AS receipt
              JOIN runtime.command_slots AS slot
                ON slot.command_slot_id = receipt.command_slot_id
               AND slot.job_id = %s
              JOIN runtime.artifact_sets AS artifact_set
                ON artifact_set.artifact_set_id = receipt.result_artifact_set_id
               AND artifact_set.command_slot_id = slot.command_slot_id
               AND artifact_set.job_id = slot.job_id
             WHERE receipt.receipt_id = %s
               AND receipt.result_artifact_set_id = %s
               AND receipt.outcome = 'succeeded'
               AND slot.state = 'succeeded'
            """,
            (
                durable_job_id,
                receipt_id,
                artifact_set_id,
            ),
        )
        set_rows: list[tuple[object, ...]] = []
        while (row := cursor.fetchone()) is not None:
            set_rows.append(row)
        if len(set_rows) != 1:
            raise SemanticInputUnavailableError(
                "exact committed semantic Receipt/ArtifactSet is unavailable"
            )
        command_slot_id, command_name, set_hash, member_count, request_hash, execution_kind = set_rows[0]
        slot_id = UUID(str(command_slot_id))
        try:
            self._validate_sha256(request_hash, "committed slot request_hash")
            _required_text(command_name, "committed slot command_name")
            if type(execution_kind) is not str or execution_kind not in ("deterministic", "generation"):  # noqa: E721
                raise StoreValidationError("committed slot execution kind is unsupported")
        except StoreValidationError as error:
            raise SemanticInputIntegrityError("committed slot producer identity is invalid") from error
        cursor.execute(
            """
            SELECT member.ordinal, artifact.artifact_type, artifact.logical_id,
                   artifact.revision, artifact.namespace, artifact.scope_kind,
                   artifact.scope_key, artifact.content_hash,
                   artifact.payload_json::text
              FROM runtime.artifact_set_members AS member
              JOIN runtime.artifacts AS artifact
                ON artifact.artifact_id = member.artifact_id
               AND artifact.artifact_set_id = member.artifact_set_id
               AND artifact.job_id = %s
             WHERE member.artifact_set_id = %s
             ORDER BY member.ordinal
            """,
            (durable_job_id, artifact_set_id),
        )
        members: list[tuple[int, ArtifactMember]] = []
        while (row := cursor.fetchone()) is not None:
            (
                ordinal,
                artifact_type,
                logical_id,
                revision,
                namespace,
                scope_kind,
                scope_key,
                content_hash,
                payload_json,
            ) = row
            serialized = _text(payload_json)
            try:
                if canonical_payload_hash(serialized) != _text(content_hash):
                    raise StoreValidationError(
                        "committed semantic member payload hash is invalid"
                    )
                members.append(
                    (
                        int(_text(ordinal)),
                        ArtifactMember(
                            _text(artifact_type),
                            _text(logical_id),
                            int(_text(revision)),
                            ArtifactScope(
                                _text(namespace),
                                _text(scope_kind),
                                _text(scope_key),
                            ),
                            _text(content_hash),
                            serialized,
                        ),
                    )
                )
            except (StoreValidationError, TypeError, ValueError) as error:
                raise SemanticInputIntegrityError(
                    "committed semantic member failed immutable verification"
                ) from error
        member_tuple = tuple(members)
        if (
            len(member_tuple) != int(_text(member_count))
            or tuple(item[0] for item in member_tuple)
            != tuple(range(len(member_tuple)))
        ):
            raise SemanticInputIntegrityError(
                "committed semantic ArtifactSet membership is incomplete or duplicated"
            )
        try:
            CommandSuccess(
                slot_id,
                _text(set_hash),
                tuple(item[1] for item in member_tuple),
            )
        except StoreValidationError as error:
            raise SemanticInputIntegrityError(
                "committed semantic ArtifactSet hash is invalid"
            ) from error
        return _CommittedSet(
            durable_job_id,
            slot_id,
            _text(command_name),
            _text(set_hash),
            member_tuple,
            _text(request_hash),
            execution_kind,
        )

    @staticmethod
    def _validate_uuid(value: object, field_name: str) -> None:
        if not isinstance(value, UUID):
            raise StoreValidationError(f"{field_name} must be a UUID")

    @staticmethod
    def _validate_sha256(value: object, field_name: str) -> None:
        if (
            type(value) is not str  # noqa: E721
            or len(value) != 71
            or not value.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in value[7:])
        ):
            raise StoreValidationError(f"{field_name} must be a lowercase sha256 digest")

    def _write_success(
        self, cursor: DbCursor, success: CommandSuccess, job_id: UUID
    ) -> CommandOutcome:
        """Shared internal ArtifactSet/Receipt writer; caller holds Job then slot locks."""

        artifact_set_id = uuid4()
        cursor.execute(
            """
            INSERT INTO runtime.artifact_sets
                (artifact_set_id, command_slot_id, job_id, set_hash, member_count)
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
                """
                INSERT INTO runtime.artifact_set_members (artifact_set_id, ordinal, artifact_id)
                VALUES (%s, %s, %s)
                """,
                (artifact_set_id, ordinal, artifact_id),
            )
            inserted.append((artifact_id, artifact))
        for artifact_id, artifact in inserted:
            cursor.execute(
                """
                INSERT INTO runtime.logical_heads
                    (job_id, namespace, scope_kind, scope_key, artifact_type, logical_id,
                     artifact_id, revision)
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
        self._complete_slot(cursor, success.command_slot_id, "succeeded")
        return CommandOutcome(
            command_slot_id=success.command_slot_id,
            state="succeeded",
            receipt_id=receipt_id,
            artifact_set_id=artifact_set_id,
            job_id=job_id,
        )

    def _write_rejection(
        self, cursor: DbCursor, rejection: CommandRejection, job_id: UUID
    ) -> CommandOutcome:
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
        self._complete_slot(cursor, rejection.command_slot_id, rejection.outcome)
        return CommandOutcome(
            command_slot_id=rejection.command_slot_id,
            state=rejection.outcome,
            receipt_id=receipt_id,
            failure_code=rejection.failure_code,
            failure_detail_json=rejection.failure_detail_json,
            job_id=job_id,
        )

    @staticmethod
    def _assert_no_other_open_slots(cursor: DbCursor, job_id: UUID, finalizer_id: UUID) -> None:
        cursor.execute(
            """
            SELECT command_slot_id, state FROM runtime.command_slots
             WHERE job_id = %s AND command_slot_id <> %s
             ORDER BY command_slot_id FOR UPDATE
            """,
            (job_id, finalizer_id),
        )
        while (row := cursor.fetchone()) is not None:
            if _text(row[1]) in ("pending", "running"):
                raise CommandStateError(
                    "run finalizer is blocked while another command slot is pending or running"
                )

    def _record_generation_blob_transition(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        raw_response: BlobRef,
        provider_request_id: str | None,
        dispatch_lease_token: str,
        source_state: str,
        target_state: str,
    ) -> GenerationAttempt:
        if provider_request_id is not None and (
            type(provider_request_id) is not str or not provider_request_id.strip()  # noqa: E721
        ):
            raise StoreValidationError("provider_request_id must be non-empty when present")

        def operation(cursor: DbCursor) -> GenerationAttempt:
            attempt, job_id, _, _ = self._locked_attempt_aggregate(cursor, attempt_id)
            self._require_attempt_transition(
                attempt, expected_version, (source_state,), target_state
            )
            self._require_attempt_lease(
                attempt, dispatch_lease_token, target_state
            )
            verified_blob = self._claimed_blob_ref(
                cursor,
                job_id,
                raw_response,
                field_name="raw-response",
            )
            effective_request_id = self._exact_provider_request_id(
                attempt.provider_request_id, provider_request_id
            )
            cursor.execute(
                """
                UPDATE runtime.generation_attempts
                   SET state = %s, provider_request_id = %s, raw_response_object_id = %s,
                       dispatch_lease_token = NULL, dispatch_lease_expires_at = NULL,
                       version = version + 1, responded_at = transaction_timestamp()
                 WHERE attempt_id = %s AND version = %s
                   AND dispatch_lease_token = %s
                   AND dispatch_lease_expires_at > transaction_timestamp()
                """,
                (
                    target_state,
                    effective_request_id,
                    verified_blob.object_id,
                    attempt_id,
                    expected_version,
                    dispatch_lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise GenerationAttemptStateError("generation response lease was lost")
            return self._read_generation_attempt_by_id(cursor, attempt_id, for_update=False)

        return self._transaction(operation)

    @staticmethod
    def _require_attempt_transition(
        attempt: GenerationAttempt,
        expected_version: int,
        allowed_states: tuple[str, ...],
        operation_name: str,
    ) -> None:
        if type(expected_version) is not int or expected_version < 0:  # noqa: E721
            raise StoreValidationError("expected_version must be a non-negative integer")
        if attempt.version != expected_version:
            raise GenerationAttemptStateError(
                f"stale generation version for {operation_name}: expected {attempt.version}"
            )
        if attempt.state not in allowed_states:
            raise GenerationAttemptStateError(
                f"generation in state {attempt.state} cannot {operation_name}"
            )

    @staticmethod
    def _require_attempt_lease(
        attempt: GenerationAttempt,
        dispatch_lease_token: str | None,
        operation_name: str,
    ) -> None:
        if type(dispatch_lease_token) is not str or not dispatch_lease_token.strip():  # noqa: E721
            raise StoreValidationError(
                f"dispatch_lease_token is required to {operation_name}"
            )
        if attempt.dispatch_lease_token != dispatch_lease_token:
            raise GenerationAttemptStateError("generation dispatch lease is owned elsewhere")
        if not attempt.dispatch_lease_is_active():
            raise GenerationAttemptStateError("generation dispatch lease has expired")

    @staticmethod
    def _bind_generation_receipt_chain(
        cursor: DbCursor,
        command_slot_id: UUID,
        receipt_id: UUID | None,
    ) -> None:
        if receipt_id is None:
            raise RuntimeStoreError("terminal generation outcome lost its Receipt")
        cursor.execute(
            """
            INSERT INTO runtime.generation_receipt_attempts
                (receipt_id, attempt_id, attempt_ordinal)
            SELECT %s, attempt_id, attempt_ordinal
              FROM runtime.generation_attempts
             WHERE command_slot_id = %s
             ORDER BY attempt_ordinal
            """,
            (receipt_id, command_slot_id),
        )

    @staticmethod
    def _exact_provider_request_id(existing: str | None, supplied: str | None) -> str | None:
        if existing is not None and supplied is not None and existing != supplied:
            raise IdempotencyConflictError("provider_request_id cannot change for one attempt")
        return supplied or existing

    def _locked_attempt_aggregate(
        self, cursor: DbCursor, attempt_id: UUID
    ) -> tuple[GenerationAttempt, UUID, str, str]:
        self._validate_uuid(attempt_id, "attempt_id")
        cursor.execute(
            "SELECT job_id, command_slot_id FROM runtime.generation_attempts WHERE attempt_id = %s",
            (attempt_id,),
        )
        identity = cursor.fetchone()
        if identity is None:
            raise StoreValidationError("attempt_id is unknown")
        expected_job_id = UUID(str(identity[0]))
        slot_id = UUID(str(identity[1]))
        job_id, slot_state, command_name, _ = self._locked_job_then_slot(cursor, slot_id)
        self._require_slot_execution_kind(cursor, slot_id, "generation")
        if job_id != expected_job_id:
            raise RuntimeStoreError("generation attempt changed jobs while being locked")
        attempt = self._read_generation_attempt_by_id(cursor, attempt_id, for_update=True)
        if attempt.job_id != job_id or attempt.command_slot_id != slot_id:
            raise RuntimeStoreError("generation attempt identity changed while being locked")
        return attempt, job_id, slot_state, command_name

    @staticmethod
    def _require_slot_execution_kind(
        cursor: DbCursor,
        slot_id: UUID,
        expected_kind: str,
    ) -> None:
        cursor.execute(
            "SELECT execution_kind FROM runtime.command_slots WHERE command_slot_id = %s",
            (slot_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeStoreError("command slot vanished while validating execution kind")
        if _text(row[0]) != expected_kind:
            raise CommandStateError(
                f"command slot execution kind must be {expected_kind} for this operation"
            )

    def _read_generation_attempt_by_id(
        self, cursor: DbCursor, attempt_id: UUID, *, for_update: bool
    ) -> GenerationAttempt:
        suffix = " FOR UPDATE OF attempt" if for_update else ""
        cursor.execute(
            """
            SELECT attempt.job_id, attempt.command_slot_id, attempt.request_hash,
                   attempt.provider_id, attempt.provider_idempotency_key,
                   request_blob.object_id, request_blob.content_hash,
                   request_blob.byte_length, request_blob.media_type,
                   attempt.state, attempt.version, attempt.provider_request_id,
                   response_blob.object_id, response_blob.content_hash,
                   response_blob.byte_length, response_blob.media_type,
                   attempt.receipt_id, attempt.artifact_set_id,
                   attempt.failure_code, attempt.failure_detail::text,
                   attempt.attempt_ordinal, attempt.previous_attempt_id,
                   attempt.retry_policy_hash, attempt.max_attempts,
                   attempt.failure_disposition, attempt.dispatch_lease_token,
                   attempt.dispatch_lease_expires_at, attempt.not_before_at,
                   attempt.retry_backoff_seconds
              FROM runtime.generation_attempts AS attempt
              JOIN storage.blob_objects AS request_blob
                ON request_blob.object_id = attempt.request_payload_object_id
              LEFT JOIN storage.blob_objects AS response_blob
                ON response_blob.object_id = attempt.raw_response_object_id
             WHERE attempt.attempt_id = %s
            """
            + suffix,
            (attempt_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise StoreValidationError("attempt_id is unknown")
        (
            job_id,
            slot_id,
            request_hash,
            provider_id,
            provider_idempotency_key,
            request_object_id,
            request_blob_hash,
            request_byte_length,
            request_media_type,
            state,
            version,
            provider_request_id,
            object_id,
            blob_hash,
            byte_length,
            media_type,
            receipt_id,
            artifact_set_id,
            failure_code,
            failure_detail,
            attempt_ordinal,
            previous_attempt_id,
            retry_policy_hash,
            max_attempts,
            failure_disposition,
            dispatch_lease_token,
            dispatch_lease_expires_at,
            not_before_at,
            retry_backoff_seconds,
        ) = row
        request_payload = BlobRef(
            UUID(str(request_object_id)),
            _text(request_blob_hash),
            int(_text(request_byte_length)),
            _text(request_media_type),
        )
        raw_response = None
        if object_id is not None:
            raw_response = BlobRef(
                UUID(str(object_id)),
                _text(blob_hash),
                int(_text(byte_length)),
                _text(media_type),
            )
        return GenerationAttempt(
            UUID(str(attempt_id)),
            UUID(str(job_id)),
            UUID(str(slot_id)),
            _text(request_hash),
            _text(provider_id),
            _text(provider_idempotency_key),
            request_payload,
            _text(state),  # type: ignore[arg-type]
            int(_text(version)),
            None if provider_request_id is None else _text(provider_request_id),
            raw_response,
            None if receipt_id is None else UUID(str(receipt_id)),
            None if artifact_set_id is None else UUID(str(artifact_set_id)),
            None if failure_code is None else _text(failure_code),
            None if failure_detail is None else _text(failure_detail),
            attempt_ordinal=int(_text(attempt_ordinal)),
            previous_attempt_id=(
                None
                if previous_attempt_id is None
                else UUID(str(previous_attempt_id))
            ),
            retry_policy_hash=_text(retry_policy_hash),
            max_attempts=int(_text(max_attempts)),
            failure_disposition=(
                None
                if failure_disposition is None
                else _text(failure_disposition)  # type: ignore[arg-type]
            ),
            dispatch_lease_token=(
                None if dispatch_lease_token is None else _text(dispatch_lease_token)
            ),
            dispatch_lease_expires_at=cast(
                datetime | None, dispatch_lease_expires_at
            ),
            not_before_at=cast(datetime | None, not_before_at),
            retry_backoff_seconds=int(_text(retry_backoff_seconds)),
        )

    @staticmethod
    def _generation_retry_backoff_seconds(
        cursor: DbCursor,
        previous: GenerationAttempt,
    ) -> int:
        cursor.execute(
            "SELECT content_bytes FROM storage.blob_objects WHERE object_id = %s",
            (previous.request_payload.object_id,),
        )
        row = cursor.fetchone()
        if row is None or not isinstance(row[0], (bytes, bytearray, memoryview)):
            raise BlobIntegrityError("generation retry policy payload is unavailable")
        raw = row[0].tobytes() if isinstance(row[0], memoryview) else bytes(row[0])
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise BlobIntegrityError(
                "generation retry policy payload is not canonical JSON"
            ) from error
        if not isinstance(payload, dict):
            raise BlobIntegrityError("generation retry policy payload must be an object")
        payload_object = cast(dict[str, object], payload)
        policy = payload_object.get("retry_policy")
        declared_hash = payload_object.get("retry_policy_sha256")
        if not isinstance(policy, dict) or declared_hash != previous.retry_policy_hash:
            raise BlobIntegrityError("generation retry policy identity is unavailable")
        policy_object = cast(dict[str, object], policy)
        policy_json = json.dumps(
            policy_object,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if canonical_payload_hash(policy_json) != previous.retry_policy_hash:
            raise BlobIntegrityError("generation retry policy hash does not match its bytes")
        if set(policy_object) != {
            "backoff_seconds",
            "max_attempts",
            "strategy_version",
        }:
            raise BlobIntegrityError("generation retry policy shape is not closed")
        if policy_object.get("max_attempts") != previous.max_attempts:
            raise BlobIntegrityError("generation retry budget changed after reservation")
        backoffs = policy_object.get("backoff_seconds")
        if not isinstance(backoffs, list):
            raise BlobIntegrityError("generation retry backoff schedule is invalid")
        backoff_values = cast(list[object], backoffs)
        if len(backoff_values) != previous.max_attempts - 1:
            raise BlobIntegrityError("generation retry backoff schedule is invalid")
        value = backoff_values[previous.attempt_ordinal - 1]
        if type(value) is not int or value < 0:  # noqa: E721
            raise BlobIntegrityError("generation retry backoff value is invalid")
        return value

    @staticmethod
    def _claimed_blob_ref(
        cursor: DbCursor,
        job_id: UUID,
        reference: BlobRef,
        *,
        field_name: str,
    ) -> BlobRef:
        cursor.execute(
            """
            SELECT object.object_id, object.content_hash, object.byte_length, object.media_type
              FROM storage.blob_objects AS object
              JOIN storage.blob_claims AS claim ON claim.object_id = object.object_id
             WHERE claim.job_id = %s AND object.object_id = %s
            """,
            (job_id, reference.object_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise BlobIntegrityError(
                f"{field_name} BlobRef is not claimed by the attempt Job"
            )
        durable = BlobRef(
            UUID(str(row[0])), _text(row[1]), int(_text(row[2])), _text(row[3])
        )
        if durable != reference:
            raise BlobIntegrityError(
                f"{field_name} BlobRef does not match durable blob metadata"
            )
        return durable

    @staticmethod
    def _validate_nonnegative_version(value: int, field_name: str) -> None:
        if type(value) is not int or value < 0:  # noqa: E721
            raise StoreValidationError(f"{field_name} must be a non-negative integer")

    @staticmethod
    def _require_shadow_local_plan_claim(
        claim: CommandClaim, plan: ShadowLocalMeasurementPlan
    ) -> None:
        if type(claim) is not CommandClaim or type(plan) is not ShadowLocalMeasurementPlan:  # noqa: E721
            raise StoreValidationError("shadow-local owner requires exact claim and plan")
        if claim != plan.claim:
            raise StoreValidationError("shadow-local claim does not equal its immutable plan claim")
        if claim.command_name != SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME:
            raise StoreValidationError("shadow-local owner requires its reserved command")
        if claim.execution_kind != "deterministic":
            raise StoreValidationError("shadow-local measurement requires deterministic execution kind")

    def _read_shadow_local_attempt_by_slot(
        self, cursor: DbCursor, command_slot_id: UUID
    ) -> ShadowLocalMeasurementAttempt | None:
        cursor.execute(
            """
            SELECT attempt_id FROM runtime.shadow_local_calibration_measurement_attempts
             WHERE command_slot_id = %s
             ORDER BY attempt_ordinal DESC
             LIMIT 1
            """,
            (command_slot_id,),
        )
        row = cursor.fetchone()
        return None if row is None else self._read_shadow_local_attempt_by_id(cursor, UUID(str(row[0])))

    def _read_shadow_local_attempt_by_id(
        self, cursor: DbCursor, attempt_id: UUID
    ) -> ShadowLocalMeasurementAttempt | None:
        cursor.execute(
            """
            SELECT attempt.command_slot_id, job.job_key, job.profile, attempt.plan_hash,
                   attempt.plan_json::text, attempt.attempt_ordinal, attempt.previous_attempt_id,
                   attempt.state, attempt.version, attempt.recovery_lease_expires_at
              FROM runtime.shadow_local_calibration_measurement_attempts AS attempt
              JOIN runtime.jobs AS job ON job.job_id = attempt.job_id
             WHERE attempt.attempt_id = %s
            """,
            (attempt_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        (
            slot_id, job_key, profile, plan_hash, plan_json, ordinal, previous_id,
            state, version, recovery_expires,
        ) = row
        command_slot_id = UUID(str(slot_id))
        job_id = self._slot_job_id(cursor, command_slot_id)
        outcome = self._read_outcome_by_slot(cursor, command_slot_id, job_id)
        cursor.execute(
            """
            SELECT case_sha256, request_sha256, member_ordinal, case_json::text, request_json::text,
                   source_job_id, source_blob_object_id, source_blob_content_hash,
                   source_blob_byte_length, source_blob_media_type, source_blob_reference_sha256,
                   binding_sha256, service_profile_sha256, max_response_bytes, state, version,
                   raw_blob_object_id, raw_content_hash, raw_byte_length, raw_media_type,
                   evidence_json::text, busy_proof_blob_object_id, busy_proof_content_hash,
                   busy_proof_byte_length, busy_proof_media_type, busy_proof_json::text,
                   lease_expires_at
              FROM runtime.shadow_local_calibration_measurement_members
             WHERE attempt_id = %s ORDER BY member_ordinal
            """,
            (attempt_id,),
        )
        members: list[ShadowLocalMeasurementMember] = []
        while (member_row := cursor.fetchone()) is not None:
            (
                case_hash, request_hash, member_ordinal, case_json, request_json, source_job_id,
                source_blob_id, source_hash, source_length, source_media_type, source_reference_hash,
                binding_hash, service_profile_hash, max_response_bytes, member_state, member_version,
                raw_id, raw_hash, raw_length, raw_type, evidence_json, busy_id, busy_hash,
                busy_length, busy_type, busy_json, lease_expires,
            ) = member_row
            source_blob = BlobRef(
                UUID(str(source_blob_id)), _text(source_hash), int(_text(source_length)), _text(source_media_type)
            )
            raw_blob = (
                None if raw_id is None else BlobRef(
                    UUID(str(raw_id)), _text(raw_hash), int(_text(raw_length)), _text(raw_type)
                )
            )
            busy_blob = (
                None if busy_id is None else BlobRef(
                    UUID(str(busy_id)), _text(busy_hash), int(_text(busy_length)), _text(busy_type)
                )
            )
            members.append(
                ShadowLocalMeasurementMember(
                    attempt_id, _text(case_hash), _text(request_hash), int(_text(member_ordinal)),
                    _canonical_media_db_json(_text(case_json)), _canonical_media_db_json(_text(request_json)),
                    UUID(str(source_job_id)), source_blob, _text(source_reference_hash), _text(binding_hash),
                    _text(service_profile_hash), int(_text(max_response_bytes)),
                    cast(ShadowLocalMeasurementMemberState, _text(member_state)), int(_text(member_version)),
                    raw_blob, None if evidence_json is None else _canonical_media_db_json(_text(evidence_json)),
                    busy_blob, None if busy_json is None else _canonical_media_db_json(_text(busy_json)),
                    cast(datetime | None, lease_expires),
                )
            )
        return ShadowLocalMeasurementAttempt(
            attempt_id, command_slot_id, Job(_text(job_key), cast(JobProfile, _text(profile))),
            _text(plan_hash), _canonical_db_json(_text(plan_json)), int(_text(ordinal)),
            None if previous_id is None else UUID(str(previous_id)),
            cast(ShadowLocalMeasurementAttemptState, _text(state)), int(_text(version)), tuple(members),
            outcome, cast(datetime | None, recovery_expires),
        )

    def _locked_shadow_local_attempt(
        self, cursor: DbCursor, attempt_id: UUID
    ) -> ShadowLocalMeasurementAttempt:
        cursor.execute(
            "SELECT command_slot_id FROM runtime.shadow_local_calibration_measurement_attempts WHERE attempt_id = %s",
            (attempt_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise StoreValidationError("shadow-local attempt_id is unknown")
        slot_id = UUID(str(row[0]))
        self._locked_job_then_slot(cursor, slot_id)
        self._require_slot_execution_kind(cursor, slot_id, "deterministic")
        cursor.execute(
            "SELECT attempt_id FROM runtime.shadow_local_calibration_measurement_attempts "
            "WHERE attempt_id = %s FOR UPDATE",
            (attempt_id,),
        )
        if cursor.fetchone() is None:
            raise RuntimeStoreError("shadow-local attempt vanished while locking")
        attempt = self._read_shadow_local_attempt_by_id(cursor, attempt_id)
        if attempt is None:
            raise RuntimeStoreError("shadow-local attempt vanished while reading")
        return attempt

    def _validate_shadow_local_attempt_chain(
        self, cursor: DbCursor, attempt: ShadowLocalMeasurementAttempt
    ) -> None:
        """Lock and close the full same-slot predecessor chain before publication/read."""

        cursor.execute(
            """
            SELECT attempt_id FROM runtime.shadow_local_calibration_measurement_attempts
             WHERE command_slot_id = %s ORDER BY attempt_ordinal FOR UPDATE
            """,
            (attempt.command_slot_id,),
        )
        attempt_ids: list[UUID] = []
        while (row := cursor.fetchone()) is not None:
            attempt_ids.append(UUID(str(row[0])))
        if not attempt_ids or attempt_ids[-1] != attempt.attempt_id:
            raise StoreValidationError("shadow-local final attempt is not the exact current same-slot attempt")
        attempts: list[ShadowLocalMeasurementAttempt] = []
        for item_id in attempt_ids:
            loaded = self._read_shadow_local_attempt_by_id(cursor, item_id)
            if loaded is None:
                raise RuntimeStoreError("shadow-local predecessor vanished while locked")
            attempts.append(loaded)
        first = attempts[0]
        immutable_members = tuple(
            (
                member.member_ordinal, member.case_sha256, member.request_sha256,
                member.canonical_case_json, member.canonical_request_json, member.source_job_id,
                member.source_blob, member.source_blob_reference_sha256, member.binding_sha256,
                member.service_profile_sha256, member.max_response_bytes,
            )
            for member in first.members
        )
        for ordinal, item in enumerate(attempts, start=1):
            members = tuple(
                (
                    member.member_ordinal, member.case_sha256, member.request_sha256,
                    member.canonical_case_json, member.canonical_request_json, member.source_job_id,
                    member.source_blob, member.source_blob_reference_sha256, member.binding_sha256,
                    member.service_profile_sha256, member.max_response_bytes,
                )
                for member in item.members
            )
            if (
                item.attempt_ordinal != ordinal
                or item.command_slot_id != attempt.command_slot_id
                or item.job != attempt.job
                or item.plan_hash != attempt.plan_hash
                or item.canonical_plan_json != attempt.canonical_plan_json
                or members != immutable_members
                or (item.previous_attempt_id if ordinal > 1 else None)
                != (attempts[ordinal - 2].attempt_id if ordinal > 1 else None)
            ):
                raise StoreValidationError("shadow-local predecessor chain is not contiguous and immutable")

    @staticmethod
    def _shadow_local_total_response_limit(attempt: ShadowLocalMeasurementAttempt) -> int:
        plan = _strict_json_object(attempt.canonical_plan_json, "shadow-local attempt plan")
        inputs = plan.get("shadow_local_inputs")
        if type(inputs) is not dict:  # noqa: E721
            raise StoreValidationError("shadow-local attempt lacks frozen response limits")
        limits = cast(dict[str, object], inputs).get("limits")
        if type(limits) is not dict:  # noqa: E721
            raise StoreValidationError("shadow-local attempt lacks frozen response limits")
        value = cast(dict[str, object], limits).get("max_total_response_bytes")
        if type(value) is not int or value <= 0:  # noqa: E721
            raise StoreValidationError("shadow-local attempt total response limit is invalid")
        return value

    def _read_shadow_local_staged_responses(
        self,
        cursor: DbCursor,
        attempt: ShadowLocalMeasurementAttempt,
        job_id: UUID | None,
    ) -> dict[tuple[int, str], bytes]:
        """Validate all claims/budgets before reading any raw local response byte."""

        if type(job_id) is not UUID:  # noqa: E721
            raise RuntimeStoreError("shadow-local attempt lost its durable Job UUID")
        if any(member.state != "staged" or member.raw_blob is None for member in attempt.members):
            raise StoreValidationError("shadow-local committed attempt does not have complete staged responses")
        limit = self._shadow_local_total_response_limit(attempt)
        durable: list[tuple[ShadowLocalMeasurementMember, BlobRef]] = []
        total = 0
        for member in attempt.members:
            raw_blob = cast(BlobRef, member.raw_blob)
            if not 0 < raw_blob.byte_length <= member.max_response_bytes:
                raise StoreValidationError("shadow-local raw response violates its per-member byte budget")
            total += raw_blob.byte_length
            durable.append(
                (
                    member,
                    self._claimed_blob_ref(
                        cursor, job_id, raw_blob, field_name="shadow-local raw response"
                    ),
                )
            )
        if total > limit:
            raise StoreValidationError("shadow-local raw response metadata exceeds its total byte budget")
        responses: dict[tuple[int, str], bytes] = {}
        for member, raw_blob in durable:
            cursor.execute(
                "SELECT content_bytes FROM storage.blob_objects WHERE object_id = %s",
                (raw_blob.object_id,),
            )
            row = cursor.fetchone()
            if row is None or not isinstance(row[0], (bytes, bytearray, memoryview)):
                raise BlobUnavailableError("shadow-local staged raw response bytes are unavailable")
            raw = row[0].tobytes() if isinstance(row[0], memoryview) else bytes(row[0])
            if (
                len(raw) != raw_blob.byte_length
                or "sha256:" + hashlib.sha256(raw).hexdigest() != raw_blob.content_hash
            ):
                raise BlobIntegrityError("shadow-local staged raw response fails its exact BlobRef")
            responses[(member.member_ordinal, member.case_sha256)] = raw
        return responses

    @staticmethod
    def _locked_shadow_local_member(
        cursor: DbCursor, attempt_id: UUID, case_sha256: str
    ) -> ShadowLocalMeasurementMember | None:
        cursor.execute(
            """
            SELECT case_sha256, request_sha256, member_ordinal, case_json::text, request_json::text,
                   source_job_id, source_blob_object_id, source_blob_content_hash,
                   source_blob_byte_length, source_blob_media_type, source_blob_reference_sha256,
                   binding_sha256, service_profile_sha256, max_response_bytes, state, version,
                   raw_blob_object_id, raw_content_hash, raw_byte_length, raw_media_type,
                   evidence_json::text, busy_proof_blob_object_id, busy_proof_content_hash,
                   busy_proof_byte_length, busy_proof_media_type, busy_proof_json::text,
                   lease_expires_at
              FROM runtime.shadow_local_calibration_measurement_members
             WHERE attempt_id = %s AND case_sha256 = %s FOR UPDATE
            """,
            (attempt_id, case_sha256),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        (
            case_hash, request_hash, member_ordinal, case_json, request_json, source_job_id,
            source_blob_id, source_hash, source_length, source_media_type, source_reference_hash,
            binding_hash, service_profile_hash, max_response_bytes, state, version,
            raw_id, raw_hash, raw_length, raw_type, evidence_json, busy_id, busy_hash,
            busy_length, busy_type, busy_json, lease_expires,
        ) = row
        source_blob = BlobRef(
            UUID(str(source_blob_id)), _text(source_hash), int(_text(source_length)), _text(source_media_type)
        )
        raw_blob = None if raw_id is None else BlobRef(
            UUID(str(raw_id)), _text(raw_hash), int(_text(raw_length)), _text(raw_type)
        )
        busy_blob = None if busy_id is None else BlobRef(
            UUID(str(busy_id)), _text(busy_hash), int(_text(busy_length)), _text(busy_type)
        )
        return ShadowLocalMeasurementMember(
            attempt_id, _text(case_hash), _text(request_hash), int(_text(member_ordinal)),
            _canonical_media_db_json(_text(case_json)), _canonical_media_db_json(_text(request_json)), UUID(str(source_job_id)),
            source_blob, _text(source_reference_hash), _text(binding_hash), _text(service_profile_hash),
            int(_text(max_response_bytes)), cast(ShadowLocalMeasurementMemberState, _text(state)),
            int(_text(version)), raw_blob,
            None if evidence_json is None else _canonical_media_db_json(_text(evidence_json)), busy_blob,
            None if busy_json is None else _canonical_media_db_json(_text(busy_json)), cast(datetime | None, lease_expires),
        )

    def _verify_shadow_local_source_owner(
        self, cursor: DbCursor, member: ShadowLocalMeasurementMemberPlan
    ) -> Job:
        if type(member) is not ShadowLocalMeasurementMemberPlan:  # noqa: E721
            raise StoreValidationError("shadow-local source verification requires exact member plan")
        return self._verify_shadow_local_source_owner_values(
            cursor, member.source_job_id, member.source_blob
        )

    def _verify_shadow_local_source_owner_from_member(
        self, cursor: DbCursor, member: ShadowLocalMeasurementMember
    ) -> tuple[Job, BlobRef]:
        if type(member) is not ShadowLocalMeasurementMember:  # noqa: E721
            raise StoreValidationError("shadow-local source verification requires exact stored member")
        job = self._verify_shadow_local_source_owner_values(cursor, member.source_job_id, member.source_blob)
        return job, member.source_blob

    @staticmethod
    def _shadow_local_max_attempt_count(canonical_plan_json: str) -> int:
        """Read the bounded retry budget from the exact Store-canonical plan."""

        plan = _strict_json_object(canonical_plan_json, "shadow-local attempt plan")
        inputs = plan.get("shadow_local_inputs")
        if type(inputs) is not dict:  # noqa: E721
            raise StoreValidationError("shadow-local attempt plan inputs are invalid")
        max_attempt_count = cast(dict[str, object], inputs).get("max_attempt_count")
        if type(max_attempt_count) is not int or max_attempt_count <= 0:  # noqa: E721
            raise StoreValidationError("shadow-local attempt plan max_attempt_count is invalid")
        return max_attempt_count

    @staticmethod
    def _verify_shadow_local_source_owner_values(
        cursor: DbCursor, source_job_id: UUID, source_blob: BlobRef
    ) -> Job:
        cursor.execute(
            """
            SELECT source.job_key, source.profile, source.state,
                   object.object_id, object.content_hash, object.byte_length, object.media_type
              FROM runtime.jobs AS source
              JOIN storage.blob_claims AS claim ON claim.job_id = source.job_id
              JOIN storage.blob_objects AS object ON object.object_id = claim.object_id
             WHERE source.job_id = %s AND object.object_id = %s
             FOR KEY SHARE
            """,
            (source_job_id, source_blob.object_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise StoreValidationError("shadow-local source BlobRef is not owned by its source Job")
        job_key, profile, state, object_id, content_hash, byte_length, media_type = row
        if _text(state) != "succeeded":
            raise CommandStateError("shadow-local source Job must already be succeeded")
        durable = BlobRef(
            UUID(str(object_id)), _text(content_hash), int(_text(byte_length)), _text(media_type)
        )
        if durable != source_blob:
            raise StoreValidationError("shadow-local source BlobRef metadata drifted from its source owner")
        return Job(_text(job_key), cast(JobProfile, _text(profile)))

    @staticmethod
    def _insert_shadow_local_member(
        cursor: DbCursor,
        attempt_id: UUID,
        member: ShadowLocalMeasurementMemberPlan,
        *,
        state: Literal["pending", "staged"],
        staged: ShadowLocalMeasurementMember | None = None,
    ) -> None:
        if state == "staged":
            if staged is None or staged.raw_blob is None or staged.evidence_json is None:
                raise RuntimeStoreError("shadow-local staged successor lost durable evidence")
            cursor.execute(
                """
                INSERT INTO runtime.shadow_local_calibration_measurement_members
                    (attempt_id, case_sha256, request_sha256, member_ordinal, case_json, request_json,
                     source_job_id, source_blob_object_id, source_blob_content_hash,
                     source_blob_byte_length, source_blob_media_type, source_blob_reference_sha256,
                     binding_sha256, service_profile_sha256, max_response_bytes, state, version,
                     raw_blob_object_id, raw_content_hash, raw_byte_length, raw_media_type, evidence_json)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, 'staged', 0, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    attempt_id, member.case_sha256, member.request_sha256, member.member_ordinal,
                    member.canonical_case_json, member.canonical_request_json, member.source_job_id,
                    member.source_blob.object_id, member.source_blob.content_hash,
                    member.source_blob.byte_length, member.source_blob.media_type,
                    member.source_blob_reference_sha256, member.binding_sha256,
                    member.service_profile_sha256, member.max_response_bytes, staged.raw_blob.object_id,
                    staged.raw_blob.content_hash, staged.raw_blob.byte_length, staged.raw_blob.media_type,
                    staged.evidence_json,
                ),
            )
            return
        cursor.execute(
            """
            INSERT INTO runtime.shadow_local_calibration_measurement_members
                (attempt_id, case_sha256, request_sha256, member_ordinal, case_json, request_json,
                 source_job_id, source_blob_object_id, source_blob_content_hash,
                 source_blob_byte_length, source_blob_media_type, source_blob_reference_sha256,
                 binding_sha256, service_profile_sha256, max_response_bytes, state, version)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, 'pending', 0)
            """,
            (
                attempt_id, member.case_sha256, member.request_sha256, member.member_ordinal,
                member.canonical_case_json, member.canonical_request_json, member.source_job_id,
                member.source_blob.object_id, member.source_blob.content_hash,
                member.source_blob.byte_length, member.source_blob.media_type,
                member.source_blob_reference_sha256, member.binding_sha256,
                member.service_profile_sha256, member.max_response_bytes,
            ),
        )

    @staticmethod
    def _require_shadow_local_member_lease(
        cursor: DbCursor,
        member: ShadowLocalMeasurementMember,
        lease_token: str,
        operation: str,
    ) -> None:
        if member.lease_expires_at is None or member.lease_expires_at <= datetime.now(timezone.utc):
            raise CommandStateError(f"shadow-local member lease expired before {operation}")
        if type(lease_token) is not str or not lease_token.strip():  # noqa: E721
            raise StoreValidationError(f"shadow-local member lease_token is required to {operation}")
        cursor.execute(
            """
            SELECT lease_token FROM runtime.shadow_local_calibration_measurement_members
             WHERE attempt_id = %s AND case_sha256 = %s FOR KEY SHARE
            """,
            (member.attempt_id, member.case_sha256),
        )
        row = cursor.fetchone()
        if row is None or row[0] is None or _text(row[0]) != lease_token:
            raise CommandStateError(f"shadow-local member lease was lost before {operation}")

    @staticmethod
    def _require_shadow_local_recovery_lease(
        cursor: DbCursor,
        attempt: ShadowLocalMeasurementAttempt,
        lease_token: str,
        operation: str,
    ) -> None:
        if (
            attempt.recovery_lease_expires_at is None
            or attempt.recovery_lease_expires_at <= datetime.now(timezone.utc)
        ):
            raise CommandStateError(f"shadow-local recovery lease expired before {operation}")
        if type(lease_token) is not str or not lease_token.strip():  # noqa: E721
            raise StoreValidationError(f"shadow-local recovery lease_token is required to {operation}")
        cursor.execute(
            "SELECT recovery_lease_token FROM runtime.shadow_local_calibration_measurement_attempts "
            "WHERE attempt_id = %s FOR KEY SHARE",
            (attempt.attempt_id,),
        )
        row = cursor.fetchone()
        if row is None or row[0] is None or _text(row[0]) != lease_token:
            raise CommandStateError(f"shadow-local recovery lease was lost before {operation}")

    @staticmethod
    def _transition_shadow_local_attempt(
        cursor: DbCursor, attempt: ShadowLocalMeasurementAttempt, state: str
    ) -> None:
        cursor.execute(
            """
            UPDATE runtime.shadow_local_calibration_measurement_attempts
               SET state = %s, version = version + 1
             WHERE attempt_id = %s AND version = %s
            """,
            (state, attempt.attempt_id, attempt.version),
        )
        if cursor.rowcount != 1:
            raise CommandStateError("shadow-local attempt CAS was lost")

    def _put_shadow_local_blob(
        self,
        cursor: DbCursor,
        job: Job,
        raw_bytes: bytes,
        content_hash: str,
        media_type: str,
    ) -> BlobRef:
        job_id = self._ensure_job(cursor, job)
        object_id = uuid4()
        cursor.execute(
            """
            INSERT INTO storage.blob_objects
                (object_id, content_hash, byte_length, media_type, content_bytes)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (content_hash) DO NOTHING
            RETURNING object_id, content_hash, byte_length, media_type
            """,
            (object_id, content_hash, len(raw_bytes), media_type, raw_bytes),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                """
                SELECT object_id, content_hash, byte_length, media_type, content_bytes
                  FROM storage.blob_objects WHERE content_hash = %s FOR UPDATE
                """,
                (content_hash,),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeStoreError("shadow-local blob vanished after deduplication conflict")
            value = row[4]
            if not isinstance(value, (bytes, bytearray, memoryview)):
                raise BlobIntegrityError("shadow-local durable blob returned invalid bytes")
            durable_bytes = value.tobytes() if isinstance(value, memoryview) else bytes(value)
            if (
                _text(row[1]) != content_hash
                or int(_text(row[2])) != len(raw_bytes)
                or _text(row[3]) != media_type
                or durable_bytes != raw_bytes
            ):
                raise BlobIntegrityError("shadow-local staged bytes do not exactly match durable BlobRef")
        reference = BlobRef(UUID(str(row[0])), _text(row[1]), int(_text(row[2])), _text(row[3]))
        cursor.execute(
            """
            INSERT INTO storage.blob_claims (blob_claim_id, object_id, job_id)
            VALUES (%s, %s, %s) ON CONFLICT (job_id, object_id) DO NOTHING
            """,
            (uuid4(), reference.object_id, job_id),
        )
        return reference

    @staticmethod
    def _require_shadow_plan_claim(claim: CommandClaim, plan: ShadowMeasurementPlan) -> None:
        if type(claim) is not CommandClaim or type(plan) is not ShadowMeasurementPlan:  # noqa: E721
            raise StoreValidationError("shadow owner requires exact claim and plan")
        if claim != plan.claim:
            raise StoreValidationError("shadow owner claim does not equal its immutable plan claim")
        if claim.execution_kind != "deterministic":
            raise StoreValidationError("shadow measurement requires deterministic execution kind")

    def _read_shadow_attempt_by_slot(
        self, cursor: DbCursor, command_slot_id: UUID
    ) -> ShadowMeasurementAttempt | None:
        cursor.execute(
            "SELECT attempt_id FROM runtime.shadow_calibration_measurement_attempts WHERE command_slot_id = %s",
            (command_slot_id,),
        )
        row = cursor.fetchone()
        return None if row is None else self._read_shadow_attempt_by_id(cursor, UUID(str(row[0])))

    def _read_shadow_attempt_by_id(
        self, cursor: DbCursor, attempt_id: UUID
    ) -> ShadowMeasurementAttempt | None:
        cursor.execute(
            """
            SELECT attempt.command_slot_id, job.job_key, job.profile, attempt.plan_hash,
                   attempt.plan_json::text, attempt.attempt_ordinal, attempt.previous_attempt_id,
                   attempt.state, attempt.version, attempt.recovery_lease_expires_at
              FROM runtime.shadow_calibration_measurement_attempts AS attempt
              JOIN runtime.jobs AS job ON job.job_id = attempt.job_id
             WHERE attempt.attempt_id = %s
            """,
            (attempt_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        (
            slot_id, job_key, profile, plan_hash, plan_json, ordinal, previous_id,
            state, version, recovery_expires,
        ) = row
        job_id = self._slot_job_id(cursor, UUID(str(slot_id)))
        outcome = self._read_outcome_by_slot(cursor, UUID(str(slot_id)), job_id)
        cursor.execute(
            """
            SELECT corpus_member_reference_sha256, member_ordinal, invocation_json::text,
                   context_json::text, expected_anchor_reference_sha256, state, version,
                   raw_blob_object_id, raw_content_hash, raw_byte_length, raw_media_type,
                   projection_json::text, lease_expires_at
              FROM runtime.shadow_calibration_measurement_members
             WHERE attempt_id = %s ORDER BY member_ordinal
            """,
            (attempt_id,),
        )
        members: list[ShadowMeasurementMember] = []
        while (member_row := cursor.fetchone()) is not None:
            (
                reference, member_ordinal, invocation_json, context_json, anchor_reference,
                member_state, member_version, blob_id, blob_hash, blob_length, blob_type,
                projection_json, lease_expires,
            ) = member_row
            blob = (
                None
                if blob_id is None
                else BlobRef(UUID(str(blob_id)), _text(blob_hash), int(_text(blob_length)), _text(blob_type))
            )
            members.append(
                ShadowMeasurementMember(
                    attempt_id, _text(reference), int(_text(member_ordinal)),
                    _canonical_db_json(_text(invocation_json)), _canonical_db_json(_text(context_json)),
                    _text(anchor_reference), cast(ShadowMeasurementMemberState, _text(member_state)), int(_text(member_version)),
                    blob, None if projection_json is None else _canonical_db_json(_text(projection_json)),
                    cast(datetime | None, lease_expires),
                )
            )
        return ShadowMeasurementAttempt(
            attempt_id, UUID(str(slot_id)), Job(_text(job_key), cast(JobProfile, _text(profile))),
            _text(plan_hash), _canonical_db_json(_text(plan_json)), int(_text(ordinal)),
            None if previous_id is None else UUID(str(previous_id)), cast(ShadowMeasurementAttemptState, _text(state)),
            int(_text(version)), tuple(members), outcome, cast(datetime | None, recovery_expires),
        )

    def _locked_shadow_attempt(self, cursor: DbCursor, attempt_id: UUID) -> ShadowMeasurementAttempt:
        cursor.execute(
            "SELECT command_slot_id FROM runtime.shadow_calibration_measurement_attempts WHERE attempt_id = %s",
            (attempt_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise StoreValidationError("shadow attempt_id is unknown")
        slot_id = UUID(str(row[0]))
        self._locked_job_then_slot(cursor, slot_id)
        self._require_slot_execution_kind(cursor, slot_id, "deterministic")
        cursor.execute(
            "SELECT attempt_id FROM runtime.shadow_calibration_measurement_attempts WHERE attempt_id = %s FOR UPDATE",
            (attempt_id,),
        )
        if cursor.fetchone() is None:
            raise RuntimeStoreError("shadow attempt vanished while locking")
        attempt = self._read_shadow_attempt_by_id(cursor, attempt_id)
        if attempt is None:
            raise RuntimeStoreError("shadow attempt vanished while reading")
        return attempt

    @staticmethod
    def _locked_shadow_member(
        cursor: DbCursor, attempt_id: UUID, member_reference_sha256: str
    ) -> ShadowMeasurementMember | None:
        cursor.execute(
            """
            SELECT corpus_member_reference_sha256, member_ordinal, invocation_json::text,
                   context_json::text, expected_anchor_reference_sha256, state, version,
                   raw_blob_object_id, raw_content_hash, raw_byte_length, raw_media_type,
                   projection_json::text, lease_expires_at
              FROM runtime.shadow_calibration_measurement_members
             WHERE attempt_id = %s AND corpus_member_reference_sha256 = %s FOR UPDATE
            """,
            (attempt_id, member_reference_sha256),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        (
            reference, ordinal, invocation_json, context_json, anchor_reference, state, version,
            blob_id, blob_hash, blob_length, blob_type, projection_json, lease_expires,
        ) = row
        blob = (
            None if blob_id is None
            else BlobRef(UUID(str(blob_id)), _text(blob_hash), int(_text(blob_length)), _text(blob_type))
        )
        return ShadowMeasurementMember(
            attempt_id, _text(reference), int(_text(ordinal)), _canonical_db_json(_text(invocation_json)),
            _canonical_db_json(_text(context_json)), _text(anchor_reference), cast(ShadowMeasurementMemberState, _text(state)),
            int(_text(version)), blob,
            None if projection_json is None else _canonical_db_json(_text(projection_json)),
            cast(datetime | None, lease_expires),
        )

    def _transition_shadow_attempt(
        self, cursor: DbCursor, attempt: ShadowMeasurementAttempt, state: str
    ) -> None:
        cursor.execute(
            """
            UPDATE runtime.shadow_calibration_measurement_attempts
               SET state = %s, version = version + 1
             WHERE attempt_id = %s AND version = %s
            """,
            (state, attempt.attempt_id, attempt.version),
        )
        if cursor.rowcount != 1:
            raise CommandStateError("shadow attempt CAS was lost")

    @staticmethod
    def _require_shadow_member_lease(
        member: ShadowMeasurementMember, lease_token: str, operation: str
    ) -> None:
        if member.lease_expires_at is None or member.lease_expires_at <= datetime.now(timezone.utc):
            raise CommandStateError(f"shadow member lease expired before {operation}")
        # The token is checked by the update predicate; it intentionally is not
        # included in public snapshots after the operation completes.
        if type(lease_token) is not str or not lease_token.strip():  # noqa: E721
            raise StoreValidationError(f"shadow member lease_token is required to {operation}")

    @staticmethod
    def _require_shadow_recovery_lease(
        attempt: ShadowMeasurementAttempt, lease_token: str, operation: str
    ) -> None:
        if attempt.recovery_lease_expires_at is None or attempt.recovery_lease_expires_at <= datetime.now(timezone.utc):
            raise CommandStateError(f"shadow recovery lease expired before {operation}")
        if type(lease_token) is not str or not lease_token.strip():  # noqa: E721
            raise StoreValidationError(f"shadow recovery lease_token is required to {operation}")

    def _put_shadow_blob(
        self, cursor: DbCursor, job: Job, staged: ShadowMeasurementStagedResponse
    ) -> BlobRef:
        job_id = self._ensure_job(cursor, job)
        object_id = uuid4()
        cursor.execute(
            """
            INSERT INTO storage.blob_objects
                (object_id, content_hash, byte_length, media_type, content_bytes)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (content_hash) DO NOTHING
            RETURNING object_id, content_hash, byte_length, media_type
            """,
            (object_id, staged.content_hash, len(staged.raw_bytes), staged.media_type, staged.raw_bytes),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                """
                SELECT object_id, content_hash, byte_length, media_type, content_bytes
                  FROM storage.blob_objects WHERE content_hash = %s FOR UPDATE
                """,
                (staged.content_hash,),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeStoreError("shadow blob vanished after deduplication conflict")
            bytes_value = row[4]
            if not isinstance(bytes_value, (bytes, bytearray, memoryview)):
                raise BlobIntegrityError("shadow durable blob returned invalid bytes")
            durable_bytes = bytes_value.tobytes() if isinstance(bytes_value, memoryview) else bytes(bytes_value)
            if (
                _text(row[1]) != staged.content_hash or int(_text(row[2])) != len(staged.raw_bytes)
                or _text(row[3]) != staged.media_type or durable_bytes != staged.raw_bytes
            ):
                raise BlobIntegrityError("shadow staged bytes do not exactly match durable BlobRef")
        reference = BlobRef(UUID(str(row[0])), _text(row[1]), int(_text(row[2])), _text(row[3]))
        cursor.execute(
            """
            INSERT INTO storage.blob_claims (blob_claim_id, object_id, job_id)
            VALUES (%s, %s, %s) ON CONFLICT (job_id, object_id) DO NOTHING
            """,
            (uuid4(), reference.object_id, job_id),
        )
        return reference

    @staticmethod
    def _slot_job_id(cursor: DbCursor, slot_id: UUID) -> UUID:
        cursor.execute("SELECT job_id FROM runtime.command_slots WHERE command_slot_id = %s", (slot_id,))
        row = cursor.fetchone()
        if row is None:
            raise RuntimeStoreError("shadow command slot vanished")
        return UUID(str(row[0]))

    def _shadow_measurement_artifacts(
        self, attempt: ShadowMeasurementAttempt
    ) -> tuple[ArtifactMember, ArtifactMember]:
        plan = _strict_json_object(attempt.canonical_plan_json, "shadow measurement plan")
        inputs = cast(dict[str, object], plan["shadow_inputs"])
        encoded_members = cast(list[object], plan["corpus_members"])
        scope = ArtifactScope("autocut_calibration", "shadow_run", attempt.plan_hash.removeprefix("sha256:"))
        native_invocations: list[dict[str, object]] = []
        result_members: list[dict[str, object]] = []
        for member, encoded in zip(attempt.members, encoded_members, strict=True):
            if member.raw_blob is None or member.projection_json is None or not isinstance(encoded, dict):
                raise CommandStateError("shadow finalizer lost staged member evidence")
            encoded_member = cast(dict[str, object], encoded)
            invocation = _strict_json_object(member.invocation_json, "shadow staged invocation")
            raw_context = _strict_json_object(member.context_json, "shadow staged raw context")
            projection = _strict_json_object(member.projection_json, "shadow staged projection")
            if (
                set(encoded_member)
                != {
                    "corpus_member_reference_sha256",
                    "expected_anchor_reference_sha256",
                    "native_invocation",
                    "raw_context",
                }
                or encoded_member["corpus_member_reference_sha256"]
                != member.corpus_member_reference_sha256
                or encoded_member["expected_anchor_reference_sha256"]
                != member.expected_anchor_reference_sha256
                or json.dumps(
                    encoded_member["native_invocation"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ) != member.invocation_json
                or json.dumps(
                    encoded_member["raw_context"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ) != member.context_json
            ):
                raise CommandStateError(
                    "shadow finalizer member context drifts from its immutable plan"
                )
            blob = _shadow_blob_mapping(member.raw_blob)
            native_invocations.append({
                "corpus_member_reference_sha256": member.corpus_member_reference_sha256,
                "expected_anchor_reference_sha256": member.expected_anchor_reference_sha256,
                "native_invocation": invocation,
                "native_response_blob": blob,
                "raw_context": raw_context,
            })
            result_members.append({
                "corpus_member_reference_sha256": member.corpus_member_reference_sha256,
                "expected_anchor_reference_sha256": member.expected_anchor_reference_sha256,
                "native_invocation": invocation,
                "native_response_blob": blob,
                "native_response_sha256": member.raw_blob.content_hash,
                "projection": projection,
            })
        manifest_payload: dict[str, object] = {
            "alignment_policy_sha256": inputs["alignment_policy_sha256"],
            "acceptance_policy_sha256": inputs["acceptance_policy_sha256"],
            "calibration_corpus_set_sha256": inputs["calibration_corpus_set_sha256"],
            "measurement_request_sha256": attempt.plan_hash,
            "native_invocations": native_invocations,
            "native_port_identity_sha256": inputs["native_port_identity_sha256"],
            "registry_snapshot_sha256": inputs["registry_snapshot_sha256"],
            "schema_version": "shadow-calibration-measurement-manifest-v3",
            "shadow_profile_source_sha256": inputs["profile_source_sha256"],
            "vad_merge_policy_sha256": inputs["vad_merge_policy_sha256"],
            "word_gap_policy_sha256": inputs["word_gap_policy_sha256"],
        }
        manifest = _shadow_artifact(scope, "calibration_measurement_manifest", "measurement-manifest", manifest_payload)
        result_payload: dict[str, object] = {
            "measurement_manifest_sha256": manifest.content_hash,
            "members": result_members,
            "per_producer_measurements": {
                "asr": _shadow_member_bound_aggregate(attempt.members, "asr"),
                "vad": _shadow_member_bound_aggregate(attempt.members, "vad"),
            },
            "schema_version": "shadow-calibration-measurement-results-v2",
        }
        return manifest, _shadow_artifact(
            scope, "calibration_measurement_results", "measurement-results", result_payload
        )

    def _ensure_job(self, cursor: DbCursor, job: Job) -> UUID:
        """Insert-or-verify one Job row, never leaking a raw unique violation.

        Uses INSERT … ON CONFLICT DO NOTHING RETURNING so that two concurrent
        _ensure_job calls for the same job_key never race.
        """
        job_id = uuid4()
        cursor.execute(
            """
            INSERT INTO runtime.jobs (job_id, job_key, profile, state)
            VALUES (%s, %s, %s, 'pending')
            ON CONFLICT (job_key) DO NOTHING
            RETURNING job_id, profile
            """,
            (job_id, job.job_key, job.profile),
        )
        row = cursor.fetchone()
        if row is not None:
            return UUID(str(row[0]))

        # Another transaction already created this job — verify profile match.
        cursor.execute(
            "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s FOR UPDATE",
            (job.job_key,),
        )
        existing = cursor.fetchone()
        if existing is None:
            raise RuntimeStoreError("job vanished after conflict")
        existing_id, profile = existing
        if _text(profile) != job.profile:
            raise StoreValidationError("job_key cannot be reused with a different profile")
        return UUID(str(existing_id))

    @staticmethod
    def _locked_slot(cursor: DbCursor, slot_id: UUID) -> tuple[UUID, str, str, str]:
        cursor.execute(
            """
            SELECT job_id, state, command_name, request_hash
              FROM runtime.command_slots WHERE command_slot_id = %s FOR UPDATE
            """,
            (slot_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise StoreValidationError("command_slot_id is unknown")
        return UUID(str(row[0])), _text(row[1]), _text(row[2]), _text(row[3])

    def _locked_job_then_slot(
        self, cursor: DbCursor, slot_id: UUID
    ) -> tuple[UUID, str, str, str]:
        """Lock an existing slot's aggregate in the global Job → slot order."""
        cursor.execute(
            "SELECT job_id FROM runtime.command_slots WHERE command_slot_id = %s", (slot_id,)
        )
        row = cursor.fetchone()
        if row is None:
            raise StoreValidationError("command_slot_id is unknown")
        job_id = UUID(str(row[0]))
        self._locked_job_state(cursor, job_id)
        locked_job_id, state, command_name, request_hash = self._locked_slot(cursor, slot_id)
        if locked_job_id != job_id:
            raise RuntimeStoreError("command slot changed jobs while being completed")
        return job_id, state, command_name, request_hash

    @staticmethod
    def _locked_job_state(cursor: DbCursor, job_id: UUID) -> str:
        cursor.execute("SELECT state FROM runtime.jobs WHERE job_id = %s FOR UPDATE", (job_id,))
        row = cursor.fetchone()
        if row is None:
            raise RuntimeStoreError("job vanished before command claim")
        return _text(row[0])

    @staticmethod
    def _complete_slot(cursor: DbCursor, slot_id: UUID, outcome: str) -> None:
        cursor.execute(
            "UPDATE runtime.command_slots SET state = %s, completed_at = transaction_timestamp()"
            " WHERE command_slot_id = %s",
            (outcome, slot_id),
        )

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
        expected = 1 if current is None else int(_text(current[0])) + 1
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
            if row is None or _text(row[0]) != set_hash:
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
            state=_text(state),  # type: ignore[arg-type]
            receipt_id=None if receipt_id is None else UUID(str(receipt_id)),
            artifact_set_id=None if set_id is None else UUID(str(set_id)),
            failure_code=None if failure_code is None else _text(failure_code),
            failure_detail_json=None if failure_detail is None else _text(failure_detail),
            job_id=job_id,
        )

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
            if isinstance(error, RuntimeStoreError):
                raise
            sqlstate = getattr(error, "sqlstate", None)
            if sqlstate == "23505":
                if self._is_first_head_race(error):
                    raise StaleHeadError("a concurrent write created the same logical artifact head") from error
                raise PersistenceConflictError("a persistence uniqueness constraint was violated") from error
            if sqlstate in ("40001", "40P01"):
                raise StoreConcurrencyError("database transaction should be retried") from error
            if self._is_runtime_database_error(error):
                raise RuntimeStoreError("database operation failed") from error
            raise
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

    @staticmethod
    def _is_first_head_race(error: Exception) -> bool:
        diagnostic = getattr(error, "diag", None)
        return (
            getattr(error, "sqlstate", None) == "23505"
            and getattr(diagnostic, "constraint_name", None) == "runtime_artifacts_scope_revision_key"
        )

    @staticmethod
    def _is_runtime_database_error(error: Exception) -> bool:
        """Keep caller mistakes visible while hiding driver-level failures."""
        return isinstance(error, (DatabaseError, InterfaceError))
