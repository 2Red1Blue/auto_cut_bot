"""Conservative duration lower bounds for Stage 2 semantic candidates.

These bounds preserve timing uncertainty; they are explicitly not edit
endpoints, cut feasibility, or a story-duration assertion.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import gcd
from typing import cast

from ..media.types import TickRange, TimeBase
from ..vlm.models import VlmSemanticSupport
from ..vlm.semantic_support_v4 import _ObservationSupportV4
from ..vlm.window import ProxyTimelineMap, ProxyTimelineSegment

_SUPPORTED_SUPPORT_TYPES: tuple[type, ...] = (VlmSemanticSupport, _ObservationSupportV4)


class CandidateDurationError(ValueError):
    """A candidate support cannot be represented as a closed duration bound."""


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value < 2**53:  # noqa: E721
        raise CandidateDurationError(f"{label} must be an exact JSON-safe integer")
    return value


@dataclass(frozen=True, slots=True)
class ConservativeDuration:
    """A non-negative reduced rational number of seconds."""

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        numerator = _integer(self.numerator, "duration.numerator")
        denominator = _integer(self.denominator, "duration.denominator", minimum=1)
        if (numerator == 0 and denominator != 1) or (numerator and gcd(numerator, denominator) != 1):
            raise CandidateDurationError("duration fraction must be reduced")

    @property
    def fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def to_mapping(self) -> dict[str, int]:
        return {"numerator": self.numerator, "denominator": self.denominator}

    @classmethod
    def from_mapping(cls, value: object) -> ConservativeDuration:
        if type(value) is not dict:  # noqa: E721
            raise CandidateDurationError("duration must be a closed mapping")
        item = cast(dict[str, object], value)
        if set(item) != {"numerator", "denominator"}:
            raise CandidateDurationError("duration must be a closed mapping")
        return cls(_integer(item["numerator"], "duration.numerator"), _integer(
            item["denominator"], "duration.denominator", minimum=1
        ))


def _seconds(ticks: int, time_base: TimeBase) -> Fraction:
    return Fraction(ticks * time_base.numerator, time_base.denominator)


def conservative_support_duration(
    support: VlmSemanticSupport,
    timeline_map: ProxyTimelineMap,
) -> ConservativeDuration:
    """Derive an uncertainty-shrunk lower bound from original support channels.

    The persisted mapped Source range is an outer envelope and must not be
    subtracted as if its errors were independent. Scan every segment touched by
    either endpoint's uncertainty domain, then choose the latest possible start
    and earliest possible end across all segment mapping errors. Looking only
    at a shrunken endpoint can miss a neighbouring segment's larger error.
    """
    if not isinstance(support, _SUPPORTED_SUPPORT_TYPES) or type(timeline_map) is not ProxyTimelineMap:  # noqa: E721
        raise CandidateDurationError("support must be an exact VlmSemanticSupport or V4 observation support")
    latest_start, earliest_end = conservative_support_bounds(support, timeline_map)
    lower = _seconds(max(0, earliest_end - latest_start), timeline_map.source_time_base)
    return ConservativeDuration(lower.numerator, lower.denominator)


def conservative_support_bounds(
    support: VlmSemanticSupport,
    timeline_map: ProxyTimelineMap,
) -> tuple[int, int]:
    """Return conservative inner Source bounds, without creating edit endpoints.

    The pair is useful to later semantic-material checks only.  It may be
    reversed or equal, which denotes zero usable coarse support; callers must
    never repair it into a physical range.
    """
    if not isinstance(support, _SUPPORTED_SUPPORT_TYPES) or type(timeline_map) is not ProxyTimelineMap:  # noqa: E721
        raise CandidateDurationError("support must be an exact VlmSemanticSupport or V4 observation support")
    proxy = support.proxy_interval
    if type(proxy.proxy_range) is not TickRange:
        raise CandidateDurationError("support proxy range is invalid")
    if (
        timeline_map.proxy_time_base != support.source_interval.proxy_time_base
        or timeline_map.source_time_base != support.source_interval.source_time_base
    ):
        raise CandidateDurationError("support and timeline map time bases differ")
    try:
        expected_outer = timeline_map.map_interval(
            proxy.proxy_range, provider_uncertainty_proxy_pts=proxy.uncertainty_pts
        )
    except ValueError as error:
        raise CandidateDurationError("support proxy range is outside its timeline map") from error
    if expected_outer != support.source_interval:
        raise CandidateDurationError("support mapping differs from the exact timeline map")
    start_domain = (
        max(timeline_map.proxy_range.start_pts, proxy.proxy_range.start_pts - proxy.uncertainty_pts),
        min(timeline_map.proxy_range.end_pts, proxy.proxy_range.start_pts + proxy.uncertainty_pts),
    )
    end_domain = (
        max(timeline_map.proxy_range.start_pts, proxy.proxy_range.end_pts - proxy.uncertainty_pts),
        min(timeline_map.proxy_range.end_pts, proxy.proxy_range.end_pts + proxy.uncertainty_pts),
    )
    start_values = _boundary_values(timeline_map, *start_domain, latest=True)
    end_values = _boundary_values(timeline_map, *end_domain, latest=False)
    if not start_values or not end_values:
        raise CandidateDurationError("support uncertainty domain is outside the timeline map")
    return (
        max(timeline_map.source_range.start_pts, max(start_values)),
        min(timeline_map.source_range.end_pts, min(end_values)),
    )


def _boundary_values(
    timeline_map: ProxyTimelineMap,
    domain_start: int,
    domain_end: int,
    *,
    latest: bool,
) -> tuple[int, ...]:
    """Map an uncertainty domain through every intersecting affine segment.

    Closed intersection at a segment boundary intentionally includes both
    neighbouring error terms.  That over-approximates uncertainty and keeps
    the resulting duration lower bound conservative.
    """
    values: list[int] = []
    for segment in timeline_map.segments:
        left = max(domain_start, segment.proxy_range.start_pts)
        right = min(domain_end, segment.proxy_range.end_pts)
        if left > right:
            continue
        if latest:
            values.append(_ceil_map(segment, right) + segment.max_source_error_pts)
        else:
            values.append(_floor_map(segment, left) - segment.max_source_error_pts)
    return tuple(values)


def _floor_map(segment: ProxyTimelineSegment, point: int) -> int:
    relative = point - segment.proxy_range.start_pts
    return segment.source_range.start_pts + relative * segment.source_range.duration_pts // segment.proxy_range.duration_pts


def _ceil_map(segment: ProxyTimelineSegment, point: int) -> int:
    relative = point - segment.proxy_range.start_pts
    numerator = relative * segment.source_range.duration_pts
    denominator = segment.proxy_range.duration_pts
    return segment.source_range.start_pts + (numerator + denominator - 1) // denominator
