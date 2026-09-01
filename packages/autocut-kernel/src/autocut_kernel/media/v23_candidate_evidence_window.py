"""Compile current V23/V4 semantic candidates into bounded physical evidence windows.

The compatibility lane deliberately ignores candidate semantic context and the
candidate's own potentially full-window support.  Only anchor, supporting and
payoff Event supports may seed physical evidence extraction.  This module does
not mint physical edit endpoints; it only emits a frame-fenced extraction
request or a fail-closed routing decision.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from enum import Enum

from ..vlm.semantic_pack_v4 import (
    VlmCandidateHypothesisV4,
    VlmEventV4,
    VlmSemanticPackV4,
)
from ..vlm.window import WindowManifest
from .root_evidence import CanonicalEvidence, FramePtsIndexSet
from .timed_evidence import (
    CandidateEvidenceWindow,
    TimedEvidenceValidationError,
)
from .types import (
    MediaValidationError,
    TickRange,
    TimeBase,
    canonical_sha256,
    require_pts,
    sha256_prefixed,
)


class V23CandidateWindowCompileOutcome(str, Enum):
    """Whether one semantic candidate may enter candidate-local preflight."""

    ELIGIBLE = "eligible"
    EPISODE_ARC = "episode_arc"
    INDETERMINATE = "indeterminate"


class V23CandidateWindowCompileReason(str, Enum):
    """Closed reason codes for deterministic routing and release diagnostics."""

    BOUNDED_DIRECT_EVENTS = "bounded_direct_events"
    DISCONNECTED_DIRECT_EVENT_REGIONS = "disconnected_direct_event_regions"
    UNCERTAINTY_HULL_EXCEEDS_DURATION = "uncertainty_hull_exceeds_duration"
    UNCERTAINTY_HULL_EXCEEDS_SOURCE_RATIO = "uncertainty_hull_exceeds_source_ratio"
    PHYSICAL_WINDOW_EXCEEDS_DURATION = "physical_window_exceeds_duration"
    PHYSICAL_WINDOW_EXCEEDS_SOURCE_RATIO = "physical_window_exceeds_source_ratio"
    FRAME_INDEX_CANNOT_COVER_DIRECT_EVENT_HULL = "frame_index_cannot_cover_direct_event_hull"


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise TimedEvidenceValidationError(f"{field_name} must be non-empty text")
    return value


def _tick(value: object, field_name: str, *, positive: bool = False) -> int:
    try:
        result = require_pts(value, field_name)
    except MediaValidationError as error:
        raise TimedEvidenceValidationError(str(error)) from error
    if result < 0 or (positive and result <= 0):
        qualifier = "positive" if positive else "non-negative"
        raise TimedEvidenceValidationError(f"{field_name} must be {qualifier}")
    return result


def _sha(value: object, field_name: str) -> str:
    try:
        return sha256_prefixed(value, field_name)
    except MediaValidationError as error:
        raise TimedEvidenceValidationError(str(error)) from error


@dataclass(frozen=True, slots=True)
class V23CandidateWindowCompilePolicy(CanonicalEvidence):
    """Frozen locality policy; integer PTS and ppm avoid runtime float choices."""

    strategy_version: str
    time_base: TimeBase
    initial_left_expansion_pts: int
    initial_right_expansion_pts: int
    max_direct_event_gap_pts: int
    max_seed_duration_pts: int
    max_source_coverage_ppm: int

    def __post_init__(self) -> None:
        _text(self.strategy_version, "policy.strategy_version")
        if type(self.time_base) is not TimeBase:  # noqa: E721
            raise TimedEvidenceValidationError("policy.time_base must be a TimeBase")
        _tick(self.initial_left_expansion_pts, "policy.initial_left_expansion_pts")
        _tick(self.initial_right_expansion_pts, "policy.initial_right_expansion_pts")
        _tick(self.max_direct_event_gap_pts, "policy.max_direct_event_gap_pts")
        _tick(self.max_seed_duration_pts, "policy.max_seed_duration_pts", positive=True)
        ratio = _tick(
            self.max_source_coverage_ppm,
            "policy.max_source_coverage_ppm",
            positive=True,
        )
        if ratio > 1_000_000:
            raise TimedEvidenceValidationError(
                "policy.max_source_coverage_ppm must not exceed 1000000"
            )


@dataclass(frozen=True, slots=True)
class V23DirectEventSupport(CanonicalEvidence):
    """One direct semantic Event and its conservative Source-clock support."""

    event_ref: str
    coarse_range: TickRange
    mapping_error_bound_source_pts: int
    uncertainty_expanded_range: TickRange

    def __post_init__(self) -> None:
        _sha(self.event_ref, "direct_support.event_ref")
        if type(self.coarse_range) is not TickRange:  # noqa: E721
            raise TimedEvidenceValidationError("direct_support.coarse_range must be a TickRange")
        _tick(
            self.mapping_error_bound_source_pts,
            "direct_support.mapping_error_bound_source_pts",
        )
        if type(self.uncertainty_expanded_range) is not TickRange:  # noqa: E721
            raise TimedEvidenceValidationError(
                "direct_support.uncertainty_expanded_range must be a TickRange"
            )
        if not self.uncertainty_expanded_range.contains(self.coarse_range):
            raise TimedEvidenceValidationError(
                "direct support uncertainty range must contain its coarse range"
            )


@dataclass(frozen=True, slots=True)
class V23CandidateWindowCompileDecision(CanonicalEvidence):
    """Auditable P1A result; only eligible decisions contain an extraction window."""

    policy_sha256: str
    semantic_pack_sha256: str
    direct_support_dependency_sha256: str
    vlm_candidate_sha256: str
    candidate_id: str
    vlm_request_identity_sha256: str
    source_id: str
    source_sha256: str
    source_clock_id: str
    source_time_base: TimeBase
    window_manifest_sha256: str
    frame_pts_index_set_sha256: str
    source_range: TickRange
    direct_event_supports: tuple[V23DirectEventSupport, ...]
    direct_event_hull: TickRange
    merged_uncertainty_regions: tuple[TickRange, ...]
    outcome: V23CandidateWindowCompileOutcome
    reason: V23CandidateWindowCompileReason
    window: CandidateEvidenceWindow | None

    def __post_init__(self) -> None:
        for field_name in (
            "policy_sha256",
            "semantic_pack_sha256",
            "direct_support_dependency_sha256",
            "vlm_candidate_sha256",
            "candidate_id",
            "vlm_request_identity_sha256",
            "source_sha256",
            "window_manifest_sha256",
            "frame_pts_index_set_sha256",
        ):
            _sha(getattr(self, field_name), f"decision.{field_name}")
        _text(self.source_id, "decision.source_id")
        _text(self.source_clock_id, "decision.source_clock_id")
        if type(self.source_time_base) is not TimeBase:  # noqa: E721
            raise TimedEvidenceValidationError("decision.source_time_base must be a TimeBase")
        if type(self.source_range) is not TickRange:  # noqa: E721
            raise TimedEvidenceValidationError("decision.source_range must be a TickRange")
        supports = tuple(self.direct_event_supports)
        if not supports or any(type(item) is not V23DirectEventSupport for item in supports):  # noqa: E721
            raise TimedEvidenceValidationError(
                "decision.direct_event_supports must contain direct Event support"
            )
        object.__setattr__(self, "direct_event_supports", supports)
        refs = tuple(item.event_ref for item in supports)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise TimedEvidenceValidationError(
                "decision direct Event supports must be sorted and unique"
            )
        if any(
            not self.source_range.contains(item.coarse_range)
            or not self.source_range.contains(item.uncertainty_expanded_range)
            for item in supports
        ):
            raise TimedEvidenceValidationError(
                "decision direct Event supports must stay in the Source range"
            )
        if type(self.direct_event_hull) is not TickRange:  # noqa: E721
            raise TimedEvidenceValidationError("decision.direct_event_hull must be a TickRange")
        expected_hull = TickRange(
            min(item.coarse_range.start_pts for item in supports),
            max(item.coarse_range.end_pts for item in supports),
        )
        if self.direct_event_hull != expected_hull:
            raise TimedEvidenceValidationError(
                "decision direct Event hull must equal the hull of its supports"
            )
        if not self.source_range.contains(self.direct_event_hull):
            raise TimedEvidenceValidationError(
                "decision direct Event hull must stay in the Source range"
            )
        regions = tuple(self.merged_uncertainty_regions)
        if not regions or any(type(item) is not TickRange for item in regions):  # noqa: E721
            raise TimedEvidenceValidationError(
                "decision.merged_uncertainty_regions must contain TickRange values"
            )
        object.__setattr__(self, "merged_uncertainty_regions", regions)
        if any(left.end_pts >= right.start_pts for left, right in zip(regions, regions[1:])):
            raise TimedEvidenceValidationError(
                "decision uncertainty regions must be sorted and disjoint"
            )
        if any(not self.source_range.contains(item) for item in regions):
            raise TimedEvidenceValidationError(
                "decision uncertainty regions must stay in the Source range"
            )
        if type(self.outcome) is not V23CandidateWindowCompileOutcome:  # noqa: E721
            raise TimedEvidenceValidationError("decision.outcome is not registered")
        if type(self.reason) is not V23CandidateWindowCompileReason:  # noqa: E721
            raise TimedEvidenceValidationError("decision.reason is not registered")
        eligible = self.outcome is V23CandidateWindowCompileOutcome.ELIGIBLE
        if eligible != (type(self.window) is CandidateEvidenceWindow):
            raise TimedEvidenceValidationError(
                "only an eligible decision may contain a CandidateEvidenceWindow"
            )
        if eligible and self.reason is not V23CandidateWindowCompileReason.BOUNDED_DIRECT_EVENTS:
            raise TimedEvidenceValidationError("eligible decision must use bounded_direct_events")
        if (
            self.outcome is V23CandidateWindowCompileOutcome.EPISODE_ARC
            and self.reason is not V23CandidateWindowCompileReason.DISCONNECTED_DIRECT_EVENT_REGIONS
        ):
            raise TimedEvidenceValidationError(
                "episode_arc requires disconnected direct Event regions"
            )
        if self.outcome is V23CandidateWindowCompileOutcome.INDETERMINATE and self.reason not in {
            V23CandidateWindowCompileReason.UNCERTAINTY_HULL_EXCEEDS_DURATION,
            V23CandidateWindowCompileReason.UNCERTAINTY_HULL_EXCEEDS_SOURCE_RATIO,
            V23CandidateWindowCompileReason.PHYSICAL_WINDOW_EXCEEDS_DURATION,
            V23CandidateWindowCompileReason.PHYSICAL_WINDOW_EXCEEDS_SOURCE_RATIO,
            V23CandidateWindowCompileReason.FRAME_INDEX_CANNOT_COVER_DIRECT_EVENT_HULL,
        }:
            raise TimedEvidenceValidationError(
                "indeterminate decision requires a registered locality limit reason"
            )
        if eligible:
            assert self.window is not None
            if (
                self.window.source_id != self.source_id
                or self.window.source_sha256 != self.source_sha256
                or self.window.source_clock_id != self.source_clock_id
                or self.window.source_time_base != self.source_time_base
                or self.window.source_range != self.source_range
                or self.window.vlm_candidate_sha256 != self.vlm_candidate_sha256
                or self.window.vlm_request_identity_sha256 != self.vlm_request_identity_sha256
                or self.window.window_manifest_sha256 != self.window_manifest_sha256
                or self.window.frame_pts_index_set_sha256 != self.frame_pts_index_set_sha256
                or self.window.coarse_range != self.direct_event_hull
                or self.window.expansion_ordinal != 0
            ):
                raise TimedEvidenceValidationError(
                    "eligible window does not bind the exact compile decision"
                )


def _source_range(
    window_manifest: WindowManifest,
    frame_pts_index: FramePtsIndexSet,
) -> TickRange:
    if type(window_manifest) is not WindowManifest:  # noqa: E721
        raise TimedEvidenceValidationError("window_manifest must be a WindowManifest")
    if type(frame_pts_index) is not FramePtsIndexSet:  # noqa: E721
        raise TimedEvidenceValidationError("frame_pts_index must be a FramePtsIndexSet")
    if window_manifest.frame_pts_index_set_sha256 != frame_pts_index.canonical_hash:
        raise TimedEvidenceValidationError(
            "window manifest does not bind the supplied frame PTS index"
        )
    context = frame_pts_index.context
    if (
        context.source_id != window_manifest.source_id
        or context.source_sha256 != window_manifest.source_sha256
        or context.clock_id != window_manifest.source_clock_id
        or context.time_base != window_manifest.source_time_base
    ):
        raise TimedEvidenceValidationError(
            "frame PTS source clock does not match the window manifest"
        )
    result = TickRange(context.origin_tick, context.end_tick)
    if not result.contains(window_manifest.source_range):
        raise TimedEvidenceValidationError(
            "VLM window range must stay within the full source extent"
        )
    return result


def _merge_regions(
    ranges: tuple[TickRange, ...],
    max_gap: int,
) -> tuple[TickRange, ...]:
    ordered = tuple(sorted(ranges, key=lambda item: (item.start_pts, item.end_pts)))
    merged: list[TickRange] = []
    for item in ordered:
        if not merged or item.start_pts - merged[-1].end_pts > max_gap:
            merged.append(item)
            continue
        previous = merged[-1]
        merged[-1] = TickRange(previous.start_pts, max(previous.end_pts, item.end_pts))
    return tuple(merged)


def _snap_outward(
    frame_pts_index: FramePtsIndexSet,
    source_range: TickRange,
    desired_start: int,
    desired_end: int,
) -> TickRange | None:
    """Snap to the complete decoded PTS lattice in logarithmic time.

    A large gap between adjacent PTS values is not missing coverage: with a
    complete FramePtsIndexSet it is the presentation duration of the earlier
    decoded frame.  Only an unusable lattice or failure to enclose the direct
    semantic hull is indeterminate.
    """

    ticks = frame_pts_index.pts_index.ticks
    lower = bisect_left(ticks, source_range.start_pts)
    upper = bisect_left(ticks, source_range.end_pts)
    if lower >= upper:
        return None
    start_position = bisect_right(ticks, desired_start, lower, upper) - 1
    start = ticks[start_position] if start_position >= lower else ticks[lower]
    end_position = bisect_left(ticks, desired_end, lower, upper)
    end = ticks[end_position] if end_position < upper else source_range.end_pts
    if start >= end:
        later_position = bisect_right(ticks, start, lower, upper)
        end = ticks[later_position] if later_position < upper else source_range.end_pts
    if start >= end:
        return None
    return TickRange(start, end)


def _direct_support(
    event: VlmEventV4,
    source_range: TickRange,
    window_manifest: WindowManifest,
) -> V23DirectEventSupport:
    support_manifest = event.support.manifest
    if (
        support_manifest.canonical_hash != window_manifest.canonical_hash
        or support_manifest.source_id != window_manifest.source_id
        or support_manifest.source_sha256 != window_manifest.source_sha256
        or support_manifest.source_clock_id != window_manifest.source_clock_id
        or support_manifest.source_time_base != window_manifest.source_time_base
    ):
        raise TimedEvidenceValidationError(
            "direct Event support does not bind the supplied Source clock"
        )
    interval = event.support.source_interval
    if interval.source_time_base != window_manifest.source_time_base:
        raise TimedEvidenceValidationError(
            "direct Event support and window manifest clocks disagree"
        )
    coarse = interval.coarse_range
    if not window_manifest.source_range.contains(coarse):
        raise TimedEvidenceValidationError("direct Event coarse range is outside its VLM window")
    error = interval.mapping_error_bound_source_pts
    expanded = TickRange(
        max(source_range.start_pts, coarse.start_pts - error),
        min(source_range.end_pts, coarse.end_pts + error),
    )
    return V23DirectEventSupport(
        event_ref=event.event_id,
        coarse_range=coarse,
        mapping_error_bound_source_pts=error,
        uncertainty_expanded_range=expanded,
    )


def compile_v23_candidate_evidence_window(
    candidate: VlmCandidateHypothesisV4,
    semantic_pack: VlmSemanticPackV4,
    window_manifest: WindowManifest,
    frame_pts_index: FramePtsIndexSet,
    policy: V23CandidateWindowCompilePolicy,
) -> V23CandidateWindowCompileDecision:
    """Project direct Event semantics into one bounded, frame-fenced request."""

    if type(candidate) is not VlmCandidateHypothesisV4:  # noqa: E721
        raise TimedEvidenceValidationError("candidate must be a VlmCandidateHypothesisV4")
    if type(semantic_pack) is not VlmSemanticPackV4:  # noqa: E721
        raise TimedEvidenceValidationError("semantic_pack must be a VlmSemanticPackV4")
    if type(policy) is not V23CandidateWindowCompilePolicy:  # noqa: E721
        raise TimedEvidenceValidationError("policy must be a V23CandidateWindowCompilePolicy")
    if semantic_pack.window_manifest_sha256 != window_manifest.canonical_hash:
        raise TimedEvidenceValidationError(
            "semantic pack does not bind the supplied window manifest"
        )
    if candidate not in semantic_pack.candidate_hypotheses:
        raise TimedEvidenceValidationError(
            "candidate is not a member of the supplied semantic pack"
        )
    if policy.time_base != window_manifest.source_time_base:
        raise TimedEvidenceValidationError("policy time base does not match the video clock")
    source_range = _source_range(window_manifest, frame_pts_index)
    events_by_id = {event.event_id: event for event in semantic_pack.events}
    direct_refs = tuple(
        sorted(
            {
                candidate.anchor_event_ref,
                *candidate.supporting_event_refs,
                *candidate.payoff_event_refs,
            }
        )
    )
    if not direct_refs or any(event_ref not in events_by_id for event_ref in direct_refs):
        raise TimedEvidenceValidationError("candidate direct Event reference is not closed")
    direct_events = tuple(events_by_id[event_ref] for event_ref in direct_refs)
    supports = tuple(
        _direct_support(event, source_range, window_manifest) for event in direct_events
    )
    direct_hull = TickRange(
        min(item.coarse_range.start_pts for item in supports),
        max(item.coarse_range.end_pts for item in supports),
    )
    merged = _merge_regions(
        tuple(item.uncertainty_expanded_range for item in supports),
        policy.max_direct_event_gap_pts,
    )
    candidate_hash = canonical_sha256(candidate.to_mapping())
    dependency_hash = canonical_sha256(
        {
            "candidate_roles": {
                "anchor_event_ref": candidate.anchor_event_ref,
                "supporting_event_refs": list(candidate.supporting_event_refs),
                "payoff_event_refs": list(candidate.payoff_event_refs),
            },
            "events": [event.to_mapping() for event in direct_events],
        }
    )

    outcome = V23CandidateWindowCompileOutcome.ELIGIBLE
    reason = V23CandidateWindowCompileReason.BOUNDED_DIRECT_EVENTS
    window: CandidateEvidenceWindow | None = None
    if len(merged) > 1:
        outcome = V23CandidateWindowCompileOutcome.EPISODE_ARC
        reason = V23CandidateWindowCompileReason.DISCONNECTED_DIRECT_EVENT_REGIONS
    else:
        uncertainty_hull = next(iter(merged))
        if uncertainty_hull.duration_pts > policy.max_seed_duration_pts:
            outcome = V23CandidateWindowCompileOutcome.INDETERMINATE
            reason = V23CandidateWindowCompileReason.UNCERTAINTY_HULL_EXCEEDS_DURATION
        elif (
            uncertainty_hull.duration_pts * 1_000_000
            > source_range.duration_pts * policy.max_source_coverage_ppm
        ):
            outcome = V23CandidateWindowCompileOutcome.INDETERMINATE
            reason = V23CandidateWindowCompileReason.UNCERTAINTY_HULL_EXCEEDS_SOURCE_RATIO
        else:
            desired_start = max(
                source_range.start_pts,
                uncertainty_hull.start_pts - policy.initial_left_expansion_pts,
            )
            desired_end = min(
                source_range.end_pts,
                uncertainty_hull.end_pts + policy.initial_right_expansion_pts,
            )
            current_range = _snap_outward(
                frame_pts_index,
                source_range,
                desired_start,
                desired_end,
            )
            if current_range is None or not current_range.contains(direct_hull):
                outcome = V23CandidateWindowCompileOutcome.INDETERMINATE
                reason = V23CandidateWindowCompileReason.FRAME_INDEX_CANNOT_COVER_DIRECT_EVENT_HULL
            elif current_range.duration_pts > policy.max_seed_duration_pts:
                outcome = V23CandidateWindowCompileOutcome.INDETERMINATE
                reason = V23CandidateWindowCompileReason.PHYSICAL_WINDOW_EXCEEDS_DURATION
            elif (
                current_range.duration_pts * 1_000_000
                > source_range.duration_pts * policy.max_source_coverage_ppm
            ):
                outcome = V23CandidateWindowCompileOutcome.INDETERMINATE
                reason = V23CandidateWindowCompileReason.PHYSICAL_WINDOW_EXCEEDS_SOURCE_RATIO
            else:
                window = CandidateEvidenceWindow(
                    source_id=window_manifest.source_id,
                    source_sha256=window_manifest.source_sha256,
                    source_clock_id=window_manifest.source_clock_id,
                    source_time_base=window_manifest.source_time_base,
                    source_range=source_range,
                    vlm_candidate_sha256=candidate_hash,
                    vlm_request_identity_sha256=semantic_pack.request_identity_sha256,
                    window_manifest_sha256=window_manifest.canonical_hash,
                    frame_pts_index_set_sha256=frame_pts_index.canonical_hash,
                    coarse_range=direct_hull,
                    current_range=current_range,
                    expansion_ordinal=0,
                )

    return V23CandidateWindowCompileDecision(
        policy_sha256=policy.canonical_hash,
        semantic_pack_sha256=semantic_pack.canonical_hash,
        direct_support_dependency_sha256=dependency_hash,
        vlm_candidate_sha256=candidate_hash,
        # V4 global candidate IDs are Kernel-derived sha256 identities.
        candidate_id=candidate.candidate_id,
        vlm_request_identity_sha256=semantic_pack.request_identity_sha256,
        source_id=window_manifest.source_id,
        source_sha256=window_manifest.source_sha256,
        source_clock_id=window_manifest.source_clock_id,
        source_time_base=window_manifest.source_time_base,
        window_manifest_sha256=window_manifest.canonical_hash,
        frame_pts_index_set_sha256=frame_pts_index.canonical_hash,
        source_range=source_range,
        direct_event_supports=supports,
        direct_event_hull=direct_hull,
        merged_uncertainty_regions=merged,
        outcome=outcome,
        reason=reason,
        window=window,
    )


def verify_v23_candidate_evidence_window_decision(
    decision: V23CandidateWindowCompileDecision,
    candidate: VlmCandidateHypothesisV4,
    semantic_pack: VlmSemanticPackV4,
    window_manifest: WindowManifest,
    frame_pts_index: FramePtsIndexSet,
    policy: V23CandidateWindowCompilePolicy,
) -> V23CandidateWindowCompileDecision:
    """Independently recompute the pure decision from the immutable dependencies."""

    if type(decision) is not V23CandidateWindowCompileDecision:  # noqa: E721
        raise TimedEvidenceValidationError("decision must be a V23CandidateWindowCompileDecision")
    expected = compile_v23_candidate_evidence_window(
        candidate,
        semantic_pack,
        window_manifest,
        frame_pts_index,
        policy,
    )
    if decision != expected:
        raise TimedEvidenceValidationError(
            "candidate window compile decision does not match independent recomputation"
        )
    return decision
