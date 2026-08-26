"""Independent small-space Cartesian oracle for candidate-local physical edits."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from itertools import product
from uuid import uuid4

import pytest
from autocut_kernel.media import PTSIndex, VisualClassification
from autocut_kernel.media.stage4_predecessor import (
    PresentationSegmentContinuity,
    PresentationTrackSegment,
    RationalPresentationInterval,
    derive_presentation_timeline_facts,
)
from autocut_kernel.media.timed_evidence import CalibrationBinding
from autocut_kernel.media.types import MediaValidationError, TickRange, canonical_sha256
from autocut_kernel.physical_edit.candidate_exact_span import (
    CandidateExactSpanPolicy,
    compile_candidate_av_span,
)
from autocut_kernel.physical_edit.dialogue_guard import DialogueRequirement
from autocut_kernel.physical_edit.exact_span import (
    CandidatePairLimitError,
    ExactAvSpanRequest,
    ExactSpanValidationError,
    NoLegalSpanError,
    VideoClockRange,
)
from autocut_kernel.physical_edit.presentation_map import ReplayedPresentationMap
from autocut_kernel.store.models import BlobRef

from tests.media.test_candidate_dialogue_guard import candidate_dialogue_case
from tests.media.test_prepare_timed_media_evidence_command import (
    _manifest_and_candidate,
    _presentation_facts,
)
from tests.media.test_root_evidence import HASH_A, HASH_B, HASH_C


def _case(*, gap: tuple[int, int] | None = None, vad_only: bool = False,
          subtitle_start: int = 45, extra_shot: bool = False, unknown_start: bool = False,
          end_sentinel: bool = False):
    root, candidate, plan, profile = candidate_dialogue_case(vad_only=vad_only)
    # Nonuniform decoded frame PTS; all coarse query edges are deliberately
    # absent. The original global root is authored before compilation.
    ticks = (0, 25, 30, 35, 39, 50, 61, 65, 70, 75)
    index = PTSIndex(ticks if end_sentinel else (*ticks, 100))
    frames = replace(root.frame_pts_index, pts_index=index,
                     pts_index_sha256=canonical_sha256(list(index.ticks)))
    samples = replace(root.audio_sample_boundaries, points=tuple(
        replace(root.audio_sample_boundaries.points[0], boundary_id=f"sample-{tick}", tick=tick)
        for tick in range(101)
    ))
    shots = root.shot_boundaries.points
    if extra_shot:
        shots = (shots[0], replace(shots[0], boundary_id="shot-050", tick=50), shots[1])
    visual = root.visual_validity
    if end_sentinel:
        visual = replace(visual, intervals=(replace(visual.intervals[0], out_tick=100),))
    if unknown_start:
        visual = replace(visual, intervals=(
            replace(visual.intervals[0], out_tick=38),
            replace(visual.intervals[1], visual_interval_id="unknown-mid", in_tick=38, out_tick=40),
            replace(visual.intervals[0], visual_interval_id="valid-tail", in_tick=40),
            visual.intervals[1],
        ))
    root = replace(
        root, frame_pts_index=frames, audio_sample_boundaries=samples,
        shot_boundaries=replace(root.shot_boundaries, frame_pts_index_set_sha256=frames.canonical_hash, points=shots),
        scene_boundaries=replace(root.scene_boundaries, frame_pts_index_set_sha256=frames.canonical_hash),
        visual_validity=visual,
        subtitle_cues=replace(root.subtitle_cues, cues=(
            replace(root.subtitle_cues.cues[0], in_tick=subtitle_start, out_tick=50),
        )),
    )
    window = replace(candidate.candidate_window, frame_pts_index_set_sha256=frames.canonical_hash)
    if end_sentinel:
        window = replace(window, current_range=TickRange(25, 100))
        candidate = replace(
            candidate,
            transcript=replace(candidate.transcript,
                               context=replace(candidate.transcript.context, duration_tick=41),
                               coverage=replace(candidate.transcript.coverage, out_tick=54)),
            speech_activity=replace(candidate.speech_activity,
                                    context=replace(candidate.speech_activity.context, duration_tick=41),
                                    coverage=replace(candidate.speech_activity.coverage, out_tick=54)),
        )
    assessment = replace(candidate.window_assessment, candidate_window_sha256=window.canonical_hash)
    plan = replace(plan, windows=(window,), assessments=(assessment,))
    candidate = replace(
        candidate, candidate_window=window, window_assessment=assessment,
        frame_pts_index=frames, audio_sample_boundaries=samples,
        shot_boundaries=root.shot_boundaries, scene_boundaries=root.scene_boundaries,
        subtitle_cues=root.subtitle_cues, visual_validity=visual,
    )
    manifest, _, _ = _manifest_and_candidate()
    probe = _presentation_facts(root, BlobRef(uuid4(), root.source_sha256, 1, "video/mp4"), manifest)
    if gap is not None:
        track = probe.audio
        cuts = (track.origin_tick, *gap, track.end_tick)
        probe = replace(probe, audio=replace(track, segments=tuple(
            PresentationTrackSegment(
                TickRange(start, end),
                RationalPresentationInterval.from_fractions(Fraction(start, 48_000), Fraction(end, 48_000)),
                HASH_A, PresentationSegmentContinuity.DECLARED_GAP if ordinal == 1
                else PresentationSegmentContinuity.CONTINUOUS_DECODED,
            ) for ordinal, (start, end) in enumerate(zip(cuts, cuts[1:], strict=False))
        )))
    context = samples.context
    calibration = CalibrationBinding(
        context.generation_policy_sha256, HASH_B, HASH_C, context.producer_id,
        "numerical-vector-v1", context.time_base, 1, True, HASH_A,
    )
    probe, certificate = derive_presentation_timeline_facts(
        root, probe=probe, source_manifest_sha256=HASH_B, audio_snap_calibration=calibration,
    )
    clock = ReplayedPresentationMap(root, probe, certificate, HASH_B, calibration)
    video = frames.context
    def bound(start: int, end: int) -> VideoClockRange:
        return VideoClockRange(video.source_id, video.source_sha256, video.clock_id,
                               video.time_base, TickRange(start, end))
    request = ExactAvSpanRequest(bound(26, 100 if end_sentinel else 74),
                                 bound(41, 99 if end_sentinel else 59), 20, DialogueRequirement.NOT_REQUIRED)
    policy = CandidateExactSpanPolicy(10_000, 10_000, 1, 1, 1)
    return request, root, candidate, plan, profile, clock, policy


def _oracle(request, root, policy):
    """Exhaust all four domains directly, not the compiler's bisected domains.

    This vector has zero presentation offsets, no gaps, known speech [23,25],
    local audio coverage [13,41], and valid visual content [0,80]. Compute
    rational mapping and every hard predicate here without compiler helpers.
    """
    desired, anchor = request.desired_video_range.tick_range, request.anchor_video_range.tick_range
    starts = [v for v in root.frame_pts_index.pts_index.ticks if desired.start_pts <= v <= anchor.start_pts]
    ends = [v for v in root.frame_pts_index.pts_index.ticks if anchor.end_pts <= v <= desired.end_pts]
    samples = [p.tick for p in root.audio_sample_boundaries.points if 13 <= p.tick <= 41]
    relation = []
    start_domain, end_domain = set(), set()
    for v, a in product(starts + ends, samples):
        exact = Fraction(v * 8, 15)
        low, high = exact.__floor__(), exact.__ceil__()
        if low - policy.av_sync_tolerance_audio_tick <= a <= high + policy.av_sync_tolerance_audio_tick:
            (start_domain if v in starts else end_domain).add(a)
    visits = 0
    for vin, vout, ain, aout in product(starts, ends, samples, samples):
        low_in, high_in = Fraction(vin * 8, 15).__floor__(), Fraction(vin * 8, 15).__ceil__()
        low_out, high_out = Fraction(vout * 8, 15).__floor__(), Fraction(vout * 8, 15).__ceil__()
        tolerance = policy.av_sync_tolerance_audio_tick
        if not (low_in - tolerance <= ain <= high_in + tolerance
                and low_out - tolerance <= aout <= high_out + tolerance):
            continue
        width = policy.endpoint_stability_video_tick
        if vout - vin < request.minimum_video_duration_tick or vin + width > 80 or vout - width < 0:
            continue
        if any(vin < p.tick < vin + width or vout - width < p.tick < vout for p in root.shot_boundaries.points):
            continue
        if any(c.in_tick - c.timing_error_bound.in_tick - policy.subtitle_clearance_floor_video_tick < edge
               < c.out_tick + c.timing_error_bound.out_tick + policy.subtitle_clearance_floor_video_tick
               for c, edge in product(root.subtitle_cues.cues, (vin, vout))):
            continue
        visits += 1
        if ain >= aout or 23 < ain < 25 or 23 < aout < 25:
            continue
        # Full A/V source intersection: video ends at 100/90000, while audio
        # lasts 100/48000. Never scale the latter down to the former.
        if not (0 <= min(Fraction(vin, 90_000), Fraction(ain, 48_000))
                and max(Fraction(vout, 90_000), Fraction(aout, 48_000)) <= Fraction(100, 90_000)):
            continue
        key = (anchor.start_pts - vin, vout - anchor.end_pts,
               abs(ain - (low_in + high_in) // 2), abs(aout - (low_out + high_out) // 2),
               vout - vin, aout - ain, vin, vout, ain, aout)
        relation.append({"decision_key": list(key), "endpoints": [vin, vout, ain, aout]})
    return relation, len(starts) * len(ends) * len(start_domain) * len(end_domain), visits


@pytest.mark.parametrize("tolerance", [0, 1, 3])
@pytest.mark.parametrize("vad_only", [False, True])
def test_native_search_equals_full_cartesian_oracle(tolerance: int, vad_only: bool):
    request, root, candidate, plan, profile, clock, policy = _case(vad_only=vad_only)
    policy = replace(policy, av_sync_tolerance_audio_tick=tolerance)
    original_root = root.to_mapping()
    result = compile_candidate_av_span(request, root, candidate, plan, profile, clock, policy)
    relation, logical_count, visits = _oracle(request, root, policy)
    winner = min(relation, key=lambda row: row["decision_key"])
    assert len(relation) > 1
    assert result.feasible_relation_sha256 == canonical_sha256(relation)
    assert result.feasible_count == len(relation)
    assert result.logical_cartesian_count_decimal == str(logical_count)
    assert result.visited_av_pair_count == visits
    assert list(result.canonical_decision_key) == winner["decision_key"]
    assert [result.video_range.start_pts, result.video_range.end_pts,
            result.audio_range.start_pts, result.audio_range.end_pts] == winner["endpoints"]
    assert root.to_mapping() == original_root
    assert result.dialogue_guard.root_evidence_sha256 == root.canonical_hash
    assert result.canonical_hash == compile_candidate_av_span(
        request, root, candidate, plan, profile, clock, policy,
    ).canonical_hash


def test_gap_between_otherwise_legal_endpoints_rejects_entire_span():
    args = _case(gap=(25, 28))
    # Both endpoints map individually; it is the full-span constraint that
    # rejects this cut, not an empty endpoint domain.
    assert args[5].map_video_tick_bounds(39) == (20, 21)
    assert args[5].map_video_tick_bounds(61) == (32, 33)
    with pytest.raises(NoLegalSpanError, match="complete relation"):
        compile_candidate_av_span(*args)


@pytest.mark.parametrize("limits", [(1, 10_000), (10_000, 1), (10_000, 16)])
def test_work_exhaustion_never_returns_best_partial_prefix(limits):
    *args, policy = _case()
    with pytest.raises(CandidatePairLimitError):
        compile_candidate_av_span(*args, replace(policy, max_video_pair_visits=limits[0], max_av_pair_visits=limits[1]))


@pytest.mark.parametrize("field,value", [
    ("endpoint_stability_video_tick", 0), ("subtitle_clearance_floor_video_tick", 0),
    ("max_av_pair_visits", True), ("av_sync_tolerance_audio_tick", 1.0),
])
def test_policy_never_coerces_or_defaults_safety_values(field, value):
    with pytest.raises((MediaValidationError, ExactSpanValidationError)):
        replace(_case()[-1], **{field: value})


def test_query_outside_local_window_or_with_foreign_clock_is_not_accepted():
    request, *args = _case()
    with pytest.raises(ExactSpanValidationError, match="exceeds candidate-local"):
        compile_candidate_av_span(replace(request, desired_video_range=replace(
            request.desired_video_range, tick_range=TickRange(0, 100),
        )), *args)
    foreign = replace(request, desired_video_range=replace(request.desired_video_range, clock_id="foreign"),
                      anchor_video_range=replace(request.anchor_video_range, clock_id="foreign"))
    with pytest.raises(ExactSpanValidationError, match="source video clock"):
        compile_candidate_av_span(foreign, *args)


def test_subtitle_uncertainty_and_clearance_change_the_canonical_cut():
    args = _case(subtitle_start=38)
    result = compile_candidate_av_span(*args)
    # Cue starts at 38; error=2 plus clearance=1 excludes 39, permits 35.
    assert result.video_range == TickRange(35, 61)
    *inputs, policy = args
    stricter = compile_candidate_av_span(*inputs, replace(policy, subtitle_clearance_floor_video_tick=2))
    assert stricter.video_range == TickRange(30, 61)


def test_short_shot_neighborhood_is_conjunctive_with_visual_validity():
    *args, policy = _case(extra_shot=True)
    result = compile_candidate_av_span(*args, replace(policy, endpoint_stability_video_tick=12))
    assert result.video_range == TickRange(35, 65)


def test_unknown_visual_ticks_reject_the_nearest_frame():
    args = _case(unknown_start=True)
    assert args[1].visual_validity.intervals[1].classification is VisualClassification.UNKNOWN
    result = compile_candidate_av_span(*args)
    assert result.video_range == TickRange(35, 61)


def test_no_legal_video_never_returns_a_raw_coarse_interval():
    args = _case(subtitle_start=25)
    with pytest.raises(NoLegalSpanError):
        compile_candidate_av_span(*args)


def test_proven_source_end_is_an_out_sentinel_without_stretching_audio_tail():
    args = _case(end_sentinel=True)
    result = compile_candidate_av_span(*args)
    assert not args[1].frame_pts_index.pts_index.contains(100)
    assert result.video_range == TickRange(39, 100)
    # Physical common coverage ends at 53 1/3 samples, although the source
    # audio extends to 100. Tolerance may not admit sample 54 outside it.
    assert result.audio_range == TickRange(20, 53)
