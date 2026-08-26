"""Assess raw-bound local speech against an exact, replayed A/V presentation map.

This is a pure transition input for ``advance_candidate_evidence_window``.  It
does not commit a child, admit a profile, or infer sentence completeness.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Iterable

from autocut_kernel.physical_edit.presentation_map import ReplayedPresentationMap

from .local_audio_window import LocalAudioWindowSpec
from .local_speech_window_projection import (
    LocalSpeechWindowEvidence,
    LocalSpeechWindowProjectionError,
    project_local_speech_window,
)
from .root_evidence import CoverageOutcome, SpeechActivitySet, TranscriptSet
from .timed_evidence import (
    AdaptiveEvidenceWindowPolicy,
    CalibrationBinding,
    CandidateEvidenceWindow,
    CandidateWindowAssessment,
    SentenceCompleteness,
    validate_calibration_bindings,
)
from .types import TickRange


class MappedLocalWindowAssessmentError(ValueError):
    """A local observation cannot be proved against its original A/V clocks."""


def _exact(value: object, expected: type[object], name: str) -> None:
    if type(value) is not expected:  # noqa: E721
        raise MappedLocalWindowAssessmentError(f"{name} must be an exact {expected.__name__}")


def _replay(evidence: LocalSpeechWindowEvidence) -> LocalSpeechWindowEvidence:
    _exact(evidence, LocalSpeechWindowEvidence, "local speech evidence")
    try:
        evidence.decoded.report.validate_for(evidence.decoded.request.extraction)
        replayed = project_local_speech_window(evidence.decoded)
    except (LocalSpeechWindowProjectionError, ValueError) as error:
        raise MappedLocalWindowAssessmentError("local speech evidence cannot replay raw projection") from error
    if (
        replayed.decoded != evidence.decoded
        or replayed.transcript != evidence.transcript
        or replayed.speech_activity != evidence.speech_activity
    ):
        raise MappedLocalWindowAssessmentError("local speech typed evidence differs from raw projection")
    return replayed


def _validate_local_contexts(
    transcript: TranscriptSet,
    speech: SpeechActivitySet,
    spec: LocalAudioWindowSpec,
    asr_calibration: CalibrationBinding,
    vad_calibration: CalibrationBinding,
) -> None:
    expected = (
        spec.source_id,
        spec.source_sha256,
        spec.clock_id,
        spec.time_base,
        spec.requested_range.start_pts,
        spec.requested_range.end_pts,
    )
    for name, evidence in (("transcript", transcript), ("speech activity", speech)):
        context = evidence.context
        coverage = evidence.coverage
        if (
            context.source_id,
            context.source_sha256,
            context.clock_id,
            context.time_base,
            context.origin_tick,
            context.end_tick,
        ) != expected:
            raise MappedLocalWindowAssessmentError(f"{name} does not bind the exact local extraction")
        if (
            coverage.outcome is not CoverageOutcome.COMPLETE
            or (coverage.in_tick, coverage.out_tick)
            != (spec.requested_range.start_pts, spec.requested_range.end_pts)
        ):
            raise MappedLocalWindowAssessmentError(f"{name} local coverage is incomplete")
    try:
        validate_calibration_bindings(
            (asr_calibration, vad_calibration),
            (transcript.context, speech.context),
        )
    except ValueError as error:
        raise MappedLocalWindowAssessmentError("ASR/VAD calibration bindings do not close local contexts") from error
    for name, calibration, context in (
        ("ASR", asr_calibration, transcript.context),
        ("VAD", vad_calibration, speech.context),
    ):
        if (
            calibration.producer_id,
            calibration.policy_sha256,
            calibration.time_base,
        ) != (
            context.producer_id,
            context.generation_policy_sha256,
            context.time_base,
        ):
            raise MappedLocalWindowAssessmentError(
                f"{name} calibration does not bind its own local producer context"
            )


def _validate_map_bindings(
    candidate: CandidateEvidenceWindow,
    presentation_map: ReplayedPresentationMap,
    spec: LocalAudioWindowSpec,
) -> tuple[int, int, int, int]:
    root = presentation_map.root
    video = root.frame_pts_index.context
    audio = root.audio_sample_boundaries.context
    if (
        candidate.source_id,
        candidate.source_sha256,
        candidate.source_clock_id,
        candidate.source_time_base,
        candidate.source_range,
        candidate.frame_pts_index_set_sha256,
    ) != (
        root.source_id,
        root.source_sha256,
        video.clock_id,
        video.time_base,
        TickRange(video.origin_tick, video.end_tick),
        root.frame_pts_index.canonical_hash,
    ):
        raise MappedLocalWindowAssessmentError("candidate does not bind the exact mapped video source")
    if (
        spec.source_id,
        spec.source_sha256,
        spec.audio_stream_index,
        spec.clock_id,
        spec.time_base,
        spec.source_range,
        spec.audio_boundary_set_sha256,
    ) != (
        root.source_id,
        root.source_sha256,
        presentation_map.probe.audio.stream_index,
        audio.clock_id,
        audio.time_base,
        TickRange(audio.origin_tick, audio.end_tick),
        root.audio_sample_boundaries.canonical_hash,
    ):
        raise MappedLocalWindowAssessmentError("local extraction does not bind exact audio facts")
    try:
        start_floor, start_ceil = presentation_map.map_video_tick_bounds(
            candidate.current_range.start_pts
        )
        end_floor, end_ceil = presentation_map.map_video_tick_bounds(
            candidate.current_range.end_pts
        )
        if (
            spec.requested_range.start_pts > start_floor
            or spec.requested_range.end_pts < end_ceil
        ):
            raise MappedLocalWindowAssessmentError(
                "local extraction does not outwardly cover mapped video boundaries"
            )
        presentation_map.require_av_span_covered(candidate.current_range, spec.requested_range)
    except MappedLocalWindowAssessmentError:
        raise
    except ValueError as error:
        raise MappedLocalWindowAssessmentError("candidate local A/V span is unmapped or uncovered") from error
    return start_floor, start_ceil, end_floor, end_ceil


def _guard(
    presentation_map: ReplayedPresentationMap,
    video_start: int,
    video_end: int,
    *,
    allowance: int,
) -> tuple[int, int]:
    start_floor, _ = _audio_tick_bounds(presentation_map, video_start)
    _, end_ceil = _audio_tick_bounds(presentation_map, video_end)
    return start_floor - allowance, end_ceil + allowance


def _audio_tick_bounds(presentation_map: ReplayedPresentationMap, video_tick: int) -> tuple[int, int]:
    """Map an interior measurement endpoint without pretending it is a frame.

    ``require_av_span_covered`` has already established the whole candidate in
    one certificate interval.  These policy-margin endpoints are merely
    measurement guards, so they retain exact absolute presentation arithmetic
    rather than weakening the public decoded-frame map API.
    """
    video_base = presentation_map.probe.video.time_base
    audio_base = presentation_map.probe.audio.time_base
    presentation = Fraction(video_tick * video_base.numerator, video_base.denominator)
    audio_tick = presentation / Fraction(audio_base.numerator, audio_base.denominator)
    return (
        audio_tick.numerator // audio_tick.denominator,
        -((-audio_tick.numerator) // audio_tick.denominator),
    )


def _overlaps(records: Iterable[object], low: int, high: int) -> bool:
    return any(getattr(record, "in_tick") < high and low < getattr(record, "out_tick") for record in records)


def _spans(records: Iterable[object], low: int, high: int) -> bool:
    return any(getattr(record, "in_tick") < low and high < getattr(record, "out_tick") for record in records)


def assess_mapped_local_window(
    evidence: LocalSpeechWindowEvidence,
    candidate_window: CandidateEvidenceWindow,
    presentation_map: ReplayedPresentationMap,
    *,
    adaptive_policy: AdaptiveEvidenceWindowPolicy,
    asr_calibration: CalibrationBinding,
    vad_calibration: CalibrationBinding,
) -> CandidateWindowAssessment:
    """Recompute one conservative local observation for the legacy plan transition.

    Speech timestamps remain in the original audio clock.  The only video/audio
    relation is the replayed presentation map; no local origin rebasing is used.
    """
    _exact(candidate_window, CandidateEvidenceWindow, "candidate window")
    _exact(presentation_map, ReplayedPresentationMap, "presentation map")
    _exact(adaptive_policy, AdaptiveEvidenceWindowPolicy, "adaptive policy")
    _exact(asr_calibration, CalibrationBinding, "ASR calibration")
    _exact(vad_calibration, CalibrationBinding, "VAD calibration")
    if adaptive_policy.time_base != candidate_window.source_time_base:
        raise MappedLocalWindowAssessmentError("adaptive policy clock differs from candidate video clock")

    replayed = _replay(evidence)
    spec = replayed.decoded.request.extraction
    _validate_local_contexts(
        replayed.transcript,
        replayed.speech_activity,
        spec,
        asr_calibration,
        vad_calibration,
    )
    start_floor, start_ceil, end_floor, end_ceil = _validate_map_bindings(
        candidate_window,
        presentation_map,
        spec,
    )

    window = candidate_window.current_range
    margin = adaptive_policy.boundary_touch_margin_pts
    left_video_end = min(window.end_pts, window.start_pts + margin)
    right_video_start = max(window.start_pts, window.end_pts - margin)
    snap = presentation_map.certificate.snap_error_allowance_audio_tick
    asr_allowance = asr_calibration.timing_error_bound_tick + snap
    vad_allowance = vad_calibration.timing_error_bound_tick + snap
    asr_left = _guard(
        presentation_map,
        window.start_pts,
        left_video_end,
        allowance=asr_allowance,
    )
    asr_right = _guard(
        presentation_map,
        right_video_start,
        window.end_pts,
        allowance=asr_allowance,
    )
    vad_left = _guard(
        presentation_map,
        window.start_pts,
        left_video_end,
        allowance=vad_allowance,
    )
    vad_right = _guard(
        presentation_map,
        right_video_start,
        window.end_pts,
        allowance=vad_allowance,
    )
    asr_boundary_left = (start_floor - asr_allowance, start_ceil + asr_allowance)
    asr_boundary_right = (end_floor - asr_allowance, end_ceil + asr_allowance)
    video = presentation_map.root.frame_pts_index.context
    audio = presentation_map.root.audio_sample_boundaries.context
    true_left_edge = (
        window.start_pts == video.origin_tick
        and spec.requested_range.start_pts == audio.origin_tick
    )
    true_right_edge = (
        window.end_pts == video.end_tick
        and spec.requested_range.end_pts == audio.end_tick
    )
    transcript_records = (*replayed.transcript.segments, *replayed.transcript.words)
    speech = replayed.speech_activity.segments
    return CandidateWindowAssessment(
        candidate_window.canonical_hash,
        not true_left_edge and _overlaps(transcript_records, *asr_left),
        not true_right_edge and _overlaps(transcript_records, *asr_right),
        not true_left_edge and _overlaps(speech, *vad_left),
        not true_right_edge and _overlaps(speech, *vad_right),
        not true_left_edge and _spans(transcript_records, *asr_boundary_left),
        not true_right_edge and _spans(transcript_records, *asr_boundary_right),
        SentenceCompleteness.NOT_APPLICABLE,
    )


__all__ = ["MappedLocalWindowAssessmentError", "assess_mapped_local_window"]
