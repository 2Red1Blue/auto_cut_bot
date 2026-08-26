"""Candidate-local timed evidence contracts and adaptive window state machine."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from fractions import Fraction

from ..vlm.models import (
    VlmCandidateHypothesis,
    VlmSemanticPack,
    derive_vlm_global_id,
)
from ..vlm.window import WindowManifest
from .root_evidence import (
    AudioSampleBoundarySet,
    CanonicalEvidence,
    Coverage,
    EvidenceCompleteness,
    FramePtsIndexSet,
    SceneBoundarySet,
    ShotBoundarySet,
    SpeechActivitySet,
    SubtitleCueSet,
    TranscriptSet,
    VisualValiditySet,
)
from .types import (
    MediaValidationError,
    TickRange,
    TimeBase,
    canonical_sha256,
    require_pts,
    sha256_prefixed,
)


class TimedEvidenceValidationError(MediaValidationError):
    """Raised when candidate-local timed evidence is not closed."""


class CandidateWindowOutcome(str, Enum):
    """State of one incremental evidence-window plan."""

    AWAITING_EVIDENCE = "awaiting_evidence"
    COMPLETE = "complete"
    EXHAUSTED = "exhausted"
    INDETERMINATE = "indeterminate"


class SentenceCompleteness(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


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


def _boolean(value: object, field_name: str) -> bool:
    if type(value) is not bool:  # noqa: E721
        raise TimedEvidenceValidationError(f"{field_name} must be a boolean")
    return value


def _time_base(value: object, field_name: str) -> TimeBase:
    if type(value) is not TimeBase:  # noqa: E721
        raise TimedEvidenceValidationError(f"{field_name} must be a TimeBase")
    return value


def _range(value: object, field_name: str) -> TickRange:
    if type(value) is not TickRange:  # noqa: E721
        raise TimedEvidenceValidationError(f"{field_name} must be a TickRange")
    return value


def _sha(value: object, field_name: str) -> str:
    try:
        return sha256_prefixed(value, field_name)
    except MediaValidationError as error:
        raise TimedEvidenceValidationError(str(error)) from error


@dataclass(frozen=True, slots=True)
class AdaptiveEvidenceWindowPolicy(CanonicalEvidence):
    """Frozen expansion policy for one exact source-video clock."""

    strategy_version: str
    time_base: TimeBase
    initial_left_expansion_pts: int
    initial_right_expansion_pts: int
    expansion_step_pts: int
    max_expansion_count: int
    boundary_touch_margin_pts: int

    def __post_init__(self) -> None:
        _text(self.strategy_version, "policy.strategy_version")
        _time_base(self.time_base, "policy.time_base")
        _tick(self.initial_left_expansion_pts, "policy.initial_left_expansion_pts")
        _tick(self.initial_right_expansion_pts, "policy.initial_right_expansion_pts")
        _tick(self.expansion_step_pts, "policy.expansion_step_pts", positive=True)
        _tick(self.max_expansion_count, "policy.max_expansion_count")
        _tick(self.boundary_touch_margin_pts, "policy.boundary_touch_margin_pts")


@dataclass(frozen=True, slots=True)
class CalibrationBinding(CanonicalEvidence):
    """Active detector calibration with an explicit non-zero timing allowance."""

    policy_sha256: str
    detector_sha256: str
    calibration_record_sha256: str
    producer_id: str
    producer_version: str
    time_base: TimeBase
    timing_error_bound_tick: int
    active: bool
    adapter_sha256: str | None = None

    def __post_init__(self) -> None:
        _sha(self.policy_sha256, "calibration.policy_sha256")
        _sha(self.detector_sha256, "calibration.detector_sha256")
        _sha(self.calibration_record_sha256, "calibration.calibration_record_sha256")
        _text(self.producer_id, "calibration.producer_id")
        _text(self.producer_version, "calibration.producer_version")
        if self.adapter_sha256 is not None:
            _sha(self.adapter_sha256, "calibration.adapter_sha256")
        _time_base(self.time_base, "calibration.time_base")
        _tick(
            self.timing_error_bound_tick,
            "calibration.timing_error_bound_tick",
            positive=True,
        )
        if not _boolean(self.active, "calibration.active"):
            raise TimedEvidenceValidationError("calibration binding must be active")


@dataclass(frozen=True, slots=True)
class CandidateEvidenceWindow(CanonicalEvidence):
    """One source-local extraction request, without producer or admission claims."""

    source_id: str
    source_sha256: str
    source_clock_id: str
    source_time_base: TimeBase
    source_range: TickRange
    vlm_candidate_sha256: str
    vlm_request_identity_sha256: str
    window_manifest_sha256: str
    frame_pts_index_set_sha256: str
    coarse_range: TickRange
    current_range: TickRange
    expansion_ordinal: int

    def __post_init__(self) -> None:
        _text(self.source_id, "candidate.source_id")
        _sha(self.source_sha256, "candidate.source_sha256")
        _text(self.source_clock_id, "candidate.source_clock_id")
        _time_base(self.source_time_base, "candidate.source_time_base")
        source_range = _range(self.source_range, "candidate.source_range")
        coarse_range = _range(self.coarse_range, "candidate.coarse_range")
        current_range = _range(self.current_range, "candidate.current_range")
        if not source_range.contains(current_range):
            raise TimedEvidenceValidationError(
                "candidate current_range must stay in the full source range"
            )
        if not current_range.contains(coarse_range):
            raise TimedEvidenceValidationError(
                "candidate current_range must contain the coarse VLM range"
            )
        _sha(self.vlm_candidate_sha256, "candidate.vlm_candidate_sha256")
        _sha(
            self.vlm_request_identity_sha256,
            "candidate.vlm_request_identity_sha256",
        )
        _sha(self.window_manifest_sha256, "candidate.window_manifest_sha256")
        _sha(self.frame_pts_index_set_sha256, "candidate.frame_pts_index_set_sha256")
        _tick(self.expansion_ordinal, "candidate.expansion_ordinal")

    @property
    def candidate_evidence_window_id(self) -> str:
        return self.canonical_hash


@dataclass(frozen=True, slots=True)
class CandidateWindowAssessment(CanonicalEvidence):
    """Observed ASR/VAD/decoder closure facts for one exact extraction window."""

    candidate_window_sha256: str
    transcript_left_boundary_touch: bool
    transcript_right_boundary_touch: bool
    speech_left_boundary_touch: bool
    speech_right_boundary_touch: bool
    left_truncated: bool
    right_truncated: bool
    sentence_completeness: SentenceCompleteness

    def __post_init__(self) -> None:
        _sha(self.candidate_window_sha256, "assessment.candidate_window_sha256")
        for field_name in (
            "transcript_left_boundary_touch",
            "transcript_right_boundary_touch",
            "speech_left_boundary_touch",
            "speech_right_boundary_touch",
            "left_truncated",
            "right_truncated",
        ):
            _boolean(getattr(self, field_name), f"assessment.{field_name}")
        if type(self.sentence_completeness) is not SentenceCompleteness:  # noqa: E721
            raise TimedEvidenceValidationError(
                "assessment.sentence_completeness must be a SentenceCompleteness"
            )

    @property
    def expand_left(self) -> bool:
        return (
            self.transcript_left_boundary_touch
            or self.speech_left_boundary_touch
            or self.left_truncated
        )

    @property
    def expand_right(self) -> bool:
        return (
            self.transcript_right_boundary_touch
            or self.speech_right_boundary_touch
            or self.right_truncated
        )

    @property
    def needs_expansion(self) -> bool:
        return self.expand_left or self.expand_right

    @property
    def closed(self) -> bool:
        return not self.needs_expansion


def _can_expand(window: CandidateEvidenceWindow, assessment: CandidateWindowAssessment) -> bool:
    return (
        assessment.expand_left and window.current_range.start_pts > window.source_range.start_pts
    ) or (assessment.expand_right and window.current_range.end_pts < window.source_range.end_pts)


@dataclass(frozen=True, slots=True)
class CandidateEvidenceWindowPlan(CanonicalEvidence):
    """Complete incremental expansion trace for one VLM candidate hypothesis."""

    policy_sha256: str
    max_expansion_count: int
    vlm_candidate_sha256: str
    window_manifest_sha256: str
    windows: tuple[CandidateEvidenceWindow, ...]
    assessments: tuple[CandidateWindowAssessment, ...]
    outcome: CandidateWindowOutcome

    def __post_init__(self) -> None:
        _sha(self.policy_sha256, "plan.policy_sha256")
        _tick(self.max_expansion_count, "plan.max_expansion_count")
        _sha(self.vlm_candidate_sha256, "plan.vlm_candidate_sha256")
        _sha(self.window_manifest_sha256, "plan.window_manifest_sha256")
        windows = tuple(self.windows)
        assessments = tuple(self.assessments)
        if not windows or any(type(item) is not CandidateEvidenceWindow for item in windows):  # noqa: E721
            raise TimedEvidenceValidationError(
                "plan.windows must contain CandidateEvidenceWindow values"
            )
        if any(type(item) is not CandidateWindowAssessment for item in assessments):  # noqa: E721
            raise TimedEvidenceValidationError(
                "plan.assessments must contain CandidateWindowAssessment values"
            )
        if type(self.outcome) is not CandidateWindowOutcome:  # noqa: E721
            raise TimedEvidenceValidationError("plan.outcome must be a CandidateWindowOutcome")
        if self.outcome is CandidateWindowOutcome.AWAITING_EVIDENCE:
            if len(assessments) != len(windows) - 1:
                raise TimedEvidenceValidationError(
                    "awaiting plan must have one unassessed final window"
                )
        elif len(assessments) != len(windows):
            raise TimedEvidenceValidationError(
                "terminal plan must assess every extraction window"
            )
        if tuple(item.expansion_ordinal for item in windows) != tuple(range(len(windows))):
            raise TimedEvidenceValidationError(
                "plan windows must have contiguous expansion ordinals"
            )
        first = windows[0]
        if any(
            item.vlm_candidate_sha256 != self.vlm_candidate_sha256
            or item.window_manifest_sha256 != self.window_manifest_sha256
            or item.source_id != first.source_id
            or item.source_sha256 != first.source_sha256
            or item.source_clock_id != first.source_clock_id
            or item.source_time_base != first.source_time_base
            or item.source_range != first.source_range
            or item.coarse_range != first.coarse_range
            or item.frame_pts_index_set_sha256 != first.frame_pts_index_set_sha256
            for item in windows
        ):
            raise TimedEvidenceValidationError(
                "plan windows must share exact source and VLM provenance"
            )
        for prior, current in zip(windows, windows[1:], strict=False):
            if (
                not current.current_range.contains(prior.current_range)
                or current.current_range == prior.current_range
            ):
                raise TimedEvidenceValidationError(
                    "every next plan window must strictly expand its predecessor"
                )
        if any(
            assessment.candidate_window_sha256 != windows[position].canonical_hash
            for position, assessment in enumerate(assessments)
        ):
            raise TimedEvidenceValidationError("each assessment must bind its exact plan window")
        if any(not item.needs_expansion for item in assessments[: len(windows) - 1]):
            raise TimedEvidenceValidationError(
                "only an observed boundary/truncation may create another window"
            )
        if self.outcome is not CandidateWindowOutcome.AWAITING_EVIDENCE:
            final_assessment = assessments[-1]
            final_window = windows[-1]
            expected = CandidateWindowOutcome.INDETERMINATE
            if final_assessment.closed:
                expected = CandidateWindowOutcome.COMPLETE
            elif final_assessment.needs_expansion and (
                final_window.expansion_ordinal >= self.max_expansion_count
                or not _can_expand(final_window, final_assessment)
            ):
                expected = CandidateWindowOutcome.EXHAUSTED
            if self.outcome is not expected:
                raise TimedEvidenceValidationError(
                    "plan outcome does not match the final producer facts and budget"
                )
        object.__setattr__(self, "windows", windows)
        object.__setattr__(self, "assessments", assessments)

    @property
    def final_window(self) -> CandidateEvidenceWindow:
        return self.windows[-1]

    @property
    def final_assessment(self) -> CandidateWindowAssessment | None:
        return self.assessments[-1] if len(self.assessments) == len(self.windows) else None

    @property
    def exhausted(self) -> bool:
        return self.outcome is CandidateWindowOutcome.EXHAUSTED


def _context_key(evidence: object) -> tuple[str, str, str, TimeBase]:
    context = getattr(evidence, "context", None)
    if context is None:
        raise TimedEvidenceValidationError("evidence must expose a validated context")
    return (context.source_id, context.source_sha256, context.clock_id, context.time_base)


def _physical_tick(tick: int, origin: int, time_base: TimeBase) -> Fraction:
    return Fraction(tick - origin) * Fraction(time_base.numerator, time_base.denominator)


def _coverage_contains_range(
    coverage: Coverage,
    candidate_range: TickRange,
    candidate_time_base: TimeBase,
    candidate_origin: int,
    coverage_origin: int,
) -> bool:
    candidate_start = _physical_tick(
        candidate_range.start_pts, candidate_origin, candidate_time_base
    )
    candidate_end = _physical_tick(candidate_range.end_pts, candidate_origin, candidate_time_base)
    coverage_start = _physical_tick(coverage.in_tick, coverage_origin, coverage.time_base)
    coverage_end = _physical_tick(coverage.out_tick, coverage_origin, coverage.time_base)
    return coverage_start <= candidate_start and candidate_end <= coverage_end


@dataclass(frozen=True, slots=True)
class CandidateTimedEvidenceSet(CanonicalEvidence):
    """Conjunctive candidate-local evidence; it grants no cut or admission result."""

    candidate_window: CandidateEvidenceWindow
    window_assessment: CandidateWindowAssessment
    transcript: TranscriptSet
    speech_activity: SpeechActivitySet
    audio_sample_boundaries: AudioSampleBoundarySet
    frame_pts_index: FramePtsIndexSet
    shot_boundaries: ShotBoundarySet
    scene_boundaries: SceneBoundarySet
    visual_validity: VisualValiditySet
    subtitle_cues: SubtitleCueSet
    calibration_bindings: tuple[CalibrationBinding, ...]

    def __post_init__(self) -> None:
        if type(self.candidate_window) is not CandidateEvidenceWindow:  # noqa: E721
            raise TimedEvidenceValidationError("candidate_window must be a CandidateEvidenceWindow")
        if type(self.window_assessment) is not CandidateWindowAssessment:  # noqa: E721
            raise TimedEvidenceValidationError(
                "window_assessment must be a CandidateWindowAssessment"
            )
        if self.window_assessment.candidate_window_sha256 != self.candidate_window.canonical_hash:
            raise TimedEvidenceValidationError(
                "window assessment does not bind the exact candidate window"
            )
        expected_types = (
            (self.transcript, TranscriptSet, "transcript"),
            (self.speech_activity, SpeechActivitySet, "speech_activity"),
            (self.audio_sample_boundaries, AudioSampleBoundarySet, "audio_sample_boundaries"),
            (self.frame_pts_index, FramePtsIndexSet, "frame_pts_index"),
            (self.shot_boundaries, ShotBoundarySet, "shot_boundaries"),
            (self.scene_boundaries, SceneBoundarySet, "scene_boundaries"),
            (self.visual_validity, VisualValiditySet, "visual_validity"),
            (self.subtitle_cues, SubtitleCueSet, "subtitle_cues"),
        )
        for value, expected_type, field_name in expected_types:
            if type(value) is not expected_type:  # noqa: E721
                raise TimedEvidenceValidationError(f"{field_name} has an invalid evidence type")
        window = self.candidate_window
        frame_key = _context_key(self.frame_pts_index)
        if frame_key != (
            window.source_id,
            window.source_sha256,
            window.source_clock_id,
            window.source_time_base,
        ):
            raise TimedEvidenceValidationError(
                "frame evidence does not bind the candidate video clock"
            )
        video_sets = (
            self.frame_pts_index,
            self.shot_boundaries,
            self.scene_boundaries,
            self.visual_validity,
            self.subtitle_cues,
        )
        if any(_context_key(item) != frame_key for item in video_sets):
            raise TimedEvidenceValidationError("video evidence sets must share one source clock")
        audio_key = _context_key(self.transcript)
        if any(
            _context_key(item) != audio_key
            for item in (self.speech_activity, self.audio_sample_boundaries)
        ):
            raise TimedEvidenceValidationError("audio evidence sets must share one source clock")
        for evidence in (
            *video_sets,
            self.transcript,
            self.speech_activity,
            self.audio_sample_boundaries,
        ):
            if (
                evidence.context.source_id != window.source_id
                or evidence.context.source_sha256 != window.source_sha256
            ):
                raise TimedEvidenceValidationError(
                    "evidence source identity does not match the candidate"
                )
            if not _coverage_contains_range(
                evidence.coverage,
                window.current_range,
                window.source_time_base,
                self.frame_pts_index.context.origin_tick,
                evidence.context.origin_tick,
            ):
                raise TimedEvidenceValidationError(
                    "evidence coverage does not cover the candidate window"
                )
        if self.frame_pts_index.canonical_hash != window.frame_pts_index_set_sha256:
            raise TimedEvidenceValidationError(
                "candidate frame PTS hash does not match the evidence"
            )
        if self.shot_boundaries.frame_pts_index_set_sha256 != self.frame_pts_index.canonical_hash:
            raise TimedEvidenceValidationError(
                "shot boundaries do not bind the exact frame PTS set"
            )
        if self.scene_boundaries.frame_pts_index_set_sha256 != self.frame_pts_index.canonical_hash:
            raise TimedEvidenceValidationError(
                "scene boundaries do not bind the exact frame PTS set"
            )
        if not self.frame_pts_index.pts_index.contains(window.current_range.start_pts):
            raise TimedEvidenceValidationError("candidate start must be a decoded frame PTS")
        if (
            not self.frame_pts_index.pts_index.contains(window.current_range.end_pts)
            and window.current_range.end_pts != self.frame_pts_index.context.end_tick
        ):
            raise TimedEvidenceValidationError(
                "candidate end must be a decoded frame PTS or exact source-end sentinel"
            )
        if not _sentence_fact_matches(
            self.window_assessment.sentence_completeness, self.transcript
        ):
            raise TimedEvidenceValidationError(
                "window sentence fact disagrees with transcript evidence"
            )
        bindings = tuple(self.calibration_bindings)
        if not bindings or any(type(item) is not CalibrationBinding for item in bindings):  # noqa: E721
            raise TimedEvidenceValidationError(
                "calibration_bindings must contain active CalibrationBinding values"
            )
        binding_keys = tuple(
            (item.producer_id, item.policy_sha256, item.time_base) for item in bindings
        )
        if len(binding_keys) != len(set(binding_keys)):
            raise TimedEvidenceValidationError("calibration bindings must be unique")
        expected_binding_keys = {
            (
                evidence.context.producer_id,
                evidence.context.generation_policy_sha256,
                evidence.context.time_base,
            )
            for evidence, _, _ in expected_types
        }
        if set(binding_keys) != expected_binding_keys:
            raise TimedEvidenceValidationError(
                "calibration bindings must cover every exact evidence producer policy"
            )
        object.__setattr__(self, "calibration_bindings", bindings)

    @property
    def candidate_timed_evidence_set_id(self) -> str:
        return self.canonical_hash


def _sentence_fact_matches(fact: SentenceCompleteness, transcript: TranscriptSet) -> bool:
    if fact is SentenceCompleteness.UNKNOWN:
        return True
    if fact is SentenceCompleteness.COMPLETE:
        return transcript.completeness.sentence is EvidenceCompleteness.COMPLETE
    if fact is SentenceCompleteness.PARTIAL:
        return transcript.completeness.sentence in {
            EvidenceCompleteness.PARTIAL,
            EvidenceCompleteness.FAILED,
        }
    return transcript.completeness.sentence is EvidenceCompleteness.NOT_APPLICABLE


def _snap_outward(
    frame_pts_index: FramePtsIndexSet,
    source_range: TickRange,
    desired_start: int,
    desired_end: int,
) -> TickRange:
    ticks = tuple(
        tick
        for tick in frame_pts_index.pts_index.ticks
        if source_range.start_pts <= tick < source_range.end_pts
    )
    if not ticks:
        raise TimedEvidenceValidationError("source range has no decoded frame PTS")
    start_candidates = tuple(tick for tick in ticks if tick <= desired_start)
    start = start_candidates[-1] if start_candidates else ticks[0]
    end_candidates = tuple(tick for tick in ticks if tick >= desired_end)
    end = end_candidates[0] if end_candidates else source_range.end_pts
    if start >= end:
        later = tuple(tick for tick in ticks if tick > start)
        end = later[0] if later else source_range.end_pts
    if start >= end:
        raise TimedEvidenceValidationError(
            "frame PTS index cannot form a non-empty candidate window"
        )
    return TickRange(start, end)


def _validate_plan_inputs(
    candidate: VlmCandidateHypothesis,
    semantic_pack: VlmSemanticPack,
    window_manifest: WindowManifest,
    frame_pts_index: FramePtsIndexSet,
    policy: AdaptiveEvidenceWindowPolicy,
) -> TickRange:
    if type(candidate) is not VlmCandidateHypothesis:  # noqa: E721
        raise TimedEvidenceValidationError("candidate must be a VlmCandidateHypothesis")
    if type(semantic_pack) is not VlmSemanticPack:  # noqa: E721
        raise TimedEvidenceValidationError("semantic_pack must be a VlmSemanticPack")
    if type(window_manifest) is not WindowManifest:  # noqa: E721
        raise TimedEvidenceValidationError("window_manifest must be a WindowManifest")
    if type(frame_pts_index) is not FramePtsIndexSet:  # noqa: E721
        raise TimedEvidenceValidationError("frame_pts_index must be a FramePtsIndexSet")
    if type(policy) is not AdaptiveEvidenceWindowPolicy:  # noqa: E721
        raise TimedEvidenceValidationError("policy must be an AdaptiveEvidenceWindowPolicy")
    if semantic_pack.window_manifest_sha256 != window_manifest.canonical_hash:
        raise TimedEvidenceValidationError(
            "semantic pack does not bind the supplied window manifest"
        )
    if candidate not in semantic_pack.candidate_hypotheses:
        raise TimedEvidenceValidationError(
            "candidate is not a member of the supplied semantic pack"
        )
    if candidate.candidate_id != derive_vlm_global_id(
        "candidate",
        candidate.local_candidate_id,
        semantic_pack.request_identity_sha256,
    ):
        raise TimedEvidenceValidationError(
            "candidate does not bind the semantic pack request identity"
        )
    if window_manifest.frame_pts_index_set_sha256 != frame_pts_index.canonical_hash:
        raise TimedEvidenceValidationError(
            "window manifest does not bind the supplied frame PTS index"
        )
    frame_context = frame_pts_index.context
    if (
        frame_context.source_id != window_manifest.source_id
        or frame_context.source_sha256 != window_manifest.source_sha256
        or frame_context.clock_id != window_manifest.source_clock_id
        or frame_context.time_base != window_manifest.source_time_base
    ):
        raise TimedEvidenceValidationError(
            "frame PTS source clock does not match the window manifest"
        )
    interval = candidate.support.source_interval
    if interval.source_time_base != window_manifest.source_time_base:
        raise TimedEvidenceValidationError("candidate and window manifest clocks disagree")
    if policy.time_base != window_manifest.source_time_base:
        raise TimedEvidenceValidationError("policy time base does not match the video clock")
    source_range = TickRange(frame_context.origin_tick, frame_context.end_tick)
    if not source_range.contains(window_manifest.source_range):
        raise TimedEvidenceValidationError(
            "VLM window range must stay within the full source extent"
        )
    if not window_manifest.source_range.contains(interval.coarse_range):
        raise TimedEvidenceValidationError("candidate coarse range is outside its VLM window")
    return source_range


def plan_candidate_evidence_window(
    candidate: VlmCandidateHypothesis,
    semantic_pack: VlmSemanticPack,
    window_manifest: WindowManifest,
    frame_pts_index: FramePtsIndexSet,
    policy: AdaptiveEvidenceWindowPolicy,
) -> CandidateEvidenceWindowPlan:
    """Create only the initial extraction request; real evidence drives expansion."""

    source_range = _validate_plan_inputs(
        candidate, semantic_pack, window_manifest, frame_pts_index, policy
    )
    interval = candidate.support.source_interval
    error = interval.mapping_error_bound_source_pts
    desired_start = max(
        source_range.start_pts,
        interval.coarse_range.start_pts - error - policy.initial_left_expansion_pts,
    )
    desired_end = min(
        source_range.end_pts,
        interval.coarse_range.end_pts + error + policy.initial_right_expansion_pts,
    )
    current = _snap_outward(frame_pts_index, source_range, desired_start, desired_end)
    candidate_hash = canonical_sha256(candidate.to_mapping())
    initial = CandidateEvidenceWindow(
        source_id=window_manifest.source_id,
        source_sha256=window_manifest.source_sha256,
        source_clock_id=window_manifest.source_clock_id,
        source_time_base=window_manifest.source_time_base,
        source_range=source_range,
        vlm_candidate_sha256=candidate_hash,
        vlm_request_identity_sha256=semantic_pack.request_identity_sha256,
        window_manifest_sha256=window_manifest.canonical_hash,
        frame_pts_index_set_sha256=frame_pts_index.canonical_hash,
        coarse_range=interval.coarse_range,
        current_range=current,
        expansion_ordinal=0,
    )
    return CandidateEvidenceWindowPlan(
        policy_sha256=policy.canonical_hash,
        max_expansion_count=policy.max_expansion_count,
        vlm_candidate_sha256=candidate_hash,
        window_manifest_sha256=window_manifest.canonical_hash,
        windows=(initial,),
        assessments=(),
        outcome=CandidateWindowOutcome.AWAITING_EVIDENCE,
    )


def advance_candidate_evidence_window(
    plan: CandidateEvidenceWindowPlan,
    assessment: CandidateWindowAssessment,
    frame_pts_index: FramePtsIndexSet,
    policy: AdaptiveEvidenceWindowPolicy,
) -> CandidateEvidenceWindowPlan:
    """Apply one producer assessment and either close, stop, or expand exactly once."""

    if type(plan) is not CandidateEvidenceWindowPlan:  # noqa: E721
        raise TimedEvidenceValidationError("plan must be a CandidateEvidenceWindowPlan")
    if type(assessment) is not CandidateWindowAssessment:  # noqa: E721
        raise TimedEvidenceValidationError("assessment must be a CandidateWindowAssessment")
    if plan.outcome is not CandidateWindowOutcome.AWAITING_EVIDENCE:
        raise TimedEvidenceValidationError("only an awaiting plan may advance")
    if type(frame_pts_index) is not FramePtsIndexSet:  # noqa: E721
        raise TimedEvidenceValidationError("frame_pts_index must be a FramePtsIndexSet")
    if type(policy) is not AdaptiveEvidenceWindowPolicy:  # noqa: E721
        raise TimedEvidenceValidationError("policy must be an AdaptiveEvidenceWindowPolicy")
    if (
        plan.policy_sha256 != policy.canonical_hash
        or plan.max_expansion_count != policy.max_expansion_count
        or plan.final_window.frame_pts_index_set_sha256 != frame_pts_index.canonical_hash
        or assessment.candidate_window_sha256 != plan.final_window.canonical_hash
    ):
        raise TimedEvidenceValidationError("advance inputs do not match the exact awaiting plan")
    assessments = (*plan.assessments, assessment)
    if assessment.closed:
        return replace(
            plan,
            assessments=assessments,
            outcome=CandidateWindowOutcome.COMPLETE,
        )
    if not assessment.needs_expansion:
        return replace(
            plan,
            assessments=assessments,
            outcome=CandidateWindowOutcome.INDETERMINATE,
        )
    current = plan.final_window
    if current.expansion_ordinal >= policy.max_expansion_count or not _can_expand(
        current, assessment
    ):
        return replace(
            plan,
            assessments=assessments,
            outcome=CandidateWindowOutcome.EXHAUSTED,
        )
    desired_start = current.current_range.start_pts
    desired_end = current.current_range.end_pts
    if assessment.expand_left:
        desired_start = max(
            current.source_range.start_pts,
            desired_start - policy.expansion_step_pts,
        )
    if assessment.expand_right:
        desired_end = min(
            current.source_range.end_pts,
            desired_end + policy.expansion_step_pts,
        )
    expanded_range = _snap_outward(
        frame_pts_index,
        current.source_range,
        desired_start,
        desired_end,
    )
    if expanded_range == current.current_range:
        return replace(
            plan,
            assessments=assessments,
            outcome=CandidateWindowOutcome.EXHAUSTED,
        )
    next_window = replace(
        current,
        current_range=expanded_range,
        expansion_ordinal=current.expansion_ordinal + 1,
    )
    return replace(
        plan,
        windows=(*plan.windows, next_window),
        assessments=assessments,
        outcome=CandidateWindowOutcome.AWAITING_EVIDENCE,
    )


plan_adaptive_evidence_window = plan_candidate_evidence_window
plan_candidate_window = plan_candidate_evidence_window


__all__ = [
    "AdaptiveEvidenceWindowPolicy",
    "CalibrationBinding",
    "CandidateEvidenceWindow",
    "CandidateEvidenceWindowPlan",
    "CandidateTimedEvidenceSet",
    "CandidateWindowAssessment",
    "CandidateWindowOutcome",
    "SentenceCompleteness",
    "TimedEvidenceValidationError",
    "advance_candidate_evidence_window",
    "plan_adaptive_evidence_window",
    "plan_candidate_evidence_window",
    "plan_candidate_window",
]
