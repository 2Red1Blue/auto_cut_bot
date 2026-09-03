"""Candidate compiler authority projections keep CPU and PC-CUDA distinct."""

from __future__ import annotations

from dataclasses import replace

import pytest
from autocut_kernel.media.types import TimeBase
from autocut_kernel.physical_edit.candidate_dialogue_guard import (
    derive_candidate_dialogue_guard,
)
from autocut_kernel.physical_edit.candidate_exact_span import compile_candidate_av_span
from autocut_kernel.physical_edit.candidate_timed_speech_authority import (
    CandidateTimedSpeechAuthorityError,
    CandidateTimedSpeechAuthorityKind,
    project_candidate_timed_speech_authority_from_registry_entry,
    project_candidate_timed_speech_authority_from_runtime_projection,
)
from autocut_kernel.physical_edit.dialogue_guard import (
    DialogueGuardError,
    DialogueGuardIndeterminateError,
    DialogueRequirement,
)
from autocut_kernel.registry.authority_profiles import TimingPolicies

from tests.media.test_candidate_dialogue_guard import (
    HASH_D,
    candidate_dialogue_case,
)
from tests.media.test_candidate_exact_span import _case
from tests.media.test_prepare_runtime_timed_media_evidence_command import (
    _runtime_projection,
)
from tests.media.test_root_evidence import HASH_A, HASH_B, HASH_C

HASH_E = "sha256:" + "e" * 64


def _cuda_projection(root):
    projection = _runtime_projection(root)
    vad = replace(
        projection.producers[1],
        calibration_policy_sha256=HASH_D,
        model_sha256=HASH_D,
    )
    return replace(
        projection,
        vad_calibration_record_sha256=HASH_D,
        producers=(projection.producers[0], vad),
    )


def _timing_policies(*, word_gap_ms: int = 0, vad_merge_gap_ms: int = 0) -> TimingPolicies:
    return TimingPolicies(
        HASH_C,
        HASH_A,
        HASH_B,
        HASH_C,
        HASH_A,
        word_gap_ms,
        vad_merge_gap_ms,
    )


def test_projects_store_read_cpu_profile_without_losing_original_authority() -> None:
    _, _, _, profile = candidate_dialogue_case()

    authority = project_candidate_timed_speech_authority_from_registry_entry(profile)

    assert authority.authority_kind is CandidateTimedSpeechAuthorityKind.INSTALLED_CPU_PROFILE
    assert authority.original_authority_sha256 == profile.canonical_hash
    assert authority.installed_cpu_profile is profile
    assert authority.runtime_cuda_capability is None
    assert authority.guard_policy == profile.guard_policy
    assert authority.canonical_hash == replace(authority).canonical_hash
    assert hash(authority) == hash(replace(authority))


def test_projects_pc_cuda_as_word_guard_with_exact_integer_ceiling() -> None:
    root, _, _, _ = candidate_dialogue_case()
    projection = replace(
        _cuda_projection(root),
        source_time_base=TimeBase(1, 44_100),
        word_gap_ms=1,
        vad_merge_gap_ms=7,
    )

    authority = project_candidate_timed_speech_authority_from_runtime_projection(
        projection,
        _timing_policies(word_gap_ms=1, vad_merge_gap_ms=7),
    )

    assert authority.authority_kind is CandidateTimedSpeechAuthorityKind.RUNTIME_CUDA_CAPABILITY
    assert authority.original_authority_sha256 == projection.canonical_hash
    assert authority.installed_cpu_profile is None
    assert authority.runtime_cuda_capability is projection
    assert authority.profile_kind.value == "sensevoice_word_guard_v1"
    assert authority.capability.value == "known_speech_only"
    assert authority.guard_policy.word_gap_tick == 45
    assert authority.guard_policy.vad_merge_gap_tick == 309
    assert authority.guard_policy.pre_roll_tick == 0
    assert authority.guard_policy.post_roll_tick == 0
    assert authority.canonical_hash == replace(authority).canonical_hash
    assert hash(authority) == hash(replace(authority))
    with pytest.raises(CandidateTimedSpeechAuthorityError, match="resolver timing policies"):
        replace(
            authority,
            guard_policy=replace(
                authority.guard_policy,
                word_gap_tick=authority.guard_policy.word_gap_tick + 1,
            ),
        )


@pytest.mark.parametrize(
    "field_name",
    (
        "timed_speech_policy_sha256",
        "word_gap_policy_sha256",
        "vad_merge_policy_sha256",
        "alignment_policy_sha256",
        "acceptance_policy_sha256",
    ),
)
def test_cuda_projection_rejects_each_timing_policy_hash_drift(field_name: str) -> None:
    root, _, _, _ = candidate_dialogue_case()
    projection = _cuda_projection(root)
    policies = replace(_timing_policies(), **{field_name: HASH_E})

    with pytest.raises(CandidateTimedSpeechAuthorityError, match="timing policy hashes"):
        project_candidate_timed_speech_authority_from_runtime_projection(projection, policies)


@pytest.mark.parametrize("field_name", ("word_gap_ms", "vad_merge_gap_ms"))
def test_same_cuda_projection_cannot_bypass_accepted_gap_values(field_name: str) -> None:
    root, _, _, _ = candidate_dialogue_case()
    projection = _cuda_projection(root)
    changed = replace(_timing_policies(), **{field_name: 1})

    with pytest.raises(CandidateTimedSpeechAuthorityError, match="accepted timing-policy gap"):
        project_candidate_timed_speech_authority_from_runtime_projection(projection, changed)


@pytest.mark.parametrize(
    ("tamper", "message"),
    (
        ("source_clock", "source audio clock"),
        ("producer", "root/authority"),
        ("calibration", "producer/calibration/native adapter"),
        ("adapter", "producer/calibration/native adapter"),
    ),
)
def test_cuda_candidate_guard_rejects_authority_or_evidence_tampering(
    tamper: str,
    message: str,
) -> None:
    root, candidate, plan, _ = candidate_dialogue_case()
    projection = _cuda_projection(root)
    if tamper == "source_clock":
        projection = replace(projection, source_clock_id="foreign-audio-clock")
    elif tamper == "producer":
        projection = replace(
            projection,
            producers=(
                replace(projection.producers[0], producer_id="foreign-asr"),
                projection.producers[1],
            ),
        )
    elif tamper == "calibration":
        projection = replace(projection, asr_calibration_record_sha256=HASH_E)
    else:
        projection = replace(projection, native_port_identity_sha256=HASH_E)
    authority = project_candidate_timed_speech_authority_from_runtime_projection(
        projection,
        _timing_policies(),
    )

    with pytest.raises(DialogueGuardError, match=message):
        derive_candidate_dialogue_guard(
            root,
            candidate,
            plan,
            authority,
            DialogueRequirement.NOT_REQUIRED,
        )


def test_cuda_word_guard_stays_indeterminate_for_complete_dialogue_compilation() -> None:
    request, root, candidate, plan, _, clock, policy = _case()
    authority = project_candidate_timed_speech_authority_from_runtime_projection(
        _cuda_projection(root),
        _timing_policies(),
    )

    with pytest.raises(DialogueGuardIndeterminateError, match="word guard cannot satisfy"):
        compile_candidate_av_span(
            replace(request, dialogue_requirement=DialogueRequirement.COMPLETE),
            root,
            candidate,
            plan,
            authority,
            clock,
            policy,
        )


def test_cpu_compiler_regression_records_normalized_and_original_authority() -> None:
    request, root, candidate, plan, profile, clock, policy = _case()
    authority = project_candidate_timed_speech_authority_from_registry_entry(profile)

    legacy_input = compile_candidate_av_span(
        request, root, candidate, plan, profile, clock, policy
    )
    projected_input = compile_candidate_av_span(
        request, root, candidate, plan, authority, clock, policy
    )

    assert projected_input == legacy_input
    guard = projected_input.dialogue_guard
    assert guard.timed_speech_authority_sha256 == authority.canonical_hash
    assert guard.original_authority_kind is CandidateTimedSpeechAuthorityKind.INSTALLED_CPU_PROFILE
    assert guard.original_authority_sha256 == profile.canonical_hash
