from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from autocut_kernel.physical_edit.span_variant_set import SpanVariantSetPolicy
from autocut_kernel.pipeline import build_span_variant_set_command as command_module
from autocut_kernel.pipeline.build_span_variant_set_command import (
    BUILD_SPAN_VARIANT_SET_COMMAND,
    SPAN_VARIANT_SET_PAYLOAD_TOO_LARGE,
    BuildSpanVariantSetCommand,
    BuildSpanVariantSetError,
    BuildSpanVariantSetRequest,
)
from autocut_kernel.pipeline.compile_production_recipe_command import (
    CompileProductionRecipeCommand,
)
from autocut_kernel.store.models import (
    CommandOutcome,
    CommittedArtifactMemberReference,
    PersistedCommittedArtifactMember,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
    canonical_payload_hash,
)

from tests.authority.editorial_media_fixture import editorial_timed_media_case
from tests.pipeline.test_compile_production_recipe_command import (
    _install_non_dialogue_blueprint_projection,
    _Stage4Store,
)
from tests.pipeline.test_compile_production_recipe_command import (
    _request as parent_request,
)


class _SpanVariantStore(_Stage4Store):
    def __init__(self, base: object) -> None:
        super().__init__(base)
        self.variant_record: PersistedCommittedArtifactSet | None = None

    def commit_span_variant_set_success(  # type: ignore[no-untyped-def]
        self,
        request,
        success,
        *,
        authority_profile_resolver,
        limits,
    ):
        assert type(request) is BuildSpanVariantSetRequest
        assert authority_profile_resolver is not None
        assert limits is not None
        assert self.stage4_record is not None
        claim = self.claims[-1]
        assert claim.command_name == BUILD_SPAN_VARIANT_SET_COMMAND
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
        self.variant_record = PersistedCommittedArtifactSet(
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
        if expected["expected_command_name"] == BUILD_SPAN_VARIANT_SET_COMMAND:
            assert self.variant_record is not None
            return self.variant_record
        return super().read_committed_artifact_set(job, **expected)


def _prepared_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    _install_non_dialogue_blueprint_projection(monkeypatch)
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    base, *_rest, resolver, limits = case
    store = _SpanVariantStore(base)
    parent = parent_request(case)
    parent_result = CompileProductionRecipeCommand(store, resolver, limits).execute(parent)
    assert parent_result.outcome.state == "succeeded"
    assert parent_result.committed is not None
    assert store.stage4_record is not None
    record = store.stage4_record
    refs = record.references
    request = BuildSpanVariantSetRequest(
        parent.job,
        "stage4:span-variants:one",
        parent.artifact_scope,
        parent.artifact_revision,
        parent,
        parent_result.outcome,
        record.request_hash,
        record.set_hash,
        refs[0],
        refs[1:-1],
        refs[-1],
        SpanVariantSetPolicy(max_variants=3),
    )
    return store, request, resolver, limits, parent_result.committed


def test_success_is_one_child_artifact_and_replay_rebuilds_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, resolver, limits, parent_committed = _prepared_case(tmp_path, monkeypatch)
    assert store.stage4_record is not None
    original_parent = store.stage4_record
    original = command_module.compile_candidate_av_span_variants
    calls = 0

    def counting_compile(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(command_module, "compile_candidate_av_span_variants", counting_compile)
    first = BuildSpanVariantSetCommand(store, resolver, limits).execute(request)

    assert first.outcome.state == "succeeded"
    assert first.committed is not None
    assert first.committed.record is store.variant_record
    assert store.stage4_record == original_parent
    assert tuple(member.reference.artifact_type for member in first.committed.record.members) == (
        "span_variant_set",
    )
    assert first.committed.value.parent_request_sha256 == request.expected_parent_request_hash
    assert first.committed.value.parent_artifact_set_sha256 == request.expected_parent_set_hash
    for variant_entry, parent_entry in zip(
        first.committed.value.entries,
        parent_committed.report.entries,
        strict=True,
    ):
        assert variant_entry.variants[0].exact_span_result == parent_entry.selected_result

    calls_after_first = calls
    replay = BuildSpanVariantSetCommand(store, resolver, limits).execute(request)

    assert replay.outcome.is_fresh_claim is False
    assert replay.outcome.receipt_id == first.outcome.receipt_id
    assert replay.committed == first.committed
    assert calls > calls_after_first


def test_parent_failure_is_rejected_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, resolver, limits, _parent = _prepared_case(tmp_path, monkeypatch)
    claim_count = len(store.claims)
    object.__setattr__(
        request,
        "parent_outcome",
        CommandOutcome(request.parent_outcome.command_slot_id, "failed"),
    )

    with pytest.raises(BuildSpanVariantSetError, match="parent outcome"):
        BuildSpanVariantSetCommand(store, resolver, limits).execute(request)

    assert len(store.claims) == claim_count
    assert store.variant_record is None


def test_parent_member_mismatch_is_rejected_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, resolver, limits, _parent = _prepared_case(tmp_path, monkeypatch)
    claim_count = len(store.claims)
    changed = replace(
        request.parent_recipe_refs[0],
        content_hash="sha256:" + "1" * 64,
    )
    mismatched = replace(
        request,
        parent_recipe_refs=(changed, *request.parent_recipe_refs[1:]),
    )

    with pytest.raises(BuildSpanVariantSetError, match="exact parent Recipe set"):
        BuildSpanVariantSetCommand(store, resolver, limits).execute(mismatched)

    assert len(store.claims) == claim_count
    assert store.variant_record is None


def test_idempotency_key_must_be_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, request, _resolver, _limits, _parent = _prepared_case(tmp_path, monkeypatch)

    with pytest.raises(BuildSpanVariantSetError, match="independent idempotency"):
        replace(request, idempotency_key=request.parent_request.idempotency_key)


def test_payload_ceiling_denies_without_child_artifact_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, resolver, limits, _parent = _prepared_case(tmp_path, monkeypatch)
    monkeypatch.setattr(command_module, "MAX_SPAN_VARIANT_SET_PAYLOAD_BYTES", 1)

    result = BuildSpanVariantSetCommand(store, resolver, limits).execute(request)

    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == SPAN_VARIANT_SET_PAYLOAD_TOO_LARGE
    assert result.committed is None
    assert store.variant_record is None


def test_replay_rejects_a_self_consistent_tampered_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, resolver, limits, _parent = _prepared_case(tmp_path, monkeypatch)
    first = BuildSpanVariantSetCommand(store, resolver, limits).execute(request)
    assert first.outcome.state == "succeeded"
    assert store.variant_record is not None
    member = store.variant_record.members[0]
    payload = '{"tampered":true}'
    changed_reference = replace(
        member.reference,
        content_hash=canonical_payload_hash(payload),
    )
    changed_member = PersistedCommittedArtifactMember(
        changed_reference,
        payload,
        member.command_slot_id,
    )
    changed_artifact = replace(
        store.variant_record.artifacts[0],
        content_hash=changed_reference.content_hash,
        payload_json=payload,
    )
    store.variant_record = replace(
        store.variant_record,
        members=(changed_member,),
        set_hash=artifact_set_hash((changed_artifact,)),
    )

    with pytest.raises(BuildSpanVariantSetError, match="codec is invalid"):
        BuildSpanVariantSetCommand(store, resolver, limits).execute(request)
