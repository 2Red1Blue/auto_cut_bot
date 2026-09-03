"""Opt-in PostgreSQL acceptance for hybrid inline/object-backed blob metadata."""

from __future__ import annotations

import hashlib
import io
import os
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4

import pytest
from autocut_kernel.store import (
    BlobIntegrityError,
    BlobUnavailableError,
    CommandClaim,
    CommandRejection,
    CommandStateError,
    Job,
    ObjectStoreWriteLimits,
    PendingObjectIntent,
    PostgresRuntimeStore,
    S3ObjectStoreConfig,
    S3PendingObjectStore,
    StoreValidationError,
)
from autocut_kernel.store.models import MaterializationError, MaterializationLimits
from autocut_kernel.store.object_store import (
    _issue_pending_object_reservation,  # pyright: ignore[reportPrivateUsage]
)

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set AUTOCUT_TEST_POSTGRES_DSN to disposable ac_autocut_verify",
)
MIGRATIONS = Path("packages/autocut-kernel/migrations")
UPGRADE_JOB_ID = uuid4()
UPGRADE_OBJECT_ID = uuid4()
UPGRADE_CONTENT = b'{"preexisting":"inline evidence"}'


def _hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


class _ExactObjectClient:
    def __init__(self) -> None:
        self.object: dict[str, object] | None = None
        self.content: bytes | None = None

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        body = cast(BinaryIO, kwargs["Body"])
        content = body.read()
        assert len(content) == kwargs["ContentLength"]
        self.content = content
        self.object = {
            "ChecksumSHA256": kwargs["ChecksumSHA256"],
            "ContentLength": kwargs["ContentLength"],
            "ContentType": kwargs["ContentType"],
            "ETag": '"exact-etag"',
            "Key": kwargs["Key"],
            "Metadata": kwargs["Metadata"],
            "VersionId": "version-1",
        }
        return {}

    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        assert self.object is not None
        assert kwargs["Key"] == self.object["Key"]
        if "VersionId" in kwargs:
            assert kwargs["VersionId"] == self.object["VersionId"]
        return self.object

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        assert self.object is not None
        assert self.content is not None
        assert kwargs["Key"] == self.object["Key"]
        assert kwargs["IfMatch"] == self.object["ETag"]
        assert kwargs["VersionId"] == self.object["VersionId"]
        return {**self.object, "Body": io.BytesIO(self.content)}


def _object_adapter(
    client: _ExactObjectClient | None = None,
) -> S3PendingObjectStore:
    return S3PendingObjectStore(
        _ExactObjectClient() if client is None else client,
        S3ObjectStoreConfig(
            backend_id="workspace-s3",
            storage_region="local-primary",
            bucket="private-renders",
            key_prefix="autocut/workspace",
        ),
    )


def _external_store() -> tuple[PostgresRuntimeStore, S3PendingObjectStore]:
    assert DSN is not None
    adapter = _object_adapter()
    return (
        PostgresRuntimeStore(
            lambda: psycopg.connect(DSN),
            object_store_verifier=adapter,
        ),
        adapter,
    )


def _verified_object(
    content: bytes,
    attempt_directory: Path,
    store: PostgresRuntimeStore,
    job: Job,
    adapter: S3PendingObjectStore,
) -> tuple[object, object]:
    attempt_directory.mkdir(mode=0o700)
    output = attempt_directory / "render.mp4"
    output.write_bytes(content)
    output.chmod(0o400)
    intent = PendingObjectIntent(
        object_id=uuid4(),
        content_hash=_hash(content),
        byte_length=len(content),
        media_type="video/mp4",
    )
    reservation = store.reserve_object_write(job, intent, adapter.target_for(intent))
    verified = adapter.put_path(
        reservation,
        source_path=output,
        attempt_directory=attempt_directory,
        limits=ObjectStoreWriteLimits(
            max_object_bytes=1024 * 1024,
            verification_chunk_bytes=1024,
        ),
    )
    return reservation, verified


def _unreserved_verified_object(
    content: bytes,
    attempt_directory: Path,
    adapter: S3PendingObjectStore,
) -> tuple[object, object]:
    attempt_directory.mkdir(mode=0o700)
    output = attempt_directory / "render.mp4"
    output.write_bytes(content)
    output.chmod(0o400)
    intent = PendingObjectIntent(
        object_id=uuid4(),
        content_hash=_hash(content),
        byte_length=len(content),
        media_type="video/mp4",
    )
    reservation = _issue_pending_object_reservation(
        intent=intent,
        target=adapter.target_for(intent),
        job_id=uuid4(),
        reservation_token=uuid4(),
        expected_version=0,
    )
    return reservation, adapter.put_path(
        reservation,
        source_path=output,
        attempt_directory=attempt_directory,
        limits=ObjectStoreWriteLimits(
            max_object_bytes=1024 * 1024,
            verification_chunk_bytes=1024,
        ),
    )


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        if connection.info.dbname != "ac_autocut_verify":
            pytest.fail("object-backed blob tests may reset only ac_autocut_verify")
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            migrations = sorted(MIGRATIONS.glob("*.sql"))
            upgrade = MIGRATIONS / "0054_object_backed_blob_metadata.sql"
            for migration in migrations:
                if migration == upgrade:
                    break
                cursor.execute(migration.read_text(encoding="utf-8"))
            cursor.execute(
                """
                INSERT INTO runtime.jobs (job_id, job_key, profile, state)
                VALUES (%s, 'preexisting-inline-upgrade', 'test', 'pending')
                """,
                (UPGRADE_JOB_ID,),
            )
            cursor.execute(
                """
                INSERT INTO storage.blob_objects (
                    object_id, content_hash, byte_length, media_type, content_bytes
                ) VALUES (%s, %s, %s, 'application/json', %s)
                """,
                (
                    UPGRADE_OBJECT_ID,
                    _hash(UPGRADE_CONTENT),
                    len(UPGRADE_CONTENT),
                    UPGRADE_CONTENT,
                ),
            )
            cursor.execute(
                """
                INSERT INTO storage.blob_claims (blob_claim_id, object_id, job_id)
                VALUES (%s, %s, %s)
                """,
                (uuid4(), UPGRADE_OBJECT_ID, UPGRADE_JOB_ID),
            )
            cursor.execute(upgrade.read_text(encoding="utf-8"))
            for migration in migrations[migrations.index(upgrade) + 1 :]:
                cursor.execute(migration.read_text(encoding="utf-8"))


def test_preexisting_inline_blob_and_claim_survive_0054_upgrade() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    reference = store.put_immutable_blob(
        Job("preexisting-inline-upgrade", "test"),
        content=UPGRADE_CONTENT,
        content_hash=_hash(UPGRADE_CONTENT),
        media_type="application/json",
    )

    assert reference.object_id == UPGRADE_OBJECT_ID
    assert (
        store.read_immutable_blob(Job("preexisting-inline-upgrade", "test"), reference)
        == UPGRADE_CONTENT
    )


def test_existing_inline_blob_round_trip_survives_hybrid_migration() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("hybrid-inline", "test")
    content = b'{"bounded":"evidence"}'

    reference = store.put_immutable_blob(
        job,
        content=content,
        content_hash=_hash(content),
        media_type="application/json",
    )

    assert store.read_immutable_blob(job, reference) == content
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT storage_kind, storage_backend_id, storage_locator, verified_at
              FROM storage.blob_objects WHERE object_id = %s
            """,
            (reference.object_id,),
        )
        assert cursor.fetchone() == ("postgres_inline", None, None, None)


def test_external_blob_shape_accepts_locator_only_and_remains_immutable() -> None:
    assert DSN is not None
    object_id = uuid4()
    content_hash = _hash(b"external-render")
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO storage.blob_objects (
                object_id, content_hash, byte_length, media_type, content_bytes,
                storage_kind, storage_backend_id, storage_region,
                storage_locator, storage_etag, storage_version_id,
                write_strategy, verified_at
            ) VALUES (
                %s, %s, 15, 'video/mp4', NULL,
                's3_compatible', 'workspace-s3', 'local-primary',
                %s, '"etag"', 'version-1', 's3-single-put-v1',
                transaction_timestamp()
            )
            """,
            (object_id, content_hash, f"autocut/workspace/{object_id.hex}"),
        )
        connection.commit()

        with pytest.raises(Exception, match="immutable blob objects"):
            cursor.execute(
                "UPDATE storage.blob_objects SET storage_etag = '\"changed\"' WHERE object_id = %s",
                (object_id,),
            )
        connection.rollback()


def test_object_write_intent_cannot_be_inserted_as_already_resolved() -> None:
    assert DSN is not None
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT job_id FROM runtime.jobs WHERE job_key = 'preexisting-inline-upgrade'"
        )
        job_id = cursor.fetchone()[0]
        with pytest.raises(Exception, match="must begin reserved at version zero"):
            cursor.execute(
                """
                INSERT INTO storage.object_write_intents (
                    object_id, job_id, content_hash, byte_length, media_type,
                    storage_backend_id, storage_region, storage_locator,
                    write_strategy, reservation_token, state, version,
                    resolved_object_id, resolved_at
                ) VALUES (
                    %s, %s, %s, 1, 'video/mp4',
                    'workspace-s3', 'local-primary', 'autocut/workspace/resolved',
                    's3-single-put-v1', %s, 'resolved', 1,
                    %s, transaction_timestamp()
                )
                """,
                (uuid4(), job_id, _hash(b"x"), uuid4(), UPGRADE_OBJECT_ID),
            )
        connection.rollback()


def test_external_blob_rejects_inline_bytes_and_duplicate_locator() -> None:
    assert DSN is not None
    locator = "autocut/workspace/fixed-locator"
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO storage.blob_objects (
                object_id, content_hash, byte_length, media_type, content_bytes,
                storage_kind, storage_backend_id, storage_region,
                storage_locator, storage_etag, write_strategy, verified_at
            ) VALUES (
                %s, %s, 1, 'video/mp4', NULL,
                's3_compatible', 'workspace-s3', 'local-primary',
                %s, '"etag-a"', 's3-single-put-v1', transaction_timestamp()
            )
            """,
            (uuid4(), _hash(b"a"), locator),
        )
        connection.commit()

        with pytest.raises(Exception):
            cursor.execute(
                """
                INSERT INTO storage.blob_objects (
                    object_id, content_hash, byte_length, media_type, content_bytes,
                    storage_kind, storage_backend_id, storage_region,
                    storage_locator, storage_etag, write_strategy, verified_at
                ) VALUES (
                    %s, %s, 1, 'video/mp4', %s,
                    's3_compatible', 'workspace-s3', 'local-primary',
                    'autocut/workspace/with-bytes', '"etag-b"',
                    's3-single-put-v1', transaction_timestamp()
                )
                """,
                (uuid4(), _hash(b"b"), b"b"),
            )
        connection.rollback()

        with pytest.raises(Exception):
            cursor.execute(
                """
                INSERT INTO storage.blob_objects (
                    object_id, content_hash, byte_length, media_type, content_bytes,
                    storage_kind, storage_backend_id, storage_region,
                    storage_locator, storage_etag, write_strategy, verified_at
                ) VALUES (
                    %s, %s, 0, 'video/mp4', NULL,
                    's3_compatible', 'workspace-s3', 'local-primary',
                    'autocut/workspace/empty', '"etag-empty"',
                    's3-single-put-v1', transaction_timestamp()
                )
                """,
                (uuid4(), _hash(b"")),
            )
        connection.rollback()

        with pytest.raises(Exception):
            cursor.execute(
                """
                INSERT INTO storage.blob_objects (
                    object_id, content_hash, byte_length, media_type, content_bytes,
                    storage_kind, storage_backend_id, storage_region,
                    storage_locator, storage_etag, write_strategy, verified_at
                ) VALUES (
                    %s, %s, 1, 'video/mp4', NULL,
                    's3_compatible', 'workspace-s3', 'local-primary',
                    %s, '"etag-c"', 's3-single-put-v1', transaction_timestamp()
                )
                """,
                (uuid4(), _hash(b"c"), locator),
            )


def test_verified_external_object_is_claimed_without_exposing_locator(
    tmp_path: Path,
) -> None:
    assert DSN is not None
    store, adapter = _external_store()
    job = Job("verified-external-object", "test")
    content = b"verified production render"
    reservation, verified = _verified_object(
        content,
        tmp_path / "attempt-1",
        store,
        job,
        adapter,
    )

    reference = store.claim_verified_object(job, reservation, verified)

    assert store.claim_verified_object(job, reservation, verified) == reference
    assert type(reference.object_id) is UUID
    assert reference.content_hash == _hash(content)
    assert reference.byte_length == len(content)
    assert reference.media_type == "video/mp4"
    assert not hasattr(reference, "storage_locator")
    with pytest.raises(BlobUnavailableError, match="bytes are unavailable"):
        store.read_immutable_blob(job, reference)
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT object.content_bytes, object.storage_kind,
                   object.storage_backend_id, object.storage_region,
                   object.storage_locator, object.storage_etag,
                   object.storage_version_id, object.write_strategy,
                   object.verified_at IS NOT NULL, count(claim.blob_claim_id)
              FROM storage.blob_objects AS object
              JOIN storage.blob_claims AS claim ON claim.object_id = object.object_id
             WHERE object.object_id = %s
             GROUP BY object.object_id
            """,
            (reference.object_id,),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0:4] == (None, "s3_compatible", "workspace-s3", "local-primary")
        assert str(row[4]).startswith("autocut/workspace/")
        assert row[5:] == (
            '"exact-etag"',
            "version-1",
            "s3-single-put-v1",
            True,
            1,
        )


def test_external_blob_materializes_after_store_restart_without_local_path(
    tmp_path: Path,
) -> None:
    assert DSN is not None
    client = _ExactObjectClient()
    writer_adapter = _object_adapter(client)
    writer_store = PostgresRuntimeStore(
        lambda: psycopg.connect(DSN),
        object_store_verifier=writer_adapter,
    )
    job = Job("restart-safe-external-materialization", "test")
    content = b"restart-safe exact production render bytes"
    reservation, verified = _verified_object(
        content,
        tmp_path / "attempt-restart-safe",
        writer_store,
        job,
        writer_adapter,
    )
    reference = writer_store.claim_verified_object(
        job,
        reservation,
        verified,
    )

    staging_root = tmp_path / "restart-safe-staging"
    restarted_store = PostgresRuntimeStore(
        lambda: psycopg.connect(DSN),
        materialization_staging_root=staging_root,
        object_store_verifier=_object_adapter(client),
    )
    limits = MaterializationLimits(
        max_source_bytes=1024,
        timed_speech_max_request_bytes=1024,
        copy_chunk_bytes=7,
        staging_quota_bytes=4096,
    )

    lease = restarted_store.materialize_immutable_blob(job, reference, limits)
    materialized_path = lease.path
    try:
        assert lease.reference == reference
        assert materialized_path.read_bytes() == content
        assert materialized_path.stat().st_mode & 0o777 == 0o400
        assert str(materialized_path).startswith(str(staging_root))
    finally:
        lease.close()
    assert not materialized_path.exists()

    with pytest.raises(MaterializationError) as foreign_error:
        restarted_store.materialize_immutable_blob(
            Job("restart-safe-foreign-job", "test"),
            reference,
            limits,
        )
    assert foreign_error.value.code == "COMMITTED_SOURCE_BLOB_INTEGRITY_FAILED"

    unconfigured_root = tmp_path / "unconfigured-staging"
    unconfigured_store = PostgresRuntimeStore(
        lambda: psycopg.connect(DSN),
        materialization_staging_root=unconfigured_root,
    )
    with pytest.raises(MaterializationError) as unconfigured_error:
        unconfigured_store.materialize_immutable_blob(job, reference, limits)
    assert unconfigured_error.value.code == "MEDIA_MATERIALIZATION_INFRASTRUCTURE_FAILED"
    assert list(unconfigured_root.iterdir()) == []


def test_external_claim_rejects_tampered_or_foreign_adapter_verification(
    tmp_path: Path,
) -> None:
    assert DSN is not None
    store, adapter = _external_store()
    job = Job("adapter-bound-verification", "test")
    reservation, verified = _verified_object(
        b"adapter-bound immutable render",
        tmp_path / "attempt-adapter-bound",
        store,
        job,
        adapter,
    )

    tampered = replace(cast(Any, verified), etag='"forged"')
    with pytest.raises(BlobIntegrityError, match="signature"):
        store.claim_verified_object(job, reservation, tampered)

    foreign_store = PostgresRuntimeStore(
        lambda: psycopg.connect(DSN),
        object_store_verifier=_object_adapter(),
    )
    with pytest.raises(BlobIntegrityError, match="signature"):
        foreign_store.claim_verified_object(job, reservation, verified)

    unconfigured_store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    with pytest.raises(StoreValidationError, match="configured object-store verifier"):
        unconfigured_store.claim_verified_object(job, reservation, verified)

    assert store.claim_verified_object(job, reservation, verified).content_hash == _hash(
        b"adapter-bound immutable render"
    )


def test_verified_content_deduplicates_across_jobs_and_claims_existing_object(
    tmp_path: Path,
) -> None:
    assert DSN is not None
    store, adapter = _external_store()
    content = b"same immutable render bytes"
    first_job = Job("dedup-first", "test")
    second_job = Job("dedup-second", "test")
    first = _verified_object(
        content,
        tmp_path / "attempt-first",
        store,
        first_job,
        adapter,
    )
    second = _verified_object(
        content,
        tmp_path / "attempt-second",
        store,
        second_job,
        adapter,
    )

    first_reference = store.claim_verified_object(first_job, *first)
    second_reference = store.claim_verified_object(second_job, *second)

    assert second_reference == first_reference
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)
              FROM storage.blob_claims
             WHERE object_id = %s
               AND job_id IN (
                    SELECT job_id FROM runtime.jobs
                     WHERE job_key IN ('dedup-first', 'dedup-second')
               )
            """,
            (first_reference.object_id,),
        )
        assert cursor.fetchone() == (2,)


def test_terminal_job_cannot_claim_verified_external_object(tmp_path: Path) -> None:
    assert DSN is not None
    store, adapter = _external_store()
    job = Job("terminal-external-claim", "test")
    seed = b"create terminal job"
    store.put_immutable_blob(
        job,
        content=seed,
        content_hash=_hash(seed),
        media_type="application/json",
    )
    reservation, verified = _verified_object(
        b"must not be claimed",
        tmp_path / "attempt-terminal",
        store,
        job,
        adapter,
    )
    finalizer = store.claim_command(
        CommandClaim(
            job,
            "terminal-external-claim-finalizer",
            "FinalizeRunOutcome",
            _hash(b"terminal-external-claim-finalizer"),
            execution_kind="deterministic",
        )
    )
    store.finalize_run_rejection(
        CommandRejection(
            finalizer.command_slot_id,
            "EXPECTED_TEST_TERMINATION",
            "{}",
            "failed",
        )
    )

    with pytest.raises(CommandStateError, match="terminal jobs cannot claim"):
        store.claim_verified_object(job, reservation, verified)


def test_external_claim_rejects_same_hash_inline_blob(tmp_path: Path) -> None:
    assert DSN is not None
    store, adapter = _external_store()
    content = b"inline bytes cannot satisfy an external render claim"
    inline_job = Job("external-inline-conflict-source", "test")
    store.put_immutable_blob(
        inline_job,
        content=content,
        content_hash=_hash(content),
        media_type="video/mp4",
    )
    external_job = Job("external-inline-conflict-target", "test")
    reservation, verified = _verified_object(
        content,
        tmp_path / "attempt-inline-conflict",
        store,
        external_job,
        adapter,
    )

    with pytest.raises(BlobIntegrityError, match="conflicts with an inline"):
        store.claim_verified_object(external_job, reservation, verified)


def test_external_claim_requires_matching_persisted_reservation(tmp_path: Path) -> None:
    assert DSN is not None
    store, adapter = _external_store()
    job = Job("missing-object-reservation", "test")
    reservation, verified = _unreserved_verified_object(
        b"provider object without database reservation",
        tmp_path / "attempt-unreserved",
        adapter,
    )

    with pytest.raises(BlobIntegrityError, match="no durable pre-write reservation"):
        store.claim_verified_object(job, reservation, verified)


def test_external_claim_rejects_wrong_token_job_target_and_version(
    tmp_path: Path,
) -> None:
    assert DSN is not None
    store, adapter = _external_store()
    job = Job("tampered-object-reservation", "test")
    reservation, verified = _verified_object(
        b"exact reservation must not be rewritten",
        tmp_path / "attempt-tampered",
        store,
        job,
        adapter,
    )
    durable = cast(Any, reservation)
    wrong_target = replace(durable.target, storage_region="other-region")

    for tampered in (
        replace(durable, reservation_token=uuid4()),
        replace(durable, expected_version=1),
        replace(durable, target=wrong_target),
    ):
        with pytest.raises(
            BlobIntegrityError,
            match="signature|durable pre-write reservation",
        ):
            store.claim_verified_object(job, tampered, verified)
    with pytest.raises(BlobIntegrityError, match="persisted write reservation"):
        store.claim_verified_object(Job("wrong-reservation-job", "test"), reservation, verified)


def test_concurrent_exact_claim_has_one_resolution_and_one_safe_replay(
    tmp_path: Path,
) -> None:
    assert DSN is not None
    store, adapter = _external_store()
    job = Job("concurrent-external-claim", "test")
    reservation, verified = _verified_object(
        b"one exact concurrent external object",
        tmp_path / "attempt-concurrent",
        store,
        job,
        adapter,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                store.claim_verified_object,
                job,
                reservation,
                verified,
            )
            for _ in range(2)
        ]
    references = [future.result() for future in futures]

    assert references[0] == references[1]
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT intent.state, intent.version, count(claim.blob_claim_id)
              FROM storage.object_write_intents AS intent
              JOIN storage.blob_claims AS claim
                ON claim.object_id = intent.resolved_object_id
             WHERE intent.object_id = %s
             GROUP BY intent.object_id
            """,
            (references[0].object_id,),
        )
        assert cursor.fetchone() == ("resolved", 1, 1)
