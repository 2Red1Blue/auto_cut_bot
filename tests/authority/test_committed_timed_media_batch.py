"""Exact aggregate-reader coverage for committed timed-media evidence batches."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import autocut_kernel.pipeline.finalize_timed_media_evidence_batch_command as batch_module
import pytest
from autocut_kernel.pipeline.committed_timed_media import inspect_committed_timed_media_evidence
from autocut_kernel.pipeline.finalize_timed_media_evidence_batch_command import (
    FINALIZE_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND,
    FinalizeTimedMediaEvidenceBatchCommand,
    FinalizeTimedMediaEvidenceBatchRequest,
    TimedMediaEvidenceBatchChild,
    TimedMediaEvidenceBatchError,
    read_committed_timed_media_evidence_batch,
)
from autocut_kernel.source_manifest import (
    DecodedSourceManifest,
    SourceOperationGrant,
    SourceOperationPolicy,
    decode_source_manifest,
)
from autocut_kernel.store.models import (
    CommandOutcome,
    CommandSuccess,
    CommittedArtifactMemberReference,
    CommittedSemanticInputs,
    CommittedVlmSemanticInput,
    Job,
    PersistedCommittedArtifactMember,
    PersistedCommittedArtifactSet,
    PersistedVlmSemanticPack,
    SourceWindowIdentity,
    VlmRequestRecordReference,
    canonical_payload_hash,
    canonical_recipe_scope,
)

import tests.media.test_prepare_timed_media_evidence_command as media_fixture
from tests.authority.test_committed_timed_media import (
    _installed_resource,
    _limits,
    _ReaderStore,
    _record,
)
from tests.media.test_prepare_timed_media_evidence_command import _request


class _BatchStore(_ReaderStore):
    def __init__(self, anchor: object, root: Path) -> None:
        super().__init__(anchor, root)
        self.final_record: PersistedCommittedArtifactSet | None = None
        self.child_records: dict[tuple[object, object, object], PersistedCommittedArtifactSet] = {}
        self.observed_open_lease_before_materialization = False

    def materialize_immutable_blob(self, job, reference, limits):  # type: ignore[no-untyped-def]
        self.observed_open_lease_before_materialization |= bool(tuple(self.root.glob("*.blob")))
        return super().materialize_immutable_blob(job, reference, limits)

    def read_committed_artifact_set(self, job, **expected):  # type: ignore[no-untyped-def]
        if expected["expected_command_name"] == FINALIZE_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND:
            record = self.final_record
            assert record is not None
            return record
        return self.child_records.get(
            (expected["command_slot_id"], expected["receipt_id"], expected["artifact_set_id"]),
            self.record,
        )

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        if len(success.artifacts) == 5:
            return super().commit_command_success(success)
        self.successes.append(success)
        claim = self.claims[-1]
        child_record = self.record
        assert child_record is not None
        receipt_id, artifact_set_id = uuid4(), uuid4()
        outcome = CommandOutcome(
            success.command_slot_id,
            "succeeded",
            receipt_id=receipt_id,
            artifact_set_id=artifact_set_id,
            job_id=child_record.job_id,
        )
        self._replace_slot(outcome)
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
        self.final_record = PersistedCommittedArtifactSet(
            claim.job,
            child_record.job_id,
            success.command_slot_id,
            receipt_id,
            artifact_set_id,
            claim.request_hash,
            claim.command_name,
            claim.execution_kind,
            success.set_hash,
            members,
        )
        return outcome


def _batch_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    resource, anchor = _installed_resource(tmp_path, monkeypatch)
    store = _BatchStore(anchor, tmp_path)
    request = _request(store, with_candidates=True)
    outcome, resolver = _record(store, request, resource)
    assert store.record is not None
    store.child_records[(outcome.command_slot_id, outcome.receipt_id, outcome.artifact_set_id)] = store.record
    batch = FinalizeTimedMediaEvidenceBatchRequest(
        request.job,
        "media-preflight:batch",
        request.artifact_scope,
        request.artifact_revision,
        (TimedMediaEvidenceBatchChild(request, outcome),),
    )
    return store, request, outcome, resolver, batch


def _two_episode_batch_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    resource, anchor = _installed_resource(tmp_path, monkeypatch)
    store = _BatchStore(anchor, tmp_path)
    request_one = _request(store, with_candidates=True)
    second_store = media_fixture._Store()
    request_two_seed = _request(second_store, with_candidates=True)
    assert store.source_manifest is not None and second_store.source_manifest is not None
    decoded_one = decode_source_manifest(store.source_manifest.payload_json, store.source_manifest.proxy_blobs)
    decoded_two = decode_source_manifest(
        second_store.source_manifest.payload_json, second_store.source_manifest.proxy_blobs
    )
    source_one = replace(decoded_one.census.sources[0], relative_path="episode-1.mp4")
    source_two = replace(decoded_two.census.sources[0], relative_path="episode-2.mp4")
    episode_one = replace(
        decoded_one.episodes[0],
        media_probe=replace(decoded_one.episodes[0].media_probe, source=source_one),
    )
    episode_two = replace(
        decoded_two.episodes[0],
        media_probe=replace(decoded_two.episodes[0].media_probe, source=source_two),
    )
    policy = SourceOperationPolicy(
        decoded_one.census.policy.authorization_id,
        decoded_one.census.policy.series_id,
        2,
        decoded_one.census.policy.authorized_purposes,
    )
    merged = DecodedSourceManifest(
        SourceOperationGrant(policy, "all_or_nothing", (source_one, source_two)),
        (episode_one, episode_two),
    )
    merged_payload = json.dumps(merged.to_mapping(), separators=(",", ":"), sort_keys=True)
    store.source_manifest = replace(
        store.source_manifest,
        reference=replace(
            store.source_manifest.reference,
            content_hash=canonical_payload_hash(merged_payload),
        ),
        payload_json=merged_payload,
        proxy_blobs=(
            store.source_manifest.proxy_blobs[0],
            second_store.source_manifest.proxy_blobs[0],
        ),
    )
    store.blobs.update(second_store.blobs)
    selector, pack_one = media_fixture._register_semantic_inputs(store, with_candidates=True)
    assert store.semantic_inputs is not None and second_store.semantic_inputs is not None
    input_one = store.semantic_inputs.inputs[0]
    old_input = second_store.semantic_inputs.inputs[0]
    old_child = old_input.semantic_pack.source_child
    child_payload = json.loads(old_child.payload_json)
    child_payload.update({
        "episode_index": 1,
        "idempotency_key": "fixture-vlm-child-1",
        "proxy_blob": _blob_mapping(second_store.source_manifest.proxy_blobs[0]),
        "source_manifest_sha256": store.source_manifest.reference.content_hash,
        "source_provenance_sha256": store.source_manifest.canonical_hash,
    })
    child_payload_json = json.dumps(child_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    child_two = replace(
        old_child,
        reference=VlmRequestRecordReference(
            old_child.reference.scope,
            old_child.reference.logical_id,
            old_child.reference.revision,
            canonical_payload_hash(child_payload_json),
        ),
        payload_json=child_payload_json,
        kernel_job_id=store.source_manifest.job_id,
        idempotency_key="fixture-vlm-child-1",
        episode_index=1,
        source_manifest_sha256=store.source_manifest.reference.content_hash,
        source_provenance_sha256=store.source_manifest.canonical_hash,
    )
    pack_two = PersistedVlmSemanticPack(
        old_input.semantic_pack.reference,
        old_input.semantic_pack.payload_json,
        old_input.semantic_pack.semantic_pack,
        child_two,
    )
    manifest_two = episode_two.manifest
    window_two = SourceWindowIdentity(
        1,
        manifest_two.stream_index,
        manifest_two.core_range.start_pts,
        manifest_two.core_range.end_pts,
        manifest_two.canonical_hash,
        manifest_two.source_id,
        manifest_two.source_sha256,
        manifest_two.source_clock_id,
        episode_two.manifest_set.canonical_hash,
        second_store.source_manifest.proxy_blobs[0],
    )
    input_two = CommittedVlmSemanticInput(
        window_two,
        old_input.request_identity,
        pack_two,
        old_input.response_record,
        old_input.raw_response,
    )
    store.semantic_inputs = CommittedSemanticInputs(
        store.source_manifest,
        merged.census,
        selector.vlm_semantic_pack_set,
        input_one.semantic_pack.source_child.request_policy,
        (input_one, input_two),
    )
    request_one = replace(
        request_one,
        source_manifest_reference=store.source_manifest.reference,
        source_provenance_sha256=store.source_manifest.canonical_hash,
        semantic_inputs_request=selector,
        semantic_pack=pack_one,
    )
    request_two = replace(
        request_two_seed,
        job=request_one.job,
        idempotency_key="media-preflight:episode:1",
        episode_index=1,
        artifact_scope=request_one.artifact_scope,
        source_blob=second_store.source_manifest.proxy_blobs[0],
        source_manifest_reference=store.source_manifest.reference,
        source_manifest_receipt_id=store.source_manifest.receipt_id,
        source_manifest_artifact_set_id=store.source_manifest.artifact_set_id,
        source_manifest_command_slot_id=store.source_manifest.command_slot_id,
        source_provenance_sha256=store.source_manifest.canonical_hash,
        semantic_inputs_request=selector,
        window_manifest=manifest_two,
        semantic_pack=pack_two.semantic_pack,
        frame_pts_index=manifest_two.frame_pts_index_set,
        audio_sample_boundaries=episode_two.media_probe.audio_sample_boundaries,
    )
    outcome_one, resolver = _record(store, request_one, resource)
    assert store.record is not None
    store.child_records[(outcome_one.command_slot_id, outcome_one.receipt_id, outcome_one.artifact_set_id)] = store.record
    outcome_two, _ = _record(store, request_two, resource)
    assert store.record is not None
    assert outcome_one.job_id is not None
    outcome_two = replace(outcome_two, job_id=outcome_one.job_id)
    store.record = replace(store.record, job_id=outcome_one.job_id)
    store.child_records[(outcome_two.command_slot_id, outcome_two.receipt_id, outcome_two.artifact_set_id)] = store.record
    batch = FinalizeTimedMediaEvidenceBatchRequest(
        request_one.job,
        "media-preflight:batch",
        request_one.artifact_scope,
        request_one.artifact_revision,
        (
            TimedMediaEvidenceBatchChild(request_one, outcome_one),
            TimedMediaEvidenceBatchChild(request_two, outcome_two),
        ),
    )
    return store, request_one, resolver, batch


def _blob_mapping(reference) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {
        "object_id": str(reference.object_id),
        "content_hash": reference.content_hash,
        "byte_length": reference.byte_length,
        "media_type": reference.media_type,
    }


def test_batch_commits_and_rereads_one_exact_final_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, _, resolver, batch = _batch_case(tmp_path, monkeypatch)
    command = FinalizeTimedMediaEvidenceBatchCommand(store, resolver, _limits(request))

    result = command.execute(batch)
    assert result.outcome.state == "succeeded"
    assert result.artifact is not None
    assert len(result.child_member_references) == 1
    assert len(result.child_member_references[0]) == 5
    assert store.final_record is not None
    payload = json.loads(store.final_record.members[0].payload_json)
    assert payload["children"][0]["record"]["members"] == [
        item.to_mapping() for item in result.child_member_references[0]
    ]

    replay = command.execute(batch)

    def forbid_write(*args, **kwargs):
        raise AssertionError("public batch reader must not claim or commit")

    monkeypatch.setattr(store, "claim_command", forbid_write)
    monkeypatch.setattr(store, "commit_command_success", forbid_write)
    reread = read_committed_timed_media_evidence_batch(
        store,
        batch,
        result.outcome,
        authority_profile_resolver=resolver,
        limits=_limits(request),
    )
    assert replay.outcome == result.outcome
    assert reread.child_member_references == result.child_member_references
    assert len(store.successes) == 2  # child + exactly one final batch success


def test_batch_metadata_limit_rejects_before_any_full_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, _, resolver, batch = _batch_case(tmp_path, monkeypatch)
    before_claims = len(store.claims)
    before_materializations = store.materialization_attempts
    narrow = replace(_limits(request), max_total_blob_bytes=1)

    with pytest.raises(TimedMediaEvidenceBatchError, match="child metadata"):
        FinalizeTimedMediaEvidenceBatchCommand(store, resolver, narrow).execute(batch)

    assert len(store.claims) == before_claims
    assert store.materialization_attempts == before_materializations


def test_batch_rejects_child_that_is_not_the_exact_succeeded_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, outcome, resolver, batch = _batch_case(tmp_path, monkeypatch)
    forged = replace(outcome, receipt_id=uuid4())
    request = replace(batch, children=(TimedMediaEvidenceBatchChild(request, forged),))
    before_claims = len(store.claims)

    with pytest.raises((TimedMediaEvidenceBatchError, ValueError)):
        FinalizeTimedMediaEvidenceBatchCommand(store, resolver, _limits(batch.children[0].request)).execute(request)

    assert len(store.claims) == before_claims


def test_batch_rejects_running_replay_without_treating_it_as_final_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, _, resolver, batch = _batch_case(tmp_path, monkeypatch)
    command = FinalizeTimedMediaEvidenceBatchCommand(store, resolver, _limits(request))
    first = command.execute(batch)
    store.outcomes[batch.idempotency_key] = CommandOutcome(first.outcome.command_slot_id, "running")

    replay = command.execute(batch)

    assert replay.outcome.state == "running"
    assert replay.artifact is None


def test_batch_requires_every_actual_source_episode_and_retains_only_five_refs_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, resolver, batch = _two_episode_batch_case(tmp_path, monkeypatch)
    command = FinalizeTimedMediaEvidenceBatchCommand(store, resolver, _limits(request))
    before_attempts = store.materialization_attempts
    before_closed = store.closed

    result = command.execute(batch)

    assert result.outcome.state == "succeeded"
    assert tuple(len(refs) for refs in result.child_member_references) == (5, 5)
    assert store.materialization_attempts - before_attempts == 10
    assert store.closed - before_closed == 10
    assert not store.observed_open_lease_before_materialization
    assert not tuple(tmp_path.glob("*.blob"))
    missing = replace(batch, children=(batch.children[0],))
    before_claims = len(store.claims)
    with pytest.raises(TimedMediaEvidenceBatchError, match="every committed Source episode"):
        command.execute(missing)
    assert len(store.claims) == before_claims


def test_mixed_batch_reuses_origin_child_and_rereads_selected_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, resolver, origin_batch = _two_episode_batch_case(tmp_path, monkeypatch)
    origin_job = request.job
    target_job = Job("media-recompute-successor", origin_job.profile)
    selected_origin = origin_batch.children[1].request
    selected_target = replace(
        selected_origin,
        job=target_job,
        idempotency_key="media-preflight:episode:1:successor",
        artifact_scope=canonical_recipe_scope(target_job),
        input_job=origin_job,
    )
    selected_outcome, _ = _record(store, selected_target, resolver.resource)
    assert store.record is not None
    store.child_records[
        (
            selected_outcome.command_slot_id,
            selected_outcome.receipt_id,
            selected_outcome.artifact_set_id,
        )
    ] = store.record
    mixed = FinalizeTimedMediaEvidenceBatchRequest(
        target_job,
        "media-preflight:batch:successor",
        canonical_recipe_scope(target_job),
        1,
        (
            origin_batch.children[0],
            TimedMediaEvidenceBatchChild(selected_target, selected_outcome),
        ),
    )

    result = FinalizeTimedMediaEvidenceBatchCommand(
        store, resolver, _limits(request)
    ).execute(mixed)

    assert result.outcome.state == "succeeded"
    assert result.artifact is not None
    assert result.artifact.scope == canonical_recipe_scope(target_job)
    assert tuple(child.request.job for child in mixed.children) == (
        origin_job,
        target_job,
    )
    assert all(
        child.request.evidence_job == origin_job for child in mixed.children
    )
    assert len(result.child_member_references) == 2


def test_two_episode_batch_cumulative_limit_rejects_before_any_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, resolver, batch = _two_episode_batch_case(tmp_path, monkeypatch)
    limits = _limits(request)
    totals = tuple(
        sum(
            ref.byte_length
            for ref in inspect_committed_timed_media_evidence(
                store,
                child.request,
                child.outcome,
                authority_profile_resolver=resolver,
                limits=limits,
            ).blob_refs
        )
        for child in batch.children
    )
    narrow = replace(limits, max_total_blob_bytes=max(totals))
    before_claims = len(store.claims)
    before_materializations = store.materialization_attempts

    with pytest.raises(TimedMediaEvidenceBatchError, match="cumulative byte ceiling"):
        FinalizeTimedMediaEvidenceBatchCommand(store, resolver, narrow).execute(batch)

    assert len(store.claims) == before_claims
    assert store.materialization_attempts == before_materializations


def test_public_batch_reader_rejects_foreign_kernel_job_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, _, resolver, batch = _batch_case(tmp_path, monkeypatch)
    result = FinalizeTimedMediaEvidenceBatchCommand(store, resolver, _limits(request)).execute(batch)

    with pytest.raises(TimedMediaEvidenceBatchError, match="succeeded Receipt"):
        read_committed_timed_media_evidence_batch(
            store,
            batch,
            replace(result.outcome, job_id=uuid4()),
            authority_profile_resolver=resolver,
            limits=_limits(request),
        )


@pytest.mark.skipif(sys.implementation.name != "cpython", reason="CPython lifetime observation")
def test_batch_releases_full_metadata_and_decoded_values_before_next_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, resolver, batch = _two_episode_batch_case(tmp_path, monkeypatch)
    inspector = batch_module.inspect_committed_timed_media_evidence
    reader = batch_module.read_committed_timed_media_evidence
    metadata_values: list[object] = []
    decoded_values: list[object] = []

    def assert_only_observer_retains(values: list[object]) -> None:
        for value in values:
            # The observer list, this loop local and getrefcount's temporary
            # argument are the only references. Retaining prior full DTOs in a
            # production list/tuple fails even when every Blob lease is closed.
            assert sys.getrefcount(value) == 3

    def inspect(*args, **kwargs):
        assert_only_observer_retains(metadata_values)
        value = inspector(*args, **kwargs)
        metadata_values.append(value)
        return value

    def read(*args, **kwargs):
        assert_only_observer_retains(metadata_values)
        assert_only_observer_retains(decoded_values)
        value = reader(*args, **kwargs)
        decoded_values.append(value)
        return value

    # Observe real validation and its actual return values, not synthetic
    # successful results or mocked admission/Source coverage.
    monkeypatch.setattr(batch_module, "inspect_committed_timed_media_evidence", inspect)
    monkeypatch.setattr(batch_module, "read_committed_timed_media_evidence", read)
    result = FinalizeTimedMediaEvidenceBatchCommand(store, resolver, _limits(request)).execute(batch)
    assert result.outcome.state == "succeeded"
    assert len(metadata_values) == len(decoded_values) == 2
    assert_only_observer_retains(metadata_values)
    assert_only_observer_retains(decoded_values)
