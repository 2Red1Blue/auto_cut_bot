"""Pure invariant tests for reusable whole-episode evidence selection."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from autocut_kernel.pipeline.evidence_index import (
    EvidenceIndex,
    EvidenceIndexEntry,
    EvidenceIndexError,
    EvidenceRequirement,
)
from autocut_kernel.store.models import (
    ArtifactScope,
    CommittedArtifactMemberReference,
    Job,
)


def _hash(label: str) -> str:
    return "sha256:" + (label.encode("utf-8").hex() * 64)[:64]


def _requirement(episode_index: int = 0) -> EvidenceRequirement:
    return EvidenceRequirement(
        episode_index=episode_index,
        source_content_sha256=_hash("source"),
        window_manifest_sha256=_hash("windows"),
        proxy_timeline_map_sha256=_hash("timeline"),
        semantic_pack_sha256=_hash("semantic"),
        physical_timeline_inputs_sha256=_hash("physical"),
        physical_detector_policy_sha256=_hash("detector"),
        adaptive_plan_policy_sha256=_hash("plan"),
        timed_speech_profile_sha256=_hash("speech"),
        runtime_calibration_capability_sha256=_hash("capability"),
        authority_registry_snapshot_sha256=_hash("registry"),
        strategy_version="exact-timed-evidence-v1",
    )


def _entry(episode_index: int = 0, *, job_key: str = "child-a") -> EvidenceIndexEntry:
    job = Job(job_key, "production")
    scope = ArtifactScope("pipeline", "job", job.job_key)
    receipt, artifact_set, slot = (UUID(int=value + episode_index * 10) for value in (1, 2, 3))
    layout = (
        ("root_media_evidence_bundle", "root_media_evidence"),
        ("candidate_timed_evidence_index", "candidate_timed_evidence"),
        ("timed_speech_profile_admission", "timed_speech_profile_admission"),
        ("presentation_timeline_probe", "presentation_timeline_probe"),
        ("committed_video_to_audio_clock_map_certificate", "video_to_audio_clock_map"),
    )
    return EvidenceIndexEntry(
        requirement=_requirement(episode_index),
        origin_job=job,
        command_slot_id=slot,
        receipt_id=receipt,
        artifact_set_id=artifact_set,
        request_hash=_hash(f"request-{episode_index}"),
        set_hash=_hash(f"set-{episode_index}"),
        members=tuple(
            CommittedArtifactMemberReference(
                receipt,
                artifact_set,
                ordinal,
                scope,
                artifact_type,
                f"{prefix}_episode_{episode_index:04d}",
                1,
                _hash(f"member-{episode_index}-{ordinal}"),
            )
            for ordinal, (artifact_type, prefix) in enumerate(layout)
        ),
    )


def test_requirement_fingerprint_is_independent_of_child_job_ownership() -> None:
    first = _entry(0, job_key="initial-attempt")
    retried = _entry(0, job_key="later-attempt")

    assert first.requirement.fingerprint_sha256 == retried.requirement.fingerprint_sha256
    assert first.origin_job != retried.origin_job
    assert first.to_mapping()["origin_job"] != retried.to_mapping()["origin_job"]


def test_index_requires_an_ordered_complete_target_and_exact_member_closure() -> None:
    first, second = _entry(0), _entry(1, job_key="child-b")
    index = EvidenceIndex((first, second))

    assert index.content_hash.startswith("sha256:")
    with pytest.raises(EvidenceIndexError, match="exact order"):
        EvidenceIndex((second, first))

    invalid_member = replace(first.members[3], logical_id="presentation_timeline_probe_episode_0099")
    with pytest.raises(EvidenceIndexError, match="exact origin child closure"):
        replace(first, members=first.members[:3] + (invalid_member,) + first.members[4:])


def test_index_cannot_reuse_one_exact_child_closure_twice() -> None:
    first = _entry(0)
    second = _entry(1, job_key=first.origin_job.job_key)
    duplicate_handle = replace(
        second,
        command_slot_id=first.command_slot_id,
        receipt_id=first.receipt_id,
        artifact_set_id=first.artifact_set_id,
        members=tuple(
            replace(member, receipt_id=first.receipt_id, artifact_set_id=first.artifact_set_id)
            for member in second.members
        ),
    )

    with pytest.raises(EvidenceIndexError, match="same child closure twice"):
        EvidenceIndex((first, duplicate_handle))
