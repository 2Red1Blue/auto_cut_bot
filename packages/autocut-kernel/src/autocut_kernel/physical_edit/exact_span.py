"""Fail-closed exact A/V span compilation from source-root evidence.

``compile_exact_av_span`` is the production compiler. The fixture-only records
at the end remain a compatibility projection for the shadow pipeline; they are
not an alternative production optimizer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from typing import Iterable

from ..media.root_evidence import (
    AudioSourceOutcome,
    CoverageOutcome,
    EvidenceCompleteness,
    MediaKind,
    RootMediaEvidenceBundle,
    VisualClassification,
)
from ..media.types import (
    MediaValidationError,
    PTSIndex,
    TickRange,
    TimeBase,
    ValidityIntervals,
    canonical_sha256,
    require_pts,
    sha256_prefixed,
)


class ExactSpanError(ValueError):
    """Base error for exact-span compilation outcomes."""


class ExactSpanValidationError(ExactSpanError):
    """The request or supplied evidence is not a closed legal input."""


class CandidatePairLimitError(ExactSpanValidationError):
    """The full Cartesian candidate domain exceeds the declared hard limit."""


class NoLegalSpanError(ExactSpanError):
    """No candidate in the complete domain satisfies every hard constraint."""


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExactSpanValidationError(f"{field_name} must be a non-empty string")
    return value


def _tb(value: TimeBase) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _ticks(value: TickRange) -> dict[str, int]:
    return {"start_pts": value.start_pts, "end_pts": value.end_pts}


@dataclass(frozen=True, slots=True)
class VideoClockRange:
    """An integer range bound to one exact source video clock."""

    source_id: str
    source_sha256: str
    clock_id: str
    time_base: TimeBase
    tick_range: TickRange

    def __post_init__(self) -> None:
        _require_text(self.source_id, "video_range.source_id")
        try:
            sha256_prefixed(self.source_sha256, "video_range.source_sha256")
        except MediaValidationError as error:
            raise ExactSpanValidationError(str(error)) from error
        _require_text(self.clock_id, "video_range.clock_id")
        if type(self.time_base) is not TimeBase:  # noqa: E721
            raise ExactSpanValidationError("video_range.time_base must be an exact TimeBase")
        if type(self.tick_range) is not TickRange:  # noqa: E721
            raise ExactSpanValidationError("video_range.tick_range must be an exact TickRange")

    def to_mapping(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "clock_id": self.clock_id,
            "time_base": _tb(self.time_base),
            "tick_range": _ticks(self.tick_range),
        }


@dataclass(frozen=True, slots=True)
class ExactAvSpanRequest:
    """Coarse editorial bounds which can never become endpoints directly."""

    desired_video_range: VideoClockRange
    anchor_video_range: VideoClockRange
    minimum_video_duration_tick: int

    def __post_init__(self) -> None:
        if type(self.desired_video_range) is not VideoClockRange:  # noqa: E721
            raise ExactSpanValidationError("desired_video_range must be a VideoClockRange")
        if type(self.anchor_video_range) is not VideoClockRange:  # noqa: E721
            raise ExactSpanValidationError("anchor_video_range must be a VideoClockRange")
        desired = self.desired_video_range
        anchor = self.anchor_video_range
        if (
            desired.source_id,
            desired.source_sha256,
            desired.clock_id,
            desired.time_base,
        ) != (
            anchor.source_id,
            anchor.source_sha256,
            anchor.clock_id,
            anchor.time_base,
        ):
            raise ExactSpanValidationError("desired and anchor ranges must bind one video clock")
        if not desired.tick_range.contains(anchor.tick_range):
            raise ExactSpanValidationError("desired_video_range must contain anchor_video_range")
        minimum = require_pts(self.minimum_video_duration_tick, "minimum_video_duration_tick")
        if minimum <= 0:
            raise ExactSpanValidationError("minimum_video_duration_tick must be positive")

    def to_mapping(self) -> dict[str, object]:
        return {
            "desired_video_range": self.desired_video_range.to_mapping(),
            "anchor_video_range": self.anchor_video_range.to_mapping(),
            "minimum_video_duration_tick": self.minimum_video_duration_tick,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


class ClockMapOutcome(str, Enum):
    COMPLETE = "complete"
    INDETERMINATE = "indeterminate"


class NonOverlapPosition(str, Enum):
    LEADING = "leading"
    TRAILING = "trailing"


@dataclass(frozen=True, slots=True)
class PresentationTimeRange:
    """A normalized half-open range on the source presentation timeline."""

    start_numerator: int
    start_denominator: int
    end_numerator: int
    end_denominator: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.start_numerator, "presentation_range.start_numerator"),
            (self.end_numerator, "presentation_range.end_numerator"),
        ):
            require_pts(value, name)
        for value, name in (
            (self.start_denominator, "presentation_range.start_denominator"),
            (self.end_denominator, "presentation_range.end_denominator"),
        ):
            if require_pts(value, name) <= 0:
                raise ExactSpanValidationError(f"{name} must be positive")
        start = Fraction(self.start_numerator, self.start_denominator)
        end = Fraction(self.end_numerator, self.end_denominator)
        if start >= end:
            raise ExactSpanValidationError("presentation range must satisfy start < end")
        object.__setattr__(self, "start_numerator", start.numerator)
        object.__setattr__(self, "start_denominator", start.denominator)
        object.__setattr__(self, "end_numerator", end.numerator)
        object.__setattr__(self, "end_denominator", end.denominator)

    @classmethod
    def from_fractions(cls, start: Fraction, end: Fraction) -> PresentationTimeRange:
        return cls(start.numerator, start.denominator, end.numerator, end.denominator)

    @property
    def start(self) -> Fraction:
        return Fraction(self.start_numerator, self.start_denominator)

    @property
    def end(self) -> Fraction:
        return Fraction(self.end_numerator, self.end_denominator)

    def to_mapping(self) -> dict[str, object]:
        return {
            "start": {
                "numerator": self.start_numerator,
                "denominator": self.start_denominator,
            },
            "end": {
                "numerator": self.end_numerator,
                "denominator": self.end_denominator,
            },
        }


@dataclass(frozen=True, slots=True)
class PresentationNonOverlap:
    media_kind: MediaKind
    position: NonOverlapPosition
    presentation_range: PresentationTimeRange

    def __post_init__(self) -> None:
        if self.media_kind not in (MediaKind.VIDEO, MediaKind.AUDIO):
            raise ExactSpanValidationError("non-overlap media kind must be video or audio")
        if type(self.position) is not NonOverlapPosition:  # noqa: E721
            raise ExactSpanValidationError("non-overlap position is invalid")
        if type(self.presentation_range) is not PresentationTimeRange:  # noqa: E721
            raise ExactSpanValidationError("non-overlap requires an exact presentation range")

    def to_mapping(self) -> dict[str, object]:
        return {
            "media_kind": self.media_kind.value,
            "position": self.position.value,
            "presentation_range": self.presentation_range.to_mapping(),
        }


def _presentation_tick(tick: int, time_base: TimeBase) -> Fraction:
    return Fraction(tick * time_base.numerator, time_base.denominator)


def _stream_presentation_range(context: object) -> tuple[Fraction, Fraction]:
    origin_tick = require_pts(getattr(context, "origin_tick"), "context.origin_tick")
    end_tick = require_pts(getattr(context, "end_tick"), "context.end_tick")
    time_base = getattr(context, "time_base")
    if type(time_base) is not TimeBase:  # noqa: E721
        raise ExactSpanValidationError("context.time_base must be an exact TimeBase")
    return _presentation_tick(origin_tick, time_base), _presentation_tick(end_tick, time_base)


def _expected_presentation_partition(
    evidence: RootMediaEvidenceBundle,
) -> tuple[PresentationTimeRange, tuple[PresentationNonOverlap, ...]]:
    video_start, video_end = _stream_presentation_range(evidence.frame_pts_index.context)
    audio_start, audio_end = _stream_presentation_range(
        evidence.audio_sample_boundaries.context
    )
    common_start = max(video_start, audio_start)
    common_end = min(video_end, audio_end)
    if common_start >= common_end:
        raise ExactSpanValidationError("video and audio have no common presentation interval")
    records: list[PresentationNonOverlap] = []
    for media_kind, start, end in (
        (MediaKind.VIDEO, video_start, video_end),
        (MediaKind.AUDIO, audio_start, audio_end),
    ):
        if start < common_start:
            records.append(
                PresentationNonOverlap(
                    media_kind,
                    NonOverlapPosition.LEADING,
                    PresentationTimeRange.from_fractions(start, common_start),
                )
            )
        if common_end < end:
            records.append(
                PresentationNonOverlap(
                    media_kind,
                    NonOverlapPosition.TRAILING,
                    PresentationTimeRange.from_fractions(common_end, end),
                )
            )
    records.sort(key=lambda item: (item.media_kind.value, item.position.value))
    return PresentationTimeRange.from_fractions(common_start, common_end), tuple(records)


@dataclass(frozen=True, slots=True)
class VideoToAudioClockMapCertificate:
    """Equal-presentation-time A/V mapping over the proven common interval."""

    source_id: str
    source_sha256: str
    video_clock_id: str
    video_time_base: TimeBase
    audio_clock_id: str
    audio_time_base: TimeBase
    outcome: ClockMapOutcome
    common_presentation_range: PresentationTimeRange | None
    non_overlaps: tuple[PresentationNonOverlap, ...]
    max_error_audio_tick: int
    source_media_probe_sha256: str
    generation_policy_sha256: str

    def __post_init__(self) -> None:
        _require_text(self.source_id, "clock_map.source_id")
        try:
            sha256_prefixed(self.source_sha256, "clock_map.source_sha256")
        except MediaValidationError as error:
            raise ExactSpanValidationError(str(error)) from error
        _require_text(self.video_clock_id, "clock_map.video_clock_id")
        _require_text(self.audio_clock_id, "clock_map.audio_clock_id")
        if type(self.video_time_base) is not TimeBase:  # noqa: E721
            raise ExactSpanValidationError("video_time_base must be an exact TimeBase")
        if type(self.audio_time_base) is not TimeBase:  # noqa: E721
            raise ExactSpanValidationError("audio_time_base must be an exact TimeBase")
        if type(self.outcome) is not ClockMapOutcome:  # noqa: E721
            raise ExactSpanValidationError("clock map outcome must be a ClockMapOutcome")
        if require_pts(self.max_error_audio_tick, "clock_map.max_error_audio_tick") < 0:
            raise ExactSpanValidationError("clock map error must be non-negative")
        try:
            sha256_prefixed(
                self.source_media_probe_sha256, "clock_map.source_media_probe_sha256"
            )
            sha256_prefixed(
                self.generation_policy_sha256, "clock_map.generation_policy_sha256"
            )
        except MediaValidationError as error:
            raise ExactSpanValidationError(str(error)) from error
        non_overlaps = tuple(self.non_overlaps)
        if not all(type(item) is PresentationNonOverlap for item in non_overlaps):
            raise ExactSpanValidationError("clock map contains an invalid non-overlap")
        keys = tuple((item.media_kind.value, item.position.value) for item in non_overlaps)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ExactSpanValidationError("clock map non-overlaps must be canonical and unique")
        if self.outcome is ClockMapOutcome.INDETERMINATE:
            if self.common_presentation_range is not None or non_overlaps:
                raise ExactSpanValidationError(
                    "indeterminate clock map cannot assert presentation coverage"
                )
            object.__setattr__(self, "non_overlaps", non_overlaps)
            return
        if type(self.common_presentation_range) is not PresentationTimeRange:  # noqa: E721
            raise ExactSpanValidationError("complete clock map requires presentation coverage")
        object.__setattr__(self, "non_overlaps", non_overlaps)

    @classmethod
    def from_root_evidence(
        cls,
        evidence: RootMediaEvidenceBundle,
        *,
        max_error_audio_tick: int,
        source_media_probe_sha256: str,
        generation_policy_sha256: str,
    ) -> VideoToAudioClockMapCertificate:
        common, non_overlaps = _expected_presentation_partition(evidence)
        video = evidence.frame_pts_index.context
        audio = evidence.audio_sample_boundaries.context
        return cls(
            evidence.source_id,
            evidence.source_sha256,
            video.clock_id,
            video.time_base,
            audio.clock_id,
            audio.time_base,
            ClockMapOutcome.COMPLETE,
            common,
            non_overlaps,
            max_error_audio_tick,
            source_media_probe_sha256,
            generation_policy_sha256,
        )

    def assert_complete_for(self, evidence: RootMediaEvidenceBundle) -> None:
        if self.outcome is not ClockMapOutcome.COMPLETE:
            raise ExactSpanValidationError("video-to-audio clock map is indeterminate")
        video = evidence.frame_pts_index.context
        audio = evidence.audio_sample_boundaries.context
        if (
            self.source_id,
            self.source_sha256,
            self.video_clock_id,
            self.video_time_base,
            self.audio_clock_id,
            self.audio_time_base,
        ) != (
            evidence.source_id,
            evidence.source_sha256,
            video.clock_id,
            video.time_base,
            audio.clock_id,
            audio.time_base,
        ):
            raise ExactSpanValidationError("clock map source/hash/clock/time base mismatch")
        common, non_overlaps = _expected_presentation_partition(evidence)
        if self.common_presentation_range != common or self.non_overlaps != non_overlaps:
            raise ExactSpanValidationError(
                "clock map must expose the exact common interval and all non-overlap"
            )

    def map_video_tick_bounds(self, video_tick: int) -> tuple[int, int]:
        """Return conservative inclusive audio-tick bounds for a video tick."""
        tick = require_pts(video_tick, "video_tick")
        if self.outcome is not ClockMapOutcome.COMPLETE:
            raise ExactSpanValidationError("cannot use an indeterminate clock map")
        common = self.common_presentation_range
        if common is None:
            raise ExactSpanValidationError("clock map has no presentation coverage")
        presentation = _presentation_tick(tick, self.video_time_base)
        if not common.start <= presentation <= common.end:
            raise ExactSpanValidationError(
                "video tick is outside the common presentation interval"
            )
        exact_audio_tick = presentation / Fraction(
            self.audio_time_base.numerator, self.audio_time_base.denominator
        )
        floor_value = exact_audio_tick.numerator // exact_audio_tick.denominator
        ceil_value = -((-exact_audio_tick.numerator) // exact_audio_tick.denominator)
        return (
            floor_value - self.max_error_audio_tick,
            ceil_value + self.max_error_audio_tick,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "video_clock_id": self.video_clock_id,
            "video_time_base": _tb(self.video_time_base),
            "audio_clock_id": self.audio_clock_id,
            "audio_time_base": _tb(self.audio_time_base),
            "outcome": self.outcome.value,
            "common_presentation_range": None
            if self.common_presentation_range is None
            else self.common_presentation_range.to_mapping(),
            "non_overlaps": [item.to_mapping() for item in self.non_overlaps],
            "max_error_audio_tick": self.max_error_audio_tick,
            "source_media_probe_sha256": self.source_media_probe_sha256,
            "generation_policy_sha256": self.generation_policy_sha256,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


_DEFAULT_FORBIDDEN = (
    VisualClassification.BLACK,
    VisualClassification.WHITE,
    VisualClassification.FROZEN,
    VisualClassification.TRANSITION,
    VisualClassification.UNKNOWN,
)


@dataclass(frozen=True, slots=True)
class ExactAvSpanPolicy:
    candidate_cartesian_limit: int
    endpoint_stability_video_tick: int
    subtitle_clearance_floor_video_tick: int
    av_sync_tolerance_audio_tick: int
    maximum_mapping_error_audio_tick: int
    require_audio: bool = True
    forbidden_visual_classes: tuple[VisualClassification, ...] = _DEFAULT_FORBIDDEN

    def __post_init__(self) -> None:
        for field_name, positive in (
            ("candidate_cartesian_limit", True),
            ("endpoint_stability_video_tick", True),
            ("subtitle_clearance_floor_video_tick", True),
            ("av_sync_tolerance_audio_tick", False),
            ("maximum_mapping_error_audio_tick", False),
        ):
            value = require_pts(getattr(self, field_name), field_name)
            if (positive and value <= 0) or (not positive and value < 0):
                qualifier = "positive" if positive else "non-negative"
                raise ExactSpanValidationError(f"{field_name} must be {qualifier}")
        if type(self.require_audio) is not bool:  # noqa: E721
            raise ExactSpanValidationError("require_audio must be a boolean")
        forbidden = tuple(self.forbidden_visual_classes)
        canonical = tuple(item for item in VisualClassification if item in forbidden)
        if (
            not forbidden
            or not all(type(item) is VisualClassification for item in forbidden)
            or forbidden != canonical
            or len(forbidden) != len(set(forbidden))
        ):
            raise ExactSpanValidationError("forbidden visual classes must be canonical")
        if VisualClassification.VALID_CONTENT in forbidden:
            raise ExactSpanValidationError("valid content cannot be forbidden")
        object.__setattr__(self, "forbidden_visual_classes", forbidden)

    def to_mapping(self) -> dict[str, object]:
        return {
            "candidate_cartesian_limit": self.candidate_cartesian_limit,
            "endpoint_stability_video_tick": self.endpoint_stability_video_tick,
            "subtitle_clearance_floor_video_tick": self.subtitle_clearance_floor_video_tick,
            "av_sync_tolerance_audio_tick": self.av_sync_tolerance_audio_tick,
            "maximum_mapping_error_audio_tick": self.maximum_mapping_error_audio_tick,
            "require_audio": self.require_audio,
            "forbidden_visual_classes": [item.value for item in self.forbidden_visual_classes],
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class BoundaryProof:
    source_id: str
    source_sha256: str
    video_clock_id: str
    video_time_base: TimeBase
    video_in_tick: int
    video_out_tick: int
    audio_clock_id: str
    audio_time_base: TimeBase
    audio_in_tick: int
    audio_out_tick: int
    frame_pts_index_set_sha256: str
    audio_sample_boundary_set_sha256: str
    visual_validity_set_sha256: str
    subtitle_cue_set_sha256: str
    clock_map_certificate_sha256: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "video_clock_id": self.video_clock_id,
            "video_time_base": _tb(self.video_time_base),
            "video_in_tick": self.video_in_tick,
            "video_out_tick": self.video_out_tick,
            "audio_clock_id": self.audio_clock_id,
            "audio_time_base": _tb(self.audio_time_base),
            "audio_in_tick": self.audio_in_tick,
            "audio_out_tick": self.audio_out_tick,
            "frame_pts_index_set_sha256": self.frame_pts_index_set_sha256,
            "audio_sample_boundary_set_sha256": self.audio_sample_boundary_set_sha256,
            "visual_validity_set_sha256": self.visual_validity_set_sha256,
            "subtitle_cue_set_sha256": self.subtitle_cue_set_sha256,
            "clock_map_certificate_sha256": self.clock_map_certificate_sha256,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class DialogueIntegrityProof:
    transcript_set_sha256: str
    speech_activity_set_sha256: str
    checked_word_count: int
    checked_sentence_count: int
    checked_vad_range_count: int
    audio_in_tick: int
    audio_out_tick: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "transcript_set_sha256": self.transcript_set_sha256,
            "speech_activity_set_sha256": self.speech_activity_set_sha256,
            "checked_word_count": self.checked_word_count,
            "checked_sentence_count": self.checked_sentence_count,
            "checked_vad_range_count": self.checked_vad_range_count,
            "audio_in_tick": self.audio_in_tick,
            "audio_out_tick": self.audio_out_tick,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ExactAvSpanResult:
    video_range: TickRange
    audio_range: TickRange
    boundary_proof: BoundaryProof
    dialogue_integrity_proof: DialogueIntegrityProof
    canonical_decision_key: tuple[int, ...]
    total_cartesian_count: int
    feasible_count: int
    request_sha256: str
    policy_sha256: str
    root_media_evidence_bundle_sha256: str
    candidate_domain_sha256: str
    feasible_relation_sha256: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "video_range": _ticks(self.video_range),
            "audio_range": _ticks(self.audio_range),
            "boundary_proof": self.boundary_proof.to_mapping(),
            "dialogue_integrity_proof": self.dialogue_integrity_proof.to_mapping(),
            "canonical_decision_key": list(self.canonical_decision_key),
            "total_cartesian_count": self.total_cartesian_count,
            "feasible_count": self.feasible_count,
            "request_sha256": self.request_sha256,
            "policy_sha256": self.policy_sha256,
            "root_media_evidence_bundle_sha256": self.root_media_evidence_bundle_sha256,
            "candidate_domain_sha256": self.candidate_domain_sha256,
            "feasible_relation_sha256": self.feasible_relation_sha256,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def _validate_production_inputs(
    request: ExactAvSpanRequest,
    evidence: RootMediaEvidenceBundle,
    clock_map: VideoToAudioClockMapCertificate,
    policy: ExactAvSpanPolicy,
) -> None:
    if type(request) is not ExactAvSpanRequest:  # noqa: E721
        raise ExactSpanValidationError("request must be an ExactAvSpanRequest")
    if type(evidence) is not RootMediaEvidenceBundle:  # noqa: E721
        raise ExactSpanValidationError("evidence must be a RootMediaEvidenceBundle")
    if type(clock_map) is not VideoToAudioClockMapCertificate:  # noqa: E721
        raise ExactSpanValidationError("a complete video-to-audio clock map is required")
    if type(policy) is not ExactAvSpanPolicy:  # noqa: E721
        raise ExactSpanValidationError("policy must be an ExactAvSpanPolicy")
    video = evidence.frame_pts_index.context
    bound = request.desired_video_range
    if (
        bound.source_id,
        bound.source_sha256,
        bound.clock_id,
        bound.time_base,
    ) != (evidence.source_id, evidence.source_sha256, video.clock_id, video.time_base):
        raise ExactSpanValidationError("request ranges do not bind the root video clock")
    if not TickRange(video.origin_tick, video.end_tick).contains(bound.tick_range):
        raise ExactSpanValidationError("request range is outside the root video clock")
    frame_ticks = evidence.frame_pts_index.pts_index.ticks
    if frame_ticks[0] != video.origin_tick or frame_ticks[-1] != video.end_tick:
        raise ExactSpanValidationError("complete frame index requires endpoint sentinels")
    audio = evidence.audio_sample_boundaries
    if not policy.require_audio:
        raise ExactSpanValidationError("canonical A/V compilation requires audio in v1")
    if audio.source_outcome is not AudioSourceOutcome.BOUNDARIES_AVAILABLE:
        raise ExactSpanValidationError("policy requires exact audio sample boundaries")
    audio_ticks = tuple(point.tick for point in audio.points)
    if (
        not audio_ticks
        or audio_ticks[0] != audio.context.origin_tick
        or audio_ticks[-1] != audio.context.end_tick
    ):
        raise ExactSpanValidationError("complete audio boundaries require endpoint sentinels")
    for coverage in (
        evidence.frame_pts_index.coverage,
        audio.coverage,
        evidence.transcript.coverage,
        evidence.speech_activity.coverage,
        evidence.visual_validity.coverage,
        evidence.subtitle_cues.coverage,
    ):
        if coverage.outcome is not CoverageOutcome.COMPLETE:
            raise ExactSpanValidationError("exact compilation requires complete evidence")
    if any(
        item is not EvidenceCompleteness.COMPLETE
        for item in (
            evidence.transcript.completeness.word,
            evidence.transcript.completeness.sentence,
        )
    ):
        raise ExactSpanValidationError("word and sentence transcript evidence must be complete")
    clock_map.assert_complete_for(evidence)
    if clock_map.max_error_audio_tick > policy.maximum_mapping_error_audio_tick:
        raise ExactSpanValidationError("clock map uncertainty exceeds policy maximum")


def _visual_stable(
    evidence: RootMediaEvidenceBundle,
    endpoint: int,
    *,
    is_out: bool,
    policy: ExactAvSpanPolicy,
) -> bool:
    context = evidence.visual_validity.context
    width = policy.endpoint_stability_video_tick
    start, end = (endpoint - width, endpoint) if is_out else (endpoint, endpoint + width)
    if start < context.origin_tick or end > context.end_tick:
        return False
    cursor = start
    for interval in evidence.visual_validity.intervals:
        overlap_start = max(start, interval.in_tick)
        overlap_end = min(end, interval.out_tick)
        if overlap_start >= overlap_end:
            continue
        if overlap_start != cursor:
            return False
        if (
            interval.classification in policy.forbidden_visual_classes
            or interval.classification is not VisualClassification.VALID_CONTENT
        ):
            return False
        cursor = overlap_end
    return cursor == end


def _subtitle_clear(
    evidence: RootMediaEvidenceBundle, endpoint: int, policy: ExactAvSpanPolicy
) -> bool:
    floor = policy.subtitle_clearance_floor_video_tick
    return not any(
        cue.in_tick - cue.timing_error_bound.in_tick - floor
        < endpoint
        < cue.out_tick + cue.timing_error_bound.out_tick + floor
        for cue in evidence.subtitle_cues.cues
    )


def _dialogue_clear(evidence: RootMediaEvidenceBundle, endpoint: int) -> bool:
    protected = (
        tuple((item.in_tick, item.out_tick) for item in evidence.transcript.words)
        + tuple((item.in_tick, item.out_tick) for item in evidence.transcript.sentences)
        + tuple((item.in_tick, item.out_tick) for item in evidence.speech_activity.segments)
    )
    return not any(start < endpoint < end for start, end in protected)


def _map_matches(
    clock_map: VideoToAudioClockMapCertificate,
    video_tick: int,
    audio_tick: int,
    tolerance: int,
) -> bool:
    lower, upper = clock_map.map_video_tick_bounds(video_tick)
    return lower - tolerance <= audio_tick <= upper + tolerance


def _decision_key(
    request: ExactAvSpanRequest,
    clock_map: VideoToAudioClockMapCertificate,
    endpoints: tuple[int, int, int, int],
) -> tuple[int, ...]:
    video_in, video_out, audio_in, audio_out = endpoints
    anchor = request.anchor_video_range.tick_range
    audio_in_bounds = clock_map.map_video_tick_bounds(video_in)
    audio_out_bounds = clock_map.map_video_tick_bounds(video_out)
    return (
        anchor.start_pts - video_in,
        video_out - anchor.end_pts,
        abs(audio_in - (audio_in_bounds[0] + audio_in_bounds[1]) // 2),
        abs(audio_out - (audio_out_bounds[0] + audio_out_bounds[1]) // 2),
        video_out - video_in,
        audio_out - audio_in,
        video_in,
        video_out,
        audio_in,
        audio_out,
    )


def compile_exact_av_span(
    request: ExactAvSpanRequest,
    evidence: RootMediaEvidenceBundle,
    clock_map: VideoToAudioClockMapCertificate,
    policy: ExactAvSpanPolicy,
) -> ExactAvSpanResult:
    """Exhaust the bounded four-endpoint domain and select its canonical minimum."""
    _validate_production_inputs(request, evidence, clock_map, policy)
    desired = request.desired_video_range.tick_range
    anchor = request.anchor_video_range.tick_range
    frame_index = evidence.frame_pts_index.pts_index
    video_starts = frame_index.ticks_between(desired.start_pts, anchor.start_pts)
    video_ends = frame_index.ticks_between(anchor.end_pts, desired.end_pts)
    audio_points = tuple(point.tick for point in evidence.audio_sample_boundaries.points)
    desired_in = clock_map.map_video_tick_bounds(desired.start_pts)
    anchor_in = clock_map.map_video_tick_bounds(anchor.start_pts)
    anchor_out = clock_map.map_video_tick_bounds(anchor.end_pts)
    desired_out = clock_map.map_video_tick_bounds(desired.end_pts)
    tolerance = policy.av_sync_tolerance_audio_tick
    audio_starts = tuple(
        tick
        for tick in audio_points
        if desired_in[0] - tolerance <= tick <= anchor_in[1] + tolerance
    )
    audio_ends = tuple(
        tick
        for tick in audio_points
        if anchor_out[0] - tolerance <= tick <= desired_out[1] + tolerance
    )
    total_count = len(video_starts) * len(video_ends) * len(audio_starts) * len(audio_ends)
    if total_count > policy.candidate_cartesian_limit:
        raise CandidatePairLimitError(
            f"candidate Cartesian count {total_count} exceeds limit "
            f"{policy.candidate_cartesian_limit}"
        )
    candidate_domain = {
        "video_starts": list(video_starts),
        "video_ends": list(video_ends),
        "audio_starts": list(audio_starts),
        "audio_ends": list(audio_ends),
    }
    feasible: list[tuple[tuple[int, ...], tuple[int, int, int, int]]] = []
    for video_in in video_starts:
        for video_out in video_ends:
            if video_in >= video_out:
                continue
            if video_out - video_in < request.minimum_video_duration_tick:
                continue
            if not _visual_stable(evidence, video_in, is_out=False, policy=policy):
                continue
            if not _visual_stable(evidence, video_out, is_out=True, policy=policy):
                continue
            if not _subtitle_clear(evidence, video_in, policy):
                continue
            if not _subtitle_clear(evidence, video_out, policy):
                continue
            for audio_in in audio_starts:
                if not _dialogue_clear(evidence, audio_in):
                    continue
                if not _map_matches(clock_map, video_in, audio_in, tolerance):
                    continue
                for audio_out in audio_ends:
                    if audio_in >= audio_out or not _dialogue_clear(evidence, audio_out):
                        continue
                    if not _map_matches(clock_map, video_out, audio_out, tolerance):
                        continue
                    endpoints = (video_in, video_out, audio_in, audio_out)
                    feasible.append((_decision_key(request, clock_map, endpoints), endpoints))
    if not feasible:
        raise NoLegalSpanError("no legal exact A/V span exists in the complete domain")
    feasible.sort(key=lambda item: (item[0], item[1]))
    selected_key, selected = feasible[0]
    video_in, video_out, audio_in, audio_out = selected
    boundary_proof = BoundaryProof(
        evidence.source_id,
        evidence.source_sha256,
        evidence.frame_pts_index.context.clock_id,
        evidence.frame_pts_index.context.time_base,
        video_in,
        video_out,
        evidence.audio_sample_boundaries.context.clock_id,
        evidence.audio_sample_boundaries.context.time_base,
        audio_in,
        audio_out,
        evidence.frame_pts_index.canonical_hash,
        evidence.audio_sample_boundaries.canonical_hash,
        evidence.visual_validity.canonical_hash,
        evidence.subtitle_cues.canonical_hash,
        clock_map.canonical_hash,
    )
    dialogue_proof = DialogueIntegrityProof(
        evidence.transcript.canonical_hash,
        evidence.speech_activity.canonical_hash,
        len(evidence.transcript.words),
        len(evidence.transcript.sentences),
        len(evidence.speech_activity.segments),
        audio_in,
        audio_out,
    )
    relation = [
        {"decision_key": list(key), "endpoints": list(endpoints)}
        for key, endpoints in feasible
    ]
    return ExactAvSpanResult(
        TickRange(video_in, video_out),
        TickRange(audio_in, audio_out),
        boundary_proof,
        dialogue_proof,
        selected_key,
        total_count,
        len(feasible),
        request.canonical_hash,
        policy.canonical_hash,
        evidence.canonical_hash,
        canonical_sha256(candidate_domain),
        canonical_sha256(relation),
    )


@dataclass(frozen=True, slots=True)
class CanonicalExactAvSpanCompiler:
    """The sole production exact span compiler with an immutable policy."""

    policy: ExactAvSpanPolicy

    def compile(
        self,
        request: ExactAvSpanRequest,
        evidence: RootMediaEvidenceBundle,
        clock_map: VideoToAudioClockMapCertificate,
    ) -> ExactAvSpanResult:
        return compile_exact_av_span(request, evidence, clock_map, self.policy)


# Fixture-only compatibility projection. Production code must use the A/V API.


@dataclass(frozen=True, slots=True)
class FixtureBeatInput:
    desired_start_pts: int
    anchor_start_pts: int
    anchor_end_pts: int
    desired_end_pts: int
    minimum_duration_pts: int

    def __post_init__(self) -> None:
        desired_start = require_pts(self.desired_start_pts, "desired_start_pts")
        anchor_start = require_pts(self.anchor_start_pts, "anchor_start_pts")
        anchor_end = require_pts(self.anchor_end_pts, "anchor_end_pts")
        desired_end = require_pts(self.desired_end_pts, "desired_end_pts")
        minimum_duration = require_pts(self.minimum_duration_pts, "minimum_duration_pts")
        if not desired_start <= anchor_start < anchor_end <= desired_end:
            raise ExactSpanValidationError(
                "fixture beat must satisfy desired_start <= anchor_start < anchor_end <= desired_end"
            )
        if minimum_duration <= 0:
            raise ExactSpanValidationError("minimum_duration_pts must be positive")


@dataclass(frozen=True, slots=True)
class SpanSelectionPolicy:
    candidate_pair_limit: int
    forbidden_ranges: tuple[TickRange, ...] = ()

    def __post_init__(self) -> None:
        if require_pts(self.candidate_pair_limit, "candidate_pair_limit") <= 0:
            raise ExactSpanValidationError("candidate_pair_limit must be positive")
        object.__setattr__(self, "forbidden_ranges", tuple(self.forbidden_ranges))

    @classmethod
    def with_forbidden_ranges(
        cls, candidate_pair_limit: int, forbidden_ranges: Iterable[TickRange]
    ) -> SpanSelectionPolicy:
        return cls(candidate_pair_limit, tuple(forbidden_ranges))


def _validate_indexed_range(pts_index: PTSIndex, value: TickRange, field_name: str) -> None:
    pts_index.require_member(value.start_pts, f"{field_name}.start_pts")
    pts_index.require_member(value.end_pts, f"{field_name}.end_pts")


def _validate_fixture_domain(
    beat: FixtureBeatInput,
    pts_index: PTSIndex,
    validity_intervals: ValidityIntervals,
    policy: SpanSelectionPolicy,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    try:
        for field_name in (
            "desired_start_pts",
            "anchor_start_pts",
            "anchor_end_pts",
            "desired_end_pts",
        ):
            pts_index.require_member(getattr(beat, field_name), field_name)
        validity_intervals.require_indexed(pts_index)
        for position, forbidden_range in enumerate(policy.forbidden_ranges):
            _validate_indexed_range(pts_index, forbidden_range, f"forbidden_ranges[{position}]")
    except MediaValidationError as error:
        raise ExactSpanValidationError(str(error)) from error
    starts = pts_index.ticks_between(beat.desired_start_pts, beat.anchor_start_pts)
    ends = pts_index.ticks_between(beat.anchor_end_pts, beat.desired_end_pts)
    pair_count = len(starts) * len(ends)
    if pair_count > policy.candidate_pair_limit:
        raise CandidatePairLimitError(
            f"candidate pair count {pair_count} exceeds limit {policy.candidate_pair_limit}"
        )
    return starts, ends


def select_exact_span(
    beat: FixtureBeatInput,
    pts_index: PTSIndex,
    validity_intervals: ValidityIntervals,
    policy: SpanSelectionPolicy,
) -> TickRange:
    """Fixture-only compatibility projection for the shadow command."""
    starts, ends = _validate_fixture_domain(beat, pts_index, validity_intervals, policy)
    selected: TickRange | None = None
    selected_key: tuple[int, int, int, int, int] | None = None
    for start_pts in starts:
        for end_pts in ends:
            if start_pts >= end_pts:
                continue
            candidate = TickRange(start_pts, end_pts)
            if candidate.duration_pts < beat.minimum_duration_pts:
                continue
            if not validity_intervals.covers(candidate):
                continue
            if any(candidate.overlaps(forbidden) for forbidden in policy.forbidden_ranges):
                continue
            key = (
                beat.anchor_start_pts - start_pts,
                end_pts - beat.anchor_end_pts,
                candidate.duration_pts,
                start_pts,
                end_pts,
            )
            if selected_key is None or key < selected_key:
                selected, selected_key = candidate, key
    if selected is None:
        raise NoLegalSpanError("no legal span exists in the complete candidate domain")
    return selected


@dataclass(frozen=True, slots=True)
class ExactSpanCompiler:
    """Deprecated fixture adapter; production uses CanonicalExactAvSpanCompiler."""

    pts_index: PTSIndex
    validity_intervals: ValidityIntervals
    policy: SpanSelectionPolicy

    def compile(self, beat: FixtureBeatInput) -> TickRange:
        return select_exact_span(beat, self.pts_index, self.validity_intervals, self.policy)
