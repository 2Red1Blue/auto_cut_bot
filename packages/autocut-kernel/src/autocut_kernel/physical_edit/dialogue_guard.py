"""Closed source-audio dialogue protection derived from timed evidence.

This module deliberately derives physical protected ranges only.  SenseVoice
word timing and FSMN-VAD can protect known speech, but cannot establish
semantic sentence completeness.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..media.root_evidence import (
    AudioSourceOutcome,
    EvidenceCompleteness,
    RootMediaEvidenceBundle,
    SpeechSourceOutcome,
    TranscriptSourceOutcome,
)
from ..media.types import TimeBase, canonical_sha256, require_pts, sha256_prefixed


class DialogueGuardError(ValueError):
    """Timed evidence cannot produce a closed dialogue guard."""


class DialogueGuardIndeterminateError(DialogueGuardError):
    """A complete-dialogue request lacks a calibrated sentence proof."""


class TimedSpeechProfileKind(str, Enum):
    SENSEVOICE_WORD_GUARD_V1 = "sensevoice_word_guard_v1"
    SENTENCE_BOUNDARY_GUARD_V1 = "sentence_boundary_guard_v1"


class DialogueRequirement(str, Enum):
    COMPLETE = "complete"
    NOT_REQUIRED = "not_required"


class DialogueGuardKind(str, Enum):
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class TimedSpeechProfile:
    """Registered, calibrated producer capability; never inferred from ASR data."""

    kind: TimedSpeechProfileKind
    profile_id: str
    profile_version: str
    producer_model_sha256: str
    adapter_sha256: str
    calibration_sha256: str

    def __post_init__(self) -> None:
        if type(self.kind) is not TimedSpeechProfileKind:  # noqa: E721
            raise DialogueGuardError("timed speech profile kind is invalid")
        for field_name in ("profile_id", "profile_version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise DialogueGuardError(f"timed speech profile {field_name} must be non-empty")
        for field_name in (
            "producer_model_sha256",
            "adapter_sha256",
            "calibration_sha256",
        ):
            try:
                sha256_prefixed(getattr(self, field_name), f"timed_speech_profile.{field_name}")
            except ValueError as error:
                raise DialogueGuardError(str(error)) from error

    def to_mapping(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "producer_model_sha256": self.producer_model_sha256,
            "adapter_sha256": self.adapter_sha256,
            "calibration_sha256": self.calibration_sha256,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class TimedSpeechGuardPolicy:
    """Frozen integer-tick grouping, merge, and protection-roll policy."""

    word_gap_tick: int
    vad_merge_gap_tick: int
    pre_roll_tick: int
    post_roll_tick: int

    def __post_init__(self) -> None:
        for field_name in (
            "word_gap_tick",
            "vad_merge_gap_tick",
            "pre_roll_tick",
            "post_roll_tick",
        ):
            value = require_pts(getattr(self, field_name), field_name)
            if value < 0:
                raise DialogueGuardError(f"{field_name} must be non-negative")

    def to_mapping(self) -> dict[str, int]:
        return {
            "word_gap_tick": self.word_gap_tick,
            "vad_merge_gap_tick": self.vad_merge_gap_tick,
            "pre_roll_tick": self.pre_roll_tick,
            "post_roll_tick": self.post_roll_tick,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ProtectedAudioRange:
    """A source-clock half-open range after policy-defined protection rolls."""

    source_id: str
    source_sha256: str
    clock_id: str
    time_base: TimeBase
    in_tick: int
    out_tick: int

    def __post_init__(self) -> None:
        if type(self.source_id) is not str or not self.source_id:  # noqa: E721
            raise DialogueGuardError("protected range source_id must be non-empty")
        try:
            sha256_prefixed(self.source_sha256, "protected_range.source_sha256")
        except ValueError as error:
            raise DialogueGuardError(str(error)) from error
        if type(self.clock_id) is not str or not self.clock_id:  # noqa: E721
            raise DialogueGuardError("protected range clock_id must be non-empty")
        if type(self.time_base) is not TimeBase:  # noqa: E721
            raise DialogueGuardError("protected range time_base must be exact")
        start = require_pts(self.in_tick, "protected_range.in_tick")
        end = require_pts(self.out_tick, "protected_range.out_tick")
        if start >= end:
            raise DialogueGuardError("protected range must satisfy in_tick < out_tick")

    def to_mapping(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "clock_id": self.clock_id,
            "time_base": {
                "numerator": self.time_base.numerator,
                "denominator": self.time_base.denominator,
            },
            "in_tick": self.in_tick,
            "out_tick": self.out_tick,
        }


@dataclass(frozen=True, slots=True)
class SourceDialogueGuardEvidence:
    """Closed `required|not_required|not_applicable` source dialogue union."""

    kind: DialogueGuardKind
    reason: str
    timed_speech_profile: TimedSpeechProfile | None
    timed_speech_policy_sha256: str | None
    transcript_set_sha256: str | None
    speech_activity_set_sha256: str | None
    protected_ranges: tuple[ProtectedAudioRange, ...] = ()

    def __post_init__(self) -> None:
        if type(self.kind) is not DialogueGuardKind:  # noqa: E721
            raise DialogueGuardError("dialogue guard kind is invalid")
        protected = tuple(self.protected_ranges)
        if not all(type(item) is ProtectedAudioRange for item in protected):  # noqa: E721
            raise DialogueGuardError("protected ranges must be exact ProtectedAudioRange records")
        if tuple(sorted(protected, key=lambda item: (item.in_tick, item.out_tick))) != protected:
            raise DialogueGuardError("protected ranges must be canonically ordered")
        if any(left.out_tick >= right.in_tick for left, right in zip(protected, protected[1:])):
            raise DialogueGuardError("protected ranges must be disjoint and non-adjacent")

        if self.kind is DialogueGuardKind.NOT_APPLICABLE:
            if (
                self.reason != "no_audio"
                or self.timed_speech_profile is not None
                or self.timed_speech_policy_sha256 is not None
                or self.transcript_set_sha256 is not None
                or self.speech_activity_set_sha256 is not None
                or protected
            ):
                raise DialogueGuardError("not_applicable dialogue guard is only no_audio")
            return

        expected_reason = (
            "blueprint_requires_complete_dialogue"
            if self.kind is DialogueGuardKind.REQUIRED
            else "blueprint_does_not_require_complete_dialogue"
        )
        if self.reason != expected_reason:
            raise DialogueGuardError("dialogue guard reason does not match its closed arm")
        if type(self.timed_speech_profile) is not TimedSpeechProfile:  # noqa: E721
            raise DialogueGuardError("audio dialogue guard requires a timed speech profile")
        for field_name in (
            "timed_speech_policy_sha256",
            "transcript_set_sha256",
            "speech_activity_set_sha256",
        ):
            value = getattr(self, field_name)
            try:
                sha256_prefixed(value, f"dialogue_guard.{field_name}")
            except ValueError as error:
                raise DialogueGuardError(str(error)) from error

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {"kind": self.kind.value, "reason": self.reason}
        if self.kind is not DialogueGuardKind.NOT_APPLICABLE:
            assert self.timed_speech_profile is not None
            result.update(
                {
                    "timed_speech_profile": self.timed_speech_profile.to_mapping(),
                    "timed_speech_policy_sha256": self.timed_speech_policy_sha256,
                    "transcript_set_sha256": self.transcript_set_sha256,
                    "speech_activity_set_sha256": self.speech_activity_set_sha256,
                    "protected_ranges": [item.to_mapping() for item in self.protected_ranges],
                }
            )
        return result

    @property
    def protected_ranges_sha256(self) -> str:
        return canonical_sha256([item.to_mapping() for item in self.protected_ranges])

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def _validate_source_range(
    evidence: RootMediaEvidenceBundle, start: int, end: int, field_name: str
) -> None:
    context = evidence.audio_sample_boundaries.context
    if start < context.origin_tick or end > context.end_tick or start >= end:
        raise DialogueGuardError(f"{field_name} is outside the source audio clock")


def _validate_words(evidence: RootMediaEvidenceBundle) -> None:
    context = evidence.transcript.context
    previous_end: int | None = None
    for word in evidence.transcript.words:
        if (
            word.source_id != context.source_id
            or word.source_sha256 != context.source_sha256
            or word.clock_id != context.clock_id
            or word.time_base != context.time_base
        ):
            raise DialogueGuardError("word timing is not bound to the source audio clock")
        _validate_source_range(evidence, word.in_tick, word.out_tick, "word timing")
        if previous_end is not None and previous_end > word.in_tick:
            raise DialogueGuardError("word timings must be monotonic and non-overlapping")
        previous_end = word.out_tick


def derive_utterance_ranges(
    evidence: RootMediaEvidenceBundle, policy: TimedSpeechGuardPolicy
) -> tuple[tuple[int, int], ...]:
    """Group ordered words; exactly-threshold gaps remain in the same utterance."""
    _validate_words(evidence)
    words = evidence.transcript.words
    if not words:
        return ()
    ranges: list[tuple[int, int]] = []
    start, end = words[0].in_tick, words[0].out_tick
    for word in words[1:]:
        if word.in_tick - end > policy.word_gap_tick:
            ranges.append((start, end))
            start = word.in_tick
        end = word.out_tick
    ranges.append((start, end))
    return tuple(ranges)


def merge_vad_ranges(
    evidence: RootMediaEvidenceBundle, policy: TimedSpeechGuardPolicy
) -> tuple[tuple[int, int], ...]:
    """Merge FSMN-VAD activity with the frozen inclusive gap policy."""
    context = evidence.speech_activity.context
    ranges: list[tuple[int, int]] = []
    previous_end: int | None = None
    for segment in evidence.speech_activity.segments:
        if (
            segment.source_id != context.source_id
            or segment.source_sha256 != context.source_sha256
            or segment.clock_id != context.clock_id
            or segment.time_base != context.time_base
        ):
            raise DialogueGuardError("VAD timing is not bound to the source audio clock")
        _validate_source_range(evidence, segment.in_tick, segment.out_tick, "VAD timing")
        if previous_end is not None and previous_end > segment.in_tick:
            raise DialogueGuardError("VAD timings must be monotonic and non-overlapping")
        previous_end = segment.out_tick
        if ranges and segment.in_tick - ranges[-1][1] <= policy.vad_merge_gap_tick:
            ranges[-1] = (ranges[-1][0], segment.out_tick)
        else:
            ranges.append((segment.in_tick, segment.out_tick))
    return tuple(ranges)


def _merge_and_roll(
    evidence: RootMediaEvidenceBundle,
    ranges: tuple[tuple[int, int], ...],
    policy: TimedSpeechGuardPolicy,
) -> tuple[ProtectedAudioRange, ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    context = evidence.audio_sample_boundaries.context
    rolled = [
        (
            max(context.origin_tick, start - policy.pre_roll_tick),
            min(context.end_tick, end + policy.post_roll_tick),
        )
        for start, end in merged
    ]
    final: list[tuple[int, int]] = []
    for start, end in rolled:
        if final and start <= final[-1][1]:
            final[-1] = (final[-1][0], max(final[-1][1], end))
        else:
            final.append((start, end))
    return tuple(
        ProtectedAudioRange(
            context.source_id,
            context.source_sha256,
            context.clock_id,
            context.time_base,
            start,
            end,
        )
        for start, end in final
    )


def _require_audio_evidence(evidence: RootMediaEvidenceBundle) -> None:
    if evidence.audio_sample_boundaries.source_outcome is not AudioSourceOutcome.BOUNDARIES_AVAILABLE:
        raise DialogueGuardError("audio dialogue guard requires exact audio sample boundaries")
    if evidence.transcript.source_outcome is TranscriptSourceOutcome.NOT_APPLICABLE:
        raise DialogueGuardError("audio-bearing transcript cannot be not_applicable")
    if evidence.speech_activity.source_outcome is SpeechSourceOutcome.NOT_APPLICABLE:
        raise DialogueGuardError("audio-bearing VAD cannot be not_applicable")


def derive_dialogue_guard(
    evidence: RootMediaEvidenceBundle,
    profile: TimedSpeechProfile,
    policy: TimedSpeechGuardPolicy,
    requirement: DialogueRequirement,
) -> SourceDialogueGuardEvidence:
    """Derive the only valid guard arm from profile capabilities and source evidence."""
    if type(evidence) is not RootMediaEvidenceBundle:  # noqa: E721
        raise DialogueGuardError("dialogue guard requires a RootMediaEvidenceBundle")
    if type(profile) is not TimedSpeechProfile:  # noqa: E721
        raise DialogueGuardError("dialogue guard requires an explicit timed speech profile")
    if type(policy) is not TimedSpeechGuardPolicy:  # noqa: E721
        raise DialogueGuardError("dialogue guard requires an explicit timed speech policy")
    if type(requirement) is not DialogueRequirement:  # noqa: E721
        raise DialogueGuardError("dialogue guard requirement is invalid")

    if evidence.audio_sample_boundaries.source_outcome is AudioSourceOutcome.NOT_APPLICABLE:
        if requirement is DialogueRequirement.COMPLETE:
            raise DialogueGuardIndeterminateError("complete dialogue cannot be proven without audio")
        return SourceDialogueGuardEvidence(
            DialogueGuardKind.NOT_APPLICABLE, "no_audio", None, None, None, None
        )

    _require_audio_evidence(evidence)
    transcript = evidence.transcript
    speech = evidence.speech_activity
    utterances: tuple[tuple[int, int], ...] = ()
    sentence_ranges: tuple[tuple[int, int], ...] = ()

    if profile.kind is TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1:
        if transcript.source_outcome is TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE:
            if (
                transcript.completeness.word is not EvidenceCompleteness.COMPLETE
                or transcript.completeness.sentence is not EvidenceCompleteness.NOT_APPLICABLE
                or not transcript.words
                or transcript.sentences
            ):
                raise DialogueGuardError("word guard requires complete words and no sentence evidence")
            utterances = derive_utterance_ranges(evidence, policy)
        elif transcript.source_outcome not in {
            TranscriptSourceOutcome.NO_LEXICAL_CONTENT,
            TranscriptSourceOutcome.NO_SPEECH,
        }:
            raise DialogueGuardError("word guard transcript outcome is indeterminate")
        if requirement is DialogueRequirement.COMPLETE:
            raise DialogueGuardIndeterminateError(
                "sensevoice_word_guard_v1 cannot satisfy complete dialogue"
            )
    else:
        if transcript.source_outcome is TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE:
            if (
                transcript.completeness.word is not EvidenceCompleteness.COMPLETE
                or transcript.completeness.sentence is not EvidenceCompleteness.COMPLETE
                or not transcript.words
                or not transcript.sentences
            ):
                raise DialogueGuardIndeterminateError(
                    "sentence guard requires complete word and sentence proof"
                )
            _validate_words(evidence)
            utterances = derive_utterance_ranges(evidence, policy)
            sentence_ranges = tuple((item.in_tick, item.out_tick) for item in transcript.sentences)
        elif (
            requirement is DialogueRequirement.NOT_REQUIRED
            and transcript.source_outcome
            in {TranscriptSourceOutcome.NO_LEXICAL_CONTENT, TranscriptSourceOutcome.NO_SPEECH}
        ):
            pass
        else:
            raise DialogueGuardIndeterminateError("sentence guard requires transcript evidence")

    if speech.source_outcome is SpeechSourceOutcome.INDETERMINATE:
        raise DialogueGuardError("VAD outcome is indeterminate")
    if speech.source_outcome is SpeechSourceOutcome.SPEECH_DETECTED:
        vad_ranges = merge_vad_ranges(evidence, policy)
    elif speech.source_outcome is SpeechSourceOutcome.NONE_DETECTED:
        vad_ranges = ()
    else:
        raise DialogueGuardError("audio-bearing VAD cannot be not_applicable")

    ranges = _merge_and_roll(evidence, utterances + sentence_ranges + vad_ranges, policy)
    kind = (
        DialogueGuardKind.REQUIRED
        if requirement is DialogueRequirement.COMPLETE
        else DialogueGuardKind.NOT_REQUIRED
    )
    return SourceDialogueGuardEvidence(
        kind,
        (
            "blueprint_requires_complete_dialogue"
            if kind is DialogueGuardKind.REQUIRED
            else "blueprint_does_not_require_complete_dialogue"
        ),
        profile,
        policy.canonical_hash,
        transcript.canonical_hash,
        speech.canonical_hash,
        ranges,
    )


__all__ = [
    "DialogueGuardError",
    "DialogueGuardIndeterminateError",
    "DialogueGuardKind",
    "DialogueRequirement",
    "ProtectedAudioRange",
    "SourceDialogueGuardEvidence",
    "TimedSpeechGuardPolicy",
    "TimedSpeechProfile",
    "TimedSpeechProfileKind",
    "derive_dialogue_guard",
    "derive_utterance_ranges",
    "merge_vad_ranges",
]
