"""Candidate-local dialogue guard proofs over strict timed-media values."""

from __future__ import annotations

from dataclasses import replace

import pytest
from autocut_kernel.media import (
    CalibrationBinding,
    CandidateEvidenceWindowPlan,
    CandidateTimedEvidenceSet,
    Coverage,
    CoverageDiagnostic,
    CoverageOutcome,
    EvidenceCompleteness,
    SpeechActivitySegment,
    SpeechActivitySet,
    SpeechSourceOutcome,
    TranscriptCompleteness,
    TranscriptSegment,
    TranscriptSet,
    TranscriptSourceOutcome,
    TranscriptWord,
    advance_candidate_evidence_window,
)
from autocut_kernel.media.root_evidence import RootMediaEvidenceBundle
from autocut_kernel.media.stage4_predecessor import (
    TimedSpeechCapability,
    TimedSpeechGuardPolicy,
    TimedSpeechProducerRequirement,
    TimedSpeechProfileKind,
    TimedSpeechProfileRegistryEntry,
)
from autocut_kernel.media.timed_evidence import SentenceCompleteness
from autocut_kernel.media.types import TickRange
from autocut_kernel.physical_edit.candidate_dialogue_guard import (
    derive_candidate_dialogue_guard,
    verify_candidate_dialogue_guard,
)
from autocut_kernel.physical_edit.dialogue_guard import (
    DialogueGuardError,
    DialogueGuardIndeterminateError,
    DialogueGuardKind,
    DialogueRequirement,
)

from tests.media.test_root_evidence import HASH_A, HASH_B, HASH_C, SOURCE_HASH, SOURCE_ID, _bundle
from tests.media.test_timed_evidence import _assessment, _bindings, _initial_plan

HASH_D = "sha256:" + "d" * 64
_LOCAL_AUDIO_RANGE = TickRange(13, 41)


def _coverage(
    context: object,
    *,
    outcome: CoverageOutcome = CoverageOutcome.COMPLETE,
) -> Coverage:
    source_id = getattr(context, "source_id")
    source_sha256 = getattr(context, "source_sha256")
    clock_id = getattr(context, "clock_id")
    time_base = getattr(context, "time_base")
    diagnostics = ()
    if outcome is CoverageOutcome.PARTIAL:
        diagnostics = (
            CoverageDiagnostic(
                _LOCAL_AUDIO_RANGE.start_pts,
                _LOCAL_AUDIO_RANGE.start_pts + 1,
                "LOCAL_TRUNCATION",
                "test-only closed local diagnostic",
                HASH_A,
            ),
        )
    return Coverage(
        source_id,
        source_sha256,
        clock_id,
        time_base,
        _LOCAL_AUDIO_RANGE.start_pts,
        _LOCAL_AUDIO_RANGE.end_pts,
        outcome,
        diagnostics,
    )


def _local_transcript(root: RootMediaEvidenceBundle, *, vad_only: bool) -> TranscriptSet:
    context = replace(
        root.transcript.context,
        origin_tick=_LOCAL_AUDIO_RANGE.start_pts,
        duration_tick=_LOCAL_AUDIO_RANGE.end_pts - _LOCAL_AUDIO_RANGE.start_pts,
    )
    coverage = _coverage(context)
    if vad_only:
        return TranscriptSet(
            "candidate-no-lexical",
            context,
            coverage,
            TranscriptSourceOutcome.NO_LEXICAL_CONTENT,
            TranscriptCompleteness(
                EvidenceCompleteness.COMPLETE,
                EvidenceCompleteness.COMPLETE,
                EvidenceCompleteness.NOT_APPLICABLE,
            ),
            (),
            (),
            (),
        )
    word = TranscriptWord(
        "candidate-word",
        SOURCE_ID,
        SOURCE_HASH,
        context.clock_id,
        context.time_base,
        23,
        25,
        "local only",
    )
    segment = TranscriptSegment(
        "candidate-segment",
        SOURCE_ID,
        SOURCE_HASH,
        context.clock_id,
        context.time_base,
        23,
        25,
        (),
        "local only",
    )
    return TranscriptSet(
        "candidate-word-only",
        context,
        coverage,
        TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE,
        TranscriptCompleteness(
            EvidenceCompleteness.COMPLETE,
            EvidenceCompleteness.COMPLETE,
            EvidenceCompleteness.NOT_APPLICABLE,
        ),
        (segment,),
        (word,),
        (),
    )


def _local_speech(root: RootMediaEvidenceBundle) -> SpeechActivitySet:
    context = replace(
        root.speech_activity.context,
        origin_tick=_LOCAL_AUDIO_RANGE.start_pts,
        duration_tick=_LOCAL_AUDIO_RANGE.end_pts - _LOCAL_AUDIO_RANGE.start_pts,
    )
    return SpeechActivitySet(
        "candidate-vad",
        context,
        _coverage(context),
        SpeechSourceOutcome.SPEECH_DETECTED,
        (
            SpeechActivitySegment(
                "candidate-speech",
                SOURCE_ID,
                SOURCE_HASH,
                context.clock_id,
                context.time_base,
                23,
                25,
                900_000,
            ),
        ),
    )


def _profile(
    transcript: TranscriptSet,
    speech: SpeechActivitySet,
    *,
    pre_roll_tick: int = 0,
    post_roll_tick: int = 0,
) -> TimedSpeechProfileRegistryEntry:
    def requirement(
        evidence: TranscriptSet | SpeechActivitySet,
        *,
        producer_kind: str,
        model_sha256: str,
        calibration_sha256: str,
    ) -> TimedSpeechProducerRequirement:
        context = evidence.context
        return TimedSpeechProducerRequirement(
            producer_id=context.producer_id,
            generation_policy_sha256=context.generation_policy_sha256,
            model_sha256=model_sha256,
            adapter_sha256=HASH_A,
            calibration_record_sha256=calibration_sha256,
            clock_id=context.clock_id,
            time_base=context.time_base,
            producer_kind=producer_kind,
            inference_kind=(
                "sensevoice-word-timestamp" if producer_kind == "asr" else "fsmn-vad-direct"
            ),
        )

    return TimedSpeechProfileRegistryEntry(
        profile_id="candidate-word-guard",
        profile_version="1",
        kind=TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1,
        capability=TimedSpeechCapability.KNOWN_SPEECH_ONLY,
        transcript_requirement=requirement(
            transcript,
            producer_kind="asr",
            model_sha256=HASH_B,
            calibration_sha256=HASH_C,
        ),
        vad_requirement=requirement(
            speech,
            producer_kind="vad",
            model_sha256=HASH_C,
            calibration_sha256=HASH_D,
        ),
        guard_policy=TimedSpeechGuardPolicy(
            HASH_A,
            transcript.context.clock_id,
            transcript.context.time_base,
            word_gap_tick=0,
            vad_merge_gap_tick=0,
            pre_roll_tick=pre_roll_tick,
            post_roll_tick=post_roll_tick,
        ),
        registry_contract_sha256=HASH_A,
    )


def _candidate_bindings(
    values: tuple[object, ...],
    transcript: TranscriptSet,
    speech: SpeechActivitySet,
) -> tuple[CalibrationBinding, ...]:
    bindings = _bindings(values)
    return tuple(
        replace(
            item,
            detector_sha256=(
                HASH_B
                if item.producer_id == transcript.context.producer_id
                else (HASH_C if item.producer_id == speech.context.producer_id else item.detector_sha256)
            ),
            calibration_record_sha256=(
                HASH_C
                if item.producer_id == transcript.context.producer_id
                else (
                    HASH_D
                    if item.producer_id == speech.context.producer_id
                    else item.calibration_record_sha256
                )
            ),
        )
        for item in bindings
    )


def candidate_dialogue_case(
    *,
    vad_only: bool = False,
    pre_roll_tick: int = 0,
    post_roll_tick: int = 0,
) -> tuple[
    RootMediaEvidenceBundle,
    CandidateTimedEvidenceSet,
    CandidateEvidenceWindowPlan,
    TimedSpeechProfileRegistryEntry,
]:
    """A strict local ASR/VAD window over an unchanged full root bundle."""
    root = _bundle()
    initial, manifest, policy = _initial_plan()
    plan = advance_candidate_evidence_window(
        initial,
        _assessment(initial.final_window, sentence=SentenceCompleteness.NOT_APPLICABLE),
        manifest.frame_pts_index_set,
        policy,
    )
    transcript = _local_transcript(root, vad_only=vad_only)
    speech = _local_speech(root)
    physical = (
        root.audio_sample_boundaries,
        root.frame_pts_index,
        root.shot_boundaries,
        root.scene_boundaries,
        root.visual_validity,
        root.subtitle_cues,
    )
    bindings = _candidate_bindings((transcript, speech, *physical), transcript, speech)
    candidate = CandidateTimedEvidenceSet(
        plan.final_window,
        plan.final_assessment,
        transcript,
        speech,
        root.audio_sample_boundaries,
        root.frame_pts_index,
        root.shot_boundaries,
        root.scene_boundaries,
        root.visual_validity,
        root.subtitle_cues,
        bindings,
    )
    return root, candidate, plan, _profile(
        transcript,
        speech,
        pre_roll_tick=pre_roll_tick,
        post_roll_tick=post_roll_tick,
    )


def test_local_candidate_guard_needs_no_full_episode_asr_and_does_not_mutate_root() -> None:
    root, candidate, plan, profile = candidate_dialogue_case()
    root_mapping = root.to_mapping()
    root_hash = root.canonical_hash

    guard = derive_candidate_dialogue_guard(
        root,
        candidate,
        plan,
        profile,
        DialogueRequirement.NOT_REQUIRED,
    )

    assert candidate.transcript.coverage.to_mapping() != root.transcript.coverage.to_mapping()
    assert guard.kind is DialogueGuardKind.NOT_REQUIRED
    assert guard.source_audio_range == _LOCAL_AUDIO_RANGE
    assert [(item.in_tick, item.out_tick) for item in guard.protected_ranges] == [(23, 25)]
    assert root.to_mapping() == root_mapping
    assert root.canonical_hash == root_hash
    verify_candidate_dialogue_guard(
        root,
        candidate,
        plan,
        profile,
        DialogueRequirement.NOT_REQUIRED,
        guard,
    )


def test_word_only_candidate_cannot_prove_complete_dialogue() -> None:
    root, candidate, plan, profile = candidate_dialogue_case()

    with pytest.raises(DialogueGuardIndeterminateError, match="word guard cannot satisfy"):
        derive_candidate_dialogue_guard(
            root,
            candidate,
            plan,
            profile,
            DialogueRequirement.COMPLETE,
        )


def test_unknown_sentence_assessment_is_rejected_by_candidate_guard() -> None:
    root, candidate, plan, profile = candidate_dialogue_case()
    assessment = replace(
        candidate.window_assessment,
        sentence_completeness=SentenceCompleteness.UNKNOWN,
    )
    unknown_plan = replace(plan, assessments=(assessment,))
    unknown_candidate = replace(candidate, window_assessment=assessment)

    with pytest.raises(DialogueGuardIndeterminateError, match="sentence assessment is unknown"):
        derive_candidate_dialogue_guard(
            root,
            unknown_candidate,
            unknown_plan,
            profile,
            DialogueRequirement.NOT_REQUIRED,
        )


def test_vad_only_candidate_preserves_local_speech_protection() -> None:
    root, candidate, plan, profile = candidate_dialogue_case(vad_only=True)

    guard = derive_candidate_dialogue_guard(
        root,
        candidate,
        plan,
        profile,
        DialogueRequirement.NOT_REQUIRED,
    )

    assert candidate.transcript.words == ()
    assert [(item.in_tick, item.out_tick) for item in guard.protected_ranges] == [(23, 25)]


@pytest.mark.parametrize("field_name", ("boundary_touch_left", "truncated"))
def test_candidate_transcript_touch_or_truncation_fails_closed(field_name: str) -> None:
    root, candidate, plan, profile = candidate_dialogue_case()
    transcript = candidate.transcript
    if field_name == "truncated":
        transcript = replace(
            transcript,
            coverage=_coverage(transcript.context, outcome=CoverageOutcome.PARTIAL),
            truncated=True,
        )
    else:
        transcript = replace(transcript, boundary_touch_left=True)
    altered = replace(candidate, transcript=transcript)

    with pytest.raises(DialogueGuardError, match="truncated or boundary-touching"):
        derive_candidate_dialogue_guard(
            root,
            altered,
            plan,
            profile,
            DialogueRequirement.NOT_REQUIRED,
        )


def test_candidate_partial_word_evidence_is_indeterminate() -> None:
    root, candidate, plan, profile = candidate_dialogue_case()
    transcript = replace(
        candidate.transcript,
        completeness=replace(candidate.transcript.completeness, word=EvidenceCompleteness.PARTIAL),
    )

    with pytest.raises(DialogueGuardIndeterminateError, match="partial or failed"):
        derive_candidate_dialogue_guard(
            root,
            replace(candidate, transcript=transcript),
            plan,
            profile,
            DialogueRequirement.NOT_REQUIRED,
        )


def test_candidate_guard_rejects_foreign_profile_and_calibration_hash() -> None:
    root, candidate, plan, profile = candidate_dialogue_case()
    foreign_profile = replace(
        profile,
        transcript_requirement=replace(
            profile.transcript_requirement,
            generation_policy_sha256=HASH_D,
        ),
    )
    bindings = tuple(
        replace(item, detector_sha256=HASH_D)
        if item.producer_id == candidate.transcript.context.producer_id
        else item
        for item in candidate.calibration_bindings
    )

    with pytest.raises(DialogueGuardError, match="does not bind original root/profile"):
        derive_candidate_dialogue_guard(
            root,
            candidate,
            plan,
            foreign_profile,
            DialogueRequirement.NOT_REQUIRED,
        )
    with pytest.raises(DialogueGuardError, match="model/adapter/calibration"):
        derive_candidate_dialogue_guard(
            root,
            replace(candidate, calibration_bindings=bindings),
            plan,
            profile,
            DialogueRequirement.NOT_REQUIRED,
        )


def test_candidate_roll_cannot_escape_its_proven_local_coverage() -> None:
    root, candidate, plan, profile = candidate_dialogue_case(pre_roll_tick=11, post_roll_tick=20)

    with pytest.raises(DialogueGuardError, match="rolls exceed proven local coverage"):
        derive_candidate_dialogue_guard(
            root,
            candidate,
            plan,
            profile,
            DialogueRequirement.NOT_REQUIRED,
        )
