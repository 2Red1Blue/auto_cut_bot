"""Closed candidate-local view of a Store-resolved timed-speech authority.

The CPU registry entry and the PC-CUDA runtime capability are different
authority kinds.  This module projects either already-resolved value into the
one immutable shape consumed by candidate compilation without pretending that
CUDA is an installed ``local_run`` registry profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from ..media.calibration_record import CalibrationRecordRole
from ..media.stage4_predecessor import (
    TimedSpeechCapability,
    TimedSpeechGuardPolicy,
    TimedSpeechProfileKind,
    TimedSpeechProfileRegistryEntry,
)
from ..media.types import TimeBase, canonical_sha256, sha256_prefixed
from ..registry.authority_profiles import TimingPolicies
from ..registry.runtime_timed_speech import RuntimeTimedSpeechProjection


class CandidateTimedSpeechAuthorityError(ValueError):
    """A resolved timed-speech authority cannot be projected for a candidate."""


class CandidateTimedSpeechAuthorityKind(str, Enum):
    INSTALLED_CPU_PROFILE = "installed_cpu_profile"
    RUNTIME_CUDA_CAPABILITY = "runtime_cuda_capability"


def _validate_cuda_projection_inputs(
    projection: RuntimeTimedSpeechProjection,
    timing_policies: TimingPolicies,
) -> None:
    for field_name in (
        "timed_speech_policy_sha256",
        "word_gap_policy_sha256",
        "vad_merge_policy_sha256",
        "alignment_policy_sha256",
        "acceptance_policy_sha256",
    ):
        try:
            sha256_prefixed(getattr(timing_policies, field_name), field_name)
        except ValueError as error:
            raise CandidateTimedSpeechAuthorityError(
                "CUDA resolver timing policy contains an invalid hash"
            ) from error
    if (
        projection.runtime_capability_id != "pc_cuda"
        or projection.device_class != "cuda"
        or type(projection.source_clock_id) is not str  # noqa: E721
        or not projection.source_clock_id
        or projection.source_clock_id != projection.source_clock_id.strip()
        or type(projection.source_time_base) is not TimeBase  # noqa: E721
    ):
        raise CandidateTimedSpeechAuthorityError(
            "CUDA candidate authority requires the exact PC-CUDA source clock"
        )
    expected_policy_hashes = tuple(
        getattr(timing_policies, field_name)
        for field_name in (
            "timed_speech_policy_sha256",
            "word_gap_policy_sha256",
            "vad_merge_policy_sha256",
            "alignment_policy_sha256",
            "acceptance_policy_sha256",
        )
    )
    actual_policy_hashes = (
        projection.timed_speech_policy_sha256,
        projection.word_gap_policy_sha256,
        projection.vad_merge_policy_sha256,
        projection.alignment_policy_sha256,
        projection.acceptance_policy_sha256,
    )
    if actual_policy_hashes != expected_policy_hashes:
        raise CandidateTimedSpeechAuthorityError(
            "CUDA runtime projection differs from resolver timing policy hashes"
        )
    if (
        timing_policies.word_gap_ms != projection.word_gap_ms
        or timing_policies.vad_merge_gap_ms != projection.vad_merge_gap_ms
    ):
        raise CandidateTimedSpeechAuthorityError(
            "CUDA runtime projection differs from accepted timing-policy gap values"
        )
    asr, vad = projection.producers
    if (
        asr.role is not CalibrationRecordRole.ASR
        or asr.model_id != "SenseVoiceSmall"
        or asr.inference_kind != "sensevoice-word-timestamp"
        or vad.role is not CalibrationRecordRole.VAD
        or vad.model_id != "fsmn-vad"
        or vad.inference_kind != "fsmn-vad-direct"
        or asr.producer_id == vad.producer_id
        or asr.model_sha256 == vad.model_sha256
        or asr.detector_sha256 == vad.detector_sha256
        or projection.asr_calibration_record_sha256
        == projection.vad_calibration_record_sha256
    ):
        raise CandidateTimedSpeechAuthorityError(
            "CUDA authority requires distinct SenseVoice and FSMN producer identities"
        )
    try:
        sha256_prefixed(projection.native_port_identity_sha256, "CUDA native adapter")
        sha256_prefixed(
            projection.asr_calibration_record_sha256, "CUDA ASR calibration record"
        )
        sha256_prefixed(
            projection.vad_calibration_record_sha256, "CUDA VAD calibration record"
        )
    except ValueError as error:
        raise CandidateTimedSpeechAuthorityError(
            "CUDA authority adapter/calibration identity is invalid"
        ) from error


@dataclass(frozen=True, slots=True)
class CandidateTimedSpeechAuthority:
    """Immutable discriminated authority used by candidate-level compilation."""

    authority_kind: CandidateTimedSpeechAuthorityKind
    original_authority_sha256: str
    profile_kind: TimedSpeechProfileKind
    capability: TimedSpeechCapability
    guard_policy: TimedSpeechGuardPolicy
    installed_cpu_profile: TimedSpeechProfileRegistryEntry | None
    runtime_cuda_capability: RuntimeTimedSpeechProjection | None
    runtime_timing_policies: TimingPolicies | None

    def __post_init__(self) -> None:
        if type(self.authority_kind) is not CandidateTimedSpeechAuthorityKind:  # noqa: E721
            raise CandidateTimedSpeechAuthorityError("candidate authority kind is invalid")
        try:
            sha256_prefixed(self.original_authority_sha256, "original authority hash")
        except ValueError as error:
            raise CandidateTimedSpeechAuthorityError(
                "candidate authority requires an exact original authority hash"
            ) from error
        if (
            type(self.profile_kind) is not TimedSpeechProfileKind  # noqa: E721
            or type(self.capability) is not TimedSpeechCapability  # noqa: E721
            or type(self.guard_policy) is not TimedSpeechGuardPolicy  # noqa: E721
        ):
            raise CandidateTimedSpeechAuthorityError(
                "candidate authority profile kind, capability, and guard policy must be exact"
            )
        if self.authority_kind is CandidateTimedSpeechAuthorityKind.INSTALLED_CPU_PROFILE:
            profile = self.installed_cpu_profile
            if (
                type(profile) is not TimedSpeechProfileRegistryEntry  # noqa: E721
                or self.runtime_cuda_capability is not None
                or self.runtime_timing_policies is not None
                or self.original_authority_sha256 != profile.canonical_hash
                or self.profile_kind is not profile.kind
                or self.capability is not profile.capability
                or self.guard_policy != profile.guard_policy
            ):
                raise CandidateTimedSpeechAuthorityError(
                    "installed CPU candidate authority does not close to its registry entry"
                )
            return
        projection = self.runtime_cuda_capability
        policies = self.runtime_timing_policies
        if (
            self.installed_cpu_profile is not None
            or type(projection) is not RuntimeTimedSpeechProjection  # noqa: E721
            or type(policies) is not TimingPolicies  # noqa: E721
            or self.original_authority_sha256 != projection.canonical_hash
            or self.profile_kind is not TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1
            or self.capability is not TimedSpeechCapability.KNOWN_SPEECH_ONLY
            or self.guard_policy.source_audio_clock_id != projection.source_clock_id
            or self.guard_policy.source_audio_time_base != projection.source_time_base
            or self.guard_policy.policy_sha256 != projection.timed_speech_policy_sha256
            or self.guard_policy.pre_roll_tick != 0
            or self.guard_policy.post_roll_tick != 0
        ):
            raise CandidateTimedSpeechAuthorityError(
                "runtime CUDA candidate authority does not close to its capability"
            )
        _validate_cuda_projection_inputs(projection, policies)
        if (
            self.guard_policy.word_gap_tick,
            self.guard_policy.vad_merge_gap_tick,
        ) != (
            _ceil_milliseconds_to_tick(policies.word_gap_ms, projection.source_time_base),
            _ceil_milliseconds_to_tick(policies.vad_merge_gap_ms, projection.source_time_base),
        ):
            raise CandidateTimedSpeechAuthorityError(
                "runtime CUDA candidate authority differs from resolver timing policies"
            )

    def to_mapping(self) -> dict[str, object]:
        original: TimedSpeechProfileRegistryEntry | RuntimeTimedSpeechProjection
        if self.authority_kind is CandidateTimedSpeechAuthorityKind.INSTALLED_CPU_PROFILE:
            profile = self.installed_cpu_profile
            if profile is None:  # pragma: no cover - constructor closes this arm.
                raise CandidateTimedSpeechAuthorityError("installed CPU authority is unavailable")
            original = profile
        else:
            projection = self.runtime_cuda_capability
            if projection is None:  # pragma: no cover - constructor closes this arm.
                raise CandidateTimedSpeechAuthorityError("runtime CUDA authority is unavailable")
            original = projection
        return {
            "schema_version": "candidate-timed-speech-authority-v1",
            "authority_kind": self.authority_kind.value,
            "original_authority_sha256": self.original_authority_sha256,
            "profile_kind": self.profile_kind.value,
            "capability": self.capability.value,
            "guard_policy": self.guard_policy.to_mapping(),
            "original_authority": original.to_mapping(),
            "runtime_timing_policies": (
                None
                if self.runtime_timing_policies is None
                else self.runtime_timing_policies.to_mapping()
            ),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


CandidateTimedSpeechAuthorityInput: TypeAlias = (
    CandidateTimedSpeechAuthority | TimedSpeechProfileRegistryEntry
)


def _ceil_milliseconds_to_tick(milliseconds: int, time_base: TimeBase) -> int:
    if type(milliseconds) is not int or milliseconds < 0:  # noqa: E721
        raise CandidateTimedSpeechAuthorityError(
            "CUDA timing-policy durations must be non-negative integer milliseconds"
        )
    numerator = milliseconds * time_base.denominator
    denominator = 1_000 * time_base.numerator
    return (numerator + denominator - 1) // denominator


def project_candidate_timed_speech_authority_from_registry_entry(
    profile: TimedSpeechProfileRegistryEntry,
) -> CandidateTimedSpeechAuthority:
    """Project an exact Store-read installed CPU registry entry."""
    if type(profile) is not TimedSpeechProfileRegistryEntry:  # noqa: E721
        raise CandidateTimedSpeechAuthorityError(
            "CPU candidate authority requires an exact Store-read registry entry"
        )
    return CandidateTimedSpeechAuthority(
        CandidateTimedSpeechAuthorityKind.INSTALLED_CPU_PROFILE,
        profile.canonical_hash,
        profile.kind,
        profile.capability,
        profile.guard_policy,
        profile,
        None,
        None,
    )


def project_candidate_timed_speech_authority_from_runtime_projection(
    projection: RuntimeTimedSpeechProjection,
    timing_policies: TimingPolicies,
) -> CandidateTimedSpeechAuthority:
    """Project an exact Store-read PC-CUDA capability and its static policies."""
    if type(projection) is not RuntimeTimedSpeechProjection:  # noqa: E721
        raise CandidateTimedSpeechAuthorityError(
            "CUDA candidate authority requires an exact Store-read runtime projection"
        )
    if type(timing_policies) is not TimingPolicies:  # noqa: E721
        raise CandidateTimedSpeechAuthorityError(
            "CUDA candidate authority requires exact resolver timing policies"
        )
    _validate_cuda_projection_inputs(projection, timing_policies)
    guard_policy = TimedSpeechGuardPolicy(
        policy_sha256=projection.timed_speech_policy_sha256,
        source_audio_clock_id=projection.source_clock_id,
        source_audio_time_base=projection.source_time_base,
        word_gap_tick=_ceil_milliseconds_to_tick(
            projection.word_gap_ms, projection.source_time_base
        ),
        vad_merge_gap_tick=_ceil_milliseconds_to_tick(
            projection.vad_merge_gap_ms, projection.source_time_base
        ),
        pre_roll_tick=0,
        post_roll_tick=0,
    )
    return CandidateTimedSpeechAuthority(
        CandidateTimedSpeechAuthorityKind.RUNTIME_CUDA_CAPABILITY,
        projection.canonical_hash,
        TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1,
        TimedSpeechCapability.KNOWN_SPEECH_ONLY,
        guard_policy,
        None,
        projection,
        timing_policies,
    )


def normalize_candidate_timed_speech_authority(
    authority: CandidateTimedSpeechAuthorityInput,
) -> CandidateTimedSpeechAuthority:
    """Immediately normalize the only two compiler input forms."""
    if type(authority) is CandidateTimedSpeechAuthority:  # noqa: E721
        return authority
    if type(authority) is TimedSpeechProfileRegistryEntry:  # noqa: E721
        return project_candidate_timed_speech_authority_from_registry_entry(authority)
    raise CandidateTimedSpeechAuthorityError(
        "candidate compiler requires an exact projected authority or CPU registry entry"
    )
