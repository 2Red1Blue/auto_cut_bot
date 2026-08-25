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

from .root_evidence import (
    AudioSourceOutcome,
    EvidenceCompleteness,
    RootMediaEvidenceBundle,
    SpeechActivitySet,
    TranscriptSet,
)
from .timed_evidence import CalibrationBinding
from .types import MediaValidationError, TimeBase, canonical_sha256, sha256_prefixed


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

    def __post_init__(self) -> None:
        _text(self.producer_id, "producer requirement producer_id")
        _text(self.clock_id, "producer requirement clock_id")
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
                "generation_policy_sha256", "model_sha256", "producer_id", "time_base",
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


def _presentation(tick: int, origin: int, time_base: TimeBase) -> Fraction:
    return Fraction((tick - origin) * time_base.numerator, time_base.denominator)


def _presentation_partition(root: RootMediaEvidenceBundle) -> tuple[RationalPresentationInterval, tuple[PresentationNonOverlap, ...]]:
    video = root.frame_pts_index.context
    audio = root.audio_sample_boundaries.context
    video_range = (_presentation(video.origin_tick, video.origin_tick, video.time_base), _presentation(video.end_tick, video.origin_tick, video.time_base))
    audio_range = (_presentation(audio.origin_tick, audio.origin_tick, audio.time_base), _presentation(audio.end_tick, audio.origin_tick, audio.time_base))
    common_start, common_end = max(video_range[0], audio_range[0]), min(video_range[1], audio_range[1])
    if common_start >= common_end:
        raise Stage4PredecessorError("A/V streams have no common presentation interval")
    records: list[PresentationNonOverlap] = []
    for media, bounds in ((PresentationNonOverlapMedia.AUDIO, audio_range), (PresentationNonOverlapMedia.VIDEO, video_range)):
        if bounds[0] < common_start:
            records.append(PresentationNonOverlap(media, PresentationNonOverlapPosition.LEADING, RationalPresentationInterval.from_fractions(bounds[0], common_start)))
        if common_end < bounds[1]:
            records.append(PresentationNonOverlap(media, PresentationNonOverlapPosition.TRAILING, RationalPresentationInterval.from_fractions(common_end, bounds[1])))
    records.sort(key=lambda value: (value.media.value, value.position.value))
    return RationalPresentationInterval.from_fractions(common_start, common_end), tuple(records)


@dataclass(frozen=True, slots=True)
class PresentationTimelineProbe:
    """Probe facts bound to the exact decoded frame and sample indexes."""

    source_id: str
    source_sha256: str
    root_evidence_sha256: str
    frame_pts_index_sha256: str
    audio_boundary_set_sha256: str
    video_clock_id: str
    video_time_base: TimeBase
    video_origin_tick: int
    video_end_tick: int
    audio_clock_id: str
    audio_time_base: TimeBase
    audio_origin_tick: int
    audio_end_tick: int
    probe_tool_identity_sha256: str
    probe_tool_version_sha256: str
    probe_invocation_sha256: str
    mapping_policy_sha256: str

    def __post_init__(self) -> None:
        for name in ("source_id", "video_clock_id", "audio_clock_id"):
            _text(getattr(self, name), name)
        for name in ("source_sha256", "root_evidence_sha256", "frame_pts_index_sha256", "audio_boundary_set_sha256", "probe_tool_identity_sha256", "probe_tool_version_sha256", "probe_invocation_sha256", "mapping_policy_sha256"):
            _sha(getattr(self, name), name)
        for name in ("video_time_base", "audio_time_base"):
            _time_base(getattr(self, name), name)
        for start, end, name in ((self.video_origin_tick, self.video_end_tick, "video"), (self.audio_origin_tick, self.audio_end_tick, "audio")):
            if type(start) is not int or type(end) is not int or start >= end:  # noqa: E721
                raise Stage4PredecessorError(f"{name} probe range is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "audio_boundary_set_sha256": self.audio_boundary_set_sha256,
            "audio_clock_id": self.audio_clock_id,
            "audio_end_tick": self.audio_end_tick,
            "audio_origin_tick": self.audio_origin_tick,
            "audio_time_base": {"denominator": self.audio_time_base.denominator, "numerator": self.audio_time_base.numerator},
            "frame_pts_index_sha256": self.frame_pts_index_sha256,
            "mapping_policy_sha256": self.mapping_policy_sha256,
            "probe_invocation_sha256": self.probe_invocation_sha256,
            "probe_tool_identity_sha256": self.probe_tool_identity_sha256,
            "probe_tool_version_sha256": self.probe_tool_version_sha256,
            "root_evidence_sha256": self.root_evidence_sha256,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "video_clock_id": self.video_clock_id,
            "video_end_tick": self.video_end_tick,
            "video_origin_tick": self.video_origin_tick,
            "video_time_base": {"denominator": self.video_time_base.denominator, "numerator": self.video_time_base.numerator},
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class CommittedVideoToAudioClockMapCertificate:
    """Certificate from one probe, never a root-evidence convenience factory."""

    probe_sha256: str
    root_evidence_sha256: str
    frame_pts_index_sha256: str
    audio_boundary_set_sha256: str
    mapping_policy_sha256: str
    algorithm_version: str
    snap_error_allowance_audio_tick: int
    common_presentation_interval: RationalPresentationInterval
    non_overlaps: tuple[PresentationNonOverlap, ...]

    def __post_init__(self) -> None:
        for name in ("probe_sha256", "root_evidence_sha256", "frame_pts_index_sha256", "audio_boundary_set_sha256", "mapping_policy_sha256"):
            _sha(getattr(self, name), name)
        if self.algorithm_version != "equal-presentation-time-rational-v1":
            raise Stage4PredecessorError("clock map algorithm is not the registered rational mapper")
        if type(self.snap_error_allowance_audio_tick) is not int or self.snap_error_allowance_audio_tick < 0:  # noqa: E721
            raise Stage4PredecessorError("clock map snap allowance is invalid")
        if type(self.common_presentation_interval) is not RationalPresentationInterval:  # noqa: E721
            raise Stage4PredecessorError("clock map common interval is invalid")
        overlaps = tuple(self.non_overlaps)
        if any(type(item) is not PresentationNonOverlap for item in overlaps):  # noqa: E721
            raise Stage4PredecessorError("clock map non-overlaps are invalid")
        keys = tuple((item.media.value, item.position.value) for item in overlaps)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise Stage4PredecessorError("clock map non-overlaps are not canonical")
        object.__setattr__(self, "non_overlaps", overlaps)

    def to_mapping(self) -> dict[str, object]:
        return {
            "algorithm_version": self.algorithm_version,
            "audio_boundary_set_sha256": self.audio_boundary_set_sha256,
            "common_presentation_interval": self.common_presentation_interval.to_mapping(),
            "frame_pts_index_sha256": self.frame_pts_index_sha256,
            "mapping_policy_sha256": self.mapping_policy_sha256,
            "non_overlaps": [item.to_mapping() for item in self.non_overlaps],
            "probe_sha256": self.probe_sha256,
            "root_evidence_sha256": self.root_evidence_sha256,
            "snap_error_allowance_audio_tick": self.snap_error_allowance_audio_tick,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def assert_replays_probe(self, probe: PresentationTimelineProbe, root: RootMediaEvidenceBundle) -> None:
        if type(probe) is not PresentationTimelineProbe or type(root) is not RootMediaEvidenceBundle:  # noqa: E721
            raise Stage4PredecessorError("clock certificate requires exact probe and root evidence")
        if (
            self.probe_sha256 != probe.canonical_hash
            or self.root_evidence_sha256 != root.canonical_hash
            or self.frame_pts_index_sha256 != root.frame_pts_index.canonical_hash
            or self.audio_boundary_set_sha256 != root.audio_sample_boundaries.canonical_hash
            or self.mapping_policy_sha256 != probe.mapping_policy_sha256
        ):
            raise Stage4PredecessorError("clock certificate does not bind the committed probe/index facts")
        video, audio = root.frame_pts_index.context, root.audio_sample_boundaries.context
        if (
            probe.source_id, probe.source_sha256, probe.video_clock_id, probe.video_time_base, probe.video_origin_tick, probe.video_end_tick,
            probe.audio_clock_id, probe.audio_time_base, probe.audio_origin_tick, probe.audio_end_tick,
        ) != (
            root.source_id, root.source_sha256, video.clock_id, video.time_base, video.origin_tick, video.end_tick,
            audio.clock_id, audio.time_base, audio.origin_tick, audio.end_tick,
        ):
            raise Stage4PredecessorError("probe does not bind the committed source clocks")
        common, non_overlaps = _presentation_partition(root)
        if self.common_presentation_interval != common or self.non_overlaps != non_overlaps:
            raise Stage4PredecessorError("clock certificate common interval/tails do not replay")


def derive_presentation_timeline_facts(
    root: RootMediaEvidenceBundle,
    *,
    probe_tool_identity_sha256: str,
    probe_tool_version_sha256: str,
    probe_invocation_sha256: str,
    mapping_policy_sha256: str,
    snap_error_allowance_audio_tick: int,
) -> tuple[PresentationTimelineProbe, CommittedVideoToAudioClockMapCertificate]:
    """Create the preflight-owned probe and certificate from actual indexes."""
    if type(root) is not RootMediaEvidenceBundle:  # noqa: E721
        raise Stage4PredecessorError("presentation probe requires exact root evidence")
    if root.audio_sample_boundaries.source_outcome is AudioSourceOutcome.NOT_APPLICABLE:
        raise Stage4PredecessorError("presentation clock map requires audio boundaries")
    video, audio = root.frame_pts_index.context, root.audio_sample_boundaries.context
    probe = PresentationTimelineProbe(
        root.source_id, root.source_sha256, root.canonical_hash,
        root.frame_pts_index.canonical_hash, root.audio_sample_boundaries.canonical_hash,
        video.clock_id, video.time_base, video.origin_tick, video.end_tick,
        audio.clock_id, audio.time_base, audio.origin_tick, audio.end_tick,
        probe_tool_identity_sha256, probe_tool_version_sha256, probe_invocation_sha256,
        mapping_policy_sha256,
    )
    common, non_overlaps = _presentation_partition(root)
    certificate = CommittedVideoToAudioClockMapCertificate(
        probe.canonical_hash, root.canonical_hash, root.frame_pts_index.canonical_hash,
        root.audio_sample_boundaries.canonical_hash, mapping_policy_sha256,
        "equal-presentation-time-rational-v1", snap_error_allowance_audio_tick,
        common, non_overlaps,
    )
    certificate.assert_replays_probe(probe, root)
    return probe, certificate


__all__ = [
    "CommittedVideoToAudioClockMapCertificate",
    "PresentationNonOverlap",
    "PresentationNonOverlapMedia",
    "PresentationNonOverlapPosition",
    "PresentationTimelineProbe",
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
