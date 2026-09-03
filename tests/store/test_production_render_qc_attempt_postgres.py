"""Disposable PostgreSQL acceptance for production render QC fencing."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from autocut_kernel.pipeline.compile_production_recipe_command import (
    CompileProductionRecipeCommand,
)
from autocut_kernel.store import (
    CommandRejection,
    CommandStateError,
    CommittedArtifactMemberReference,
    IdempotencyConflictError,
    Job,
    PostgresRuntimeStore,
    ProductionRenderAttempt,
    ProductionRenderQcLease,
    RuntimeStoreError,
    StoreValidationError,
)

from tests.authority.editorial_media_fixture import editorial_timed_media_case
from tests.pipeline.test_compile_production_recipe_command import (
    _install_non_dialogue_blueprint_projection,
    _request,
)
from tests.store.test_production_render_attempt_postgres import (
    _external_blob,
    _facts,
    _hash,
    _reserve,
    _Stage4BridgeStore,
)

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set AUTOCUT_TEST_POSTGRES_DSN to disposable ac_autocut_verify",
)
MIGRATIONS = Path("packages/autocut-kernel/migrations")


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        if connection.info.dbname != "ac_autocut_verify":
            pytest.fail("render QC attempt tests may reset only ac_autocut_verify")
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            migrations = sorted(MIGRATIONS.glob("*.sql"))
            assert any(
                migration.name == "0057_production_render_qc_attempts.sql"
                for migration in migrations
            )
            for migration in migrations:
                cursor.execute(migration.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def qc_stage4_authority(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[PostgresRuntimeStore, Job, CommittedArtifactMemberReference]:
    patcher = pytest.MonkeyPatch()
    _install_non_dialogue_blueprint_projection(patcher)
    try:
        case = editorial_timed_media_case(
            tmp_path_factory.mktemp("render-qc-stage4-authority"),
            patcher,
        )
        predecessor, *_rest, resolver, limits = case
        durable = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
        request = _request(case)
        result = CompileProductionRecipeCommand(
            _Stage4BridgeStore(predecessor, durable),
            resolver,
            limits,
        ).execute(request)
        assert result.outcome.state == "succeeded"
        assert result.committed is not None
        return durable, request.job, result.committed.record.members[1].reference
    finally:
        patcher.undo()


def _rendered_parent(
    store: PostgresRuntimeStore,
    job: Job,
    recipe: CommittedArtifactMemberReference,
    *,
    suffix: str,
) -> ProductionRenderAttempt:
    attempt = _reserve(store, job, recipe, suffix=suffix)
    lease = store.acquire_production_render_lease(
        attempt.attempt_id,
        expected_version=attempt.version,
        lease_seconds=60,
    )
    assert lease is not None
    output = _external_blob(job, ("qc-output:" + suffix).encode())
    return store.record_production_render_output(
        lease,
        output_blob=output,
        facts=_facts(attempt, job, output),
    )


def _reserve_qc(
    store: PostgresRuntimeStore,
    parent: ProductionRenderAttempt,
    *,
    policy: str = "policy",
    check_set: str = "full-file-v1",
    runner: str = "runner",
):
    return store.reserve_production_render_qc_attempt(
        parent.attempt_id,
        expected_render_version=parent.version,
        qc_policy_sha256=_hash(policy.encode()),
        required_check_set_version=check_set,
        qc_runner_identity_sha256=_hash(runner.encode()),
    )


def _rejection(
    parent: ProductionRenderAttempt,
    outcome: Literal["denied", "failed"],
) -> CommandRejection:
    return CommandRejection(
        parent.command_slot_id,
        "PUBLICATION_QC_DENIED" if outcome == "denied" else "QC_EXECUTION_FAILED",
        '{"reason":"QC journal must resolve atomically"}',
        outcome,
    )


def _assert_render_command_open_without_receipt(parent: ProductionRenderAttempt) -> None:
    assert DSN is not None
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT state FROM runtime.command_slots WHERE command_slot_id = %s",
            (parent.command_slot_id,),
        )
        assert cursor.fetchone() == ("running",)
        cursor.execute(
            "SELECT count(*) FROM runtime.command_receipts "
            "WHERE command_slot_id = %s",
            (parent.command_slot_id,),
        )
        assert cursor.fetchone() == (0,)


def _wait_for_application_lock(application_name: str) -> None:
    assert DSN is not None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with psycopg.connect(DSN, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT wait_event_type FROM pg_stat_activity "
                    "WHERE application_name = %s",
                    (application_name,),
                )
                if cursor.fetchone() == ("Lock",):
                    return
        time.sleep(0.01)
    pytest.fail(f"{application_name} did not reach its PostgreSQL lock")


def _wait_for_qc_lease_expiry(qc_attempt_id: object) -> None:
    assert DSN is not None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with psycopg.connect(DSN, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT lease_expires_at <= clock_timestamp() "
                    "FROM runtime.production_render_qc_attempts "
                    "WHERE qc_attempt_id = %s",
                    (qc_attempt_id,),
                )
                if cursor.fetchone() == (True,):
                    return
        time.sleep(0.01)
    pytest.fail("production render QC lease did not expire by PostgreSQL time")


def _qc_database_row(qc_attempt_id: object) -> tuple[object, ...]:
    assert DSN is not None
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT qc_attempt_id, render_attempt_id, job_id, command_slot_id, "
            "rendered_version, output_object_id, render_facts_sha256, "
            "qc_policy_sha256, required_check_set_version, "
            "qc_runner_identity_sha256, state, version, lease_token, "
            "lease_expires_at, reserved_at "
            "FROM runtime.production_render_qc_attempts WHERE qc_attempt_id = %s",
            (qc_attempt_id,),
        )
        row = cursor.fetchone()
        assert row is not None
        assert cursor.fetchone() is None
        return tuple(row)


def _assert_qc_transition_guard_enabled() -> None:
    assert DSN is not None
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgenabled FROM pg_trigger "
            "WHERE tgrelid = 'runtime.production_render_qc_attempts'::regclass "
            "AND tgname = 'runtime_production_render_qc_attempt_transition_guard' "
            "AND NOT tgisinternal",
        )
        assert cursor.fetchone() == ("O",)
        assert cursor.fetchone() is None


def test_reservation_derives_exact_parent_identity_and_replays(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    store, job, recipe = qc_stage4_authority
    parent = _rendered_parent(store, job, recipe, suffix="qc-reservation")
    attempt = _reserve_qc(store, parent)
    replay = _reserve_qc(store, parent)

    assert attempt.is_fresh_reservation
    assert not replay.is_fresh_reservation
    assert replay == attempt
    assert attempt.render_attempt_id == parent.attempt_id
    assert attempt.job_id == parent.job_id
    assert attempt.command_slot_id == parent.command_slot_id
    assert attempt.rendered_version == parent.version
    assert attempt.output_blob == parent.output_blob
    assert attempt.render_facts_sha256 == parent.render_facts_sha256
    assert store.read_production_render_qc_attempt_for_render(parent.attempt_id) == attempt

    with pytest.raises(IdempotencyConflictError, match="different identity"):
        _reserve_qc(store, parent, policy="different-policy")
    with pytest.raises(IdempotencyConflictError, match="different identity"):
        _reserve_qc(store, parent, check_set="full-file-v2")
    with pytest.raises(IdempotencyConflictError, match="different identity"):
        _reserve_qc(store, parent, runner="different-runner")


def test_reservation_rejects_non_rendered_and_stale_parent(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    store, job, recipe = qc_stage4_authority
    reserved = _reserve(store, job, recipe, suffix="qc-not-rendered")
    with pytest.raises(CommandStateError, match="rendered parent"):
        _reserve_qc(store, reserved)

    rendered = _rendered_parent(store, job, recipe, suffix="qc-stale-render")
    with pytest.raises(CommandStateError, match="parent version is stale"):
        store.reserve_production_render_qc_attempt(
            rendered.attempt_id,
            expected_render_version=rendered.version - 1,
            qc_policy_sha256=_hash(b"policy"),
            required_check_set_version="full-file-v1",
            qc_runner_identity_sha256=_hash(b"runner"),
        )


@pytest.mark.parametrize("qc_state", ("reserved", "scanning"))
@pytest.mark.parametrize("terminal_state", ("committed", "denied", "failed"))
def test_qc_journal_blocks_every_parent_terminalization_and_rolls_back_atomically(
    qc_state: Literal["reserved", "scanning"],
    terminal_state: Literal["committed", "denied", "failed"],
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    assert DSN is not None
    store, job, recipe = qc_stage4_authority
    suffix = f"qc-terminal-barrier-{qc_state}-{terminal_state}"
    parent = _rendered_parent(store, job, recipe, suffix=suffix)
    qc_attempt = _reserve_qc(store, parent)
    if qc_state == "scanning":
        lease = store.acquire_production_render_qc_lease(
            qc_attempt.qc_attempt_id,
            expected_version=qc_attempt.version,
            lease_seconds=60,
        )
        assert lease is not None
        qc_attempt = store.read_production_render_qc_attempt(qc_attempt.qc_attempt_id)

    if terminal_state == "committed":
        with pytest.raises(psycopg.Error, match="active QC journal"):
            with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE runtime.production_render_attempts "
                    "SET state = 'committed', version = version + 1, "
                    "receipt_id = %s, artifact_set_id = %s, "
                    "completed_at = clock_timestamp() WHERE attempt_id = %s",
                    (uuid4(), uuid4(), parent.attempt_id),
                )
    else:
        with pytest.raises(RuntimeStoreError, match="database operation failed"):
            store.commit_production_render_rejection(
                parent.attempt_id,
                expected_version=parent.version,
                rejection=_rejection(parent, terminal_state),
            )

    assert store.read_production_render_attempt(parent.attempt_id) == parent
    assert (
        store.read_production_render_qc_attempt(qc_attempt.qc_attempt_id)
        == qc_attempt
    )
    _assert_render_command_open_without_receipt(parent)


@pytest.mark.parametrize("first_operation", ("reserve_qc", "terminalize"))
def test_qc_reservation_and_parent_terminalization_serialize_without_bypass(
    first_operation: Literal["reserve_qc", "terminalize"],
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    assert DSN is not None
    store, job, recipe = qc_stage4_authority
    parent = _rendered_parent(
        store,
        job,
        recipe,
        suffix=f"qc-terminal-race-{first_operation}",
    )
    reservation_application = f"qc-reservation-{uuid4()}"
    terminal_application = f"qc-terminal-{uuid4()}"
    reservation_store = PostgresRuntimeStore(
        lambda: psycopg.connect(DSN, application_name=reservation_application)
    )
    terminal_store = PostgresRuntimeStore(
        lambda: psycopg.connect(DSN, application_name=terminal_application)
    )

    executor = ThreadPoolExecutor(max_workers=2)
    try:
        with psycopg.connect(DSN) as blocker, blocker.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM runtime.jobs WHERE job_id = %s FOR UPDATE",
                (parent.job_id,),
            )
            assert cursor.fetchone() is not None
            if first_operation == "reserve_qc":
                reserve_future = executor.submit(_reserve_qc, reservation_store, parent)
                _wait_for_application_lock(reservation_application)
                terminal_future = executor.submit(
                    terminal_store.commit_production_render_rejection,
                    parent.attempt_id,
                    expected_version=parent.version,
                    rejection=_rejection(parent, "denied"),
                )
                _wait_for_application_lock(terminal_application)
            else:
                terminal_future = executor.submit(
                    terminal_store.commit_production_render_rejection,
                    parent.attempt_id,
                    expected_version=parent.version,
                    rejection=_rejection(parent, "denied"),
                )
                _wait_for_application_lock(terminal_application)
                reserve_future = executor.submit(_reserve_qc, reservation_store, parent)
                _wait_for_application_lock(reservation_application)

        if first_operation == "reserve_qc":
            qc_attempt = reserve_future.result(timeout=5)
            with pytest.raises(RuntimeStoreError, match="database operation failed"):
                terminal_future.result(timeout=5)
            assert store.read_production_render_attempt(parent.attempt_id) == parent
            assert (
                store.read_production_render_qc_attempt_for_render(parent.attempt_id)
                == qc_attempt
            )
            _assert_render_command_open_without_receipt(parent)
        else:
            terminal = terminal_future.result(timeout=5)
            assert terminal.state == "denied"
            with pytest.raises(CommandStateError, match="cannot reserve"):
                reserve_future.result(timeout=5)
            assert (
                store.read_production_render_qc_attempt_for_render(parent.attempt_id)
                is None
            )
            closed_parent = store.read_production_render_attempt(parent.attempt_id)
            assert closed_parent.state == "denied"
            assert closed_parent.receipt_id == terminal.receipt_id
    finally:
        executor.shutdown(wait=True)


@pytest.mark.parametrize(
    ("mutation", "error_match"),
    (
        ("identity", "identity is immutable"),
        ("version", "exact version increment"),
        ("delete", "durable and cannot be deleted"),
        ("reserved_to_reserved", "invalid production render QC attempt state transition"),
    ),
)
def test_qc_transition_guard_rejects_illegal_reserved_row_mutation(
    mutation: Literal["identity", "version", "delete", "reserved_to_reserved"],
    error_match: str,
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    assert DSN is not None
    store, job, recipe = qc_stage4_authority
    parent = _rendered_parent(store, job, recipe, suffix=f"qc-guard-{mutation}")
    attempt = _reserve_qc(store, parent)
    before = _qc_database_row(attempt.qc_attempt_id)
    _assert_qc_transition_guard_enabled()

    with pytest.raises(psycopg.Error, match=error_match):
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            if mutation == "identity":
                cursor.execute(
                    "UPDATE runtime.production_render_qc_attempts "
                    "SET qc_policy_sha256 = %s, version = version + 1 "
                    "WHERE qc_attempt_id = %s",
                    (_hash(b"mutated-qc-policy"), attempt.qc_attempt_id),
                )
            elif mutation == "version":
                cursor.execute(
                    "UPDATE runtime.production_render_qc_attempts "
                    "SET version = version + 2 WHERE qc_attempt_id = %s",
                    (attempt.qc_attempt_id,),
                )
            elif mutation == "delete":
                cursor.execute(
                    "DELETE FROM runtime.production_render_qc_attempts "
                    "WHERE qc_attempt_id = %s",
                    (attempt.qc_attempt_id,),
                )
            else:
                cursor.execute(
                    "UPDATE runtime.production_render_qc_attempts "
                    "SET version = version + 1 WHERE qc_attempt_id = %s",
                    (attempt.qc_attempt_id,),
                )

    _assert_qc_transition_guard_enabled()
    assert _qc_database_row(attempt.qc_attempt_id) == before


def test_qc_transition_guard_rejects_same_token_renewal_after_expiry(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    assert DSN is not None
    store, job, recipe = qc_stage4_authority
    parent = _rendered_parent(store, job, recipe, suffix="qc-guard-expired-renewal")
    attempt = _reserve_qc(store, parent)
    lease = store.acquire_production_render_qc_lease(
        attempt.qc_attempt_id,
        expected_version=attempt.version,
        lease_seconds=1,
    )
    assert lease is not None
    _wait_for_qc_lease_expiry(attempt.qc_attempt_id)
    before = _qc_database_row(attempt.qc_attempt_id)
    _assert_qc_transition_guard_enabled()

    with pytest.raises(
        psycopg.Error,
        match="lease renewal requires an active lease and later expiry",
    ):
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE runtime.production_render_qc_attempts "
                "SET lease_expires_at = clock_timestamp() + make_interval(secs => 60), "
                "version = version + 1 WHERE qc_attempt_id = %s AND lease_token = %s",
                (attempt.qc_attempt_id, lease.token),
            )

    _assert_qc_transition_guard_enabled()
    assert _qc_database_row(attempt.qc_attempt_id) == before


def test_qc_transition_guard_rejects_active_lease_token_takeover(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    assert DSN is not None
    store, job, recipe = qc_stage4_authority
    parent = _rendered_parent(store, job, recipe, suffix="qc-guard-active-takeover")
    attempt = _reserve_qc(store, parent)
    lease = store.acquire_production_render_qc_lease(
        attempt.qc_attempt_id,
        expected_version=attempt.version,
        lease_seconds=60,
    )
    assert lease is not None
    before = _qc_database_row(attempt.qc_attempt_id)
    _assert_qc_transition_guard_enabled()

    with pytest.raises(psycopg.Error, match="active production render QC lease cannot be taken over"):
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE runtime.production_render_qc_attempts "
                "SET lease_token = %s, "
                "lease_expires_at = clock_timestamp() + make_interval(secs => 120), "
                "version = version + 1 WHERE qc_attempt_id = %s",
                (uuid4(), attempt.qc_attempt_id),
            )

    _assert_qc_transition_guard_enabled()
    assert _qc_database_row(attempt.qc_attempt_id) == before


@pytest.mark.parametrize(
    "check_set",
    ("", "Uppercase", "-leading", "contains/slash", "x" * 129),
)
def test_reservation_rejects_unsafe_check_set_version(
    check_set: str,
) -> None:
    store = _store_without_connection()
    with pytest.raises(StoreValidationError, match="safe lowercase"):
        store.reserve_production_render_qc_attempt(
            _fake_uuid(),
            expected_render_version=2,
            qc_policy_sha256=_hash(b"policy"),
            required_check_set_version=check_set,
            qc_runner_identity_sha256=_hash(b"runner"),
        )


def _store_without_connection() -> PostgresRuntimeStore:
    def unavailable():  # type: ignore[no-untyped-def]
        raise AssertionError("invalid input must fail before opening PostgreSQL")

    return PostgresRuntimeStore(unavailable)


def _fake_uuid():  # type: ignore[no-untyped-def]
    from uuid import UUID

    return UUID("00000000-0000-0000-0000-000000000001")


def test_concurrent_acquire_has_one_winner_and_active_lease_is_excluded(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    store, job, recipe = qc_stage4_authority
    parent = _rendered_parent(store, job, recipe, suffix="qc-concurrent")
    attempt = _reserve_qc(store, parent)

    def acquire() -> ProductionRenderQcLease | None:
        try:
            return store.acquire_production_render_qc_lease(
                attempt.qc_attempt_id,
                expected_version=attempt.version,
                lease_seconds=60,
            )
        except CommandStateError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result() for future in (executor.submit(acquire), executor.submit(acquire))
        ]

    leases = [result for result in results if result is not None]
    assert len(leases) == 1
    active = store.read_production_render_qc_attempt(attempt.qc_attempt_id)
    assert active.state == "scanning"
    assert active.version == 1
    assert not hasattr(active, "token")
    assert not hasattr(active, "lease_token")
    assert (
        store.acquire_production_render_qc_lease(
            attempt.qc_attempt_id,
            expected_version=active.version,
            lease_seconds=60,
        )
        is None
    )


def test_renew_expiry_takeover_and_old_token_fencing(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    store, job, recipe = qc_stage4_authority
    parent = _rendered_parent(store, job, recipe, suffix="qc-takeover")
    attempt = _reserve_qc(store, parent)
    first = store.acquire_production_render_qc_lease(
        attempt.qc_attempt_id,
        expected_version=attempt.version,
        lease_seconds=1,
    )
    assert first is not None
    renewed = store.renew_production_render_qc_lease(first, lease_seconds=2)
    assert renewed.version == first.version + 1
    assert renewed.token == first.token
    with pytest.raises(CommandStateError, match="stale|owned elsewhere"):
        store.renew_production_render_qc_lease(first, lease_seconds=60)

    _wait_for_qc_lease_expiry(attempt.qc_attempt_id)
    expired = store.read_production_render_qc_attempt(attempt.qc_attempt_id)
    takeover = store.acquire_production_render_qc_lease(
        attempt.qc_attempt_id,
        expected_version=expired.version,
        lease_seconds=60,
    )
    assert takeover is not None
    assert takeover.token != renewed.token
    with pytest.raises(CommandStateError, match="stale|owned elsewhere"):
        store.renew_production_render_qc_lease(renewed, lease_seconds=60)


def test_lock_wait_cannot_renew_after_database_time_expiry(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    assert DSN is not None
    store, job, recipe = qc_stage4_authority
    parent = _rendered_parent(store, job, recipe, suffix="qc-lock-wait")
    attempt = _reserve_qc(store, parent)
    lease = store.acquire_production_render_qc_lease(
        attempt.qc_attempt_id,
        expected_version=attempt.version,
        lease_seconds=1,
    )
    assert lease is not None

    renewal_application = f"qc-expiry-renewal-{uuid4()}"
    renewal_store = PostgresRuntimeStore(
        lambda: psycopg.connect(DSN, application_name=renewal_application)
    )
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with psycopg.connect(DSN) as blocker, blocker.cursor() as cursor:
            cursor.execute(
                "SELECT qc_attempt_id FROM runtime.production_render_qc_attempts "
                "WHERE qc_attempt_id = %s FOR UPDATE",
                (attempt.qc_attempt_id,),
            )
            future = executor.submit(
                renewal_store.renew_production_render_qc_lease,
                lease,
                lease_seconds=60,
            )
            _wait_for_application_lock(renewal_application)
            _wait_for_qc_lease_expiry(attempt.qc_attempt_id)
        with pytest.raises(CommandStateError, match="renewal CAS was lost"):
            future.result(timeout=5)
    finally:
        executor.shutdown(wait=True)


def test_reread_rejects_deleted_and_wrong_job_output_claim(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    assert DSN is not None
    store, job, recipe = qc_stage4_authority
    parent = _rendered_parent(store, job, recipe, suffix="qc-claim-tamper")
    attempt = _reserve_qc(store, parent)
    foreign_job_id = uuid4()

    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT blob_claim_id, claimed_at FROM storage.blob_claims "
                "WHERE object_id = %s AND job_id = %s",
                (attempt.output_blob.object_id, attempt.job_id),
            )
            claim_row = cursor.fetchone()
            assert claim_row is not None
            claim_id, claimed_at = claim_row
            cursor.execute(
                "INSERT INTO runtime.jobs (job_id, job_key, profile, state) "
                "VALUES (%s, %s, 'production', 'pending')",
                (foreign_job_id, f"qc-foreign-claim-{foreign_job_id}"),
            )
            cursor.execute("ALTER TABLE storage.blob_claims DISABLE TRIGGER USER")
            try:
                cursor.execute(
                    "DELETE FROM storage.blob_claims WHERE blob_claim_id = %s",
                    (claim_id,),
                )
                with pytest.raises(RuntimeStoreError, match="storage authority is invalid"):
                    store.read_production_render_qc_attempt(attempt.qc_attempt_id)

                cursor.execute(
                    "INSERT INTO storage.blob_claims "
                    "(blob_claim_id, object_id, job_id, claimed_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (
                        claim_id,
                        attempt.output_blob.object_id,
                        attempt.job_id,
                        claimed_at,
                    ),
                )
                cursor.execute(
                    "UPDATE storage.blob_claims SET job_id = %s "
                    "WHERE blob_claim_id = %s",
                    (foreign_job_id, claim_id),
                )
                with pytest.raises(RuntimeStoreError, match="storage authority is invalid"):
                    store.read_production_render_qc_attempt(attempt.qc_attempt_id)
            finally:
                cursor.execute(
                    "INSERT INTO storage.blob_claims "
                    "(blob_claim_id, object_id, job_id, claimed_at) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (blob_claim_id) DO UPDATE "
                    "SET object_id = EXCLUDED.object_id, job_id = EXCLUDED.job_id, "
                    "claimed_at = EXCLUDED.claimed_at",
                    (
                        claim_id,
                        attempt.output_blob.object_id,
                        attempt.job_id,
                        claimed_at,
                    ),
                )
                cursor.execute("ALTER TABLE storage.blob_claims ENABLE TRIGGER USER")
                cursor.execute("DELETE FROM runtime.jobs WHERE job_id = %s", (foreign_job_id,))

    assert store.read_production_render_qc_attempt(attempt.qc_attempt_id) == attempt


def test_reread_rejects_output_storage_metadata_tamper(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    assert DSN is not None
    store, job, recipe = qc_stage4_authority
    suffix = "qc-storage-tamper"
    output_bytes = ("qc-output:" + suffix).encode()
    parent = _rendered_parent(store, job, recipe, suffix=suffix)
    attempt = _reserve_qc(store, parent)

    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT content_hash, byte_length, media_type, content_bytes, "
                "storage_kind, storage_backend_id, storage_region, storage_locator, "
                "storage_etag, storage_version_id, write_strategy, verified_at "
                "FROM storage.blob_objects WHERE object_id = %s",
                (attempt.output_blob.object_id,),
            )
            original = cursor.fetchone()
            assert original is not None
            cursor.execute("ALTER TABLE storage.blob_objects DISABLE TRIGGER USER")
            try:
                cursor.execute(
                    "UPDATE storage.blob_objects SET media_type = 'application/octet-stream' "
                    "WHERE object_id = %s",
                    (attempt.output_blob.object_id,),
                )
                with pytest.raises(RuntimeStoreError, match="storage authority is invalid"):
                    store.read_production_render_qc_attempt(attempt.qc_attempt_id)

                cursor.execute(
                    "UPDATE storage.blob_objects SET media_type = %s, "
                    "storage_kind = 'postgres_inline', content_bytes = %s, "
                    "storage_backend_id = NULL, storage_region = NULL, "
                    "storage_locator = NULL, storage_etag = NULL, "
                    "storage_version_id = NULL, write_strategy = NULL, verified_at = NULL "
                    "WHERE object_id = %s",
                    (
                        attempt.output_blob.media_type,
                        output_bytes,
                        attempt.output_blob.object_id,
                    ),
                )
                with pytest.raises(RuntimeStoreError, match="storage authority is invalid"):
                    store.read_production_render_qc_attempt(attempt.qc_attempt_id)
            finally:
                cursor.execute(
                    "UPDATE storage.blob_objects SET content_hash = %s, byte_length = %s, "
                    "media_type = %s, content_bytes = %s, storage_kind = %s, "
                    "storage_backend_id = %s, storage_region = %s, storage_locator = %s, "
                    "storage_etag = %s, storage_version_id = %s, write_strategy = %s, "
                    "verified_at = %s WHERE object_id = %s",
                    (*original, attempt.output_blob.object_id),
                )
                cursor.execute("ALTER TABLE storage.blob_objects ENABLE TRIGGER USER")

    assert store.read_production_render_qc_attempt(attempt.qc_attempt_id) == attempt


def test_restart_reread_hides_token_and_detects_parent_identity_tamper(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    assert DSN is not None
    store, job, recipe = qc_stage4_authority
    parent = _rendered_parent(store, job, recipe, suffix="qc-restart")
    attempt = _reserve_qc(store, parent)
    lease = store.acquire_production_render_qc_lease(
        attempt.qc_attempt_id,
        expected_version=attempt.version,
        lease_seconds=60,
    )
    assert lease is not None

    restarted = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    reread = restarted.read_production_render_qc_attempt(attempt.qc_attempt_id)
    assert reread.state == "scanning"
    assert reread.version == lease.version
    assert not hasattr(reread, "token")
    assert not hasattr(reread, "lease_token")

    tampered_hash = _hash(b"tampered-render-facts")
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE runtime.production_render_qc_attempts DISABLE TRIGGER USER")
            try:
                cursor.execute(
                    "UPDATE runtime.production_render_qc_attempts "
                    "SET render_facts_sha256 = %s WHERE qc_attempt_id = %s",
                    (tampered_hash, attempt.qc_attempt_id),
                )
                with pytest.raises(RuntimeStoreError, match="disagrees with its parent"):
                    restarted.read_production_render_qc_attempt(attempt.qc_attempt_id)
                cursor.execute(
                    "UPDATE runtime.production_render_qc_attempts "
                    "SET render_facts_sha256 = %s, rendered_version = %s "
                    "WHERE qc_attempt_id = %s",
                    (
                        attempt.render_facts_sha256,
                        attempt.rendered_version + 1,
                        attempt.qc_attempt_id,
                    ),
                )
                with pytest.raises(RuntimeStoreError, match="disagrees with its parent"):
                    restarted.read_production_render_qc_attempt(attempt.qc_attempt_id)
            finally:
                cursor.execute(
                    "UPDATE runtime.production_render_qc_attempts "
                    "SET render_facts_sha256 = %s, rendered_version = %s "
                    "WHERE qc_attempt_id = %s",
                    (
                        attempt.render_facts_sha256,
                        attempt.rendered_version,
                        attempt.qc_attempt_id,
                    ),
                )
                cursor.execute(
                    "ALTER TABLE runtime.production_render_qc_attempts ENABLE TRIGGER USER"
                )
