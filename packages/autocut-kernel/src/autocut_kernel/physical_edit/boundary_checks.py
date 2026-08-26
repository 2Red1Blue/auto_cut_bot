"""Shared deterministic physical endpoint predicates, with no admission claim."""

from __future__ import annotations

from ..media.root_evidence import RootMediaEvidenceBundle, VisualClassification


def visual_stable(
    evidence: RootMediaEvidenceBundle, endpoint: int, *, is_out: bool, width: int,
) -> bool:
    context = evidence.visual_validity.context
    start, end = (endpoint - width, endpoint) if is_out else (endpoint, endpoint + width)
    if start < context.origin_tick or end > context.end_tick:
        return False
    cursor = start
    for interval in evidence.visual_validity.intervals:
        overlap_start = max(start, interval.in_tick)
        overlap_end = min(end, interval.out_tick)
        if overlap_start >= overlap_end:
            continue
        if overlap_start != cursor or interval.classification is not VisualClassification.VALID_CONTENT:
            return False
        cursor = overlap_end
    return cursor == end


def subtitle_clear(evidence: RootMediaEvidenceBundle, endpoint: int, *, clearance: int) -> bool:
    return not any(
        cue.in_tick - cue.timing_error_bound.in_tick - clearance
        < endpoint
        < cue.out_tick + cue.timing_error_bound.out_tick + clearance
        for cue in evidence.subtitle_cues.cues
    )


def shot_stable(
    evidence: RootMediaEvidenceBundle, endpoint: int, *, is_out: bool, width: int,
) -> bool:
    """A stable endpoint neighborhood must not cross a known shot boundary."""
    start, end = (endpoint - width, endpoint) if is_out else (endpoint, endpoint + width)
    context = evidence.shot_boundaries.context
    return (
        context.origin_tick <= start < end <= context.end_tick
        and not any(start < point.tick < end for point in evidence.shot_boundaries.points)
    )
