"""Migration vectors for the closed Stage 4 dialogue guard."""

from __future__ import annotations

from dataclasses import replace

import pytest
from autocut_kernel.media import (
    AudioBoundaryMethod,
    AudioSampleBoundary,
    AudioSampleBoundarySet,
    AudioSourceOutcome,
    Coverage,
    CoverageOutcome,
    EvidenceCompleteness,
    EvidenceContext,
    FramePtsIndexSet,
    MediaKind,
    PTSIndex,
    RootMediaEvidenceBundle,
    SceneBoundarySet,
    ShotBoundarySet,
    SpeechActivitySegment,
    SpeechActivitySet,
    SpeechSourceOutcome,
    SubtitleCueSet,
    SubtitleDetectionMode,
    SubtitleSourceOutcome,
    TimeBase,
    TranscriptCompleteness,
    TranscriptSegment,
    TranscriptSentence,
    TranscriptSet,
    TranscriptSourceOutcome,
    TranscriptWord,
    VisualClassification,
    VisualValidityInterval,
    VisualValiditySet,
)
from autocut_kernel.media.types import MediaValidationError, TickRange, canonical_sha256
from autocut_kernel.physical_edit import (
    DialogueGuardError,
    DialogueGuardIndeterminateError,
    DialogueGuardKind,
    DialogueRequirement,
    ExactAvSpanPolicy,
    ExactAvSpanRequest,
    ExactSpanValidationError,
    TimedSpeechGuardPolicy,
    TimedSpeechProducerRecord,
    TimedSpeechProfile,
    TimedSpeechProfileBinding,
    TimedSpeechProfileKind,
    VideoClockRange,
    VideoToAudioClockMapCertificate,
    compile_exact_av_span,
    derive_dialogue_guard,
    derive_utterance_ranges,
    merge_vad_ranges,
)

SOURCE_ID = "dialogue-guard-source"
SOURCE_HASH = "sha256:" + "1" * 64
MODEL_HASH = "sha256:" + "2" * 64
ADAPTER_HASH = "sha256:" + "3" * 64
CALIBRATION_HASH = "sha256:" + "4" * 64
POLICY_HASH = "sha256:" + "5" * 64
VIDEO_BASE = TimeBase(1, 90_000)
AUDIO_BASE = TimeBase(1, 48_000)
VIDEO_CLOCK = "dialogue-guard-source:video"
AUDIO_CLOCK = "dialogue-guard-source:audio"
VIDEO_TICKS = (0, 60, 120, 180)
AUDIO_TICKS = (0, 32, 64, 96)


def _context(kind: MediaKind, producer: str) -> EvidenceContext:
    return EvidenceContext(
        SOURCE_ID,
        SOURCE_HASH,
        kind,
        VIDEO_CLOCK if kind is MediaKind.VIDEO else AUDIO_CLOCK,
        VIDEO_BASE if kind is MediaKind.VIDEO else AUDIO_BASE,
        0,
        180 if kind is MediaKind.VIDEO else 96,
        producer,
        MODEL_HASH,
    )


def _coverage(context: EvidenceContext) -> Coverage:
    return Coverage(
        context.source_id,
        context.source_sha256,
        context.clock_id,
        context.time_base,
        context.origin_tick,
        context.end_tick,
        CoverageOutcome.COMPLETE,
    )


def _word(word_id: str, start: int, end: int) -> TranscriptWord:
    return TranscriptWord(word_id, SOURCE_ID, SOURCE_HASH, AUDIO_CLOCK, AUDIO_BASE, start, end, word_id)


def _bundle(
    *,
    words: tuple[TranscriptWord, ...] = (),
    vad_ranges: tuple[tuple[int, int], ...] = (),
    lexical_outcome: TranscriptSourceOutcome = TranscriptSourceOutcome.NO_SPEECH,
    audio: bool = True,
) -> RootMediaEvidenceBundle:
    video = _context(MediaKind.VIDEO, "frame-index")
    audio_context = _context(MediaKind.AUDIO, "audio-boundary")
    frames = FramePtsIndexSet(
        "frames", video, _coverage(video), PTSIndex(VIDEO_TICKS), canonical_sha256(list(VIDEO_TICKS))
    )
    shots = ShotBoundarySet(
        "shots", replace(video, producer_id="shots"), _coverage(replace(video, producer_id="shots")), frames.canonical_hash, ()
    )
    scenes = SceneBoundarySet(
        "scenes", replace(video, producer_id="scenes"), _coverage(replace(video, producer_id="scenes")), frames.canonical_hash, ()
    )
    audio_points = tuple(
        AudioSampleBoundary(
            f"audio-{tick}", SOURCE_ID, SOURCE_HASH, AUDIO_CLOCK, AUDIO_BASE, tick, AudioBoundaryMethod.DECODER
        )
        for tick in AUDIO_TICKS
    )
    boundaries = AudioSampleBoundarySet(
        "audio",
        audio_context,
        _coverage(audio_context),
        AudioSourceOutcome.BOUNDARIES_AVAILABLE if audio else AudioSourceOutcome.NOT_APPLICABLE,
        audio_points if audio else (),
    )
    transcript_context = replace(audio_context, producer_id="sensevoice")
    if not audio:
        transcript = TranscriptSet(
            "transcript-na",
            transcript_context,
            _coverage(transcript_context),
            TranscriptSourceOutcome.NOT_APPLICABLE,
            TranscriptCompleteness(*([EvidenceCompleteness.NOT_APPLICABLE] * 3)),
            (), (), (),
        )
        vad = SpeechActivitySet(
            "vad-na", replace(audio_context, producer_id="vad"), _coverage(replace(audio_context, producer_id="vad")), SpeechSourceOutcome.NOT_APPLICABLE, ()
        )
    elif lexical_outcome is TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE:
        segment = TranscriptSegment(
            "segment-1", SOURCE_ID, SOURCE_HASH, AUDIO_CLOCK, AUDIO_BASE, words[0].in_tick, words[-1].out_tick, (), "opaque"
        )
        transcript = TranscriptSet(
            "word-only",
            transcript_context,
            _coverage(transcript_context),
            lexical_outcome,
            TranscriptCompleteness(EvidenceCompleteness.COMPLETE, EvidenceCompleteness.COMPLETE, EvidenceCompleteness.NOT_APPLICABLE),
            (segment,), words, (),
        )
        vad = _vad(audio_context, vad_ranges)
    elif lexical_outcome is TranscriptSourceOutcome.NO_LEXICAL_CONTENT:
        transcript = TranscriptSet(
            "non-lexical",
            transcript_context,
            _coverage(transcript_context),
            lexical_outcome,
            TranscriptCompleteness(EvidenceCompleteness.COMPLETE, EvidenceCompleteness.COMPLETE, EvidenceCompleteness.NOT_APPLICABLE),
            (), (), (),
        )
        vad = _vad(audio_context, vad_ranges)
    else:
        transcript = TranscriptSet(
            "no-speech",
            transcript_context,
            _coverage(transcript_context),
            TranscriptSourceOutcome.NO_SPEECH,
            TranscriptCompleteness(*([EvidenceCompleteness.COMPLETE] * 3)),
            (), (), (),
        )
        vad = _vad(audio_context, ())
    visual_context = replace(video, producer_id="visual")
    visual = VisualValiditySet(
        "visual",
        visual_context,
        _coverage(visual_context),
        (VisualValidityInterval(
            "visual-1", SOURCE_ID, SOURCE_HASH, VIDEO_CLOCK, VIDEO_BASE, 0, 180,
            VisualClassification.VALID_CONTENT, 1_000_000,
        ),),
    )
    subtitle_context = replace(video, producer_id="subtitle")
    subtitles = SubtitleCueSet(
        "subtitles", subtitle_context, _coverage(subtitle_context),
        (SubtitleDetectionMode.EMBEDDED,), (SubtitleDetectionMode.EMBEDDED,),
        SubtitleSourceOutcome.NONE_DETECTED, (),
    )
    return RootMediaEvidenceBundle(
        "root", SOURCE_ID, SOURCE_HASH, MODEL_HASH, ADAPTER_HASH, frames, shots, scenes,
        boundaries, transcript, vad, visual, subtitles,
    )


def _vad(context: EvidenceContext, ranges: tuple[tuple[int, int], ...]) -> SpeechActivitySet:
    vad_context = replace(context, producer_id="vad")
    if not ranges:
        return SpeechActivitySet("vad-none", vad_context, _coverage(vad_context), SpeechSourceOutcome.NONE_DETECTED, ())
    return SpeechActivitySet(
        "vad",
        vad_context,
        _coverage(vad_context),
        SpeechSourceOutcome.SPEECH_DETECTED,
        tuple(
            SpeechActivitySegment(
                f"vad-{position}", SOURCE_ID, SOURCE_HASH, AUDIO_CLOCK, AUDIO_BASE, start, end, 1_000_000
            )
            for position, (start, end) in enumerate(ranges)
        ),
    )


def _profile(kind: TimedSpeechProfileKind = TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1) -> TimedSpeechProfile:
    return TimedSpeechProfile(kind, kind.value, "1.0.0", MODEL_HASH, ADAPTER_HASH, CALIBRATION_HASH)


def _guard_policy() -> TimedSpeechGuardPolicy:
    return TimedSpeechGuardPolicy(
        AUDIO_CLOCK, AUDIO_BASE, POLICY_HASH, word_gap_tick=7, vad_merge_gap_tick=2, pre_roll_tick=2,
        post_roll_tick=3,
    )


def _binding(
    bundle: RootMediaEvidenceBundle,
    kind: TimedSpeechProfileKind = TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1,
) -> TimedSpeechProfileBinding:
    def producer(evidence_set: TranscriptSet | SpeechActivitySet) -> TimedSpeechProducerRecord:
        context = evidence_set.context
        return TimedSpeechProducerRecord(
            evidence_set.canonical_hash,
            context.source_id,
            context.source_sha256,
            context.clock_id,
            context.time_base,
            context.producer_id,
            context.generation_policy_sha256,
            MODEL_HASH,
            ADAPTER_HASH,
            CALIBRATION_HASH,
        )

    return TimedSpeechProfileBinding(
        _profile(kind), POLICY_HASH, producer(bundle.transcript), producer(bundle.speech_activity)
    )


def _request(bundle: RootMediaEvidenceBundle, requirement: DialogueRequirement) -> ExactAvSpanRequest:
    video = bundle.frame_pts_index.context
    desired = VideoClockRange(SOURCE_ID, SOURCE_HASH, video.clock_id, video.time_base, TickRange(0, 180))
    anchor = VideoClockRange(SOURCE_ID, SOURCE_HASH, video.clock_id, video.time_base, TickRange(60, 120))
    return ExactAvSpanRequest(desired, anchor, 40, requirement)


def _exact_policy(bundle: RootMediaEvidenceBundle) -> ExactAvSpanPolicy:
    return ExactAvSpanPolicy(1_000, 1, 1, 0, _binding(bundle), _guard_policy())


def _sentence_complete_bundle() -> RootMediaEvidenceBundle:
    bundle = _bundle(
        words=(_word("one", 36, 40),),
        vad_ranges=((42, 48),),
        lexical_outcome=TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE,
    )
    word = bundle.transcript.words[0]
    sentence = TranscriptSentence(
        "sentence-1", SOURCE_ID, SOURCE_HASH, AUDIO_CLOCK, AUDIO_BASE, 36, 40, (word.word_id,), "opaque"
    )
    segment = TranscriptSegment(
        "segment-1", SOURCE_ID, SOURCE_HASH, AUDIO_CLOCK, AUDIO_BASE, 36, 40, (sentence.sentence_id,), "opaque"
    )
    transcript = TranscriptSet(
        "sentence-complete",
        bundle.transcript.context,
        bundle.transcript.coverage,
        TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE,
        TranscriptCompleteness(*([EvidenceCompleteness.COMPLETE] * 3)),
        (segment,),
        (word,),
        (sentence,),
    )
    return replace(bundle, transcript=transcript)


def test_word_only_non_dialogue_derives_deterministic_gap_and_vad_protection() -> None:
    bundle = _bundle(
        words=(_word("one", 20, 22), _word("two", 29, 30), _word("three", 38, 40)),
        vad_ranges=((42, 44), (46, 48)),
        lexical_outcome=TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE,
    )

    assert derive_utterance_ranges(bundle, _guard_policy()) == ((20, 30), (38, 40))
    assert merge_vad_ranges(bundle, _guard_policy()) == ((42, 48),)
    first = derive_dialogue_guard(bundle, _binding(bundle), _guard_policy(), DialogueRequirement.NOT_REQUIRED)
    second = derive_dialogue_guard(bundle, _binding(bundle), _guard_policy(), DialogueRequirement.NOT_REQUIRED)

    assert first.kind is DialogueGuardKind.NOT_REQUIRED
    assert [(item.in_tick, item.out_tick) for item in first.protected_ranges] == [(18, 33), (36, 51)]
    assert first.canonical_hash == second.canonical_hash
    assert first.protected_ranges_sha256 == second.protected_ranges_sha256


def test_word_only_complete_dialogue_is_indeterminate_and_cannot_compile_recipe() -> None:
    bundle = _bundle(
        words=(_word("one", 36, 40),),
        vad_ranges=((42, 48),),
        lexical_outcome=TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE,
    )

    with pytest.raises(DialogueGuardIndeterminateError, match="cannot satisfy complete dialogue"):
        derive_dialogue_guard(bundle, _binding(bundle), _guard_policy(), DialogueRequirement.COMPLETE)
    with pytest.raises(ExactSpanValidationError, match="cannot satisfy complete dialogue"):
        compile_exact_av_span(
            _request(bundle, DialogueRequirement.COMPLETE),
            bundle,
            VideoToAudioClockMapCertificate.from_root_evidence(bundle),
            _exact_policy(bundle),
        )


def test_only_sentence_profile_with_complete_membership_can_satisfy_required_dialogue() -> None:
    bundle = _sentence_complete_bundle()

    guard = derive_dialogue_guard(
        bundle,
        _binding(bundle, TimedSpeechProfileKind.SENTENCE_BOUNDARY_GUARD_V1),
        _guard_policy(),
        DialogueRequirement.COMPLETE,
    )

    assert guard.kind is DialogueGuardKind.REQUIRED
    assert [(item.in_tick, item.out_tick) for item in guard.protected_ranges] == [(34, 51)]


def test_bare_profile_is_not_a_registered_sentence_capability() -> None:
    bundle = _sentence_complete_bundle()

    with pytest.raises(DialogueGuardError, match="registered timed speech binding"):
        derive_dialogue_guard(
            bundle,
            _profile(TimedSpeechProfileKind.SENTENCE_BOUNDARY_GUARD_V1),  # type: ignore[arg-type]
            _guard_policy(),
            DialogueRequirement.COMPLETE,
        )


@pytest.mark.parametrize("field_name", ["producer_model_sha256", "adapter_sha256", "calibration_sha256"])
def test_claimed_profile_identity_must_match_the_registered_transcript_producer(
    field_name: str,
) -> None:
    bundle = _sentence_complete_bundle()
    binding = _binding(bundle, TimedSpeechProfileKind.SENTENCE_BOUNDARY_GUARD_V1)
    forged_profile = replace(binding.profile, **{field_name: "sha256:" + "f" * 64})

    with pytest.raises(DialogueGuardError, match="profile identity does not match"):
        TimedSpeechProfileBinding(
            forged_profile,
            binding.timed_speech_policy_sha256,
            binding.transcript_producer,
            binding.vad_producer,
        )


def test_registered_sentence_profile_rejects_source_or_committed_record_mismatch() -> None:
    bundle = _sentence_complete_bundle()
    binding = _binding(bundle, TimedSpeechProfileKind.SENTENCE_BOUNDARY_GUARD_V1)
    wrong_source = replace(
        binding,
        transcript_producer=replace(binding.transcript_producer, source_id="other-source"),
        vad_producer=replace(binding.vad_producer, source_id="other-source"),
    )
    wrong_record = replace(
        binding,
        transcript_producer=replace(
            binding.transcript_producer, evidence_set_sha256="sha256:" + "f" * 64
        ),
    )

    with pytest.raises(DialogueGuardError, match="source audio clock"):
        derive_dialogue_guard(bundle, wrong_source, _guard_policy(), DialogueRequirement.COMPLETE)
    with pytest.raises(DialogueGuardError, match="committed evidence"):
        derive_dialogue_guard(bundle, wrong_record, _guard_policy(), DialogueRequirement.COMPLETE)


def test_registered_policy_and_audio_clock_units_must_match_the_source() -> None:
    bundle = _sentence_complete_bundle()
    binding = _binding(bundle, TimedSpeechProfileKind.SENTENCE_BOUNDARY_GUARD_V1)
    wrong_policy = replace(_guard_policy(), timed_speech_policy_sha256=MODEL_HASH)
    wrong_clock = replace(_guard_policy(), clock_id="other-audio-clock")

    with pytest.raises(DialogueGuardError, match="binding policy"):
        derive_dialogue_guard(bundle, binding, wrong_policy, DialogueRequirement.COMPLETE)
    with pytest.raises(DialogueGuardError, match="policy units"):
        derive_utterance_ranges(bundle, wrong_clock)
    with pytest.raises(DialogueGuardError, match="policy units"):
        merge_vad_ranges(bundle, wrong_clock)


@pytest.mark.parametrize("component", ["segment", "word", "sentence"])
def test_partial_transcript_components_fail_closed_for_audio(component: str) -> None:
    bundle = _sentence_complete_bundle()
    transcript = bundle.transcript
    completeness = replace(transcript.completeness, **{component: EvidenceCompleteness.PARTIAL})
    partial = replace(bundle, transcript=replace(transcript, completeness=completeness))

    with pytest.raises(DialogueGuardError, match="transcript"):
        derive_dialogue_guard(
            partial,
            _binding(partial, TimedSpeechProfileKind.SENTENCE_BOUNDARY_GUARD_V1),
            _guard_policy(),
            DialogueRequirement.NOT_REQUIRED,
        )


def test_partial_vad_coverage_and_truncation_fail_closed_for_audio() -> None:
    bundle = _sentence_complete_bundle()
    object.__setattr__(bundle.speech_activity.coverage, "outcome", CoverageOutcome.PARTIAL)
    with pytest.raises(DialogueGuardError, match="complete VAD coverage"):
        derive_dialogue_guard(
            bundle,
            _binding(bundle, TimedSpeechProfileKind.SENTENCE_BOUNDARY_GUARD_V1),
            _guard_policy(),
            DialogueRequirement.NOT_REQUIRED,
        )

    bundle = _sentence_complete_bundle()
    object.__setattr__(bundle.transcript, "truncated", True)
    with pytest.raises(DialogueGuardError, match="truncated transcript"):
        derive_dialogue_guard(
            bundle,
            _binding(bundle, TimedSpeechProfileKind.SENTENCE_BOUNDARY_GUARD_V1),
            _guard_policy(),
            DialogueRequirement.NOT_REQUIRED,
        )


def test_vad_only_nonlexical_audio_is_not_required_and_keeps_protection() -> None:
    bundle = _bundle(
        vad_ranges=((42, 44), (46, 48)), lexical_outcome=TranscriptSourceOutcome.NO_LEXICAL_CONTENT
    )

    guard = derive_dialogue_guard(bundle, _binding(bundle), _guard_policy(), DialogueRequirement.NOT_REQUIRED)

    assert guard.kind is DialogueGuardKind.NOT_REQUIRED
    assert [(item.in_tick, item.out_tick) for item in guard.protected_ranges] == [(40, 51)]


def test_video_only_is_the_only_not_applicable_guard_arm() -> None:
    bundle = _bundle(audio=False)

    guard = derive_dialogue_guard(bundle, _binding(bundle), _guard_policy(), DialogueRequirement.NOT_REQUIRED)

    assert guard.kind is DialogueGuardKind.NOT_APPLICABLE
    assert guard.to_mapping() == {"kind": "not_applicable", "reason": "no_audio"}


def test_malformed_word_timing_fails_closed_at_the_input_boundary() -> None:
    with pytest.raises(MediaValidationError, match="word must satisfy"):
        _word("bad", 20, 20)
