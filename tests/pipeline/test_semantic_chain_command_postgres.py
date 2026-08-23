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


def test_postgres_semantic_command_persists_one_set_and_replays_without_rebuild() -> None:
    assert DSN is not None
    command = SemanticChainCommand(PostgresRuntimeStore(lambda: psycopg.connect(DSN)))
    first = command.execute(_request())
    replay = command.execute(_request())

    assert first.outcome.state == replay.outcome.state == "succeeded"
    assert replay.outcome.artifact_set_id == first.outcome.artifact_set_id
    assert first.resolved_beat is not None
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM runtime.artifact_sets")
            assert cursor.fetchone() == (1,)
            cursor.execute("SELECT count(*) FROM runtime.artifact_members")
            assert cursor.fetchone() == (3,)
