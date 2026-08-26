"""Strict value decoding for persisted preflight clock and speech proofs.

Decoding does not establish commitment or grant physical admission. Consumers
must replay these values against the actual SourceManifest probe, root evidence,
registered profile and calibration. SourceManifest already owns probe decoding;
this module deliberately does not introduce a second probe decoder.
"""

from __future__ import annotations

from typing import cast

from .root_evidence_codec import decode_time_base
from .stage4_predecessor import (
    AVPresentationMapSegment,
    CommittedVideoToAudioClockMapCertificate,
    PresentationNonOverlap,
    PresentationNonOverlapMedia,
    PresentationNonOverlapPosition,
    RationalPresentationInterval,
    Stage4PredecessorError,
    TimedSpeechCapability,
    TimedSpeechProfileAdmission,
)
from .types import TickRange, require_pts


def _object(value: object, fields: tuple[str, ...], name: str) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise Stage4PredecessorError(f"{name} must match its closed object schema")
    raw = cast(dict[object, object], value)  # Runtime checked the exact container type.
    if set(raw) != set(fields):
        raise Stage4PredecessorError(f"{name} must match its closed object schema")
    # Exact dict and closed string keys have been checked at the wire boundary.
    return cast(dict[str, object], value)


def _array(value: object, name: str) -> list[object]:
    if type(value) is not list:  # noqa: E721
        raise Stage4PredecessorError(f"{name} must be a JSON array")
    return cast(list[object], value)


def _text(value: object, name: str) -> str:
    if type(value) is not str:  # noqa: E721
        raise Stage4PredecessorError(f"{name} must be text")
    return value


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:  # noqa: E721
        raise Stage4PredecessorError(f"{name} must be boolean")
    return value


def _interval(value: object) -> RationalPresentationInterval:
    raw = _object(value, ("start_numerator", "start_denominator", "end_numerator", "end_denominator"),
                  "presentation interval")
    return RationalPresentationInterval(
        require_pts(raw["start_numerator"], "start_numerator"),
        require_pts(raw["start_denominator"], "start_denominator"),
        require_pts(raw["end_numerator"], "end_numerator"),
        require_pts(raw["end_denominator"], "end_denominator"),
    )


def _tick_range(value: object) -> TickRange:
    # This producer emits start_tick/end_tick, not root evidence's PTS keys.
    raw = _object(value, ("start_tick", "end_tick"), "presentation stream range")
    return TickRange(require_pts(raw["start_tick"], "start_tick"),
                     require_pts(raw["end_tick"], "end_tick"))


def _segment(value: object) -> AVPresentationMapSegment:
    raw = _object(value, ("video_tick_range", "audio_tick_range", "presentation_interval"),
                  "presentation map segment")
    return AVPresentationMapSegment(
        _tick_range(raw["video_tick_range"]), _tick_range(raw["audio_tick_range"]),
        _interval(raw["presentation_interval"]),
    )


def _non_overlap(value: object) -> PresentationNonOverlap:
    raw = _object(value, ("media", "position", "presentation_interval"), "non-overlap")
    try:
        media = PresentationNonOverlapMedia(_text(raw["media"], "non-overlap media"))
        position = PresentationNonOverlapPosition(_text(raw["position"], "non-overlap position"))
    except ValueError as error:
        raise Stage4PredecessorError("non-overlap enum is invalid") from error
    return PresentationNonOverlap(media, position, _interval(raw["presentation_interval"]))


def decode_committed_video_to_audio_clock_map_certificate(
    value: object,
) -> CommittedVideoToAudioClockMapCertificate:
    """Preserve all v2 segments and provenance; do not rebuild a primitive map."""
    raw = _object(value, (
        "schema_version", "certificate_compiler_id", "certificate_compiler_contract_sha256",
        "facts_sha256", "root_evidence_sha256", "frame_pts_index_sha256",
        "audio_boundary_set_sha256", "source_manifest_sha256", "algorithm", "map_segments",
        "common_presentation_intervals", "non_overlaps", "snap_error_allowance_audio_tick",
        "calibration_binding_sha256", "window_manifest_sha256", "source_proxy_timeline_map_sha256",
    ), "committed presentation map")
    return CommittedVideoToAudioClockMapCertificate(
        schema_version=_text(raw["schema_version"], "schema_version"),
        certificate_compiler_id=_text(raw["certificate_compiler_id"], "certificate_compiler_id"),
        certificate_compiler_contract_sha256=_text(raw["certificate_compiler_contract_sha256"], "certificate compiler hash"),
        facts_sha256=_text(raw["facts_sha256"], "facts_sha256"),
        root_evidence_sha256=_text(raw["root_evidence_sha256"], "root_evidence_sha256"),
        frame_pts_index_sha256=_text(raw["frame_pts_index_sha256"], "frame_pts_index_sha256"),
        audio_boundary_set_sha256=_text(raw["audio_boundary_set_sha256"], "audio_boundary_set_sha256"),
        source_manifest_sha256=_text(raw["source_manifest_sha256"], "source_manifest_sha256"),
        algorithm=_text(raw["algorithm"], "algorithm"),
        map_segments=tuple(_segment(item) for item in _array(raw["map_segments"], "map_segments")),
        common_presentation_intervals=tuple(
            _interval(item) for item in _array(raw["common_presentation_intervals"], "common intervals")
        ),
        non_overlaps=tuple(_non_overlap(item) for item in _array(raw["non_overlaps"], "non_overlaps")),
        snap_error_allowance_audio_tick=require_pts(raw["snap_error_allowance_audio_tick"], "snap allowance"),
        calibration_binding_sha256=_text(raw["calibration_binding_sha256"], "calibration_binding_sha256"),
        window_manifest_sha256=_optional_text(raw["window_manifest_sha256"], "window_manifest_sha256"),
        source_proxy_timeline_map_sha256=_optional_text(raw["source_proxy_timeline_map_sha256"], "source_proxy_timeline_map_sha256"),
    )


def decode_timed_speech_profile_admission(value: object) -> TimedSpeechProfileAdmission:
    """Decode the value body only; the reader owns the outer registry reference."""
    raw = _object(value, (
        "registry_member_content_hash", "registry_entry_sha256", "root_evidence_sha256",
        "transcript_evidence_sha256", "vad_evidence_sha256", "transcript_calibration_sha256",
        "vad_calibration_sha256", "source_id", "source_sha256", "source_audio_clock_id",
        "source_audio_time_base", "words_complete", "sentences_complete", "vad_complete", "capability",
    ), "timed speech profile admission")
    try:
        capability = TimedSpeechCapability(_text(raw["capability"], "capability"))
    except ValueError as error:
        raise Stage4PredecessorError("timed speech capability is invalid") from error
    return TimedSpeechProfileAdmission(
        registry_member_content_hash=_text(raw["registry_member_content_hash"], "registry_member_content_hash"),
        registry_entry_sha256=_text(raw["registry_entry_sha256"], "registry_entry_sha256"),
        root_evidence_sha256=_text(raw["root_evidence_sha256"], "root_evidence_sha256"),
        transcript_evidence_sha256=_text(raw["transcript_evidence_sha256"], "transcript_evidence_sha256"),
        vad_evidence_sha256=_text(raw["vad_evidence_sha256"], "vad_evidence_sha256"),
        transcript_calibration_sha256=_text(raw["transcript_calibration_sha256"], "transcript_calibration_sha256"),
        vad_calibration_sha256=_text(raw["vad_calibration_sha256"], "vad_calibration_sha256"),
        source_id=_text(raw["source_id"], "source_id"),
        source_sha256=_text(raw["source_sha256"], "source_sha256"),
        source_audio_clock_id=_text(raw["source_audio_clock_id"], "source_audio_clock_id"),
        source_audio_time_base=decode_time_base(raw["source_audio_time_base"]),
        words_complete=_boolean(raw["words_complete"], "words_complete"),
        sentences_complete=_boolean(raw["sentences_complete"], "sentences_complete"),
        vad_complete=_boolean(raw["vad_complete"], "vad_complete"),
        capability=capability,
    )


__all__ = (
    "decode_committed_video_to_audio_clock_map_certificate",
    "decode_timed_speech_profile_admission",
)
