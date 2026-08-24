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
            ):
                cursor.execute((MIGRATIONS / name).read_text())


def test_provider_media_identity_is_reused_and_transitions_with_exact_cas() -> None:
    assert DSN is not None
    cache = PostgresArkFileCache(lambda: psycopg.connect(DSN))
    identity = {
        "provider_id": "doubao-ark-responses-stream",
        "content_hash": "sha256:" + "a" * 64,
        "byte_length": 123,
        "media_type": "video/mp4",
        "preprocess_policy_hash": "sha256:" + "b" * 64,
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
    )
    available = cache.record_available(
        processing.media_object_id,
        expected_version=processing.version,
        provider_status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(days=5),
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
        "content_hash": "sha256:" + "c" * 64,
        "byte_length": 123,
        "media_type": "video/mp4",
        "preprocess_policy_hash": "sha256:" + "d" * 64,
    }
    cache.claim(**identity)

    with pytest.raises(ArkFileCacheError):
        cache.claim(**{**identity, "byte_length": 124})
