"""Complete candidate-local A/V selection over the original v2 source clocks.

This pure compiler does not authorize an editorial query or persist/admit a
Recipe. Its caller must read exact committed inputs; physical Admission must
independently replay the selection. No whole-episode ASR or synthetic root is
needed. Work-limit exhaustion never returns a partial optimum.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left, bisect_right, insort
from dataclasses import dataclass
from typing import Final

from ..media.root_evidence import AudioSourceOutcome, CoverageOutcome, RootMediaEvidenceBundle
from ..media.timed_evidence import CandidateEvidenceWindowPlan, CandidateTimedEvidenceSet
from ..media.types import TickRange, canonical_sha256, require_pts
from .boundary_checks import shot_stable, subtitle_clear, visual_stable
from .candidate_dialogue_guard import CandidateDialogueGuard, derive_candidate_dialogue_guard
from .candidate_timed_speech_authority import CandidateTimedSpeechAuthorityInput
from .exact_span import (
    BoundaryProof,
    CandidatePairLimitError,
    ExactAvSpanRequest,
    ExactSpanValidationError,
    NoLegalSpanError,
)
from .presentation_map import PresentationMapValidationError, ReplayedPresentationMap

_MAX_PORTABLE_COUNT: Final = 2**53 - 1
_MAX_RETAINED_VARIANTS: Final = 16


@dataclass(frozen=True, slots=True)
class CandidateExactSpanPolicy:
    """Explicit work and safety bounds; no default or runtime authority."""

    max_video_pair_visits: int
    max_av_pair_visits: int
    endpoint_stability_video_tick: int
    subtitle_clearance_floor_video_tick: int
    av_sync_tolerance_audio_tick: int

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = require_pts(getattr(self, name), name)
            if value < 0 or (name != "av_sync_tolerance_audio_tick" and value == 0):
                raise ExactSpanValidationError(f"invalid native span policy {name}")

    def to_mapping(self) -> dict[str, object]:
        return {"strategy": "candidate-local-exact-v1", **{
            name: getattr(self, name) for name in self.__dataclass_fields__
        }}

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class CandidateExactSpanResult:
    video_range: TickRange
    audio_range: TickRange
    boundary_proof: BoundaryProof
    dialogue_guard: CandidateDialogueGuard
    common_segment_ordinal: int
    canonical_decision_key: tuple[int, ...]
    logical_cartesian_count_decimal: str
    visited_av_pair_count: int
    feasible_count: int
    request_sha256: str
    policy_sha256: str
    candidate_domain_sha256: str
    feasible_relation_sha256: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "strategy": "candidate-local-exact-v1",
            "video_range": {"start_pts": self.video_range.start_pts, "end_pts": self.video_range.end_pts},
            "audio_range": {"start_pts": self.audio_range.start_pts, "end_pts": self.audio_range.end_pts},
            "boundary_proof": self.boundary_proof.to_mapping(),
            "dialogue_guard": self.dialogue_guard.to_mapping(),
            "common_segment_ordinal": self.common_segment_ordinal,
            "canonical_decision_key": list(self.canonical_decision_key),
            "logical_cartesian_count_decimal": self.logical_cartesian_count_decimal,
            "visited_av_pair_count": self.visited_av_pair_count,
            "feasible_count": self.feasible_count,
            "request_sha256": self.request_sha256,
            "policy_sha256": self.policy_sha256,
            "candidate_domain_sha256": self.candidate_domain_sha256,
            "feasible_relation_sha256": self.feasible_relation_sha256,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def _validate(
    request: ExactAvSpanRequest, root: RootMediaEvidenceBundle,
    candidate: CandidateTimedEvidenceSet, clock_map: ReplayedPresentationMap,
    policy: CandidateExactSpanPolicy,
) -> None:
    if (type(request) is not ExactAvSpanRequest or type(root) is not RootMediaEvidenceBundle  # noqa: E721
            or type(candidate) is not CandidateTimedEvidenceSet  # noqa: E721
            or type(clock_map) is not ReplayedPresentationMap  # noqa: E721
            or type(policy) is not CandidateExactSpanPolicy):  # noqa: E721
        raise ExactSpanValidationError("native compilation requires exact typed inputs")
    if clock_map.root != root:
        raise ExactSpanValidationError("native map must retain the original root")
    context = root.frame_pts_index.context
    bound = request.desired_video_range
    if (bound.source_id, bound.source_sha256, bound.clock_id, bound.time_base) != (
        context.source_id, context.source_sha256, context.clock_id, context.time_base,
    ):
        raise ExactSpanValidationError("query does not bind the source video clock")
    if not candidate.candidate_window.current_range.contains(bound.tick_range):
        raise ExactSpanValidationError("query exceeds candidate-local evidence window")
    if root.audio_sample_boundaries.source_outcome is not AudioSourceOutcome.BOUNDARIES_AVAILABLE:
        raise ExactSpanValidationError("native A/V compilation requires decoded audio samples")
    for evidence in (root.frame_pts_index, root.audio_sample_boundaries,
                     root.visual_validity, root.subtitle_cues, root.shot_boundaries):
        if evidence.coverage.outcome is not CoverageOutcome.COMPLETE:
            raise ExactSpanValidationError("physical root evidence coverage is incomplete")


def _endpoint_domains(
    ticks: tuple[int, ...], clock_map: ReplayedPresentationMap,
    guard: CandidateDialogueGuard, policy: CandidateExactSpanPolicy,
) -> tuple[tuple[int, tuple[int, ...]], ...]:
    points = clock_map.root.audio_sample_boundaries.points
    rows: list[tuple[int, tuple[int, ...]]] = []
    count = 0
    tolerance = policy.av_sync_tolerance_audio_tick
    coverage = guard.source_audio_range
    if coverage is None:
        raise ExactSpanValidationError("native A/V compilation requires local audio coverage")
    for tick in ticks:
        try:
            lower, upper = clock_map.map_video_tick_bounds(tick)
        except PresentationMapValidationError:
            rows.append((tick, ()))
            continue
        lower = max(lower - tolerance, coverage.start_pts)
        upper = min(upper + tolerance, coverage.end_pts)
        start = bisect_left(points, lower, key=lambda point: point.tick)
        stop = bisect_right(points, upper, key=lambda point: point.tick)
        count += max(0, stop - start)
        if count > policy.max_av_pair_visits:
            raise CandidatePairLimitError("aligned endpoint domain exceeds explicit work limit")
        rows.append((tick, tuple(point.tick for point in points[start:stop])))
    return tuple(rows)


def _safe_video(root: RootMediaEvidenceBundle, tick: int, *, out: bool, policy: CandidateExactSpanPolicy) -> bool:
    width = policy.endpoint_stability_video_tick
    return (
        visual_stable(root, tick, is_out=out, width=width)
        and shot_stable(root, tick, is_out=out, width=width)
        and subtitle_clear(root, tick, clearance=policy.subtitle_clearance_floor_video_tick)
    )


def _safe_audio(guard: CandidateDialogueGuard, tick: int) -> bool:
    return not any(item.in_tick < tick < item.out_tick for item in guard.protected_ranges)


def _key(request: ExactAvSpanRequest, clock_map: ReplayedPresentationMap,
         endpoints: tuple[int, int, int, int]) -> tuple[int, ...]:
    vin, vout, ain, aout = endpoints
    anchor = request.anchor_video_range.tick_range
    lower_in, upper_in = clock_map.map_video_tick_bounds(vin)
    lower_out, upper_out = clock_map.map_video_tick_bounds(vout)
    return (anchor.start_pts - vin, vout - anchor.end_pts,
            abs(ain - (lower_in + upper_in) // 2), abs(aout - (lower_out + upper_out) // 2),
            vout - vin, aout - ain, vin, vout, ain, aout)


def _validate_max_variants(max_variants: int) -> int:
    value = require_pts(max_variants, "max_variants")
    if not 1 <= value <= _MAX_RETAINED_VARIANTS:
        raise ExactSpanValidationError("max_variants must be between 1 and 16")
    return value


def _compile_candidate_av_span_variants(
    request: ExactAvSpanRequest, root: RootMediaEvidenceBundle,
    candidate: CandidateTimedEvidenceSet, plan: CandidateEvidenceWindowPlan,
    profile: CandidateTimedSpeechAuthorityInput, clock_map: ReplayedPresentationMap,
    policy: CandidateExactSpanPolicy, *, retained_limit: int,
) -> tuple[CandidateExactSpanResult, ...]:
    """Exhaust the relation while retaining only its canonical bounded prefix."""
    _validate(request, root, candidate, clock_map, policy)
    guard = derive_candidate_dialogue_guard(root, candidate, plan, profile, request.dialogue_requirement)
    desired = request.desired_video_range.tick_range
    anchor = request.anchor_video_range.tick_range
    index = root.frame_pts_index.pts_index
    video_starts = tuple(tick for tick in index.ticks_between(desired.start_pts, anchor.start_pts)
                         if tick < root.frame_pts_index.context.end_tick)
    video_ends = index.ticks_between(anchor.end_pts, desired.end_pts)
    source_end = root.frame_pts_index.context.end_tick
    if anchor.end_pts <= source_end <= desired.end_pts and source_end not in video_ends:
        video_ends = (*video_ends, source_end)
    if len(video_starts) * len(video_ends) > policy.max_video_pair_visits:
        raise CandidatePairLimitError("video pair domain exceeds explicit work limit")
    starts = _endpoint_domains(video_starts, clock_map, guard, policy)
    ends = _endpoint_domains(video_ends, clock_map, guard, policy)
    audio_starts = {tick for _, ticks in starts for tick in ticks}
    audio_ends = {tick for _, ticks in ends for tick in ticks}
    logical_count = len(starts) * len(ends) * len(audio_starts) * len(audio_ends)
    if logical_count > _MAX_PORTABLE_COUNT:
        raise CandidatePairLimitError(
            "logical candidate relation exceeds the portable exact-integer limit"
        )
    domain_hash = canonical_sha256({
        "strategy": "candidate-local-exact-v1", "starts": starts, "ends": ends,
        "clock_map_sha256": clock_map.certificate.canonical_hash,
        "guard_sha256": guard.canonical_hash,
    })
    # Hash the canonical JSON array in lexicographic endpoint order. Only a
    # fixed-size digest and the current canonical minimum survive enumeration.
    relation_hash = hashlib.sha256()
    relation_hash.update(b"[")
    feasible_count = visits = 0
    retained: list[tuple[tuple[int, ...], tuple[int, int, int, int], int]] = []
    safe_starts = {tick: _safe_video(root, tick, out=False, policy=policy) for tick, _ in starts}
    safe_ends = {tick: _safe_video(root, tick, out=True, policy=policy) for tick, _ in ends}
    for vin, in_samples in starts:
        for vout, out_samples in ends:
            if (vout - vin < request.minimum_video_duration_tick
                    or not safe_starts[vin] or not safe_ends[vout]):
                continue
            for ain in in_samples:
                for aout in out_samples:
                    visits += 1
                    if visits > policy.max_av_pair_visits:
                        raise CandidatePairLimitError("A/V pair search exhausted explicit work limit")
                    if ain >= aout or not _safe_audio(guard, ain) or not _safe_audio(guard, aout):
                        continue
                    try:
                        ordinal = clock_map.require_av_span_covered(TickRange(vin, vout), TickRange(ain, aout))
                    except PresentationMapValidationError:
                        continue
                    endpoints = (vin, vout, ain, aout)
                    key = _key(request, clock_map, endpoints)
                    if feasible_count:
                        relation_hash.update(b",")
                    relation_hash.update(json.dumps(
                        {"decision_key": list(key), "endpoints": list(endpoints)},
                        sort_keys=True, separators=(",", ":"),
                    ).encode("ascii"))
                    feasible_count += 1
                    if feasible_count > _MAX_PORTABLE_COUNT:
                        raise CandidatePairLimitError(
                            "feasible relation exceeds the portable exact-integer limit"
                        )
                    row = (key, endpoints, ordinal)
                    if len(retained) < retained_limit:
                        insort(retained, row)
                    elif row < retained[-1]:
                        insort(retained, row)
                        retained.pop()
    relation_hash.update(b"]")
    if not retained:
        raise NoLegalSpanError("no legal candidate-local A/V span in the complete relation")
    video, audio = root.frame_pts_index.context, root.audio_sample_boundaries.context
    relation_sha256 = "sha256:" + relation_hash.hexdigest()
    return tuple(
        CandidateExactSpanResult(
            TickRange(vin, vout),
            TickRange(ain, aout),
            BoundaryProof(
                root.source_id, root.source_sha256, video.clock_id, video.time_base, vin, vout,
                audio.clock_id, audio.time_base, ain, aout, root.frame_pts_index.canonical_hash,
                root.audio_sample_boundaries.canonical_hash, root.visual_validity.canonical_hash,
                root.subtitle_cues.canonical_hash, clock_map.certificate.canonical_hash,
            ),
            guard,
            ordinal,
            key,
            str(logical_count),
            visits,
            feasible_count,
            request.canonical_hash,
            policy.canonical_hash,
            domain_hash,
            relation_sha256,
        )
        for key, (vin, vout, ain, aout), ordinal in retained
    )


def compile_candidate_av_span(
    request: ExactAvSpanRequest, root: RootMediaEvidenceBundle,
    candidate: CandidateTimedEvidenceSet, plan: CandidateEvidenceWindowPlan,
    profile: CandidateTimedSpeechAuthorityInput, clock_map: ReplayedPresentationMap,
    policy: CandidateExactSpanPolicy,
) -> CandidateExactSpanResult:
    """Exhaust all aligned sample pairs, without materializing their Cartesian product."""
    return _compile_candidate_av_span_variants(
        request, root, candidate, plan, profile, clock_map, policy, retained_limit=1,
    )[0]


def compile_candidate_av_span_variants(
    request: ExactAvSpanRequest, root: RootMediaEvidenceBundle,
    candidate: CandidateTimedEvidenceSet, plan: CandidateEvidenceWindowPlan,
    profile: CandidateTimedSpeechAuthorityInput, clock_map: ReplayedPresentationMap,
    policy: CandidateExactSpanPolicy, *, max_variants: int,
) -> tuple[CandidateExactSpanResult, ...]:
    """Return canonical top-K complete results from one exhaustive relation search.

    The returned tuple is intentionally only the retained prefix.  Every item
    carries the complete relation count and digest so callers can record how
    many feasible results were omitted without calling the prefix a relation.
    """
    return _compile_candidate_av_span_variants(
        request,
        root,
        candidate,
        plan,
        profile,
        clock_map,
        policy,
        retained_limit=_validate_max_variants(max_variants),
    )
