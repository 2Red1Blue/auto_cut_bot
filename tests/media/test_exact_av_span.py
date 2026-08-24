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
    NonOverlapPosition,
    PresentationNonOverlap,
    PresentationTimeRange,
    VideoClockRange,
    VideoToAudioClockMapCertificate,
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
VIDEO_END = 180
AUDIO_END = 96


def _context(
    kind: MediaKind,
    producer: str,
    *,
    video_origin: int = 0,
    video_end: int = VIDEO_END,
    audio_origin: int = 0,
    audio_end: int = AUDIO_END,
) -> EvidenceContext:
    origin = video_origin if kind is MediaKind.VIDEO else audio_origin
    end = video_end if kind is MediaKind.VIDEO else audio_end
    return EvidenceContext(
        SOURCE_ID,
        SOURCE_HASH,
        kind,
        VIDEO_CLOCK if kind is MediaKind.VIDEO else AUDIO_CLOCK,
        VIDEO_BASE if kind is MediaKind.VIDEO else AUDIO_BASE,
        origin,
        end - origin,
        producer,
        HASH_A,
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


def _frame_set(
    ticks: tuple[int, ...], *, video_origin: int = 0, video_end: int = VIDEO_END
) -> FramePtsIndexSet:
    context = _context(
        MediaKind.VIDEO,
        "frame-v1",
        video_origin=video_origin,
        video_end=video_end,
    )
    index = PTSIndex(ticks)
    return FramePtsIndexSet(
        "frames-vfr",
        context,
        _coverage(context),
        index,
        canonical_sha256(list(ticks)),
    )


def _audio_set(
    ticks: tuple[int, ...],
    *,
    present: bool = True,
    audio_origin: int = 0,
    audio_end: int = AUDIO_END,
) -> AudioSampleBoundarySet:
    context = _context(
        MediaKind.AUDIO,
        "audio-v1",
        audio_origin=audio_origin,
        audio_end=audio_end,
    )
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


def _transcript(
    *,
    protected: tuple[int, int] | None,
    audio_present: bool,
    audio_origin: int = 0,
    audio_end: int = AUDIO_END,
) -> TranscriptSet:
    context = _context(
        MediaKind.AUDIO,
        "asr-v1",
        audio_origin=audio_origin,
        audio_end=audio_end,
    )
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


def _vad(
    *,
    protected: tuple[int, int] | None,
    audio_present: bool,
    audio_origin: int = 0,
    audio_end: int = AUDIO_END,
) -> SpeechActivitySet:
    context = _context(
        MediaKind.AUDIO,
        "vad-v1",
        audio_origin=audio_origin,
        audio_end=audio_end,
    )
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
    *,
    video_origin: int = 0,
    video_end: int = VIDEO_END,
) -> VisualValiditySet:
    context = _context(
        MediaKind.VIDEO,
        "visual-v1",
        video_origin=video_origin,
        video_end=video_end,
    )
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


def _subtitles(
    cue: tuple[int, int, int, int] | None,
    *,
    video_origin: int = 0,
    video_end: int = VIDEO_END,
) -> SubtitleCueSet:
    context = _context(
        MediaKind.VIDEO,
        "subtitle-v1",
        video_origin=video_origin,
        video_end=video_end,
    )
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
    frame_ticks: tuple[int, ...] = (0, 15, 60, 120, 180),
    audio_ticks: tuple[int, ...] = (0, 8, 32, 64, 96),
    transcript_protected: tuple[int, int] | None = None,
    vad_protected: tuple[int, int] | None = None,
    visual: tuple[tuple[int, int, VisualClassification], ...] = (
        (0, VIDEO_END, VisualClassification.VALID_CONTENT),
    ),
    subtitle: tuple[int, int, int, int] | None = None,
    audio_present: bool = True,
    video_origin: int = 0,
    video_end: int = VIDEO_END,
    audio_origin: int = 0,
    audio_end: int = AUDIO_END,
) -> RootMediaEvidenceBundle:
    frame_set = _frame_set(
        frame_ticks,
        video_origin=video_origin,
        video_end=video_end,
    )
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
        _audio_set(
            audio_ticks,
            present=audio_present,
            audio_origin=audio_origin,
            audio_end=audio_end,
        ),
        _transcript(
            protected=transcript_protected,
            audio_present=audio_present,
            audio_origin=audio_origin,
            audio_end=audio_end,
        ),
        _vad(
            protected=vad_protected,
            audio_present=audio_present,
            audio_origin=audio_origin,
            audio_end=audio_end,
        ),
        _visual(
            visual,
            video_origin=video_origin,
            video_end=video_end,
        ),
        _subtitles(
            subtitle,
            video_origin=video_origin,
            video_end=video_end,
        ),
    )


def _request(bundle: RootMediaEvidenceBundle) -> ExactAvSpanRequest:
    context = bundle.frame_pts_index.context
    desired = VideoClockRange(
        SOURCE_ID,
        SOURCE_HASH,
        context.clock_id,
        context.time_base,
        TickRange(context.origin_tick, context.end_tick),
    )
    anchor = VideoClockRange(
        SOURCE_ID,
        SOURCE_HASH,
        context.clock_id,
        context.time_base,
        TickRange(context.origin_tick + 60, context.origin_tick + 120),
    )
    return ExactAvSpanRequest(desired, anchor, 40)


def _clock_map(
    bundle: RootMediaEvidenceBundle,
    *,
    outcome: ClockMapOutcome = ClockMapOutcome.COMPLETE,
) -> VideoToAudioClockMapCertificate:
    if outcome is ClockMapOutcome.INDETERMINATE:
        return VideoToAudioClockMapCertificate(
            SOURCE_ID,
            SOURCE_HASH,
            bundle.frame_pts_index.context.clock_id,
            bundle.frame_pts_index.context.time_base,
            bundle.audio_sample_boundaries.context.clock_id,
            bundle.audio_sample_boundaries.context.time_base,
            outcome,
            None,
            (),
        )
    return VideoToAudioClockMapCertificate.from_root_evidence(bundle)


def _policy(**changes: int | bool) -> ExactAvSpanPolicy:
    values: dict[str, int | bool] = {
        "candidate_cartesian_limit": 1_000,
        "endpoint_stability_video_tick": 1,
        "subtitle_clearance_floor_video_tick": 1,
        "av_sync_tolerance_audio_tick": 0,
        "require_audio": True,
    }
    values.update(changes)
    return ExactAvSpanPolicy(**values)  # type: ignore[arg-type]


def test_vfr_membership_four_endpoints_and_recomputable_proofs() -> None:
    bundle = _bundle()
    result = compile_exact_av_span(_request(bundle), bundle, _clock_map(bundle), _policy())

    assert result.video_range == TickRange(60, 120)
    assert result.audio_range == TickRange(32, 64)
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

    assert result.audio_range == TickRange(8, 96)
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

    assert result.video_range == TickRange(15, 120)


def test_zero_subtitle_clearance_floor_is_not_a_production_policy() -> None:
    with pytest.raises(ExactSpanValidationError, match="subtitle_clearance.*positive"):
        _policy(subtitle_clearance_floor_video_tick=0)


@pytest.mark.parametrize("classification", [VisualClassification.UNKNOWN, VisualClassification.BLACK])
def test_forbidden_or_unknown_visual_endpoint_region_fails_closed(
    classification: VisualClassification,
) -> None:
    bundle = _bundle(visual=((0, VIDEO_END, classification),))
    with pytest.raises(NoLegalSpanError):
        compile_exact_av_span(_request(bundle), bundle, _clock_map(bundle), _policy())


def test_equal_presentation_map_is_exact() -> None:
    bundle = _bundle()
    certificate = _clock_map(bundle)
    assert certificate.map_video_tick_bounds(60) == (32, 32)
    assert certificate.map_video_tick_bounds(120) == (64, 64)
    assert compile_exact_av_span(
        _request(bundle), bundle, certificate, _policy()
    ).audio_range == TickRange(32, 64)


def test_unequal_audio_tail_is_exposed_without_duration_ratio_drift() -> None:
    bundle = _bundle(
        audio_ticks=(0, 8, 32, 64, 96, 100),
        audio_end=100,
    )
    certificate = _clock_map(bundle)

    assert certificate.map_video_tick_bounds(120) == (64, 64)
    assert certificate.map_video_tick_bounds(VIDEO_END) == (96, 96)
    assert certificate.non_overlaps == (
        PresentationNonOverlap(
            MediaKind.AUDIO,
            NonOverlapPosition.TRAILING,
            PresentationTimeRange(1, 500, 1, 480),
        ),
    )
    assert compile_exact_av_span(
        _request(bundle), bundle, certificate, _policy()
    ).audio_range == TickRange(32, 64)


def test_leading_video_non_overlap_is_explicit_and_outside_endpoint_fails() -> None:
    bundle = _bundle(
        frame_ticks=(-15, 0, 45, 105, 180),
        visual=((-15, VIDEO_END, VisualClassification.VALID_CONTENT),),
        video_origin=-15,
    )
    certificate = _clock_map(bundle)

    assert certificate.non_overlaps == (
        PresentationNonOverlap(
            MediaKind.VIDEO,
            NonOverlapPosition.LEADING,
            PresentationTimeRange(-1, 6000, 0, 1),
        ),
    )
    with pytest.raises(ExactSpanValidationError, match="common presentation interval"):
        compile_exact_av_span(_request(bundle), bundle, certificate, _policy())


def test_nonzero_origins_and_fractional_rounding_use_absolute_presentation_time() -> None:
    bundle = _bundle(
        frame_ticks=(90, 105, 150, 210, 270),
        audio_ticks=(48, 56, 80, 112, 144),
        visual=((90, 270, VisualClassification.VALID_CONTENT),),
        video_origin=90,
        video_end=270,
        audio_origin=48,
        audio_end=144,
    )
    certificate = _clock_map(bundle)

    assert certificate.non_overlaps == ()
    assert certificate.map_video_tick_bounds(150) == (80, 80)
    assert compile_exact_av_span(
        _request(bundle), bundle, certificate, _policy()
    ).audio_range == TickRange(80, 112)

    fractional = VideoToAudioClockMapCertificate(
        SOURCE_ID,
        SOURCE_HASH,
        VIDEO_CLOCK,
        VIDEO_BASE,
        AUDIO_CLOCK,
        AUDIO_BASE,
        ClockMapOutcome.COMPLETE,
        PresentationTimeRange(-1, 1, 1, 1),
        (),
    )
    assert fractional.map_video_tick_bounds(1) == (0, 1)
    assert fractional.map_video_tick_bounds(-15) == (-8, -8)


def test_tampered_partition_and_indeterminate_map_are_rejected() -> None:
    bundle = _bundle()
    valid = _clock_map(bundle)
    forged = replace(
        valid,
        non_overlaps=(
            PresentationNonOverlap(
                MediaKind.AUDIO,
                NonOverlapPosition.TRAILING,
                PresentationTimeRange(1, 600, 1, 500),
            ),
        ),
    )
    with pytest.raises(ExactSpanValidationError, match="exact common interval"):
        compile_exact_av_span(
            _request(bundle), bundle, forged, _policy()
        )
    with pytest.raises(ExactSpanValidationError, match="indeterminate"):
        compile_exact_av_span(
            _request(bundle),
            bundle,
            _clock_map(bundle, outcome=ClockMapOutcome.INDETERMINATE),
            _policy(),
        )


def test_no_audio_and_missing_frame_sentinel_fail_closed() -> None:
    no_audio = _bundle(audio_present=False)
    with pytest.raises(ExactSpanValidationError, match="audio sample"):
        compile_exact_av_span(_request(no_audio), no_audio, _clock_map(no_audio), _policy())

    no_sentinel = _bundle(frame_ticks=(15, 60, 120, 180))
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
