"""Candidate-local speech protection without replacing the original root.

The installed registry entry and both calibration bindings remain explicit.
This pure derivation proves neither Store commitment nor edit admission. Coverage
is compared on absolute rational presentation time, never by rebasing each
stream to its own origin. Rolls are clipped only at real source-audio edges.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from ..media.root_evidence import (
    AudioSourceOutcome,
    Coverage,
    CoverageOutcome,
    EvidenceCompleteness,
    EvidenceContext,
    RootMediaEvidenceBundle,
    SpeechActivitySet,
    SpeechSourceOutcome,
    TranscriptSet,
    TranscriptSourceOutcome,
)
from ..media.stage4_predecessor import (
    TimedSpeechProducerRequirement,
    TimedSpeechProfileKind,
    TimedSpeechProfileRegistryEntry,
)
from ..media.timed_evidence import (
    CalibrationBinding,
    CandidateEvidenceWindowPlan,
    CandidateTimedEvidenceSet,
    CandidateWindowOutcome,
    SentenceCompleteness,
)
from ..media.types import TickRange, TimeBase, canonical_sha256, sha256_prefixed
from .dialogue_guard import (
    DialogueGuardError,
    DialogueGuardIndeterminateError,
    DialogueGuardKind,
    DialogueRequirement,
    ProtectedAudioRange,
    group_transcript_words,
    merge_speech_activity,
    roll_protected_audio_ranges,
)


@dataclass(frozen=True, slots=True)
class CandidateDialogueGuard:
    """Hash-bound derived value, not a capability or an accepted proof."""

    root_evidence_sha256: str
    candidate_evidence_sha256: str
    candidate_window_sha256: str
    window_plan_sha256: str
    profile_sha256: str
    guard_policy_sha256: str
    source_id: str
    source_sha256: str
    source_audio_clock_id: str
    source_audio_time_base: TimeBase
    source_audio_range: TickRange | None
    requirement: DialogueRequirement
    kind: DialogueGuardKind
    reason: str
    protected_ranges: tuple[ProtectedAudioRange, ...]

    def __post_init__(self) -> None:
        for name in (
            "root_evidence_sha256", "candidate_evidence_sha256", "candidate_window_sha256",
            "window_plan_sha256", "profile_sha256", "guard_policy_sha256", "source_sha256",
        ):
            sha256_prefixed(getattr(self, name), name)
        for text in (self.source_id, self.source_audio_clock_id):
            if type(text) is not str or not text.strip():  # noqa: E721
                raise DialogueGuardError("candidate guard source/clock must be exact text")
            text.encode("utf-8")
        if type(self.source_audio_time_base) is not TimeBase:  # noqa: E721
            raise DialogueGuardError("candidate guard requires an exact audio time base")
        if type(self.requirement) is not DialogueRequirement or type(self.kind) is not DialogueGuardKind:  # noqa: E721
            raise DialogueGuardError("candidate guard requirement/kind is invalid")
        if type(self.protected_ranges) is not tuple or any(  # noqa: E721
            type(item) is not ProtectedAudioRange for item in self.protected_ranges  # noqa: E721
        ):
            raise DialogueGuardError("candidate guard requires exact immutable protected ranges")
        if self.kind is DialogueGuardKind.NOT_APPLICABLE:
            if (
                self.requirement is not DialogueRequirement.NOT_REQUIRED
                or self.reason != "no_audio"
                or self.source_audio_range is not None
                or self.protected_ranges
            ):
                raise DialogueGuardError("not-applicable candidate guard must be no_audio")
            return
        expected_kind = (
            DialogueGuardKind.REQUIRED
            if self.requirement is DialogueRequirement.COMPLETE
            else DialogueGuardKind.NOT_REQUIRED
        )
        if self.kind is not expected_kind or self.reason != _reason(self.kind):
            raise DialogueGuardError("candidate guard arm disagrees with requirement")
        if type(self.source_audio_range) is not TickRange:  # noqa: E721
            raise DialogueGuardError("audio-bearing guard requires a proven local audio range")
        for item in self.protected_ranges:
            if (
                item.source_id, item.source_sha256, item.clock_id, item.time_base
            ) != (
                self.source_id, self.source_sha256,
                self.source_audio_clock_id, self.source_audio_time_base,
            ) or not self.source_audio_range.contains(TickRange(item.in_tick, item.out_tick)):
                raise DialogueGuardError("protected range escapes its source/clock/local coverage")
        if any(
            left.out_tick >= right.in_tick
            for left, right in zip(self.protected_ranges, self.protected_ranges[1:])
        ):
            raise DialogueGuardError("protected ranges must be ordered, disjoint and non-adjacent")

    def to_mapping(self) -> dict[str, object]:
        coverage = self.source_audio_range
        return {
            "schema_version": "candidate-dialogue-guard-v1",
            "root_evidence_sha256": self.root_evidence_sha256,
            "candidate_evidence_sha256": self.candidate_evidence_sha256,
            "candidate_window_sha256": self.candidate_window_sha256,
            "window_plan_sha256": self.window_plan_sha256,
            "profile_sha256": self.profile_sha256,
            "guard_policy_sha256": self.guard_policy_sha256,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "source_audio_clock_id": self.source_audio_clock_id,
            "source_audio_time_base": {
                "numerator": self.source_audio_time_base.numerator,
                "denominator": self.source_audio_time_base.denominator,
            },
            "source_audio_range": None if coverage is None else {
                "start_pts": coverage.start_pts, "end_pts": coverage.end_pts,
            },
            "requirement": self.requirement.value,
            "kind": self.kind.value,
            "reason": self.reason,
            "protected_ranges": [item.to_mapping() for item in self.protected_ranges],
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def _reason(kind: DialogueGuardKind) -> str:
    if kind is DialogueGuardKind.NOT_APPLICABLE:
        return "no_audio"
    return (
        "blueprint_requires_complete_dialogue"
        if kind is DialogueGuardKind.REQUIRED
        else "blueprint_does_not_require_complete_dialogue"
    )


def _time(tick: int, base: TimeBase) -> Fraction:
    return Fraction(tick * base.numerator, base.denominator)


def _bind_producer(
    evidence: TranscriptSet | SpeechActivitySet,
    original: TranscriptSet | SpeechActivitySet,
    requirement: TimedSpeechProducerRequirement,
    bindings: tuple[CalibrationBinding, ...],
) -> None:
    context = evidence.context
    if (
        context.source_id, context.source_sha256, context.clock_id, context.time_base
    ) != (
        original.context.source_id, original.context.source_sha256,
        original.context.clock_id, original.context.time_base,
    ) or (
        context.producer_id, context.generation_policy_sha256, context.clock_id, context.time_base
    ) != (
        requirement.producer_id, requirement.generation_policy_sha256,
        requirement.clock_id, requirement.time_base,
    ):
        raise DialogueGuardError("candidate speech producer does not bind original root/profile")
    if (
        context.origin_tick < original.context.origin_tick
        or context.end_tick > original.context.end_tick
    ):
        raise DialogueGuardError("candidate speech context exceeds original source audio extent")
    matches = tuple(item for item in bindings if item.producer_id == requirement.producer_id)
    if len(matches) != 1:
        raise DialogueGuardError("candidate speech producer requires one exact calibration binding")
    binding = matches[0]
    # The existing registry owner binds detector_sha256 to model_sha256.
    if (
        binding.policy_sha256, binding.detector_sha256, binding.adapter_sha256,
        binding.calibration_record_sha256, binding.time_base, binding.active,
    ) != (
        requirement.generation_policy_sha256, requirement.model_sha256,
        requirement.adapter_sha256, requirement.calibration_record_sha256,
        requirement.time_base, True,
    ):
        raise DialogueGuardError("candidate speech model/adapter/calibration does not match profile")


def _covered_component(
    coverage: Coverage, start: Fraction, end: Fraction,
) -> TickRange:
    """Return only the continuous proved component containing the full window."""
    if coverage.outcome is CoverageOutcome.FAILED:
        raise DialogueGuardError("candidate speech coverage failed")
    base = coverage.time_base
    low, high = coverage.in_tick, coverage.out_tick
    if _time(low, base) > start or _time(high, base) < end:
        raise DialogueGuardError("candidate speech does not cover absolute window presentation")
    for gap in coverage.diagnostics:
        if _time(gap.in_tick, base) < end and start < _time(gap.out_tick, base):
            raise DialogueGuardError("candidate speech coverage contains an unproved gap")
        if _time(gap.out_tick, base) <= start:
            low = max(low, gap.out_tick)
        if _time(gap.in_tick, base) >= end:
            high = min(high, gap.in_tick)
    return TickRange(low, high)


def _validate_window(
    root: RootMediaEvidenceBundle, candidate: CandidateTimedEvidenceSet,
    plan: CandidateEvidenceWindowPlan,
) -> None:
    if (
        plan.outcome is not CandidateWindowOutcome.COMPLETE
        or plan.final_window != candidate.candidate_window
        or plan.final_assessment != candidate.window_assessment
        or not candidate.window_assessment.closed
    ):
        raise DialogueGuardError("candidate window/plan is not exactly closed")
    if candidate.window_assessment.sentence_completeness in {
        SentenceCompleteness.UNKNOWN, SentenceCompleteness.PARTIAL,
    }:
        raise DialogueGuardIndeterminateError("candidate sentence assessment is unknown or partial")
    window = candidate.candidate_window
    video = root.frame_pts_index.context
    if window.source_range != TickRange(video.origin_tick, video.end_tick):
        raise DialogueGuardError("candidate window source edges do not bind original root")
    for name in (
        "audio_sample_boundaries", "frame_pts_index", "shot_boundaries", "scene_boundaries",
        "visual_validity", "subtitle_cues",
    ):
        if getattr(candidate, name) != getattr(root, name):
            raise DialogueGuardError("candidate physical evidence must equal original root sets")
    transcript = candidate.transcript
    if transcript.truncated or transcript.boundary_touch_left or transcript.boundary_touch_right:
        raise DialogueGuardError("candidate transcript is truncated or boundary-touching")
    start = _time(window.current_range.start_pts, window.source_time_base)
    end = _time(window.current_range.end_pts, window.source_time_base)
    # Do not trust a claimed assessment when actual known speech crosses/touches
    # a local boundary. Only real source edges can exempt boundary contact.
    for records, context in (
        ((*transcript.words, *transcript.sentences), transcript.context),
        (candidate.speech_activity.segments, candidate.speech_activity.context),
    ):
        for record in records:
            low, high = _time(record.in_tick, context.time_base), _time(record.out_tick, context.time_base)
            if (
                window.current_range.start_pts != video.origin_tick and low <= start <= high
            ) or (
                window.current_range.end_pts != video.end_tick and low <= end <= high
            ):
                raise DialogueGuardError("candidate speech touches an unclosed local boundary")


def _transcript_ranges(
    transcript: TranscriptSet, audio: EvidenceContext,
    profile: TimedSpeechProfileRegistryEntry, requirement: DialogueRequirement,
) -> tuple[tuple[int, int], ...]:
    word_only = profile.kind is TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1
    if word_only and requirement is DialogueRequirement.COMPLETE:
        raise DialogueGuardIndeterminateError("word guard cannot satisfy complete dialogue")
    if transcript.completeness.segment is not EvidenceCompleteness.COMPLETE or any(
        value in {EvidenceCompleteness.PARTIAL, EvidenceCompleteness.FAILED}
        for value in (transcript.completeness.word, transcript.completeness.sentence)
    ):
        raise DialogueGuardIndeterminateError("candidate transcript completeness is partial or failed")
    if transcript.source_outcome in {
        TranscriptSourceOutcome.NO_SPEECH, TranscriptSourceOutcome.NO_LEXICAL_CONTENT,
    }:
        if requirement is DialogueRequirement.COMPLETE:
            raise DialogueGuardIndeterminateError("complete dialogue requires sentence evidence")
        return ()
    if transcript.source_outcome is not TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE:
        raise DialogueGuardError("candidate transcript outcome is not usable")
    expected_sentence = (
        EvidenceCompleteness.NOT_APPLICABLE if word_only else EvidenceCompleteness.COMPLETE
    )
    if (
        transcript.completeness.word is not EvidenceCompleteness.COMPLETE
        or transcript.completeness.sentence is not expected_sentence
        or not transcript.words
        or (bool(transcript.sentences) == word_only)
    ):
        raise DialogueGuardIndeterminateError("candidate transcript lacks profile-required completeness")
    words = group_transcript_words(transcript, audio, word_gap_tick=profile.guard_policy.word_gap_tick)
    return words + tuple((item.in_tick, item.out_tick) for item in transcript.sentences)


def derive_candidate_dialogue_guard(
    root: RootMediaEvidenceBundle,
    candidate: CandidateTimedEvidenceSet,
    plan: CandidateEvidenceWindowPlan,
    profile: TimedSpeechProfileRegistryEntry,
    requirement: DialogueRequirement,
) -> CandidateDialogueGuard:
    """Recompute local protection using original physical sets and real profile."""
    if (
        type(root) is not RootMediaEvidenceBundle  # noqa: E721
        or type(candidate) is not CandidateTimedEvidenceSet  # noqa: E721
        or type(plan) is not CandidateEvidenceWindowPlan  # noqa: E721
        or type(profile) is not TimedSpeechProfileRegistryEntry  # noqa: E721
        or type(requirement) is not DialogueRequirement  # noqa: E721
    ):
        raise DialogueGuardError("candidate guard requires exact typed inputs")
    _validate_window(root, candidate, plan)
    audio = root.audio_sample_boundaries.context
    policy = profile.guard_policy
    if (policy.source_audio_clock_id, policy.source_audio_time_base) != (audio.clock_id, audio.time_base):
        raise DialogueGuardError("candidate guard policy uses a foreign source audio clock")
    _bind_producer(candidate.transcript, root.transcript, profile.transcript_requirement, candidate.calibration_bindings)
    _bind_producer(candidate.speech_activity, root.speech_activity, profile.vad_requirement, candidate.calibration_bindings)
    coverage: TickRange | None = None
    protected: tuple[ProtectedAudioRange, ...] = ()
    if root.audio_sample_boundaries.source_outcome is AudioSourceOutcome.NOT_APPLICABLE:
        if requirement is DialogueRequirement.COMPLETE:
            raise DialogueGuardIndeterminateError("complete dialogue cannot be proven without audio")
        if (
            candidate.transcript.source_outcome is not TranscriptSourceOutcome.NOT_APPLICABLE
            or candidate.speech_activity.source_outcome is not SpeechSourceOutcome.NOT_APPLICABLE
        ):
            raise DialogueGuardError("no-audio guard requires explicitly not-applicable speech evidence")
        kind = DialogueGuardKind.NOT_APPLICABLE
    else:
        if root.audio_sample_boundaries.source_outcome is not AudioSourceOutcome.BOUNDARIES_AVAILABLE:
            raise DialogueGuardError("source audio boundaries are indeterminate")
        window = candidate.candidate_window
        start = _time(window.current_range.start_pts, window.source_time_base)
        end = _time(window.current_range.end_pts, window.source_time_base)
        transcript_range = _covered_component(candidate.transcript.coverage, start, end)
        vad_range = _covered_component(candidate.speech_activity.coverage, start, end)
        coverage = TickRange(max(transcript_range.start_pts, vad_range.start_pts), min(transcript_range.end_pts, vad_range.end_pts))
        ranges = _transcript_ranges(candidate.transcript, audio, profile, requirement)
        speech = candidate.speech_activity
        if speech.source_outcome is SpeechSourceOutcome.SPEECH_DETECTED:
            ranges += merge_speech_activity(speech, audio, vad_merge_gap_tick=policy.vad_merge_gap_tick)
        elif speech.source_outcome is not SpeechSourceOutcome.NONE_DETECTED:
            raise DialogueGuardError("candidate VAD outcome is not usable")
        protected = roll_protected_audio_ranges(
            ranges, audio, pre_roll_tick=policy.pre_roll_tick, post_roll_tick=policy.post_roll_tick,
        )
        if any(not coverage.contains(TickRange(item.in_tick, item.out_tick)) for item in protected):
            raise DialogueGuardError("speech protection rolls exceed proven local coverage")
        kind = DialogueGuardKind.REQUIRED if requirement is DialogueRequirement.COMPLETE else DialogueGuardKind.NOT_REQUIRED
    return CandidateDialogueGuard(
        root.canonical_hash, candidate.canonical_hash, candidate.candidate_window.canonical_hash,
        plan.canonical_hash, profile.canonical_hash, canonical_sha256(policy.to_mapping()),
        root.source_id, root.source_sha256, audio.clock_id, audio.time_base, coverage,
        requirement, kind, _reason(kind), protected,
    )


def verify_candidate_dialogue_guard(
    root: RootMediaEvidenceBundle,
    candidate: CandidateTimedEvidenceSet,
    plan: CandidateEvidenceWindowPlan,
    profile: TimedSpeechProfileRegistryEntry,
    requirement: DialogueRequirement,
    guard: CandidateDialogueGuard,
) -> None:
    """Recompute all content; a directly constructed guard grants no authority."""
    if type(guard) is not CandidateDialogueGuard or guard != derive_candidate_dialogue_guard(  # noqa: E721
        root, candidate, plan, profile, requirement,
    ):
        raise DialogueGuardError("candidate dialogue guard differs from recomputed evidence")
