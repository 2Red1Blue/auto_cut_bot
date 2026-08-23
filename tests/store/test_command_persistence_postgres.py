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
    PostgresRuntimeStore,
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

    member = _make_member(job.job_key)
    set_hash = _make_set_hash((member,))
    succeeded = store.commit_command_success(
        CommandSuccess(running.command_slot_id, set_hash, (member,))
    )
    assert succeeded.state == "succeeded"
    assert succeeded.receipt_id is not None and succeeded.artifact_set_id is not None
    assert store.claim_command(claim).artifact_set_id == succeeded.artifact_set_id


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
            assert cur.fetchone()[0] == "succeeded"


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
