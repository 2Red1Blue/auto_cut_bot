"""Disposable PostgreSQL acceptance for production Render attempt fencing."""

from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from autocut_kernel.pipeline.compile_production_recipe_command import (
    COMPILE_PRODUCTION_RECIPE_COMMAND,
    CompileProductionRecipeCommand,
)
from autocut_kernel.store import (
    PRODUCTION_RECIPE_COMMAND_NAME,
    PRODUCTION_RENDER_COMMAND_NAME,
    ArtifactMember,
    ArtifactScope,
    BlobIntegrityError,
    BlobRef,
    CommandClaim,
    CommandRejection,
    CommandStateError,
    CommandSuccess,
    CommittedArtifactMemberReference,
    IdempotencyConflictError,
    Job,
    PostgresRuntimeStore,
    ProductionRenderLease,
    StoreValidationError,
)
from autocut_kernel.store.models import artifact_set_hash, canonical_payload_hash

from tests.authority.editorial_media_fixture import editorial_timed_media_case
from tests.pipeline.test_compile_production_recipe_command import (
    _install_non_dialogue_blueprint_projection,
    _request,
)

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set AUTOCUT_TEST_POSTGRES_DSN to disposable ac_autocut_verify",
)
MIGRATIONS = Path("packages/autocut-kernel/migrations")


def _hash(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _member(
    artifact_type: str,
    logical_id: str,
    scope: ArtifactScope,
    value: dict[str, object],
) -> ArtifactMember:
    payload = _json(value)
    return ArtifactMember(
        artifact_type,
        logical_id,
        1,
        scope,
        canonical_payload_hash(payload),
        payload,
    )


def _store() -> PostgresRuntimeStore:
    assert DSN is not None
    return PostgresRuntimeStore(lambda: psycopg.connect(DSN))


def _non_stage4_recipe(
    store: PostgresRuntimeStore,
    job: Job,
    *,
    command_name: str = "NotStage4Compiler",
) -> CommittedArtifactMemberReference:
    scope = ArtifactScope("pipeline", "job", job.job_key)
    members = (
        _member(
            "physical_edit_compilation_report",
            "forged_physical_edit_compilation_report",
            scope,
            {"schema_version": "test-report-v1"},
        ),
        _member(
            "recipe",
            "production_recipe@forged-story",
            scope,
            {"schema_version": "test-recipe-v1"},
        ),
        _member(
            "physical_edit_admission",
            "forged_physical_edit_admission",
            scope,
            {"schema_version": "test-admission-v1", "next_action": "render"},
        ),
    )
    request_hash = _hash((job.job_key + command_name).encode())
    claimed = store.claim_command(
        CommandClaim(
            job,
            "stage4:" + command_name,
            command_name,
            request_hash,
            execution_kind="deterministic",
        )
    )
    success = CommandSuccess(claimed.command_slot_id, artifact_set_hash(members), members)
    outcome = store.commit_command_success(success)
    assert outcome.receipt_id is not None
    assert outcome.artifact_set_id is not None
    return CommittedArtifactMemberReference(
        outcome.receipt_id,
        outcome.artifact_set_id,
        1,
        scope,
        "recipe",
        "production_recipe@forged-story",
        1,
        members[1].content_hash,
    )


class _Stage4BridgeStore:
    """Persist only the real Stage 4 closure in PostgreSQL for this Store test."""

    def __init__(self, predecessor: object, durable: PostgresRuntimeStore) -> None:
        self._predecessor = predecessor
        self._durable = durable

    def __getattr__(self, name: str) -> object:
        return getattr(self._predecessor, name)

    def claim_command(self, claim):  # type: ignore[no-untyped-def]
        return self._durable.claim_command(claim)

    def commit_production_recipe_success(self, verified):  # type: ignore[no-untyped-def]
        return self._durable.commit_production_recipe_success(verified)

    def commit_command_rejection(self, rejection):  # type: ignore[no-untyped-def]
        return self._durable.commit_command_rejection(rejection)

    def read_committed_artifact_set(self, job, **expected):  # type: ignore[no-untyped-def]
        if expected["expected_command_name"] == COMPILE_PRODUCTION_RECIPE_COMMAND:
            return self._durable.read_committed_artifact_set(job, **expected)
        return self._predecessor.read_committed_artifact_set(job, **expected)  # type: ignore[attr-defined]


def _render_slot(
    store: PostgresRuntimeStore,
    job: Job,
    *,
    suffix: str,
) -> tuple[UUID, str]:
    request_hash = _hash((job.job_key + suffix).encode())
    claimed = store.claim_command(
        CommandClaim(
            job,
            "render:" + suffix,
            PRODUCTION_RENDER_COMMAND_NAME,
            request_hash,
            execution_kind="deterministic",
        )
    )
    return claimed.command_slot_id, request_hash


def _reserve(
    store: PostgresRuntimeStore,
    job: Job,
    recipe: CommittedArtifactMemberReference,
    *,
    suffix: str,
):
    slot_id, request_hash = _render_slot(store, job, suffix=suffix)
    return store.reserve_production_render_attempt(
        slot_id,
        request_hash,
        recipe=recipe,
        render_plan_sha256=_hash(b"render-plan:" + suffix.encode()),
        render_profile_sha256=_hash(b"render-profile"),
        renderer_identity_sha256=_hash(b"ffmpeg-identity"),
        max_output_bytes=1024,
    )


def _external_blob(job: Job, content: bytes, *, media_type: str = "video/mp4") -> BlobRef:
    assert DSN is not None
    object_id = uuid4()
    reference = BlobRef(object_id, _hash(content), len(content), media_type)
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT job_id FROM runtime.jobs WHERE job_key = %s",
            (job.job_key,),
        )
        row = cursor.fetchone()
        assert row is not None
        cursor.execute(
            """
            INSERT INTO storage.blob_objects (
                object_id, content_hash, byte_length, media_type, content_bytes,
                storage_kind, storage_backend_id, storage_region,
                storage_locator, storage_etag, write_strategy, verified_at
            ) VALUES (
                %s, %s, %s, %s, NULL,
                's3_compatible', 'workspace-s3', 'local-primary',
                %s, '"render-etag"', 's3-single-put-v1',
                transaction_timestamp()
            )
            """,
            (
                object_id,
                reference.content_hash,
                reference.byte_length,
                reference.media_type,
                "autocut/workspace/render/" + object_id.hex,
            ),
        )
        cursor.execute(
            """
            INSERT INTO storage.blob_claims (blob_claim_id, object_id, job_id)
            VALUES (%s, %s, %s)
            """,
            (uuid4(), object_id, UUID(str(row[0]))),
        )
    return reference


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        if connection.info.dbname != "ac_autocut_verify":
            pytest.fail("render attempt tests may reset only ac_autocut_verify")
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for migration in sorted(MIGRATIONS.glob("*.sql")):
                cursor.execute(migration.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def stage4_authority(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[PostgresRuntimeStore, Job, CommittedArtifactMemberReference]:
    patcher = pytest.MonkeyPatch()
    _install_non_dialogue_blueprint_projection(patcher)
    try:
        case = editorial_timed_media_case(
            tmp_path_factory.mktemp("render-stage4-authority"),
            patcher,
        )
        predecessor, *_rest, resolver, limits = case
        durable = _store()
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


def test_reservation_is_exactly_replayable_and_conflicts_fail(
    stage4_authority: tuple[PostgresRuntimeStore, Job, CommittedArtifactMemberReference],
) -> None:
    store, job, recipe = stage4_authority
    attempt = _reserve(store, job, recipe, suffix="reservation")

    replay = store.reserve_production_render_attempt(
        attempt.command_slot_id,
        attempt.request_hash,
        recipe=recipe,
        render_plan_sha256=attempt.render_plan_sha256,
        render_profile_sha256=attempt.render_profile_sha256,
        renderer_identity_sha256=attempt.renderer_identity_sha256,
        max_output_bytes=attempt.max_output_bytes,
    )

    assert attempt.is_fresh_reservation
    assert not replay.is_fresh_reservation
    assert replay == attempt
    assert store.read_production_render_attempt_for_slot(job, attempt.command_slot_id) == attempt
    with pytest.raises(IdempotencyConflictError, match="different identity"):
        store.reserve_production_render_attempt(
            attempt.command_slot_id,
            attempt.request_hash,
            recipe=recipe,
            render_plan_sha256=_hash(b"different-plan"),
            render_profile_sha256=attempt.render_profile_sha256,
            renderer_identity_sha256=attempt.renderer_identity_sha256,
            max_output_bytes=attempt.max_output_bytes,
        )


def test_non_stage4_recipe_authority_is_rejected(
    stage4_authority: tuple[PostgresRuntimeStore, Job, CommittedArtifactMemberReference],
) -> None:
    store, job, _ = stage4_authority
    recipe = _non_stage4_recipe(store, job)
    slot_id, request_hash = _render_slot(store, job, suffix="forged-recipe")

    with pytest.raises(StoreValidationError, match="admitted Stage 4"):
        store.reserve_production_render_attempt(
            slot_id,
            request_hash,
            recipe=recipe,
            render_plan_sha256=_hash(b"plan"),
            render_profile_sha256=_hash(b"profile"),
            renderer_identity_sha256=_hash(b"renderer"),
            max_output_bytes=1024,
        )


def test_stage4_and_render_owner_commands_reject_generic_success(
    stage4_authority: tuple[PostgresRuntimeStore, Job, CommittedArtifactMemberReference],
) -> None:
    store, stage4_job, render_recipe = stage4_authority
    stage4_scope = ArtifactScope("pipeline", "job", stage4_job.job_key)
    stage4_members = (
        _member(
            "physical_edit_compilation_report",
            "physical_edit_compilation_report",
            stage4_scope,
            {"schema_version": "test-report-v1"},
        ),
        _member(
            "recipe",
            "production_recipe@story-1",
            stage4_scope,
            {"schema_version": "test-recipe-v1"},
        ),
        _member(
            "physical_edit_admission",
            "physical_edit_admission",
            stage4_scope,
            {"schema_version": "test-admission-v1", "next_action": "render"},
        ),
    )
    stage4_claim = store.claim_command(
        CommandClaim(
            stage4_job,
            "stage4:owner-api",
            PRODUCTION_RECIPE_COMMAND_NAME,
            _hash(b"stage4-owner-api"),
            execution_kind="deterministic",
        )
    )
    with pytest.raises(CommandStateError, match="Stage 4 owner API"):
        store.commit_command_success(
            CommandSuccess(
                stage4_claim.command_slot_id,
                artifact_set_hash(stage4_members),
                stage4_members,
            )
        )

    with pytest.raises(StoreValidationError, match="verified command capability"):
        store.commit_production_recipe_success(
            CommandSuccess(
                stage4_claim.command_slot_id,
                artifact_set_hash(stage4_members),
                stage4_members,
            )
        )

    render_job = stage4_job
    attempt = _reserve(store, render_job, render_recipe, suffix="owner-api")
    render_member = _member(
        "render_attempt_result",
        "render_attempt_result",
        ArtifactScope("pipeline", "job", render_job.job_key),
        {"schema_version": "test-render-result-v1"},
    )
    with pytest.raises(CommandStateError, match="render-attempt owner API"):
        store.commit_command_success(
            CommandSuccess(
                attempt.command_slot_id,
                artifact_set_hash((render_member,)),
                (render_member,),
            )
        )


def test_concurrent_acquire_has_one_winner_and_active_lease_cannot_be_stolen(
    stage4_authority: tuple[PostgresRuntimeStore, Job, CommittedArtifactMemberReference],
) -> None:
    store, job, recipe = stage4_authority
    attempt = _reserve(store, job, recipe, suffix="concurrent")

    def acquire() -> ProductionRenderLease | None:
        try:
            return store.acquire_production_render_lease(
                attempt.attempt_id,
                expected_version=0,
                lease_seconds=60,
            )
        except CommandStateError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result() for future in (executor.submit(acquire), executor.submit(acquire))
        ]

    leases = [item for item in results if item is not None]
    assert len(leases) == 1
    active = store.read_production_render_attempt(attempt.attempt_id)
    assert active.version == 1
    assert not hasattr(active, "lease_token")
    assert (
        store.acquire_production_render_lease(
            attempt.attempt_id,
            expected_version=active.version,
            lease_seconds=60,
        )
        is None
    )


def test_expired_lease_takeover_and_renew_fence_old_owner(
    stage4_authority: tuple[PostgresRuntimeStore, Job, CommittedArtifactMemberReference],
) -> None:
    store, job, recipe = stage4_authority
    attempt = _reserve(store, job, recipe, suffix="takeover")
    first = store.acquire_production_render_lease(
        attempt.attempt_id,
        expected_version=0,
        lease_seconds=60,
    )
    assert first is not None
    renewed = store.renew_production_render_lease(first, lease_seconds=120)
    assert renewed.version == first.version + 1
    assert renewed.token == first.token
    with pytest.raises(CommandStateError, match="stale|owned elsewhere"):
        store.renew_production_render_lease(first, lease_seconds=120)

    long_attempt = _reserve(store, job, recipe, suffix="renew-shorter")
    long_lease = store.acquire_production_render_lease(
        long_attempt.attempt_id,
        expected_version=0,
        lease_seconds=120,
    )
    assert long_lease is not None
    with pytest.raises(CommandStateError, match="would not extend expiry"):
        store.renew_production_render_lease(long_lease, lease_seconds=60)
    assert store.read_production_render_attempt(long_attempt.attempt_id).version == 1

    takeover_attempt = _reserve(
        store,
        job,
        recipe,
        suffix="takeover-expired",
    )
    short = store.acquire_production_render_lease(
        takeover_attempt.attempt_id,
        expected_version=0,
        lease_seconds=1,
    )
    assert short is not None
    time.sleep(1.5)
    expired = store.read_production_render_attempt(takeover_attempt.attempt_id)
    takeover = store.acquire_production_render_lease(
        takeover_attempt.attempt_id,
        expected_version=expired.version,
        lease_seconds=60,
    )
    assert takeover is not None
    assert takeover.token != short.token
    output = _external_blob(job, b"takeover output bytes")
    with pytest.raises(CommandStateError, match="stale|owned elsewhere"):
        store.record_production_render_output(short, output_blob=output)


def test_lock_wait_cannot_renew_a_lease_that_expired_in_real_database_time(
    stage4_authority: tuple[PostgresRuntimeStore, Job, CommittedArtifactMemberReference],
) -> None:
    assert DSN is not None
    store, job, recipe = stage4_authority
    attempt = _reserve(store, job, recipe, suffix="lock-wait-expiry")
    lease = store.acquire_production_render_lease(
        attempt.attempt_id,
        expected_version=0,
        lease_seconds=1,
    )
    assert lease is not None

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with psycopg.connect(DSN) as blocker, blocker.cursor() as cursor:
            cursor.execute(
                "SELECT attempt_id FROM runtime.production_render_attempts "
                "WHERE attempt_id = %s FOR UPDATE",
                (attempt.attempt_id,),
            )
            future = executor.submit(
                store.renew_production_render_lease,
                lease,
                lease_seconds=60,
            )
            time.sleep(1.5)
        with pytest.raises(CommandStateError, match="renewal CAS was lost"):
            future.result(timeout=5)
    finally:
        executor.shutdown(wait=True)


def test_external_output_is_bound_and_rereads_after_store_restart(
    stage4_authority: tuple[PostgresRuntimeStore, Job, CommittedArtifactMemberReference],
) -> None:
    store, job, recipe = stage4_authority
    attempt = _reserve(store, job, recipe, suffix="output")
    lease = store.acquire_production_render_lease(
        attempt.attempt_id,
        expected_version=0,
        lease_seconds=60,
    )
    assert lease is not None
    output = _external_blob(job, b"verified rendered MP4 bytes")

    rendered = store.record_production_render_output(lease, output_blob=output)

    assert rendered.state == "rendered"
    assert rendered.output_blob == output
    assert rendered.version == lease.version + 1
    assert not hasattr(rendered, "lease_token")
    assert _store().read_production_render_attempt(attempt.attempt_id) == rendered
    with pytest.raises(CommandStateError, match="stale|owned elsewhere"):
        store.record_production_render_output(lease, output_blob=output)


def test_output_must_be_external_mp4_claimed_by_same_job(
    stage4_authority: tuple[PostgresRuntimeStore, Job, CommittedArtifactMemberReference],
) -> None:
    store, job, recipe = stage4_authority
    attempt = _reserve(store, job, recipe, suffix="boundary")
    lease = store.acquire_production_render_lease(
        attempt.attempt_id,
        expected_version=0,
        lease_seconds=60,
    )
    assert lease is not None
    foreign_job = Job("render-output-foreign", "production")
    store.claim_command(
        CommandClaim(
            foreign_job,
            "foreign-job:setup",
            "ForeignJobSetupCommand",
            _hash(b"foreign-job-setup"),
            execution_kind="deterministic",
        )
    )
    foreign = _external_blob(foreign_job, b"foreign rendered bytes")

    with pytest.raises(BlobIntegrityError, match="not claimed"):
        store.record_production_render_output(lease, output_blob=foreign)

    inline_bytes = b"inline rendered bytes"
    inline = store.put_immutable_blob(
        job,
        content=inline_bytes,
        content_hash=_hash(inline_bytes),
        media_type="video/mp4",
    )
    with pytest.raises(BlobIntegrityError, match="allowed external MP4"):
        store.record_production_render_output(lease, output_blob=inline)

    wrong_media = _external_blob(job, b"wrong media", media_type="video/webm")
    with pytest.raises(BlobIntegrityError, match="allowed external MP4"):
        store.record_production_render_output(lease, output_blob=wrong_media)

    with pytest.raises(Exception, match="blob_objects_storage_shape"):
        _external_blob(job, b"")

    oversized = _external_blob(job, b"x" * 1025)
    with pytest.raises(BlobIntegrityError, match="allowed external MP4"):
        store.record_production_render_output(lease, output_blob=oversized)


def test_pre_render_rejection_is_atomic_replayable_and_generic_path_is_blocked(
    stage4_authority: tuple[PostgresRuntimeStore, Job, CommittedArtifactMemberReference],
) -> None:
    store, job, recipe = stage4_authority
    attempt = _reserve(store, job, recipe, suffix="pre-reject")
    rejection = CommandRejection(
        attempt.command_slot_id,
        "RENDER_POLICY_DENIED",
        _json({"reason": "fixture denial"}),
        "denied",
    )

    with pytest.raises(CommandStateError, match="render-attempt owner API"):
        store.commit_command_rejection(rejection)
    outcome = store.commit_production_render_rejection(
        attempt.attempt_id,
        expected_version=0,
        rejection=rejection,
    )
    replay = store.commit_production_render_rejection(
        attempt.attempt_id,
        expected_version=0,
        rejection=rejection,
    )

    assert replay == outcome
    terminal = store.read_production_render_attempt(attempt.attempt_id)
    assert terminal.state == "denied"
    assert terminal.receipt_id == outcome.receipt_id
    assert terminal.output_blob is None


def test_post_render_qc_denial_retains_private_blob_without_artifact_set(
    stage4_authority: tuple[PostgresRuntimeStore, Job, CommittedArtifactMemberReference],
) -> None:
    store, job, recipe = stage4_authority
    attempt = _reserve(store, job, recipe, suffix="post-reject")
    lease = store.acquire_production_render_lease(
        attempt.attempt_id,
        expected_version=0,
        lease_seconds=60,
    )
    assert lease is not None
    output = _external_blob(job, b"private output rejected by QC")
    rendered = store.record_production_render_output(lease, output_blob=output)
    rejection = CommandRejection(
        attempt.command_slot_id,
        "PUBLICATION_QC_DENIED",
        _json({"reason": "black frame fixture"}),
        "denied",
    )

    outcome = store.commit_production_render_rejection(
        attempt.attempt_id,
        expected_version=rendered.version,
        rejection=rejection,
    )

    terminal = store.read_production_render_attempt(attempt.attempt_id)
    assert terminal.state == "denied"
    assert terminal.output_blob == output
    assert terminal.receipt_id == outcome.receipt_id
    assert terminal.artifact_set_id is None


def test_direct_identity_rewrite_and_delete_are_rejected(
    stage4_authority: tuple[PostgresRuntimeStore, Job, CommittedArtifactMemberReference],
) -> None:
    assert DSN is not None
    store, job, recipe = stage4_authority
    attempt = _reserve(store, job, recipe, suffix="trigger")

    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        with pytest.raises(Exception, match="identity is immutable"):
            cursor.execute(
                """
                UPDATE runtime.production_render_attempts
                   SET render_plan_sha256 = %s, version = version + 1
                 WHERE attempt_id = %s
                """,
                (_hash(b"rewritten"), attempt.attempt_id),
            )
        connection.rollback()
        with pytest.raises(Exception, match="cannot be deleted"):
            cursor.execute(
                "DELETE FROM runtime.production_render_attempts WHERE attempt_id = %s",
                (attempt.attempt_id,),
            )
