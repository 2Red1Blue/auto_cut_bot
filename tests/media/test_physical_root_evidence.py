"""Contract tests for the isolated physical root evidence value and codec."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest
from autocut_kernel.media.physical_root import PhysicalRootMediaEvidence
from autocut_kernel.media.physical_root_codec import (
    decode_physical_root_media_evidence,
    decode_physical_root_media_evidence_json,
)
from autocut_kernel.media.root_evidence import (
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
from autocut_kernel.media.root_evidence_codec import decode_root_media_evidence_bundle
from autocut_kernel.media.types import MediaValidationError, PTSIndex, canonical_sha256

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
SOURCE_ID = "source-001"
SOURCE_HASH = "sha256:" + "1" * 64
AUDIO_BASE = TimeBase(1, 48_000)
VIDEO_BASE = TimeBase(1, 90_000)


def _context(kind: MediaKind, producer_id: str, *, base: TimeBase | None = None) -> EvidenceContext:
    return EvidenceContext(
        source_id=SOURCE_ID,
        source_sha256=SOURCE_HASH,
        media_kind=kind,
        clock_id=f"{SOURCE_ID}:{'audio_sample' if kind is MediaKind.AUDIO else 'video_pts'}",
        time_base=base or (AUDIO_BASE if kind is MediaKind.AUDIO else VIDEO_BASE),
        origin_tick=0,
        duration_tick=100,
        producer_id=producer_id,
        generation_policy_sha256=HASH_A,
    )


def _coverage(context: EvidenceContext, outcome: CoverageOutcome = CoverageOutcome.COMPLETE) -> Coverage:
    diagnostics: tuple[CoverageDiagnostic, ...] = ()
    if outcome is not CoverageOutcome.COMPLETE:
        start, end = context.origin_tick, context.end_tick
        diagnostics = (
            CoverageDiagnostic(start, end, "PRODUCER_GAP", "uncovered span", HASH_B),
        )
    return Coverage(
        source_id=context.source_id,
        source_sha256=context.source_sha256,
        clock_id=context.clock_id,
        time_base=context.time_base,
        in_tick=context.origin_tick,
        out_tick=context.end_tick,
        outcome=outcome,
        diagnostics=diagnostics,
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


def _audio_boundaries(
    *, origin: int = 0, duration: int = 100, base: TimeBase = AUDIO_BASE,
) -> AudioSampleBoundarySet:
    context = EvidenceContext(
        source_id=SOURCE_ID,
        source_sha256=SOURCE_HASH,
        media_kind=MediaKind.AUDIO,
        clock_id=f"{SOURCE_ID}:audio_sample",
        time_base=base,
        origin_tick=origin,
        duration_tick=duration,
        producer_id="audio-decoder-v1",
        generation_policy_sha256=HASH_A,
    )
    mid = origin + duration // 2
    end = origin + duration
    return AudioSampleBoundarySet(
        audio_sample_boundary_set_id="audio-boundaries-001",
        context=context,
        coverage=_coverage(context),
        source_outcome=AudioSourceOutcome.BOUNDARIES_AVAILABLE,
        points=(
            AudioSampleBoundary(
                "audio-000", SOURCE_ID, SOURCE_HASH, context.clock_id, context.time_base,
                origin, AudioBoundaryMethod.DECODER,
            ),
            AudioSampleBoundary(
                "audio-mid", SOURCE_ID, SOURCE_HASH, context.clock_id, context.time_base,
                mid, AudioBoundaryMethod.DECODER,
            ),
            AudioSampleBoundary(
                "audio-end", SOURCE_ID, SOURCE_HASH, context.clock_id, context.time_base,
                end, AudioBoundaryMethod.DECODER,
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


def _physical_root() -> PhysicalRootMediaEvidence:
    frame_pts = _frame_pts(_context(MediaKind.VIDEO, "frame-decoder-v1"))
    shots, scenes = _video_boundary_sets(frame_pts)
    return PhysicalRootMediaEvidence(
        physical_root_id="physical-root-001",
        source_id=SOURCE_ID,
        source_sha256=SOURCE_HASH,
        source_manifest_sha256=HASH_B,
        root_input_manifest_sha256=HASH_C,
        frame_pts_index=frame_pts,
        shot_boundaries=shots,
        scene_boundaries=scenes,
        audio_sample_boundaries=_audio_boundaries(),
        visual_validity=_visual(_context(MediaKind.VIDEO, "visual-detector-v1")),
        subtitle_cues=_subtitles(_context(MediaKind.VIDEO, "subtitle-detector-v1")),
    )


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


def _bundle() -> RootMediaEvidenceBundle:
    frame_pts = _frame_pts(_context(MediaKind.VIDEO, "frame-decoder-v1"))
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
        audio_sample_boundaries=_audio_boundaries(),
        transcript=_transcript(),
        speech_activity=_speech(),
        visual_validity=_visual(_context(MediaKind.VIDEO, "visual-detector-v1")),
        subtitle_cues=_subtitles(_context(MediaKind.VIDEO, "subtitle-detector-v1")),
    )


def _raw(mapping: object) -> bytes:
    return json.dumps(mapping, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _paths(value: object, kind: type, path: tuple[object, ...] = ()):
    if type(value) is kind:  # noqa: E721
        yield path
    if type(value) is dict:  # noqa: E721
        for key, child in value.items():
            yield from _paths(child, kind, (*path, key))
    elif type(value) is list:  # noqa: E721
        for index, child in enumerate(value):
            yield from _paths(child, kind, (*path, index))


def _get(value: object, path: tuple[object, ...]) -> object:
    current: object = value
    for key in path:
        current = current[key]  # type: ignore[index]
    return current


def _change(mapping: dict[str, object], path: tuple[object, ...], value: object) -> None:
    target: object = mapping
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]


_WIRE = _physical_root().to_mapping()


def test_physical_root_roundtrip_preserves_canonical_bytes_and_hash():
    original = _physical_root()
    mapping = original.to_mapping()
    raw = _raw(mapping)
    decoded = decode_physical_root_media_evidence(mapping)
    assert decoded == original
    assert decoded.canonical_hash == original.canonical_hash
    assert _raw(decoded.to_mapping()) == raw
    assert decode_physical_root_media_evidence_json(raw, max_bytes=len(raw)) == original


@pytest.mark.parametrize("identifier", ("", "  ", None, 123, "root-\ud800"))
def test_direct_root_identifier_rejects_values_that_cannot_roundtrip(identifier):
    with pytest.raises(MediaValidationError):
        replace(_physical_root(), physical_root_id=identifier)


def test_unicode_root_identifier_roundtrips_without_normalization():
    original = replace(_physical_root(), physical_root_id="片段-物理证据-😀")
    raw = _raw(original.to_mapping())
    assert decode_physical_root_media_evidence_json(raw, max_bytes=len(raw)) == original


def test_mapping_and_json_reject_surrogate_root_identifier():
    mapping = deepcopy(_WIRE)
    mapping["physical_root_id"] = "root-\ud800"
    with pytest.raises(MediaValidationError):
        decode_physical_root_media_evidence(mapping)
    raw = _raw(mapping)
    with pytest.raises(MediaValidationError):
        decode_physical_root_media_evidence_json(raw, max_bytes=len(raw))


def test_physical_root_carries_no_speech_members_or_schema_version():
    mapping = _physical_root().to_mapping()
    assert set(mapping) == {
        "physical_root_id",
        "source_id",
        "source_sha256",
        "source_manifest_sha256",
        "root_input_manifest_sha256",
        "frame_pts_index",
        "shot_boundaries",
        "scene_boundaries",
        "audio_sample_boundaries",
        "visual_validity",
        "subtitle_cues",
    }
    assert "transcript" not in mapping and "speech_activity" not in mapping
    assert "no_speech" not in mapping and "schema_version" not in mapping


def test_aggregate_hash_is_new_and_six_set_hashes_are_preserved():
    physical = _physical_root()
    bundle = _bundle()
    assert physical.canonical_hash != bundle.canonical_hash
    assert physical.frame_pts_index.canonical_hash == _frame_pts().canonical_hash
    assert physical.shot_boundaries.canonical_hash == _video_boundary_sets(_frame_pts())[0].canonical_hash
    assert physical.scene_boundaries.canonical_hash == _video_boundary_sets(_frame_pts())[1].canonical_hash
    assert physical.audio_sample_boundaries.canonical_hash == _audio_boundaries().canonical_hash
    assert physical.visual_validity.canonical_hash == _visual().canonical_hash
    assert physical.subtitle_cues.canonical_hash == _subtitles().canonical_hash


def test_independent_native_audio_clock_and_unequal_tails_are_valid():
    physical = replace(
        _physical_root(),
        audio_sample_boundaries=_audio_boundaries(
            origin=5, duration=85, base=TimeBase(1, 44_100)
        ),
    )
    assert physical.audio_sample_boundaries.context.clock_id != (
        physical.frame_pts_index.context.clock_id
    )
    assert physical.audio_sample_boundaries.context.end_tick == 90
    assert physical.frame_pts_index.context.end_tick == 100
    assert physical.audio_sample_boundaries.context.time_base == TimeBase(1, 44_100)


@pytest.mark.parametrize("mutation", ("extra", "missing"))
def test_physical_root_object_is_closed(mutation):
    mapping = deepcopy(_WIRE)
    if mutation == "extra":
        mapping["transcript"] = {"claimed": "injected speech member"}
    else:
        del mapping["subtitle_cues"]
    with pytest.raises(MediaValidationError):
        decode_physical_root_media_evidence(mapping)


@pytest.mark.parametrize(
    ("field", "mutation"),
    [
        ("source_id", "foreign"),
        ("source_sha256", "foreign"),
        ("source_manifest_sha256", "foreign"),
        ("root_input_manifest_sha256", "foreign"),
    ],
)
def test_text_identity_fields_reject_non_text_or_empty(field, mutation):
    mapping = deepcopy(_WIRE)
    if mutation == "foreign":
        mapping[field] = "" if field == "source_id" else "not-a-hash"
    with pytest.raises(MediaValidationError):
        decode_physical_root_media_evidence(mapping)


def test_source_identity_drift_across_sets_rejects():
    physical = _physical_root()
    context = physical.frame_pts_index.context
    drifted_context = replace(context, source_id="other-source", source_sha256=HASH_B)
    drifted_coverage = replace(
        physical.frame_pts_index.coverage, source_id="other-source", source_sha256=HASH_B
    )
    bad_frame = replace(
        physical.frame_pts_index, context=drifted_context, coverage=drifted_coverage
    )
    with pytest.raises(MediaValidationError):
        replace(physical, frame_pts_index=bad_frame)


def test_non_complete_coverage_rejects():
    physical = _physical_root()
    context = physical.visual_validity.context
    bad_visual = replace(
        physical.visual_validity,
        coverage=_coverage(context, CoverageOutcome.PARTIAL),
    )
    with pytest.raises(MediaValidationError):
        replace(physical, visual_validity=bad_visual)


def test_video_clock_drift_rejects():
    physical = _physical_root()
    context = physical.frame_pts_index.context
    drifted_context = replace(context, clock_id="other-video-clock")
    drifted_coverage = replace(
        physical.frame_pts_index.coverage, clock_id="other-video-clock"
    )
    bad_frame = replace(
        physical.frame_pts_index, context=drifted_context, coverage=drifted_coverage
    )
    with pytest.raises(MediaValidationError):
        replace(physical, frame_pts_index=bad_frame)


def test_shot_or_scene_frame_hash_drift_rejects():
    physical = _physical_root()
    bad_shots = replace(physical.shot_boundaries, frame_pts_index_set_sha256=HASH_B)
    with pytest.raises(MediaValidationError):
        replace(physical, shot_boundaries=bad_shots)


def test_boundary_not_in_frame_index_rejects():
    physical = _physical_root()
    bad_shots = replace(
        physical.shot_boundaries,
        points=(
            VideoBoundaryPoint(
                "shot-026",
                SOURCE_ID,
                SOURCE_HASH,
                physical.shot_boundaries.context.clock_id,
                physical.shot_boundaries.context.time_base,
                26,
                VideoBoundaryType.SHOT,
                VideoBoundaryMethod.DETECTOR,
                960_000,
            ),
        ),
    )
    with pytest.raises(MediaValidationError):
        replace(physical, shot_boundaries=bad_shots)


def test_exact_set_types_are_required():
    physical = _physical_root()
    with pytest.raises(MediaValidationError):
        replace(physical, frame_pts_index="not-a-frame-pts-set")


def test_value_is_immutable_and_mapping_is_independent():
    physical = _physical_root()
    with pytest.raises(FrozenInstanceError):
        physical.source_id = "mutated"  # type: ignore[misc]
    mapping = physical.to_mapping()
    before = deepcopy(mapping)
    mapping["source_id"] = "caller mutation"
    assert physical.source_id == SOURCE_ID
    assert _physical_root().to_mapping() == before


@pytest.mark.parametrize("path", tuple(_paths(_WIRE, dict)))
@pytest.mark.parametrize("mutation", ("extra", "missing", "null", "array"))
def test_every_object_boundary_is_closed(path, mutation):
    mapping = deepcopy(_WIRE)
    target = _get(mapping, path)
    if mutation == "extra":
        cast(dict[str, object], target)["claimed_pass"] = True
    elif mutation == "missing":
        del cast(dict[str, object], target)[next(iter(cast(dict[str, object], target)))]
    elif not path:
        mapping = None if mutation == "null" else []
    else:
        _change(mapping, path, None if mutation == "null" else [])
    with pytest.raises(MediaValidationError):
        decode_physical_root_media_evidence(mapping)


@pytest.mark.parametrize("path", tuple(_paths(_WIRE, int)))
@pytest.mark.parametrize("mutation", ("float", "bool", "str", "null"))
def test_every_integer_rejects_coercion_even_when_python_equality_would_pass(path, mutation):
    mapping = deepcopy(_WIRE)
    original = _get(mapping, path)
    value = {
        "float": float(cast(int, original)),
        "bool": bool(cast(int, original)),
        "str": str(cast(int, original)),
        "null": None,
    }[mutation]
    _change(mapping, path, value)
    with pytest.raises(MediaValidationError):
        decode_physical_root_media_evidence(mapping)


def test_json_duplicate_keys_reject():
    good = _raw(_WIRE)
    assert decode_physical_root_media_evidence_json(good, max_bytes=len(good)) == _physical_root()
    member = b'"physical_root_id":"physical-root-001"'
    assert good.count(member) == 1
    raw = good.replace(member, member + b"," + member, 1)
    # Last-key-wins JSON would recover the otherwise valid payload unchanged.
    assert json.loads(raw) == _WIRE
    with pytest.raises(MediaValidationError):
        decode_physical_root_media_evidence_json(raw, max_bytes=len(raw))


def test_json_floats_and_nonfinite_constants_reject():
    good = _raw(_WIRE)
    # A float anywhere in the JSON is rejected before field decoding.
    with_float = json.dumps(
        {**_WIRE, "source_id": 1.0},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    with pytest.raises(MediaValidationError):
        decode_physical_root_media_evidence_json(with_float, max_bytes=len(with_float))
    # Nonfinite constants are rejected by the parser.
    with_nan = good.replace(b'"source-001"', b"NaN", 1)
    with pytest.raises(MediaValidationError):
        decode_physical_root_media_evidence_json(with_nan, max_bytes=len(with_nan))


@pytest.mark.parametrize("max_bytes", [0, -1, 1])
def test_json_byte_bound_is_explicit_and_positive(max_bytes):
    raw = _raw(_WIRE)
    with pytest.raises(MediaValidationError):
        decode_physical_root_media_evidence_json(raw, max_bytes=max_bytes)


def test_empty_bytes_reject():
    with pytest.raises(MediaValidationError):
        decode_physical_root_media_evidence_json(b"", max_bytes=1)


def test_existing_v1_bundle_roundtrip_is_unchanged():
    original = _bundle()
    assert decode_root_media_evidence_bundle(original.to_mapping()) == original
    # The v1 decoder still requires the two speech members the physical root omits.
    reduced = {key: value for key, value in original.to_mapping().items()
               if key not in {"transcript", "speech_activity"}}
    with pytest.raises(MediaValidationError):
        decode_root_media_evidence_bundle(reduced)
