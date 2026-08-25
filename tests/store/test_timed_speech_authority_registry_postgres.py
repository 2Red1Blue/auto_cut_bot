"""Disposable-PostgreSQL acceptance for explicit timed-speech authority bootstrap."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from autocut_kernel.media import (
    TimedSpeechCapability,
    TimedSpeechGuardPolicy,
    TimedSpeechProducerRequirement,
    TimedSpeechProfileKind,
    TimedSpeechProfileRegistryEntry,
)
from autocut_kernel.media.types import TimeBase
from autocut_kernel.registry import (
    AuthorityRegistrySnapshot,
    BootstrapTimedSpeechProfileRegistryCommand,
    StoreAnchoredTimedSpeechProfileResolver,
    TimedSpeechProfileKey,
    VerifiedTimedSpeechAuthorityContext,
)
from autocut_kernel.store import (
    IdempotencyConflictError,
    PostgresRuntimeStore,
)

from auto_cut_bot.pipeline.runtime.composition import (
    PipelineRuntime,
    PipelineRuntimeConfigurationError,
)

psycopg = pytest.importorskip("psycopg")

VERIFY_POSTGRES_DSN = "postgresql://ac_user:ac_password_2026@127.0.0.1:5433/ac_autocut_verify"
MIGRATIONS = Path("packages/autocut-kernel/migrations")
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64


def _entry() -> TimedSpeechProfileRegistryEntry:
    clock = TimeBase(1, 48_000)
    return TimedSpeechProfileRegistryEntry(
        profile_id="sensevoice_word_guard_v1",
        profile_version="1",
        kind=TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1,
        capability=TimedSpeechCapability.KNOWN_SPEECH_ONLY,
        transcript_requirement=TimedSpeechProducerRequirement(
            producer_id="funasr-asr",
            producer_kind="asr",
            inference_kind="sensevoice-word-timestamp",
            generation_policy_sha256=HASH_A,
            model_sha256=HASH_B,
            adapter_sha256=HASH_C,
            calibration_record_sha256=HASH_D,
            clock_id="audio-48k",
            time_base=clock,
        ),
        vad_requirement=TimedSpeechProducerRequirement(
            producer_id="funasr-vad",
            producer_kind="vad",
            inference_kind="fsmn-vad-direct",
            generation_policy_sha256=HASH_B,
            model_sha256=HASH_C,
            adapter_sha256=HASH_D,
            calibration_record_sha256=HASH_A,
            clock_id="audio-48k",
            time_base=clock,
        ),
        guard_policy=TimedSpeechGuardPolicy(HASH_B, "audio-48k", clock, 1, 1, 1, 1),
        registry_contract_sha256=HASH_C,
    )


def _context(registry_hash: str = HASH_D) -> VerifiedTimedSpeechAuthorityContext:
    entry = _entry()
    return VerifiedTimedSpeechAuthorityContext(
        AuthorityRegistrySnapshot(
            registry_hash,
            TimedSpeechProfileKey(entry.profile_id, entry.profile_version),
        ),
        entry,
    )


class _Worker:
    async def startup_reconstruct(self) -> tuple[str, ...]:
        return ()


@pytest.fixture(autouse=True)
def migrated_database() -> None:
    try:
        connection = psycopg.connect(VERIFY_POSTGRES_DSN, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("disposable authority PostgreSQL is unavailable")
    with connection:
        if connection.info.dbname != "ac_autocut_verify":
            pytest.fail("authority acceptance may run only against ac_autocut_verify")
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for migration in sorted(MIGRATIONS.glob("*.sql")):
                cursor.execute(migration.read_text(encoding="utf-8"))


def test_migration_bootstrap_resolver_and_preflight_prerequisite_replay() -> None:
    store = PostgresRuntimeStore(lambda: psycopg.connect(VERIFY_POSTGRES_DSN))
    context = _context()
    resolver = StoreAnchoredTimedSpeechProfileResolver(context.snapshot)
    runtime = PipelineRuntime(  # type: ignore[arg-type]
        None,
        _Worker(),
        None,
        resolver,
        store,
    )

    with pytest.raises(PipelineRuntimeConfigurationError, match="bootstrap anchor"):
        asyncio.run(runtime.startup_reconstruct())

    first = BootstrapTimedSpeechProfileRegistryCommand(store).execute(context.bootstrap_request())
    resolved = resolver.resolve(store)
    replay = BootstrapTimedSpeechProfileRegistryCommand(store).execute(context.bootstrap_request())
    replay_resolved = resolver.resolve(PostgresRuntimeStore(lambda: psycopg.connect(VERIFY_POSTGRES_DSN)))
    assert asyncio.run(runtime.startup_reconstruct()) == ()

    assert first.state == "succeeded"
    assert replay == first
    assert replay_resolved == resolved
    assert resolved.entry == context.entry
    assert resolved.snapshot == context.snapshot


def test_divergent_snapshot_conflicts_and_placeholder_snapshot_is_denied() -> None:
    store = PostgresRuntimeStore(lambda: psycopg.connect(VERIFY_POSTGRES_DSN))
    initial = _context()
    BootstrapTimedSpeechProfileRegistryCommand(store).execute(initial.bootstrap_request())

    with pytest.raises(IdempotencyConflictError, match="already anchored"):
        BootstrapTimedSpeechProfileRegistryCommand(store).execute(
            _context("sha256:" + "e" * 64).bootstrap_request()
        )
    with pytest.raises(ValueError, match="placeholder hash"):
        AuthorityRegistrySnapshot(
            "sha256:" + "0" * 64,
            TimedSpeechProfileKey("sensevoice_word_guard_v1", "1"),
        )
