from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer

from auto_cut_bot.api.server import create_app
from auto_cut_bot.pipeline.runtime import (
    DurablePipelineRunService,
    IdempotencyConflictError,
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineRunValidationError,
    PipelineStageContext,
    PipelineStageReconciler,
    PipelineStageResult,
    PostgresPipelineRunStore,
    PostgresPipelineScheduler,
    SourceDeniedError,
    StaleRunVersionError,
)
from auto_cut_bot.pipeline.runtime.composition import ConfiguredSourceCatalog, SourceCatalogEntry
from auto_cut_bot.pipeline.source_prep import AuthorizedSeriesSourceRoot
from auto_cut_bot.pipeline.vlm.request_factory import DoubaoVlmRequestPolicy

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set AUTOCUT_TEST_POSTGRES_DSN to a disposable PostgreSQL database",
)


@pytest.fixture(autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            for migration in sorted(
                Path("packages/autocut-kernel/migrations").glob("*.sql")
            ):
                cursor.execute(migration.read_text(encoding="utf-8"))


def _composition(source_root: Path, *additional_source_roots: Path):
    assert DSN is not None

    def factory():
        return psycopg.connect(DSN)

    store = PostgresPipelineRunStore(factory)
    scheduler = PostgresPipelineScheduler(factory)
    authority = _source_catalog(source_root / "input", *additional_source_roots)
    return (
        DurablePipelineRunService(
            store,
            scheduler,
            authority,
            execution_profile=_execution_profile(),
        ),
        store,
        scheduler,
    )


def _source_catalog(
    source_root: Path,
    *additional_source_roots: Path,
) -> ConfiguredSourceCatalog:
    roots = (source_root, *additional_source_roots)
    return ConfiguredSourceCatalog(
        tuple(
            SourceCatalogEntry(
                AuthorizedSeriesSourceRoot(
                    root=root.resolve(),
                    authorization_id=f"source:fixture-{index}",
                    series_id=f"series:fixture-{index}",
                    expected_source_count=1,
                )
            )
            for index, root in enumerate(roots, start=1)
        )
    )


def _execution_profile(
    *,
    model_id: str = "doubao-seed-2-1-pro-260628",
) -> PipelineExecutionProfile:
    return PipelineExecutionProfile.from_doubao_policy(
        DoubaoVlmRequestPolicy(model_id=model_id)
    )


def _force_terminal_command(cursor, command_id: str, outcome: str) -> str:
    lease_id = f"fixture-{command_id}"
    cursor.execute(
        """
        UPDATE runtime.pipeline_commands
           SET state = 'running', version = version + 1, lease_id = %s,
               lease_expires_at = transaction_timestamp() + interval '1 hour',
               updated_at = transaction_timestamp()
         WHERE command_id = %s AND state = 'pending'
        """,
        (lease_id, command_id),
    )
    receipt_id = str(uuid4())
    cursor.execute(
        """
        INSERT INTO runtime.pipeline_run_receipts (receipt_id, command_id, outcome)
        VALUES (%s, %s, %s)
        """,
        (receipt_id, command_id, outcome),
    )
    cursor.execute(
        """
        UPDATE runtime.pipeline_commands
           SET state = %s, version = version + 1,
               lease_id = NULL, lease_expires_at = NULL,
               completed_at = transaction_timestamp(),
               updated_at = transaction_timestamp()
         WHERE command_id = %s AND state = 'running' AND lease_id = %s
        """,
        (outcome, command_id, lease_id),
    )
    return receipt_id


@pytest.mark.asyncio
async def test_claim_and_outbox_are_atomic_and_reconstruct_after_restart(tmp_path: Path) -> None:
    service, store, scheduler = _composition(tmp_path)
    request = PipelineRunRequest.from_mapping(
        {"profile": "test", "source_root": str(tmp_path / "input")}
    )

    first = await service.submit(request, "postgres-request-1")
    replay = await service.submit(request, "postgres-request-1")

    assert replay.replayed is True
    assert replay.snapshot.run_id == first.snapshot.run_id
    assert replay.snapshot.commands[0].stage == "source_prep"
    assert replay.snapshot.commands[0].status == "pending"
    assert replay.snapshot.commands[0].receipt_id is None
    assert tuple(command.stage for command in replay.snapshot.commands) == (
        "source_prep",
        "vlm",
    )
    assert await scheduler.pending_run_ids() == (first.snapshot.run_id,)

    restarted = DurablePipelineRunService(
        PostgresPipelineRunStore(store.connection_factory),
        PostgresPipelineScheduler(store.connection_factory),
        _source_catalog(tmp_path / "input"),
    )
    assert await restarted.status(first.snapshot.run_id) == first.snapshot
    assert await restarted.reconstruct() == (first.snapshot.run_id,)


@pytest.mark.asyncio
async def test_postgres_replay_uses_frozen_profile_and_new_key_uses_changed_profile(
    tmp_path: Path,
) -> None:
    assert DSN is not None

    def factory():
        return psycopg.connect(DSN)

    store = PostgresPipelineRunStore(factory)
    scheduler = PostgresPipelineScheduler(factory)
    authority = _source_catalog(tmp_path / "input")
    first_profile = _execution_profile(model_id="doubao-model-v1")
    changed_profile = _execution_profile(model_id="doubao-model-v2")
    first_service = DurablePipelineRunService(
        store,
        scheduler,
        authority,
        execution_profile=first_profile,
    )
    changed_service = DurablePipelineRunService(
        PostgresPipelineRunStore(factory),
        PostgresPipelineScheduler(factory),
        authority,
        execution_profile=changed_profile,
    )
    request = PipelineRunRequest("test", source_root=str(tmp_path / "input"))

    first = await first_service.submit(request, "postgres-profile-frozen")
    replay = await changed_service.submit(request, "postgres-profile-frozen")
    changed = await changed_service.submit(request, "postgres-profile-new")

    assert replay.replayed is True
    assert replay.snapshot.execution_profile == first_profile
    assert replay.snapshot.execution_profile_hash == first_profile.canonical_hash
    assert changed.snapshot.execution_profile == changed_profile
    assert changed.snapshot.execution_profile_hash == changed_profile.canonical_hash
    assert first.snapshot.request_hash == changed.snapshot.request_hash
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT execution_profile, execution_profile_hash
                  FROM runtime.pipeline_runs WHERE run_id = %s
                """,
                (first.snapshot.run_id,),
            )
            persisted = cursor.fetchone()
            assert persisted is not None
            assert persisted[0] == first_profile.to_mapping()
            persisted_hash = (
                persisted[1].decode() if isinstance(persisted[1], bytes) else persisted[1]
            )
            assert persisted_hash == first_profile.canonical_hash


@pytest.mark.asyncio
async def test_postgres_read_recomputes_execution_profile_hash(tmp_path: Path) -> None:
    assert DSN is not None
    service, store, _scheduler = _composition(tmp_path)
    submitted = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "input")),
        "postgres-profile-hash-tamper",
    )

    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE runtime.pipeline_runs DISABLE TRIGGER runtime_pipeline_run_transition_guard"
            )
            try:
                cursor.execute(
                    """
                    UPDATE runtime.pipeline_runs
                       SET execution_profile_hash = %s
                     WHERE run_id = %s
                    """,
                    ("sha256:" + "0" * 64, submitted.snapshot.run_id),
                )
            finally:
                cursor.execute(
                    "ALTER TABLE runtime.pipeline_runs ENABLE TRIGGER runtime_pipeline_run_transition_guard"
                )

    with pytest.raises(PipelineRunValidationError, match="hash does not bind"):
        await store.read_run(submitted.snapshot.run_id)


@pytest.mark.asyncio
async def test_postgres_idempotency_conflict_and_resume_cas(tmp_path: Path) -> None:
    service, _store, scheduler = _composition(tmp_path, tmp_path / "different")
    first_request = PipelineRunRequest.from_mapping(
        {"profile": "shadow", "source_reference": "source:fixture-1"}
    )
    first = await service.submit(first_request, "postgres-request-1")

    with pytest.raises(IdempotencyConflictError):
        await service.submit(
            PipelineRunRequest.from_mapping(
                {"profile": "test", "source_root": str(tmp_path / "different")}
            ),
            "postgres-request-1",
        )

    resumed = await service.resume(first.snapshot.run_id, expected_version=0)
    assert resumed.run_id == first.snapshot.run_id
    assert resumed.version == 1
    assert resumed.commands[0].status == "pending"
    with pytest.raises(StaleRunVersionError):
        await service.resume(first.snapshot.run_id, expected_version=0)
    assert await scheduler.pending_run_ids() == (first.snapshot.run_id,)


@pytest.mark.asyncio
async def test_source_authority_denies_before_postgres_claim(tmp_path: Path) -> None:
    service, store, _scheduler = _composition(tmp_path)

    with pytest.raises(SourceDeniedError):
        await service.submit(
            PipelineRunRequest.from_mapping(
                {"profile": "test", "source_root": str(tmp_path.parent / "outside")}
            ),
            "postgres-request-1",
        )

    assert await store.list_reconstructible_runs() == ()


@pytest.mark.asyncio
async def test_leased_result_and_receipt_are_one_cas_projection(tmp_path: Path) -> None:
    service, store, _scheduler = _composition(tmp_path)
    submitted = await service.submit(
        PipelineRunRequest.from_mapping(
            {"profile": "test", "source_root": str(tmp_path / "input")}
        ),
        "postgres-request-1",
    )
    run_id = submitted.snapshot.run_id
    command = await store.claim_next_pending(
        run_id,
        expected_version=0,
        lease_id="worker-lease-1",
    )
    assert command is not None
    receipt_id = uuid4()

    await store.record_result(
        run_id,
        result=PipelineStageResult(command.command_id, "succeeded", receipt_id),
        expected_version=1,
        lease_id="worker-lease-1",
    )

    restarted = PostgresPipelineRunStore(store.connection_factory)
    projected = await restarted.read_run(run_id)
    assert projected is not None
    assert projected.status == "running"
    assert projected.commands[0].status == "succeeded"
    assert projected.commands[0].receipt_id == receipt_id
    assert projected.commands[1].status == "pending"
    vlm = await restarted.claim_next_pending(
        run_id,
        expected_version=0,
        lease_id="worker-lease-2",
    )
    assert vlm is not None and vlm.stage == "vlm"
    vlm_receipt_id = uuid4()
    await restarted.record_result(
        run_id,
        result=PipelineStageResult(vlm.command_id, "succeeded", vlm_receipt_id),
        expected_version=vlm.version,
        lease_id="worker-lease-2",
    )
    completed = await restarted.read_run(run_id)
    assert completed is not None and completed.status == "succeeded"
    with pytest.raises(StaleRunVersionError):
        await restarted.record_result(
            run_id,
            result=PipelineStageResult(command.command_id, "succeeded", receipt_id),
            expected_version=1,
            lease_id="worker-lease-1",
        )


@pytest.mark.asyncio
async def test_predecessor_denial_atomically_blocks_vlm_and_terminates_run(
    tmp_path: Path,
) -> None:
    service, store, _scheduler = _composition(tmp_path)
    submitted = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "input")),
        "postgres-denied-1",
    )
    source = await store.claim_next_pending(
        submitted.snapshot.run_id,
        expected_version=0,
        lease_id="source-lease",
    )
    assert source is not None
    receipt_id = uuid4()
    await store.record_result(
        submitted.snapshot.run_id,
        result=PipelineStageResult(source.command_id, "denied", receipt_id),
        expected_version=source.version,
        lease_id="source-lease",
    )

    snapshot = await store.read_run(submitted.snapshot.run_id)
    assert snapshot is not None and snapshot.status == "denied"
    assert snapshot.commands[0].status == "denied"
    assert snapshot.commands[1].status == "blocked"
    assert snapshot.commands[1].receipt_id is None
    assert snapshot.commands[1].blocking_command_id == source.command_id
    assert (
        await store.claim_next_pending(
            snapshot.run_id,
            expected_version=snapshot.commands[1].version,
            lease_id="forbidden-vlm-lease",
        )
        is None
    )


@pytest.mark.asyncio
async def test_blocked_command_rejects_cross_run_blocker(tmp_path: Path) -> None:
    assert DSN is not None
    service, store, _scheduler = _composition(
        tmp_path,
        tmp_path / "denied",
        tmp_path / "target",
    )
    denied_run = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "denied")),
        "postgres-blocker-cross-a",
    )
    source = await store.claim_next_pending(
        denied_run.snapshot.run_id,
        expected_version=0,
        lease_id="cross-source",
    )
    assert source is not None
    await store.record_result(
        denied_run.snapshot.run_id,
        result=PipelineStageResult(source.command_id, "denied", uuid4()),
        expected_version=source.version,
        lease_id="cross-source",
    )
    target_run = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "target")),
        "postgres-blocker-cross-b",
    )
    target_vlm = target_run.snapshot.commands[1]

    with pytest.raises(psycopg.DatabaseError, match="earlier denied/failed"):
        with psycopg.connect(DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE runtime.pipeline_commands
                       SET state = 'blocked', version = version + 1,
                           blocking_command_id = %s,
                           completed_at = transaction_timestamp(),
                           updated_at = transaction_timestamp()
                     WHERE command_id = %s
                    """,
                    (source.command_id, target_vlm.command_id),
                )


@pytest.mark.asyncio
async def test_blocked_command_rejects_later_failed_blocker(tmp_path: Path) -> None:
    assert DSN is not None
    service, _store, _scheduler = _composition(tmp_path, tmp_path / "later")
    submitted = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "later")),
        "postgres-blocker-later",
    )
    source, vlm = submitted.snapshot.commands

    with pytest.raises(psycopg.DatabaseError, match="earlier denied/failed"):
        with psycopg.connect(DSN) as connection:
            with connection.cursor() as cursor:
                _force_terminal_command(cursor, vlm.command_id, "failed")
                cursor.execute(
                    """
                    UPDATE runtime.pipeline_commands
                       SET state = 'blocked', version = version + 1,
                           blocking_command_id = %s,
                           completed_at = transaction_timestamp(),
                           updated_at = transaction_timestamp()
                     WHERE command_id = %s
                    """,
                    (vlm.command_id, source.command_id),
                )


@pytest.mark.asyncio
async def test_blocked_command_rejects_non_failure_predecessor(tmp_path: Path) -> None:
    assert DSN is not None
    service, _store, _scheduler = _composition(tmp_path, tmp_path / "success")
    submitted = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "success")),
        "postgres-blocker-success",
    )
    source, vlm = submitted.snapshot.commands

    with pytest.raises(psycopg.DatabaseError, match="earlier denied/failed"):
        with psycopg.connect(DSN) as connection:
            with connection.cursor() as cursor:
                _force_terminal_command(cursor, source.command_id, "succeeded")
                cursor.execute(
                    """
                    UPDATE runtime.pipeline_commands
                       SET state = 'blocked', version = version + 1,
                           blocking_command_id = %s,
                           completed_at = transaction_timestamp(),
                           updated_at = transaction_timestamp()
                     WHERE command_id = %s
                    """,
                    (source.command_id, vlm.command_id),
                )


@pytest.mark.asyncio
async def test_outbox_lease_ack_requeue_and_command_heartbeat(tmp_path: Path) -> None:
    service, store, scheduler = _composition(tmp_path)
    submitted = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "input")),
        "postgres-worker-1",
    )
    outbox = await scheduler.claim_next(lease_id="outbox-1")
    assert outbox is not None and outbox.run_id == submitted.snapshot.run_id
    assert await scheduler.claim_next(lease_id="outbox-2") is None
    await scheduler.requeue(outbox)
    replay = await scheduler.claim_next(lease_id="outbox-3")
    assert replay is not None and replay.version > outbox.version
    renewed_outbox = await scheduler.renew(replay)
    assert renewed_outbox.version == replay.version + 1

    command = await store.claim_next_pending(
        submitted.snapshot.run_id,
        expected_version=0,
        lease_id="command-1",
    )
    assert command is not None
    renewed = await store.renew_running_lease(
        submitted.snapshot.run_id,
        command_id=command.command_id,
        expected_version=command.version,
        lease_id="command-1",
    )
    assert renewed.version == command.version + 1
    await scheduler.acknowledge(renewed_outbox)
    assert await scheduler.pending_run_ids() == ()


@pytest.mark.asyncio
async def test_expired_outbox_lease_is_reclaimed_with_a_new_cas_version(
    tmp_path: Path,
) -> None:
    assert DSN is not None

    def factory():
        return psycopg.connect(DSN)

    store = PostgresPipelineRunStore(factory)
    scheduler = PostgresPipelineScheduler(factory, lease_seconds=1)
    service = DurablePipelineRunService(
        store,
        scheduler,
        _source_catalog(tmp_path / "input"),
    )
    submitted = await service.submit(
        PipelineRunRequest("test", source_root=str(tmp_path / "input")),
        "postgres-expired-outbox-1",
    )
    expired = await scheduler.claim_next(lease_id="expired-owner")
    assert expired is not None and expired.run_id == submitted.snapshot.run_id
    await asyncio.sleep(1.05)

    reclaimed = await scheduler.claim_next(lease_id="replacement-owner")

    assert reclaimed is not None
    assert reclaimed.outbox_id == expired.outbox_id
    assert reclaimed.version == expired.version + 2
    await scheduler.requeue(reclaimed)


class _RecoveredStage:
    def __init__(self) -> None:
        self.calls = 0
        self.receipt_id = uuid4()

    async def reconcile(self, context: PipelineStageContext) -> PipelineStageResult:
        self.calls += 1
        return PipelineStageResult(context.command.command_id, "succeeded", self.receipt_id)


@pytest.mark.asyncio
async def test_expired_lease_resumes_only_through_reconciler(tmp_path: Path) -> None:
    assert DSN is not None

    def factory():
        return psycopg.connect(DSN)

    store = PostgresPipelineRunStore(factory, lease_seconds=1)
    scheduler = PostgresPipelineScheduler(factory)
    service = DurablePipelineRunService(
        store,
        scheduler,
        _source_catalog(tmp_path / "input"),
    )
    submitted = await service.submit(
        PipelineRunRequest.from_mapping(
            {"profile": "test", "source_root": str(tmp_path / "input")}
        ),
        "postgres-request-1",
    )
    run_id = submitted.snapshot.run_id
    claimed = await store.claim_next_pending(
        run_id,
        expected_version=0,
        lease_id="crashed-worker-lease",
    )
    assert claimed is not None
    await asyncio.sleep(1.05)

    with pytest.raises(StaleRunVersionError):
        await store.record_result(
            run_id,
            result=PipelineStageResult(claimed.command_id, "succeeded", uuid4()),
            expected_version=1,
            lease_id="crashed-worker-lease",
        )
    expired = await store.expire_running_lease(
        run_id,
        expected_version=1,
        lease_id="crashed-worker-lease",
    )
    assert expired.status == "indeterminate"
    assert expired.version == 2
    with pytest.raises(StaleRunVersionError):
        await store.record_result(
            run_id,
            result=PipelineStageResult(claimed.command_id, "succeeded", uuid4()),
            expected_version=1,
            lease_id="crashed-worker-lease",
        )

    resumed = await service.resume(run_id, expected_version=2)
    assert resumed.commands[0].status == "indeterminate"
    assert resumed.version == 3
    assert await service.reconstruct() == (run_id,)

    recovered_stage = _RecoveredStage()
    reconciler = PipelineStageReconciler.from_ports(
        PostgresPipelineRunStore(factory),
        ("source_prep", recovered_stage),
    )
    snapshot = await store.read_run(run_id)
    assert snapshot is not None
    result = await reconciler.reconcile(snapshot)
    assert result is not None
    assert recovered_stage.calls == 1
    projected = await store.read_run(run_id)
    assert projected is not None
    assert projected.status == "running"
    assert projected.commands[0].receipt_id == recovered_stage.receipt_id
    assert projected.commands[1].status == "pending"


def test_0007_upgrades_active_0005_single_stage_runs_with_causal_vlm_state() -> None:
    assert DSN is not None
    migration_root = Path("packages/autocut-kernel/migrations")
    cases = (
        ("pending", "accepted", "pending", "accepted"),
        ("running", "running", "pending", "running"),
        ("succeeded", "running", "pending", "running"),
        ("denied", "running", "blocked", "denied"),
        ("failed", "accepted", "blocked", "failed"),
        ("succeeded", "succeeded", None, "succeeded"),
    )

    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            for migration in sorted(migration_root.glob("000[1-5]_*.sql")):
                cursor.execute(migration.read_text(encoding="utf-8"))

            identities: list[tuple[str, str, str, str | None, str]] = []
            for index, (
                source_state,
                initial_run_state,
                expected_vlm_state,
                expected_run_state,
            ) in enumerate(cases, start=1):
                run_id = f"pipeline_run_{index:032x}"
                command_id = str(uuid4())
                with connection.transaction():
                    cursor.execute(
                        """
                        INSERT INTO runtime.pipeline_runs
                            (run_id, idempotency_key, request_hash, profile,
                             source_kind, source_value, state, version)
                        VALUES (%s, %s, %s, 'test', 'root', %s, 'accepted', 0)
                        """,
                        (
                            run_id,
                            f"upgrade-{index}",
                            "sha256:" + f"{index:x}" * 64,
                            f"/upgrade/{index}",
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO runtime.pipeline_commands
                            (command_id, run_id, ordinal, stage, state, version)
                        VALUES (%s, %s, 0, 'source_prep', 'pending', 0)
                        """,
                        (command_id, run_id),
                    )
                    if source_state == "running":
                        cursor.execute(
                            """
                            UPDATE runtime.pipeline_commands
                               SET state = 'running', version = 1,
                                   lease_id = 'upgrade-running',
                                   lease_expires_at = transaction_timestamp()
                                       + interval '1 hour',
                                   updated_at = transaction_timestamp()
                             WHERE command_id = %s
                            """,
                            (command_id,),
                        )
                    elif source_state in ("succeeded", "denied", "failed"):
                        _force_terminal_command(cursor, command_id, source_state)
                    if initial_run_state != "accepted":
                        cursor.execute(
                            """
                            UPDATE runtime.pipeline_runs
                               SET state = %s, version = version + 1,
                                   updated_at = transaction_timestamp()
                             WHERE run_id = %s
                            """,
                            (initial_run_state, run_id),
                        )
                identities.append(
                    (
                        run_id,
                        command_id,
                        source_state,
                        expected_vlm_state,
                        expected_run_state,
                    )
                )

            cursor.execute(
                (migration_root / "0007_pipeline_stage_worker.sql").read_text(
                    encoding="utf-8"
                )
            )
            for (
                run_id,
                source_command_id,
                source_state,
                expected_vlm_state,
                expected_run_state,
            ) in identities:
                cursor.execute(
                    """
                    SELECT state FROM runtime.pipeline_runs WHERE run_id = %s
                    """,
                    (run_id,),
                )
                assert cursor.fetchone() == (expected_run_state,)
                cursor.execute(
                    """
                    SELECT state, blocking_command_id
                      FROM runtime.pipeline_commands
                     WHERE run_id = %s AND ordinal = 1
                    """,
                    (run_id,),
                )
                vlm = cursor.fetchone()
                if expected_vlm_state is None:
                    assert vlm is None
                else:
                    assert vlm is not None
                    assert vlm[0] == expected_vlm_state
                    assert (
                        str(vlm[1]) if vlm[1] is not None else None
                    ) == (
                        source_command_id
                        if source_state in ("denied", "failed")
                        else None
                    )


@pytest.mark.asyncio
async def test_0008_aborts_on_old_active_runs_and_marks_only_terminal_history() -> None:
    assert DSN is not None
    migration_root = Path("packages/autocut-kernel/migrations")
    active_request = PipelineRunRequest("test", source_root="/legacy/active")
    terminal_request = PipelineRunRequest("test", source_root="/legacy/terminal")
    active_run_id = "pipeline_run_" + "a" * 32
    terminal_run_id = "pipeline_run_" + "b" * 32

    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            for migration in sorted(migration_root.glob("000[1-6]_*.sql")):
                cursor.execute(migration.read_text(encoding="utf-8"))
            for run_id, idempotency_key, request in (
                (active_run_id, "legacy-active", active_request),
                (terminal_run_id, "legacy-terminal", terminal_request),
            ):
                command_id = str(uuid4())
                with connection.transaction():
                    cursor.execute(
                        """
                        INSERT INTO runtime.pipeline_runs
                            (run_id, idempotency_key, request_hash, profile,
                             source_kind, source_value, state, version)
                        VALUES (%s, %s, %s, 'test', 'root', %s, 'accepted', 0)
                        """,
                        (run_id, idempotency_key, request.request_hash, request.source_root),
                    )
                    cursor.execute(
                        """
                        INSERT INTO runtime.pipeline_commands
                            (command_id, run_id, ordinal, stage, state, version)
                        VALUES (%s, %s, 0, 'source_prep', 'pending', 0)
                        """,
                        (command_id, run_id),
                    )
                    _force_terminal_command(cursor, command_id, "succeeded")
                    cursor.execute(
                        """
                        UPDATE runtime.pipeline_runs
                           SET state = %s, version = version + 1,
                               updated_at = transaction_timestamp()
                         WHERE run_id = %s
                        """,
                        (
                            "running" if run_id == active_run_id else "succeeded",
                            run_id,
                        ),
                    )
            cursor.execute(
                (migration_root / "0007_pipeline_stage_worker.sql").read_text(
                    encoding="utf-8"
                )
            )
            profile_migration = (
                migration_root / "0008_pipeline_execution_profile.sql"
            ).read_text(encoding="utf-8")
            with pytest.raises(
                psycopg.DatabaseError,
                match="refuses legacy accepted/running pipeline runs",
            ):
                cursor.execute(profile_migration)
            connection.rollback()
            cursor.execute(
                """
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = 'runtime'
                   AND table_name = 'pipeline_runs'
                   AND column_name = 'execution_profile'
                """
            )
            assert cursor.fetchone() is None

            cursor.execute(
                """
                SELECT command_id FROM runtime.pipeline_commands
                 WHERE run_id = %s AND stage = 'vlm' AND state = 'pending'
                """,
                (active_run_id,),
            )
            active_vlm = cursor.fetchone()
            assert active_vlm is not None
            with connection.transaction():
                _force_terminal_command(cursor, str(active_vlm[0]), "failed")
                cursor.execute(
                    """
                    UPDATE runtime.pipeline_runs
                       SET state = 'failed', version = version + 1,
                           updated_at = transaction_timestamp()
                     WHERE run_id = %s AND state = 'running'
                    """,
                    (active_run_id,),
                )
            cursor.execute(profile_migration)

            for index, malformed_profile in enumerate(
                (
                    "{}",
                    '{"kind":"legacy_unresolved"}',
                    '{"kind":null,"schema_version":null}',
                )
            ):
                with pytest.raises(
                    psycopg.errors.CheckViolation,
                    match="pipeline_runs_execution_profile_closed_check",
                ):
                    cursor.execute(
                        """
                        INSERT INTO runtime.pipeline_runs
                            (run_id, idempotency_key, request_hash, profile,
                             source_kind, source_value, state, version,
                             execution_profile, execution_profile_hash)
                        VALUES (%s, %s, %s, 'test', 'root', %s, 'accepted', 0,
                                %s::jsonb, %s)
                        """,
                        (
                            "pipeline_run_" + str(index + 1) * 32,
                            f"malformed-profile-{index}",
                            active_request.request_hash,
                            f"/malformed/{index}",
                            malformed_profile,
                            "sha256:" + "0" * 64,
                        ),
                    )

    def factory():
        return psycopg.connect(DSN)

    store = PostgresPipelineRunStore(factory)
    failed_history = await store.read_run(active_run_id)
    succeeded_history = await store.read_run(terminal_run_id)

    assert failed_history is not None and failed_history.status == "failed"
    assert failed_history.execution_profile.is_legacy_unresolved
    assert tuple(command.stage for command in failed_history.commands) == (
        "source_prep",
        "vlm",
    )
    assert failed_history.commands[1].status == "failed"
    assert succeeded_history is not None and succeeded_history.status == "succeeded"
    assert succeeded_history.execution_profile.is_legacy_unresolved
    assert await store.list_reconstructible_runs() == ()


def _agent() -> MagicMock:
    agent = MagicMock()
    agent.process_direct = AsyncMock(return_value="unused")
    return agent


@pytest.mark.asyncio
async def test_real_http_run_status_resume_survive_app_restart(
    tmp_path: Path,
) -> None:
    assert DSN is not None
    first_service, _, _ = _composition(tmp_path)
    headers = {"Idempotency-Key": "http-postgres-request-1"}
    payload = {"profile": "test", "source_root": str(tmp_path / "input")}

    first_client = TestClient(
        TestServer(create_app(_agent(), pipeline_run_service=first_service))
    )
    await first_client.start_server()
    created = await first_client.post("/v1/pipeline/run", headers=headers, json=payload)
    assert created.status == 202
    created_body = await created.json()
    run_id = created_body["run_id"]
    await first_client.close()

    restarted_service, _, _ = _composition(tmp_path)
    restarted_client = TestClient(
        TestServer(create_app(_agent(), pipeline_run_service=restarted_service))
    )
    await restarted_client.start_server()
    replay = await restarted_client.post("/v1/pipeline/run", headers=headers, json=payload)
    status = await restarted_client.get("/v1/pipeline/status", params={"run_id": run_id})
    resumed = await restarted_client.post(
        "/v1/pipeline/resume",
        json={"run_id": run_id, "expected_version": 0},
    )
    try:
        assert replay.status == 202
        assert (await replay.json())["run_id"] == run_id
        assert (await replay.json())["replayed"] is True
        assert status.status == 200
        status_body = await status.json()
        assert status_body["commands"] == [
            {
                "command_id": status_body["commands"][0]["command_id"],
                "stage": "source_prep",
                "status": "pending",
                "receipt_id": None,
                "version": 0,
                "lease_id": None,
                "blocking_command_id": None,
            },
            {
                "command_id": status_body["commands"][1]["command_id"],
                "stage": "vlm",
                "status": "pending",
                "receipt_id": None,
                "version": 0,
                "lease_id": None,
                "blocking_command_id": None,
            }
        ]
        assert resumed.status == 202
        assert (await resumed.json())["version"] == 1
    finally:
        await restarted_client.close()
