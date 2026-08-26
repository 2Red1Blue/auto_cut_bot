"""Exact, bounded replay of the five-member timed-media predecessor.

The Store establishes immutable commitment. This reader independently replays
the producer's derived facts against committed Source/VLM and the installed
accepted speech profile. It grants neither physical edit nor publication
admission. Constructing the returned value directly establishes no authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID

from ..media.presentation_evidence_codec import (
    decode_committed_video_to_audio_clock_map_certificate,
    decode_timed_speech_profile_admission,
)
from ..media.root_evidence_codec import (
    decode_media_evidence_json,
    decode_root_media_evidence_bundle,
)
from ..media.stage4_predecessor import (
    CommittedVideoToAudioClockMapCertificate,
    TimedSpeechProfileAdmission,
    admit_timed_speech_profile,
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
    InstalledLocalRunAuthorityStore,
    InstalledLocalRunProfileResolver,
)
from ..registry.timed_speech import BootstrappedTimedSpeechProfile
from ..store.models import (
    BlobRef,
    CommandOutcome,
    Job,
    MaterializationLimits,
    PersistedCommittedArtifactSet,
)
from .prepare_timed_media_evidence_command import (
    PREPARE_TIMED_MEDIA_EVIDENCE_COMMAND,
    PrepareTimedMediaEvidenceRequest,
    ProducedTimedMediaEvidence,
    ResolvedPrepareTimedMediaEvidenceRequest,
    TimedMediaEvidenceStore,
    close_timed_media_candidates,
    resolve_committed_timed_media_request,
    timed_media_request_hash,
    validate_produced_timed_media_evidence,
)


class TimedMediaReadError(ValueError):
    """A committed predecessor cannot be fully and safely reconstructed."""


class TimedMediaReadStore(TimedMediaEvidenceStore, InstalledLocalRunAuthorityStore, Protocol):
    def read_committed_artifact_set(
        self, job: Job, *, command_slot_id: UUID, receipt_id: UUID,
        artifact_set_id: UUID, expected_request_hash: str, expected_command_name: str,
        expected_execution_kind: str,
    ) -> PersistedCommittedArtifactSet: ...


@dataclass(frozen=True, slots=True)
class TimedMediaReadLimits:
    max_blob_bytes: int
    max_total_blob_bytes: int
    max_candidates: int
    materialization: MaterializationLimits

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in (  # noqa: E721
            self.max_blob_bytes, self.max_total_blob_bytes, self.max_candidates,
        )):
            raise TimedMediaReadError("reader ceilings must be explicit positive integers")
        if type(self.materialization) is not MaterializationLimits:  # noqa: E721
            raise TimedMediaReadError("reader requires explicit materialization controls")
        if self.max_blob_bytes > self.materialization.effective_max_source_bytes:
            raise TimedMediaReadError("blob ceiling exceeds materialization limit")


@dataclass(frozen=True, slots=True)
class PersistedTimedMediaEvidence:
    record: PersistedCommittedArtifactSet
    request: ResolvedPrepareTimedMediaEvidenceRequest
    produced: ProducedTimedMediaEvidence
    plans: tuple[CandidateEvidenceWindowPlan, ...]
    candidates: tuple[CandidateTimedEvidenceSet, ...]
    profile: BootstrappedTimedSpeechProfile
    admission: TimedSpeechProfileAdmission
    certificate: CommittedVideoToAudioClockMapCertificate


def _object(value: object, fields: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict or set(cast(dict[object, object], value)) != set(fields):  # noqa: E721
        raise TimedMediaReadError("committed timed-media object has missing or unknown fields")
    return cast(dict[str, object], value)  # Exact dict and closed string keys above.


def _array(value: object) -> list[object]:
    if type(value) is not list:  # noqa: E721
        raise TimedMediaReadError("committed timed-media collection must be a JSON array")
    return cast(list[object], value)


def _text(value: object) -> str:
    if type(value) is not str or not value:  # noqa: E721
        raise TimedMediaReadError("committed timed-media identifier must be text")
    return value


def _blob(value: object, kind: str) -> BlobRef:
    raw = _object(value, ("object_id", "content_hash", "byte_length", "media_type"))
    length = raw["byte_length"]
    if type(length) is not int or length <= 0:  # noqa: E721
        raise TimedMediaReadError("evidence blob length must be positive")
    object_id = _text(raw["object_id"])
    reference = BlobRef(UUID(object_id), _text(raw["content_hash"]), length, _text(raw["media_type"]))
    if str(reference.object_id) != object_id or reference.media_type != f"application/vnd.autocut.{kind}+json":
        raise TimedMediaReadError("evidence blob identity or media type differs")
    return reference


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _read_blob(store: TimedMediaReadStore, job: Job, ref: BlobRef, limits: TimedMediaReadLimits) -> object:
    lease = store.materialize_immutable_blob(job, ref, limits.materialization)
    try:
        if lease.reference != ref:
            raise TimedMediaReadError("materialized lease differs from committed BlobRef")
        with lease.path.open("rb") as stream:
            raw = stream.read(ref.byte_length + 1)
        if len(raw) != ref.byte_length or "sha256:" + hashlib.sha256(raw).hexdigest() != ref.content_hash:
            raise TimedMediaReadError("materialized evidence length or raw hash differs")
        return decode_media_evidence_json(raw, max_bytes=limits.max_blob_bytes)
    finally:
        lease.close()


def _accepted_speech_bindings(
    produced: ProducedTimedMediaEvidence, resolver: InstalledLocalRunProfileResolver,
) -> None:
    """Membership hashes alone do not freeze producer version or accepted bound."""
    local = resolver.resource.local_run
    bindings = {item.producer_id: item for item in produced.calibration_bindings}
    provenance: object = json.loads(produced.producer_provenance_json)
    # ProducedTimedMediaEvidence already validates this closed provenance wire.
    provenance = _object(provenance, (
        "producer_identities", "schema_version", "source_provenance_sha256",
        "tool_invocations", "tool_trace_sha256",
    ))
    identities = _array(provenance["producer_identities"])
    for ordinal, producer in enumerate(local.native_timed_speech.producers, start=2):
        binding = bindings.get(producer.producer_id)
        if binding is None or (
            binding.policy_sha256 != producer.generation_policy_sha256
            or binding.detector_sha256 != producer.detector_sha256
            or binding.calibration_record_sha256 != producer.producer_record_sha256
            or binding.producer_version != producer.producer_version
            or binding.time_base != local.source_clock_policy.time_base
            or binding.timing_error_bound_tick != producer.timing_error_bound_tick
            or binding.adapter_sha256 != local.native_timed_speech.native_port_identity_sha256
            or binding.active is not True
        ):
            raise TimedMediaReadError("speech binding differs from installed accepted calibration")
        expected_identity = {
            "producer_kind": producer.producer_kind,
            "producer_id": producer.producer_id,
            "producer_version": producer.producer_version,
            "producer_policy_sha256": producer.generation_policy_sha256,
            "detector_sha256": producer.detector_sha256,
            "calibration_policy_sha256": producer.calibration_policy_sha256,
            "calibration_record_sha256": producer.producer_record_sha256,
            "timing_error_bound_tick": producer.timing_error_bound_tick,
            "adapter_sha256": local.native_timed_speech.native_port_identity_sha256,
        }
        if canonical_sha256(identities[ordinal]) != canonical_sha256(expected_identity):
            raise TimedMediaReadError("speech provenance differs from installed accepted producer")


def _record(
    store: TimedMediaReadStore, request: ResolvedPrepareTimedMediaEvidenceRequest,
    outcome: CommandOutcome, resolver: InstalledLocalRunProfileResolver,
) -> PersistedCommittedArtifactSet:
    if (
        type(outcome) is not CommandOutcome or outcome.state != "succeeded"  # noqa: E721
        or outcome.job_id is None or outcome.receipt_id is None or outcome.artifact_set_id is None
        or outcome.failure_code is not None or outcome.failure_detail_json is not None
    ):
        raise TimedMediaReadError("reader requires an exact succeeded timed-media outcome")
    request_hash = timed_media_request_hash(request, resolver.snapshot)
    record = store.read_committed_artifact_set(
        request.job, command_slot_id=outcome.command_slot_id, receipt_id=outcome.receipt_id,
        artifact_set_id=outcome.artifact_set_id, expected_request_hash=request_hash,
        expected_command_name=PREPARE_TIMED_MEDIA_EVIDENCE_COMMAND,
        expected_execution_kind="deterministic",
    )
    if type(record) is not PersistedCommittedArtifactSet or (  # noqa: E721
        record.job != request.job or record.job_id != outcome.job_id
        or record.command_slot_id != outcome.command_slot_id or record.receipt_id != outcome.receipt_id
        or record.artifact_set_id != outcome.artifact_set_id or record.request_hash != request_hash
        or record.command_name != PREPARE_TIMED_MEDIA_EVIDENCE_COMMAND
        or record.execution_kind != "deterministic"
    ):
        raise TimedMediaReadError("timed-media Store record differs from requested identity")
    expected = (
        ("root_media_evidence_bundle", "root_media_evidence"),
        ("candidate_timed_evidence_index", "candidate_timed_evidence"),
        ("timed_speech_profile_admission", "timed_speech_profile_admission"),
        ("presentation_timeline_probe", "presentation_timeline_probe"),
        ("committed_video_to_audio_clock_map_certificate", "video_to_audio_clock_map"),
    )
    if len(record.members) != len(expected):
        raise TimedMediaReadError("timed-media committed set must contain exactly five members")
    for member, (kind, prefix) in zip(record.members, expected, strict=True):
        reference = member.reference
        if (
            reference.artifact_type != kind
            or reference.logical_id != f"{prefix}_episode_{request.episode_index:04d}"
            or reference.scope != request.artifact_scope
            or reference.revision != request.artifact_revision
        ):
            raise TimedMediaReadError("timed-media member type, episode, scope or revision differs")
    return record


def read_committed_timed_media_evidence(
    store: TimedMediaReadStore, request: PrepareTimedMediaEvidenceRequest, outcome: CommandOutcome,
    *, authority_profile_resolver: InstalledLocalRunProfileResolver, limits: TimedMediaReadLimits,
) -> PersistedTimedMediaEvidence:
    """Read exact Source/VLM, accepted calibration and all five persisted members.

    Production composition must supply the fixed installed resolver and real
    Store. A fake Store or caller-built installation proves no real acceptance.
    No provider, command claim, write, head lookup or detector is invoked here.
    """
    try:
        return _read_committed_timed_media_evidence(
            store, request, outcome, authority_profile_resolver=authority_profile_resolver,
            limits=limits,
        )
    except TimedMediaReadError:
        raise
    except ValueError as error:
        # Keep domain/codec rejection distinct from I/O/Store infrastructure
        # failures. Preserve the original cause rather than retrying or repairing.
        raise TimedMediaReadError(f"committed timed-media evidence is invalid: {error}") from error


def _read_committed_timed_media_evidence(
    store: TimedMediaReadStore, request: PrepareTimedMediaEvidenceRequest, outcome: CommandOutcome,
    *, authority_profile_resolver: InstalledLocalRunProfileResolver, limits: TimedMediaReadLimits,
) -> PersistedTimedMediaEvidence:
    if type(authority_profile_resolver) is not InstalledLocalRunProfileResolver:  # noqa: E721
        raise TimedMediaReadError("reader requires the installed accepted-profile resolver")
    if type(limits) is not TimedMediaReadLimits:  # noqa: E721
        raise TimedMediaReadError("reader requires exact explicit limits")
    resolved = resolve_committed_timed_media_request(store, request)
    record = _record(store, resolved, outcome, authority_profile_resolver)
    profile = authority_profile_resolver.resolve(store)
    payloads = tuple(decode_media_evidence_json(member.payload_json.encode("utf-8"),
                                              max_bytes=limits.max_blob_bytes)
                     for member in record.members)
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
    expected_count = len(request.semantic_pack.candidate_hypotheses)
    if (len(raw_candidates) > limits.max_candidates or expected_count > limits.max_candidates
            or len(raw_candidates) != expected_count
            or type(index["candidate_count"]) is not int  # noqa: E721
            or index["candidate_count"] != expected_count):
        raise TimedMediaReadError("candidate count differs from committed VLM or reader ceiling")
    refs = (
        _blob(root_data["blob"], "root-media-evidence"),
        _blob(root_data["producer_policy_blob"], "local-media-preflight-policy"),
        _blob(root_data["producer_provenance_blob"], "local-media-producer-provenance"),
        _blob(index["plan_blob"], "candidate-window-plans"),
        *(_blob(item, "candidate-timed-evidence") for item in raw_candidates),
    )
    if (any(ref.byte_length > limits.max_blob_bytes for ref in refs)
            or sum(ref.byte_length for ref in refs) > limits.max_total_blob_bytes
            or len({ref.object_id for ref in refs}) != len(refs)):
        raise TimedMediaReadError("evidence BlobRefs exceed byte ceilings or alias each other")
    root_value = _read_blob(store, request.job, refs[0], limits)
    policy_value = _read_blob(store, request.job, refs[1], limits)
    provenance_value = _read_blob(store, request.job, refs[2], limits)
    produced = ProducedTimedMediaEvidence(
        request.producer_policy_sha256, decode_root_media_evidence_bundle(root_value),
        tuple(decode_calibration_binding(item) for item in _array(root_data["calibration_bindings"])),
        _json(policy_value), _json(provenance_value),
    )
    validate_produced_timed_media_evidence(resolved, produced)
    _accepted_speech_bindings(produced, authority_profile_resolver)
    plan_value = _object(_read_blob(store, request.job, refs[3], limits), ("plans", "schema_version"))
    if plan_value["schema_version"] != "candidate-evidence-window-plans-v1":
        raise TimedMediaReadError("unsupported candidate window-plan schema")
    raw_plans = _array(plan_value["plans"])
    if len(raw_plans) != expected_count:
        raise TimedMediaReadError("plan count differs from committed VLM")
    plans = tuple(decode_candidate_evidence_window_plan(item) for item in raw_plans)
    candidates = tuple(decode_candidate_timed_evidence_set(_read_blob(store, request.job, ref, limits))
                       for ref in refs[4:])
    expected_plans, expected_candidates = close_timed_media_candidates(resolved, produced)
    if plans != expected_plans or candidates != expected_candidates:
        raise TimedMediaReadError("persisted candidates or plans differ from committed VLM replay")
    admission = admit_timed_speech_profile(
        profile.entry, profile.reference.content_hash, produced.root_bundle, produced.calibration_bindings,
    )
    audio_binding = next(binding for binding in produced.calibration_bindings
                         if binding.producer_id == produced.root_bundle.audio_sample_boundaries.context.producer_id)
    probe, certificate = derive_presentation_timeline_facts(
        produced.root_bundle, probe=resolved.presentation_timeline_probe,
        source_manifest_sha256=request.source_manifest_sha256, audio_snap_calibration=audio_binding,
    )
    admission_value = _object(payloads[2], (*admission.to_mapping(), "registry_member_reference"))
    decoded_admission = decode_timed_speech_profile_admission({
        key: value for key, value in admission_value.items() if key != "registry_member_reference"
    })
    if (decoded_admission != admission
            or canonical_sha256(admission_value["registry_member_reference"]) != canonical_sha256(profile.reference.to_mapping())
            or canonical_sha256(payloads[3]) != probe.canonical_hash
            or decode_committed_video_to_audio_clock_map_certificate(payloads[4]) != certificate):
        raise TimedMediaReadError("persisted admission or presentation proof differs from replay")
    expected_root = {
        **root_data, "episode_index": request.episode_index,
        "producer_provenance_sha256": produced.producer_provenance_sha256,
        "producer_policy_sha256": produced.producer_policy_sha256,
        "root_bundle_sha256": produced.root_bundle.canonical_hash,
        "source_manifest_sha256": request.source_manifest_sha256,
        "source_provenance_sha256": request.source_provenance_sha256,
        "video_to_audio_presentation_map_sha256": certificate.canonical_hash,
    }
    expected_index = {
        **index, "candidate_count": len(candidates),
        "candidate_index_state": "populated" if candidates else "empty",
        "candidate_set_sha256": [item.canonical_hash for item in candidates],
        "episode_index": request.episode_index, "plan_set_sha256": canonical_sha256(plan_value),
        "schema_version": "candidate-timed-evidence-index-v1",
        "semantic_pack_sha256": request.semantic_pack.canonical_hash,
        "presentation_map_facts_sha256": probe.canonical_hash,
        "presentation_timeline_probe_sha256": probe.canonical_hash,
        "video_to_audio_presentation_map_sha256": certificate.canonical_hash,
    }
    # Canonical values distinguish true from 1; plain Python dict equality does not.
    if canonical_sha256(root_data) != canonical_sha256(expected_root) or canonical_sha256(index) != canonical_sha256(expected_index):
        raise TimedMediaReadError("root or candidate index differs from reconstructed evidence")
    return PersistedTimedMediaEvidence(record, resolved, produced, plans, candidates, profile, admission, certificate)
