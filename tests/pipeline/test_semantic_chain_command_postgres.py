"""Opt-in PostgreSQL coverage for the semantic command boundary."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from autocut_kernel.pipeline import SemanticChainCommand
from autocut_kernel.pipeline.semantic_chain_command import _set_hash
from autocut_kernel.store import (
    ArtifactMember,
    CommandClaim,
    CommandSuccess,
    PostgresRuntimeStore,
)
from test_semantic_chain_command import _request

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set AUTOCUT_TEST_POSTGRES_DSN to run disposable PostgreSQL tests"
)


class _FakeRequestStore:
    """Only supplies the helper's in-memory media registration surface."""

    def __init__(self) -> None:
        self.media: dict[tuple[str, str], str] = {}


@pytest.fixture(autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for name in ("0001_runtime_core.sql", "0002_runtime_core_constraints.sql"):
                cursor.execute((Path("packages/autocut-kernel/migrations") / name).read_text())


def test_postgres_semantic_command_reads_exact_upstream_media_evidence_and_persists_one_set() -> (
    None
):
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    command = SemanticChainCommand(store)
    source = _FakeRequestStore()
    request = _request(source)
    evidence_payload = source.media[
        (request.media_job.job_key, request.media_evidence_reference.content_hash)
    ]
    upstream = store.claim_command(
        CommandClaim(
            request.media_job,
            "upstream-media-evidence-v1",
            "upstream_fixture",
            request.request_hash,
        )
    )
    member = ArtifactMember(
        "media_evidence",
        request.media_evidence_reference.logical_id,
        request.media_evidence_reference.revision,
        request.media_evidence_reference.scope,
        request.media_evidence_reference.content_hash,
        evidence_payload,
    )
    store.commit_command_success(
        CommandSuccess(upstream.command_slot_id, _set_hash((member,)), (member,))
    )
    first = command.execute(request)
    replay = command.execute(request)

    assert first.outcome.state == replay.outcome.state == "succeeded"
    assert first.resolved_beat is not None
    assert replay.outcome.artifact_set_id == first.outcome.artifact_set_id
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM runtime.artifact_sets")
            assert cursor.fetchone() == (2,)
            cursor.execute("SELECT count(*) FROM runtime.artifacts")
            assert cursor.fetchone() == (4,)
