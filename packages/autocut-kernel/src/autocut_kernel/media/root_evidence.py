"""Closed, immutable contracts for source-root media evidence.

The records in this module are deliberately producer and persistence agnostic.
They contain only source-native integer ticks and immutable provenance, making a
bundle safe to hash before it is handed to later semantic or editing stages.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import Mapping, Protocol, TypeVar, cast

from .types import (
    MediaValidationError,
    PTSIndex,
    TimeBase,
    canonical_sha256,
    require_pts,
    sha256_prefixed,
)


class CoverageOutcome(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class MediaKind(str, Enum):
    AUDIO = "audio"
    VIDEO = "video"


class AudioBoundaryMethod(str, Enum):
    DECODER = "decoder"
    SOURCE = "source"


class VideoBoundaryType(str, Enum):
    SHOT = "shot"
    SCENE = "scene"


class VideoBoundaryMethod(str, Enum):
    DETECTOR = "detector"
    SOURCE = "source"


class AudioSourceOutcome(str, Enum):
    BOUNDARIES_AVAILABLE = "boundaries_available"
    NOT_APPLICABLE = "not_applicable"
    INDETERMINATE = "indeterminate"


class EvidenceCompleteness(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class TranscriptSourceOutcome(str, Enum):
    TRANSCRIPT_AVAILABLE = "transcript_available"
    NO_LEXICAL_CONTENT = "no_lexical_content"
    NO_SPEECH = "no_speech"
    NOT_APPLICABLE = "not_applicable"
    INDETERMINATE = "indeterminate"


class SpeechSourceOutcome(str, Enum):
    SPEECH_DETECTED = "speech_detected"
    NONE_DETECTED = "none_detected"
    NOT_APPLICABLE = "not_applicable"
    INDETERMINATE = "indeterminate"


class VisualClassification(str, Enum):
    VALID_CONTENT = "valid_content"
    BLACK = "black"
    WHITE = "white"
    FROZEN = "frozen"
    TRANSITION = "transition"
    UNKNOWN = "unknown"


class SubtitleDetectionMode(str, Enum):
    EMBEDDED = "embedded"
    BURNED_IN = "burned_in"


class SubtitleSourceOutcome(str, Enum):
    CUES_DETECTED = "cues_detected"
    NONE_DETECTED = "none_detected"
    INDETERMINATE = "indeterminate"


class SubtitleKind(str, Enum):
    SUBTITLE = "subtitle"
    CAPTION = "caption"


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MediaValidationError(f"{field_name} must be a non-empty string")
    return value


def _require_enum(value: object, enum_type: type[Enum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise MediaValidationError(f"{field_name} must be a {enum_type.__name__}")


def _require_time_base(value: object, field_name: str) -> TimeBase:
    if not isinstance(value, TimeBase):
        raise MediaValidationError(f"{field_name} must be a TimeBase")
    return value


_T = TypeVar("_T")


def _tuple(value: Iterable[_T], field_name: str) -> tuple[_T, ...]:
    if isinstance(value, (str, bytes)):
        raise MediaValidationError(f"{field_name} must be a sequence, not text")
    try:
        return tuple(value)
    except TypeError as error:
        raise MediaValidationError(f"{field_name} must be a sequence") from error


def _require_instances(values: Iterable[object], item_type: type[object], field_name: str) -> None:
    if not all(isinstance(item, item_type) for item in values):
        raise MediaValidationError(f"{field_name} contains an invalid record")


def _canonical_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(cast(object, getattr(value, field.name)))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in cast(tuple[object, ...], value)]
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): _canonical_value(item) for key, item in mapping.items()}
    return value


class CanonicalEvidence:
    """Stable JSON mapping and recomputable content identity for a frozen record."""

    def to_mapping(self) -> dict[str, object]:
        mapped = _canonical_value(self)
        if not isinstance(mapped, dict):  # pragma: no cover - subclasses are dataclasses
            raise TypeError("canonical evidence must be a dataclass")
        return cast(dict[str, object], mapped)

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class EvidenceContext(CanonicalEvidence):
    """One producer's policy-bound view of one source clock."""

    source_id: str
    source_sha256: str
    media_kind: MediaKind
    clock_id: str
    time_base: TimeBase
    origin_tick: int
    duration_tick: int
    producer_id: str
    generation_policy_sha256: str

    def __post_init__(self) -> None:
        _require_text(self.source_id, "context.source_id")
        sha256_prefixed(self.source_sha256, "context.source_sha256")
        _require_enum(self.media_kind, MediaKind, "context.media_kind")
        _require_text(self.clock_id, "context.clock_id")
        _require_time_base(self.time_base, "context.time_base")
        require_pts(self.origin_tick, "context.origin_tick")
        if require_pts(self.duration_tick, "context.duration_tick") <= 0:
            raise MediaValidationError("context.duration_tick must be positive")
        _require_text(self.producer_id, "context.producer_id")
        sha256_prefixed(self.generation_policy_sha256, "context.generation_policy_sha256")

    @property
    def end_tick(self) -> int:
        return self.origin_tick + self.duration_tick


@dataclass(frozen=True, slots=True)
class CoverageDiagnostic(CanonicalEvidence):
    """A precise uncovered/failed span with immutable producer evidence."""

    in_tick: int
    out_tick: int
    code: str
    detail: str
    producer_evidence_sha256: str

    def __post_init__(self) -> None:
        start = require_pts(self.in_tick, "diagnostic.in_tick")
        end = require_pts(self.out_tick, "diagnostic.out_tick")
        if start >= end:
            raise MediaValidationError("diagnostic range must satisfy in_tick < out_tick")
        _require_text(self.code, "diagnostic.code")
        _require_text(self.detail, "diagnostic.detail")
        sha256_prefixed(self.producer_evidence_sha256, "diagnostic.producer_evidence_sha256")


@dataclass(frozen=True, slots=True)
class Coverage(CanonicalEvidence):
    source_id: str
    source_sha256: str
    clock_id: str
    time_base: TimeBase
    in_tick: int
    out_tick: int
    outcome: CoverageOutcome
    diagnostics: tuple[CoverageDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.source_id, "coverage.source_id")
        sha256_prefixed(self.source_sha256, "coverage.source_sha256")
        _require_text(self.clock_id, "coverage.clock_id")
        _require_time_base(self.time_base, "coverage.time_base")
        start = require_pts(self.in_tick, "coverage.in_tick")
        end = require_pts(self.out_tick, "coverage.out_tick")
        if start >= end:
            raise MediaValidationError("coverage range must satisfy in_tick < out_tick")
        _require_enum(self.outcome, CoverageOutcome, "coverage.outcome")
        diagnostics = _tuple(self.diagnostics, "coverage.diagnostics")
        _require_instances(diagnostics, CoverageDiagnostic, "coverage.diagnostics")
        if self.outcome is CoverageOutcome.COMPLETE and diagnostics:
            raise MediaValidationError("complete coverage must not contain gap diagnostics")
        if self.outcome is not CoverageOutcome.COMPLETE and not diagnostics:
            raise MediaValidationError("partial/failed coverage requires diagnostics")
        for diagnostic in diagnostics:
            if diagnostic.in_tick < start or diagnostic.out_tick > end:
                raise MediaValidationError("coverage diagnostic must stay within coverage")
        keys = tuple((item.in_tick, item.out_tick, item.code) for item in diagnostics)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise MediaValidationError("coverage diagnostics must be sorted and deduplicated")
        object.__setattr__(self, "diagnostics", diagnostics)


def _validate_coverage(context: EvidenceContext, coverage: Coverage) -> None:
    if (
        coverage.source_id != context.source_id
        or coverage.source_sha256 != context.source_sha256
        or coverage.clock_id != context.clock_id
        or coverage.time_base != context.time_base
    ):
        raise MediaValidationError("coverage source/clock/time base must match its evidence set")
    if coverage.in_tick < context.origin_tick or coverage.out_tick > context.end_tick:
        raise MediaValidationError("coverage must stay within its declared source clock")
    if coverage.outcome is CoverageOutcome.COMPLETE and (
        coverage.in_tick != context.origin_tick or coverage.out_tick != context.end_tick
    ):
        raise MediaValidationError("complete coverage must cover the whole declared source clock")


def _validate_bound(
    *,
    context: EvidenceContext,
    source_id: str,
    source_sha256: str,
    clock_id: str,
    time_base: TimeBase,
    in_tick: int,
    out_tick: int,
    field_name: str,
) -> None:
    start = require_pts(in_tick, f"{field_name}.in_tick")
    end = require_pts(out_tick, f"{field_name}.out_tick")
    if start >= end:
        raise MediaValidationError(f"{field_name} must satisfy in_tick < out_tick")
    if (
        source_id != context.source_id
        or source_sha256 != context.source_sha256
        or clock_id != context.clock_id
        or time_base != context.time_base
    ):
        raise MediaValidationError(f"{field_name} source/clock/time base does not match its set")
    if start < context.origin_tick or end > context.end_tick:
        raise MediaValidationError(f"{field_name} is outside its declared source clock")


def _validate_unbound_range(
    *,
    source_id: str,
    source_sha256: str,
    clock_id: str,
    time_base: TimeBase,
    in_tick: int,
    out_tick: int,
    field_name: str,
) -> None:
    _require_text(source_id, f"{field_name}.source_id")
    sha256_prefixed(source_sha256, f"{field_name}.source_sha256")
    _require_text(clock_id, f"{field_name}.clock_id")
    _require_time_base(time_base, f"{field_name}.time_base")
    start = require_pts(in_tick, f"{field_name}.in_tick")
    end = require_pts(out_tick, f"{field_name}.out_tick")
    if start >= end:
        raise MediaValidationError(f"{field_name} must satisfy in_tick < out_tick")


_SortableKey = tuple[str | int, ...]


def _require_sorted_unique(
    items: tuple[_T, ...],
    key: Callable[[_T], _SortableKey],
    id_getter: Callable[[_T], str],
    id_name: str,
) -> None:
    keys = tuple(key(item) for item in items)
    if keys != tuple(sorted(keys)):
        raise MediaValidationError(f"{id_name} records must be in canonical sorted order")
    identifiers = tuple(id_getter(item) for item in items)
    if len(identifiers) != len(set(identifiers)):
        raise MediaValidationError(f"duplicate {id_name}")


@dataclass(frozen=True, slots=True)
class FramePtsIndexSet(CanonicalEvidence):
    """The complete decoded frame PTS index for one exact video clock."""

    frame_pts_index_set_id: str
    context: EvidenceContext
    coverage: Coverage
    pts_index: PTSIndex
    pts_index_sha256: str

    def __post_init__(self) -> None:
        _require_text(self.frame_pts_index_set_id, "frame_pts_index_set_id")
        if self.context.media_kind is not MediaKind.VIDEO:
            raise MediaValidationError("frame PTS index requires a video clock")
        _validate_coverage(self.context, self.coverage)
        if self.coverage.outcome is not CoverageOutcome.COMPLETE:
            raise MediaValidationError("frame PTS index requires complete coverage")
        _require_instances((self.pts_index,), PTSIndex, "frame.pts_index")
        expected_hash = canonical_sha256(list(self.pts_index.ticks))
        sha256_prefixed(self.pts_index_sha256, "frame.pts_index_sha256")
        if self.pts_index_sha256 != expected_hash:
            raise MediaValidationError("pts_index_sha256 must match the exact frame PTS index")
        if any(
            tick < self.context.origin_tick or tick > self.context.end_tick
            for tick in self.pts_index.ticks
        ):
            raise MediaValidationError("frame PTS tick is outside its declared source clock")


@dataclass(frozen=True, slots=True)
class VideoBoundaryPoint(CanonicalEvidence):
    boundary_id: str
    source_id: str
    source_sha256: str
    clock_id: str
    time_base: TimeBase
    tick: int
    boundary_type: VideoBoundaryType
    method: VideoBoundaryMethod
    confidence_ppm: int

    def __post_init__(self) -> None:
        _require_text(self.boundary_id, "video_boundary.boundary_id")
        _require_text(self.source_id, "video_boundary.source_id")
        sha256_prefixed(self.source_sha256, "video_boundary.source_sha256")
        _require_text(self.clock_id, "video_boundary.clock_id")
        _require_time_base(self.time_base, "video_boundary.time_base")
        require_pts(self.tick, "video_boundary.tick")
        _require_enum(self.boundary_type, VideoBoundaryType, "video_boundary.boundary_type")
        _require_enum(self.method, VideoBoundaryMethod, "video_boundary.method")
        confidence = require_pts(self.confidence_ppm, "video_boundary.confidence_ppm")
        if not 0 <= confidence <= 1_000_000:
            raise MediaValidationError(
                "video_boundary.confidence_ppm must be between 0 and 1000000"
            )


def _validate_video_boundary_points(
    *,
    context: EvidenceContext,
    coverage: Coverage,
    points: tuple[VideoBoundaryPoint, ...],
    boundary_type: VideoBoundaryType,
) -> None:
    if context.media_kind is not MediaKind.VIDEO:
        raise MediaValidationError(f"{boundary_type.value} boundaries require a video clock")
    _validate_coverage(context, coverage)
    if coverage.outcome is not CoverageOutcome.COMPLETE:
        raise MediaValidationError(f"{boundary_type.value} boundaries require complete coverage")
    _require_instances(points, VideoBoundaryPoint, f"{boundary_type.value}.points")
    _require_sorted_unique(
        points,
        lambda item: (item.source_id, item.tick, item.boundary_id),
        lambda item: item.boundary_id,
        "boundary_id",
    )
    ticks: list[int] = []
    for point in points:
        if point.boundary_type is not boundary_type:
            raise MediaValidationError(
                f"{boundary_type.value} boundary set contains another boundary type"
            )
        if (
            point.source_id != context.source_id
            or point.source_sha256 != context.source_sha256
            or point.clock_id != context.clock_id
            or point.time_base != context.time_base
        ):
            raise MediaValidationError(
                f"{boundary_type.value} boundary source/clock/time base does not match its set"
            )
        if point.tick < coverage.in_tick or point.tick > coverage.out_tick:
            raise MediaValidationError(
                f"{boundary_type.value} boundary is outside its complete coverage"
            )
        ticks.append(point.tick)
    if len(ticks) != len(set(ticks)):
        raise MediaValidationError(f"{boundary_type.value} boundary ticks must be deduplicated")


@dataclass(frozen=True, slots=True)
class ShotBoundarySet(CanonicalEvidence):
    shot_boundary_set_id: str
    context: EvidenceContext
    coverage: Coverage
    frame_pts_index_set_sha256: str
    points: tuple[VideoBoundaryPoint, ...]

    def __post_init__(self) -> None:
        _require_text(self.shot_boundary_set_id, "shot_boundary_set_id")
        sha256_prefixed(self.frame_pts_index_set_sha256, "shot.frame_pts_index_set_sha256")
        points = _tuple(self.points, "shot.points")
        _validate_video_boundary_points(
            context=self.context,
            coverage=self.coverage,
            points=points,
            boundary_type=VideoBoundaryType.SHOT,
        )
        object.__setattr__(self, "points", points)


@dataclass(frozen=True, slots=True)
class SceneBoundarySet(CanonicalEvidence):
    scene_boundary_set_id: str
    context: EvidenceContext
    coverage: Coverage
    frame_pts_index_set_sha256: str
    points: tuple[VideoBoundaryPoint, ...]

    def __post_init__(self) -> None:
        _require_text(self.scene_boundary_set_id, "scene_boundary_set_id")
        sha256_prefixed(self.frame_pts_index_set_sha256, "scene.frame_pts_index_set_sha256")
        points = _tuple(self.points, "scene.points")
        _validate_video_boundary_points(
            context=self.context,
            coverage=self.coverage,
            points=points,
            boundary_type=VideoBoundaryType.SCENE,
        )
        object.__setattr__(self, "points", points)


@dataclass(frozen=True, slots=True)
class AudioSampleBoundary(CanonicalEvidence):
    boundary_id: str
    source_id: str
    source_sha256: str
    clock_id: str
    time_base: TimeBase
    tick: int
    method: AudioBoundaryMethod

    def __post_init__(self) -> None:
        _require_text(self.boundary_id, "boundary_id")
        _require_text(self.source_id, "boundary.source_id")
        sha256_prefixed(self.source_sha256, "boundary.source_sha256")
        _require_text(self.clock_id, "boundary.clock_id")
        _require_time_base(self.time_base, "boundary.time_base")
        require_pts(self.tick, "boundary.tick")
        _require_enum(self.method, AudioBoundaryMethod, "boundary.method")


@dataclass(frozen=True, slots=True)
class AudioSampleBoundarySet(CanonicalEvidence):
    audio_sample_boundary_set_id: str
    context: EvidenceContext
    coverage: Coverage
    source_outcome: AudioSourceOutcome
    points: tuple[AudioSampleBoundary, ...]

    def __post_init__(self) -> None:
        _require_text(self.audio_sample_boundary_set_id, "audio_sample_boundary_set_id")
        if self.context.media_kind is not MediaKind.AUDIO:
            raise MediaValidationError("audio sample boundaries require an audio clock")
        _validate_coverage(self.context, self.coverage)
        _require_enum(self.source_outcome, AudioSourceOutcome, "audio.source_outcome")
        points = _tuple(self.points, "audio.points")
        _require_instances(points, AudioSampleBoundary, "audio.points")
        _require_sorted_unique(
            points,
            lambda item: (item.source_id, item.tick, item.boundary_id),
            lambda item: item.boundary_id,
            "boundary_id",
        )
        ticks: list[int] = []
        for point in points:
            if (
                point.source_id != self.context.source_id
                or point.source_sha256 != self.context.source_sha256
                or point.clock_id != self.context.clock_id
                or point.time_base != self.context.time_base
            ):
                raise MediaValidationError(
                    "audio boundary source/clock/time base does not match its set"
                )
            if point.tick < self.context.origin_tick or point.tick > self.context.end_tick:
                raise MediaValidationError("audio boundary is outside its declared source clock")
            ticks.append(point.tick)
        if len(ticks) != len(set(ticks)):
            raise MediaValidationError("audio sample ticks must be deduplicated")
        if self.coverage.outcome is CoverageOutcome.FAILED and points:
            raise MediaValidationError("failed audio coverage cannot contain successful boundaries")
        if self.source_outcome is AudioSourceOutcome.BOUNDARIES_AVAILABLE:
            if self.coverage.outcome is CoverageOutcome.COMPLETE and (
                not points
                or ticks[0] != self.context.origin_tick
                or ticks[-1] != self.context.end_tick
            ):
                raise MediaValidationError(
                    "complete audio boundaries require clock endpoint sentinels"
                )
        elif self.source_outcome is AudioSourceOutcome.NOT_APPLICABLE:
            if self.coverage.outcome is not CoverageOutcome.COMPLETE or points:
                raise MediaValidationError("not_applicable audio must be complete and empty")
        elif self.coverage.outcome is CoverageOutcome.COMPLETE:
            raise MediaValidationError("indeterminate audio cannot have complete coverage")
        object.__setattr__(self, "points", points)


@dataclass(frozen=True, slots=True)
class TranscriptCompleteness(CanonicalEvidence):
    segment: EvidenceCompleteness
    word: EvidenceCompleteness
    sentence: EvidenceCompleteness

    def __post_init__(self) -> None:
        _require_enum(self.segment, EvidenceCompleteness, "transcript.segment_completeness")
        _require_enum(self.word, EvidenceCompleteness, "transcript.word_completeness")
        _require_enum(self.sentence, EvidenceCompleteness, "transcript.sentence_completeness")


@dataclass(frozen=True, slots=True)
class TranscriptWord(CanonicalEvidence):
    word_id: str
    source_id: str
    source_sha256: str
    clock_id: str
    time_base: TimeBase
    in_tick: int
    out_tick: int
    text: str

    def __post_init__(self) -> None:
        _require_text(self.word_id, "word_id")
        _require_text(self.text, "word.text")
        _validate_unbound_range(
            source_id=self.source_id,
            source_sha256=self.source_sha256,
            clock_id=self.clock_id,
            time_base=self.time_base,
            in_tick=self.in_tick,
            out_tick=self.out_tick,
            field_name="word",
        )


@dataclass(frozen=True, slots=True)
class TranscriptSentence(CanonicalEvidence):
    sentence_id: str
    source_id: str
    source_sha256: str
    clock_id: str
    time_base: TimeBase
    in_tick: int
    out_tick: int
    word_ids: tuple[str, ...]
    text: str

    def __post_init__(self) -> None:
        _require_text(self.sentence_id, "sentence_id")
        _require_text(self.text, "sentence.text")
        _validate_unbound_range(
            source_id=self.source_id,
            source_sha256=self.source_sha256,
            clock_id=self.clock_id,
            time_base=self.time_base,
            in_tick=self.in_tick,
            out_tick=self.out_tick,
            field_name="sentence",
        )
        word_ids = _tuple(self.word_ids, "sentence.word_ids")
        _require_instances(word_ids, str, "sentence.word_ids")
        if not all(item for item in word_ids):
            raise MediaValidationError("sentence.word_ids must contain non-empty strings")
        if len(word_ids) != len(set(word_ids)):
            raise MediaValidationError("sentence.word_ids must be deduplicated")
        object.__setattr__(self, "word_ids", word_ids)


@dataclass(frozen=True, slots=True)
class TranscriptSegment(CanonicalEvidence):
    transcript_segment_id: str
    source_id: str
    source_sha256: str
    clock_id: str
    time_base: TimeBase
    in_tick: int
    out_tick: int
    sentence_ids: tuple[str, ...]
    text: str

    def __post_init__(self) -> None:
        _require_text(self.transcript_segment_id, "transcript_segment_id")
        _require_text(self.text, "segment.text")
        _validate_unbound_range(
            source_id=self.source_id,
            source_sha256=self.source_sha256,
            clock_id=self.clock_id,
            time_base=self.time_base,
            in_tick=self.in_tick,
            out_tick=self.out_tick,
            field_name="segment",
        )
        sentence_ids = _tuple(self.sentence_ids, "segment.sentence_ids")
        _require_instances(sentence_ids, str, "segment.sentence_ids")
        if not all(item for item in sentence_ids):
            raise MediaValidationError("segment.sentence_ids must contain non-empty strings")
        if len(sentence_ids) != len(set(sentence_ids)):
            raise MediaValidationError("segment.sentence_ids must be deduplicated")
        object.__setattr__(self, "sentence_ids", sentence_ids)


class _TimedEvidence(Protocol):
    @property
    def source_id(self) -> str: ...

    @property
    def source_sha256(self) -> str: ...

    @property
    def clock_id(self) -> str: ...

    @property
    def time_base(self) -> TimeBase: ...

    @property
    def in_tick(self) -> int: ...

    @property
    def out_tick(self) -> int: ...


_TimedT = TypeVar("_TimedT", bound=_TimedEvidence)


def _validate_timed_records(
    records: tuple[_TimedT, ...],
    *,
    context: EvidenceContext,
    id_name: str,
    id_getter: Callable[[_TimedT], str],
    allow_overlap: bool = False,
) -> None:
    _require_sorted_unique(
        records,
        lambda item: (
            item.source_id,
            item.in_tick,
            item.out_tick,
            id_getter(item),
        ),
        id_getter,
        id_name,
    )
    previous: _TimedT | None = None
    for record in records:
        _validate_bound(
            context=context,
            source_id=record.source_id,
            source_sha256=record.source_sha256,
            clock_id=record.clock_id,
            time_base=record.time_base,
            in_tick=record.in_tick,
            out_tick=record.out_tick,
            field_name=id_name,
        )
        if (
            not allow_overlap
            and previous is not None
            and previous.source_id == record.source_id
            and previous.out_tick > record.in_tick
        ):
            raise MediaValidationError(f"{id_name} records must not overlap")
        previous = record


@dataclass(frozen=True, slots=True)
class TranscriptSet(CanonicalEvidence):
    transcript_set_id: str
    context: EvidenceContext
    coverage: Coverage
    source_outcome: TranscriptSourceOutcome
    completeness: TranscriptCompleteness
    segments: tuple[TranscriptSegment, ...]
    words: tuple[TranscriptWord, ...]
    sentences: tuple[TranscriptSentence, ...]

    def __post_init__(self) -> None:
        _require_text(self.transcript_set_id, "transcript_set_id")
        if self.context.media_kind is not MediaKind.AUDIO:
            raise MediaValidationError("transcript evidence requires an audio clock")
        _validate_coverage(self.context, self.coverage)
        _require_enum(self.source_outcome, TranscriptSourceOutcome, "transcript.source_outcome")
        segments = _tuple(self.segments, "transcript.segments")
        words = _tuple(self.words, "transcript.words")
        sentences = _tuple(self.sentences, "transcript.sentences")
        _require_instances(segments, TranscriptSegment, "transcript.segments")
        _require_instances(words, TranscriptWord, "transcript.words")
        _require_instances(sentences, TranscriptSentence, "transcript.sentences")
        _validate_timed_records(
            segments,
            context=self.context,
            id_name="transcript_segment_id",
            id_getter=lambda item: item.transcript_segment_id,
        )
        _validate_timed_records(
            words, context=self.context, id_name="word_id", id_getter=lambda item: item.word_id
        )
        _validate_timed_records(
            sentences,
            context=self.context,
            id_name="sentence_id",
            id_getter=lambda item: item.sentence_id,
        )
        word_by_id = {item.word_id: item for item in words}
        sentence_by_id = {item.sentence_id: item for item in sentences}
        word_positions = {item.word_id: position for position, item in enumerate(words)}
        sentence_positions = {item.sentence_id: position for position, item in enumerate(sentences)}
        for sentence in sentences:
            if (
                tuple(sorted(sentence.word_ids, key=lambda item: word_positions.get(item, -1)))
                != sentence.word_ids
            ):
                raise MediaValidationError("sentence word references must be in timeline order")
            for word_id in sentence.word_ids:
                word = word_by_id.get(word_id)
                if (
                    word is None
                    or word.in_tick < sentence.in_tick
                    or word.out_tick > sentence.out_tick
                ):
                    raise MediaValidationError(
                        "sentence word references must exist within the sentence"
                    )
        for segment in segments:
            if (
                tuple(
                    sorted(segment.sentence_ids, key=lambda item: sentence_positions.get(item, -1))
                )
                != segment.sentence_ids
            ):
                raise MediaValidationError("segment sentence references must be in timeline order")
            for sentence_id in segment.sentence_ids:
                sentence = sentence_by_id.get(sentence_id)
                if (
                    sentence is None
                    or sentence.in_tick < segment.in_tick
                    or sentence.out_tick > segment.out_tick
                ):
                    raise MediaValidationError(
                        "segment sentence references must exist within the segment"
                    )
        records_present = bool(segments or words or sentences)
        all_not_applicable = all(
            value is EvidenceCompleteness.NOT_APPLICABLE
            for value in (
                self.completeness.segment,
                self.completeness.word,
                self.completeness.sentence,
            )
        )
        if self.coverage.outcome is CoverageOutcome.FAILED and records_present:
            raise MediaValidationError(
                "failed transcript coverage cannot contain successful records"
            )
        if self.source_outcome is TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE:
            if not segments:
                raise MediaValidationError("available transcript requires timed segments")
            for component, completeness, records in (
                ("segment", self.completeness.segment, segments),
                ("word", self.completeness.word, words),
                ("sentence", self.completeness.sentence, sentences),
            ):
                if completeness is EvidenceCompleteness.COMPLETE and not records:
                    raise MediaValidationError(f"complete transcript {component} requires records")
                if completeness is EvidenceCompleteness.NOT_APPLICABLE and records:
                    raise MediaValidationError(
                        f"not_applicable transcript {component} must be empty"
                    )
        elif self.source_outcome in {
            TranscriptSourceOutcome.NO_SPEECH,
            TranscriptSourceOutcome.NO_LEXICAL_CONTENT,
        }:
            if (
                self.coverage.outcome is not CoverageOutcome.COMPLETE
                or records_present
                or self.completeness.segment is not EvidenceCompleteness.COMPLETE
                or self.completeness.sentence is not EvidenceCompleteness.COMPLETE
                or self.completeness.word
                not in {EvidenceCompleteness.COMPLETE, EvidenceCompleteness.NOT_APPLICABLE}
            ):
                raise MediaValidationError(
                    "empty lexical transcript requires capability-complete proof"
                )
        elif self.source_outcome is TranscriptSourceOutcome.NOT_APPLICABLE:
            if (
                self.coverage.outcome is not CoverageOutcome.COMPLETE
                or records_present
                or not all_not_applicable
            ):
                raise MediaValidationError(
                    "not_applicable transcript must be complete, empty, and explicit"
                )
        elif self.coverage.outcome is CoverageOutcome.COMPLETE:
            raise MediaValidationError("indeterminate transcript cannot have complete coverage")
        object.__setattr__(self, "segments", segments)
        object.__setattr__(self, "words", words)
        object.__setattr__(self, "sentences", sentences)


@dataclass(frozen=True, slots=True)
class SpeechActivitySegment(CanonicalEvidence):
    speech_segment_id: str
    source_id: str
    source_sha256: str
    clock_id: str
    time_base: TimeBase
    in_tick: int
    out_tick: int
    confidence_ppm: int | None

    def __post_init__(self) -> None:
        _require_text(self.speech_segment_id, "speech_segment_id")
        _validate_unbound_range(
            source_id=self.source_id,
            source_sha256=self.source_sha256,
            clock_id=self.clock_id,
            time_base=self.time_base,
            in_tick=self.in_tick,
            out_tick=self.out_tick,
            field_name="speech",
        )
        if self.confidence_ppm is not None:
            confidence = require_pts(self.confidence_ppm, "speech.confidence_ppm")
            if not 0 <= confidence <= 1_000_000:
                raise MediaValidationError("speech.confidence_ppm must be null or valid ppm")


@dataclass(frozen=True, slots=True)
class SpeechActivitySet(CanonicalEvidence):
    speech_activity_set_id: str
    context: EvidenceContext
    coverage: Coverage
    source_outcome: SpeechSourceOutcome
    segments: tuple[SpeechActivitySegment, ...]

    def __post_init__(self) -> None:
        _require_text(self.speech_activity_set_id, "speech_activity_set_id")
        if self.context.media_kind is not MediaKind.AUDIO:
            raise MediaValidationError("speech activity requires an audio clock")
        _validate_coverage(self.context, self.coverage)
        _require_enum(self.source_outcome, SpeechSourceOutcome, "speech.source_outcome")
        segments = _tuple(self.segments, "speech.segments")
        _require_instances(segments, SpeechActivitySegment, "speech.segments")
        _validate_timed_records(
            segments,
            context=self.context,
            id_name="speech_segment_id",
            id_getter=lambda item: item.speech_segment_id,
        )
        if self.coverage.outcome is CoverageOutcome.FAILED and segments:
            raise MediaValidationError("failed speech coverage cannot contain successful segments")
        if self.source_outcome is SpeechSourceOutcome.SPEECH_DETECTED and not segments:
            raise MediaValidationError("speech_detected requires segments")
        if self.source_outcome in {
            SpeechSourceOutcome.NONE_DETECTED,
            SpeechSourceOutcome.NOT_APPLICABLE,
        } and (self.coverage.outcome is not CoverageOutcome.COMPLETE or segments):
            raise MediaValidationError(
                "none_detected/not_applicable speech must be complete and empty"
            )
        if (
            self.source_outcome is SpeechSourceOutcome.INDETERMINATE
            and self.coverage.outcome is CoverageOutcome.COMPLETE
        ):
            raise MediaValidationError("indeterminate speech cannot have complete coverage")
        object.__setattr__(self, "segments", segments)


@dataclass(frozen=True, slots=True)
class VisualValidityInterval(CanonicalEvidence):
    visual_interval_id: str
    source_id: str
    source_sha256: str
    clock_id: str
    time_base: TimeBase
    in_tick: int
    out_tick: int
    classification: VisualClassification
    confidence_ppm: int

    def __post_init__(self) -> None:
        _require_text(self.visual_interval_id, "visual_interval_id")
        _validate_unbound_range(
            source_id=self.source_id,
            source_sha256=self.source_sha256,
            clock_id=self.clock_id,
            time_base=self.time_base,
            in_tick=self.in_tick,
            out_tick=self.out_tick,
            field_name="visual",
        )
        _require_enum(self.classification, VisualClassification, "visual.classification")
        confidence = require_pts(self.confidence_ppm, "visual.confidence_ppm")
        if not 0 <= confidence <= 1_000_000:
            raise MediaValidationError("visual.confidence_ppm must be between 0 and 1000000")


@dataclass(frozen=True, slots=True)
class VisualValiditySet(CanonicalEvidence):
    visual_validity_set_id: str
    context: EvidenceContext
    coverage: Coverage
    intervals: tuple[VisualValidityInterval, ...]

    def __post_init__(self) -> None:
        _require_text(self.visual_validity_set_id, "visual_validity_set_id")
        if self.context.media_kind is not MediaKind.VIDEO:
            raise MediaValidationError("visual validity requires a video clock")
        _validate_coverage(self.context, self.coverage)
        intervals = _tuple(self.intervals, "visual.intervals")
        _require_instances(intervals, VisualValidityInterval, "visual.intervals")
        _validate_timed_records(
            intervals,
            context=self.context,
            id_name="visual_interval_id",
            id_getter=lambda item: item.visual_interval_id,
        )
        if self.coverage.outcome is CoverageOutcome.FAILED and intervals:
            raise MediaValidationError("failed visual coverage cannot contain successful intervals")
        if self.coverage.outcome is CoverageOutcome.COMPLETE:
            cursor = self.coverage.in_tick
            for interval in intervals:
                if interval.in_tick != cursor:
                    raise MediaValidationError(
                        "complete visual coverage must explicitly partition every tick"
                    )
                cursor = interval.out_tick
            if cursor != self.coverage.out_tick:
                raise MediaValidationError(
                    "complete visual coverage must explicitly partition every tick"
                )
        object.__setattr__(self, "intervals", intervals)


@dataclass(frozen=True, slots=True)
class TimingErrorBound(CanonicalEvidence):
    time_base: TimeBase
    in_tick: int
    out_tick: int

    def __post_init__(self) -> None:
        _require_time_base(self.time_base, "timing_error_bound.time_base")
        if require_pts(self.in_tick, "timing_error_bound.in_tick") < 0:
            raise MediaValidationError("timing_error_bound.in_tick must be non-negative")
        if require_pts(self.out_tick, "timing_error_bound.out_tick") < 0:
            raise MediaValidationError("timing_error_bound.out_tick must be non-negative")


@dataclass(frozen=True, slots=True)
class SubtitleCue(CanonicalEvidence):
    subtitle_cue_id: str
    source_id: str
    source_sha256: str
    clock_id: str
    time_base: TimeBase
    in_tick: int
    out_tick: int
    kind: SubtitleKind
    detection_mode: SubtitleDetectionMode
    confidence_ppm: int
    timing_error_bound: TimingErrorBound

    def __post_init__(self) -> None:
        _require_text(self.subtitle_cue_id, "subtitle_cue_id")
        _validate_unbound_range(
            source_id=self.source_id,
            source_sha256=self.source_sha256,
            clock_id=self.clock_id,
            time_base=self.time_base,
            in_tick=self.in_tick,
            out_tick=self.out_tick,
            field_name="subtitle",
        )
        _require_enum(self.kind, SubtitleKind, "subtitle.kind")
        _require_enum(self.detection_mode, SubtitleDetectionMode, "subtitle.detection_mode")
        confidence = require_pts(self.confidence_ppm, "subtitle.confidence_ppm")
        if not 0 <= confidence <= 1_000_000:
            raise MediaValidationError("subtitle.confidence_ppm must be between 0 and 1000000")
        if self.timing_error_bound.time_base != self.time_base:
            raise MediaValidationError("subtitle timing error bound must use the cue time base")


@dataclass(frozen=True, slots=True)
class SubtitleCueSet(CanonicalEvidence):
    subtitle_cue_set_id: str
    context: EvidenceContext
    coverage: Coverage
    required_modes: tuple[SubtitleDetectionMode, ...]
    successful_modes: tuple[SubtitleDetectionMode, ...]
    source_outcome: SubtitleSourceOutcome
    cues: tuple[SubtitleCue, ...]

    def __post_init__(self) -> None:
        _require_text(self.subtitle_cue_set_id, "subtitle_cue_set_id")
        if self.context.media_kind is not MediaKind.VIDEO:
            raise MediaValidationError("subtitle cues require a video clock")
        _validate_coverage(self.context, self.coverage)
        _require_enum(self.source_outcome, SubtitleSourceOutcome, "subtitle.source_outcome")
        required_modes = _tuple(self.required_modes, "subtitle.required_modes")
        successful_modes = _tuple(self.successful_modes, "subtitle.successful_modes")
        modes_order = {mode: position for position, mode in enumerate(SubtitleDetectionMode)}
        for name, modes in (
            ("required_modes", required_modes),
            ("successful_modes", successful_modes),
        ):
            _require_instances(modes, SubtitleDetectionMode, f"subtitle.{name}")
            if not modes:
                raise MediaValidationError(f"subtitle.{name} must contain closed detection modes")
            if (
                len(modes) != len(set(modes))
                or tuple(sorted(modes, key=lambda mode: modes_order[mode])) != modes
            ):
                raise MediaValidationError(f"subtitle.{name} must be sorted and deduplicated")
        if not set(successful_modes).issubset(required_modes):
            raise MediaValidationError("successful subtitle modes must be required modes")
        cues = _tuple(self.cues, "subtitle.cues")
        _require_instances(cues, SubtitleCue, "subtitle.cues")
        _validate_timed_records(
            cues,
            context=self.context,
            id_name="subtitle_cue_id",
            id_getter=lambda item: item.subtitle_cue_id,
            allow_overlap=True,
        )
        if any(cue.detection_mode not in successful_modes for cue in cues):
            raise MediaValidationError("subtitle cue mode must have a successful detector outcome")
        if self.coverage.outcome is CoverageOutcome.COMPLETE and set(successful_modes) != set(
            required_modes
        ):
            raise MediaValidationError(
                "complete subtitle coverage requires every required detector"
            )
        if self.coverage.outcome is CoverageOutcome.FAILED and cues:
            raise MediaValidationError("failed subtitle coverage cannot contain successful cues")
        if self.source_outcome is SubtitleSourceOutcome.CUES_DETECTED:
            if not cues:
                raise MediaValidationError("cues_detected requires at least one cue")
        elif self.source_outcome is SubtitleSourceOutcome.NONE_DETECTED:
            if (
                cues
                or self.coverage.outcome is not CoverageOutcome.COMPLETE
                or set(successful_modes) != set(required_modes)
            ):
                raise MediaValidationError(
                    "none_detected requires complete coverage and every required detector"
                )
        elif self.coverage.outcome is CoverageOutcome.COMPLETE:
            raise MediaValidationError("indeterminate subtitles cannot have complete coverage")
        object.__setattr__(self, "required_modes", required_modes)
        object.__setattr__(self, "successful_modes", successful_modes)
        object.__setattr__(self, "cues", cues)


@dataclass(frozen=True, slots=True)
class RootMediaEvidenceBundle(CanonicalEvidence):
    root_media_evidence_bundle_id: str
    source_id: str
    source_sha256: str
    source_manifest_sha256: str
    root_input_manifest_sha256: str
    frame_pts_index: FramePtsIndexSet
    shot_boundaries: ShotBoundarySet
    scene_boundaries: SceneBoundarySet
    audio_sample_boundaries: AudioSampleBoundarySet
    transcript: TranscriptSet
    speech_activity: SpeechActivitySet
    visual_validity: VisualValiditySet
    subtitle_cues: SubtitleCueSet

    def __post_init__(self) -> None:
        _require_text(self.root_media_evidence_bundle_id, "root_media_evidence_bundle_id")
        _require_text(self.source_id, "root.source_id")
        sha256_prefixed(self.source_sha256, "root.source_sha256")
        sha256_prefixed(self.source_manifest_sha256, "root.source_manifest_sha256")
        sha256_prefixed(self.root_input_manifest_sha256, "root.root_input_manifest_sha256")
        sets = (
            self.frame_pts_index,
            self.shot_boundaries,
            self.scene_boundaries,
            self.audio_sample_boundaries,
            self.transcript,
            self.speech_activity,
            self.visual_validity,
            self.subtitle_cues,
        )
        for evidence_set in sets:
            if (
                evidence_set.context.source_id != self.source_id
                or evidence_set.context.source_sha256 != self.source_sha256
            ):
                raise MediaValidationError("root evidence sets must bind the root source identity")
            if evidence_set.coverage.outcome is not CoverageOutcome.COMPLETE:
                raise MediaValidationError("root evidence bundle requires complete coverage")
        audio_contexts = (
            self.audio_sample_boundaries.context,
            self.transcript.context,
            self.speech_activity.context,
        )
        if any(
            (
                context.clock_id,
                context.time_base,
                context.origin_tick,
                context.duration_tick,
            )
            != (
                audio_contexts[0].clock_id,
                audio_contexts[0].time_base,
                audio_contexts[0].origin_tick,
                audio_contexts[0].duration_tick,
            )
            for context in audio_contexts[1:]
        ):
            raise MediaValidationError("root audio evidence sets must use the same source clock")
        video_contexts = (
            self.frame_pts_index.context,
            self.shot_boundaries.context,
            self.scene_boundaries.context,
            self.visual_validity.context,
            self.subtitle_cues.context,
        )
        if any(
            (
                context.clock_id,
                context.time_base,
                context.origin_tick,
                context.duration_tick,
            )
            != (
                video_contexts[0].clock_id,
                video_contexts[0].time_base,
                video_contexts[0].origin_tick,
                video_contexts[0].duration_tick,
            )
            for context in video_contexts[1:]
        ):
            raise MediaValidationError("root video evidence sets must use the same source clock")

        exact_frame_set_hash = self.frame_pts_index.canonical_hash
        for boundary_name, boundary_set in (
            ("shot", self.shot_boundaries),
            ("scene", self.scene_boundaries),
        ):
            if boundary_set.frame_pts_index_set_sha256 != exact_frame_set_hash:
                raise MediaValidationError(
                    f"{boundary_name} boundaries must bind the exact frame PTS index set hash"
                )
            for point in boundary_set.points:
                if not self.frame_pts_index.pts_index.contains(point.tick):
                    raise MediaValidationError(
                        f"{boundary_name} boundary must be a member of the exact frame PTS index"
                    )

        audio_not_applicable = (
            self.audio_sample_boundaries.source_outcome is AudioSourceOutcome.NOT_APPLICABLE
        )
        if audio_not_applicable:
            if (
                self.transcript.source_outcome is not TranscriptSourceOutcome.NOT_APPLICABLE
                or self.speech_activity.source_outcome is not SpeechSourceOutcome.NOT_APPLICABLE
            ):
                raise MediaValidationError("audio-dependent evidence must be not_applicable")
        elif (
            self.transcript.source_outcome is TranscriptSourceOutcome.NOT_APPLICABLE
            or self.speech_activity.source_outcome is SpeechSourceOutcome.NOT_APPLICABLE
        ):
            raise MediaValidationError(
                "audio-dependent evidence cannot be not_applicable when audio exists"
            )

        transcript_no_speech = self.transcript.source_outcome is TranscriptSourceOutcome.NO_SPEECH
        transcript_no_lexical = (
            self.transcript.source_outcome is TranscriptSourceOutcome.NO_LEXICAL_CONTENT
        )
        vad_none_detected = self.speech_activity.source_outcome is SpeechSourceOutcome.NONE_DETECTED
        if transcript_no_speech != vad_none_detected:
            raise MediaValidationError("transcript no_speech and VAD none_detected must agree")
        if transcript_no_speech and self.speech_activity.segments:
            raise MediaValidationError("speech segments cannot accompany transcript no_speech")
        if transcript_no_lexical and (
            self.speech_activity.source_outcome is not SpeechSourceOutcome.SPEECH_DETECTED
            or not self.speech_activity.segments
        ):
            raise MediaValidationError("no_lexical_content requires VAD protected ranges")


__all__ = [
    "AudioBoundaryMethod",
    "AudioSampleBoundary",
    "AudioSampleBoundarySet",
    "AudioSourceOutcome",
    "Coverage",
    "CoverageDiagnostic",
    "CoverageOutcome",
    "EvidenceCompleteness",
    "EvidenceContext",
    "FramePtsIndexSet",
    "MediaKind",
    "RootMediaEvidenceBundle",
    "SceneBoundarySet",
    "ShotBoundarySet",
    "SpeechActivitySegment",
    "SpeechActivitySet",
    "SpeechSourceOutcome",
    "SubtitleCue",
    "SubtitleCueSet",
    "SubtitleDetectionMode",
    "SubtitleKind",
    "SubtitleSourceOutcome",
    "TimingErrorBound",
    "TranscriptCompleteness",
    "TranscriptSegment",
    "TranscriptSentence",
    "TranscriptSet",
    "TranscriptSourceOutcome",
    "TranscriptWord",
    "VisualClassification",
    "VideoBoundaryMethod",
    "VideoBoundaryPoint",
    "VideoBoundaryType",
    "VisualValidityInterval",
    "VisualValiditySet",
]
