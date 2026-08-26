"""Native v2 clock consumption; no Store/model or complete cut-safety claim."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from uuid import uuid4

import pytest
from autocut_kernel.media.root_evidence import VisualClassification
from autocut_kernel.media.stage4_predecessor import (
    PresentationSegmentContinuity,
    PresentationTrackSegment,
    RationalPresentationInterval,
    derive_presentation_timeline_facts,
)
from autocut_kernel.media.timed_evidence import CalibrationBinding
from autocut_kernel.media.types import MediaValidationError, TickRange
from autocut_kernel.physical_edit.presentation_map import (
    PresentationMapValidationError,
    ReplayedPresentationMap,
)
from autocut_kernel.store.models import BlobRef

from tests.media.test_exact_av_span import _bundle
from tests.media.test_prepare_timed_media_evidence_command import (
    _manifest_and_candidate,
    _presentation_facts,
)
from tests.media.test_presentation_evidence_codec import _decoded_producer_case
from tests.media.test_root_evidence import HASH_A, HASH_B, HASH_C


def _numerical_case(*, gap: tuple[int, int] | None = None, **bundle_options):
    # Author original domain values and actual probe segments, then derive the
    # certificate normally. These numerical vectors are not persisted sources.
    root = _bundle(**bundle_options)
    manifest, _, _ = _manifest_and_candidate()
    probe = _presentation_facts(root, BlobRef(uuid4(), root.source_sha256, 1, "video/mp4"), manifest)
    if gap is not None:
        track = probe.audio
        cuts = (track.origin_tick, *gap, track.end_tick)
        segments = tuple(PresentationTrackSegment(
            TickRange(start, end),
            RationalPresentationInterval.from_fractions(
                Fraction(start * track.time_base.numerator, track.time_base.denominator),
                Fraction(end * track.time_base.numerator, track.time_base.denominator),
            ), HASH_A,
            PresentationSegmentContinuity.DECLARED_GAP if ordinal == 1
            else PresentationSegmentContinuity.CONTINUOUS_DECODED,
        ) for ordinal, (start, end) in enumerate(zip(cuts, cuts[1:], strict=False)))
        probe = replace(probe, audio=replace(track, segments=segments))
    context = root.audio_sample_boundaries.context
    calibration = CalibrationBinding(
        context.generation_policy_sha256, HASH_B, HASH_C, context.producer_id,
        "numerical-vector-v1", context.time_base, 1, True, HASH_A,
    )
    probe, certificate = derive_presentation_timeline_facts(
        root, probe=probe, source_manifest_sha256=HASH_B, audio_snap_calibration=calibration,
    )
    return ReplayedPresentationMap(root, probe, certificate, HASH_B, calibration)


def test_actual_decoded_producer_certificate_is_consumed_without_replacing_root():
    _, request, _, root, _, probe, certificate, calibration = _decoded_producer_case()
    original_hash = root.canonical_hash
    domain = ReplayedPresentationMap(root, probe, certificate, request.source_manifest_sha256, calibration)
    video_tick = root.frame_pts_index.pts_index.ticks[1]
    exact = Fraction(video_tick * probe.video.time_base.numerator, probe.video.time_base.denominator)
    exact /= Fraction(probe.audio.time_base.numerator, probe.audio.time_base.denominator)
    assert domain.map_video_tick_bounds(video_tick) == (
        exact.numerator // exact.denominator, -((-exact.numerator) // exact.denominator),
    )
    assert domain.root is root and root.canonical_hash == original_hash
    assert domain.certificate is certificate


def test_gap_does_not_become_coverage_when_both_endpoints_map():
    domain = _numerical_case(gap=(32, 64))
    assert len(domain.certificate.map_segments) == 2
    assert domain.map_video_tick_bounds(60) == (32, 32)
    assert domain.map_video_tick_bounds(120) == (64, 64)
    assert domain.require_av_span_covered(TickRange(0, 60), TickRange(0, 32)) == 0
    assert domain.require_av_span_covered(TickRange(120, 180), TickRange(64, 96)) == 1
    with pytest.raises(PresentationMapValidationError, match="crosses a gap"):
        domain.require_av_span_covered(TickRange(60, 120), TickRange(32, 64))
    with pytest.raises(PresentationMapValidationError, match="crosses a gap"):
        domain.require_av_span_covered(TickRange(15, 180), TickRange(8, 96))


def test_rounding_envelope_cannot_admit_one_frame_tick_inside_gap():
    domain = _numerical_case(gap=(9, 32), frame_ticks=(0, 15, 16, 17, 18, 60, 120, 180),
                             audio_ticks=(0, 8, 9, 32, 64, 96))
    assert domain.certificate.map_segments[0].video_tick_range.end_pts == 17
    assert domain.map_video_tick_bounds(16) == (8, 9)
    with pytest.raises(PresentationMapValidationError, match="outside common"):
        domain.map_video_tick_bounds(17)
    with pytest.raises(PresentationMapValidationError, match="crosses a gap"):
        domain.require_av_span_covered(TickRange(0, 17), TickRange(0, 9))


def test_negative_origins_and_fractional_mapping_are_absolute_not_rebased():
    domain = _numerical_case(
        video_origin=-90, audio_origin=-48,
        frame_ticks=(-90, -46, -45, 0, 15, 60, 120, 180),
        audio_ticks=(-48, -25, -24, 0, 8, 32, 64, 96),
        visual=((-90, 180, VisualClassification.VALID_CONTENT),),
    )
    assert domain.map_video_tick_bounds(-90) == (-48, -48)
    assert domain.map_video_tick_bounds(-46) == (-25, -24)
    assert domain.require_av_span_covered(TickRange(-90, 0), TickRange(-48, 0)) == 0


def test_unequal_audio_tail_is_not_stretched_or_hidden_by_tolerance():
    domain = _numerical_case(audio_end=104, audio_ticks=(0, 8, 32, 64, 96, 104))
    assert domain.certificate.non_overlaps
    assert domain.map_video_tick_bounds(180) == (96, 96)
    assert domain.require_av_span_covered(TickRange(0, 180), TickRange(0, 96)) == 0
    with pytest.raises(PresentationMapValidationError, match="single-stream non-overlap"):
        domain.require_av_span_covered(TickRange(0, 180), TickRange(0, 104))


@pytest.mark.parametrize("field,value", [
    ("facts_sha256", HASH_A), ("root_evidence_sha256", HASH_A),
    ("calibration_binding_sha256", HASH_A), ("snap_error_allowance_audio_tick", 2),
])
def test_rehashed_certificate_claims_must_replay_original_evidence(field, value):
    domain = _numerical_case()
    with pytest.raises(PresentationMapValidationError, match="does not replay"):
        replace(domain, certificate=replace(domain.certificate, **{field: value}))


def test_foreign_root_manifest_or_calibration_are_not_accepted():
    domain = _numerical_case()
    with pytest.raises(PresentationMapValidationError, match="does not replay"):
        replace(domain, source_manifest_sha256=HASH_C)
    with pytest.raises(PresentationMapValidationError, match="does not replay"):
        replace(domain, root=replace(domain.root, root_media_evidence_bundle_id="foreign-root"))
    with pytest.raises(PresentationMapValidationError, match="does not replay"):
        replace(domain, audio_snap_calibration=replace(domain.audio_snap_calibration, timing_error_bound_tick=2))


def test_mapping_and_coverage_require_actual_frame_and_sample_boundaries():
    domain = _numerical_case()
    with pytest.raises(PresentationMapValidationError, match="decoded frame"):
        domain.map_video_tick_bounds(14)
    with pytest.raises(PresentationMapValidationError, match="decoded sample"):
        domain.require_av_span_covered(TickRange(0, 60), TickRange(0, 31))
    with pytest.raises(PresentationMapValidationError, match="decoded frame"):
        domain.require_av_span_covered(TickRange(1, 60), TickRange(0, 32))


def test_decoded_source_end_can_be_an_out_sentinel_not_a_regular_frame():
    domain = _numerical_case(frame_ticks=(0, 15, 60, 120))
    assert domain.map_video_tick_bounds(180) == (96, 96)
    assert domain.require_av_span_covered(TickRange(0, 180), TickRange(0, 96)) == 0
    with pytest.raises(PresentationMapValidationError, match="decoded frame"):
        domain.require_av_span_covered(TickRange(180, 181), TickRange(0, 96))


@pytest.mark.parametrize("tick", [True, 15.0, "15", None])
def test_boundary_mapping_never_coerces_tick_types(tick):
    with pytest.raises(MediaValidationError):
        _numerical_case().map_video_tick_bounds(tick)
