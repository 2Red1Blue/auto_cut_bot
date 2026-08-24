from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from auto_cut_bot.pipeline.vlm import ArkFileCacheError, PostgresArkFileCache

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set AUTOCUT_TEST_POSTGRES_DSN to run disposable PostgreSQL tests",
)
MIGRATIONS = Path("packages/autocut-kernel/migrations")


@pytest.fixture(autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for name in (
                "0001_runtime_core.sql",
                "0002_runtime_core_constraints.sql",
                "0003_vlm_generation_and_run_finalization.sql",
                "0004_provider_media_objects.sql",
                "0006_ark_provider_recovery.sql",
            ):
                cursor.execute((MIGRATIONS / name).read_text())


def test_provider_media_identity_is_reused_and_transitions_with_exact_cas() -> None:
    assert DSN is not None
    cache = PostgresArkFileCache(lambda: psycopg.connect(DSN))
    identity = {
        "provider_id": "doubao-ark-responses-stream",
        "provider_scope_fingerprint": "sha256:" + "9" * 64,
        "content_hash": "sha256:" + "a" * 64,
        "byte_length": 123,
        "media_type": "video/mp4",
        "preprocess_policy_hash": "sha256:" + "b" * 64,
        "lease_seconds": 600,
        "unknown_outcome_quarantine_seconds": 86_400,
    }

    reserved, created = cache.claim(**identity)
    replay, replay_created = cache.claim(**identity)

    assert created is True
    assert replay_created is False
    assert replay == reserved

    processing = cache.record_processing(
        reserved.media_object_id,
        expected_version=0,
        provider_file_id="file-ark-1",
        provider_status="processing",
        expected_lease_token=reserved.lease_token,
    )
    available = cache.record_available(
        processing.media_object_id,
        expected_version=processing.version,
        provider_status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(days=5),
        expected_lease_token=processing.lease_token,
    )

    assert available.state == "available"
    assert available.provider_file_id == "file-ark-1"
    final_replay, _ = cache.claim(**identity)
    assert final_replay == available

    with pytest.raises(ArkFileCacheError):
        cache.mark_expired(
            available.media_object_id,
            expected_version=processing.version,
            provider_status="expired",
        )

    expired = cache.mark_expired(
        available.media_object_id,
        expected_version=available.version,
        provider_status="expired",
    )
    renewed, renewed_created = cache.claim(**identity)
    assert expired.state == "expired"
    assert renewed_created is True
    assert renewed.generation == 2
    assert renewed.media_object_id != available.media_object_id


def test_provider_media_identity_rejects_metadata_collision() -> None:
    assert DSN is not None
    cache = PostgresArkFileCache(lambda: psycopg.connect(DSN))
    identity = {
        "provider_id": "doubao-ark-responses-stream",
        "provider_scope_fingerprint": "sha256:" + "8" * 64,
        "content_hash": "sha256:" + "c" * 64,
        "byte_length": 123,
        "media_type": "video/mp4",
        "preprocess_policy_hash": "sha256:" + "d" * 64,
        "lease_seconds": 600,
        "unknown_outcome_quarantine_seconds": 86_400,
    }
    cache.claim(**identity)

    with pytest.raises(ArkFileCacheError):
        cache.claim(**{**identity, "byte_length": 124})


def test_provider_media_scope_fingerprint_prevents_cross_tenant_reuse() -> None:
    assert DSN is not None
    cache = PostgresArkFileCache(lambda: psycopg.connect(DSN))
    identity = {
        "provider_id": "doubao-ark-responses-stream",
        "content_hash": "sha256:" + "e" * 64,
        "byte_length": 123,
        "media_type": "video/mp4",
        "preprocess_policy_hash": "sha256:" + "f" * 64,
        "lease_seconds": 600,
        "unknown_outcome_quarantine_seconds": 86_400,
    }

    tenant_a, created_a = cache.claim(
        **identity,
        provider_scope_fingerprint="sha256:" + "1" * 64,
    )
    tenant_b, created_b = cache.claim(
        **identity,
        provider_scope_fingerprint="sha256:" + "2" * 64,
    )

    assert created_a is True and created_b is True
    assert tenant_a.media_object_id != tenant_b.media_object_id
    assert tenant_a.provider_scope_fingerprint != tenant_b.provider_scope_fingerprint


def test_expired_processing_lease_recovers_known_file_id_by_exact_cas() -> None:
    assert DSN is not None
    current = [datetime(2026, 8, 24, tzinfo=timezone.utc)]
    cache = PostgresArkFileCache(lambda: psycopg.connect(DSN), clock=lambda: current[0])
    identity = {
        "provider_id": "doubao-ark-responses-stream",
        "provider_scope_fingerprint": "sha256:" + "3" * 64,
        "content_hash": "sha256:" + "4" * 64,
        "byte_length": 123,
        "media_type": "video/mp4",
        "preprocess_policy_hash": "sha256:" + "5" * 64,
        "lease_seconds": 60,
        "unknown_outcome_quarantine_seconds": 600,
    }
    reserved, _ = cache.claim(**identity)
    processing = cache.record_processing(
        reserved.media_object_id,
        expected_version=reserved.version,
        expected_lease_token=reserved.lease_token,
        provider_file_id="file-known-after-crash",
        provider_status="processing",
    )

    current[0] += timedelta(seconds=61)
    recovered, lease_acquired = cache.claim(**identity)

    assert lease_acquired is True
    assert recovered.state == "processing"
    assert recovered.provider_file_id == "file-known-after-crash"
    assert recovered.version == processing.version + 1
    assert recovered.lease_token != processing.lease_token


def test_unknown_upload_is_quarantined_then_audit_expired_before_reclamation() -> None:
    assert DSN is not None
    current = [datetime(2026, 8, 24, tzinfo=timezone.utc)]
    cache = PostgresArkFileCache(lambda: psycopg.connect(DSN), clock=lambda: current[0])
    identity = {
        "provider_id": "doubao-ark-responses-stream",
        "provider_scope_fingerprint": "sha256:" + "6" * 64,
        "content_hash": "sha256:" + "7" * 64,
        "byte_length": 123,
        "media_type": "video/mp4",
        "preprocess_policy_hash": "sha256:" + "8" * 64,
        "lease_seconds": 60,
        "unknown_outcome_quarantine_seconds": 600,
    }
    reserved, _ = cache.claim(**identity)

    current[0] += timedelta(seconds=61)
    quarantined, acquired = cache.claim(**identity)
    assert acquired is False
    assert quarantined.state == "indeterminate"
    assert quarantined.provider_file_id is None
    assert quarantined.audit_expires_at == current[0] + timedelta(seconds=600)

    current[0] += timedelta(seconds=599)
    still_quarantined, acquired = cache.claim(**identity)
    assert acquired is False
    assert still_quarantined.media_object_id == reserved.media_object_id

    current[0] += timedelta(seconds=2)
    reclaimed, acquired = cache.claim(**identity)
    assert acquired is True
    assert reclaimed.state == "reserved"
    assert reclaimed.generation == 2
    assert reclaimed.media_object_id != reserved.media_object_id


def test_recovery_migration_quarantines_legacy_scope_without_reusing_it() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for name in (
                "0001_runtime_core.sql",
                "0002_runtime_core_constraints.sql",
                "0003_vlm_generation_and_run_finalization.sql",
                "0004_provider_media_objects.sql",
            ):
                cursor.execute((MIGRATIONS / name).read_text())
            cursor.execute(
                """
                INSERT INTO runtime.provider_media_objects
                    (media_object_id, provider_id, content_hash, byte_length,
                     media_type, preprocess_policy_hash, generation, state, version)
                VALUES (gen_random_uuid(), 'doubao-ark-responses-stream', %s, 123,
                        'video/mp4', %s, 1, 'reserved', 0)
                """,
                ("sha256:" + "a" * 64, "sha256:" + "b" * 64),
            )
            cursor.execute((MIGRATIONS / "0006_ark_provider_recovery.sql").read_text())
            cursor.execute(
                """
                SELECT provider_scope_fingerprint, lease_token, lease_expires_at
                  FROM runtime.provider_media_objects
                """
            )
            scope, lease_token, lease_expires_at = cursor.fetchone()

    assert scope == "sha256:" + "0" * 64
    assert lease_token is not None
    assert lease_expires_at is not None

    cache = PostgresArkFileCache(lambda: psycopg.connect(DSN))
    fresh, created = cache.claim(
        provider_id="doubao-ark-responses-stream",
        provider_scope_fingerprint="sha256:" + "c" * 64,
        content_hash="sha256:" + "a" * 64,
        byte_length=123,
        media_type="video/mp4",
        preprocess_policy_hash="sha256:" + "b" * 64,
        lease_seconds=600,
        unknown_outcome_quarantine_seconds=86_400,
    )
    assert created is True
    assert fresh.provider_scope_fingerprint == "sha256:" + "c" * 64
