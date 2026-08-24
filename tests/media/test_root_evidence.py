"""Contract tests for immutable, fail-closed root media evidence."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest
from autocut_kernel.media import (
    AudioBoundaryMethod,
    AudioSampleBoundary,
    AudioSampleBoundarySet,
    AudioSourceOutcome,
    Coverage,
    CoverageDiagnostic,
    CoverageOutcome,
    EvidenceCompleteness,
    EvidenceContext,
    FramePtsIndexSet,
    MediaKind,
    MediaValidationError,
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
    VideoBoundaryMethod,
    VideoBoundaryPoint,
    VideoBoundaryType,
    VisualClassification,
    VisualValidityInterval,
    VisualValiditySet,
)
from autocut_kernel.media.types import canonical_sha256

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
SOURCE_ID = "source-001"
SOURCE_HASH = "sha256:" + "1" * 64
AUDIO_BASE = TimeBase(1, 48_000)
VIDEO_BASE = TimeBase(1, 90_000)


def _context(kind: MediaKind, producer_id: str) -> EvidenceContext:
    return EvidenceContext(
        source_id=SOURCE_ID,
        source_sha256=SOURCE_HASH,
        media_kind=kind,
        clock_id=f"{SOURCE_ID}:{'audio_sample' if kind is MediaKind.AUDIO else 'video_pts'}",
        time_base=AUDIO_BASE if kind is MediaKind.AUDIO else VIDEO_BASE,
        origin_tick=0,
        duration_tick=100,
        producer_id=producer_id,
        generation_policy_sha256=HASH_A,
    )


def _coverage(
    context: EvidenceContext,
    outcome: CoverageOutcome = CoverageOutcome.COMPLETE,
) -> Coverage:
    diagnostics: tuple[CoverageDiagnostic, ...] = ()
    if outcome is not CoverageOutcome.COMPLETE:
        diagnostics = (
            CoverageDiagnostic(40, 60, "PRODUCER_GAP", "producer did not cover this span", HASH_B),
        )
    return Coverage(
        source_id=context.source_id,
        source_sha256=context.source_sha256,
        clock_id=context.clock_id,
        time_base=context.time_base,
        in_tick=0,
        out_tick=100,
        outcome=outcome,
        diagnostics=diagnostics,
    )


def _audio_boundaries(context: EvidenceContext | None = None) -> AudioSampleBoundarySet:
    context = context or _context(MediaKind.AUDIO, "audio-decoder-v1")
    return AudioSampleBoundarySet(
        audio_sample_boundary_set_id="audio-boundaries-001",
        context=context,
        coverage=_coverage(context),
        source_outcome=AudioSourceOutcome.BOUNDARIES_AVAILABLE,
        points=(
            AudioSampleBoundary(
                "audio-000",
                SOURCE_ID,
                SOURCE_HASH,
                context.clock_id,
                context.time_base,
                0,
                AudioBoundaryMethod.DECODER,
            ),
            AudioSampleBoundary(
                "audio-050",
                SOURCE_ID,
                SOURCE_HASH,
                context.clock_id,
                context.time_base,
                50,
                AudioBoundaryMethod.DECODER,
            ),
            AudioSampleBoundary(
                "audio-100",
                SOURCE_ID,
                SOURCE_HASH,
                context.clock_id,
                context.time_base,
                100,
                AudioBoundaryMethod.DECODER,
            ),
        ),
    )


def _frame_pts(context: EvidenceContext | None = None) -> FramePtsIndexSet:
    context = context or _context(MediaKind.VIDEO, "frame-decoder-v1")
    pts_index = PTSIndex((0, 25, 50, 75, 100))
    return FramePtsIndexSet(
        frame_pts_index_set_id="frame-pts-001",
        context=context,
        coverage=_coverage(context),
        pts_index=pts_index,
        pts_index_sha256=canonical_sha256(list(pts_index.ticks)),
    )


def _video_boundary_sets(
    frame_pts: FramePtsIndexSet,
) -> tuple[ShotBoundarySet, SceneBoundarySet]:
    shot_context = replace(frame_pts.context, producer_id="shot-detector-v1")
    scene_context = replace(frame_pts.context, producer_id="scene-detector-v1")
    frame_set_hash = frame_pts.canonical_hash
    shots = ShotBoundarySet(
        shot_boundary_set_id="shots-001",
        context=shot_context,
        coverage=_coverage(shot_context),
        frame_pts_index_set_sha256=frame_set_hash,
        points=(
            VideoBoundaryPoint(
                "shot-025",
                SOURCE_ID,
                SOURCE_HASH,
                shot_context.clock_id,
                shot_context.time_base,
                25,
                VideoBoundaryType.SHOT,
                VideoBoundaryMethod.DETECTOR,
                960_000,
            ),
            VideoBoundaryPoint(
                "shot-075",
                SOURCE_ID,
                SOURCE_HASH,
                shot_context.clock_id,
                shot_context.time_base,
                75,
                VideoBoundaryType.SHOT,
                VideoBoundaryMethod.DETECTOR,
                970_000,
            ),
        ),
    )
    scenes = SceneBoundarySet(
        scene_boundary_set_id="scenes-001",
        context=scene_context,
        coverage=_coverage(scene_context),
        frame_pts_index_set_sha256=frame_set_hash,
        points=(
            VideoBoundaryPoint(
                "scene-050",
                SOURCE_ID,
                SOURCE_HASH,
                scene_context.clock_id,
                scene_context.time_base,
                50,
                VideoBoundaryType.SCENE,
                VideoBoundaryMethod.DETECTOR,
                930_000,
            ),
        ),
    )
    return shots, scenes


def _transcript(context: EvidenceContext | None = None) -> TranscriptSet:
    context = context or _context(MediaKind.AUDIO, "asr-v1")
    word = TranscriptWord(
        "word-001", SOURCE_ID, SOURCE_HASH, context.clock_id, context.time_base, 10, 20, "Hello"
    )
    sentence = TranscriptSentence(
        "sentence-001",
        SOURCE_ID,
        SOURCE_HASH,
        context.clock_id,
        context.time_base,
        10,
        30,
        (word.word_id,),
        "Hello world.",
    )
    segment = TranscriptSegment(
        "transcript-001",
        SOURCE_ID,
        SOURCE_HASH,
        context.clock_id,
        context.time_base,
        5,
        35,
        (sentence.sentence_id,),
        "Hello world.",
    )
    return TranscriptSet(
        transcript_set_id="transcripts-001",
        context=context,
        coverage=_coverage(context),
        source_outcome=TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE,
        completeness=TranscriptCompleteness(
            EvidenceCompleteness.COMPLETE,
            EvidenceCompleteness.COMPLETE,
            EvidenceCompleteness.COMPLETE,
        ),
        segments=(segment,),
        words=(word,),
        sentences=(sentence,),
    )


def _speech(context: EvidenceContext | None = None) -> SpeechActivitySet:
    context = context or _context(MediaKind.AUDIO, "vad-v1")
    return SpeechActivitySet(
        speech_activity_set_id="speech-activity-001",
        context=context,
        coverage=_coverage(context),
        source_outcome=SpeechSourceOutcome.SPEECH_DETECTED,
        segments=(
            SpeechActivitySegment(
                "speech-001",
                SOURCE_ID,
                SOURCE_HASH,
                context.clock_id,
                context.time_base,
                8,
                38,
                970_000,
            ),
        ),
    )


def _visual(context: EvidenceContext | None = None) -> VisualValiditySet:
    context = context or _context(MediaKind.VIDEO, "visual-detector-v1")
    return VisualValiditySet(
        visual_validity_set_id="visual-validity-001",
        context=context,
        coverage=_coverage(context),
        intervals=(
            VisualValidityInterval(
                "visual-001",
                SOURCE_ID,
                SOURCE_HASH,
                context.clock_id,
                context.time_base,
                0,
                80,
                VisualClassification.VALID_CONTENT,
                990_000,
            ),
            VisualValidityInterval(
                "visual-unknown-001",
                SOURCE_ID,
                SOURCE_HASH,
                context.clock_id,
                context.time_base,
                80,
                100,
                VisualClassification.UNKNOWN,
                0,
            ),
        ),
    )


def _subtitles(context: EvidenceContext | None = None) -> SubtitleCueSet:
    context = context or _context(MediaKind.VIDEO, "subtitle-detector-v1")
    return SubtitleCueSet(
        subtitle_cue_set_id="subtitle-cues-001",
        context=context,
        coverage=_coverage(context),
        required_modes=(SubtitleDetectionMode.EMBEDDED, SubtitleDetectionMode.BURNED_IN),
        successful_modes=(SubtitleDetectionMode.EMBEDDED, SubtitleDetectionMode.BURNED_IN),
        source_outcome=SubtitleSourceOutcome.CUES_DETECTED,
        cues=(
            SubtitleCue(
                "subtitle-001",
                SOURCE_ID,
                SOURCE_HASH,
                context.clock_id,
                context.time_base,
                25,
                45,
                SubtitleKind.SUBTITLE,
                SubtitleDetectionMode.BURNED_IN,
                950_000,
                TimingErrorBound(context.time_base, 2, 3),
            ),
        ),
    )


def _bundle() -> RootMediaEvidenceBundle:
    audio_context = _context(MediaKind.AUDIO, "audio-decoder-v1")
    video_context = _context(MediaKind.VIDEO, "visual-detector-v1")
    frame_pts = _frame_pts(replace(video_context, producer_id="frame-decoder-v1"))
    shots, scenes = _video_boundary_sets(frame_pts)
    return RootMediaEvidenceBundle(
        root_media_evidence_bundle_id="root-evidence-001",
        source_id=SOURCE_ID,
        source_sha256=SOURCE_HASH,
        source_manifest_sha256=HASH_B,
        root_input_manifest_sha256=HASH_C,
        frame_pts_index=frame_pts,
        shot_boundaries=shots,
        scene_boundaries=scenes,
        audio_sample_boundaries=_audio_boundaries(audio_context),
        transcript=_transcript(replace(audio_context, producer_id="asr-v1")),
        speech_activity=_speech(replace(audio_context, producer_id="vad-v1")),
        visual_validity=_visual(video_context),
        subtitle_cues=_subtitles(replace(video_context, producer_id="subtitle-detector-v1")),
    )


def test_complete_root_bundle_is_immutable_and_has_all_provenance() -> None:
    bundle = _bundle()

    assert bundle.audio_sample_boundaries.points[-1].tick == 100
    assert bundle.transcript.completeness.sentence is EvidenceCompleteness.COMPLETE
    assert bundle.shot_boundaries.frame_pts_index_set_sha256 == bundle.frame_pts_index.canonical_hash
    assert bundle.visual_validity.intervals[-1].classification is VisualClassification.UNKNOWN
    assert "text" not in bundle.subtitle_cues.cues[0].to_mapping()
    assert bundle.to_mapping()["source_manifest_sha256"] == HASH_B
    assert bundle.to_mapping()["root_input_manifest_sha256"] == HASH_C
    with pytest.raises(FrozenInstanceError):
        bundle.source_id = "changed"  # type: ignore[misc]


def test_empty_sets_are_legal_only_when_none_or_not_applicable_is_explicit() -> None:
    audio_context = _context(MediaKind.AUDIO, "no-audio-probe-v1")
    video_context = _context(MediaKind.VIDEO, "visual-detector-v1")
    not_applicable_audio = AudioSampleBoundarySet(
        "audio-na",
        audio_context,
        _coverage(audio_context),
        AudioSourceOutcome.NOT_APPLICABLE,
        (),
    )
    transcript = TranscriptSet(
        "transcript-na",
        replace(audio_context, producer_id="asr-v1"),
        _coverage(replace(audio_context, producer_id="asr-v1")),
        TranscriptSourceOutcome.NOT_APPLICABLE,
        TranscriptCompleteness(*([EvidenceCompleteness.NOT_APPLICABLE] * 3)),
        (),
        (),
        (),
    )
    speech = SpeechActivitySet(
        "speech-na",
        replace(audio_context, producer_id="vad-v1"),
        _coverage(replace(audio_context, producer_id="vad-v1")),
        SpeechSourceOutcome.NOT_APPLICABLE,
        (),
    )
    subtitles = SubtitleCueSet(
        "subtitle-none",
        replace(video_context, producer_id="subtitle-detector-v1"),
        _coverage(replace(video_context, producer_id="subtitle-detector-v1")),
        (SubtitleDetectionMode.EMBEDDED,),
        (SubtitleDetectionMode.EMBEDDED,),
        SubtitleSourceOutcome.NONE_DETECTED,
        (),
    )

    template = _bundle()
    bundle = replace(
        template,
        root_media_evidence_bundle_id="root-na",
        audio_sample_boundaries=not_applicable_audio,
        transcript=transcript,
        speech_activity=speech,
        subtitle_cues=subtitles,
    )

    assert bundle.subtitle_cues.source_outcome is SubtitleSourceOutcome.NONE_DETECTED


def _no_speech_transcript(context: EvidenceContext) -> TranscriptSet:
    return TranscriptSet(
        transcript_set_id="transcript-no-speech",
        context=context,
        coverage=_coverage(context),
        source_outcome=TranscriptSourceOutcome.NO_SPEECH,
        completeness=TranscriptCompleteness(*([EvidenceCompleteness.COMPLETE] * 3)),
        segments=(),
        words=(),
        sentences=(),
    )


def _no_speech_vad(context: EvidenceContext) -> SpeechActivitySet:
    return SpeechActivitySet(
        speech_activity_set_id="vad-none",
        context=context,
        coverage=_coverage(context),
        source_outcome=SpeechSourceOutcome.NONE_DETECTED,
        segments=(),
    )


def test_audio_exists_no_speech_requires_complete_transcript_proof_and_vad_agreement() -> None:
    template = _bundle()
    transcript = _no_speech_transcript(template.transcript.context)
    vad = _no_speech_vad(template.speech_activity.context)

    consistent = replace(template, transcript=transcript, speech_activity=vad)
    assert consistent.transcript.completeness.word is EvidenceCompleteness.COMPLETE

    with pytest.raises(MediaValidationError, match="must agree"):
        replace(template, transcript=transcript)
    with pytest.raises(MediaValidationError, match="must agree"):
        replace(template, speech_activity=vad)
    with pytest.raises(MediaValidationError, match="complete proof"):
        replace(
            transcript,
            completeness=TranscriptCompleteness(
                *([EvidenceCompleteness.NOT_APPLICABLE] * 3)
            ),
        )


def test_audio_presence_and_not_applicable_evidence_must_agree() -> None:
    template = _bundle()
    audio_context = template.audio_sample_boundaries.context
    transcript_na = TranscriptSet(
        "transcript-na-with-audio",
        template.transcript.context,
        _coverage(template.transcript.context),
        TranscriptSourceOutcome.NOT_APPLICABLE,
        TranscriptCompleteness(*([EvidenceCompleteness.NOT_APPLICABLE] * 3)),
        (),
        (),
        (),
    )
    no_audio = AudioSampleBoundarySet(
        "audio-na",
        audio_context,
        _coverage(audio_context),
        AudioSourceOutcome.NOT_APPLICABLE,
        (),
    )

    with pytest.raises(MediaValidationError, match="cannot be not_applicable"):
        replace(template, transcript=transcript_na)
    with pytest.raises(MediaValidationError, match="must be not_applicable"):
        replace(template, audio_sample_boundaries=no_audio)


@pytest.mark.parametrize(
    ("set_name", "bad_tick"),
    [("shot_boundaries", 30), ("scene_boundaries", 60)],
)
def test_shot_and_scene_points_must_be_members_of_the_exact_frame_index(
    set_name: str, bad_tick: int
) -> None:
    template = _bundle()
    boundary_set = getattr(template, set_name)
    first = boundary_set.points[0]
    changed_set = replace(boundary_set, points=(replace(first, tick=bad_tick),))

    with pytest.raises(MediaValidationError, match="member of the exact frame PTS index"):
        replace(template, **{set_name: changed_set})


@pytest.mark.parametrize("set_name", ["shot_boundaries", "scene_boundaries"])
def test_shot_and_scene_sets_bind_frame_set_hash_and_video_clock(set_name: str) -> None:
    template = _bundle()
    boundary_set = getattr(template, set_name)

    with pytest.raises(MediaValidationError, match="exact frame PTS index set hash"):
        replace(
            template,
            **{set_name: replace(boundary_set, frame_pts_index_set_sha256=HASH_B)},
        )

    point = boundary_set.points[0]
    with pytest.raises(MediaValidationError, match="source/clock/time base does not match"):
        replace(boundary_set, points=(replace(point, clock_id="other:video_pts"),))


def test_frame_pts_index_hash_must_recompute_from_all_exact_ticks() -> None:
    frame_pts = _frame_pts()

    with pytest.raises(MediaValidationError, match="must match the exact frame PTS index"):
        replace(frame_pts, pts_index_sha256=HASH_B)


@pytest.mark.parametrize("outcome", [CoverageOutcome.PARTIAL, CoverageOutcome.FAILED])
def test_partial_or_failed_coverage_without_precise_diagnostic_is_rejected(
    outcome: CoverageOutcome,
) -> None:
    context = _context(MediaKind.AUDIO, "asr-v1")

    with pytest.raises(MediaValidationError, match="requires diagnostics"):
        Coverage(
            context.source_id,
            context.source_sha256,
            context.clock_id,
            context.time_base,
            0,
            100,
            outcome,
        )


def test_partial_and_failed_sets_remain_explicit_failures_in_their_mapping() -> None:
    context = _context(MediaKind.AUDIO, "asr-v1")
    partial = TranscriptSet(
        "transcript-partial",
        context,
        _coverage(context, CoverageOutcome.PARTIAL),
        TranscriptSourceOutcome.INDETERMINATE,
        TranscriptCompleteness(
            EvidenceCompleteness.PARTIAL,
            EvidenceCompleteness.FAILED,
            EvidenceCompleteness.FAILED,
        ),
        (),
        (),
        (),
    )

    assert partial.to_mapping()["coverage"]["outcome"] == "partial"  # type: ignore[index]
    with pytest.raises(MediaValidationError, match="requires complete coverage"):
        replace(_bundle(), transcript=partial)


def test_complete_visual_coverage_cannot_hide_an_unknown_or_gap_as_empty_success() -> None:
    context = _context(MediaKind.VIDEO, "visual-detector-v1")

    with pytest.raises(MediaValidationError, match="explicitly partition"):
        VisualValiditySet("visual-empty", context, _coverage(context), ())

    unknown = VisualValiditySet(
        "visual-unknown",
        context,
        _coverage(context),
        (
            VisualValidityInterval(
                "unknown-001",
                SOURCE_ID,
                SOURCE_HASH,
                context.clock_id,
                context.time_base,
                0,
                100,
                VisualClassification.UNKNOWN,
                0,
            ),
        ),
    )
    assert unknown.intervals[0].classification is VisualClassification.UNKNOWN


def test_none_detected_subtitles_requires_every_policy_required_detector() -> None:
    context = _context(MediaKind.VIDEO, "subtitle-detector-v1")

    with pytest.raises(MediaValidationError, match="every required detector"):
        SubtitleCueSet(
            "subtitle-incomplete-detectors",
            context,
            _coverage(context),
            (SubtitleDetectionMode.EMBEDDED, SubtitleDetectionMode.BURNED_IN),
            (SubtitleDetectionMode.EMBEDDED,),
            SubtitleSourceOutcome.NONE_DETECTED,
            (),
        )


@pytest.mark.parametrize("bad_tick", [1.5, True])
def test_float_and_boolean_ticks_are_rejected(bad_tick: object) -> None:
    with pytest.raises(MediaValidationError, match="integer PTS tick"):
        replace(_context(MediaKind.AUDIO, "decoder-v1"), origin_tick=bad_tick)  # type: ignore[arg-type]


def test_out_of_range_and_cross_clock_or_source_records_are_rejected() -> None:
    context = _context(MediaKind.AUDIO, "vad-v1")
    outside = SpeechActivitySegment(
        "outside",
        SOURCE_ID,
        SOURCE_HASH,
        context.clock_id,
        context.time_base,
        90,
        101,
        500_000,
    )
    wrong_source = replace(outside, speech_segment_id="wrong-source", in_tick=10, out_tick=20, source_id="other")
    wrong_clock = replace(outside, speech_segment_id="wrong-clock", in_tick=10, out_tick=20, clock_id="other:audio")

    with pytest.raises(MediaValidationError, match="outside"):
        SpeechActivitySet(
            "speech-outside",
            context,
            _coverage(context),
            SpeechSourceOutcome.SPEECH_DETECTED,
            (outside,),
        )
    for record in (wrong_source, wrong_clock):
        with pytest.raises(MediaValidationError, match="does not match"):
            SpeechActivitySet(
                "speech-cross-context",
                context,
                _coverage(context),
                SpeechSourceOutcome.SPEECH_DETECTED,
                (record,),
            )


def test_sorting_and_duplicate_ids_are_rejected_instead_of_silently_normalized() -> None:
    context = _context(MediaKind.AUDIO, "decoder-v1")
    first, middle, last = _audio_boundaries(context).points

    with pytest.raises(MediaValidationError, match="canonical sorted order"):
        replace(_audio_boundaries(context), points=(middle, first, last))
    with pytest.raises(MediaValidationError, match="duplicate boundary_id"):
        replace(
            _audio_boundaries(context),
            points=(first, replace(middle, boundary_id=first.boundary_id), last),
        )


def test_canonical_mapping_and_hash_are_stable_and_recomputable() -> None:
    first = _bundle()
    second = _bundle()

    assert first.to_mapping() == second.to_mapping()
    assert first.canonical_hash == second.canonical_hash
    assert canonical_sha256(first.to_mapping()) == first.canonical_hash
    assert first.canonical_hash == "sha256:" + first.canonical_hash.removeprefix("sha256:")
    assert replace(second, root_media_evidence_bundle_id="root-evidence-002").canonical_hash != first.canonical_hash
