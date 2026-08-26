"""Immutable media-preflight facts required by the Stage 4 physical editor.

These records intentionally live with preflight rather than ``physical_edit``:
they are predecessor facts, not a caller supplied edit policy.  The command
reads the profile registry member before it creates an admission and persists
only hashes of sibling facts, avoiding a self-referential Receipt hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from typing import cast

from .physical_root import PhysicalRootMediaEvidence
from .root_evidence import (
    AudioSourceOutcome,
    EvidenceCompleteness,
    MediaKind,
    RootMediaEvidenceBundle,
    SpeechActivitySet,
    TranscriptSet,
)
from .timed_evidence import CalibrationBinding
from .types import MediaValidationError, TickRange, TimeBase, canonical_sha256, sha256_prefixed


class Stage4PredecessorError(MediaValidationError):
    """A proposed Stage 4 predecessor fact is incomplete or forged."""


class TimedSpeechProfileKind(str, Enum):
    SENSEVOICE_WORD_GUARD_V1 = "sensevoice_word_guard_v1"
    SENTENCE_BOUNDARY_GUARD_V1 = "sentence_boundary_guard_v1"


class TimedSpeechCapability(str, Enum):
    KNOWN_SPEECH_ONLY = "known_speech_only"
    COMPLETE_DIALOGUE = "complete_dialogue"


class PresentationNonOverlapMedia(str, Enum):
    AUDIO = "audio"
    VIDEO = "video"


class PresentationNonOverlapPosition(str, Enum):
    LEADING = "leading"
    TRAILING = "trailing"
    INTERNAL_GAP = "internal_gap"


class PresentationSegmentContinuity(str, Enum):
    """Whether a source-presentation segment is decoded coverage or a proved gap."""

    CONTINUOUS_DECODED = "continuous_decoded"
    DECLARED_GAP = "declared_gap"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise Stage4PredecessorError(f"{name} must be non-empty text")
    return value


def _sha(value: object, name: str) -> str:
    try:
        return sha256_prefixed(value, name)
    except ValueError as error:
        raise Stage4PredecessorError(str(error)) from error


def _time_base(value: object, name: str) -> TimeBase:
    if type(value) is not TimeBase:  # noqa: E721
        raise Stage4PredecessorError(f"{name} must be an exact TimeBase")
    return value


@dataclass(frozen=True, slots=True)
class RationalPresentationInterval:
    """A closed source-presentation interval represented without float seconds."""

    start_numerator: int
    start_denominator: int
    end_numerator: int
    end_denominator: int

    def __post_init__(self) -> None:
        for name in (
            "start_numerator",
            "start_denominator",
            "end_numerator",
            "end_denominator",
        ):
            if type(getattr(self, name)) is not int:  # noqa: E721
                raise Stage4PredecessorError(f"{name} must be an integer")
        if self.start_denominator <= 0 or self.end_denominator <= 0:
            raise Stage4PredecessorError("presentation denominators must be positive")
        start, end = self.start, self.end
        if start >= end:
            raise Stage4PredecessorError("presentation interval must be non-empty")
        if (self.start_numerator, self.start_denominator) != (start.numerator, start.denominator):
            raise Stage4PredecessorError("presentation interval start is not canonical")
        if (self.end_numerator, self.end_denominator) != (end.numerator, end.denominator):
            raise Stage4PredecessorError("presentation interval end is not canonical")

    @property
    def start(self) -> Fraction:
        return Fraction(self.start_numerator, self.start_denominator)

    @property
    def end(self) -> Fraction:
        return Fraction(self.end_numerator, self.end_denominator)

    @classmethod
    def from_fractions(cls, start: Fraction, end: Fraction) -> RationalPresentationInterval:
        return cls(start.numerator, start.denominator, end.numerator, end.denominator)

    def to_mapping(self) -> dict[str, int]:
        return {
            "end_denominator": self.end_denominator,
            "end_numerator": self.end_numerator,
            "start_denominator": self.start_denominator,
            "start_numerator": self.start_numerator,
        }


@dataclass(frozen=True, slots=True)
class PresentationNonOverlap:
    media: PresentationNonOverlapMedia
    position: PresentationNonOverlapPosition
    presentation_interval: RationalPresentationInterval

    def __post_init__(self) -> None:
        if type(self.media) is not PresentationNonOverlapMedia:  # noqa: E721
            raise Stage4PredecessorError("non-overlap media is invalid")
        if type(self.position) is not PresentationNonOverlapPosition:  # noqa: E721
            raise Stage4PredecessorError("non-overlap position is invalid")
        if type(self.presentation_interval) is not RationalPresentationInterval:  # noqa: E721
            raise Stage4PredecessorError("non-overlap interval is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "media": self.media.value,
            "position": self.position.value,
            "presentation_interval": self.presentation_interval.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class TimedSpeechProducerRequirement:
    """One producer's independently calibrated immutable registry requirement."""

    producer_id: str
    generation_policy_sha256: str
    model_sha256: str
    adapter_sha256: str
    calibration_record_sha256: str
    clock_id: str
    time_base: TimeBase
    producer_kind: str
    inference_kind: str

    def __post_init__(self) -> None:
        _text(self.producer_id, "producer requirement producer_id")
        _text(self.clock_id, "producer requirement clock_id")
        expected_inference = {
            "asr": "sensevoice-word-timestamp",
            "vad": "fsmn-vad-direct",
        }
        if self.producer_kind not in expected_inference:
            raise Stage4PredecessorError("producer requirement producer_kind is invalid")
        if self.inference_kind != expected_inference[self.producer_kind]:
            raise Stage4PredecessorError("producer requirement inference_kind is invalid")
        for name in (
            "generation_policy_sha256",
            "model_sha256",
            "adapter_sha256",
            "calibration_record_sha256",
        ):
            _sha(getattr(self, name), f"producer requirement {name}")
        _time_base(self.time_base, "producer requirement time_base")

    def to_mapping(self) -> dict[str, object]:
        return {
            "adapter_sha256": self.adapter_sha256,
            "calibration_record_sha256": self.calibration_record_sha256,
            "clock_id": self.clock_id,
            "generation_policy_sha256": self.generation_policy_sha256,
            "model_sha256": self.model_sha256,
            "producer_id": self.producer_id,
            "producer_kind": self.producer_kind,
            "inference_kind": self.inference_kind,
            "time_base": {"denominator": self.time_base.denominator, "numerator": self.time_base.numerator},
        }


@dataclass(frozen=True, slots=True)
class TimedSpeechGuardPolicy:
    policy_sha256: str
    source_audio_clock_id: str
    source_audio_time_base: TimeBase
    word_gap_tick: int
    vad_merge_gap_tick: int
    pre_roll_tick: int
    post_roll_tick: int

    def __post_init__(self) -> None:
        _sha(self.policy_sha256, "guard policy hash")
        _text(self.source_audio_clock_id, "guard policy source audio clock")
        _time_base(self.source_audio_time_base, "guard policy source audio time base")
        for name in ("word_gap_tick", "vad_merge_gap_tick", "pre_roll_tick", "post_roll_tick"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:  # noqa: E721
                raise Stage4PredecessorError(f"guard policy {name} must be non-negative")

    def to_mapping(self) -> dict[str, object]:
        return {
            "policy_sha256": self.policy_sha256,
            "post_roll_tick": self.post_roll_tick,
            "pre_roll_tick": self.pre_roll_tick,
            "source_audio_clock_id": self.source_audio_clock_id,
            "source_audio_time_base": {"denominator": self.source_audio_time_base.denominator, "numerator": self.source_audio_time_base.numerator},
            "vad_merge_gap_tick": self.vad_merge_gap_tick,
            "word_gap_tick": self.word_gap_tick,
        }


@dataclass(frozen=True, slots=True)
class TimedSpeechProfileRegistryEntry:
    """Authority-owned profile.  This object is valid only when store-read."""

    profile_id: str
    profile_version: str
    kind: TimedSpeechProfileKind
    capability: TimedSpeechCapability
    transcript_requirement: TimedSpeechProducerRequirement
    vad_requirement: TimedSpeechProducerRequirement
    guard_policy: TimedSpeechGuardPolicy
    registry_contract_sha256: str

    def __post_init__(self) -> None:
        _text(self.profile_id, "profile_id")
        _text(self.profile_version, "profile_version")
        if type(self.kind) is not TimedSpeechProfileKind or type(self.capability) is not TimedSpeechCapability:  # noqa: E721
            raise Stage4PredecessorError("profile kind/capability is invalid")
        if type(self.transcript_requirement) is not TimedSpeechProducerRequirement or type(self.vad_requirement) is not TimedSpeechProducerRequirement:  # noqa: E721
            raise Stage4PredecessorError("profile requires separate transcript and VAD producers")
        if type(self.guard_policy) is not TimedSpeechGuardPolicy:  # noqa: E721
            raise Stage4PredecessorError("profile guard policy is invalid")
        _sha(self.registry_contract_sha256, "registry contract hash")
        expected = (
            TimedSpeechCapability.KNOWN_SPEECH_ONLY
            if self.kind is TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1
            else TimedSpeechCapability.COMPLETE_DIALOGUE
        )
        if self.capability is not expected:
            raise Stage4PredecessorError("profile kind cannot claim another capability")
        if (
            self.transcript_requirement.producer_id == self.vad_requirement.producer_id
            or self.transcript_requirement.producer_kind != "asr"
            or self.vad_requirement.producer_kind != "vad"
            or self.transcript_requirement.inference_kind == self.vad_requirement.inference_kind
            or self.transcript_requirement.model_sha256 == self.vad_requirement.model_sha256
            or self.transcript_requirement.calibration_record_sha256
            == self.vad_requirement.calibration_record_sha256
        ):
            raise Stage4PredecessorError("profile requires distinct ASR and VAD identities")
        if (
            self.transcript_requirement.clock_id != self.vad_requirement.clock_id
            or self.transcript_requirement.time_base != self.vad_requirement.time_base
            or self.guard_policy.source_audio_clock_id != self.transcript_requirement.clock_id
            or self.guard_policy.source_audio_time_base != self.transcript_requirement.time_base
        ):
            raise Stage4PredecessorError("profile producers and guard policy must share one audio clock")

    def to_mapping(self) -> dict[str, object]:
        return {
            "capability": self.capability.value,
            "guard_policy": self.guard_policy.to_mapping(),
            "kind": self.kind.value,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "registry_contract_sha256": self.registry_contract_sha256,
            "transcript_requirement": self.transcript_requirement.to_mapping(),
            "vad_requirement": self.vad_requirement.to_mapping(),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def _closed_mapping(value: object, fields: frozenset[str], name: str) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise Stage4PredecessorError(f"{name} must be an object")
    mapping = cast(dict[str, object], value)
    if frozenset(mapping) != fields:
        raise Stage4PredecessorError(f"{name} does not match its closed schema")
    return mapping


def _decode_time_base(value: object, name: str) -> TimeBase:
    mapping = _closed_mapping(value, frozenset({"numerator", "denominator"}), name)
    try:
        return TimeBase(cast(int, mapping["numerator"]), cast(int, mapping["denominator"]))
    except (TypeError, ValueError) as error:
        raise Stage4PredecessorError(f"{name} is invalid") from error


def _decode_requirement(value: object, name: str) -> TimedSpeechProducerRequirement:
    mapping = _closed_mapping(
        value,
        frozenset(
            {
                "adapter_sha256", "calibration_record_sha256", "clock_id",
                "generation_policy_sha256", "inference_kind", "model_sha256", "producer_id",
                "producer_kind", "time_base",
            }
        ),
        name,
    )
    return TimedSpeechProducerRequirement(
        adapter_sha256=cast(str, mapping["adapter_sha256"]),
        calibration_record_sha256=cast(str, mapping["calibration_record_sha256"]),
        clock_id=cast(str, mapping["clock_id"]),
        generation_policy_sha256=cast(str, mapping["generation_policy_sha256"]),
        model_sha256=cast(str, mapping["model_sha256"]),
        producer_id=cast(str, mapping["producer_id"]),
        producer_kind=cast(str, mapping["producer_kind"]),
        inference_kind=cast(str, mapping["inference_kind"]),
        time_base=_decode_time_base(mapping["time_base"], f"{name}.time_base"),
    )


def decode_timed_speech_profile_registry_entry(value: object) -> TimedSpeechProfileRegistryEntry:
    """Decode the closed registry-member payload at the Store consumer edge."""
    mapping = _closed_mapping(
        value,
        frozenset(
            {
                "capability", "guard_policy", "kind", "profile_id", "profile_version",
                "registry_contract_sha256", "transcript_requirement", "vad_requirement",
            }
        ),
        "timed speech profile registry entry",
    )
    policy = _closed_mapping(
        mapping["guard_policy"],
        frozenset(
            {
                "policy_sha256", "post_roll_tick", "pre_roll_tick", "source_audio_clock_id",
                "source_audio_time_base", "vad_merge_gap_tick", "word_gap_tick",
            }
        ),
        "timed speech guard policy",
    )
    try:
        return TimedSpeechProfileRegistryEntry(
            profile_id=cast(str, mapping["profile_id"]),
            profile_version=cast(str, mapping["profile_version"]),
            kind=TimedSpeechProfileKind(cast(str, mapping["kind"])),
            capability=TimedSpeechCapability(cast(str, mapping["capability"])),
            transcript_requirement=_decode_requirement(
                mapping["transcript_requirement"], "transcript producer requirement"
            ),
            vad_requirement=_decode_requirement(mapping["vad_requirement"], "VAD producer requirement"),
            guard_policy=TimedSpeechGuardPolicy(
                policy_sha256=cast(str, policy["policy_sha256"]),
                source_audio_clock_id=cast(str, policy["source_audio_clock_id"]),
                source_audio_time_base=_decode_time_base(
                    policy["source_audio_time_base"], "timed speech guard policy time base"
                ),
                word_gap_tick=cast(int, policy["word_gap_tick"]),
                vad_merge_gap_tick=cast(int, policy["vad_merge_gap_tick"]),
                pre_roll_tick=cast(int, policy["pre_roll_tick"]),
                post_roll_tick=cast(int, policy["post_roll_tick"]),
            ),
            registry_contract_sha256=cast(str, mapping["registry_contract_sha256"]),
        )
    except (TypeError, ValueError) as error:
        raise Stage4PredecessorError("timed speech profile registry payload is invalid") from error


def _requirement_matches(
    requirement: TimedSpeechProducerRequirement,
    evidence: TranscriptSet | SpeechActivitySet,
    binding: CalibrationBinding,
    label: str,
) -> None:
    context = evidence.context
    if (
        context.producer_id != requirement.producer_id
        or context.generation_policy_sha256 != requirement.generation_policy_sha256
        or context.clock_id != requirement.clock_id
        or context.time_base != requirement.time_base
    ):
        raise Stage4PredecessorError(f"{label} producer requirement does not match evidence")
    if (
        binding.producer_id != requirement.producer_id
        or binding.adapter_sha256 != requirement.adapter_sha256
        or binding.detector_sha256 != requirement.model_sha256
        or binding.calibration_record_sha256 != requirement.calibration_record_sha256
        or binding.time_base != requirement.time_base
        or not binding.active
    ):
        raise Stage4PredecessorError(f"{label} calibration requirement does not match evidence")


@dataclass(frozen=True, slots=True)
class TimedSpeechProfileAdmission:
    """Preflight fact; consumers must replay this against committed evidence."""

    registry_member_content_hash: str
    registry_entry_sha256: str
    root_evidence_sha256: str
    transcript_evidence_sha256: str
    vad_evidence_sha256: str
    transcript_calibration_sha256: str
    vad_calibration_sha256: str
    source_id: str
    source_sha256: str
    source_audio_clock_id: str
    source_audio_time_base: TimeBase
    words_complete: bool
    sentences_complete: bool
    vad_complete: bool
    capability: TimedSpeechCapability

    def __post_init__(self) -> None:
        for name in (
            "registry_member_content_hash", "registry_entry_sha256", "root_evidence_sha256",
            "transcript_evidence_sha256", "vad_evidence_sha256", "transcript_calibration_sha256",
            "vad_calibration_sha256", "source_sha256",
        ):
            _sha(getattr(self, name), name)
        _text(self.source_id, "admission source_id")
        _text(self.source_audio_clock_id, "admission source audio clock")
        _time_base(self.source_audio_time_base, "admission source audio time base")
        if type(self.capability) is not TimedSpeechCapability:  # noqa: E721
            raise Stage4PredecessorError("admission capability is invalid")
        for name in ("words_complete", "sentences_complete", "vad_complete"):
            if type(getattr(self, name)) is not bool:  # noqa: E721
                raise Stage4PredecessorError(f"admission {name} must be a boolean")

    def to_mapping(self) -> dict[str, object]:
        return {
            "capability": self.capability.value,
            "registry_entry_sha256": self.registry_entry_sha256,
            "registry_member_content_hash": self.registry_member_content_hash,
            "root_evidence_sha256": self.root_evidence_sha256,
            "sentences_complete": self.sentences_complete,
            "source_audio_clock_id": self.source_audio_clock_id,
            "source_audio_time_base": {"denominator": self.source_audio_time_base.denominator, "numerator": self.source_audio_time_base.numerator},
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "transcript_calibration_sha256": self.transcript_calibration_sha256,
            "transcript_evidence_sha256": self.transcript_evidence_sha256,
            "vad_calibration_sha256": self.vad_calibration_sha256,
            "vad_complete": self.vad_complete,
            "vad_evidence_sha256": self.vad_evidence_sha256,
            "words_complete": self.words_complete,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def admit_timed_speech_profile(
    entry: TimedSpeechProfileRegistryEntry,
    registry_member_content_hash: str,
    root: RootMediaEvidenceBundle,
    bindings: tuple[CalibrationBinding, ...],
) -> TimedSpeechProfileAdmission:
    """Recompute profile admission from committed evidence; no caller pass flag exists."""
    if type(entry) is not TimedSpeechProfileRegistryEntry or type(root) is not RootMediaEvidenceBundle:  # noqa: E721
        raise Stage4PredecessorError("profile admission requires exact registry and root evidence")
    _sha(registry_member_content_hash, "registry member content hash")
    if root.audio_sample_boundaries.source_outcome is AudioSourceOutcome.NOT_APPLICABLE:
        raise Stage4PredecessorError("timed speech profile admission requires source audio")
    exact_bindings = tuple(bindings)
    if any(type(item) is not CalibrationBinding for item in exact_bindings):  # noqa: E721
        raise Stage4PredecessorError("profile admission calibration bindings are invalid")
    binding_by_producer = {item.producer_id: item for item in exact_bindings}
    if len(binding_by_producer) != len(exact_bindings):
        raise Stage4PredecessorError("profile admission calibration producer identities collide")
    try:
        transcript_binding = binding_by_producer[entry.transcript_requirement.producer_id]
        vad_binding = binding_by_producer[entry.vad_requirement.producer_id]
    except KeyError as error:
        raise Stage4PredecessorError("profile admission lost a required producer calibration") from error
    _requirement_matches(entry.transcript_requirement, root.transcript, transcript_binding, "transcript")
    _requirement_matches(entry.vad_requirement, root.speech_activity, vad_binding, "VAD")
    transcript = root.transcript
    speech = root.speech_activity
    words_complete = transcript.completeness.word is EvidenceCompleteness.COMPLETE
    sentences_complete = transcript.completeness.sentence is EvidenceCompleteness.COMPLETE
    vad_complete = speech.coverage.outcome.value == "complete"
    if not vad_complete:
        raise Stage4PredecessorError("VAD coverage is not complete")
    if entry.kind is TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1:
        if not words_complete or sentences_complete or transcript.sentences:
            raise Stage4PredecessorError("word guard requires complete words and no sentence claim")
    elif not words_complete or not sentences_complete or not transcript.sentences:
        raise Stage4PredecessorError("sentence guard requires complete words and sentences")
    return TimedSpeechProfileAdmission(
        registry_member_content_hash=registry_member_content_hash,
        registry_entry_sha256=entry.canonical_hash,
        root_evidence_sha256=root.canonical_hash,
        transcript_evidence_sha256=transcript.canonical_hash,
        vad_evidence_sha256=speech.canonical_hash,
        transcript_calibration_sha256=transcript_binding.canonical_hash,
        vad_calibration_sha256=vad_binding.canonical_hash,
        source_id=root.source_id,
        source_sha256=root.source_sha256,
        source_audio_clock_id=root.audio_sample_boundaries.context.clock_id,
        source_audio_time_base=root.audio_sample_boundaries.context.time_base,
        words_complete=words_complete,
        sentences_complete=sentences_complete,
        vad_complete=vad_complete,
        capability=entry.capability,
    )


@dataclass(frozen=True, slots=True)
class PresentationTrackSegment:
    """One source-native range and its absolute (never rebased) presentation range."""

    stream_tick_range: TickRange
    presentation_interval: RationalPresentationInterval
    decoded_boundary_sequence_sha256: str
    continuity: PresentationSegmentContinuity

    def __post_init__(self) -> None:
        if type(self.stream_tick_range) is not TickRange:  # noqa: E721
            raise Stage4PredecessorError("presentation segment stream range is invalid")
        if type(self.presentation_interval) is not RationalPresentationInterval:  # noqa: E721
            raise Stage4PredecessorError("presentation segment interval is invalid")
        _sha(self.decoded_boundary_sequence_sha256, "presentation segment boundary hash")
        if type(self.continuity) is not PresentationSegmentContinuity:  # noqa: E721
            raise Stage4PredecessorError("presentation segment continuity is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "continuity": self.continuity.value,
            "decoded_boundary_sequence_sha256": self.decoded_boundary_sequence_sha256,
            "presentation_interval": self.presentation_interval.to_mapping(),
            "stream_tick_range": {
                "end_tick": self.stream_tick_range.end_pts,
                "start_tick": self.stream_tick_range.start_pts,
            },
        }


def _absolute_presentation(tick: int, time_base: TimeBase) -> Fraction:
    return Fraction(tick * time_base.numerator, time_base.denominator)


@dataclass(frozen=True, slots=True)
class PresentationTrack:
    """Complete source-presentation evidence for one selected decoded stream."""

    media_kind: MediaKind
    stream_index: int
    clock_id: str
    time_base: TimeBase
    origin_tick: int
    end_tick: int
    coverage_outcome: EvidenceCompleteness
    endpoint_proof: str
    index_sha256: str
    segments: tuple[PresentationTrackSegment, ...]

    def __post_init__(self) -> None:
        if type(self.media_kind) is not MediaKind:  # noqa: E721
            raise Stage4PredecessorError("presentation track media kind is invalid")
        if type(self.stream_index) is not int or self.stream_index < 0:  # noqa: E721
            raise Stage4PredecessorError("presentation track stream index is invalid")
        _text(self.clock_id, "presentation track clock id")
        _time_base(self.time_base, "presentation track time base")
        if type(self.origin_tick) is not int or type(self.end_tick) is not int or self.origin_tick >= self.end_tick:  # noqa: E721
            raise Stage4PredecessorError("presentation track source range is invalid")
        if self.coverage_outcome is not EvidenceCompleteness.COMPLETE:
            raise Stage4PredecessorError("presentation track must have complete coverage")
        if self.endpoint_proof != "decoded_start_and_end":
            raise Stage4PredecessorError("presentation track lacks decoded endpoint proof")
        _sha(self.index_sha256, "presentation track index hash")
        segments = tuple(self.segments)
        if not segments or any(type(item) is not PresentationTrackSegment for item in segments):  # noqa: E721
            raise Stage4PredecessorError("presentation track segments are invalid")
        previous: PresentationTrackSegment | None = None
        for segment in segments:
            expected = RationalPresentationInterval.from_fractions(
                _absolute_presentation(segment.stream_tick_range.start_pts, self.time_base),
                _absolute_presentation(segment.stream_tick_range.end_pts, self.time_base),
            )
            if segment.presentation_interval != expected:
                raise Stage4PredecessorError("presentation segment does not preserve source PTS")
            if previous is not None:
                if previous.stream_tick_range.end_pts != segment.stream_tick_range.start_pts:
                    raise Stage4PredecessorError("presentation track has an undeclared source discontinuity")
                if previous.presentation_interval.end != segment.presentation_interval.start:
                    raise Stage4PredecessorError("presentation track has an unordered presentation discontinuity")
                if previous.continuity is segment.continuity is PresentationSegmentContinuity.CONTINUOUS_DECODED:
                    raise Stage4PredecessorError("adjacent continuous presentation segments must be merged")
                if previous.continuity is segment.continuity is PresentationSegmentContinuity.DECLARED_GAP:
                    raise Stage4PredecessorError("adjacent declared gaps must be merged")
            previous = segment
        continuous = tuple(
            item for item in segments if item.continuity is PresentationSegmentContinuity.CONTINUOUS_DECODED
        )
        if not continuous or continuous[0].stream_tick_range.start_pts != self.origin_tick or continuous[-1].stream_tick_range.end_pts != self.end_tick:
            raise Stage4PredecessorError("presentation track does not prove declared endpoints")
        object.__setattr__(self, "segments", segments)

    @property
    def continuous_segments(self) -> tuple[PresentationTrackSegment, ...]:
        return tuple(
            item for item in self.segments if item.continuity is PresentationSegmentContinuity.CONTINUOUS_DECODED
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "clock_id": self.clock_id,
            "coverage_outcome": self.coverage_outcome.value,
            "end_tick": self.end_tick,
            "endpoint_proof": self.endpoint_proof,
            "index_sha256": self.index_sha256,
            "media_kind": self.media_kind.value,
            "origin_tick": self.origin_tick,
            "segments": [item.to_mapping() for item in self.segments],
            "stream_index": self.stream_index,
            "time_base": {"denominator": self.time_base.denominator, "numerator": self.time_base.numerator},
        }


@dataclass(frozen=True, slots=True)
class PresentationProbeExecution:
    """Reproducible ffprobe execution evidence, rather than a display version string."""

    probe_kind: str
    invocation_schema_sha256: str
    executable_sha256: str
    version_output_sha256: str
    normalized_output_sha256: str
    source_input_sha256: str

    def __post_init__(self) -> None:
        if self.probe_kind != "ffprobe-decoded-presentation-v2":
            raise Stage4PredecessorError("presentation probe kind is unsupported")
        for name in (
            "invocation_schema_sha256", "executable_sha256", "version_output_sha256",
            "normalized_output_sha256", "source_input_sha256",
        ):
            _sha(getattr(self, name), f"presentation probe {name}")

    def to_mapping(self) -> dict[str, object]:
        return {
            "executable_sha256": self.executable_sha256,
            "invocation_schema_sha256": self.invocation_schema_sha256,
            "normalized_output_sha256": self.normalized_output_sha256,
            "probe_kind": self.probe_kind,
            "source_input_sha256": self.source_input_sha256,
            "version_output_sha256": self.version_output_sha256,
        }


@dataclass(frozen=True, slots=True)
class PresentationTimelineProbe:
    """Closed source-prep facts consumed by preflight; no range-derived fallback exists."""

    schema_version: str
    source_id: str
    source_sha256: str
    source_blob_content_hash: str
    source_blob_byte_length: int
    source_blob_media_type: str
    facts_compiler_id: str
    facts_compiler_contract_sha256: str
    probe_execution: PresentationProbeExecution
    video: PresentationTrack
    audio: PresentationTrack
    frame_pts_index_set_sha256: str
    audio_sample_boundary_set_sha256: str
    source_proxy_timeline_map_sha256: str | None = None
    window_manifest_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != "presentation-map-facts-v2":
            raise Stage4PredecessorError("presentation facts schema is unsupported")
        for name in ("source_id", "facts_compiler_id", "source_blob_media_type"):
            _text(getattr(self, name), name)
        for name in (
            "source_sha256", "source_blob_content_hash", "facts_compiler_contract_sha256",
            "frame_pts_index_set_sha256", "audio_sample_boundary_set_sha256",
        ):
            _sha(getattr(self, name), name)
        if self.source_sha256 != self.source_blob_content_hash:
            raise Stage4PredecessorError("presentation facts source blob hash does not close")
        if type(self.source_blob_byte_length) is not int or self.source_blob_byte_length <= 0:  # noqa: E721
            raise Stage4PredecessorError("presentation facts source blob length is invalid")
        if type(self.probe_execution) is not PresentationProbeExecution:  # noqa: E721
            raise Stage4PredecessorError("presentation facts probe execution is invalid")
        if self.probe_execution.source_input_sha256 != self.source_sha256:
            raise Stage4PredecessorError("presentation facts probe did not consume the source hash")
        if type(self.video) is not PresentationTrack or type(self.audio) is not PresentationTrack:  # noqa: E721
            raise Stage4PredecessorError("presentation facts tracks are invalid")
        if self.video.media_kind is not MediaKind.VIDEO or self.audio.media_kind is not MediaKind.AUDIO:
            raise Stage4PredecessorError("presentation facts tracks have invalid media kinds")
        if self.video.index_sha256 != self.frame_pts_index_set_sha256 or self.audio.index_sha256 != self.audio_sample_boundary_set_sha256:
            raise Stage4PredecessorError("presentation facts track indexes do not close")
        optional_hashes = (self.source_proxy_timeline_map_sha256, self.window_manifest_sha256)
        if (optional_hashes[0] is None) != (optional_hashes[1] is None):
            raise Stage4PredecessorError("presentation facts proxy/map identities must be paired")
        for value in optional_hashes:
            if value is not None:
                _sha(value, "presentation facts optional identity")

    def to_mapping(self) -> dict[str, object]:
        return {
            "audio": self.audio.to_mapping(),
            "audio_sample_boundary_set_sha256": self.audio_sample_boundary_set_sha256,
            "facts_compiler_contract_sha256": self.facts_compiler_contract_sha256,
            "facts_compiler_id": self.facts_compiler_id,
            "frame_pts_index_set_sha256": self.frame_pts_index_set_sha256,
            "probe_execution": self.probe_execution.to_mapping(),
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "source_blob_byte_length": self.source_blob_byte_length,
            "source_blob_content_hash": self.source_blob_content_hash,
            "source_blob_media_type": self.source_blob_media_type,
            "source_proxy_timeline_map_sha256": self.source_proxy_timeline_map_sha256,
            "source_sha256": self.source_sha256,
            "video": self.video.to_mapping(),
            "window_manifest_sha256": self.window_manifest_sha256,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class AVPresentationMapSegment:
    """One exact, gap-free intersection of a video and audio source segment."""

    video_tick_range: TickRange
    audio_tick_range: TickRange
    presentation_interval: RationalPresentationInterval

    def __post_init__(self) -> None:
        if type(self.video_tick_range) is not TickRange or type(self.audio_tick_range) is not TickRange:  # noqa: E721
            raise Stage4PredecessorError("A/V map stream ranges are invalid")
        if type(self.presentation_interval) is not RationalPresentationInterval:  # noqa: E721
            raise Stage4PredecessorError("A/V map presentation interval is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "audio_tick_range": {
                "end_tick": self.audio_tick_range.end_pts,
                "start_tick": self.audio_tick_range.start_pts,
            },
            "presentation_interval": self.presentation_interval.to_mapping(),
            "video_tick_range": {
                "end_tick": self.video_tick_range.end_pts,
                "start_tick": self.video_tick_range.start_pts,
            },
        }


def _floor_tick_for_presentation(value: Fraction, time_base: TimeBase) -> int:
    tick = value * time_base.denominator / time_base.numerator
    return tick.numerator // tick.denominator


def _ceil_tick_for_presentation(value: Fraction, time_base: TimeBase) -> int:
    tick = value * time_base.denominator / time_base.numerator
    return -(-tick.numerator // tick.denominator)


def _subtract_intervals(
    source: tuple[RationalPresentationInterval, ...],
    covered: tuple[RationalPresentationInterval, ...],
) -> tuple[RationalPresentationInterval, ...]:
    result: list[RationalPresentationInterval] = []
    for interval in source:
        cursor = interval.start
        for common in covered:
            if common.end <= cursor:
                continue
            if common.start >= interval.end:
                break
            if cursor < common.start:
                result.append(RationalPresentationInterval.from_fractions(cursor, min(common.start, interval.end)))
            cursor = max(cursor, common.end)
            if cursor >= interval.end:
                break
        if cursor < interval.end:
            result.append(RationalPresentationInterval.from_fractions(cursor, interval.end))
    return tuple(result)


def _tail_position(
    interval: RationalPresentationInterval,
    common: tuple[RationalPresentationInterval, ...],
) -> PresentationNonOverlapPosition:
    if interval.end <= common[0].start:
        return PresentationNonOverlapPosition.LEADING
    if interval.start >= common[-1].end:
        return PresentationNonOverlapPosition.TRAILING
    return PresentationNonOverlapPosition.INTERNAL_GAP


def _compile_presentation_map(
    probe: PresentationTimelineProbe,
) -> tuple[
    tuple[AVPresentationMapSegment, ...],
    tuple[RationalPresentationInterval, ...],
    tuple[PresentationNonOverlap, ...],
]:
    segments: list[AVPresentationMapSegment] = []
    for video in probe.video.continuous_segments:
        for audio in probe.audio.continuous_segments:
            start, end = (
                max(video.presentation_interval.start, audio.presentation_interval.start),
                min(video.presentation_interval.end, audio.presentation_interval.end),
            )
            if start >= end:
                continue
            presentation = RationalPresentationInterval.from_fractions(start, end)
            segments.append(
                AVPresentationMapSegment(
                    TickRange(
                        _floor_tick_for_presentation(start, probe.video.time_base),
                        _ceil_tick_for_presentation(end, probe.video.time_base),
                    ),
                    TickRange(
                        _floor_tick_for_presentation(start, probe.audio.time_base),
                        _ceil_tick_for_presentation(end, probe.audio.time_base),
                    ),
                    presentation,
                )
            )
    segments.sort(
        key=lambda item: (
            item.presentation_interval.start,
            item.presentation_interval.end,
            item.video_tick_range.start_pts,
        )
    )
    if not segments:
        raise Stage4PredecessorError("A/V streams have no common presentation interval")
    common = tuple(item.presentation_interval for item in segments)
    if any(left.end >= right.start for left, right in zip(common, common[1:], strict=False)):
        raise Stage4PredecessorError("source segments yield overlapping or unproved adjacent A/V maps")
    records: list[PresentationNonOverlap] = []
    for media, track in (
        (PresentationNonOverlapMedia.AUDIO, probe.audio),
        (PresentationNonOverlapMedia.VIDEO, probe.video),
    ):
        source_intervals = tuple(item.presentation_interval for item in track.continuous_segments)
        for tail in _subtract_intervals(source_intervals, common):
            records.append(PresentationNonOverlap(media, _tail_position(tail, common), tail))
    records.sort(
        key=lambda item: (
            item.media.value,
            item.position.value,
            item.presentation_interval.start,
            item.presentation_interval.end,
        )
    )
    return tuple(segments), common, tuple(records)


@dataclass(frozen=True, slots=True)
class CommittedVideoToAudioClockMapCertificate:
    """Closed piecewise equality map compiled from source-prep presentation facts."""

    schema_version: str
    certificate_compiler_id: str
    certificate_compiler_contract_sha256: str
    facts_sha256: str
    root_evidence_sha256: str
    frame_pts_index_sha256: str
    audio_boundary_set_sha256: str
    source_manifest_sha256: str
    algorithm: str
    map_segments: tuple[AVPresentationMapSegment, ...]
    common_presentation_intervals: tuple[RationalPresentationInterval, ...]
    non_overlaps: tuple[PresentationNonOverlap, ...]
    snap_error_allowance_audio_tick: int
    calibration_binding_sha256: str
    window_manifest_sha256: str | None = None
    source_proxy_timeline_map_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != "committed-video-to-audio-presentation-map-v2":
            raise Stage4PredecessorError("clock map certificate schema is unsupported")
        _text(self.certificate_compiler_id, "clock map certificate compiler")
        for name in (
            "certificate_compiler_contract_sha256", "facts_sha256", "root_evidence_sha256",
            "frame_pts_index_sha256", "audio_boundary_set_sha256", "source_manifest_sha256",
            "calibration_binding_sha256",
        ):
            _sha(getattr(self, name), name)
        if self.algorithm != "absolute-equal-presentation-piecewise-v2":
            raise Stage4PredecessorError("clock map algorithm is not the registered piecewise mapper")
        if type(self.snap_error_allowance_audio_tick) is not int or self.snap_error_allowance_audio_tick < 0:  # noqa: E721
            raise Stage4PredecessorError("clock map snap allowance is invalid")
        segments = tuple(self.map_segments)
        if not segments or any(type(item) is not AVPresentationMapSegment for item in segments):  # noqa: E721
            raise Stage4PredecessorError("clock map segments are invalid")
        segment_intervals = tuple(item.presentation_interval for item in segments)
        common = tuple(self.common_presentation_intervals)
        if not common or any(type(item) is not RationalPresentationInterval for item in common):  # noqa: E721
            raise Stage4PredecessorError("clock map common intervals are invalid")
        if common != segment_intervals:
            raise Stage4PredecessorError("clock map common intervals must exactly match map segments")
        if tuple((item.start, item.end) for item in common) != tuple(sorted((item.start, item.end) for item in common)):
            raise Stage4PredecessorError("clock map common intervals are not canonical")
        if any(left.end >= right.start for left, right in zip(common, common[1:], strict=False)):
            raise Stage4PredecessorError("clock map common intervals overlap or are adjacent")
        overlaps = tuple(self.non_overlaps)
        if any(type(item) is not PresentationNonOverlap for item in overlaps):  # noqa: E721
            raise Stage4PredecessorError("clock map non-overlaps are invalid")
        keys = tuple(
            (item.media.value, item.position.value, item.presentation_interval.start, item.presentation_interval.end)
            for item in overlaps
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise Stage4PredecessorError("clock map non-overlaps are not canonical")
        optional_hashes = (self.window_manifest_sha256, self.source_proxy_timeline_map_sha256)
        if (optional_hashes[0] is None) != (optional_hashes[1] is None):
            raise Stage4PredecessorError("clock map proxy/map identities must be paired")
        for value in optional_hashes:
            if value is not None:
                _sha(value, "clock map optional identity")
        object.__setattr__(self, "map_segments", segments)
        object.__setattr__(self, "common_presentation_intervals", common)
        object.__setattr__(self, "non_overlaps", overlaps)

    def to_mapping(self) -> dict[str, object]:
        return {
            "algorithm": self.algorithm,
            "audio_boundary_set_sha256": self.audio_boundary_set_sha256,
            "calibration_binding_sha256": self.calibration_binding_sha256,
            "certificate_compiler_contract_sha256": self.certificate_compiler_contract_sha256,
            "certificate_compiler_id": self.certificate_compiler_id,
            "common_presentation_intervals": [item.to_mapping() for item in self.common_presentation_intervals],
            "facts_sha256": self.facts_sha256,
            "frame_pts_index_sha256": self.frame_pts_index_sha256,
            "map_segments": [item.to_mapping() for item in self.map_segments],
            "non_overlaps": [item.to_mapping() for item in self.non_overlaps],
            "root_evidence_sha256": self.root_evidence_sha256,
            "schema_version": self.schema_version,
            "snap_error_allowance_audio_tick": self.snap_error_allowance_audio_tick,
            "source_manifest_sha256": self.source_manifest_sha256,
            "source_proxy_timeline_map_sha256": self.source_proxy_timeline_map_sha256,
            "window_manifest_sha256": self.window_manifest_sha256,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def assert_replays_probe(
        self,
        probe: PresentationTimelineProbe,
        root: RootMediaEvidenceBundle | PhysicalRootMediaEvidence,
        *,
        source_manifest_sha256: str,
        calibration_binding: CalibrationBinding,
    ) -> None:
        if type(probe) is not PresentationTimelineProbe or type(root) not in (  # noqa: E721
            RootMediaEvidenceBundle, PhysicalRootMediaEvidence,
        ):
            raise Stage4PredecessorError("clock certificate requires exact probe and root evidence")
        if (
            self.facts_sha256 != probe.canonical_hash
            or self.root_evidence_sha256 != root.canonical_hash
            or self.frame_pts_index_sha256 != root.frame_pts_index.canonical_hash
            or self.audio_boundary_set_sha256 != root.audio_sample_boundaries.canonical_hash
            or self.source_manifest_sha256 != source_manifest_sha256
            or self.calibration_binding_sha256 != calibration_binding.canonical_hash
            or self.snap_error_allowance_audio_tick != calibration_binding.timing_error_bound_tick
            or not calibration_binding.active
        ):
            raise Stage4PredecessorError("clock certificate does not bind the committed probe/index facts")
        video, audio = root.frame_pts_index.context, root.audio_sample_boundaries.context
        if (
            probe.source_id, probe.source_sha256, probe.video.clock_id, probe.video.time_base, probe.video.origin_tick, probe.video.end_tick,
            probe.audio.clock_id, probe.audio.time_base, probe.audio.origin_tick, probe.audio.end_tick,
        ) != (
            root.source_id, root.source_sha256, video.clock_id, video.time_base, video.origin_tick, video.end_tick,
            audio.clock_id, audio.time_base, audio.origin_tick, audio.end_tick,
        ):
            raise Stage4PredecessorError("probe does not bind the committed source clocks")
        if (
            probe.frame_pts_index_set_sha256 != root.frame_pts_index.canonical_hash
            or probe.audio_sample_boundary_set_sha256 != root.audio_sample_boundaries.canonical_hash
            or probe.source_blob_content_hash != root.source_sha256
            or probe.window_manifest_sha256 != self.window_manifest_sha256
            or probe.source_proxy_timeline_map_sha256 != self.source_proxy_timeline_map_sha256
            or calibration_binding.time_base != audio.time_base
        ):
            raise Stage4PredecessorError("probe does not close exact root/index/calibration identities")
        segments, common, non_overlaps = _compile_presentation_map(probe)
        if self.map_segments != segments or self.common_presentation_intervals != common or self.non_overlaps != non_overlaps:
            raise Stage4PredecessorError("clock certificate common interval/tails do not replay")


def derive_presentation_timeline_facts(
    root: RootMediaEvidenceBundle | PhysicalRootMediaEvidence,
    *,
    probe: PresentationTimelineProbe,
    source_manifest_sha256: str,
    audio_snap_calibration: CalibrationBinding,
) -> tuple[PresentationTimelineProbe, CommittedVideoToAudioClockMapCertificate]:
    """Compile from exact source facts; speech is not required for a physical map.

    Both root formats retain their own aggregate hash. Supporting the physical
    root here does not grant the separate timed-speech or edit admission.
    """
    if type(root) not in (RootMediaEvidenceBundle, PhysicalRootMediaEvidence):
        raise Stage4PredecessorError("presentation probe requires exact root evidence")
    if root.audio_sample_boundaries.source_outcome is AudioSourceOutcome.NOT_APPLICABLE:
        raise Stage4PredecessorError("presentation clock map requires audio boundaries")
    if type(probe) is not PresentationTimelineProbe or type(audio_snap_calibration) is not CalibrationBinding:  # noqa: E721
        raise Stage4PredecessorError("presentation map requires exact source facts and calibration")
    _sha(source_manifest_sha256, "presentation map source manifest hash")
    if not audio_snap_calibration.active:
        raise Stage4PredecessorError("presentation map calibration is inactive")
    segments, common, non_overlaps = _compile_presentation_map(probe)
    certificate = CommittedVideoToAudioClockMapCertificate(
        "committed-video-to-audio-presentation-map-v2",
        "autocut-kernel-presentation-map-compiler-v2",
        probe.facts_compiler_contract_sha256,
        probe.canonical_hash,
        root.canonical_hash,
        root.frame_pts_index.canonical_hash,
        root.audio_sample_boundaries.canonical_hash,
        source_manifest_sha256,
        "absolute-equal-presentation-piecewise-v2",
        segments,
        common,
        non_overlaps,
        audio_snap_calibration.timing_error_bound_tick,
        audio_snap_calibration.canonical_hash,
        probe.window_manifest_sha256,
        probe.source_proxy_timeline_map_sha256,
    )
    certificate.assert_replays_probe(
        probe, root,
        source_manifest_sha256=source_manifest_sha256,
        calibration_binding=audio_snap_calibration,
    )
    return probe, certificate


__all__ = [
    "AVPresentationMapSegment",
    "CommittedVideoToAudioClockMapCertificate",
    "PresentationNonOverlap",
    "PresentationNonOverlapMedia",
    "PresentationNonOverlapPosition",
    "PresentationProbeExecution",
    "PresentationSegmentContinuity",
    "PresentationTimelineProbe",
    "PresentationTrack",
    "PresentationTrackSegment",
    "RationalPresentationInterval",
    "Stage4PredecessorError",
    "TimedSpeechCapability",
    "TimedSpeechGuardPolicy",
    "TimedSpeechProducerRequirement",
    "TimedSpeechProfileAdmission",
    "TimedSpeechProfileKind",
    "TimedSpeechProfileRegistryEntry",
    "admit_timed_speech_profile",
    "decode_timed_speech_profile_registry_entry",
    "derive_presentation_timeline_facts",
]
