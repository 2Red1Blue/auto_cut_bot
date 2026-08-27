"""Independent replay of the PC-CUDA timed-media predecessor.

This module deliberately does not adapt the historical CPU ``local_run``
reader.  A CUDA result is useful only when its own accepted runtime capability
can be re-resolved from the Store and every persisted member closes against
that projection again.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import urlparse
from uuid import UUID

from ..media.presentation_evidence_codec import (
    decode_committed_video_to_audio_clock_map_certificate,
)
from ..media.root_evidence_codec import (
    decode_media_evidence_json,
    decode_root_media_evidence_bundle,
)
from ..media.stage4_predecessor import (
    CommittedVideoToAudioClockMapCertificate,
    derive_presentation_timeline_facts,
)
from ..media.timed_evidence import CandidateEvidenceWindowPlan, CandidateTimedEvidenceSet
from ..media.timed_evidence_codec import (
    decode_calibration_binding,
    decode_candidate_evidence_window_plan,
    decode_candidate_timed_evidence_set,
)
from ..media.types import canonical_sha256
from ..registry.installed_runtime import (
    InstalledRuntimeCapabilityStore,
    InstalledRuntimeTimedSpeechAuthorityResolver,
)
from ..registry.runtime_timed_speech import (
    RuntimeTimedSpeechCapabilityAdmission,
    RuntimeTimedSpeechProjection,
)
from ..store.models import BlobRef, CommandOutcome, Job, PersistedCommittedArtifactSet
from .committed_timed_media import TimedMediaReadLimits
from .prepare_runtime_timed_media_evidence_command import (
    PREPARE_RUNTIME_TIMED_MEDIA_EVIDENCE_COMMAND,
    PrepareRuntimeTimedMediaEvidenceRequest,
    ProducedRuntimeTimedMediaEvidence,
    RuntimeTimedMediaEvidenceStore,
)
from .prepare_timed_media_evidence_command import (
    RUNTIME_CUDA_MEDIA_PRODUCER_PROVENANCE_SCHEMA,
    ProducedTimedMediaEvidence,
    ResolvedPrepareTimedMediaEvidenceRequest,
    close_timed_media_candidates,
    resolve_committed_timed_media_request,
    validate_produced_timed_media_evidence,
)


class RuntimeTimedMediaReadError(ValueError):
    """A committed CUDA predecessor cannot be reconstructed safely."""


class RuntimeTimedMediaReadStore(
    RuntimeTimedMediaEvidenceStore, InstalledRuntimeCapabilityStore, Protocol
):
    def read_committed_artifact_set(
        self,
        job: Job,
        *,
        command_slot_id: UUID,
        receipt_id: UUID,
        artifact_set_id: UUID,
        expected_request_hash: str,
        expected_command_name: str,
        expected_execution_kind: str,
    ) -> PersistedCommittedArtifactSet: ...


@dataclass(frozen=True, slots=True)
class PersistedRuntimeTimedMediaEvidence:
    record: PersistedCommittedArtifactSet
    request: ResolvedPrepareTimedMediaEvidenceRequest
    produced: ProducedRuntimeTimedMediaEvidence
    projection: RuntimeTimedSpeechProjection
    admission: RuntimeTimedSpeechCapabilityAdmission
    plans: tuple[CandidateEvidenceWindowPlan, ...]
    candidates: tuple[CandidateTimedEvidenceSet, ...]
    certificate: CommittedVideoToAudioClockMapCertificate


@dataclass(frozen=True, slots=True)
class RuntimeTimedMediaEvidenceMetadata:
    """The first pass intentionally retains only compact immutable identities."""

    record: PersistedCommittedArtifactSet
    blob_refs: tuple[BlobRef, ...]
    projection: RuntimeTimedSpeechProjection


def _object(value: object, fields: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict or set(cast(dict[object, object], value)) != set(fields):  # noqa: E721
        raise RuntimeTimedMediaReadError("runtime timed-media object has missing or unknown fields")
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    if type(value) is not list:  # noqa: E721
        raise RuntimeTimedMediaReadError("runtime timed-media collection must be a JSON array")
    return cast(list[object], value)


def _blob(value: object, kind: str) -> BlobRef:
    raw = _object(value, ("object_id", "content_hash", "byte_length", "media_type"))
    length = raw["byte_length"]
    if type(length) is not int or length <= 0:  # noqa: E721
        raise RuntimeTimedMediaReadError("runtime evidence blob length must be positive")
    try:
        reference = BlobRef(
            UUID(cast(str, raw["object_id"])),
            cast(str, raw["content_hash"]),
            length,
            cast(str, raw["media_type"]),
        )
    except (TypeError, ValueError) as error:
        raise RuntimeTimedMediaReadError("runtime evidence BlobRef is invalid") from error
    if reference.media_type != f"application/vnd.autocut.{kind}+json":
        raise RuntimeTimedMediaReadError("runtime evidence blob media type differs")
    return reference


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _read_blob(
    store: RuntimeTimedMediaReadStore,
    job: Job,
    reference: BlobRef,
    limits: TimedMediaReadLimits,
) -> object:
    lease = store.materialize_immutable_blob(job, reference, limits.materialization)
    try:
        if lease.reference != reference:
            raise RuntimeTimedMediaReadError("runtime materialized lease differs from committed BlobRef")
        with lease.path.open("rb") as stream:
            raw = stream.read(reference.byte_length + 1)
        if (
            len(raw) != reference.byte_length
            or "sha256:" + hashlib.sha256(raw).hexdigest() != reference.content_hash
        ):
            raise RuntimeTimedMediaReadError("runtime evidence blob hash or length differs")
        return decode_media_evidence_json(raw, max_bytes=limits.max_blob_bytes)
    finally:
        lease.close()


def _require_resolver(
    resolver: InstalledRuntimeTimedSpeechAuthorityResolver,
) -> InstalledRuntimeTimedSpeechAuthorityResolver:
    if type(resolver) is not InstalledRuntimeTimedSpeechAuthorityResolver:  # noqa: E721
        raise RuntimeTimedMediaReadError("runtime reader requires the installed CUDA authority resolver")
    return resolver


def _assert_projection_for_source(
    projection: RuntimeTimedSpeechProjection,
    request: ResolvedPrepareTimedMediaEvidenceRequest,
) -> None:
    audio = request.audio_sample_boundaries.context
    if (
        projection.runtime_capability_id != "pc_cuda"
        or projection.device_class != "cuda"
        or projection.source_clock_id != audio.clock_id
        or projection.source_time_base != audio.time_base
    ):
        raise RuntimeTimedMediaReadError("resolved CUDA authority does not close the source audio clock")


def _runtime_admission(
    projection: RuntimeTimedSpeechProjection,
    evidence: ProducedTimedMediaEvidence,
) -> RuntimeTimedSpeechCapabilityAdmission:
    bindings = {item.producer_id: item for item in evidence.calibration_bindings}
    try:
        pair = (
            bindings[projection.producers[0].producer_id],
            bindings[projection.producers[1].producer_id],
        )
    except KeyError as error:
        raise RuntimeTimedMediaReadError("runtime evidence lost selected ASR/VAD bindings") from error
    return RuntimeTimedSpeechCapabilityAdmission(projection, evidence.root_bundle, pair)


def _validate_runtime_output(
    resolved: ResolvedPrepareTimedMediaEvidenceRequest,
    produced: ProducedRuntimeTimedMediaEvidence,
    projection: RuntimeTimedSpeechProjection,
    *,
    static_policy_sha256: str,
) -> None:
    """Repeat the CUDA-only wire closure without invoking the CPU command path."""
    if produced.runtime_projection != projection:
        raise RuntimeTimedMediaReadError("runtime evidence projection differs from fresh Store projection")
    validate_produced_timed_media_evidence(
        resolved,
        produced.evidence,
        expected_provenance_schema=RUNTIME_CUDA_MEDIA_PRODUCER_PROVENANCE_SCHEMA,
    )
    authority = produced.runtime_authority_mapping
    if produced.evidence.producer_policy_sha256 != canonical_sha256(authority):
        raise RuntimeTimedMediaReadError("runtime evidence policy hash differs from authority")
    try:
        provenance = json.loads(produced.evidence.producer_provenance_json)
    except (TypeError, ValueError) as error:
        raise RuntimeTimedMediaReadError("runtime provenance is unavailable") from error
    if type(provenance) is not dict:  # noqa: E721
        raise RuntimeTimedMediaReadError("runtime provenance does not bind authority")
    provenance_mapping = cast(dict[str, object], provenance)
    if provenance_mapping.get("runtime_timed_speech_authority") != authority:
        raise RuntimeTimedMediaReadError("runtime provenance does not bind authority")
    _validate_runtime_authority_mapping(
        authority,
        projection,
        static_policy_sha256=static_policy_sha256,
    )


def _validate_runtime_authority_mapping(
    authority: object,
    projection: RuntimeTimedSpeechProjection,
    *,
    static_policy_sha256: str,
) -> None:
    """Recompute every closed field of the persisted CUDA authority mapping."""
    expected_keys = {
        "schema_version", "static_policy_sha256", "runtime_capability_id", "device",
        "runtime_measurement_identity_sha256", "timing_compatibility_sha256",
        "runtime_projection_compatibility_sha256", "runtime", "profile_source_sha256",
        "registry_snapshot_sha256", "accepted_record_sha256", "validation_receipt_sha256",
        "native_port_identity_sha256", "source_clock", "timing", "operation", "producers",
        "build_audit_sha256", "runtime_projection_sha256",
    }
    if type(authority) is not dict:  # noqa: E721
        raise RuntimeTimedMediaReadError("runtime authority mapping is not closed")
    raw_authority = cast(dict[object, object], authority)
    if set(raw_authority) != expected_keys:
        raise RuntimeTimedMediaReadError("runtime authority mapping is not closed")
    authority = cast(dict[str, object], authority)
    if authority.get("schema_version") != "pc-cuda-runtime-timed-speech-policy-v1":
        raise RuntimeTimedMediaReadError("runtime authority version is invalid")
    timing = _object(
        authority["timing"],
        (
            "timed_speech_policy_sha256",
            "word_gap_policy_sha256",
            "vad_merge_policy_sha256",
            "alignment_policy_sha256",
            "acceptance_policy_sha256",
            "utterance_gap_milliseconds",
            "vad_merge_gap_milliseconds",
        ),
    )
    source_clock = _object(authority["source_clock"], ("clock_id", "time_base"))
    runtime = _object(authority["runtime"], ("funasr_version", "torch_version"))
    producers = authority["producers"]
    if type(producers) is not list:  # noqa: E721
        raise RuntimeTimedMediaReadError("runtime authority producers schema is invalid")
    producers = cast(list[object], producers)
    if runtime != {
        "funasr_version": projection.funasr_version,
        "torch_version": projection.torch_version,
    }:
        raise RuntimeTimedMediaReadError("runtime authority library versions differ")
    if source_clock != {
        "clock_id": projection.source_clock_id,
        "time_base": {
            "numerator": projection.source_time_base.numerator,
            "denominator": projection.source_time_base.denominator,
        },
    }:
        raise RuntimeTimedMediaReadError("runtime authority source clock differs")
    if (
        authority.get("runtime_capability_id") != projection.runtime_capability_id
        or authority.get("device") != "cuda"
        or authority.get("runtime_measurement_identity_sha256")
        != projection.runtime_measurement_identity_sha256
        or authority.get("timing_compatibility_sha256") != projection.timing_compatibility_sha256
        or authority.get("runtime_projection_sha256") != projection.canonical_hash
        or authority.get("runtime_projection_compatibility_sha256") != projection.compatibility_hash
        or authority.get("accepted_record_sha256") != projection.record_sha256
        or authority.get("validation_receipt_sha256") != projection.validation_receipt_sha256
        or authority.get("native_port_identity_sha256") != projection.native_port_identity_sha256
        or authority.get("build_audit_sha256") != projection.build_audit_sha256
        or authority.get("profile_source_sha256") != projection.profile_source_sha256
        or authority.get("registry_snapshot_sha256") != projection.registry_snapshot_sha256
    ):
        raise RuntimeTimedMediaReadError("runtime authority differs from fresh Store projection")
    expected_timing_hashes = {
        "timed_speech_policy_sha256": projection.timed_speech_policy_sha256,
        "word_gap_policy_sha256": projection.word_gap_policy_sha256,
        "vad_merge_policy_sha256": projection.vad_merge_policy_sha256,
        "alignment_policy_sha256": projection.alignment_policy_sha256,
        "acceptance_policy_sha256": projection.acceptance_policy_sha256,
    }
    if any(timing[name] != value for name, value in expected_timing_hashes.items()):
        raise RuntimeTimedMediaReadError("runtime authority timing policies differ")
    for name in ("utterance_gap_milliseconds", "vad_merge_gap_milliseconds"):
        gap = timing[name]
        if type(gap) is not int or gap < 0:  # noqa: E721
            raise RuntimeTimedMediaReadError("runtime authority timing gaps are invalid")
    operation = _object(
        authority["operation"],
        ("endpoint_url", "provider_id", "provider_version", "timeout_seconds", "max_response_bytes"),
    )
    endpoint = operation["endpoint_url"]
    if type(endpoint) is not str:  # noqa: E721
        raise RuntimeTimedMediaReadError("runtime operation endpoint is invalid")
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None
        or not 1 <= parsed.port <= 65_535
        or parsed.path != "/v2/runtime-timed-speech-evidence" or parsed.params
        or parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None
    ):
        raise RuntimeTimedMediaReadError("runtime operation cannot use a legacy or non-loopback endpoint")
    for name in ("provider_id", "provider_version"):
        value = operation[name]
        if type(value) is not str or not value or value != value.strip():  # noqa: E721
            raise RuntimeTimedMediaReadError("runtime operation provider identity is invalid")
    for name in ("timeout_seconds", "max_response_bytes"):
        limit = operation[name]
        if type(limit) is not int or limit <= 0:  # noqa: E721
            raise RuntimeTimedMediaReadError("runtime operation limits are invalid")
    expected_producers = [
        {
            **item.to_mapping(), "calibration_record_sha256": record_sha256,
            "timing_error_bound_tick": bound_tick,
        }
        for item, record_sha256, bound_tick in zip(
            projection.producers,
            (projection.asr_calibration_record_sha256, projection.vad_calibration_record_sha256),
            (projection.asr_timing_error_bound_tick, projection.vad_timing_error_bound_tick),
            strict=True,
        )
    ]
    if producers != expected_producers:
        raise RuntimeTimedMediaReadError("runtime authority producers differ from fresh projection")


def _record(
    store: RuntimeTimedMediaReadStore,
    request: PrepareRuntimeTimedMediaEvidenceRequest,
    resolved: ResolvedPrepareTimedMediaEvidenceRequest,
    outcome: CommandOutcome,
    projection: RuntimeTimedSpeechProjection,
) -> PersistedCommittedArtifactSet:
    if (
        type(outcome) is not CommandOutcome  # noqa: E721
        or outcome.state != "succeeded"
        or outcome.job_id is None
        or outcome.receipt_id is None
        or outcome.artifact_set_id is None
        or outcome.failure_code is not None
        or outcome.failure_detail_json is not None
    ):
        raise RuntimeTimedMediaReadError("runtime reader requires an exact succeeded outcome")
    request_hash = request.request_hash_for(projection)
    record = store.read_committed_artifact_set(
        request.job,
        command_slot_id=outcome.command_slot_id,
        receipt_id=outcome.receipt_id,
        artifact_set_id=outcome.artifact_set_id,
        expected_request_hash=request_hash,
        expected_command_name=PREPARE_RUNTIME_TIMED_MEDIA_EVIDENCE_COMMAND,
        expected_execution_kind="deterministic",
    )
    if type(record) is not PersistedCommittedArtifactSet or (  # noqa: E721
        record.job != request.job
        or record.job_id != outcome.job_id
        or record.command_slot_id != outcome.command_slot_id
        or record.receipt_id != outcome.receipt_id
        or record.artifact_set_id != outcome.artifact_set_id
        or record.request_hash != request_hash
        or record.command_name != PREPARE_RUNTIME_TIMED_MEDIA_EVIDENCE_COMMAND
        or record.execution_kind != "deterministic"
    ):
        raise RuntimeTimedMediaReadError("runtime Store record differs from requested identity")
    expected = (
        ("root_media_evidence_bundle", "root_media_evidence"),
        ("candidate_timed_evidence_index", "candidate_timed_evidence"),
        ("runtime_timed_speech_capability_admission", "runtime_timed_speech_capability_admission"),
        ("presentation_timeline_probe", "presentation_timeline_probe"),
        ("committed_video_to_audio_clock_map_certificate", "video_to_audio_clock_map"),
    )
    if len(record.members) != len(expected):
        raise RuntimeTimedMediaReadError("runtime committed set must contain exactly five members")
    for member, (kind, prefix) in zip(record.members, expected, strict=True):
        reference = member.reference
        if (
            reference.artifact_type != kind
            or reference.logical_id != f"{prefix}_episode_{resolved.episode_index:04d}"
            or reference.scope != resolved.artifact_scope
            or reference.revision != resolved.artifact_revision
        ):
            raise RuntimeTimedMediaReadError("runtime member type, episode, scope or revision differs")
    return record


def _inspect(
    store: RuntimeTimedMediaReadStore,
    request: PrepareRuntimeTimedMediaEvidenceRequest,
    outcome: CommandOutcome,
    *,
    authority_resolver: InstalledRuntimeTimedSpeechAuthorityResolver,
    limits: TimedMediaReadLimits,
) -> tuple[ResolvedPrepareTimedMediaEvidenceRequest, RuntimeTimedMediaEvidenceMetadata, tuple[object, ...]]:
    resolver = _require_resolver(authority_resolver)
    if type(limits) is not TimedMediaReadLimits:  # noqa: E721
        raise RuntimeTimedMediaReadError("runtime reader requires exact explicit limits")
    resolved = resolve_committed_timed_media_request(store, request.timed_media_request)
    projection = resolver.resolve(store, request.runtime_measurement_identity)
    _assert_projection_for_source(projection, resolved)
    record = _record(store, request, resolved, outcome, projection)
    payloads = tuple(
        decode_media_evidence_json(member.payload_json.encode("utf-8"), max_bytes=limits.max_blob_bytes)
        for member in record.members
    )
    root_data = _object(payloads[0], (
        "blob", "calibration_bindings", "episode_index", "producer_provenance_blob",
        "producer_provenance_sha256", "producer_policy_blob", "producer_policy_sha256",
        "root_bundle_sha256", "source_manifest_sha256", "source_provenance_sha256",
        "video_to_audio_presentation_map_sha256",
    ))
    index = _object(payloads[1], (
        "candidate_blobs", "candidate_count", "candidate_index_state", "candidate_set_sha256",
        "episode_index", "plan_blob", "plan_set_sha256", "schema_version", "semantic_pack_sha256",
        "presentation_map_facts_sha256", "presentation_timeline_probe_sha256",
        "video_to_audio_presentation_map_sha256",
    ))
    raw_candidates = _array(index["candidate_blobs"])
    expected_count = len(resolved.request.semantic_pack.candidate_hypotheses)
    if (
        len(raw_candidates) > limits.max_candidates
        or expected_count > limits.max_candidates
        or len(raw_candidates) != expected_count
        or type(index["candidate_count"]) is not int  # noqa: E721
        or index["candidate_count"] != expected_count
    ):
        raise RuntimeTimedMediaReadError("runtime candidate count differs from committed VLM")
    refs = (
        _blob(root_data["blob"], "root-media-evidence"),
        _blob(root_data["producer_policy_blob"], "local-media-preflight-policy"),
        _blob(root_data["producer_provenance_blob"], "local-media-producer-provenance"),
        _blob(index["plan_blob"], "candidate-window-plans"),
        *(_blob(item, "candidate-timed-evidence") for item in raw_candidates),
    )
    if (
        any(ref.byte_length > limits.max_blob_bytes for ref in refs)
        or sum(ref.byte_length for ref in refs) > limits.max_total_blob_bytes
        or len({ref.object_id for ref in refs}) != len(refs)
    ):
        raise RuntimeTimedMediaReadError("runtime evidence BlobRefs exceed byte ceilings or alias each other")
    return resolved, RuntimeTimedMediaEvidenceMetadata(record, refs, projection), payloads


def inspect_committed_runtime_timed_media_evidence(
    store: RuntimeTimedMediaReadStore,
    request: PrepareRuntimeTimedMediaEvidenceRequest,
    outcome: CommandOutcome,
    *,
    authority_resolver: InstalledRuntimeTimedSpeechAuthorityResolver,
    limits: TimedMediaReadLimits,
) -> RuntimeTimedMediaEvidenceMetadata:
    try:
        _, metadata, _ = _inspect(
            store, request, outcome, authority_resolver=authority_resolver, limits=limits
        )
        return metadata
    except RuntimeTimedMediaReadError:
        raise
    except ValueError as error:
        raise RuntimeTimedMediaReadError("runtime timed-media metadata is invalid") from error


def read_committed_runtime_timed_media_evidence(
    store: RuntimeTimedMediaReadStore,
    request: PrepareRuntimeTimedMediaEvidenceRequest,
    outcome: CommandOutcome,
    *,
    authority_resolver: InstalledRuntimeTimedSpeechAuthorityResolver,
    limits: TimedMediaReadLimits,
) -> PersistedRuntimeTimedMediaEvidence:
    """Recompute capability admission from the Store before exposing CUDA evidence."""
    try:
        resolved, metadata, payloads = _inspect(
            store, request, outcome, authority_resolver=authority_resolver, limits=limits
        )
        record, refs, projection = metadata.record, metadata.blob_refs, metadata.projection
        root_data = cast(dict[str, object], payloads[0])
        index = cast(dict[str, object], payloads[1])
        root = decode_root_media_evidence_bundle(_read_blob(store, request.job, refs[0], limits))
        policy = _read_blob(store, request.job, refs[1], limits)
        provenance = _read_blob(store, request.job, refs[2], limits)
        evidence = ProducedTimedMediaEvidence(
            resolved.request.producer_policy_sha256,
            root,
            tuple(decode_calibration_binding(item) for item in _array(root_data["calibration_bindings"])),
            _json(policy),
            _json(provenance),
            producer_provenance_schema=RUNTIME_CUDA_MEDIA_PRODUCER_PROVENANCE_SCHEMA,
        )
        runtime_authority = cast(dict[str, object], policy)
        produced = ProducedRuntimeTimedMediaEvidence(evidence, projection, runtime_authority)
        validate_produced_timed_media_evidence(
            resolved,
            evidence,
            expected_provenance_schema=RUNTIME_CUDA_MEDIA_PRODUCER_PROVENANCE_SCHEMA,
        )
        _validate_runtime_output(
            resolved,
            produced,
            projection,
            static_policy_sha256=authority_resolver.static_operation_policy_sha256,
        )
        admission = _runtime_admission(projection, evidence)
        if canonical_sha256(payloads[2]) != admission.canonical_hash:
            raise RuntimeTimedMediaReadError("persisted runtime capability admission differs from replay")
        plan_value = _object(_read_blob(store, request.job, refs[3], limits), ("plans", "schema_version"))
        if plan_value["schema_version"] != "candidate-evidence-window-plans-v1":
            raise RuntimeTimedMediaReadError("unsupported runtime candidate window-plan schema")
        plans = tuple(decode_candidate_evidence_window_plan(item) for item in _array(plan_value["plans"]))
        candidates = tuple(
            decode_candidate_timed_evidence_set(_read_blob(store, request.job, ref, limits))
            for ref in refs[4:]
        )
        expected_plans, expected_candidates = close_timed_media_candidates(resolved, evidence)
        if plans != expected_plans or candidates != expected_candidates:
            raise RuntimeTimedMediaReadError("runtime candidates or plans differ from replay")
        audio = next(
            item for item in evidence.calibration_bindings
            if item.producer_id == evidence.root_bundle.audio_sample_boundaries.context.producer_id
        )
        probe, certificate = derive_presentation_timeline_facts(
            evidence.root_bundle,
            probe=resolved.presentation_timeline_probe,
            source_manifest_sha256=resolved.request.source_manifest_sha256,
            audio_snap_calibration=audio,
        )
        if (
            canonical_sha256(payloads[3]) != probe.canonical_hash
            or decode_committed_video_to_audio_clock_map_certificate(payloads[4]) != certificate
        ):
            raise RuntimeTimedMediaReadError("runtime presentation proof differs from replay")
        expected_root = {
            **root_data,
            "episode_index": resolved.episode_index,
            "producer_provenance_sha256": evidence.producer_provenance_sha256,
            "producer_policy_sha256": evidence.producer_policy_sha256,
            "root_bundle_sha256": evidence.root_bundle.canonical_hash,
            "source_manifest_sha256": resolved.request.source_manifest_sha256,
            "source_provenance_sha256": resolved.request.source_provenance_sha256,
            "video_to_audio_presentation_map_sha256": certificate.canonical_hash,
        }
        expected_index = {
            **index,
            "candidate_count": len(candidates),
            "candidate_index_state": "populated" if candidates else "empty",
            "candidate_set_sha256": [item.canonical_hash for item in candidates],
            "episode_index": resolved.episode_index,
            "plan_set_sha256": canonical_sha256(plan_value),
            "schema_version": "candidate-timed-evidence-index-v1",
            "semantic_pack_sha256": resolved.request.semantic_pack.canonical_hash,
            "presentation_map_facts_sha256": probe.canonical_hash,
            "presentation_timeline_probe_sha256": probe.canonical_hash,
            "video_to_audio_presentation_map_sha256": certificate.canonical_hash,
        }
        if (
            canonical_sha256(root_data) != canonical_sha256(expected_root)
            or canonical_sha256(index) != canonical_sha256(expected_index)
        ):
            raise RuntimeTimedMediaReadError("runtime root or candidate index differs from replay")
        return PersistedRuntimeTimedMediaEvidence(
            record, resolved, produced, projection, admission, plans, candidates, certificate
        )
    except RuntimeTimedMediaReadError:
        raise
    except (ValueError, StopIteration) as error:
        raise RuntimeTimedMediaReadError("committed runtime timed-media evidence is invalid") from error


__all__ = (
    "PersistedRuntimeTimedMediaEvidence",
    "RuntimeTimedMediaEvidenceMetadata",
    "RuntimeTimedMediaReadError",
    "RuntimeTimedMediaReadStore",
    "inspect_committed_runtime_timed_media_evidence",
    "read_committed_runtime_timed_media_evidence",
)
