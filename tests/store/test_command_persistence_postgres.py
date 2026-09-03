"""Opt-in true-PostgreSQL integration test using the tracked migrations.

Set ``AUTOCUT_TEST_POSTGRES_DSN`` to a disposable database.  This test never
creates private substitute tables; it applies the real runtime migrations.
"""

import hashlib
import json
import multiprocessing
import os
import threading
from pathlib import Path
from uuid import UUID

import autocut_kernel.store.postgres as postgres_module
import pytest
from autocut_kernel.store import (
    ArtifactMember,
    ArtifactScope,
    BlobIntegrityError,
    CommandClaim,
    CommandRejection,
    CommandStateError,
    CommandSuccess,
    GenerationAttemptStateError,
    IdempotencyConflictError,
    Job,
    JobProfileMismatchError,
    MediaEvidenceIntegrityError,
    MediaEvidenceReference,
    MediaEvidenceUnavailableError,
    MediaOutputsUnavailableError,
    PersistenceConflictError,
    PostgresRuntimeStore,
    RecipeIntegrityError,
    RecipeReference,
    RecipeUnavailableError,
)
from autocut_kernel.store.models import MaterializationError, MaterializationLimits

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set AUTOCUT_TEST_POSTGRES_DSN to run disposable PostgreSQL tests"
)


def _hold_staging_reservation(
    root: str,
    ready: object,
    release: object,
    result: object,
) -> None:
    """Child-process probe for the filesystem-backed quota ledger."""

    try:
        lease = postgres_module._reserve_materialization_quota(Path(root), 6, 8)
        ready.set()  # type: ignore[union-attr]
        if not release.wait(10):  # type: ignore[union-attr]
            raise TimeoutError("parent did not release the staging reservation")
        lease.release()
        result.put("released")  # type: ignore[union-attr]
    except Exception as error:  # pragma: no cover - asserted by the parent process
        result.put(f"error:{error}")  # type: ignore[union-attr]


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _make_member(
    job_key: str,
    artifact_type: str = "media_evidence",
    logical_id: str = "preflight",
    revision: int = 1,
    content: str = "evidence",
) -> ArtifactMember:
    return ArtifactMember(
        artifact_type=artifact_type,
        logical_id=logical_id,
        revision=revision,
        scope=ArtifactScope("pipeline", "job", job_key),
        content_hash=_digest(content),
        payload_json=json.dumps({"complete": True}),
    )


def _make_set_hash(members: tuple[ArtifactMember, ...]) -> str:
    canonical = [
        {
            "artifact_type": m.artifact_type,
            "content_hash": m.content_hash,
            "logical_id": m.logical_id,
            "payload_json": json.loads(m.payload_json),
            "revision": m.revision,
            "scope": {"key": m.scope.key, "kind": m.scope.kind, "namespace": m.scope.namespace},
        }
        for m in members
    ]
    return "sha256:" + hashlib.sha256(
        json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _make_recipe_member(job_key: str, *, revision: int = 1, payload: object | None = None) -> ArtifactMember:
    recipe_payload = payload if payload is not None else {"recipe": {"revision": revision}}
    encoded = json.dumps(recipe_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return ArtifactMember(
        artifact_type="recipe",
        logical_id="recipe",
        revision=revision,
        scope=ArtifactScope("pipeline", "job", job_key),
        content_hash=_digest(encoded),
        payload_json=encoded,
    )


def _make_media_evidence_member(
    job_key: str, *, payload: object | None = None
) -> ArtifactMember:
    evidence_payload = payload if payload is not None else {
        "source": {"byte_size": 42, "sha256": "sha256:" + "a" * 64},
        "evidence_mode": "fixture_ground_truth_v1",
    }
    encoded = json.dumps(evidence_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return ArtifactMember(
        artifact_type="media_evidence",
        logical_id="media_evidence",
        revision=1,
        scope=ArtifactScope("pipeline", "job", job_key),
        content_hash=_digest(encoded),
        payload_json=encoded,
    )


@pytest.fixture(autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        if connection.info.dbname != "ac_autocut_verify":
            pytest.fail(
                "AUTOCUT_TEST_POSTGRES_DSN must name disposable ac_autocut_verify, never ac_db"
            )
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for name in (
                "0001_runtime_core.sql",
                "0002_runtime_core_constraints.sql",
                "0003_vlm_generation_and_run_finalization.sql",
                "0004_provider_media_objects.sql",
                "0006_ark_provider_recovery.sql",
                "0009_vlm_bounded_retry.sql",
                "0011_generation_retry_schedule.sql",
                "0018_command_execution_kind.sql",
                "0054_object_backed_blob_metadata.sql",
            ):
                cursor.execute((Path("packages/autocut-kernel/migrations") / name).read_text())


# ---------------------------------------------------------------------------
# Basic lifecycle
# ---------------------------------------------------------------------------


def test_claim_success_and_replay_are_one_durable_command() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("postgres-fixture-job", "test")
    claim = CommandClaim(job, "preflight-1", "media_preflight", _digest("request"), execution_kind="deterministic")
    running = store.claim_command(claim)
    assert running.state == "running"
    assert running.is_fresh_claim is True

    member = _make_member(job.job_key)
    set_hash = _make_set_hash((member,))
    succeeded = store.commit_command_success(
        CommandSuccess(running.command_slot_id, set_hash, (member,))
    )
    assert succeeded.state == "succeeded"
    assert succeeded.receipt_id is not None and succeeded.artifact_set_id is not None
    replay = store.claim_command(claim)
    assert replay.artifact_set_id == succeeded.artifact_set_id
    assert replay.is_fresh_claim is False


def test_running_claim_replay_is_not_a_fresh_owner() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    claim = CommandClaim(Job("running-replay-job", "test"), "preflight", "media_preflight", _digest("request"), execution_kind="deterministic")

    fresh = store.claim_command(claim)
    replay = store.claim_command(claim)

    assert fresh.is_fresh_claim is True
    assert replay.command_slot_id == fresh.command_slot_id
    assert replay.state == "running"
    assert replay.is_fresh_claim is False


def test_denial_persists_a_terminal_receipt_without_an_artifact_set() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("postgres-denial-job", "test")
    running = store.claim_command(
        CommandClaim(job, "preflight-2", "media_preflight", _digest("request-2"), execution_kind="deterministic")
    )
    denied = store.commit_command_rejection(
        CommandRejection(
            running.command_slot_id, "PRECHECK_INCOMPLETE", '{"missing":"subtitle_evidence"}'
        )
    )
    assert denied.state == "denied"
    assert denied.artifact_set_id is None
    assert denied.receipt_id is not None
    assert denied.failure_code == "PRECHECK_INCOMPLETE"
    replay = store.read_outcome(job, "preflight-2")
    assert replay is not None and replay.state == "denied"


# ---------------------------------------------------------------------------
# Exact persisted Recipe reads
# ---------------------------------------------------------------------------


def test_read_recipe_returns_only_the_exact_succeeded_recipe_identity() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("recipe-read-job", "test")
    running = store.claim_command(CommandClaim(job, "recipe", "local_media", _digest("request"), execution_kind="deterministic"))
    member = _make_recipe_member(job.job_key)
    store.commit_command_success(CommandSuccess(running.command_slot_id, _make_set_hash((member,)), (member,)))
    reference = RecipeReference(member.scope, member.logical_id, member.revision, member.content_hash)

    persisted = store.read_recipe(job, reference)

    assert persisted.reference == reference
    assert json.loads(persisted.payload_json) == {"recipe": {"revision": 1}}
    assert persisted.job_id == running.job_id
    assert persisted.command_slot_id == running.command_slot_id

    with pytest.raises(RecipeUnavailableError):
        store.read_recipe(Job("other-job", "test"), reference)
    with pytest.raises(JobProfileMismatchError):
        store.read_recipe(Job(job.job_key, "production"), reference)
    with pytest.raises(RecipeUnavailableError):
        store.read_recipe(
            job,
            RecipeReference(member.scope, member.logical_id, member.revision, _digest("forged")),
        )


def test_read_recipe_rejects_a_persisted_content_hash_lie() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("recipe-tamper-job", "test")
    running = store.claim_command(CommandClaim(job, "recipe", "local_media", _digest("request"), execution_kind="deterministic"))
    member = ArtifactMember(
        artifact_type="recipe",
        logical_id="recipe",
        revision=1,
        scope=ArtifactScope("pipeline", "job", job.job_key),
        content_hash=_digest("forged-content-hash"),
        payload_json='{"recipe":{"revision":1}}',
    )
    store.commit_command_success(CommandSuccess(running.command_slot_id, _make_set_hash((member,)), (member,)))
    reference = RecipeReference(member.scope, member.logical_id, member.revision, member.content_hash)

    with pytest.raises(RecipeIntegrityError, match="hash validation"):
        store.read_recipe(job, reference)


def test_read_recipe_keeps_a_prior_revision_reproducible_after_head_advance() -> None:
    """The read path is identity-addressed, not a lookup through logical_heads."""
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("recipe-prior-revision-job", "test")
    running = store.claim_command(CommandClaim(job, "recipe-1", "local_media", _digest("request-1"), execution_kind="deterministic"))
    first = _make_recipe_member(job.job_key, revision=1)
    store.commit_command_success(CommandSuccess(running.command_slot_id, _make_set_hash((first,)), (first,)))
    second = _make_recipe_member(job.job_key, revision=2)

    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT job_id FROM runtime.jobs WHERE job_key = %s", (job.job_key,))
            job_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO runtime.command_slots
                    (execution_kind, command_slot_id, job_id, idempotency_key, command_name, request_hash, state, completed_at)
                VALUES ('deterministic', gen_random_uuid(), %s, 'recipe-2', 'local_media', %s, 'succeeded', transaction_timestamp())
                RETURNING command_slot_id
                """,
                (job_id, _digest("request-2")),
            )
            second_slot_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO runtime.artifact_sets
                    (artifact_set_id, command_slot_id, job_id, set_hash, member_count)
                VALUES (gen_random_uuid(), %s, %s, %s, 1)
                RETURNING artifact_set_id
                """,
                (second_slot_id, job_id, _make_set_hash((second,))),
            )
            second_set_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO runtime.artifacts
                    (artifact_id, artifact_set_id, job_id, artifact_type, logical_id, revision,
                     namespace, scope_kind, scope_key, content_hash, payload_json)
                VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING artifact_id
                """,
                (
                    second_set_id,
                    job_id,
                    second.artifact_type,
                    second.logical_id,
                    second.revision,
                    second.scope.namespace,
                    second.scope.kind,
                    second.scope.key,
                    second.content_hash,
                    second.payload_json,
                ),
            )
            second_artifact_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO runtime.artifact_set_members (artifact_set_id, ordinal, artifact_id) VALUES (%s, 0, %s)",
                (second_set_id, second_artifact_id),
            )
            cursor.execute(
                """
                INSERT INTO runtime.command_receipts (receipt_id, command_slot_id, outcome, result_artifact_set_id)
                VALUES (gen_random_uuid(), %s, 'succeeded', %s)
                """,
                (second_slot_id, second_set_id),
            )
            cursor.execute(
                """
                UPDATE runtime.logical_heads SET artifact_id = %s, revision = 2
                 WHERE job_id = %s AND namespace = %s AND scope_kind = %s AND scope_key = %s
                   AND artifact_type = 'recipe' AND logical_id = 'recipe'
                """,
                (second_artifact_id, job_id, second.scope.namespace, second.scope.kind, second.scope.key),
            )

    first_reference = RecipeReference(first.scope, first.logical_id, first.revision, first.content_hash)
    second_reference = RecipeReference(second.scope, second.logical_id, second.revision, second.content_hash)

    assert json.loads(store.read_recipe(job, first_reference).payload_json) == {"recipe": {"revision": 1}}
    assert json.loads(store.read_recipe(job, second_reference).payload_json) == {"recipe": {"revision": 2}}


# ---------------------------------------------------------------------------
# Exact persisted MediaEvidence reads
# ---------------------------------------------------------------------------


def test_read_media_evidence_returns_only_the_exact_succeeded_identity() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("evidence-read-job", "test")
    running = store.claim_command(CommandClaim(job, "evidence", "local_media", _digest("request"), execution_kind="deterministic"))
    member = _make_media_evidence_member(job.job_key)
    succeeded = store.commit_command_success(
        CommandSuccess(running.command_slot_id, _make_set_hash((member,)), (member,))
    )
    reference = MediaEvidenceReference(
        member.scope, member.logical_id, member.revision, member.content_hash
    )

    persisted = store.read_media_evidence(job, reference)

    assert persisted.reference == reference
    assert json.loads(persisted.payload_json)["source"] == {
        "byte_size": 42,
        "sha256": "sha256:" + "a" * 64,
    }
    assert persisted.job_id == running.job_id
    assert persisted.command_slot_id == running.command_slot_id
    assert persisted.receipt_id == succeeded.receipt_id
    assert persisted.artifact_set_id == succeeded.artifact_set_id

    with pytest.raises(MediaEvidenceUnavailableError):
        store.read_media_evidence(Job("other-job", "test"), reference)
    with pytest.raises(JobProfileMismatchError):
        store.read_media_evidence(Job(job.job_key, "production"), reference)
    with pytest.raises(MediaEvidenceUnavailableError):
        store.read_media_evidence(
            job,
            MediaEvidenceReference(member.scope, member.logical_id, member.revision, _digest("forged")),
        )


def test_read_media_evidence_rejects_a_persisted_content_hash_lie() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("evidence-tamper-job", "test")
    running = store.claim_command(CommandClaim(job, "evidence", "local_media", _digest("request"), execution_kind="deterministic"))
    member = ArtifactMember(
        artifact_type="media_evidence",
        logical_id="media_evidence",
        revision=1,
        scope=ArtifactScope("pipeline", "job", job.job_key),
        content_hash=_digest("forged-content-hash"),
        payload_json='{"source":{"byte_size":42,"sha256":"sha256:' + "a" * 64 + '"}}',
    )
    store.commit_command_success(
        CommandSuccess(running.command_slot_id, _make_set_hash((member,)), (member,))
    )
    reference = MediaEvidenceReference(
        member.scope, member.logical_id, member.revision, member.content_hash
    )

    with pytest.raises(MediaEvidenceIntegrityError, match="hash validation"):
        store.read_media_evidence(job, reference)


def test_read_succeeded_media_outputs_returns_the_pair_with_shared_provenance() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("media-output-reader-job", "test")
    running = store.claim_command(
        CommandClaim(job, "local-media", "local_media_command", _digest("request"), execution_kind="deterministic")
    )
    evidence = _make_media_evidence_member(job.job_key)
    recipe = _make_recipe_member(job.job_key)
    succeeded = store.commit_command_success(
        CommandSuccess(running.command_slot_id, _make_set_hash((evidence, recipe)), (evidence, recipe))
    )

    first = store.read_succeeded_media_outputs(job)
    restarted = store.read_succeeded_media_outputs(job)

    assert first == restarted
    assert first.media_evidence.content_hash == evidence.content_hash
    assert first.recipe.content_hash == recipe.content_hash
    assert first.job_id == running.job_id
    assert first.receipt_id == succeeded.receipt_id
    assert first.artifact_set_id == succeeded.artifact_set_id
    assert first.command_slot_id == running.command_slot_id
    with pytest.raises(MediaOutputsUnavailableError):
        store.read_succeeded_media_outputs(Job("other-media-output-job", "test"))
    with pytest.raises(JobProfileMismatchError):
        store.read_succeeded_media_outputs(Job(job.job_key, "production"))


def test_read_succeeded_media_outputs_rejects_an_incomplete_artifact_set() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("incomplete-media-output-reader-job", "test")
    running = store.claim_command(
        CommandClaim(job, "local-media", "local_media_command", _digest("request"), execution_kind="deterministic")
    )
    evidence = _make_media_evidence_member(job.job_key)
    store.commit_command_success(
        CommandSuccess(running.command_slot_id, _make_set_hash((evidence,)), (evidence,))
    )

    with pytest.raises(MediaOutputsUnavailableError, match="output pair"):
        store.read_succeeded_media_outputs(job)


def test_read_succeeded_media_outputs_requires_canonical_logical_member_ids() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("noncanonical-media-output-reader-job", "test")
    running = store.claim_command(
        CommandClaim(job, "local-media", "local_media_command", _digest("request"), execution_kind="deterministic")
    )
    evidence = _make_media_evidence_member(job.job_key)
    noncanonical_recipe = ArtifactMember(
        artifact_type="recipe",
        logical_id="other_recipe",
        revision=1,
        scope=ArtifactScope("pipeline", "job", job.job_key),
        content_hash=_digest('{"recipe":true}'),
        payload_json='{"recipe":true}',
    )
    store.commit_command_success(
        CommandSuccess(
            running.command_slot_id,
            _make_set_hash((evidence, noncanonical_recipe)),
            (evidence, noncanonical_recipe),
        )
    )

    with pytest.raises(MediaOutputsUnavailableError, match="output pair"):
        store.read_succeeded_media_outputs(job)


# ---------------------------------------------------------------------------
# Concurrent / same-intent claim
# ---------------------------------------------------------------------------


def test_concurrent_same_intent_claim_is_replay_safe() -> None:
    """Two connections claim the same idempotency key with the same intent.

    The second caller must receive the existing running slot, not a
    unique-violation error.
    """
    assert DSN is not None

    store_a = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    store_b = PostgresRuntimeStore(lambda: psycopg.connect(DSN))

    job = Job("concurrent-same-job", "test")
    claim = CommandClaim(job, "same-key", "preflight", _digest("req"), execution_kind="deterministic")

    gate = threading.Barrier(2)
    outcomes: list[object] = []

    def claim_in_parallel(store: PostgresRuntimeStore) -> None:
        gate.wait()
        try:
            outcomes.append(store.claim_command(claim))
        except Exception as error:  # pragma: no cover - assertion below reports it
            outcomes.append(error)

    workers = [threading.Thread(target=claim_in_parallel, args=(store,)) for store in (store_a, store_b)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert len(outcomes) == 2
    assert not any(isinstance(outcome, Exception) for outcome in outcomes)
    outcome_a, outcome_b = outcomes  # type: ignore[misc]
    assert outcome_b.state == "running"
    assert outcome_b.command_slot_id == outcome_a.command_slot_id
    assert {outcome_a.is_fresh_claim, outcome_b.is_fresh_claim} == {False, True}


# ---------------------------------------------------------------------------
# Different intent conflict
# ---------------------------------------------------------------------------


def test_different_intent_claim_is_rejected() -> None:
    """Same idempotency key but different command_name or request_hash must fail."""
    assert DSN is not None

    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("different-intent-job", "test")

    running = store.claim_command(CommandClaim(job, "key-1", "cmd-a", _digest("req-a"), execution_kind="deterministic"))
    assert running.state == "running"

    with pytest.raises(IdempotencyConflictError, match="already claimed by a different command"):
        store.claim_command(CommandClaim(job, "key-1", "cmd-b", _digest("req-a"), execution_kind="deterministic"))

    with pytest.raises(IdempotencyConflictError, match="already claimed by a different command"):
        store.claim_command(CommandClaim(job, "key-1", "cmd-a", _digest("req-b"), execution_kind="deterministic"))


# ---------------------------------------------------------------------------
# Job creation race
# ---------------------------------------------------------------------------


def test_concurrent_job_creation_is_race_free() -> None:
    """Two concurrent _ensure_job calls for the same job_key must not leak a
    unique-violation."""
    assert DSN is not None

    store_a = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    store_b = PostgresRuntimeStore(lambda: psycopg.connect(DSN))

    job = Job("race-job", "test")
    claim = CommandClaim(job, "cmd-1", "preflight", _digest("req"), execution_kind="deterministic")

    claim_b = CommandClaim(job, "cmd-2", "preflight", _digest("req-2"), execution_kind="deterministic")
    gate = threading.Barrier(2)
    outcomes: list[object] = []

    def claim_in_parallel(store: PostgresRuntimeStore, candidate: CommandClaim) -> None:
        gate.wait()
        try:
            outcomes.append(store.claim_command(candidate))
        except Exception as error:  # pragma: no cover
            outcomes.append(error)

    workers = [
        threading.Thread(target=claim_in_parallel, args=(store_a, claim)),
        threading.Thread(target=claim_in_parallel, args=(store_b, claim_b)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert not any(isinstance(outcome, Exception) for outcome in outcomes)
    outcome_a, outcome_b = outcomes  # type: ignore[misc]
    assert outcome_a.state == "running"
    assert outcome_b.state == "running"
    assert outcome_a.command_slot_id != outcome_b.command_slot_id


# ---------------------------------------------------------------------------
# Failed receipt (outcome = 'failed')
# ---------------------------------------------------------------------------


def test_failed_receipt_is_terminal_and_replayable() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("failed-job", "test")
    running = store.claim_command(
        CommandClaim(job, "fail-cmd", "preflight", _digest("req"), execution_kind="deterministic")
    )
    failed = store.commit_command_rejection(
        CommandRejection(
            running.command_slot_id,
            "RUNTIME_CRASH",
            '{"reason":"unexpected"}',
            outcome="failed",
        )
    )
    assert failed.state == "failed"
    assert failed.failure_code == "RUNTIME_CRASH"
    assert failed.receipt_id is not None

    replay = store.read_outcome(job, "fail-cmd")
    assert replay is not None and replay.state == "failed"


# ---------------------------------------------------------------------------
# Cross-job artifact rejection
# ---------------------------------------------------------------------------


def test_cross_job_artifact_is_rejected_by_database() -> None:
    """An artifact whose job_id differs from its artifact_set's job_id must be
    rejected by the runtime_artifact_job_matches_set_check trigger."""
    assert DSN is not None

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            # Create two jobs
            cur.execute(
                "INSERT INTO runtime.jobs (job_id, job_key, profile, state) VALUES (gen_random_uuid(), %s, 'test', 'running') RETURNING job_id",
                ("cross-job-a",),
            )
            job_a = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO runtime.jobs (job_id, job_key, profile, state) VALUES (gen_random_uuid(), %s, 'test', 'running') RETURNING job_id",
                ("cross-job-b",),
            )
            job_b = cur.fetchone()[0]

            # Create a command slot under job_a
            cur.execute(
                "INSERT INTO runtime.command_slots (execution_kind, command_slot_id, job_id, idempotency_key, command_name, request_hash, state)"
                " VALUES ('deterministic', gen_random_uuid(), %s, 'ck', 'preflight', %s, 'running') RETURNING command_slot_id",
                (job_a, _digest("req")),
            )
            slot_id = cur.fetchone()[0]

            # Create an artifact set under job_a
            cur.execute(
                "INSERT INTO runtime.artifact_sets (artifact_set_id, command_slot_id, job_id, set_hash, member_count)"
                " VALUES (gen_random_uuid(), %s, %s, %s, 1) RETURNING artifact_set_id",
                (slot_id, job_a, _digest("set")),
            )
            set_id = cur.fetchone()[0]

            # Keep the set complete so the commit failure below identifies the
            # cross-job composite relationship, rather than completeness.
            cur.execute(
                "INSERT INTO runtime.artifacts"
                " (artifact_id, artifact_set_id, job_id, artifact_type, logical_id, revision,"
                "  namespace, scope_kind, scope_key, content_hash, payload_json)"
                " VALUES (gen_random_uuid(), %s, %s, 'media', 'valid', 1, 'ns', 'job', 'k', %s, '{}'::jsonb)"
                " RETURNING artifact_id",
                (set_id, job_a, _digest("valid-content")),
            )
            artifact_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO runtime.artifact_set_members (artifact_set_id, ordinal, artifact_id)"
                " VALUES (%s, 0, %s)",
                (set_id, artifact_id),
            )

            # Constraint triggers and the composite FK are deferred: insertion
            # succeeds, but the invalid transaction must fail at commit.
            cur.execute(
                "INSERT INTO runtime.artifacts"
                " (artifact_id, artifact_set_id, job_id, artifact_type, logical_id, revision,"
                "  namespace, scope_kind, scope_key, content_hash, payload_json)"
                " VALUES (gen_random_uuid(), %s, %s, 'media', 'log', 1, 'ns', 'job', 'k', %s, '{}'::jsonb)",
                (set_id, job_b, _digest("content")),
            )
            with pytest.raises(Exception, match="violates foreign key constraint"):
                conn.commit()
            conn.rollback()


# ---------------------------------------------------------------------------
# Immutable rows
# ---------------------------------------------------------------------------


def test_committed_receipt_is_immutable() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("immutable-receipt-job", "test")
    running = store.claim_command(
        CommandClaim(job, "cmd", "preflight", _digest("req"), execution_kind="deterministic")
    )
    store.commit_command_rejection(
        CommandRejection(running.command_slot_id, "DENY", '{"r":"x"}')
    )

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT receipt_id FROM runtime.command_receipts")
            receipt_id = cur.fetchone()[0]
            for statement in (
                "UPDATE runtime.command_receipts SET failure_code = 'X' WHERE receipt_id = %s",
                "DELETE FROM runtime.command_receipts WHERE receipt_id = %s",
            ):
                with pytest.raises(Exception, match="committed receipts are immutable"):
                    cur.execute(statement, (receipt_id,))
                conn.rollback()


def test_committed_artifact_set_is_immutable() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("immutable-set-job", "test")
    running = store.claim_command(
        CommandClaim(job, "cmd", "preflight", _digest("req"), execution_kind="deterministic")
    )
    member = _make_member(job.job_key)
    set_hash = _make_set_hash((member,))
    store.commit_command_success(
        CommandSuccess(running.command_slot_id, set_hash, (member,))
    )

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT artifact_set_id FROM runtime.artifact_sets")
            set_id = cur.fetchone()[0]
            for statement in (
                "UPDATE runtime.artifact_sets SET member_count = 99 WHERE artifact_set_id = %s",
                "DELETE FROM runtime.artifact_sets WHERE artifact_set_id = %s",
            ):
                with pytest.raises(Exception, match="committed artifact sets are immutable"):
                    cur.execute(statement, (set_id,))
                conn.rollback()


def test_committed_artifact_is_immutable() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("immutable-artifact-job", "test")
    running = store.claim_command(
        CommandClaim(job, "cmd", "preflight", _digest("req"), execution_kind="deterministic")
    )
    member = _make_member(job.job_key)
    set_hash = _make_set_hash((member,))
    store.commit_command_success(
        CommandSuccess(running.command_slot_id, set_hash, (member,))
    )

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT artifact_id FROM runtime.artifacts")
            art_id = cur.fetchone()[0]
            for statement in (
                "UPDATE runtime.artifacts SET revision = 99 WHERE artifact_id = %s",
                "DELETE FROM runtime.artifacts WHERE artifact_id = %s",
            ):
                with pytest.raises(Exception, match="committed artifacts are immutable"):
                    cur.execute(statement, (art_id,))
                conn.rollback()


def test_committed_member_rows_are_immutable_for_update_and_delete() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("immutable-member-job", "test")
    running = store.claim_command(CommandClaim(job, "cmd", "preflight", _digest("req"), execution_kind="deterministic"))
    member = _make_member(job.job_key)
    store.commit_command_success(CommandSuccess(running.command_slot_id, _make_set_hash((member,)), (member,)))

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT artifact_set_id, ordinal FROM runtime.artifact_set_members")
            set_id, ordinal = cur.fetchone()
            for statement in (
                "UPDATE runtime.artifact_set_members SET ordinal = 1 WHERE artifact_set_id = %s AND ordinal = %s",
                "DELETE FROM runtime.artifact_set_members WHERE artifact_set_id = %s AND ordinal = %s",
            ):
                with pytest.raises(Exception, match="committed artifact set members are immutable"):
                    cur.execute(statement, (set_id, ordinal))
                conn.rollback()


# ---------------------------------------------------------------------------
# Incomplete sets
# ---------------------------------------------------------------------------


def test_incomplete_artifact_set_is_rejected() -> None:
    """An artifact set with member_count that doesn't match actual members must
    be rejected by the assert_artifact_set_complete trigger."""
    assert DSN is not None

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runtime.jobs (job_id, job_key, profile, state) VALUES (gen_random_uuid(), %s, 'test', 'running') RETURNING job_id",
                ("incomplete-job",),
            )
            job_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO runtime.command_slots (execution_kind, command_slot_id, job_id, idempotency_key, command_name, request_hash, state)"
                " VALUES ('deterministic', gen_random_uuid(), %s, 'ik', 'preflight', %s, 'running') RETURNING command_slot_id",
                (job_id, _digest("req")),
            )
            slot_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO runtime.artifact_sets (artifact_set_id, command_slot_id, job_id, set_hash, member_count)"
                " VALUES (gen_random_uuid(), %s, %s, %s, 2)",
                (slot_id, job_id, _digest("set")),
            )
            # member_count = 2 but only 0 members — trigger fires at commit
            with pytest.raises(Exception, match="artifact set members are incomplete"):
                conn.commit()
            conn.rollback()


# ---------------------------------------------------------------------------
# Wrong receipt/set links
# ---------------------------------------------------------------------------


def test_wrong_receipt_set_link_is_rejected() -> None:
    """A successful receipt must reference an artifact set that belongs to the
    same command slot."""
    assert DSN is not None

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            # Job + slot A
            cur.execute(
                "INSERT INTO runtime.jobs (job_id, job_key, profile, state) VALUES (gen_random_uuid(), %s, 'test', 'running') RETURNING job_id",
                ("wrong-link-job",),
            )
            job_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO runtime.command_slots (execution_kind, command_slot_id, job_id, idempotency_key, command_name, request_hash, state)"
                " VALUES ('deterministic', gen_random_uuid(), %s, 'a', 'preflight', %s, 'running') RETURNING command_slot_id",
                (job_id, _digest("req-a")),
            )
            slot_a = cur.fetchone()[0]
            # Slot B
            cur.execute(
                "INSERT INTO runtime.command_slots (execution_kind, command_slot_id, job_id, idempotency_key, command_name, request_hash, state)"
                " VALUES ('deterministic', gen_random_uuid(), %s, 'b', 'preflight', %s, 'running') RETURNING command_slot_id",
                (job_id, _digest("req-b")),
            )
            slot_b = cur.fetchone()[0]
            # Artifact set under slot A
            cur.execute(
                "INSERT INTO runtime.artifact_sets (artifact_set_id, command_slot_id, job_id, set_hash, member_count)"
                " VALUES (gen_random_uuid(), %s, %s, %s, 1) RETURNING artifact_set_id",
                (slot_a, job_id, _digest("set")),
            )
            set_a = cur.fetchone()[0]
            # Insert an artifact and member to satisfy completeness
            cur.execute(
                "INSERT INTO runtime.artifacts"
                " (artifact_id, artifact_set_id, job_id, artifact_type, logical_id, revision,"
                "  namespace, scope_kind, scope_key, content_hash, payload_json)"
                " VALUES (gen_random_uuid(), %s, %s, 'media', 'log', 1, 'ns', 'job', 'k', %s, '{}'::jsonb)",
                (set_a, job_id, _digest("content")),
            )
            cur.execute(
                "INSERT INTO runtime.artifact_set_members (artifact_set_id, ordinal, artifact_id)"
                " SELECT %s, 0, artifact_id FROM runtime.artifacts WHERE artifact_set_id = %s",
                (set_a, set_a),
            )
            # Receipt for slot B referencing set A (wrong link)
            cur.execute(
                "INSERT INTO runtime.command_receipts (receipt_id, command_slot_id, outcome, result_artifact_set_id)"
                " VALUES (gen_random_uuid(), %s, 'succeeded', %s)",
                (slot_b, set_a),
            )
            # Make the slot terminal so the receipt/slot lifecycle check passes;
            # the commit must then reach the independent receipt/set binding check.
            cur.execute(
                "UPDATE runtime.command_slots SET state = 'succeeded', completed_at = transaction_timestamp()"
                " WHERE command_slot_id = %s",
                (slot_b,),
            )
            with pytest.raises(
                Exception, match="successful receipt must reference its command slot artifact set"
            ):
                conn.commit()
            conn.rollback()


# ---------------------------------------------------------------------------
# Same set hash under different jobs is allowed
# ---------------------------------------------------------------------------


def test_same_set_hash_under_different_jobs_is_allowed() -> None:
    """The same set_hash can be used by different jobs (UNIQUE is scoped to
    (job_id, set_hash))."""
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))

    # The member payload is intentionally identical across Jobs, so this tests
    # database namespacing rather than failing CommandSuccess hash validation.
    member = _make_member("shared-scope")
    set_hash = _make_set_hash((member,))

    # Job 1
    job1 = Job("same-hash-job-1", "test")
    r1 = store.claim_command(CommandClaim(job1, "cmd1", "preflight", _digest("r1"), execution_kind="deterministic"))
    store.commit_command_success(CommandSuccess(r1.command_slot_id, set_hash, (member,)))

    # Job 2 — same set_hash, different job
    job2 = Job("same-hash-job-2", "test")
    member2 = _make_member("shared-scope")
    r2 = store.claim_command(CommandClaim(job2, "cmd2", "preflight", _digest("r2"), execution_kind="deterministic"))
    store.commit_command_success(CommandSuccess(r2.command_slot_id, set_hash, (member2,)))

    # Both jobs succeeded with the same set_hash
    assert store.read_outcome(job1, "cmd1").state == "succeeded"  # type: ignore[union-attr]
    assert store.read_outcome(job2, "cmd2").state == "succeeded"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Same set hash under same job is rejected (namespaced uniqueness)
# ---------------------------------------------------------------------------


def test_same_set_hash_under_same_job_is_rejected() -> None:
    """The same (job_id, set_hash) pair must be rejected by the UNIQUE
    constraint."""
    assert DSN is not None
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runtime.jobs (job_id, job_key, profile, state)"
                " VALUES (gen_random_uuid(), 'same-hash-job', 'test', 'running') RETURNING job_id"
            )
            job_id = cur.fetchone()[0]
            slots = []
            for key in ("one", "two"):
                cur.execute(
                    "INSERT INTO runtime.command_slots"
                    " (execution_kind, command_slot_id, job_id, idempotency_key, command_name, request_hash, state)"
                    " VALUES ('deterministic', gen_random_uuid(), %s, %s, 'preflight', %s, 'running')"
                    " RETURNING command_slot_id",
                    (job_id, key, _digest(key)),
                )
                slots.append(cur.fetchone()[0])
            set_hash = _digest("identical-set")
            cur.execute(
                "INSERT INTO runtime.artifact_sets"
                " (artifact_set_id, command_slot_id, job_id, set_hash, member_count)"
                " VALUES (gen_random_uuid(), %s, %s, %s, 1)",
                (slots[0], job_id, set_hash),
            )
            with pytest.raises(Exception, match="artifact_sets_job_id_set_hash_key"):
                cur.execute(
                    "INSERT INTO runtime.artifact_sets"
                    " (artifact_set_id, command_slot_id, job_id, set_hash, member_count)"
                    " VALUES (gen_random_uuid(), %s, %s, %s, 1)",
                    (slots[1], job_id, set_hash),
                )
            conn.rollback()


# ---------------------------------------------------------------------------
# Job finalization is explicit
# ---------------------------------------------------------------------------


def test_multi_command_job_stays_running_until_explicit_finalizer() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))

    job = Job("explicit-finalizer-job", "test")

    r1 = store.claim_command(CommandClaim(job, "cmd1", "preflight", _digest("r1"), execution_kind="deterministic"))
    r2 = store.claim_command(CommandClaim(job, "cmd2", "preflight", _digest("r2"), execution_kind="deterministic"))
    member = _make_member(job.job_key)
    set_hash = _make_set_hash((member,))
    first = store.commit_command_success(CommandSuccess(r1.command_slot_id, set_hash, (member,)))
    store.commit_command_rejection(CommandRejection(r2.command_slot_id, "DENY", '{"r":"x"}'))

    assert store.claim_command(CommandClaim(job, "cmd1", "preflight", _digest("r1"), execution_kind="deterministic")) == first
    fresh = store.claim_command(CommandClaim(job, "fresh", "preflight", _digest("fresh"), execution_kind="deterministic"))
    store.commit_command_rejection(CommandRejection(fresh.command_slot_id, "DONE", "{}"))

    finalizer = store.claim_command(
        CommandClaim(job, "finalize", "FinalizeRunOutcome", _digest("finalize"), execution_kind="deterministic")
    )
    run_outcome = _make_member(
        job.job_key,
        artifact_type="run_outcome",
        logical_id="run_outcome",
        content="run-complete",
    )
    terminal = store.finalize_run_success(
        CommandSuccess(
            finalizer.command_slot_id,
            _make_set_hash((run_outcome,)),
            (run_outcome,),
        )
    )
    assert terminal.state == "succeeded"
    assert store.finalize_run_success(
        CommandSuccess(finalizer.command_slot_id, _make_set_hash((run_outcome,)), (run_outcome,))
    ) == terminal
    with pytest.raises(CommandStateError, match="job is already terminal"):
        store.claim_command(CommandClaim(job, "post-final", "preflight", _digest("closed"), execution_kind="deterministic"))

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM runtime.jobs WHERE job_key = %s", (job.job_key,))
            state = cur.fetchone()[0]
            assert (state.decode() if isinstance(state, bytes) else state) == "succeeded"


@pytest.mark.parametrize(
    ("slot_state", "receipt_outcome", "expectation"),
    (
        ("running", "denied", "pending or running command slot must not have a receipt"),
        ("denied", None, "terminal command slot must have exactly one matching receipt"),
        ("denied", "failed", "terminal command slot must have exactly one matching receipt"),
    ),
)
def test_command_slot_receipt_lifecycle_is_enforced_at_commit(
    slot_state: str, receipt_outcome: str | None, expectation: str
) -> None:
    """Deferred checks reject invalid final lifecycle states at the real commit boundary."""
    assert DSN is not None
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runtime.jobs (job_id, job_key, profile, state)"
                " VALUES (gen_random_uuid(), %s, 'test', 'running') RETURNING job_id",
                (f"receipt-lifecycle-{slot_state}-{receipt_outcome}",),
            )
            job_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO runtime.command_slots"
                " (execution_kind, command_slot_id, job_id, idempotency_key, command_name, request_hash, state, completed_at)"
                " VALUES ('deterministic', gen_random_uuid(), %s, 'key', 'preflight', %s, %s,"
                " CASE WHEN %s IN ('denied', 'failed', 'succeeded') THEN transaction_timestamp() END)"
                " RETURNING command_slot_id",
                (job_id, _digest("receipt-lifecycle"), slot_state, slot_state),
            )
            slot_id = cur.fetchone()[0]
            if receipt_outcome is not None:
                cur.execute(
                    "INSERT INTO runtime.command_receipts"
                    " (receipt_id, command_slot_id, outcome, failure_code, failure_detail)"
                    " VALUES (gen_random_uuid(), %s, %s, 'TEST', '{}'::jsonb)",
                    (slot_id, receipt_outcome),
                )
            with pytest.raises(Exception, match=expectation):
                conn.commit()
            conn.rollback()


def test_run_finalizer_is_blocked_by_another_running_slot() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("blocked-finalizer-job", "test")
    running = store.claim_command(CommandClaim(job, "work", "preflight", _digest("work"), execution_kind="deterministic"))
    finalizer = store.claim_command(
        CommandClaim(job, "finalize", "FinalizeRunOutcome", _digest("finalize"), execution_kind="deterministic")
    )

    rejection = CommandRejection(finalizer.command_slot_id, "RUN_FAILED", "{}", "failed")
    with pytest.raises(CommandStateError, match="blocked"):
        store.finalize_run_rejection(rejection)

    store.commit_command_rejection(CommandRejection(running.command_slot_id, "WORK_DONE", "{}"))
    assert store.finalize_run_rejection(rejection).state == "failed"
    with pytest.raises(CommandStateError, match="job is already terminal"):
        store.claim_command(CommandClaim(job, "fresh", "preflight", _digest("fresh"), execution_kind="deterministic"))


def test_success_and_failure_run_finalization_race_has_one_terminal_winner() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("finalizer-outcome-race", "test")
    finalizer = store.claim_command(
        CommandClaim(job, "finalize", "FinalizeRunOutcome", _digest("finalize"), execution_kind="deterministic")
    )
    member = _make_member(
        job.job_key,
        artifact_type="run_outcome",
        logical_id="run_outcome",
        content="terminal-race",
    )
    success = CommandSuccess(
        finalizer.command_slot_id, _make_set_hash((member,)), (member,)
    )
    rejection = CommandRejection(finalizer.command_slot_id, "FAILED", "{}", "failed")
    gate = threading.Barrier(2)
    outcomes: list[object] = []

    def succeed() -> None:
        gate.wait()
        try:
            outcomes.append(store.finalize_run_success(success))
        except Exception as error:  # pragma: no cover - asserted below
            outcomes.append(error)

    def fail() -> None:
        gate.wait()
        try:
            outcomes.append(store.finalize_run_rejection(rejection))
        except Exception as error:  # pragma: no cover - asserted below
            outcomes.append(error)

    workers = [threading.Thread(target=succeed), threading.Thread(target=fail)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert sum(getattr(item, "state", None) in ("succeeded", "failed") for item in outcomes) == 1
    assert sum(isinstance(item, CommandStateError) for item in outcomes) == 1


def test_finalizer_holding_job_lock_serializes_with_a_fresh_claim() -> None:
    assert DSN is not None
    setup_store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("finalizer-fresh-claim-race", "test")
    finalizer = setup_store.claim_command(
        CommandClaim(job, "finalize", "FinalizeRunOutcome", _digest("finalize"), execution_kind="deterministic")
    )
    member = _make_member(
        job.job_key,
        artifact_type="run_outcome",
        logical_id="run_outcome",
        content="serialized-final",
    )
    success = CommandSuccess(
        finalizer.command_slot_id, _make_set_hash((member,)), (member,)
    )
    job_locked = threading.Event()
    allow_finalizer = threading.Event()

    class PausingCursor:
        def __init__(self, cursor: object) -> None:
            self._cursor = cursor

        def execute(self, query: str, params: tuple[object, ...] = ()) -> object:
            result = self._cursor.execute(query, params)  # type: ignore[attr-defined]
            if "SELECT state FROM runtime.jobs WHERE job_id = %s FOR UPDATE" in query:
                job_locked.set()
                assert allow_finalizer.wait(timeout=5)
            return result

        def fetchone(self) -> tuple[object, ...] | None:
            return self._cursor.fetchone()  # type: ignore[attr-defined,no-any-return]

        def close(self) -> None:
            self._cursor.close()  # type: ignore[attr-defined]

    class PausingConnection:
        def __init__(self) -> None:
            self._connection = psycopg.connect(DSN)

        def cursor(self) -> PausingCursor:
            return PausingCursor(self._connection.cursor())

        def commit(self) -> None:
            self._connection.commit()

        def rollback(self) -> None:
            self._connection.rollback()

        def close(self) -> None:
            self._connection.close()

    finalizer_store = PostgresRuntimeStore(PausingConnection)
    results: list[object] = []

    def finalize() -> None:
        try:
            results.append(finalizer_store.finalize_run_success(success))
        except Exception as error:  # pragma: no cover - asserted below
            results.append(error)

    def claim_fresh() -> None:
        try:
            results.append(
                setup_store.claim_command(
                    CommandClaim(job, "fresh", "preflight", _digest("fresh"), execution_kind="deterministic")
                )
            )
        except Exception as error:  # pragma: no cover - asserted below
            results.append(error)

    finalizer_worker = threading.Thread(target=finalize)
    finalizer_worker.start()
    assert job_locked.wait(timeout=5)
    claim_worker = threading.Thread(target=claim_fresh)
    claim_worker.start()
    claim_worker.join(timeout=0.2)
    assert claim_worker.is_alive()
    allow_finalizer.set()
    finalizer_worker.join(timeout=5)
    claim_worker.join(timeout=5)

    assert sum(getattr(item, "state", None) == "succeeded" for item in results) == 1
    assert sum(isinstance(item, CommandStateError) for item in results) == 1


def test_revision_race_returns_one_success_and_one_stale_head() -> None:
    assert DSN is not None
    store_a = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    store_b = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("revision-race-job", "test")
    first = store_a.claim_command(CommandClaim(job, "one", "preflight", _digest("one"), execution_kind="deterministic"))
    second = store_b.claim_command(CommandClaim(job, "two", "preflight", _digest("two"), execution_kind="deterministic"))
    member_a = _make_member(job.job_key, content="one")
    member_b = _make_member(job.job_key, content="two")
    gate = threading.Barrier(2)
    results: list[object] = []

    def commit(store: PostgresRuntimeStore, slot_id: object, member: ArtifactMember) -> None:
        gate.wait()
        try:
            results.append(store.commit_command_success(CommandSuccess(slot_id, _make_set_hash((member,)), (member,))))  # type: ignore[arg-type]
        except Exception as error:
            results.append(error)

    workers = [
        threading.Thread(target=commit, args=(store_a, first.command_slot_id, member_a)),
        threading.Thread(target=commit, args=(store_b, second.command_slot_id, member_b)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert sum(getattr(result, "state", None) == "succeeded" for result in results) == 1
    assert sum(type(result).__name__ == "StaleHeadError" for result in results) == 1


# ---------------------------------------------------------------------------
# Command terminal state mismatch
# ---------------------------------------------------------------------------


def test_recommit_with_different_outcome_is_rejected() -> None:
    """Replaying a terminal command with a different outcome must raise
    CommandStateError."""
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))

    job = Job("outcome-mismatch-job", "test")
    running = store.claim_command(
        CommandClaim(job, "cmd", "preflight", _digest("req"), execution_kind="deterministic")
    )

    # First, succeed
    member = _make_member(job.job_key)
    set_hash = _make_set_hash((member,))
    store.commit_command_success(
        CommandSuccess(running.command_slot_id, set_hash, (member,))
    )

    # Then try to deny the same slot
    with pytest.raises(CommandStateError, match="already completed as"):
        store.commit_command_rejection(
            CommandRejection(running.command_slot_id, "DENY", '{"r":"x"}')
        )


def test_recommit_success_with_different_set_is_rejected() -> None:
    """Replaying a successful command with a different set_hash must raise
    CommandStateError."""
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))

    job = Job("set-mismatch-job", "test")
    running = store.claim_command(
        CommandClaim(job, "cmd", "preflight", _digest("req"), execution_kind="deterministic")
    )

    member = _make_member(job.job_key)
    store.commit_command_success(
        CommandSuccess(running.command_slot_id, _make_set_hash((member,)), (member,))
    )

    # Try to replay with different content → different set_hash
    member2 = _make_member(job.job_key, content="different")
    with pytest.raises(CommandStateError, match="different artifact set"):
        store.commit_command_success(
            CommandSuccess(running.command_slot_id, _make_set_hash((member2,)), (member2,))
        )


# ---------------------------------------------------------------------------
# Immutable blobs and provider generation attempts
# ---------------------------------------------------------------------------


def test_blob_bytes_are_verified_immutable_and_claimable_by_multiple_jobs() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    content = b'{"provider":"raw"}'
    content_hash = "sha256:" + hashlib.sha256(content).hexdigest()

    with pytest.raises(BlobIntegrityError, match="declared SHA-256"):
        store.put_immutable_blob(
            Job("blob-mismatch", "test"),
            content=content,
            content_hash=_digest("wrong"),
            media_type="application/json",
        )

    first = store.put_immutable_blob(
        Job("blob-job-a", "test"),
        content=content,
        content_hash=content_hash,
        media_type="application/json",
    )
    second = store.put_immutable_blob(
        Job("blob-job-b", "test"),
        content=content,
        content_hash=content_hash,
        media_type="application/json",
    )
    assert second == first
    assert store.read_immutable_blob(Job("blob-job-a", "test"), first) == content
    assert store.read_immutable_blob(Job("blob-job-b", "test"), second) == content
    with pytest.raises(BlobIntegrityError, match="does not match durable"):
        store.read_immutable_blob(
            Job("blob-job-a", "test"),
            type(first)(first.object_id, first.content_hash, first.byte_length + 1, first.media_type),
        )

    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            with pytest.raises(Exception, match="immutable blob objects"):
                cursor.execute(
                    "UPDATE storage.blob_objects SET content_bytes = %s WHERE object_id = %s",
                    (b"tampered", first.object_id),
                )
            connection.rollback()


def test_bounded_materialization_streams_exact_job_claim_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert DSN is not None
    staging_root = tmp_path / "verified-media"
    staging_root.mkdir(mode=0o700)
    store = PostgresRuntimeStore(
        lambda: psycopg.connect(DSN), materialization_staging_root=staging_root
    )
    job = Job("bounded-materialization-owner", "test")
    content = b"abcdef"
    reference = store.put_immutable_blob(
        job,
        content=content,
        content_hash="sha256:" + hashlib.sha256(content).hexdigest(),
        media_type="video/mp4",
    )
    limits = MaterializationLimits(
        max_source_bytes=16,
        timed_speech_max_request_bytes=16,
        copy_chunk_bytes=2,
        staging_quota_bytes=8,
    )
    process_context = multiprocessing.get_context("spawn")
    child_ready = process_context.Event()
    child_release = process_context.Event()
    child_result = process_context.Queue()
    child = process_context.Process(
        target=_hold_staging_reservation,
        args=(str(staging_root), child_ready, child_release, child_result),
    )
    child.start()
    assert child_ready.wait(10)
    with pytest.raises(MaterializationError, match="capacity") as child_busy:
        store.materialize_immutable_blob(
            job,
            reference,
            MaterializationLimits(
                max_source_bytes=16,
                timed_speech_max_request_bytes=16,
                copy_chunk_bytes=2,
                staging_quota_bytes=8,
            ),
        )
    assert child_busy.value.code == "MEDIA_MATERIALIZATION_CAPACITY_BUSY"
    child_release.set()
    child.join(10)
    assert child.exitcode == 0
    assert child_result.get(timeout=1) == "released"

    calls: list[tuple[int, int]] = []
    original_read = store._read_immutable_blob_chunk

    def recorded_read(object_id: UUID, offset: int, size: int) -> bytes:
        calls.append((offset, size))
        return original_read(object_id, offset, size)

    monkeypatch.setattr(store, "_read_immutable_blob_chunk", recorded_read)
    lease = store.materialize_immutable_blob(job, reference, limits)
    assert lease.path.read_bytes() == content
    assert calls == [(0, 2), (2, 2), (4, 2), (6, 1)]
    assert lease.path.stat().st_mode & 0o777 == 0o400
    competing_store = PostgresRuntimeStore(
        lambda: psycopg.connect(DSN), materialization_staging_root=staging_root
    )
    with pytest.raises(MaterializationError, match="capacity") as busy:
        competing_store.materialize_immutable_blob(
            job,
            reference,
            MaterializationLimits(
                max_source_bytes=16,
                timed_speech_max_request_bytes=16,
                copy_chunk_bytes=2,
                staging_quota_bytes=8,
            ),
        )
    assert busy.value.code == "MEDIA_MATERIALIZATION_CAPACITY_BUSY"
    lease.close()
    reopened = competing_store.materialize_immutable_blob(job, reference, limits)
    reopened.close()

    with pytest.raises(MaterializationError, match="quota does not match") as mismatch:
        competing_store.materialize_immutable_blob(
            job,
            reference,
            MaterializationLimits(
                max_source_bytes=16,
                timed_speech_max_request_bytes=16,
                copy_chunk_bytes=2,
                staging_quota_bytes=9,
            ),
        )
    assert mismatch.value.code == "MEDIA_MATERIALIZATION_QUOTA_CONFIGURATION_MISMATCH"

    orphan = staging_root / ".autocut-media-reservations" / ("a" * 32 + ".lease")
    orphan.write_text("15\n", encoding="ascii")
    stale_reclaimed = competing_store.materialize_immutable_blob(job, reference, limits)
    stale_reclaimed.close()
    assert not orphan.exists()

    with pytest.raises(MaterializationError, match="integrity") as foreign:
        store.materialize_immutable_blob(
            Job("bounded-materialization-other-job", "test"), reference, limits
        )
    assert foreign.value.code == "COMMITTED_SOURCE_BLOB_INTEGRITY_FAILED"

    def corrupting_read(object_id: UUID, offset: int, size: int) -> bytes:
        chunk = original_read(object_id, offset, size)
        return b"X" * len(chunk) if offset == 2 else chunk

    monkeypatch.setattr(store, "_read_immutable_blob_chunk", corrupting_read)
    with pytest.raises(MaterializationError, match="integrity") as corrupt:
        store.materialize_immutable_blob(job, reference, limits)
    assert corrupt.value.code == "COMMITTED_SOURCE_BLOB_INTEGRITY_FAILED"
    reservations = staging_root / ".autocut-media-reservations"
    assert list(reservations.iterdir()) == []


def test_same_generation_request_reserves_once_even_concurrently() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("generation-reservation-race", "test")
    request_hash = _digest("generation-request")
    slot = store.claim_command(
        CommandClaim(job, "generation", "GenerateVlmEvidenceCommand", request_hash, execution_kind="generation")
    )
    payload = b'{"request":"reservation-race"}'
    payload_ref = store.put_immutable_blob(
        job,
        content=payload,
        content_hash="sha256:" + hashlib.sha256(payload).hexdigest(),
        media_type="application/json",
    )
    gate = threading.Barrier(2)
    attempts: list[object] = []

    def reserve() -> None:
        gate.wait()
        try:
            attempts.append(
                store.reserve_generation_attempt(
                    slot.command_slot_id,
                    request_hash,
                    provider_id="provider-test",
                    provider_idempotency_key="reservation-race",
                    request_payload=payload_ref,
                )
            )
        except Exception as error:  # pragma: no cover - asserted below
            attempts.append(error)

    workers = [threading.Thread(target=reserve), threading.Thread(target=reserve)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert len(attempts) == 2
    assert len({getattr(item, "attempt_id", None) for item in attempts}) == 1
    assert sum(getattr(item, "is_fresh_reservation", False) for item in attempts) == 1


def test_generation_reservation_binds_exact_provider_and_request_payload() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("generation-request-binding", "test")
    request_hash = _digest("generation-request-binding")
    slot = store.claim_command(
        CommandClaim(job, "generation", "GenerateVlmEvidenceCommand", request_hash, execution_kind="generation")
    )
    payload = b'{"request":"bound"}'
    payload_ref = store.put_immutable_blob(
        job,
        content=payload,
        content_hash="sha256:" + hashlib.sha256(payload).hexdigest(),
        media_type="application/json",
    )
    reserved = store.reserve_generation_attempt(
        slot.command_slot_id,
        request_hash,
        provider_id="provider-test",
        provider_idempotency_key="binding-key",
        request_payload=payload_ref,
    )
    assert reserved.provider_id == "provider-test"
    assert reserved.provider_idempotency_key == "binding-key"
    assert reserved.request_payload == payload_ref

    with pytest.raises(IdempotencyConflictError, match="provider request identity"):
        store.reserve_generation_attempt(
            slot.command_slot_id,
            request_hash,
            provider_id="provider-other",
            provider_idempotency_key="binding-key",
            request_payload=payload_ref,
        )

    foreign_payload = store.put_immutable_blob(
        Job("generation-request-binding-foreign", "test"),
        content=b"foreign",
        content_hash="sha256:" + hashlib.sha256(b"foreign").hexdigest(),
        media_type="application/octet-stream",
    )
    with pytest.raises(BlobIntegrityError, match="request-payload"):
        foreign_job = Job("generation-request-binding-foreign-slot", "test")
        foreign_hash = _digest("generation-request-binding-foreign-slot")
        foreign_slot = store.claim_command(
            CommandClaim(
                foreign_job,
                "generation",
                "GenerateVlmEvidenceCommand",
                foreign_hash,
                execution_kind="generation",
            )
        )
        store.reserve_generation_attempt(
            foreign_slot.command_slot_id,
            foreign_hash,
            provider_id="provider-test",
            provider_idempotency_key="foreign-binding-key",
            request_payload=foreign_payload,
        )


def test_indeterminate_attempt_cannot_blind_retry_and_exact_reconciliation_can_commit() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("generation-reconcile", "test")
    request_hash = _digest("generation-request")
    slot = store.claim_command(
        CommandClaim(job, "generation", "GenerateVlmEvidenceCommand", request_hash, execution_kind="generation")
    )
    payload = b'{"request":"reconcile"}'
    payload_ref = store.put_immutable_blob(
        job,
        content=payload,
        content_hash="sha256:" + hashlib.sha256(payload).hexdigest(),
        media_type="application/json",
    )
    reserved = store.reserve_generation_attempt(
        slot.command_slot_id,
        request_hash,
        provider_id="provider-test",
        provider_idempotency_key="generation-reconcile",
        request_payload=payload_ref,
    )
    dispatched = store.dispatch_generation_attempt(
        reserved.attempt_id,
        expected_version=reserved.version,
        provider_request_id="provider-request-reconcile",
    )
    assert dispatched is not None
    indeterminate = store.mark_generation_indeterminate(
        reserved.attempt_id,
        expected_version=dispatched.version,
        dispatch_lease_token=dispatched.dispatch_lease_token or "",
    )
    replay = store.reserve_generation_attempt(
        slot.command_slot_id,
        request_hash,
        provider_id="provider-test",
        provider_idempotency_key="generation-reconcile",
        request_payload=payload_ref,
    )
    assert replay.state == "indeterminate"
    assert replay.is_fresh_reservation is False
    with pytest.raises(GenerationAttemptStateError):
        store.dispatch_generation_attempt(
            reserved.attempt_id,
            expected_version=indeterminate.version,
        )

    raw = b'{"schema_version":3}'
    raw_ref = store.put_immutable_blob(
        job,
        content=raw,
        content_hash="sha256:" + hashlib.sha256(raw).hexdigest(),
        media_type="application/json",
    )
    reconcile_lease = store.acquire_generation_reconcile_lease(
        reserved.attempt_id,
        expected_version=indeterminate.version,
    )
    assert reconcile_lease is not None
    reconciled = store.reconcile_generation_response(
        reserved.attempt_id,
        expected_version=reconcile_lease.version,
        raw_response=raw_ref,
        dispatch_lease_token=reconcile_lease.dispatch_lease_token or "",
    )
    member = _make_member(
        job.job_key,
        artifact_type="vlm_semantic_pack",
        logical_id="vlm_semantic_pack",
        content="vlm-semantic-pack",
    )
    success = CommandSuccess(slot.command_slot_id, _make_set_hash((member,)), (member,))
    committed = store.commit_generation_success(
        reserved.attempt_id,
        expected_version=reconciled.version,
        success=success,
    )
    assert committed.state == "committed"
    assert committed.raw_response == raw_ref
    assert committed.receipt_id is not None and committed.artifact_set_id is not None
    assert store.commit_generation_success(
        reserved.attempt_id,
        expected_version=reconciled.version,
        success=success,
    ) == committed

    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM runtime.jobs WHERE job_key = %s", (job.job_key,)
            )
            state = cursor.fetchone()[0]
            assert (state.decode() if isinstance(state, bytes) else state) == "running"


def test_provider_request_identity_is_unique_across_generation_attempts() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    attempts = []
    for suffix in ("a", "b"):
        job = Job(f"provider-request-unique-{suffix}", "test")
        request_hash = _digest(f"request-{suffix}")
        slot = store.claim_command(
            CommandClaim(job, "generation", "GenerateVlmEvidenceCommand", request_hash, execution_kind="generation")
        )
        payload = f'{{"request":"{suffix}"}}'.encode()
        payload_ref = store.put_immutable_blob(
            job,
            content=payload,
            content_hash="sha256:" + hashlib.sha256(payload).hexdigest(),
            media_type="application/json",
        )
        attempts.append(
            store.reserve_generation_attempt(
                slot.command_slot_id,
                request_hash,
                provider_id="provider-test",
                provider_idempotency_key=f"provider-attempt-{suffix}",
                request_payload=payload_ref,
            )
        )

    first_dispatch = store.dispatch_generation_attempt(
        attempts[0].attempt_id,
        expected_version=0,
        provider_request_id="provider-request-shared",
    )
    assert first_dispatch is not None
    with pytest.raises(PersistenceConflictError):
        store.dispatch_generation_attempt(
            attempts[1].attempt_id,
            expected_version=0,
            provider_request_id="provider-request-shared",
        )
