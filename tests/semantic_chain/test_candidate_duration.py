"""Candidate coarse-support bounds are rational and never physical endpoints."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.semantic_chain.candidate_duration import (
    CandidateDurationError,
    ConservativeDuration,
    conservative_support_bounds,
    conservative_support_duration,
)
from autocut_kernel.vlm.models import VlmProxyInterval, VlmSemanticSupport
from autocut_kernel.vlm.window import ProxyTimelineMap, ProxyTimelineSegment


def _support(timeline: ProxyTimelineMap, *, uncertainty: int = 2) -> VlmSemanticSupport:
    proxy = VlmProxyInterval(TickRange(20, 80), uncertainty)
    return VlmSemanticSupport(
        proxy, ("sha256:" + "a" * 64,), Decimal("0.9"),
        timeline.map_interval(proxy.proxy_range, provider_uncertainty_proxy_pts=uncertainty),
        "sha256:" + "b" * 64,
    )


def test_piecewise_timeline_scans_all_uncertainty_touched_segments():
    base = TimeBase(1, 1_000)
    timeline = ProxyTimelineMap(
        base, base,
        (
            ProxyTimelineSegment(TickRange(0, 50), TickRange(1_000, 1_100), 1),
            ProxyTimelineSegment(TickRange(50, 100), TickRange(1_100, 1_150), 9),
        ),
        "piecewise_monotonic",
    )
    support = _support(timeline, uncertainty=35)
    start, end = conservative_support_bounds(support, timeline)
    duration = conservative_support_duration(support, timeline)
    assert start >= timeline.source_range.start_pts
    assert end <= timeline.source_range.end_pts
    assert duration.fraction >= 0
    assert duration.fraction == Fraction(max(0, end - start) * base.numerator, base.denominator)


def test_more_uncertainty_never_increases_conservative_duration():
    base = TimeBase(1, 1_000)
    timeline = ProxyTimelineMap.translation(
        time_base=base, proxy_range=TickRange(0, 100), source_start_pts=1_000, max_source_error_pts=2,
    )
    assert conservative_support_duration(_support(timeline, uncertainty=8), timeline).fraction <= (
        conservative_support_duration(_support(timeline, uncertainty=1), timeline).fraction
    )


def test_zero_bound_and_noncanonical_fraction_are_not_repaired():
    base = TimeBase(1, 1_000)
    timeline = ProxyTimelineMap.translation(
        time_base=base, proxy_range=TickRange(0, 100), source_start_pts=1_000,
    )
    support = _support(timeline, uncertainty=40)
    assert conservative_support_duration(support, timeline) == ConservativeDuration(0, 1)
    with pytest.raises(CandidateDurationError):
        ConservativeDuration(0, 2)
    with pytest.raises(CandidateDurationError):
        ConservativeDuration(2, 4)


@pytest.mark.parametrize("numerator,denominator", [(2**53, 1), (1, 2**53), (True, 1), (1, True), (-1, 1), (1, 0)])
def test_duration_is_json_safe_before_hashing(numerator, denominator):
    with pytest.raises(CandidateDurationError):
        ConservativeDuration(numerator, denominator)
    with pytest.raises(CandidateDurationError):
        ConservativeDuration.from_mapping({"numerator": numerator, "denominator": denominator})


def test_error_drop_across_boundary_never_makes_uncertainty_more_usable():
    base = TimeBase(1, 1)
    timeline = ProxyTimelineMap(base, base, (
        ProxyTimelineSegment(TickRange(0, 10), TickRange(0, 10), 8),
        ProxyTimelineSegment(TickRange(10, 30), TickRange(10, 30), 0),
    ), "piecewise_monotonic")
    durations = []
    for uncertainty in range(12):
        proxy = VlmProxyInterval(TickRange(8, 25), uncertainty)
        support = VlmSemanticSupport(proxy, ("sha256:" + "a" * 64,), Decimal("0.9"),
                                     timeline.map_interval(proxy.proxy_range, provider_uncertainty_proxy_pts=uncertainty), "sha256:" + "b" * 64)
        durations.append(conservative_support_duration(support, timeline).fraction)
    assert durations[0] == 9 and durations[3] == 4
    assert durations == sorted(durations, reverse=True)


def test_different_clock_slope_negative_origin_and_boundary_use_source_seconds():
    timeline = ProxyTimelineMap(TimeBase(1, 1000), TimeBase(1, 90000), (
        ProxyTimelineSegment(TickRange(0, 50), TickRange(-9000, 0), 1),
        ProxyTimelineSegment(TickRange(50, 100), TickRange(0, 4500), 10),
    ), "piecewise_monotonic")
    support = _support(timeline, uncertainty=0)
    # f(20)=-5400, f(80)=2700, endpoint errors 1 and 10.
    assert conservative_support_bounds(support, timeline) == (-5399, 2690)
    assert conservative_support_duration(support, timeline).fraction == Fraction(8089, 90000)
