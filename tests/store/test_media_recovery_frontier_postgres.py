from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from autocut_kernel.store import (  # noqa: E402
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandSuccess,
    Job,
    PostgresRuntimeStore,
    RuntimeStoreError,
    StoreValidationError,
)
from autocut_kernel.store.media_recovery_frontier import (  # noqa: E402
    MediaRecoveryEntry,
    MediaRecoveryPlan,
)
from autocut_kernel.store.models import artifact_set_hash, canonical_payload_hash  # noqa: E402

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
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for migration in sorted(Path("packages/autocut-kernel/migrations").glob("*.sql")):
                cursor.execute(migration.read_text(encoding="utf-8"))


def _sha(character: str) -> str:
    return "sha256:" + character * 64


def _store() -> PostgresRuntimeStore:
    assert DSN is not None
    return PostgresRuntimeStore(lambda: psycopg.connect(DSN))


def _plan(base: Job, producer_kind: str) -> MediaRecoveryPlan:
    return MediaRecoveryPlan(
        base,
        _sha("2"),
        _sha("3"),
        _sha("4"),
        _sha("5"),
        producer_kind,  # type: ignore[arg-type]
        _sha("6"),
        (_sha("7"), _sha("8")),
    )


def _prepare_success(
    store: PostgresRuntimeStore,
    plan: MediaRecoveryPlan,
    job: Job,
    episode_index: int,
) -> MediaRecoveryEntry:
    command_name = (
        "PrepareTimedMediaEvidence@2.1.3"
        if plan.producer_kind == "local_cpu"
        else "PrepareRuntimeTimedMediaEvidence@1.0.0"
    )
    request_hash = _sha("a" if episode_index == 0 else "b")
    claim = store.claim_command(
        CommandClaim(
            job,
            f"prepare:{episode_index}",
            command_name,
            request_hash,
            execution_kind="deterministic",
        )
    )
    types = (
        (
            "root_media_evidence_bundle",
            "candidate_timed_evidence_index",
            "timed_speech_profile_admission",
            "presentation_timeline_probe",
            "committed_video_to_audio_clock_map_certificate",
        )
        if plan.producer_kind == "local_cpu"
        else (
            "root_media_evidence_bundle",
            "candidate_timed_evidence_index",
            "runtime_timed_speech_capability_admission",
            "presentation_timeline_probe",
            "committed_video_to_audio_clock_map_certificate",
        )
    )
    scope = ArtifactScope("pipeline", "job", job.job_key)
    artifacts: list[ArtifactMember] = []
    for ordinal, artifact_type in enumerate(types):
        payload = json.dumps(
            {"episode_index": episode_index, "ordinal": ordinal},
            separators=(",", ":"),
            sort_keys=True,
        )
        artifacts.append(
            ArtifactMember(
                artifact_type,
                f"episode-{episode_index}-{ordinal}",
                1,
                scope,
                canonical_payload_hash(payload),
                payload,
            )
        )
    outcome = store.commit_command_success(
        CommandSuccess(
            claim.command_slot_id,
            artifact_set_hash(tuple(artifacts)),
            tuple(artifacts),
        )
    )
    assert outcome.receipt_id is not None and outcome.artifact_set_id is not None
    return MediaRecoveryEntry(
        episode_index,
        plan.requirement_sha256s[episode_index],
        job,
        f"prepare:{episode_index}",
        request_hash,
        2,
        outcome.command_slot_id,
        outcome.receipt_id,
        outcome.artifact_set_id,
    )


@pytest.mark.parametrize("producer_kind", ("local_cpu", "pc_cuda"))
def test_media_recovery_frontier_real_store_is_idempotent_and_closed(
    producer_kind: str,
) -> None:
    store = _store()
    base = Job(f"pg-frontier-base-{producer_kind}", "production")
    store.claim_command(
        CommandClaim(base, "base:seed", "SeedMediaFrontier@1.0.0", _sha("1"), execution_kind="deterministic")
    )
    plan = _plan(base, producer_kind)
    assert store.claim_media_recovery_frontier(plan).state == "open"

    first_job = Job(f"pg-frontier-first-{producer_kind}", "production")
    first_entry = _prepare_success(store, plan, first_job, 0)
    open_frontier = store.merge_media_recovery_successes(plan, first_job, (first_entry,))
    assert open_frontier.state == "open"
    assert tuple(entry.episode_index for entry in open_frontier.entries) == (0,)
    assert store.merge_media_recovery_successes(plan, first_job, (first_entry,)) == open_frontier

    second_job = Job(f"pg-frontier-second-{producer_kind}", "production")
    second_entry = _prepare_success(store, plan, second_job, 1)
    complete = store.merge_media_recovery_successes(plan, second_job, (second_entry,))
    assert complete.state == "complete"
    assert complete.finalizer_job == second_job

    final_command = (
        "FinalizeTimedMediaEvidenceBatch@2.1.3"
        if producer_kind == "local_cpu"
        else "FinalizeRuntimeTimedMediaEvidenceBatch@1.0.0"
    )
    final_claim = store.claim_command(
        CommandClaim(second_job, "finalize", final_command, _sha("c"), execution_kind="deterministic")
    )
    scope = ArtifactScope("pipeline", "job", second_job.job_key)
    payload = json.dumps({"frontier": plan.plan_sha256}, separators=(",", ":"), sort_keys=True)
    final_type = (
        "timed_media_evidence_batch"
        if producer_kind == "local_cpu"
        else "runtime_timed_media_evidence_batch"
    )
    final_artifact = ArtifactMember(
        final_type,
        final_type,
        1,
        scope,
        canonical_payload_hash(payload),
        payload,
    )
    final_outcome = store.commit_command_success(
        CommandSuccess(
            final_claim.command_slot_id,
            artifact_set_hash((final_artifact,)),
            (final_artifact,),
        )
    )
    finalized = store.mark_media_recovery_finalized(plan, second_job, final_outcome)
    assert finalized.state == "finalized"
    assert store.mark_media_recovery_finalized(plan, second_job, final_outcome) == finalized


def test_media_recovery_frontier_rejects_wrong_finalizer_command() -> None:
    store = _store()
    base = Job("pg-frontier-wrong-base", "production")
    store.claim_command(
        CommandClaim(base, "base:seed", "SeedMediaFrontier@1.0.0", _sha("1"), execution_kind="deterministic")
    )
    plan = _plan(base, "local_cpu")
    assert store.claim_media_recovery_frontier(plan).state == "open"
    owner = Job("pg-frontier-wrong-owner", "production")
    entry0 = _prepare_success(store, plan, owner, 0)
    entry1 = _prepare_success(store, plan, owner, 1)
    other = Job("pg-frontier-other-participant", "production")
    store.claim_command(
        CommandClaim(
            other,
            "other:seed",
            "SeedMediaFrontier@1.0.0",
            _sha("e"),
            execution_kind="deterministic",
        )
    )
    with pytest.raises(StoreValidationError, match="participant Job"):
        store.merge_media_recovery_successes(plan, other, (entry0,))
    # The second call above intentionally uses a different idempotency key and
    # therefore creates a second command under the same owner.
    assert store.merge_media_recovery_successes(plan, owner, (entry0, entry1)).state == "complete"
    wrong_claim = store.claim_command(
        CommandClaim(owner, "wrong-finalize", "NotTheFinalizer@1.0.0", _sha("d"), execution_kind="deterministic")
    )
    scope = ArtifactScope("pipeline", "job", owner.job_key)
    payload = json.dumps({"frontier": plan.plan_sha256}, separators=(",", ":"), sort_keys=True)
    artifact = ArtifactMember(
        "timed_media_evidence_batch",
        "timed_media_evidence_batch",
        1,
        scope,
        canonical_payload_hash(payload),
        payload,
    )
    outcome = store.commit_command_success(
        CommandSuccess(wrong_claim.command_slot_id, artifact_set_hash((artifact,)), (artifact,))
    )
    with pytest.raises(RuntimeStoreError, match="database operation failed"):
        store.mark_media_recovery_finalized(plan, owner, outcome)


def test_media_recovery_frontier_accepts_base_and_successor_origins() -> None:
    store = _store()
    base = Job("pg-frontier-inherited-base", "production")
    store.claim_command(
        CommandClaim(
            base,
            "base:seed",
            "SeedMediaFrontier@1.0.0",
            _sha("1"),
            execution_kind="deterministic",
        )
    )
    plan = _plan(base, "local_cpu")
    assert store.claim_media_recovery_frontier(plan).state == "open"
    base_entry = _prepare_success(store, plan, base, 0)
    successor = Job("pg-frontier-inherited-successor", "production")
    successor_entry = _prepare_success(store, plan, successor, 1)

    complete = store.merge_media_recovery_successes(
        plan, successor, (base_entry, successor_entry)
    )
    assert complete.state == "complete"
    assert complete.finalizer_job == successor


def test_media_recovery_frontier_serializes_concurrent_last_slots() -> None:
    store = _store()
    base = Job("pg-frontier-race-base", "production")
    store.claim_command(
        CommandClaim(
            base,
            "base:seed",
            "SeedMediaFrontier@1.0.0",
            _sha("1"),
            execution_kind="deterministic",
        )
    )
    plan = MediaRecoveryPlan(
        base,
        _sha("2"),
        _sha("3"),
        _sha("4"),
        _sha("5"),
        "local_cpu",
        _sha("6"),
        (_sha("7"), _sha("8"), _sha("9")),
    )
    assert store.claim_media_recovery_frontier(plan).state == "open"
    base_entry = _prepare_success(store, plan, base, 0)
    assert store.merge_media_recovery_successes(plan, base, (base_entry,)).state == "open"
    first_job = Job("pg-frontier-race-first", "production")
    second_job = Job("pg-frontier-race-second", "production")
    first_entry = _prepare_success(store, plan, first_job, 1)
    second_entry = _prepare_success(store, plan, second_job, 2)

    def merge(job: Job, entry: MediaRecoveryEntry):
        return store.merge_media_recovery_successes(plan, job, (entry,))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(merge, first_job, first_entry),
            executor.submit(merge, second_job, second_entry),
        )
        results = tuple(future.result() for future in futures)

    assert any(result.state == "complete" for result in results)
    final = store.claim_media_recovery_frontier(plan)
    assert final.state == "complete"
    assert final.finalizer_job in (first_job, second_job)
    assert tuple(entry.episode_index for entry in final.entries) == (0, 1, 2)
