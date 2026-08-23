"""Opt-in true-PostgreSQL integration test using the tracked migrations.

Set ``AUTOCUT_TEST_POSTGRES_DSN`` to a disposable database.  This test never
creates private substitute tables; it applies the real runtime migrations.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest
from autocut_kernel.store import (
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandRejection,
    CommandSuccess,
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


@pytest.fixture(autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for name in ("0001_runtime_core.sql", "0002_runtime_core_constraints.sql"):
                cursor.execute((Path("packages/autocut-kernel/migrations") / name).read_text())


def test_claim_success_and_replay_are_one_durable_command() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("postgres-fixture-job", "test")
    claim = CommandClaim(job, "preflight-1", "media_preflight", _digest("request"))
    running = store.claim_command(claim)
    assert running.state == "running"

    member = ArtifactMember(
        artifact_type="media_evidence",
        logical_id="preflight",
        revision=1,
        scope=ArtifactScope("pipeline", "job", job.job_key),
        content_hash=_digest("evidence"),
        payload_json='{"complete":true}',
    )
    canonical = [
        {
            "artifact_type": member.artifact_type,
            "content_hash": member.content_hash,
            "logical_id": member.logical_id,
            "payload_json": {"complete": True},
            "revision": 1,
            "scope": {"key": job.job_key, "kind": "job", "namespace": "pipeline"},
        }
    ]
    set_hash = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
    )
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
