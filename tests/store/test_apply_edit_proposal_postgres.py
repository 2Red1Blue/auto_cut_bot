"""Opt-in PostgreSQL transaction proof for the guarded Recipe edit writer."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from autocut_kernel.pipeline import apply_edit_proposal_command as command_module
from autocut_kernel.store import (
    ArtifactMember,
    CommandClaim,
    CommandSuccess,
    PostgresRuntimeStore,
    StaleHeadError,
)
from autocut_kernel.store.models import artifact_set_hash, canonical_payload_hash

from tests.pipeline.test_apply_edit_proposal_command import _prepared_apply_case

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
            pytest.fail("AUTOCUT_TEST_POSTGRES_DSN must name disposable ac_autocut_verify")
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


def _artifact(request, artifact_type: str, logical_id: str, revision: int) -> ArtifactMember:  # type: ignore[no-untyped-def]
    payload = f'{{"revision":{revision},"type":"{artifact_type}"}}'
    return ArtifactMember(
        artifact_type,
        logical_id,
        revision,
        request.artifact_scope,
        canonical_payload_hash(payload),
        payload,
    )


def _parent_members(request, revision: int) -> tuple[ArtifactMember, ...]:  # type: ignore[no-untyped-def]
    return (
        _artifact(request, "physical_edit_compilation_report", "physical_edit_compilation_report", revision),
        _artifact(request, "recipe", request.proposal.base_recipe_ref.logical_id, revision),
        _artifact(request, "physical_edit_admission", "physical_edit_admission", revision),
    )


def _commit_generic_parent(store: PostgresRuntimeStore, request, members, suffix: str):  # type: ignore[no-untyped-def]
    request_hash = "sha256:" + (suffix[0] * 64)
    claimed = store.claim_command(
        CommandClaim(
            request.job,
            f"synthetic-parent:{suffix}",
            f"SyntheticParent:{suffix}",
            request_hash,
            execution_kind="deterministic",
        )
    )
    outcome = store.commit_command_success(
        CommandSuccess(claimed.command_slot_id, artifact_set_hash(members), members)
    )
    assert outcome.receipt_id is not None and outcome.artifact_set_id is not None
    return store.read_committed_artifact_set(
        request.job,
        command_slot_id=claimed.command_slot_id,
        receipt_id=outcome.receipt_id,
        artifact_set_id=outcome.artifact_set_id,
        expected_request_hash=request_hash,
        expected_command_name=f"SyntheticParent:{suffix}",
        expected_execution_kind="deterministic",
    )


def _durable_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    _fixture_store, request, _resolver, _limits = _prepared_apply_case(tmp_path, monkeypatch)
    durable = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    parent_members = _parent_members(request, request.proposal.base_revision)
    parent = _commit_generic_parent(durable, request, parent_members, "a")
    proposal = replace(
        request.proposal,
        base_recipe_ref=parent.references[1],
        base_recipe_set_hash=parent.set_hash,
    )
    request = replace(request, proposal=proposal)
    edited_members = _parent_members(request, request.artifact_revision)
    resolved = SimpleNamespace(
        parent=SimpleNamespace(record=parent),
        request_hash="sha256:" + "e" * 64,
    )
    monkeypatch.setattr(
        command_module,
        "resolve_apply_edit_proposal_request",
        lambda *_args, **_kwargs: resolved,
    )
    monkeypatch.setattr(
        command_module,
        "rebuild_applied_edit_artifacts",
        lambda _resolved: SimpleNamespace(artifacts=edited_members),
    )
    success_hash = artifact_set_hash(edited_members)
    return durable, request, resolved, edited_members, success_hash


def _claim_apply(store: PostgresRuntimeStore, request, request_hash: str):  # type: ignore[no-untyped-def]
    return store.claim_command(
        CommandClaim(
            request.job,
            request.proposal.idempotency_key,
            command_module.APPLY_EDIT_PROPOSAL_COMMAND,
            request_hash,
            execution_kind="deterministic",
        )
    )


def test_writer_commits_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable, request, resolved, edited_members, success_hash = _durable_case(tmp_path, monkeypatch)
    claimed = _claim_apply(durable, request, resolved.request_hash)
    success = CommandSuccess(claimed.command_slot_id, success_hash, edited_members)
    monkeypatch.setattr(command_module, "_open_verified_edited_recipe_commit", lambda _value: success)

    first = durable.commit_apply_edit_proposal_success(
        request,
        object(),
        authority_profile_resolver=None,
        limits=None,
    )
    replay = durable.commit_apply_edit_proposal_success(
        request,
        object(),
        authority_profile_resolver=None,
        limits=None,
    )
    assert first.state == "succeeded"
    assert replay.receipt_id == first.receipt_id
    assert replay.artifact_set_id == first.artifact_set_id


def test_writer_rejects_stale_parent_without_partial_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable, request, resolved, edited_members, success_hash = _durable_case(tmp_path, monkeypatch)
    _commit_generic_parent(
        durable,
        request,
        _parent_members(request, request.artifact_revision),
        "b",
    )
    claimed = _claim_apply(durable, request, resolved.request_hash)
    success = CommandSuccess(claimed.command_slot_id, success_hash, edited_members)
    monkeypatch.setattr(command_module, "_open_verified_edited_recipe_commit", lambda _value: success)
    with pytest.raises(StaleHeadError, match="parent closure is stale"):
        durable.commit_apply_edit_proposal_success(
            request,
            object(),
            authority_profile_resolver=None,
            limits=None,
        )
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM runtime.artifact_sets")
        assert cursor.fetchone() == (2,)
        cursor.execute("SELECT count(*) FROM runtime.command_receipts")
        assert cursor.fetchone() == (2,)
