"""Opt-in PostgreSQL coverage for the semantic command boundary."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from autocut_kernel.pipeline import SemanticChainCommand
from autocut_kernel.store import PostgresRuntimeStore
from test_semantic_chain_command import _request

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set AUTOCUT_TEST_POSTGRES_DSN to run disposable PostgreSQL tests"
)


@pytest.fixture(autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for name in ("0001_runtime_core.sql", "0002_runtime_core_constraints.sql"):
                cursor.execute((Path("packages/autocut-kernel/migrations") / name).read_text())


def test_postgres_semantic_command_denies_an_uncommitted_exact_evidence_reference() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    command = SemanticChainCommand(store)
    # The helper registers media only in its fake Store. PostgreSQL has no
    # matching upstream committed artifact, so this must become a receipt-only
    # expected denial rather than accepting caller-provided evidence.
    request = _request(_FakeRequestStore())
    first = command.execute(request)
    replay = command.execute(request)

    assert first.outcome.state == replay.outcome.state == "denied"
    assert first.outcome.artifact_set_id is None
    assert replay.outcome.receipt_id == first.outcome.receipt_id
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM runtime.artifact_sets")
            assert cursor.fetchone() == (0,)
            cursor.execute("SELECT count(*) FROM runtime.artifact_members")
            assert cursor.fetchone() == (0,)


class _FakeRequestStore:
    """Only supplies the helper's in-memory media registration surface."""

    def __init__(self) -> None:
        self.media: dict[tuple[str, str], str] = {}
