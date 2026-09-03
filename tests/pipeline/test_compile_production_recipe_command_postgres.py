"""Opt-in PostgreSQL proof for the Stage 4 production Recipe transaction.

Set ``AUTOCUT_TEST_POSTGRES_DSN`` to the disposable ``ac_autocut_verify``
database.  The committed Stage 1-3/media fixture remains in memory so this
module proves only the Stage 4 claim/commit/exact-reread boundary; it does not
claim a durable whole-pipeline fixture.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from autocut_kernel.pipeline.compile_production_recipe_command import (
    COMPILE_PRODUCTION_RECIPE_COMMAND,
    CompileProductionRecipeCommand,
)
from autocut_kernel.store import PostgresRuntimeStore

from tests.authority.editorial_media_fixture import editorial_timed_media_case
from tests.pipeline.test_compile_production_recipe_command import (
    _install_non_dialogue_blueprint_projection,
    _request,
)

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set AUTOCUT_TEST_POSTGRES_DSN to run disposable PostgreSQL tests",
)
MIGRATIONS = Path("packages/autocut-kernel/migrations")


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
            ):
                cursor.execute((MIGRATIONS / name).read_text())


class _PostgresStage4Store:
    """Route only Stage 4 persistence to PostgreSQL."""

    def __init__(self, predecessor_store: object, durable: PostgresRuntimeStore) -> None:
        self._predecessor_store = predecessor_store
        self._durable = durable

    def __getattr__(self, name: str) -> object:
        return getattr(self._predecessor_store, name)

    def claim_command(self, claim):  # type: ignore[no-untyped-def]
        return self._durable.claim_command(claim)

    def commit_production_recipe_success(self, verified):  # type: ignore[no-untyped-def]
        return self._durable.commit_production_recipe_success(verified)

    def commit_command_rejection(self, rejection):  # type: ignore[no-untyped-def]
        return self._durable.commit_command_rejection(rejection)

    def read_committed_artifact_set(self, job, **expected):  # type: ignore[no-untyped-def]
        if expected["expected_command_name"] == COMPILE_PRODUCTION_RECIPE_COMMAND:
            return self._durable.read_committed_artifact_set(job, **expected)
        return self._predecessor_store.read_committed_artifact_set(job, **expected)  # type: ignore[attr-defined]


def test_postgres_commit_immediate_reread_and_restart_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert DSN is not None
    _install_non_dialogue_blueprint_projection(monkeypatch)
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    predecessor, *_rest, resolver, limits = case
    request = _request(case)
    store = _PostgresStage4Store(
        predecessor,
        PostgresRuntimeStore(lambda: psycopg.connect(DSN)),
    )

    first = CompileProductionRecipeCommand(store, resolver, limits).execute(request)

    assert first.outcome.state == "succeeded"
    assert first.committed is not None
    assert first.committed.admission.render_authorized is True
    assert tuple(member.reference.artifact_type for member in first.committed.record.members) == (
        "physical_edit_compilation_report",
        *("recipe" for _ in first.committed.recipes),
        "physical_edit_admission",
    )
    assert any(": " in member.payload_json for member in first.committed.record.members)

    restarted = _PostgresStage4Store(
        predecessor,
        PostgresRuntimeStore(lambda: psycopg.connect(DSN)),
    )
    replay = CompileProductionRecipeCommand(restarted, resolver, limits).execute(request)

    assert replay.outcome.state == "succeeded"
    assert replay.outcome.is_fresh_claim is False
    assert replay.outcome.receipt_id == first.outcome.receipt_id
    assert replay.outcome.artifact_set_id == first.outcome.artifact_set_id
    assert replay.committed == first.committed
