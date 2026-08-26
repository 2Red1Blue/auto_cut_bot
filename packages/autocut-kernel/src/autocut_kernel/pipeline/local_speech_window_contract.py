"""Local child content identities, not claim, retry or local-profile permission.

The future resolver must reread all handles, replay expansion/BUSY predecessors,
derive the extraction through the physical map, and independently validate an
accepted *local-mode* profile. The existing whole-source installed resolver is
not sufficient. No constructor here grants that permission.

Parent reconstruction remains owned by the existing Source/physical resolver:
``from_mapping(..., parent=...)`` compares its complete canonical projection,
rather than inventing an alternative decoder or fake Source/VLM objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from ..media.audio_stream_facts import AudioStreamFacts, decode_audio_stream_facts
from ..media.local_audio_window import LocalAudioWindowSpec
from ..media.local_speech_window import LocalSpeechWindowPolicy, LocalSpeechWindowRequest
from ..media.local_speech_window_codec import (
    decode_local_audio_window_spec,
    decode_local_speech_window_policy,
)
from ..media.timed_evidence import CandidateEvidenceWindow
from ..media.timed_evidence_codec import decode_candidate_evidence_window
from ..media.types import TickRange, canonical_sha256, sha256_prefixed
from ..registry.timed_speech import (
    TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE,
    TIMED_SPEECH_PROFILE_REGISTRY_SCOPE,
)
from ..store.models import CommandOutcome, CommittedArtifactMemberReference, canonical_recipe_scope
from .physical_media_contract import ResolvedPreparePhysicalMediaEvidenceRequest, physical_json
from .prepare_physical_media_evidence_command import physical_member_layout

LOCAL_SPEECH_WINDOW_LIFECYCLE_VERSION = "local-speech-window-lifecycle-v1"
PREPARE_LOCAL_SPEECH_WINDOW_CHILD_COMMAND = "PrepareLocalSpeechWindowChild@2.1.3"


class LocalSpeechWindowContractError(ValueError):
    """A local child content identity is incomplete or internally inconsistent."""


def _object(value: object, fields: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict or set(cast(dict[object, object], value)) != set(fields):
        raise LocalSpeechWindowContractError("local child object has missing or unknown fields")
    return cast(dict[str, object], value)


def _integer(value: object, *, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise LocalSpeechWindowContractError("local child integer is outside its exact safe range")
    return value


def _hash(value: object) -> str:
    return sha256_prefixed(value, "local child hash")


def _uuid(value: object) -> UUID:
    if type(value) is not str:
        raise LocalSpeechWindowContractError("local child UUID wire value must be text")
    result = UUID(value)
    if str(result) != value:
        raise LocalSpeechWindowContractError("local child UUID must be canonical")
    return result


def _handle_ids(*values: object) -> None:
    if any(type(value) is not UUID for value in values):
        raise LocalSpeechWindowContractError("local child handles require exact UUIDs")


@dataclass(frozen=True, slots=True)
class LocalSpeechWindowSuccessHandle:
    job_id: UUID
    command_slot_id: UUID
    receipt_id: UUID
    artifact_set_id: UUID
    request_hash: str

    def __post_init__(self) -> None:
        _handle_ids(self.job_id, self.command_slot_id, self.receipt_id, self.artifact_set_id)
        _hash(self.request_hash)

    def to_mapping(self) -> dict[str, object]:
        return {"job_id": str(self.job_id), "command_slot_id": str(self.command_slot_id),
                "receipt_id": str(self.receipt_id), "artifact_set_id": str(self.artifact_set_id),
                "request_hash": self.request_hash}

    @classmethod
    def from_mapping(cls, value: object) -> LocalSpeechWindowSuccessHandle:
        r = _object(value, ("job_id", "command_slot_id", "receipt_id", "artifact_set_id", "request_hash"))
        return cls(_uuid(r["job_id"]), _uuid(r["command_slot_id"]), _uuid(r["receipt_id"]),
                   _uuid(r["artifact_set_id"]), _hash(r["request_hash"]))

    @classmethod
    def from_outcome(cls, outcome: CommandOutcome, *, request_hash: str) -> LocalSpeechWindowSuccessHandle:
        if (type(outcome) is not CommandOutcome or outcome.state != "succeeded"
                or outcome.failure_code is not None or outcome.failure_detail_json is not None):
            raise LocalSpeechWindowContractError("success handle requires an exact succeeded outcome")
        return cls(cast(UUID, outcome.job_id), outcome.command_slot_id, cast(UUID, outcome.receipt_id),
                   cast(UUID, outcome.artifact_set_id), request_hash)

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class LocalSpeechWindowBusyHandle:
    """A terminal lookup selector, not proof that inference did not start."""

    job_id: UUID
    command_slot_id: UUID
    receipt_id: UUID
    request_hash: str

    def __post_init__(self) -> None:
        _handle_ids(self.job_id, self.command_slot_id, self.receipt_id)
        _hash(self.request_hash)

    def to_mapping(self) -> dict[str, object]:
        return {"job_id": str(self.job_id), "command_slot_id": str(self.command_slot_id),
                "receipt_id": str(self.receipt_id), "request_hash": self.request_hash}

    @classmethod
    def from_mapping(cls, value: object) -> LocalSpeechWindowBusyHandle:
        r = _object(value, ("job_id", "command_slot_id", "receipt_id", "request_hash"))
        return cls(_uuid(r["job_id"]), _uuid(r["command_slot_id"]), _uuid(r["receipt_id"]), _hash(r["request_hash"]))

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class LocalSpeechWindowReadLimits:
    """Frozen byte ceilings; a future chain reader must accumulate total usage."""

    max_raw_response_bytes: int
    max_projection_bytes: int
    max_metadata_bytes: int
    max_total_bytes: int

    def __post_init__(self) -> None:
        for value in (self.max_raw_response_bytes, self.max_projection_bytes,
                      self.max_metadata_bytes, self.max_total_bytes):
            _integer(value)

    def to_mapping(self) -> dict[str, object]:
        return {"max_raw_response_bytes": self.max_raw_response_bytes,
                "max_projection_bytes": self.max_projection_bytes,
                "max_metadata_bytes": self.max_metadata_bytes, "max_total_bytes": self.max_total_bytes}

    @classmethod
    def from_mapping(cls, value: object) -> LocalSpeechWindowReadLimits:
        r = _object(value, ("max_raw_response_bytes", "max_projection_bytes", "max_metadata_bytes", "max_total_bytes"))
        return cls(_integer(r["max_raw_response_bytes"]), _integer(r["max_projection_bytes"]),
                   _integer(r["max_metadata_bytes"]), _integer(r["max_total_bytes"]))

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class LocalSpeechWindowLifecycle:
    parent: ResolvedPreparePhysicalMediaEvidenceRequest
    physical_predecessor: LocalSpeechWindowSuccessHandle
    physical_members: tuple[CommittedArtifactMemberReference, ...]
    vlm_candidate_sha256: str
    audio_stream_facts: AudioStreamFacts
    expected_registry_sha256: str
    expected_profile_reference: CommittedArtifactMemberReference
    expected_asr_calibration_sha256: str
    expected_vad_calibration_sha256: str
    expected_native_port_identity_sha256: str
    policy: LocalSpeechWindowPolicy
    decoder_identity_sha256: str
    max_attempts: int
    max_outward_padding_audio_ticks: int
    max_decode_frames: int
    max_frame_bytes: int
    max_pcm_bytes: int
    read_limits: LocalSpeechWindowReadLimits

    def __post_init__(self) -> None:
        if (type(self.parent) is not ResolvedPreparePhysicalMediaEvidenceRequest
                or type(self.physical_predecessor) is not LocalSpeechWindowSuccessHandle
                or type(self.audio_stream_facts) is not AudioStreamFacts
                or type(self.policy) is not LocalSpeechWindowPolicy
                or type(self.read_limits) is not LocalSpeechWindowReadLimits):
            raise LocalSpeechWindowContractError("lifecycle requires exact parent/facts/policy/limits")
        for value in (self.vlm_candidate_sha256, self.expected_registry_sha256,
                      self.expected_asr_calibration_sha256, self.expected_vad_calibration_sha256,
                      self.expected_native_port_identity_sha256, self.decoder_identity_sha256):
            _hash(value)
        for value in (self.max_attempts, self.max_decode_frames, self.max_frame_bytes, self.max_pcm_bytes):
            _integer(value)
        _integer(self.max_outward_padding_audio_ticks, minimum=0)
        source = self.parent.source
        if source.audio_stream_facts is None or source.audio_stream_facts != self.audio_stream_facts:
            raise LocalSpeechWindowContractError("local lifecycle requires the parent's exact measured audio facts")
        self.audio_stream_facts.assert_matches(source.presentation_timeline_probe, source.audio_sample_boundaries)
        if self.vlm_candidate_sha256 not in {
            canonical_sha256(candidate.to_mapping()) for candidate in source.semantic_pack.candidate_hypotheses
        }:
            raise LocalSpeechWindowContractError("lifecycle candidate is not in the parent's VLM pack")
        predecessor = self.physical_predecessor
        if predecessor.request_hash != self.parent.request_hash:
            raise LocalSpeechWindowContractError("physical predecessor request differs")
        if (type(self.physical_members) is not tuple or len(self.physical_members) != 3
                or any(type(ref) is not CommittedArtifactMemberReference for ref in self.physical_members)):
            raise LocalSpeechWindowContractError("physical predecessor must name three exact ordered members")
        for ordinal, (ref, (kind, logical_id)) in enumerate(zip(
            self.physical_members, physical_member_layout(self.parent), strict=True,
        )):
            if (ref.receipt_id, ref.artifact_set_id, ref.member_ordinal, ref.scope,
                    ref.artifact_type, ref.logical_id, ref.revision) != (
                predecessor.receipt_id, predecessor.artifact_set_id, ordinal, canonical_recipe_scope(self.parent.job),
                kind, logical_id, 1,
            ):
                raise LocalSpeechWindowContractError("physical member owner/order/identity differs")
        profile = self.expected_profile_reference
        if (type(profile) is not CommittedArtifactMemberReference
                or profile.scope != TIMED_SPEECH_PROFILE_REGISTRY_SCOPE
                or profile.artifact_type != TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE):
            raise LocalSpeechWindowContractError("profile expectation requires an exact registry member reference")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": LOCAL_SPEECH_WINDOW_LIFECYCLE_VERSION,
            "parent": self.parent.canonical_payload(),
            "adaptive_policy": self.parent.source.adaptive_policy.to_mapping(),
            "physical_predecessor": self.physical_predecessor.to_mapping(),
            "physical_members": [ref.to_mapping() for ref in self.physical_members],
            "vlm_candidate_sha256": self.vlm_candidate_sha256,
            "audio_stream_facts": self.audio_stream_facts.to_mapping(),
            "expected_registry_sha256": self.expected_registry_sha256,
            "expected_profile_reference": self.expected_profile_reference.to_mapping(),
            "expected_asr_calibration_sha256": self.expected_asr_calibration_sha256,
            "expected_vad_calibration_sha256": self.expected_vad_calibration_sha256,
            "expected_native_port_identity_sha256": self.expected_native_port_identity_sha256,
            "policy": self.policy.to_mapping(), "decoder_identity_sha256": self.decoder_identity_sha256,
            "max_attempts": self.max_attempts, "max_outward_padding_audio_ticks": self.max_outward_padding_audio_ticks,
            "max_decode_frames": self.max_decode_frames, "max_frame_bytes": self.max_frame_bytes,
            "max_pcm_bytes": self.max_pcm_bytes, "read_limits": self.read_limits.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object, *, parent: ResolvedPreparePhysicalMediaEvidenceRequest) -> LocalSpeechWindowLifecycle:
        r = _object(value, (
            "schema_version", "parent", "adaptive_policy", "physical_predecessor", "physical_members",
            "vlm_candidate_sha256", "audio_stream_facts", "expected_registry_sha256", "expected_profile_reference",
            "expected_asr_calibration_sha256", "expected_vad_calibration_sha256", "expected_native_port_identity_sha256",
            "policy", "decoder_identity_sha256", "max_attempts", "max_outward_padding_audio_ticks",
            "max_decode_frames", "max_frame_bytes", "max_pcm_bytes", "read_limits",
        ))
        if (type(parent) is not ResolvedPreparePhysicalMediaEvidenceRequest
                or r["schema_version"] != LOCAL_SPEECH_WINDOW_LIFECYCLE_VERSION
                or physical_json(r["parent"]) != physical_json(parent.canonical_payload())
                or physical_json(r["adaptive_policy"]) != physical_json(parent.source.adaptive_policy.to_mapping())):
            raise LocalSpeechWindowContractError("persisted lifecycle parent/policy/version differs")
        if type(r["physical_members"]) is not list:
            raise LocalSpeechWindowContractError("physical members must be an array")
        return cls(parent, LocalSpeechWindowSuccessHandle.from_mapping(r["physical_predecessor"]),
                   tuple(CommittedArtifactMemberReference.from_mapping(ref) for ref in cast(list[object], r["physical_members"])),
                   _hash(r["vlm_candidate_sha256"]), decode_audio_stream_facts(r["audio_stream_facts"]),
                   _hash(r["expected_registry_sha256"]), CommittedArtifactMemberReference.from_mapping(r["expected_profile_reference"]),
                   _hash(r["expected_asr_calibration_sha256"]), _hash(r["expected_vad_calibration_sha256"]),
                   _hash(r["expected_native_port_identity_sha256"]), decode_local_speech_window_policy(r["policy"]),
                   _hash(r["decoder_identity_sha256"]), _integer(r["max_attempts"]),
                   _integer(r["max_outward_padding_audio_ticks"], minimum=0), _integer(r["max_decode_frames"]),
                   _integer(r["max_frame_bytes"]), _integer(r["max_pcm_bytes"]), LocalSpeechWindowReadLimits.from_mapping(r["read_limits"]))

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class PrepareLocalSpeechWindowChildRequest:
    lifecycle: LocalSpeechWindowLifecycle
    window: CandidateEvidenceWindow
    extraction: LocalAudioWindowSpec
    attempt_ordinal: int
    previous_expansion: LocalSpeechWindowSuccessHandle | None
    previous_busy: LocalSpeechWindowBusyHandle | None

    def __post_init__(self) -> None:
        if (type(self.lifecycle) is not LocalSpeechWindowLifecycle
                or type(self.window) is not CandidateEvidenceWindow or type(self.extraction) is not LocalAudioWindowSpec):
            raise LocalSpeechWindowContractError("child requires exact lifecycle/window/extraction")
        life, window, spec = self.lifecycle, self.window, self.extraction
        _integer(self.attempt_ordinal)
        _integer(window.expansion_ordinal, minimum=0)
        if self.attempt_ordinal > life.max_attempts or window.expansion_ordinal > life.parent.source.adaptive_policy.max_expansion_count:
            raise LocalSpeechWindowContractError("child exceeds frozen retry/expansion budget")
        for ordinal, previous, expected_type in (
            (window.expansion_ordinal, self.previous_expansion, LocalSpeechWindowSuccessHandle),
            (self.attempt_ordinal - 1, self.previous_busy, LocalSpeechWindowBusyHandle),
        ):
            if (ordinal == 0 and previous is not None) or (ordinal > 0 and type(previous) is not expected_type):
                raise LocalSpeechWindowContractError("child ordinal requires exactly its predecessor handle or null")
            if previous is not None and previous.job_id != life.physical_predecessor.job_id:
                raise LocalSpeechWindowContractError("child predecessor belongs to another Job UUID")
        source, facts = life.parent.source, life.audio_stream_facts
        video = source.frame_pts_index.context
        if (window.source_id, window.source_sha256, window.source_clock_id, window.source_time_base,
                window.source_range, window.vlm_candidate_sha256, window.vlm_request_identity_sha256,
                window.window_manifest_sha256, window.frame_pts_index_set_sha256) != (
            video.source_id, video.source_sha256, video.clock_id, video.time_base,
            TickRange(video.origin_tick, video.end_tick), life.vlm_candidate_sha256,
            source.semantic_pack.request_identity_sha256, source.window_manifest.canonical_hash,
            source.frame_pts_index.canonical_hash,
        ):
            raise LocalSpeechWindowContractError("child window does not bind the lifecycle Source/VLM")
        if (spec.source_id, spec.source_sha256, spec.audio_stream_index, spec.clock_id, spec.time_base,
                spec.source_range, spec.sample_rate, spec.channels, spec.audio_boundary_set_sha256,
                spec.decoder_identity_sha256, spec.max_source_bytes, spec.max_decode_frames,
                spec.max_frame_bytes, spec.max_pcm_bytes) != (
            facts.source_id, facts.source_sha256, facts.stream_index, facts.clock_id, facts.time_base,
            TickRange(facts.origin_tick, facts.end_tick), facts.sample_rate, facts.channels,
            facts.audio_sample_boundary_set_sha256, life.decoder_identity_sha256,
            source.materialization_limits.effective_max_source_bytes, life.max_decode_frames,
            life.max_frame_bytes, life.max_pcm_bytes,
        ):
            raise LocalSpeechWindowContractError("extraction differs from frozen measured audio/decoder/limits")

    def _binding_mapping(self) -> dict[str, object]:
        return {"schema_version": "local-speech-window-child-request-v1",
                "lifecycle": self.lifecycle.to_mapping(), "window": self.window.to_mapping(),
                "extraction": self.extraction.to_mapping(), "attempt_ordinal": self.attempt_ordinal,
                "previous_expansion": None if self.previous_expansion is None else self.previous_expansion.to_mapping(),
                "previous_busy": None if self.previous_busy is None else self.previous_busy.to_mapping()}

    @property
    def binding_sha256(self) -> str:
        return canonical_sha256(self._binding_mapping())

    @property
    def wire_request(self) -> LocalSpeechWindowRequest:
        return LocalSpeechWindowRequest(self.extraction, self.lifecycle.policy, self.binding_sha256,
                                        self.lifecycle.read_limits.max_raw_response_bytes)

    def to_mapping(self) -> dict[str, object]:
        return {**self._binding_mapping(), "wire_request": self.wire_request.to_mapping()}

    @classmethod
    def from_mapping(cls, value: object, *, parent: ResolvedPreparePhysicalMediaEvidenceRequest) -> PrepareLocalSpeechWindowChildRequest:
        r = _object(value, ("schema_version", "lifecycle", "window", "extraction", "attempt_ordinal",
                            "previous_expansion", "previous_busy", "wire_request"))
        result = cls(LocalSpeechWindowLifecycle.from_mapping(r["lifecycle"], parent=parent),
                     decode_candidate_evidence_window(r["window"]), decode_local_audio_window_spec(r["extraction"]),
                     _integer(r["attempt_ordinal"]),
                     None if r["previous_expansion"] is None else LocalSpeechWindowSuccessHandle.from_mapping(r["previous_expansion"]),
                     None if r["previous_busy"] is None else LocalSpeechWindowBusyHandle.from_mapping(r["previous_busy"]))
        if physical_json(r) != physical_json(result.to_mapping()):
            raise LocalSpeechWindowContractError("child wire request/version differs from recomputed binding")
        return result

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def request_hash(self) -> str:
        return self.canonical_hash

    @property
    def idempotency_key(self) -> str:
        return (f"local-speech:{self.lifecycle.canonical_hash}:"
                f"expansion:{self.window.expansion_ordinal}:attempt:{self.attempt_ordinal}")
