"""Resolve calibration inputs from exact committed sources and independent anchors.

The deployment owns verified authority loading and corpus selection. This seam
reads source facts only; it neither calls a provider nor grants runtime authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from autocut_kernel.contracts.compiler.canonical import canonical_json_hash
from autocut_kernel.media import (
    CalibrationAnchor,
    CalibrationProducer,
    ShadowCalibrationAudioClock,
    ShadowCalibrationContainer,
    ShadowCalibrationInvocation,
    ShadowCalibrationPolicies,
    ShadowCalibrationProducerIdentity,
    ShadowCalibrationRawContext,
    ShadowCalibrationRequestMapping,
    ShadowCalibrationSource,
    ShadowCalibrationSourceByteLimits,
    ShadowCalibrationTranscriptCapability,
    shadow_calibration_anchor_reference_sha256,
)
from autocut_kernel.media.types import sha256_prefixed
from autocut_kernel.pipeline.measure_shadow_calibration_command import (
    MeasureShadowCalibrationRequest,
    ShadowCalibrationCorpusMember,
    ShadowCalibrationInputs,
)
from autocut_kernel.registry.authority_profiles import (
    CalibrationCorpusMember,
    NativeTimedSpeechProducer,
    ShadowCalibrationProfileSource,
    Stage1NarrativeProfileSource,
)
from autocut_kernel.source_manifest import decode_source_manifest
from autocut_kernel.store import BlobRef, CommittedArtifactMemberReference, Job
from autocut_kernel.store.models import (
    MaterializationLimits,
    PersistedWholeSeriesSourceManifest,
    canonical_recipe_scope,
)

from .shadow_calibration_http import ShadowCalibrationSourceBinding
from .shadow_calibration_service_profile import build_funasr_shadow_service_profile


class ShadowCalibrationInputError(ValueError):
    """The deployment inputs do not close over the committed corpus."""


def _hash(value: str, name: str) -> None:
    sha256_prefixed(value, name)
    if value == "sha256:" + "0" * 64:
        raise ShadowCalibrationInputError(f"{name} cannot be zero")


@dataclass(frozen=True, slots=True)
class CommittedCalibrationSourceHandle:
    corpus_member_reference_sha256: str
    owner_job: Job
    manifest_reference: CommittedArtifactMemberReference
    command_slot_id: UUID
    source_provenance_sha256: str
    asr_anchors: tuple[CalibrationAnchor, ...]
    vad_anchors: tuple[CalibrationAnchor, ...]

    def __post_init__(self) -> None:
        _hash(self.corpus_member_reference_sha256, "corpus reference")
        _hash(self.source_provenance_sha256, "source provenance")
        if (
            type(self.owner_job) is not Job
            or type(self.manifest_reference) is not CommittedArtifactMemberReference
            or type(self.command_slot_id) is not UUID
        ):
            raise ShadowCalibrationInputError("source handle requires exact typed identities")
        reference = self.manifest_reference
        if (
            reference.artifact_type != "whole_series_source_manifest"
            or reference.logical_id != "whole_series_source_manifest"
            or reference.member_ordinal != 0
            or reference.scope != canonical_recipe_scope(self.owner_job)
        ):
            raise ShadowCalibrationInputError("source handle is not the owner's source member")
        for anchors in (self.asr_anchors, self.vad_anchors):
            if type(anchors) is not tuple or not anchors or any(
                type(anchor) is not CalibrationAnchor for anchor in anchors
            ):
                raise ShadowCalibrationInputError("independent anchors must be nonempty typed tuples")


class CalibrationSourceStore(Protocol):
    def read_whole_series_source_manifest(
        self, job: Job, artifact_set_id: UUID
    ) -> PersistedWholeSeriesSourceManifest: ...


@dataclass(frozen=True, slots=True)
class ResolvedShadowCalibrationInputs:
    request: MeasureShadowCalibrationRequest
    source_bindings: tuple[ShadowCalibrationSourceBinding, ...]
    source_handles: tuple[CommittedCalibrationSourceHandle, ...]
    service_profile_bytes: bytes


def _producer(value: NativeTimedSpeechProducer) -> ShadowCalibrationProducerIdentity:
    return ShadowCalibrationProducerIdentity(
        CalibrationProducer(value.producer_kind), value.producer_id, value.producer_version,
        value.generation_policy_sha256, value.detector_sha256, value.calibration_policy_sha256,
        value.model_id, value.model_revision, value.model_sha256, value.inference_kind,
        value.service_sha256,
    )


def _verify_handle(
    persisted: PersistedWholeSeriesSourceManifest, handle: CommittedCalibrationSourceHandle
) -> None:
    reference = persisted.reference
    actual = CommittedArtifactMemberReference(
        persisted.receipt_id, persisted.artifact_set_id, 0, reference.scope,
        reference.artifact_type, reference.logical_id, reference.revision, reference.content_hash,
    )
    if (
        actual != handle.manifest_reference
        or persisted.command_slot_id != handle.command_slot_id
        or persisted.source_job != handle.owner_job
        or persisted.canonical_hash != handle.source_provenance_sha256
    ):
        raise ShadowCalibrationInputError("source handle differs from committed provenance")


def _resolve_member(
    persisted: PersistedWholeSeriesSourceManifest,
    handle: CommittedCalibrationSourceHandle,
    locked: CalibrationCorpusMember,
    profile: ShadowCalibrationProfileSource,
    limits: MaterializationLimits,
    max_response_bytes: int,
) -> tuple[ShadowCalibrationCorpusMember, ShadowCalibrationSourceBinding]:
    _verify_handle(persisted, handle)
    decoded = decode_source_manifest(persisted.payload_json, persisted.proxy_blobs)
    matches = tuple(
        episode for episode in decoded.episodes
        if episode.media_probe.source.source_id == locked.source_id
    )
    if len(matches) != 1:
        raise ShadowCalibrationInputError("locked source is not unique in its committed manifest")
    episode = matches[0]
    source, blob = episode.media_probe.source, episode.proxy_blob
    reference = BlobRef(blob.object_id, blob.content_hash, blob.byte_length, blob.media_type)
    if (
        blob.content_hash != source.content_sha256
        or blob.content_hash != locked.source_sha256
        or blob.byte_length != source.byte_size
        or blob.byte_length > limits.effective_max_source_bytes
        or blob.media_type != "video/mp4"
        or canonical_json_hash({
            "object_id": str(blob.object_id), "content_hash": blob.content_hash,
            "byte_length": blob.byte_length, "media_type": blob.media_type,
        }) != locked.source_blob_reference_sha256
    ):
        raise ShadowCalibrationInputError("calibration requires the locked original source bytes")
    audio = episode.media_probe.audio_sample_boundaries.context
    if (audio.clock_id, audio.time_base) != (
        profile.source_clock_policy.clock_id, profile.source_clock_policy.time_base
    ):
        raise ShadowCalibrationInputError("committed audio clock differs from the locked policy")
    clock = ShadowCalibrationAudioClock(
        audio.clock_id, audio.time_base, audio.origin_tick, audio.duration_tick
    )
    policy = profile.timing_policies
    producers = (_producer(profile.native_timed_speech.producers[0]),
                 _producer(profile.native_timed_speech.producers[1]))
    context = ShadowCalibrationRawContext(
        ShadowCalibrationSource(
            source.source_id, source.content_sha256, locked.corpus_member_reference_sha256,
            str(blob.object_id), blob.content_hash, blob.byte_length, blob.media_type,
        ),
        ShadowCalibrationSourceByteLimits(
            limits.max_source_bytes, limits.timed_speech_max_request_bytes,
            limits.effective_max_source_bytes,
        ),
        ShadowCalibrationContainer("video/mp4", ".mp4"), clock,
        ShadowCalibrationPolicies(
            policy.timed_speech_policy_sha256, policy.word_gap_policy_sha256,
            policy.vad_merge_policy_sha256, policy.word_gap_ms, policy.vad_merge_gap_ms,
        ),
        profile.native_timed_speech.native_port_identity_sha256,
        ShadowCalibrationTranscriptCapability(
            "sensevoice_word_guard_v1", "complete", "utterance_gap_protected_range",
            "not_applicable", "complete", "required",
        ),
        *producers, handle.asr_anchors, handle.vad_anchors,
    )
    if shadow_calibration_anchor_reference_sha256(context) != locked.expected_anchor_reference_sha256:
        raise ShadowCalibrationInputError("independent anchors differ from the locked corpus")
    mapping = ShadowCalibrationRequestMapping(
        context.source, context.source_byte_limits, context.container, clock, clock.full_range,
        context.native_profile_identity_sha256, max_response_bytes, context.transcript_capability,
        policy.timed_speech_policy_sha256, policy.word_gap_policy_sha256,
        policy.vad_merge_policy_sha256, policy.word_gap_ms, policy.vad_merge_gap_ms, producers,
    )
    invocation = ShadowCalibrationInvocation(
        locked.corpus_member_reference_sha256, mapping.sha256, mapping, mapping.sha256
    )
    return (
        ShadowCalibrationCorpusMember(
            locked.corpus_member_reference_sha256, locked.expected_anchor_reference_sha256,
            context, invocation,
        ),
        ShadowCalibrationSourceBinding(locked.corpus_member_reference_sha256, handle.owner_job, reference),
    )


def resolve_shadow_calibration_inputs(
    *,
    store: CalibrationSourceStore,
    profile: ShadowCalibrationProfileSource,
    narrative: Stage1NarrativeProfileSource,
    expected_profile_contract_sha256: str,
    registry_snapshot_sha256: str,
    source_handles: tuple[CommittedCalibrationSourceHandle, ...],
    limits: MaterializationLimits,
    max_response_bytes: int,
) -> ResolvedShadowCalibrationInputs:
    service_profile = build_funasr_shadow_service_profile(
        profile=profile, narrative=narrative,
        expected_profile_contract_sha256=expected_profile_contract_sha256,
    )
    _hash(registry_snapshot_sha256, "registry snapshot")
    if (
        type(source_handles) is not tuple
        or any(type(item) is not CommittedCalibrationSourceHandle for item in source_handles)
        or type(limits) is not MaterializationLimits
        or type(max_response_bytes) is not int or max_response_bytes <= 0
        or limits.timed_speech_max_request_bytes != profile.native_timed_speech.max_request_bytes
    ):
        raise ShadowCalibrationInputError("calibration deployment input types or limits are invalid")
    locked_members = profile.calibration_corpus.members
    if tuple(item.corpus_member_reference_sha256 for item in source_handles) != tuple(
        item.corpus_member_reference_sha256 for item in locked_members
    ):
        raise ShadowCalibrationInputError("source handles must exactly cover the ordered locked corpus")
    cache: dict[tuple[Job, UUID], PersistedWholeSeriesSourceManifest] = {}
    members: list[ShadowCalibrationCorpusMember] = []
    bindings: list[ShadowCalibrationSourceBinding] = []
    for handle, locked in zip(source_handles, locked_members, strict=True):
        key = (handle.owner_job, handle.manifest_reference.artifact_set_id)
        if key not in cache:
            cache[key] = store.read_whole_series_source_manifest(*key)
        member, binding = _resolve_member(cache[key], handle, locked, profile, limits, max_response_bytes)
        members.append(member)
        bindings.append(binding)
    policy, clock, native = profile.timing_policies, profile.source_clock_policy, profile.native_timed_speech
    inputs = ShadowCalibrationInputs(
        profile.source_sha256, registry_snapshot_sha256, profile.calibration_corpus.corpus_set_sha256,
        native.native_port_identity_sha256, policy.word_gap_policy_sha256, policy.vad_merge_policy_sha256,
        policy.alignment_policy_sha256, policy.acceptance_policy_sha256,
        native.producers[0].producer_id, native.producers[1].producer_id, clock.clock_id, clock.time_base,
    )
    request = MeasureShadowCalibrationRequest(inputs, tuple(members))
    if any(binding.owner_job == request.job for binding in bindings):
        raise ShadowCalibrationInputError("measurement Job cannot be its own source predecessor")
    return ResolvedShadowCalibrationInputs(request, tuple(bindings), source_handles, service_profile)
