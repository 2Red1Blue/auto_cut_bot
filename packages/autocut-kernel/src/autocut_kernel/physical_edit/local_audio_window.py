"""Derive a native audio extraction window from exact physical clock facts.

This pure mapping is neither Store authority nor speech/edit admission. The
later child request must bind the candidate, physical prelude and returned
spec; native decoding must still prove the requested samples exist.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from fractions import Fraction

from ..media.audio_stream_facts import AudioStreamFacts
from ..media.local_audio_window import LocalAudioWindowError, LocalAudioWindowSpec
from ..media.timed_evidence import CandidateEvidenceWindow
from ..media.types import TickRange
from .presentation_map import ReplayedPresentationMap


def derive_local_audio_window_spec(
    candidate_window: CandidateEvidenceWindow,
    presentation_map: ReplayedPresentationMap,
    audio_stream_facts: AudioStreamFacts,
    *,
    decoder_identity_sha256: str,
    max_outward_padding_audio_ticks: int,
    max_source_bytes: int,
    max_decode_frames: int,
    max_frame_bytes: int,
    max_pcm_bytes: int,
) -> LocalAudioWindowSpec:
    """Choose nearest outward *actual* samples, never round inward or rebase.

    Padding is measured from each exact rational mapped endpoint in audio
    ticks. Floor/ceil alone cannot establish zero-padding or full gap coverage.
    """
    if (type(candidate_window) is not CandidateEvidenceWindow
            or type(presentation_map) is not ReplayedPresentationMap
            or type(audio_stream_facts) is not AudioStreamFacts):
        raise LocalAudioWindowError("window mapping requires exact candidate/map/audio facts")
    if type(max_outward_padding_audio_ticks) is not int or max_outward_padding_audio_ticks < 0:
        raise LocalAudioWindowError("outward padding must be a nonnegative integer")
    root = presentation_map.root
    frame_index = root.frame_pts_index
    video = frame_index.context
    if (
        candidate_window.source_id != root.source_id
        or candidate_window.source_sha256 != root.source_sha256
        or candidate_window.source_clock_id != video.clock_id
        or candidate_window.source_time_base != video.time_base
        or candidate_window.source_range != TickRange(video.origin_tick, video.end_tick)
        or candidate_window.frame_pts_index_set_sha256 != frame_index.canonical_hash
    ):
        raise LocalAudioWindowError("candidate differs from exact source video evidence")
    audio_index = root.audio_sample_boundaries
    audio_stream_facts.assert_matches(presentation_map.probe, audio_index)
    current = candidate_window.current_range
    start_floor, _ = presentation_map.map_video_tick_bounds(current.start_pts)
    _, end_ceil = presentation_map.map_video_tick_bounds(current.end_pts)
    points = audio_index.points
    start_ordinal = bisect_right(points, start_floor, key=lambda point: point.tick) - 1
    end_ordinal = bisect_left(points, end_ceil, key=lambda point: point.tick)
    if start_ordinal < 0 or end_ordinal == len(points):
        raise LocalAudioWindowError("no actual outward audio endpoints cover the video window")
    start, end = points[start_ordinal].tick, points[end_ordinal].tick
    # Absolute source presentation: audio/video origins are not interchangeable.
    audio_base = audio_stream_facts.time_base
    ratio = Fraction(video.time_base.numerator, video.time_base.denominator) / Fraction(
        audio_base.numerator, audio_base.denominator,
    )
    if (current.start_pts * ratio - start > max_outward_padding_audio_ticks
            or end - current.end_pts * ratio > max_outward_padding_audio_ticks):
        raise LocalAudioWindowError("actual audio endpoints exceed exact outward padding")
    requested_range = TickRange(start, end)
    presentation_map.require_av_span_covered(current, requested_range)
    return LocalAudioWindowSpec(
        source_id=root.source_id,
        source_sha256=root.source_sha256,
        audio_stream_index=audio_stream_facts.stream_index,
        clock_id=audio_stream_facts.clock_id,
        time_base=audio_base,
        source_range=TickRange(audio_stream_facts.origin_tick, audio_stream_facts.end_tick),
        requested_range=requested_range,
        sample_rate=audio_stream_facts.sample_rate,
        channels=audio_stream_facts.channels,
        audio_boundary_set_sha256=audio_index.canonical_hash,
        decoder_identity_sha256=decoder_identity_sha256,
        max_source_bytes=max_source_bytes,
        max_decode_frames=max_decode_frames,
        max_frame_bytes=max_frame_bytes,
        max_pcm_bytes=max_pcm_bytes,
    )
