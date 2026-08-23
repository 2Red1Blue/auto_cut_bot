"""Opt-in true-PostgreSQL integration test using the tracked migrations.

Set ``AUTOCUT_TEST_POSTGRES_DSN`` to a disposable database.  This test never
creates private substitute tables; it applies the real runtime migrations.
"""

import hashlib
import json
import os
import threading
from pathlib import Path

import pytest
from autocut_kernel.store import (
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandRejection,
    CommandStateError,
    CommandSuccess,
    IdempotencyConflictError,
    Job,
    JobProfileMismatchError,
    MediaEvidenceIntegrityError,
    MediaEvidenceReference,
    MediaEvidenceUnavailableError,
    MediaOutputsUnavailableError,
    PostgresRuntimeStore,
    RecipeIntegrityError,
    RecipeReference,
    RecipeUnavailableError,
)

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set AUTOCUT_TEST_POSTGRES_DSN to run disposable PostgreSQL tests"
)


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
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for name in ("0001_runtime_core.sql", "0002_runtime_core_constraints.sql"):
                cursor.execute((Path("packages/autocut-kernel/migrations") / name).read_text())


# ---------------------------------------------------------------------------
# Basic lifecycle
# ---------------------------------------------------------------------------


def test_claim_success_and_replay_are_one_durable_command() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("postgres-fixture-job", "test")
    claim = CommandClaim(job, "preflight-1", "media_preflight", _digest("request"))
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
    claim = CommandClaim(Job("running-replay-job", "test"), "preflight", "media_preflight", _digest("request"))

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
        CommandClaim(job, "preflight-2", "media_preflight", _digest("request-2"))
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
    running = store.claim_command(CommandClaim(job, "recipe", "local_media", _digest("request")))
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
    running = store.claim_command(CommandClaim(job, "recipe", "local_media", _digest("request")))
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
    running = store.claim_command(CommandClaim(job, "recipe-1", "local_media", _digest("request-1")))
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
                    (command_slot_id, job_id, idempotency_key, command_name, request_hash, state, completed_at)
                VALUES (gen_random_uuid(), %s, 'recipe-2', 'local_media', %s, 'succeeded', transaction_timestamp())
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
    running = store.claim_command(CommandClaim(job, "evidence", "local_media", _digest("request")))
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
    running = store.claim_command(CommandClaim(job, "evidence", "local_media", _digest("request")))
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
        CommandClaim(job, "local-media", "local_media_command", _digest("request"))
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
        CommandClaim(job, "local-media", "local_media_command", _digest("request"))
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
        CommandClaim(job, "local-media", "local_media_command", _digest("request"))
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
    claim = CommandClaim(job, "same-key", "preflight", _digest("req"))

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

    running = store.claim_command(CommandClaim(job, "key-1", "cmd-a", _digest("req-a")))
    assert running.state == "running"

    with pytest.raises(IdempotencyConflictError, match="already claimed by a different command"):
        store.claim_command(CommandClaim(job, "key-1", "cmd-b", _digest("req-a")))

    with pytest.raises(IdempotencyConflictError, match="already claimed by a different command"):
        store.claim_command(CommandClaim(job, "key-1", "cmd-a", _digest("req-b")))


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
    claim = CommandClaim(job, "cmd-1", "preflight", _digest("req"))

    claim_b = CommandClaim(job, "cmd-2", "preflight", _digest("req-2"))
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
        CommandClaim(job, "fail-cmd", "preflight", _digest("req"))
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
                "INSERT INTO runtime.command_slots (command_slot_id, job_id, idempotency_key, command_name, request_hash, state)"
                " VALUES (gen_random_uuid(), %s, 'ck', 'preflight', %s, 'running') RETURNING command_slot_id",
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
        CommandClaim(job, "cmd", "preflight", _digest("req"))
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
        CommandClaim(job, "cmd", "preflight", _digest("req"))
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
        CommandClaim(job, "cmd", "preflight", _digest("req"))
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
    running = store.claim_command(CommandClaim(job, "cmd", "preflight", _digest("req")))
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
                "INSERT INTO runtime.command_slots (command_slot_id, job_id, idempotency_key, command_name, request_hash, state)"
                " VALUES (gen_random_uuid(), %s, 'ik', 'preflight', %s, 'running') RETURNING command_slot_id",
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
                "INSERT INTO runtime.command_slots (command_slot_id, job_id, idempotency_key, command_name, request_hash, state)"
                " VALUES (gen_random_uuid(), %s, 'a', 'preflight', %s, 'running') RETURNING command_slot_id",
                (job_id, _digest("req-a")),
            )
            slot_a = cur.fetchone()[0]
            # Slot B
            cur.execute(
                "INSERT INTO runtime.command_slots (command_slot_id, job_id, idempotency_key, command_name, request_hash, state)"
                " VALUES (gen_random_uuid(), %s, 'b', 'preflight', %s, 'running') RETURNING command_slot_id",
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
    r1 = store.claim_command(CommandClaim(job1, "cmd1", "preflight", _digest("r1")))
    store.commit_command_success(CommandSuccess(r1.command_slot_id, set_hash, (member,)))

    # Job 2 — same set_hash, different job
    job2 = Job("same-hash-job-2", "test")
    member2 = _make_member("shared-scope")
    r2 = store.claim_command(CommandClaim(job2, "cmd2", "preflight", _digest("r2")))
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
                    " (command_slot_id, job_id, idempotency_key, command_name, request_hash, state)"
                    " VALUES (gen_random_uuid(), %s, %s, 'preflight', %s, 'running')"
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
# Job terminal state is not overwritten
# ---------------------------------------------------------------------------


def test_terminal_job_closes_fresh_keys_but_replays_existing_keys() -> None:
    """A terminal Job is closed to new claims, but pre-existing slots finish."""
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))

    job = Job("terminal-job", "test")

    # Both slots are claimed while the Job is still running.
    r1 = store.claim_command(CommandClaim(job, "cmd1", "preflight", _digest("r1")))
    r2 = store.claim_command(CommandClaim(job, "cmd2", "preflight", _digest("r2")))
    member = _make_member(job.job_key)
    set_hash = _make_set_hash((member,))
    first = store.commit_command_success(CommandSuccess(r1.command_slot_id, set_hash, (member,)))

    assert store.claim_command(CommandClaim(job, "cmd1", "preflight", _digest("r1"))) == first
    with pytest.raises(CommandStateError, match="job is already terminal"):
        store.claim_command(CommandClaim(job, "fresh", "preflight", _digest("fresh")))

    # A previously claimed command may still complete, without changing the
    # Job's terminal state.
    store.commit_command_rejection(
        CommandRejection(r2.command_slot_id, "DENY", '{"r":"x"}')
    )

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
                " (command_slot_id, job_id, idempotency_key, command_name, request_hash, state, completed_at)"
                " VALUES (gen_random_uuid(), %s, 'key', 'preflight', %s, %s,"
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


def test_terminal_job_and_fresh_claim_are_serialized() -> None:
    """A fresh claim blocked behind the aggregate lock observes the committed terminal state."""
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("terminal-race-job", "test")
    first = store.claim_command(CommandClaim(job, "first", "preflight", _digest("first")))

    terminal_connection = psycopg.connect(DSN)
    terminal_cursor = terminal_connection.cursor()
    terminal_cursor.execute("SELECT job_id FROM runtime.jobs WHERE job_key = %s FOR UPDATE", (job.job_key,))
    terminal_cursor.execute(
        "UPDATE runtime.jobs SET state = 'denied' WHERE job_key = %s", (job.job_key,)
    )

    outcome: list[object] = []

    def claim_fresh() -> None:
        try:
            outcome.append(
                store.claim_command(CommandClaim(job, "fresh", "preflight", _digest("fresh")))
            )
        except Exception as error:  # pragma: no cover - asserted below
            outcome.append(error)

    worker = threading.Thread(target=claim_fresh)
    worker.start()
    terminal_connection.commit()
    worker.join()
    terminal_cursor.close()
    terminal_connection.close()

    assert len(outcome) == 1
    assert isinstance(outcome[0], CommandStateError)
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM runtime.command_slots WHERE job_id = %s AND idempotency_key = 'fresh'",
                (first.job_id,),
            )
            assert cur.fetchone() is None


def test_revision_race_returns_one_success_and_one_stale_head() -> None:
    assert DSN is not None
    store_a = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    store_b = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("revision-race-job", "test")
    first = store_a.claim_command(CommandClaim(job, "one", "preflight", _digest("one")))
    second = store_b.claim_command(CommandClaim(job, "two", "preflight", _digest("two")))
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
        CommandClaim(job, "cmd", "preflight", _digest("req"))
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
        CommandClaim(job, "cmd", "preflight", _digest("req"))
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
