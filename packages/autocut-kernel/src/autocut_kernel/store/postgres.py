"""Closed PostgreSQL persistence adapter for the local Pipeline MVP.

The adapter accepts only semantic command operations.  It does not expose a
cursor, generic row writer, legacy ArtifactBus object, or an execution escape
hatch to callers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol, TypeVar, cast
from uuid import UUID, uuid4

from psycopg import DatabaseError, InterfaceError

from ..vlm import (
    GENERATION_PROVIDER_LEASE_SECONDS,
    VlmValidationError,
    decode_vlm_observation_set,
)
from .errors import (
    BlobIntegrityError,
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
    RuntimeStoreError,
    SemanticResolutionProofIntegrityError,
    SemanticResolutionProofUnavailableError,
    StaleHeadError,
    StoreConcurrencyError,
    StoreValidationError,
    VlmObservationIntegrityError,
    VlmObservationUnavailableError,
)
from .models import (
    ArtifactMember,
    ArtifactScope,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    GenerationAttempt,
    Job,
    MediaEvidenceReference,
    PersistedMediaEvidence,
    PersistedMediaOutputs,
    PersistedRecipe,
    PersistedSemanticResolutionProof,
    PersistedVlmGenerationChild,
    PersistedVlmObservationSet,
    PersistedWholeSeriesSourceManifest,
    RecipeReference,
    SemanticResolutionProofReference,
    VlmObservationSetReference,
    VlmRequestRecordReference,
    WholeSeriesSourceManifestReference,
    canonical_payload_hash,
    canonical_recipe_scope,
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
        return tuple(references)
    except (KeyError, TypeError, ValueError, StoreValidationError) as error:
        raise StoreValidationError(
            "source manifest contains invalid proxy BlobRefs"
        ) from error


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


class PostgresRuntimeStore:
    """Persist one Job's idempotent commands, receipts and immutable artifacts."""

    def __init__(self, connection_factory: Callable[[], DbConnection]) -> None:
        if not callable(connection_factory):
            raise StoreValidationError("connection_factory must be callable")
        self._connection_factory = connection_factory

    # ------------------------------------------------------------------
    # claim_command
    # ------------------------------------------------------------------

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
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
                SELECT command_slot_id, command_name, request_hash
                  FROM runtime.command_slots
                 WHERE job_id = %s AND idempotency_key = %s
                   FOR UPDATE
                """,
                (job_id, claim.idempotency_key),
            )
            existing = cursor.fetchone()
            if existing is not None:
                slot_id_existing, command_name, request_hash = existing
                if _text(command_name) != claim.command_name or _text(request_hash) != claim.request_hash:
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
                    (command_slot_id, job_id, idempotency_key, command_name, request_hash, state)
                VALUES (%s, %s, %s, %s, %s, 'running')
                """,
                (slot_id, job_id, claim.idempotency_key, claim.command_name, claim.request_hash),
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

    # ------------------------------------------------------------------
    # commit_command_success
    # ------------------------------------------------------------------

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        """Atomically persist one non-empty immutable result set and success Receipt."""

        def operation(cursor: DbCursor) -> CommandOutcome:
            job_id, state, command_name, _ = self._locked_job_then_slot(
                cursor, success.command_slot_id
            )
            if state != "running":
                return self._replay_or_raise(
                    cursor, success.command_slot_id, job_id, "succeeded", success.set_hash
                )
            if command_name == "FinalizeRunOutcome":
                raise CommandStateError("FinalizeRunOutcome requires the explicit run finalizer API")
            if command_name == "GenerateVlmEvidenceCommand":
                raise CommandStateError(
                    "GenerateVlmEvidenceCommand success requires a committed generation attempt"
                )
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
            if state != "running":
                return self._replay_or_raise(
                    cursor, rejection.command_slot_id, job_id, rejection.outcome, None
                )
            if command_name == "FinalizeRunOutcome":
                raise CommandStateError("FinalizeRunOutcome requires the explicit run finalizer API")
            if command_name == "GenerateVlmEvidenceCommand":
                raise CommandStateError(
                    "GenerateVlmEvidenceCommand rejection requires the explicit generation API"
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
                raise BlobIntegrityError("immutable blob bytes are unavailable")
            content = row[0].tobytes() if isinstance(row[0], memoryview) else bytes(row[0])
            if (
                len(content) != durable.byte_length
                or "sha256:" + hashlib.sha256(content).hexdigest() != durable.content_hash
            ):
                raise BlobIntegrityError("immutable blob bytes fail exact integrity validation")
            return content

        return self._transaction(operation)

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
            job_id, state, command_name, slot_request_hash = self._locked_job_then_slot(
                cursor, command_slot_id
            )
            if command_name != "GenerateVlmEvidenceCommand":
                raise CommandStateError(
                    "generation attempts require a GenerateVlmEvidenceCommand slot"
                )
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
            previous, job_id, slot_state, command_name = self._locked_attempt_aggregate(
                cursor, previous_attempt_id
            )
            self._require_attempt_transition(
                previous, expected_version, ("failed",), "reserve retry"
            )
            if slot_state != "running" or command_name != "GenerateVlmEvidenceCommand":
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
            attempt, _, slot_state, command_name = self._locked_attempt_aggregate(cursor, attempt_id)
            self._require_attempt_transition(
                attempt, expected_version, ("reserved",), "dispatch"
            )
            if slot_state != "running" or command_name != "GenerateVlmEvidenceCommand":
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
            attempt, _, slot_state, command_name = self._locked_attempt_aggregate(
                cursor, attempt_id
            )
            self._require_attempt_transition(
                attempt,
                expected_version,
                ("dispatched", "indeterminate"),
                "acquire reconcile lease",
            )
            if slot_state != "running" or command_name != "GenerateVlmEvidenceCommand":
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
            attempt, _, slot_state, command_name = self._locked_attempt_aggregate(
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
            if slot_state != "running" or command_name != "GenerateVlmEvidenceCommand":
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
            attempt, job_id, slot_state, command_name = self._locked_attempt_aggregate(
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
            if slot_state != "running" or command_name != "GenerateVlmEvidenceCommand":
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
            attempt, job_id, slot_state, command_name = self._locked_attempt_aggregate(
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
            if command_name != "GenerateVlmEvidenceCommand":
                raise GenerationAttemptStateError("generation rejection requires generation slot")
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
            cursor.execute(
                """
                SELECT attempt_id
                  FROM runtime.generation_attempts
                 WHERE job_id = %s AND command_slot_id = %s
                 ORDER BY attempt_ordinal
                """,
                (UUID(str(job_id)), command_slot_id),
            )
            attempt_ids: list[UUID] = []
            while (row := cursor.fetchone()) is not None:
                attempt_ids.append(UUID(str(row[0])))
            return tuple(
                self._read_generation_attempt_by_id(
                    cursor, item, for_update=False
                )
                for item in attempt_ids
            )

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

    def read_committed_vlm_generation_child(
        self,
        job: Job,
        idempotency_key: str,
    ) -> PersistedVlmGenerationChild:
        """Reconstruct one committed VLM child from its immutable Store evidence."""

        if type(idempotency_key) is not str or not idempotency_key.strip():  # noqa: E721
            raise StoreValidationError("idempotency_key must be a non-empty string")

        def operation(cursor: DbCursor) -> PersistedVlmGenerationChild:
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
                attempt_receipt_id,
                attempt_artifact_set_id,
                receipt_id,
                receipt_outcome,
                receipt_artifact_set_id,
                set_hash,
                member_count,
            ) = row
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
            cursor.execute(
                """
                SELECT artifact.artifact_type, artifact.logical_id,
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
            artifacts: list[ArtifactMember] = []
            while (artifact_row := cursor.fetchone()) is not None:
                (
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
                    )
                )
            artifact_tuple = tuple(artifacts)
            if len(artifact_tuple) != 3:
                raise StoreValidationError("VLM ArtifactSet must contain exactly three members")
            CommandSuccess(slot_id, _text(set_hash), artifact_tuple)
            request_records = tuple(
                artifact
                for artifact in artifact_tuple
                if artifact.artifact_type == "vlm_request_record"
            )
            if len(request_records) != 1:
                raise StoreValidationError(
                    "VLM ArtifactSet requires one exact request record"
                )
            record = request_records[0]
            if record.scope != canonical_recipe_scope(job):
                raise StoreValidationError("VLM request record has a non-canonical scope")
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
            return PersistedVlmGenerationChild(
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
            )

        return self._transaction(operation)

    def read_committed_vlm_observation_set(
        self,
        job: Job,
        idempotency_key: str,
    ) -> PersistedVlmObservationSet:
        """Read and independently decode one exact committed observation set.

        The request record is proved first.  The second immutable read is pinned
        to that child's exact ArtifactSet id, so downstream stages never consume
        an uncommitted head or a caller-supplied VLM payload.
        """

        child = self.read_committed_vlm_generation_child(job, idempotency_key)

        def operation(cursor: DbCursor) -> PersistedVlmObservationSet:
            cursor.execute(
                """
                SELECT artifact.logical_id, artifact.revision,
                       artifact.namespace, artifact.scope_kind,
                       artifact.scope_key, artifact.content_hash,
                       artifact.payload_json::text
                  FROM runtime.artifact_set_members AS member
                  JOIN runtime.artifacts AS artifact
                    ON artifact.artifact_id = member.artifact_id
                   AND artifact.artifact_set_id = member.artifact_set_id
                 WHERE member.artifact_set_id = %s
                   AND artifact.artifact_type = 'vlm_observation_set'
                 ORDER BY member.ordinal
                """,
                (child.artifact_set_id,),
            )
            rows: list[tuple[object, ...]] = []
            while (row := cursor.fetchone()) is not None:
                rows.append(row)
            if len(rows) != 1:
                raise VlmObservationUnavailableError(
                    "committed VLM child does not contain one exact observation set"
                )
            (
                logical_id,
                revision,
                namespace,
                scope_kind,
                scope_key,
                content_hash,
                payload_json,
            ) = rows[0]
            serialized = _text(payload_json)
            reference = VlmObservationSetReference(
                scope=ArtifactScope(
                    _text(namespace),
                    _text(scope_kind),
                    _text(scope_key),
                ),
                logical_id=_text(logical_id),
                revision=int(_text(revision)),
                content_hash=_text(content_hash),
            )
            try:
                if canonical_payload_hash(serialized) != reference.content_hash:
                    raise StoreValidationError(
                        "VLM observation payload hash is invalid"
                    )
                decoded = decode_vlm_observation_set(
                    _strict_json_object(serialized, "VLM observation set")
                )
                return PersistedVlmObservationSet(
                    reference=reference,
                    payload_json=serialized,
                    observation_set=decoded,
                    source_child=child,
                )
            except (StoreValidationError, VlmValidationError) as error:
                raise VlmObservationIntegrityError(
                    "committed VLM observation set failed independent verification"
                ) from error

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
                SELECT artifact.logical_id, artifact.revision, artifact.namespace,
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
                   AND slot.command_name = 'PrepareWholeSeriesSourcesCommand'
                   AND slot.state = 'succeeded'
                   AND receipt.outcome = 'succeeded'
                   AND artifact_set.member_count = 1
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
                raise StoreValidationError("source manifest ArtifactSet is not singleton")
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
            CommandSuccess(slot_id, _text(set_hash), (artifact,))
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
            return PersistedWholeSeriesSourceManifest(
                reference,
                serialized,
                durable_blobs,
                durable_job_id,
                UUID(str(receipt_id)),
                artifact_set_id,
                slot_id,
            )

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
    # read_succeeded_semantic_resolution_proof
    # ------------------------------------------------------------------

    def read_succeeded_semantic_resolution_proof(
        self, job: Job
    ) -> PersistedSemanticResolutionProof:
        """Read the proof only from a complete succeeded semantic ArtifactSet.

        The query requires the four-member semantic output shape rather than a
        generic proof lookup: narrative graph, story set, editorial blueprint,
        and the exact resolution proof must share the command receipt/set.
        """

        def operation(cursor: DbCursor) -> PersistedSemanticResolutionProof:
            cursor.execute(
                "SELECT job_id, profile FROM runtime.jobs WHERE job_key = %s",
                (job.job_key,),
            )
            job_row = cursor.fetchone()
            if job_row is None:
                raise SemanticResolutionProofUnavailableError(
                    "job has no succeeded semantic resolution proof"
                )
            job_id, profile = job_row
            if _text(profile) != job.profile:
                raise JobProfileMismatchError("job_key belongs to a different profile")

            cursor.execute(
                """
                SELECT proof.logical_id, proof.revision, proof.content_hash,
                       proof.payload_json::text, artifact_set.artifact_set_id,
                       receipt.receipt_id, slot.command_slot_id
                  FROM runtime.command_slots AS slot
                  JOIN runtime.artifact_sets AS artifact_set
                    ON artifact_set.command_slot_id = slot.command_slot_id
                   AND artifact_set.job_id = slot.job_id
                  JOIN runtime.command_receipts AS receipt
                    ON receipt.command_slot_id = slot.command_slot_id
                   AND receipt.result_artifact_set_id = artifact_set.artifact_set_id
                  JOIN runtime.artifacts AS narrative
                    ON narrative.artifact_set_id = artifact_set.artifact_set_id
                   AND narrative.job_id = artifact_set.job_id
                   AND narrative.artifact_type = 'narrative_graph'
                   AND narrative.namespace = 'pipeline'
                   AND narrative.scope_kind = 'job'
                   AND narrative.scope_key = %s
                  JOIN runtime.artifact_set_members AS narrative_member
                    ON narrative_member.artifact_set_id = artifact_set.artifact_set_id
                   AND narrative_member.artifact_id = narrative.artifact_id
                  JOIN runtime.artifacts AS story
                    ON story.artifact_set_id = artifact_set.artifact_set_id
                   AND story.job_id = artifact_set.job_id
                   AND story.artifact_type = 'story_set'
                   AND story.logical_id = 'story_set'
                   AND story.namespace = 'pipeline'
                   AND story.scope_kind = 'job'
                   AND story.scope_key = %s
                  JOIN runtime.artifact_set_members AS story_member
                    ON story_member.artifact_set_id = artifact_set.artifact_set_id
                   AND story_member.artifact_id = story.artifact_id
                  JOIN runtime.artifacts AS blueprint
                    ON blueprint.artifact_set_id = artifact_set.artifact_set_id
                   AND blueprint.job_id = artifact_set.job_id
                   AND blueprint.artifact_type = 'editorial_blueprint'
                   AND blueprint.namespace = 'pipeline'
                   AND blueprint.scope_kind = 'job'
                   AND blueprint.scope_key = %s
                  JOIN runtime.artifact_set_members AS blueprint_member
                    ON blueprint_member.artifact_set_id = artifact_set.artifact_set_id
                   AND blueprint_member.artifact_id = blueprint.artifact_id
                  JOIN runtime.artifacts AS proof
                    ON proof.artifact_set_id = artifact_set.artifact_set_id
                   AND proof.job_id = artifact_set.job_id
                   AND proof.artifact_type = 'semantic_resolution_proof'
                   AND proof.logical_id = 'semantic_resolution_proof'
                   AND proof.namespace = 'pipeline'
                   AND proof.scope_kind = 'job'
                   AND proof.scope_key = %s
                  JOIN runtime.artifact_set_members AS proof_member
                    ON proof_member.artifact_set_id = artifact_set.artifact_set_id
                   AND proof_member.artifact_id = proof.artifact_id
                 WHERE slot.job_id = %s
                   AND slot.command_name = 'semantic_chain_command'
                   AND slot.state = 'succeeded'
                   AND receipt.outcome = 'succeeded'
                   AND artifact_set.member_count = 4
                """,
                (job.job_key, job.job_key, job.job_key, job.job_key, UUID(str(job_id))),
            )
            rows: list[tuple[object, ...]] = []
            while (row := cursor.fetchone()) is not None:
                rows.append(row)
            if not rows:
                raise SemanticResolutionProofUnavailableError(
                    "no complete succeeded semantic resolution proof is available"
                )
            if len(rows) != 1:
                raise SemanticResolutionProofIntegrityError(
                    "semantic resolution proof resolved to multiple durable rows"
                )
            logical_id, revision, content_hash, payload_json, artifact_set_id, receipt_id, command_slot_id = rows[0]
            try:
                reference = SemanticResolutionProofReference(
                    canonical_recipe_scope(job),
                    _text(logical_id),
                    int(_text(revision)),
                    _text(content_hash),
                )
                if canonical_payload_hash(_text(payload_json)) != reference.content_hash:
                    raise StoreValidationError("semantic proof payload hash does not match artifact identity")
                return PersistedSemanticResolutionProof(
                    reference,
                    _text(payload_json),
                    UUID(str(job_id)),
                    UUID(str(receipt_id)),
                    UUID(str(artifact_set_id)),
                    UUID(str(command_slot_id)),
                )
            except (StoreValidationError, TypeError, ValueError) as error:
                raise SemanticResolutionProofIntegrityError(
                    "semantic resolution proof failed immutable provenance validation"
                ) from error

        return self._transaction(operation)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

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
        if job_id != expected_job_id:
            raise RuntimeStoreError("generation attempt changed jobs while being locked")
        attempt = self._read_generation_attempt_by_id(cursor, attempt_id, for_update=True)
        if attempt.job_id != job_id or attempt.command_slot_id != slot_id:
            raise RuntimeStoreError("generation attempt identity changed while being locked")
        return attempt, job_id, slot_state, command_name

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
