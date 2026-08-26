"""Closed decoders for the existing source-root evidence producer wire.

These functions restore values, not commitment, safety or Admission. The domain
constructors remain responsible for source/clock, coverage and record relations.
Every emitted field is required, including explicit empty arrays, false flags
and nullable VAD confidence. No defaults, normalization or reflection is used.

Media's canonical JSON permits arbitrary integer ticks and uses ensure_ascii;
the decoder therefore does not impose the unrelated compiler/JCS safe-int cap.
The bytes entry point requires a caller-owned explicit size bound. Store/blob
identity and provenance checks belong to the later committed evidence reader.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from enum import Enum
from typing import TypeVar, cast

from .root_evidence import (
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
from .types import MediaValidationError, PTSIndex, TimeBase, require_pts, sha256_prefixed

_T = TypeVar("_T")
_E = TypeVar("_E", bound=Enum)


def _object(value: object, fields: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise MediaValidationError("evidence value must be a JSON object")
    item = cast(dict[object, object], value)
    if any(type(key) is not str for key in item) or set(item) != set(fields):  # noqa: E721
        raise MediaValidationError("evidence object has missing or unknown fields")
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    if type(value) is not str or not value:  # noqa: E721
        raise MediaValidationError("evidence text must be an exact nonempty string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise MediaValidationError("evidence text must be valid UTF-8") from error
    return value


def _hash(value: object) -> str:
    return sha256_prefixed(_text(value), "evidence hash")


def _integer(value: object) -> int:
    return require_pts(value, "evidence integer")


def _boolean(value: object) -> bool:
    if type(value) is not bool:  # noqa: E721
        raise MediaValidationError("evidence flag must be an exact boolean")
    return value


def _enum(value: object, enum: type[_E]) -> _E:
    try:
        return enum(_text(value))
    except ValueError as error:
        raise MediaValidationError("evidence enum value is unsupported") from error


def _array(value: object, decode: Callable[[object], _T]) -> tuple[_T, ...]:
    if type(value) is not list:  # noqa: E721
        raise MediaValidationError("evidence collection must be a JSON array")
    return tuple(decode(item) for item in cast(list[object], value))


def decode_time_base(value: object) -> TimeBase:
    item = _object(value, ("numerator", "denominator"))
    return TimeBase(_integer(item["numerator"]), _integer(item["denominator"]))


def _source_clock(item: dict[str, object]) -> tuple[str, str, str, TimeBase]:
    return (_text(item["source_id"]), _hash(item["source_sha256"]),
            _text(item["clock_id"]), decode_time_base(item["time_base"]))


def _range(item: dict[str, object]) -> tuple[int, int]:
    return _integer(item["in_tick"]), _integer(item["out_tick"])


def decode_evidence_context(value: object) -> EvidenceContext:
    item = _object(value, ("source_id", "source_sha256", "media_kind", "clock_id", "time_base",
                           "origin_tick", "duration_tick", "producer_id", "generation_policy_sha256"))
    return EvidenceContext(
        _text(item["source_id"]), _hash(item["source_sha256"]),
        _enum(item["media_kind"], MediaKind), _text(item["clock_id"]),
        decode_time_base(item["time_base"]), _integer(item["origin_tick"]),
        _integer(item["duration_tick"]), _text(item["producer_id"]),
        _hash(item["generation_policy_sha256"]),
    )


def _diagnostic(value: object) -> CoverageDiagnostic:
    item = _object(value, ("in_tick", "out_tick", "code", "detail", "producer_evidence_sha256"))
    return CoverageDiagnostic(*_range(item), _text(item["code"]), _text(item["detail"]),
                              _hash(item["producer_evidence_sha256"]))


def decode_coverage(value: object) -> Coverage:
    item = _object(value, ("source_id", "source_sha256", "clock_id", "time_base", "in_tick",
                           "out_tick", "outcome", "diagnostics"))
    return Coverage(*_source_clock(item), *_range(item), _enum(item["outcome"], CoverageOutcome),
                    _array(item["diagnostics"], _diagnostic))


def decode_frame_pts_index_set(value: object) -> FramePtsIndexSet:
    item = _object(value, ("frame_pts_index_set_id", "context", "coverage", "pts_index", "pts_index_sha256"))
    index = _object(item["pts_index"], ("ticks",))
    return FramePtsIndexSet(
        _text(item["frame_pts_index_set_id"]), decode_evidence_context(item["context"]),
        decode_coverage(item["coverage"]), PTSIndex(_array(index["ticks"], _integer)),
        _hash(item["pts_index_sha256"]),
    )


def _video_boundary(value: object) -> VideoBoundaryPoint:
    item = _object(value, ("boundary_id", "source_id", "source_sha256", "clock_id", "time_base",
                           "tick", "boundary_type", "method", "confidence_ppm"))
    return VideoBoundaryPoint(
        _text(item["boundary_id"]), *_source_clock(item), _integer(item["tick"]),
        _enum(item["boundary_type"], VideoBoundaryType), _enum(item["method"], VideoBoundaryMethod),
        _integer(item["confidence_ppm"]),
    )


def decode_shot_boundary_set(value: object) -> ShotBoundarySet:
    item = _object(value, ("shot_boundary_set_id", "context", "coverage", "frame_pts_index_set_sha256", "points"))
    return ShotBoundarySet(
        _text(item["shot_boundary_set_id"]), decode_evidence_context(item["context"]),
        decode_coverage(item["coverage"]), _hash(item["frame_pts_index_set_sha256"]),
        _array(item["points"], _video_boundary),
    )


def decode_scene_boundary_set(value: object) -> SceneBoundarySet:
    item = _object(value, ("scene_boundary_set_id", "context", "coverage", "frame_pts_index_set_sha256", "points"))
    return SceneBoundarySet(
        _text(item["scene_boundary_set_id"]), decode_evidence_context(item["context"]),
        decode_coverage(item["coverage"]), _hash(item["frame_pts_index_set_sha256"]),
        _array(item["points"], _video_boundary),
    )


def _audio_boundary(value: object) -> AudioSampleBoundary:
    item = _object(value, ("boundary_id", "source_id", "source_sha256", "clock_id", "time_base", "tick", "method"))
    return AudioSampleBoundary(_text(item["boundary_id"]), *_source_clock(item),
                               _integer(item["tick"]), _enum(item["method"], AudioBoundaryMethod))


def decode_audio_sample_boundary_set(value: object) -> AudioSampleBoundarySet:
    item = _object(value, ("audio_sample_boundary_set_id", "context", "coverage", "source_outcome", "points"))
    return AudioSampleBoundarySet(
        _text(item["audio_sample_boundary_set_id"]), decode_evidence_context(item["context"]),
        decode_coverage(item["coverage"]), _enum(item["source_outcome"], AudioSourceOutcome),
        _array(item["points"], _audio_boundary),
    )


def _completeness(value: object) -> TranscriptCompleteness:
    item = _object(value, ("segment", "word", "sentence"))
    return TranscriptCompleteness(_enum(item["segment"], EvidenceCompleteness),
                                  _enum(item["word"], EvidenceCompleteness),
                                  _enum(item["sentence"], EvidenceCompleteness))


def _word(value: object) -> TranscriptWord:
    item = _object(value, ("word_id", "source_id", "source_sha256", "clock_id", "time_base", "in_tick", "out_tick", "text"))
    return TranscriptWord(_text(item["word_id"]), *_source_clock(item), *_range(item), _text(item["text"]))


def _sentence(value: object) -> TranscriptSentence:
    item = _object(value, ("sentence_id", "source_id", "source_sha256", "clock_id", "time_base",
                           "in_tick", "out_tick", "word_ids", "text"))
    return TranscriptSentence(_text(item["sentence_id"]), *_source_clock(item), *_range(item),
                              _array(item["word_ids"], _text), _text(item["text"]))


def _transcript_segment(value: object) -> TranscriptSegment:
    item = _object(value, ("transcript_segment_id", "source_id", "source_sha256", "clock_id", "time_base",
                           "in_tick", "out_tick", "sentence_ids", "text"))
    return TranscriptSegment(_text(item["transcript_segment_id"]), *_source_clock(item), *_range(item),
                             _array(item["sentence_ids"], _text), _text(item["text"]))


def decode_transcript_set(value: object) -> TranscriptSet:
    item = _object(value, ("transcript_set_id", "context", "coverage", "source_outcome", "completeness",
                           "segments", "words", "sentences", "boundary_touch_left", "boundary_touch_right", "truncated"))
    return TranscriptSet(
        _text(item["transcript_set_id"]), decode_evidence_context(item["context"]),
        decode_coverage(item["coverage"]), _enum(item["source_outcome"], TranscriptSourceOutcome),
        _completeness(item["completeness"]), _array(item["segments"], _transcript_segment),
        _array(item["words"], _word), _array(item["sentences"], _sentence),
        _boolean(item["boundary_touch_left"]), _boolean(item["boundary_touch_right"]),
        _boolean(item["truncated"]),
    )


def _speech_segment(value: object) -> SpeechActivitySegment:
    item = _object(value, ("speech_segment_id", "source_id", "source_sha256", "clock_id", "time_base",
                           "in_tick", "out_tick", "confidence_ppm"))
    confidence = item["confidence_ppm"]
    return SpeechActivitySegment(_text(item["speech_segment_id"]), *_source_clock(item), *_range(item),
                                 None if confidence is None else _integer(confidence))


def decode_speech_activity_set(value: object) -> SpeechActivitySet:
    item = _object(value, ("speech_activity_set_id", "context", "coverage", "source_outcome", "segments"))
    return SpeechActivitySet(
        _text(item["speech_activity_set_id"]), decode_evidence_context(item["context"]),
        decode_coverage(item["coverage"]), _enum(item["source_outcome"], SpeechSourceOutcome),
        _array(item["segments"], _speech_segment),
    )


def _visual_interval(value: object) -> VisualValidityInterval:
    item = _object(value, ("visual_interval_id", "source_id", "source_sha256", "clock_id", "time_base",
                           "in_tick", "out_tick", "classification", "confidence_ppm"))
    return VisualValidityInterval(_text(item["visual_interval_id"]), *_source_clock(item), *_range(item),
                                  _enum(item["classification"], VisualClassification), _integer(item["confidence_ppm"]))


def decode_visual_validity_set(value: object) -> VisualValiditySet:
    item = _object(value, ("visual_validity_set_id", "context", "coverage", "intervals"))
    return VisualValiditySet(_text(item["visual_validity_set_id"]), decode_evidence_context(item["context"]),
                             decode_coverage(item["coverage"]), _array(item["intervals"], _visual_interval))


def _timing_error(value: object) -> TimingErrorBound:
    item = _object(value, ("time_base", "in_tick", "out_tick"))
    return TimingErrorBound(decode_time_base(item["time_base"]), *_range(item))


def _subtitle(value: object) -> SubtitleCue:
    item = _object(value, ("subtitle_cue_id", "source_id", "source_sha256", "clock_id", "time_base", "in_tick",
                           "out_tick", "kind", "detection_mode", "confidence_ppm", "timing_error_bound"))
    return SubtitleCue(
        _text(item["subtitle_cue_id"]), *_source_clock(item), *_range(item),
        _enum(item["kind"], SubtitleKind), _enum(item["detection_mode"], SubtitleDetectionMode),
        _integer(item["confidence_ppm"]), _timing_error(item["timing_error_bound"]),
    )


def decode_subtitle_cue_set(value: object) -> SubtitleCueSet:
    item = _object(value, ("subtitle_cue_set_id", "context", "coverage", "required_modes", "successful_modes", "source_outcome", "cues"))
    return SubtitleCueSet(
        _text(item["subtitle_cue_set_id"]), decode_evidence_context(item["context"]),
        decode_coverage(item["coverage"]),
        _array(item["required_modes"], lambda mode: _enum(mode, SubtitleDetectionMode)),
        _array(item["successful_modes"], lambda mode: _enum(mode, SubtitleDetectionMode)),
        _enum(item["source_outcome"], SubtitleSourceOutcome), _array(item["cues"], _subtitle),
    )


def decode_root_media_evidence_bundle(value: object) -> RootMediaEvidenceBundle:
    item = _object(value, ("root_media_evidence_bundle_id", "source_id", "source_sha256", "source_manifest_sha256",
                           "root_input_manifest_sha256", "frame_pts_index", "shot_boundaries", "scene_boundaries",
                           "audio_sample_boundaries", "transcript", "speech_activity", "visual_validity", "subtitle_cues"))
    return RootMediaEvidenceBundle(
        _text(item["root_media_evidence_bundle_id"]), _text(item["source_id"]), _hash(item["source_sha256"]),
        _hash(item["source_manifest_sha256"]), _hash(item["root_input_manifest_sha256"]),
        decode_frame_pts_index_set(item["frame_pts_index"]), decode_shot_boundary_set(item["shot_boundaries"]),
        decode_scene_boundary_set(item["scene_boundaries"]), decode_audio_sample_boundary_set(item["audio_sample_boundaries"]),
        decode_transcript_set(item["transcript"]), decode_speech_activity_set(item["speech_activity"]),
        decode_visual_validity_set(item["visual_validity"]), decode_subtitle_cue_set(item["subtitle_cues"]),
    )


def _json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MediaValidationError("evidence JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_number(_value: str) -> object:
    raise MediaValidationError("evidence JSON forbids floats and nonfinite numbers")


def decode_root_media_evidence_bundle_json(raw: bytes, *, max_bytes: int) -> RootMediaEvidenceBundle:
    """Decode bounded strict UTF-8 JSON; formatting does not confer authority."""
    if type(raw) is not bytes or not raw:  # noqa: E721
        raise MediaValidationError("evidence JSON must be nonempty bytes")
    if _integer(max_bytes) <= 0 or len(raw) > max_bytes:
        raise MediaValidationError("evidence JSON exceeds its explicit positive byte limit")
    try:
        value: object = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_json_pairs,
                                   parse_float=_reject_number, parse_constant=_reject_number)
    except MediaValidationError:
        raise
    except (ValueError, RecursionError) as error:
        raise MediaValidationError("evidence must be finite-depth strict UTF-8 JSON") from error
    return decode_root_media_evidence_bundle(value)
