"""Opt-in PostgreSQL coverage for the local media command adapter."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
from autocut_kernel.pipeline import LocalMediaCommand
from autocut_kernel.store import ArtifactScope, Job, PostgresRuntimeStore
from test_local_media_command import _Port, _request

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


def test_postgres_command_persists_one_success_set_and_no_denial_set(tmp_path: Path) -> None:
    assert DSN is not None
    port = _Port()
    command = LocalMediaCommand(PostgresRuntimeStore(lambda: psycopg.connect(DSN)), port=port)  # type: ignore[arg-type]
    success_dir = tmp_path / "success"
    success_dir.mkdir()
    success_request = _request(success_dir)
    succeeded = command.execute(success_request)
    replay = command.execute(success_request)

    denial_dir = tmp_path / "denial"
    denial_dir.mkdir()
    denial_request = replace(
        _request(denial_dir, minimum_duration_pts=31),
        job=Job("local-command-denial-job", "test"),
        idempotency_key="local-media-denial-v1",
        artifact_scope=ArtifactScope("pipeline", "job", "local-command-denial-job"),
    )
    denied = command.execute(denial_request)

    assert succeeded.state == "succeeded"
    assert replay.artifact_set_id == succeeded.artifact_set_id
    assert denied.state == "denied"
    assert denied.artifact_set_id is None
    assert port.calls == 2
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM runtime.artifact_sets")
            assert cursor.fetchone() == (1,)
            cursor.execute("SELECT count(*) FROM runtime.command_receipts WHERE outcome = 'succeeded'")
            assert cursor.fetchone() == (1,)
            cursor.execute("SELECT count(*) FROM runtime.command_receipts WHERE outcome = 'denied'")
            assert cursor.fetchone() == (1,)
