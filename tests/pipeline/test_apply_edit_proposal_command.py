from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from autocut_kernel.pipeline.apply_edit_proposal_command import (
    APPLY_EDIT_PROPOSAL_COMMAND,
    EDITED_RECIPE_STALE_PARENT,
    ApplyEditProposalCommand,
    ApplyEditProposalError,
    ApplyEditProposalRequest,
    _open_verified_edited_recipe_commit,
)
from autocut_kernel.pipeline.build_span_variant_set_command import BuildSpanVariantSetCommand
from autocut_kernel.pipeline.edit_proposal import (
    EditProposal,
    EditProposalActor,
    SelectVariantOperation,
)
from autocut_kernel.store.errors import StaleHeadError
from autocut_kernel.store.models import (
    CommandOutcome,
    CommittedArtifactMemberReference,
    PersistedCommittedArtifactMember,
    PersistedCommittedArtifactSet,
)

from tests.pipeline.test_build_span_variant_set_command import _prepared_case, _SpanVariantStore


class _EditedRecipeStore(_SpanVariantStore):
    """The existing SpanVariant fixture with the one new guarded commit seam."""

    def __init__(self, base: object) -> None:
        super().__init__(base)
        self.edited_record: PersistedCommittedArtifactSet | None = None

    def commit_apply_edit_proposal_success(  # type: ignore[no-untyped-def]
        self,
        request,
        issued_capability,
        *,
        authority_profile_resolver,
        limits,
    ):
        assert authority_profile_resolver is not None and limits is not None
        success = _open_verified_edited_recipe_commit(issued_capability)
        claim = self.claims[-1]
        assert claim.command_name == APPLY_EDIT_PROPOSAL_COMMAND
        assert self.stage4_record is not None
        receipt_id, artifact_set_id = uuid4(), uuid4()
        outcome = CommandOutcome(
            success.command_slot_id,
            "succeeded",
            receipt_id=receipt_id,
            artifact_set_id=artifact_set_id,
            job_id=self.stage4_record.job_id,
        )
        members = tuple(
            PersistedCommittedArtifactMember(
                CommittedArtifactMemberReference(
                    receipt_id,
                    artifact_set_id,
                    ordinal,
                    artifact.scope,
                    artifact.artifact_type,
                    artifact.logical_id,
                    artifact.revision,
                    artifact.content_hash,
                ),
                artifact.payload_json,
                success.command_slot_id,
            )
            for ordinal, artifact in enumerate(success.artifacts)
        )
        self.edited_record = PersistedCommittedArtifactSet(
            claim.job,
            self.stage4_record.job_id,
            success.command_slot_id,
            receipt_id,
            artifact_set_id,
            claim.request_hash,
            claim.command_name,
            claim.execution_kind,
            success.set_hash,
            members,
        )
        self.outcomes[claim.idempotency_key] = outcome
        return outcome

    def read_committed_artifact_set(self, job, **expected):  # type: ignore[no-untyped-def]
        if expected["expected_command_name"] == APPLY_EDIT_PROPOSAL_COMMAND:
            assert self.edited_record is not None
            return self.edited_record
        return super().read_committed_artifact_set(job, **expected)


def _prepared_apply_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    _store, variant_request, resolver, limits, _parent = _prepared_case(tmp_path, monkeypatch)
    assert _store.stage4_record is not None
    store = _EditedRecipeStore(_store.base)
    store.stage4_record = _store.stage4_record
    store.outcomes = dict(_store.outcomes)
    variants = BuildSpanVariantSetCommand(store, resolver, limits).execute(variant_request)
    assert variants.outcome.state == "succeeded" and variants.committed is not None
    base_recipe_ref = store.stage4_record.references[1]
    selected = variants.committed.value.entries[0].variants[0]
    proposal = EditProposal(
        uuid4(),
        base_recipe_ref,
        base_recipe_ref.revision,
        store.stage4_record.set_hash,
        (SelectVariantOperation(variants.committed.record.references[0], selected.variant_id),),
        "Rebuild the same committed variant through the guarded edit path.",
        EditProposalActor("human", "operator-42"),
        "2026-09-09T00:00:00Z",
    )
    request = ApplyEditProposalRequest(
        variant_request.job,
        proposal,
        variant_request.parent_request,
        variant_request.parent_outcome,
        variant_request,
        variants.outcome,
    )
    return store, request, resolver, limits


def test_apply_creates_new_revision_and_replays_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, resolver, limits = _prepared_apply_case(tmp_path, monkeypatch)

    first = ApplyEditProposalCommand(store, resolver, limits).execute(request)

    assert first.outcome.state == "succeeded" and first.committed is not None
    assert first.committed.record is store.edited_record
    assert all(
        member.reference.revision == request.proposal.base_revision + 1
        for member in first.committed.record.members
    )
    assert first.committed.report.input_binding_sha256 != request.span_variant_request.expected_parent_request_hash
    replay = ApplyEditProposalCommand(store, resolver, limits).execute(request)
    assert replay.outcome.is_fresh_claim is False
    assert replay.committed == first.committed


def test_parent_or_variant_mismatch_rejects_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, resolver, limits = _prepared_apply_case(tmp_path, monkeypatch)
    before = len(store.claims)
    mismatched = replace(request, proposal=replace(request.proposal, base_recipe_set_hash="sha256:" + "1" * 64))

    with pytest.raises(ApplyEditProposalError, match="exact parent"):
        ApplyEditProposalCommand(store, resolver, limits).execute(mismatched)

    assert len(store.claims) == before


def test_stale_parent_is_persisted_as_a_terminal_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, resolver, limits = _prepared_apply_case(tmp_path, monkeypatch)

    def stale(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise StaleHeadError("expected revision 2, received 1")

    monkeypatch.setattr(store, "commit_apply_edit_proposal_success", stale)
    result = ApplyEditProposalCommand(store, resolver, limits).execute(request)

    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == EDITED_RECIPE_STALE_PARENT
    assert result.committed is None
