"""Unit guard for the protected production Recipe edit Store writer."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from autocut_kernel.pipeline import apply_edit_proposal_command as command_module
from autocut_kernel.store import (
    ArtifactMember,
    ArtifactScope,
    CommandOutcome,
    CommandStateError,
    CommandSuccess,
    Job,
    PostgresRuntimeStore,
    StaleHeadError,
)
from autocut_kernel.store.models import (
    CommittedArtifactMemberReference,
    artifact_set_hash,
    canonical_payload_hash,
)


def test_apply_edit_proposal_cannot_use_generic_success_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct CommandSuccess must never bypass the edit command capability."""
    job_id = UUID("00000000-0000-0000-0000-000000000001")
    slot_id = UUID("00000000-0000-0000-0000-000000000002")
    request_hash = "sha256:" + "a" * 64
    member = ArtifactMember(
        "recipe",
        "production_recipe@story-1",
        2,
        ArtifactScope("pipeline", "job", "edit-guard-job"),
        canonical_payload_hash('{"recipe":"forged"}'),
        '{"recipe":"forged"}',
    )
    success = CommandSuccess(slot_id, artifact_set_hash((member,)), (member,))
    cursor = Mock()
    cursor.fetchone.side_effect = (
        (job_id,),
        ("running",),
        (job_id, "running", "ApplyEditProposalCommand@1", request_hash),
    )
    connection = Mock()
    connection.cursor.return_value = cursor
    store = PostgresRuntimeStore(lambda: connection)
    writer = Mock(side_effect=AssertionError("protected writer must be unreachable"))
    monkeypatch.setattr(store, "_write_success", writer)

    with pytest.raises(CommandStateError, match="protected edit writer"):
        store.commit_command_success(success)

    writer.assert_not_called()
    connection.rollback.assert_called_once()


def _edit_writer_case() -> tuple[
    object,
    object,
    CommandSuccess,
    tuple[object, ...],
    Job,
    UUID,
    str,
]:
    """Build a minimal command-owned closure for the Store writer seam."""
    job = Job("edit-writer-job", "test")
    job_id = UUID("00000000-0000-0000-0000-000000000011")
    slot_id = UUID("00000000-0000-0000-0000-000000000012")
    receipt_id = UUID("00000000-0000-0000-0000-000000000013")
    set_id = UUID("00000000-0000-0000-0000-000000000014")
    request_hash = "sha256:" + "b" * 64
    parent_set_hash = "sha256:" + "c" * 64
    scope = ArtifactScope("pipeline", "job", job.job_key)
    layout = (
        ("physical_edit_compilation_report", "physical_edit_compilation_report"),
        ("recipe", "production_recipe@story-1"),
        ("physical_edit_admission", "physical_edit_admission"),
    )
    parent_references = tuple(
        CommittedArtifactMemberReference(
            receipt_id,
            set_id,
            ordinal,
            scope,
            artifact_type,
            logical_id,
            1,
            canonical_payload_hash("{}"),
        )
        for ordinal, (artifact_type, logical_id) in enumerate(layout)
    )
    artifacts = tuple(
        ArtifactMember(
            artifact_type,
            logical_id,
            2,
            scope,
            canonical_payload_hash("{}"),
            "{}",
        )
        for artifact_type, logical_id in layout
    )
    success = CommandSuccess(slot_id, artifact_set_hash(artifacts), artifacts)
    record = SimpleNamespace(
        job=job,
        set_hash=parent_set_hash,
        members=tuple(SimpleNamespace(reference=reference) for reference in parent_references),
    )
    resolved = SimpleNamespace(parent=SimpleNamespace(record=record), request_hash=request_hash)

    class _Request:
        def __init__(self) -> None:
            self.job = job
            self.proposal = SimpleNamespace(
                base_recipe_set_hash=parent_set_hash,
                base_recipe_ref=parent_references[1],
            )

    return _Request(), resolved, success, parent_references, job, job_id, request_hash


def test_edit_writer_locks_every_parent_member_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, resolved, success, parent_references, job, job_id, request_hash = _edit_writer_case()
    cursor = Mock()
    cursor.fetchone.side_effect = (
        (job_id,),
        ("running",),
        (job_id, "running", "ApplyEditProposalCommand@1", request_hash),
        ("deterministic",),
        (job.job_key, job.profile),
        *( (1,) for _ in parent_references ),
    )
    connection = Mock()
    connection.cursor.return_value = cursor
    store = PostgresRuntimeStore(lambda: connection)
    committed = CommandOutcome(
        success.command_slot_id,
        "succeeded",
        UUID("00000000-0000-0000-0000-000000000015"),
        UUID("00000000-0000-0000-0000-000000000016"),
        job_id=job_id,
    )
    writer = Mock(return_value=committed)
    monkeypatch.setattr(store, "_write_success", writer)
    monkeypatch.setattr(command_module, "ApplyEditProposalRequest", type(request))
    monkeypatch.setattr(command_module, "_open_verified_edited_recipe_commit", lambda _value: success)
    monkeypatch.setattr(
        command_module,
        "resolve_apply_edit_proposal_request",
        lambda *_args, **_kwargs: resolved,
    )
    monkeypatch.setattr(
        command_module,
        "rebuild_applied_edit_artifacts",
        lambda actual: SimpleNamespace(artifacts=success.artifacts) if actual is resolved else None,
    )

    assert store.commit_apply_edit_proposal_success(
        request,
        object(),
        authority_profile_resolver=object(),
        limits=object(),
    ) == committed

    writer.assert_called_once_with(cursor, success, job_id)
    parent_head_queries = [
        parameters
        for sql, parameters in (call.args for call in cursor.execute.call_args_list)
        if "runtime.logical_heads AS head" in sql
    ]
    assert len(parent_head_queries) == len(parent_references)
    assert tuple(parameters[7:10] for parameters in parent_head_queries) == tuple(
        (reference.artifact_set_id, reference.receipt_id, reference.member_ordinal)
        for reference in parent_references
    )


def test_edit_writer_stale_parent_never_reaches_shared_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, resolved, success, _parent_references, job, job_id, request_hash = _edit_writer_case()
    cursor = Mock()
    cursor.fetchone.side_effect = (
        (job_id,),
        ("running",),
        (job_id, "running", "ApplyEditProposalCommand@1", request_hash),
        ("deterministic",),
        (job.job_key, job.profile),
        None,
    )
    connection = Mock()
    connection.cursor.return_value = cursor
    store = PostgresRuntimeStore(lambda: connection)
    writer = Mock(side_effect=AssertionError("stale parent must not write"))
    monkeypatch.setattr(store, "_write_success", writer)
    monkeypatch.setattr(command_module, "ApplyEditProposalRequest", type(request))
    monkeypatch.setattr(command_module, "_open_verified_edited_recipe_commit", lambda _value: success)
    monkeypatch.setattr(
        command_module,
        "resolve_apply_edit_proposal_request",
        lambda *_args, **_kwargs: resolved,
    )
    monkeypatch.setattr(
        command_module,
        "rebuild_applied_edit_artifacts",
        lambda actual: SimpleNamespace(artifacts=success.artifacts) if actual is resolved else None,
    )

    with pytest.raises(StaleHeadError, match="parent closure is stale"):
        store.commit_apply_edit_proposal_success(
            request,
            object(),
            authority_profile_resolver=object(),
            limits=object(),
        )

    writer.assert_not_called()
    connection.rollback.assert_called_once()
