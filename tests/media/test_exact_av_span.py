"""Contract tests for the canonical exact A/V span compiler."""

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
    SubtitleCue,
    SubtitleCueSet,
    SubtitleDetectionMode,
    SubtitleKind,
    SubtitleSourceOutcome,
    TimeBase,
    TimingErrorBound,
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
from autocut_kernel.media.types import TickRange, canonical_sha256
from autocut_kernel.physical_edit import (
    CandidatePairLimitError,
    ClockMapOutcome,
    ExactAvSpanPolicy,
    ExactAvSpanRequest,
    ExactSpanValidationError,
    NoLegalSpanError,
    VideoClockRange,
    VideoToAudioClockMapCertificate,
    VideoToAudioMapSegment,
    compile_exact_av_span,
)
from autocut_kernel.vlm import MappedSourceInterval

SOURCE_ID = "source-exact-av"
SOURCE_HASH = "sha256:" + "1" * 64
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
VIDEO_BASE = TimeBase(1, 90_000)
AUDIO_BASE = TimeBase(1, 48_000)
VIDEO_CLOCK = "source-exact-av:video"
AUDIO_CLOCK = "source-exact-av:audio"


def _context(kind: MediaKind, producer: str) -> EvidenceContext:
    return EvidenceContext(
        SOURCE_ID,
        SOURCE_HASH,
        kind,
        VIDEO_CLOCK if kind is MediaKind.VIDEO else AUDIO_CLOCK,
        VIDEO_BASE if kind is MediaKind.VIDEO else AUDIO_BASE,
        0,
        100,
        producer,
        HASH_A,
    )


def _coverage(context: EvidenceContext) -> Coverage:
    return Coverage(
        context.source_id,
        context.source_sha256,
        context.clock_id,
        context.time_base,
        0,
        100,
        CoverageOutcome.COMPLETE,
    )


def _frame_set(ticks: tuple[int, ...]) -> FramePtsIndexSet:
    context = _context(MediaKind.VIDEO, "frame-v1")
    index = PTSIndex(ticks)
    return FramePtsIndexSet(
        "frames-vfr",
        context,
        _coverage(context),
        index,
        canonical_sha256(list(ticks)),
    )


def _audio_set(
    ticks: tuple[int, ...], *, present: bool = True
) -> AudioSampleBoundarySet:
    context = _context(MediaKind.AUDIO, "audio-v1")
    points = tuple(
        AudioSampleBoundary(
            f"audio-{tick:03d}",
            SOURCE_ID,
            SOURCE_HASH,
            AUDIO_CLOCK,
            AUDIO_BASE,
            tick,
            AudioBoundaryMethod.DECODER,
        )
        for tick in ticks
    )
    return AudioSampleBoundarySet(
        "audio-boundaries",
        context,
        _coverage(context),
        AudioSourceOutcome.BOUNDARIES_AVAILABLE if present else AudioSourceOutcome.NOT_APPLICABLE,
        points if present else (),
    )


def _transcript(*, protected: tuple[int, int] | None, audio_present: bool) -> TranscriptSet:
    context = _context(MediaKind.AUDIO, "asr-v1")
    if not audio_present:
        return TranscriptSet(
            "transcript-na",
            context,
            _coverage(context),
            TranscriptSourceOutcome.NOT_APPLICABLE,
            TranscriptCompleteness(*([EvidenceCompleteness.NOT_APPLICABLE] * 3)),
            (),
            (),
            (),
        )
    if protected is None:
        return TranscriptSet(
            "transcript-none",
            context,
            _coverage(context),
            TranscriptSourceOutcome.NO_SPEECH,
            TranscriptCompleteness(*([EvidenceCompleteness.COMPLETE] * 3)),
            (),
            (),
            (),
        )
    start, end = protected
    word = TranscriptWord(
        "word-1", SOURCE_ID, SOURCE_HASH, AUDIO_CLOCK, AUDIO_BASE, start, end, "word"
    )
    sentence = TranscriptSentence(
        "sentence-1",
        SOURCE_ID,
        SOURCE_HASH,
        AUDIO_CLOCK,
        AUDIO_BASE,
        start,
        end,
        (word.word_id,),
        "word",
    )
    segment = TranscriptSegment(
        "segment-1",
        SOURCE_ID,
        SOURCE_HASH,
        AUDIO_CLOCK,
        AUDIO_BASE,
        start,
        end,
        (sentence.sentence_id,),
        "word",
    )
    return TranscriptSet(
        "transcript-present",
        context,
        _coverage(context),
        TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE,
        TranscriptCompleteness(*([EvidenceCompleteness.COMPLETE] * 3)),
        (segment,),
        (word,),
        (sentence,),
    )


def _vad(*, protected: tuple[int, int] | None, audio_present: bool) -> SpeechActivitySet:
    context = _context(MediaKind.AUDIO, "vad-v1")
    if not audio_present:
        return SpeechActivitySet(
            "vad-na",
            context,
            _coverage(context),
            SpeechSourceOutcome.NOT_APPLICABLE,
            (),
        )
    if protected is None:
        return SpeechActivitySet(
            "vad-none",
            context,
            _coverage(context),
            SpeechSourceOutcome.NONE_DETECTED,
            (),
        )
    return SpeechActivitySet(
        "vad-present",
        context,
        _coverage(context),
        SpeechSourceOutcome.SPEECH_DETECTED,
        (
            SpeechActivitySegment(
                "vad-1", SOURCE_ID, SOURCE_HASH, AUDIO_CLOCK, AUDIO_BASE, *protected, 990_000
            ),
        ),
    )


def _visual(
    intervals: tuple[tuple[int, int, VisualClassification], ...],
) -> VisualValiditySet:
    context = _context(MediaKind.VIDEO, "visual-v1")
    return VisualValiditySet(
        "visual-set",
        context,
        _coverage(context),
        tuple(
            VisualValidityInterval(
                f"visual-{position}",
                SOURCE_ID,
                SOURCE_HASH,
                VIDEO_CLOCK,
                VIDEO_BASE,
                start,
                end,
                classification,
                990_000,
            )
            for position, (start, end, classification) in enumerate(intervals)
        ),
    )


def _subtitles(cue: tuple[int, int, int, int] | None) -> SubtitleCueSet:
    context = _context(MediaKind.VIDEO, "subtitle-v1")
    cues: tuple[SubtitleCue, ...] = ()
    outcome = SubtitleSourceOutcome.NONE_DETECTED
    if cue is not None:
        start, end, in_error, out_error = cue
        cues = (
            SubtitleCue(
                "cue-1",
                SOURCE_ID,
                SOURCE_HASH,
                VIDEO_CLOCK,
                VIDEO_BASE,
                start,
                end,
                SubtitleKind.SUBTITLE,
                SubtitleDetectionMode.EMBEDDED,
                990_000,
                TimingErrorBound(VIDEO_BASE, in_error, out_error),
            ),
        )
        outcome = SubtitleSourceOutcome.CUES_DETECTED
    return SubtitleCueSet(
        "subtitle-set",
        context,
        _coverage(context),
        (SubtitleDetectionMode.EMBEDDED,),
        (SubtitleDetectionMode.EMBEDDED,),
        outcome,
        cues,
    )


def _bundle(
    *,
    frame_ticks: tuple[int, ...] = (0, 11, 37, 64, 100),
    audio_ticks: tuple[int, ...] = (0, 11, 37, 64, 100),
    transcript_protected: tuple[int, int] | None = None,
    vad_protected: tuple[int, int] | None = None,
    visual: tuple[tuple[int, int, VisualClassification], ...] = (
        (0, 100, VisualClassification.VALID_CONTENT),
    ),
    subtitle: tuple[int, int, int, int] | None = None,
    audio_present: bool = True,
) -> RootMediaEvidenceBundle:
    frame_set = _frame_set(frame_ticks)
    video_hash = frame_set.canonical_hash
    video_context = frame_set.context
    shots = ShotBoundarySet(
        "shots", replace(video_context, producer_id="shot-v1"),
        _coverage(replace(video_context, producer_id="shot-v1")), video_hash, (),
    )
    scenes = SceneBoundarySet(
        "scenes", replace(video_context, producer_id="scene-v1"),
        _coverage(replace(video_context, producer_id="scene-v1")), video_hash, (),
    )
    return RootMediaEvidenceBundle(
        "root-exact-av",
        SOURCE_ID,
        SOURCE_HASH,
        HASH_A,
        HASH_B,
        frame_set,
        shots,
        scenes,
        _audio_set(audio_ticks, present=audio_present),
        _transcript(protected=transcript_protected, audio_present=audio_present),
        _vad(protected=vad_protected, audio_present=audio_present),
        _visual(visual),
        _subtitles(subtitle),
    )


def _request(bundle: RootMediaEvidenceBundle) -> ExactAvSpanRequest:
    context = bundle.frame_pts_index.context
    desired = VideoClockRange(SOURCE_ID, SOURCE_HASH, context.clock_id, context.time_base, TickRange(0, 100))
    anchor = VideoClockRange(SOURCE_ID, SOURCE_HASH, context.clock_id, context.time_base, TickRange(37, 64))
    return ExactAvSpanRequest(desired, anchor, 20)


def _clock_map(
    bundle: RootMediaEvidenceBundle,
    *,
    error: int = 0,
    segments: tuple[VideoToAudioMapSegment, ...] | None = None,
    outcome: ClockMapOutcome = ClockMapOutcome.COMPLETE,
) -> VideoToAudioClockMapCertificate:
    if segments is None:
        segments = (VideoToAudioMapSegment(TickRange(0, 100), TickRange(0, 100), error),)
    return VideoToAudioClockMapCertificate(
        SOURCE_ID,
        SOURCE_HASH,
        bundle.frame_pts_index.context.clock_id,
        bundle.frame_pts_index.context.time_base,
        bundle.audio_sample_boundaries.context.clock_id,
        bundle.audio_sample_boundaries.context.time_base,
        outcome,
        segments,
    )


def _policy(**changes: int | bool) -> ExactAvSpanPolicy:
    values: dict[str, int | bool] = {
        "candidate_cartesian_limit": 1_000,
        "endpoint_stability_video_tick": 1,
        "subtitle_clearance_floor_video_tick": 1,
        "av_sync_tolerance_audio_tick": 0,
        "maximum_mapping_error_audio_tick": 0,
        "require_audio": True,
    }
    values.update(changes)
    return ExactAvSpanPolicy(**values)  # type: ignore[arg-type]


def test_vfr_membership_four_endpoints_and_recomputable_proofs() -> None:
    bundle = _bundle()
    result = compile_exact_av_span(_request(bundle), bundle, _clock_map(bundle), _policy())

    assert result.video_range == TickRange(37, 64)
    assert result.audio_range == TickRange(37, 64)
    assert result.boundary_proof.video_in_tick in bundle.frame_pts_index.pts_index.ticks
    assert result.boundary_proof.audio_out_tick in {
        point.tick for point in bundle.audio_sample_boundaries.points
    }
    assert result.total_cartesian_count == 36
    assert result.feasible_count > 0
    assert result.boundary_proof.frame_pts_index_set_sha256 == bundle.frame_pts_index.canonical_hash


def test_asr_success_does_not_short_circuit_vad_protection() -> None:
    bundle = _bundle(transcript_protected=(1, 2), vad_protected=(30, 70))
    result = compile_exact_av_span(_request(bundle), bundle, _clock_map(bundle), _policy())

    assert result.audio_range == TickRange(11, 100)
    assert result.dialogue_integrity_proof.checked_word_count == 1
    assert result.dialogue_integrity_proof.checked_vad_range_count == 1


def test_subtitle_error_and_policy_floor_are_both_protected() -> None:
    bundle = _bundle(subtitle=(60, 61, 1, 1))
    result = compile_exact_av_span(
        _request(bundle),
        bundle,
        _clock_map(bundle),
        _policy(subtitle_clearance_floor_video_tick=3),
    )

    assert result.video_range == TickRange(37, 100)


def test_zero_subtitle_clearance_floor_is_not_a_production_policy() -> None:
    with pytest.raises(ExactSpanValidationError, match="subtitle_clearance.*positive"):
        _policy(subtitle_clearance_floor_video_tick=0)


@pytest.mark.parametrize("classification", [VisualClassification.UNKNOWN, VisualClassification.BLACK])
def test_forbidden_or_unknown_visual_endpoint_region_fails_closed(
    classification: VisualClassification,
) -> None:
    bundle = _bundle(visual=((0, 100, classification),))
    with pytest.raises(NoLegalSpanError):
        compile_exact_av_span(_request(bundle), bundle, _clock_map(bundle), _policy())


def test_piecewise_map_is_exact_and_uncertainty_over_budget_is_rejected() -> None:
    bundle = _bundle()
    segments = (
        VideoToAudioMapSegment(TickRange(0, 37), TickRange(0, 37), 0),
        VideoToAudioMapSegment(TickRange(37, 100), TickRange(37, 100), 0),
    )
    assert compile_exact_av_span(
        _request(bundle), bundle, _clock_map(bundle, segments=segments), _policy()
    ).audio_range == TickRange(37, 64)

    with pytest.raises(ExactSpanValidationError, match="uncertainty"):
        compile_exact_av_span(
            _request(bundle), bundle, _clock_map(bundle, error=2), _policy()
        )


def test_map_gaps_and_indeterminate_map_are_rejected() -> None:
    bundle = _bundle()
    with pytest.raises(ExactSpanValidationError, match="contiguous"):
        _clock_map(
            bundle,
            segments=(
                VideoToAudioMapSegment(TickRange(0, 37), TickRange(0, 37), 0),
                VideoToAudioMapSegment(TickRange(38, 100), TickRange(37, 100), 0),
            ),
        )
    with pytest.raises(ExactSpanValidationError, match="indeterminate"):
        compile_exact_av_span(
            _request(bundle),
            bundle,
            _clock_map(bundle, segments=(), outcome=ClockMapOutcome.INDETERMINATE),
            _policy(),
        )


def test_no_audio_and_missing_frame_sentinel_fail_closed() -> None:
    no_audio = _bundle(audio_present=False)
    with pytest.raises(ExactSpanValidationError, match="audio sample"):
        compile_exact_av_span(_request(no_audio), no_audio, _clock_map(no_audio), _policy())

    no_sentinel = _bundle(frame_ticks=(11, 37, 64, 100))
    with pytest.raises(ExactSpanValidationError, match="sentinels"):
        compile_exact_av_span(
            _request(no_sentinel), no_sentinel, _clock_map(no_sentinel), _policy()
        )


def test_candidate_limit_is_checked_for_full_cartesian_domain() -> None:
    bundle = _bundle()
    with pytest.raises(CandidatePairLimitError, match="36"):
        compile_exact_av_span(
            _request(bundle),
            bundle,
            _clock_map(bundle),
            _policy(candidate_cartesian_limit=35),
        )


def test_canonical_selection_and_identity_are_deterministic() -> None:
    bundle = _bundle()
    first = compile_exact_av_span(_request(bundle), bundle, _clock_map(bundle), _policy())
    second = compile_exact_av_span(_request(bundle), bundle, _clock_map(bundle), _policy())

    assert first.canonical_decision_key == second.canonical_decision_key
    assert first.canonical_hash == second.canonical_hash
    assert first.feasible_relation_sha256 == second.feasible_relation_sha256


def test_vlm_contract_cannot_be_used_as_endpoint_or_root_input() -> None:
    bundle = _bundle()
    forged_vlm_interval = object.__new__(MappedSourceInterval)
    with pytest.raises(ExactSpanValidationError, match="VideoClockRange"):
        ExactAvSpanRequest(forged_vlm_interval, _request(bundle).anchor_video_range, 20)  # type: ignore[arg-type]
    with pytest.raises(ExactSpanValidationError, match="RootMediaEvidenceBundle"):
        compile_exact_av_span(
            _request(bundle),
            forged_vlm_interval,  # type: ignore[arg-type]
            _clock_map(bundle),
            _policy(),
        )
