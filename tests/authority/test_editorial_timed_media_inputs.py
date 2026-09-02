"""Committed Stage 3/Catalog to timed-media join coverage.

The shared fixture runs actual Stage 1--3 generation and actual deterministic
timed-media preparation/finalization over one rebuilt Source/VLM predecessor.
These tests exercise the new read-only join only: they never grant a physical
selection, invoke a provider, or create a replacement committed result.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.pipeline.editorial_timed_media_inputs import (
    EditorialTimedMediaInputError,
    read_committed_editorial_timed_media_inputs,
)
from autocut_kernel.pipeline.finalize_timed_media_evidence_batch_command import (
    FinalizeTimedMediaEvidenceBatchCommand,
    FinalizeTimedMediaEvidenceBatchRequest,
    TimedMediaEvidenceBatchChild,
)
from autocut_kernel.store import Job
from autocut_kernel.store.models import canonical_recipe_scope

from tests.authority.editorial_media_fixture import (
    _persist_media_record,
    editorial_timed_media_case,
)


def _read(case):  # type: ignore[no-untyped-def]
    store, stage3_request, stage3_outcome, batch_request, batch_outcome, resolver, limits = case
    return read_committed_editorial_timed_media_inputs(
        store,
        stage3_request=stage3_request,
        stage3_outcome=stage3_outcome,
        media_batch_request=batch_request,
        media_batch_outcome=batch_outcome,
        authority_profile_resolver=resolver,
        limits=limits,
    )


def test_join_replays_one_shared_admitted_source_vlm_chain_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    store = case[0]
    media = store.media
    editorial = store.editorial
    before_media_claims = len(media.claims)
    before_media_successes = len(media.successes)
    before_editorial_events = len(editorial.events)

    joined = _read(case)

    assert joined.editorial.record.job_id == joined.predecessors.semantic.source_manifest.job_id
    assert joined.media_batch.outcome.state == "succeeded"
    assert len(joined.alternatives) == 4
    assert tuple(row.alternative.alternative_id for row in joined.alternatives) == (
        "direct", "unchosen-secondary", "direct", "unchosen-secondary",
    )
    assert all(len(row.candidates) == 2 for row in joined.alternatives)
    assert joined.alternatives[0].candidates == tuple(reversed(joined.alternatives[1].candidates))
    for row in joined.alternatives:
        for candidate in row.candidates:
            child = joined.media_batch_request.children[candidate.episode_index]
            raw = child.request.semantic_pack.candidate_hypotheses[candidate.candidate_ordinal]
            assert candidate.raw_candidate_sha256 == canonical_sha256(raw.to_mapping())
            assert candidate.vlm_candidate_ref.object_id == raw.candidate_id
            assert candidate.child_member_references == joined.media_batch.child_member_references[
                candidate.episode_index
            ]
    assert len(media.claims) == before_media_claims
    assert len(media.successes) == before_media_successes
    assert len(editorial.events) > before_editorial_events  # readers re-audited committed predecessors


def test_join_accepts_recovery_batch_job_when_every_child_keeps_exact_base_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    store, stage3_request, stage3_outcome, batch_request, _batch_outcome, resolver, limits = case
    base_child = batch_request.children[0]
    recovery_job = Job("pipeline_run_" + "e" * 32, stage3_request.job.profile)
    recovery_request = replace(
        base_child.request,
        job=recovery_job,
        idempotency_key="media-preflight:recovered-episode-0",
        artifact_scope=canonical_recipe_scope(recovery_job),
        input_job=stage3_request.job,
    )
    recovery_outcome = _persist_media_record(store.media, recovery_request, resolver)
    recovered_batch_request = FinalizeTimedMediaEvidenceBatchRequest(
        recovery_job,
        "media-preflight:recovered-batch",
        canonical_recipe_scope(recovery_job),
        1,
        (TimedMediaEvidenceBatchChild(recovery_request, recovery_outcome),),
    )
    recovered_batch = FinalizeTimedMediaEvidenceBatchCommand(
        store.media, resolver, limits
    ).execute(recovered_batch_request)

    joined = read_committed_editorial_timed_media_inputs(
        store,
        stage3_request=stage3_request,
        stage3_outcome=stage3_outcome,
        media_batch_request=recovered_batch_request,
        media_batch_outcome=recovered_batch.outcome,
        authority_profile_resolver=resolver,
        limits=limits,
    )

    assert joined.media_batch_request.job == recovery_job
    assert joined.media_batch_request.job != stage3_request.job
    assert joined.media_batch_request.children[0].request.evidence_job == stage3_request.job


def test_join_rejects_changed_child_source_slot_with_unchanged_semantic_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    store, stage3_request, stage3_outcome, batch_request, batch_outcome, resolver, limits = case
    child = batch_request.children[0]
    changed_child = replace(child, request=replace(
        child.request, source_manifest_command_slot_id=uuid4(),
    ))
    changed_batch = replace(batch_request, children=(changed_child,))
    assert changed_child.request.semantic_inputs_request == child.request.semantic_inputs_request
    with pytest.raises(EditorialTimedMediaInputError, match="committed Source"):
        read_committed_editorial_timed_media_inputs(
            store, stage3_request=stage3_request, stage3_outcome=stage3_outcome,
            media_batch_request=changed_batch, media_batch_outcome=batch_outcome,
            authority_profile_resolver=resolver, limits=limits,
        )


def test_join_rechecks_current_render_authorization_after_all_commands_succeeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    store = case[0]
    semantic = store.editorial.inputs
    grant = semantic.source_grant
    changed = replace(semantic, source_grant=replace(
        grant, policy=replace(grant.policy, authorized_purposes=("semantic_analysis",)),
    ))
    store.editorial.inputs = store.editorial.predecessor.inputs = changed
    store.editorial.predecessor.predecessor.inputs = changed
    store.media.semantic_inputs = changed
    # Revocation changes the frozen generation-request identity. The exact
    # in-memory Store rejects the stale predecessor before purpose evaluation;
    # production Store raises an integrity error at the same boundary.
    with pytest.raises(AssertionError, match="expected_request_hash"):
        _read(case)


def test_join_rejects_foreign_kernel_job_before_any_reader_can_substitute_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    store, stage3_request, stage3_outcome, batch_request, batch_outcome, resolver, limits = case
    before_claims = len(store.media.claims)

    with pytest.raises(EditorialTimedMediaInputError, match="Job"):
        read_committed_editorial_timed_media_inputs(
            store,
            stage3_request=stage3_request,
            stage3_outcome=stage3_outcome,
            media_batch_request=batch_request,
            media_batch_outcome=replace(batch_outcome, job_id=uuid4()),
            authority_profile_resolver=resolver,
            limits=limits,
        )

    assert len(store.media.claims) == before_claims


def test_join_rejects_a_batch_child_with_a_foreign_semantic_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    store, stage3_request, stage3_outcome, batch_request, batch_outcome, resolver, limits = case
    child = batch_request.children[0]
    foreign_selector = replace(
        child.request.semantic_inputs_request,
        vlm_semantic_pack_set=replace(
            child.request.semantic_inputs_request.vlm_semantic_pack_set,
            content_hash="sha256:" + "f" * 64,
        ),
    )
    foreign_request = replace(child.request, semantic_inputs_request=foreign_selector)
    foreign_batch = replace(
        batch_request,
        children=(replace(child, request=foreign_request),),
    )

    with pytest.raises((EditorialTimedMediaInputError, ValueError)):
        read_committed_editorial_timed_media_inputs(
            store,
            stage3_request=stage3_request,
            stage3_outcome=stage3_outcome,
            media_batch_request=foreign_batch,
            media_batch_outcome=batch_outcome,
            authority_profile_resolver=resolver,
            limits=limits,
        )


def test_join_requires_the_complete_finalized_batch_not_a_direct_child_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    store, stage3_request, stage3_outcome, batch_request, _batch_outcome, resolver, limits = case
    child_outcome = batch_request.children[0].outcome

    with pytest.raises((EditorialTimedMediaInputError, ValueError)):
        read_committed_editorial_timed_media_inputs(
            store,
            stage3_request=stage3_request,
            stage3_outcome=stage3_outcome,
            media_batch_request=batch_request,
            media_batch_outcome=child_outcome,
            authority_profile_resolver=resolver,
            limits=limits,
        )
