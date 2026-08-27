"""CUDA-only durable timed-media evidence command.

This command intentionally runs beside, rather than inside,
``PrepareTimedMediaEvidence@2.1.3``.  Both commands reuse the deterministic
Source/VLM reread and physical/candidate calculations, but only this command
can persist evidence backed by a resolved ``pc_cuda`` capability.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Literal, Protocol, cast
from urllib.parse import urlparse

from ..media import (
    CandidateEvidenceWindowPlan,
    CandidateTimedEvidenceSet,
    Stage4PredecessorError,
    derive_presentation_timeline_facts,
)
from ..media.root_evidence import CanonicalEvidence
from ..media.runtime_measurement_identity import (
    PC_CUDA_RUNTIME_CAPABILITY_ID,
    RuntimeMeasurementIdentity,
)
from ..media.types import canonical_sha256
from ..registry.installed_runtime import (
    InstalledRuntimeCapabilityStore,
    InstalledRuntimeTimedSpeechAuthorityResolver,
)
from ..registry.runtime_timed_speech import (
    RuntimeTimedSpeechCapabilityAdmission,
    RuntimeTimedSpeechProjection,
    RuntimeTimedSpeechProjectionError,
)
from ..store import (
    ArtifactMember,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
    StoreValidationError,
)
from ..store.models import (
    MaterializationError,
    MaterializationLimits,
    VerifiedMaterializedBlob,
    artifact_set_hash,
)
from .prepare_timed_media_evidence_command import (
    RUNTIME_CUDA_MEDIA_PRODUCER_PROVENANCE_SCHEMA,
    TIMED_SPEECH_BUSY_RETRY_COUNT,
    TIMED_SPEECH_BUSY_RETRY_DELAY_SECONDS,
    CommittedMediaInputsStore,
    PrepareTimedMediaEvidenceRequest,
    ProducedTimedMediaEvidence,
    ResolvedPrepareTimedMediaEvidenceRequest,
    TimedMediaEvidenceCommandError,
    TimedMediaEvidenceProducerError,
    close_timed_media_candidates,
    resolve_committed_timed_media_request,
    validate_produced_timed_media_evidence,
)

PREPARE_RUNTIME_TIMED_MEDIA_EVIDENCE_COMMAND = "PrepareRuntimeTimedMediaEvidence@1.0.0"
RUNTIME_TIMED_MEDIA_EVIDENCE_STRATEGY_VERSION = "whole-episode-pc-cuda-evidence-v1"


class RuntimeTimedMediaEvidenceCommandError(ValueError):
    """The CUDA-only command request, evidence, or committed output is invalid."""


class RuntimeTimedMediaEvidenceStore(
    CommittedMediaInputsStore, InstalledRuntimeCapabilityStore, Protocol
):
    def read_outcome(self, job: Job, idempotency_key: str) -> CommandOutcome | None: ...

    def claim_command(self, claim: CommandClaim) -> CommandOutcome: ...

    def materialize_immutable_blob(
        self, job: Job, reference: BlobRef, limits: MaterializationLimits
    ) -> VerifiedMaterializedBlob: ...

    def put_immutable_blob(
        self, job: Job, *, content: bytes, content_hash: str, media_type: str
    ) -> BlobRef: ...

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome: ...

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome: ...


@dataclass(frozen=True, slots=True)
class PrepareRuntimeTimedMediaEvidenceRequest:
    """One authenticated live CUDA identity plus committed media inputs.

    The accepted capability and derived projection are intentionally *not*
    caller fields.  ``execute`` re-reads them from the Store using this
    measured identity immediately before it claims native work.
    """

    timed_media_request: PrepareTimedMediaEvidenceRequest
    runtime_measurement_identity: RuntimeMeasurementIdentity

    def __post_init__(self) -> None:
        request = self.timed_media_request
        if type(request) is not PrepareTimedMediaEvidenceRequest:  # noqa: E721
            raise RuntimeTimedMediaEvidenceCommandError("requires an exact timed-media request")
        measurement = self.runtime_measurement_identity
        if type(measurement) is not RuntimeMeasurementIdentity:  # noqa: E721
            raise RuntimeTimedMediaEvidenceCommandError(
                "requires an exact live runtime measurement"
            )
        if (
            measurement.runtime_capability_id != PC_CUDA_RUNTIME_CAPABILITY_ID
            or measurement.timing_compatibility.device.device_class != "cuda"
        ):
            raise RuntimeTimedMediaEvidenceCommandError(
                "requires the pc_cuda live CUDA measurement"
            )
        if not request.idempotency_key.startswith("runtime-media-preflight:"):
            raise RuntimeTimedMediaEvidenceCommandError(
                "runtime timed-media idempotency keys require the CUDA-only prefix"
            )

    @property
    def job(self):
        return self.timed_media_request.job

    @property
    def idempotency_key(self) -> str:
        return self.timed_media_request.idempotency_key

    def canonical_payload(self) -> dict[str, object]:
        return {
            "command": PREPARE_RUNTIME_TIMED_MEDIA_EVIDENCE_COMMAND,
            "runtime_measurement_identity": self.runtime_measurement_identity.to_mapping(),
            "runtime_measurement_identity_sha256": (
                self.runtime_measurement_identity.canonical_sha256
            ),
            "strategy_version": RUNTIME_TIMED_MEDIA_EVIDENCE_STRATEGY_VERSION,
            "timed_media_request": self.timed_media_request.canonical_payload(),
        }

    @property
    def request_hash(self) -> str:
        return canonical_sha256(self.canonical_payload())

    def request_hash_for(self, projection: RuntimeTimedSpeechProjection) -> str:
        """Bind the fresh Store-derived capability into the claimed command.

        The live measurement is caller evidence; the accepted record is not.
        Its projection therefore enters the idempotency hash only after the
        command re-reads it from the Store, before native dispatch is claimed.
        """
        if type(projection) is not RuntimeTimedSpeechProjection:  # noqa: E721
            raise RuntimeTimedMediaEvidenceCommandError(
                "request hash requires exact CUDA projection"
            )
        return canonical_sha256(
            {
                **self.canonical_payload(),
                "runtime_timed_speech_projection": projection.to_mapping(),
                "runtime_timed_speech_projection_sha256": projection.canonical_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class PrepareRuntimeTimedMediaEvidenceResult:
    outcome: CommandOutcome
    root_bundle_sha256: str | None = None
    candidate_count: int = 0


@dataclass(frozen=True, slots=True)
class ProducedRuntimeTimedMediaEvidence:
    """CUDA producer output tied to the exact command-resolved projection."""

    evidence: ProducedTimedMediaEvidence
    runtime_projection: RuntimeTimedSpeechProjection
    runtime_authority_mapping: dict[str, object]

    def __post_init__(self) -> None:
        if type(self.evidence) is not ProducedTimedMediaEvidence:  # noqa: E721
            raise RuntimeTimedMediaEvidenceCommandError("runtime producer requires exact evidence")
        if type(self.runtime_projection) is not RuntimeTimedSpeechProjection:  # noqa: E721
            raise RuntimeTimedMediaEvidenceCommandError(
                "runtime producer requires exact projection"
            )
        if type(self.runtime_authority_mapping) is not dict:  # noqa: E721
            raise RuntimeTimedMediaEvidenceCommandError(
                "runtime producer requires a canonical CUDA authority mapping"
            )


class RuntimeTimedMediaEvidenceProducerPort(Protocol):
    """CUDA-only whole-source producer invoked after capability re-read/claim."""

    def prepare(
        self,
        request: ResolvedPrepareTimedMediaEvidenceRequest,
        source: VerifiedMaterializedBlob,
        projection: RuntimeTimedSpeechProjection,
    ) -> ProducedRuntimeTimedMediaEvidence: ...


class PrepareRuntimeTimedMediaEvidenceCommand:
    """Claim, produce and commit evidence using only PC-CUDA authority."""

    def __init__(
        self,
        store: RuntimeTimedMediaEvidenceStore,
        producer: RuntimeTimedMediaEvidenceProducerPort,
        authority_resolver: InstalledRuntimeTimedSpeechAuthorityResolver,
    ) -> None:
        self._store = store
        self._producer = producer
        if type(authority_resolver) is not InstalledRuntimeTimedSpeechAuthorityResolver:  # noqa: E721
            raise RuntimeTimedMediaEvidenceCommandError(
                "runtime command requires the installed CUDA authority resolver"
            )
        self._authority_resolver = authority_resolver

    def execute(
        self, request: PrepareRuntimeTimedMediaEvidenceRequest
    ) -> PrepareRuntimeTimedMediaEvidenceResult:
        if type(request) is not PrepareRuntimeTimedMediaEvidenceRequest:  # noqa: E721
            raise RuntimeTimedMediaEvidenceCommandError(
                "requires an exact runtime timed-media request"
            )
        try:
            resolved = resolve_committed_timed_media_request(
                self._store, request.timed_media_request
            )
            projection = self._authority_resolver.resolve(
                self._store, request.runtime_measurement_identity
            )
            _validate_projection_against_request(projection, resolved.request)
        except (TimedMediaEvidenceCommandError, StoreValidationError, ValueError) as error:
            raise RuntimeTimedMediaEvidenceCommandError(
                "committed Source/VLM inputs are unavailable for runtime timed media"
            ) from error
        claimed = self._store.claim_command(
            CommandClaim(
                request.job,
                request.idempotency_key,
                PREPARE_RUNTIME_TIMED_MEDIA_EVIDENCE_COMMAND,
                request.request_hash_for(projection),
                execution_kind="deterministic",
            )
        )
        if not claimed.is_fresh_claim:
            return PrepareRuntimeTimedMediaEvidenceResult(claimed)
        source: VerifiedMaterializedBlob | None = None
        try:
            base = resolved.request
            if (
                base.source_blob.byte_length
                > base.materialization_limits.effective_max_source_bytes
            ):
                raise TimedMediaEvidenceProducerError(
                    "MEDIA_SOURCE_BYTE_LIMIT_EXCEEDED",
                    "committed source exceeds the frozen effective source-byte limit",
                )
            source = self._store.materialize_immutable_blob(
                request.job, base.source_blob, base.materialization_limits
            )
            attempts = 0
            while True:
                try:
                    produced = self._producer.prepare(resolved, source, projection)
                    break
                except TimedMediaEvidenceProducerError as error:
                    if (
                        error.code != "TIMED_SPEECH_BUSY"
                        or attempts >= TIMED_SPEECH_BUSY_RETRY_COUNT
                    ):
                        raise
                    attempts += 1
                    time.sleep(TIMED_SPEECH_BUSY_RETRY_DELAY_SECONDS)
            _validate_runtime_produced_evidence(
                resolved,
                produced,
                projection,
                expected_static_policy_sha256=(
                    self._authority_resolver.static_operation_policy_sha256
                ),
            )
            admission = _runtime_admission(projection, produced.evidence)
            plans, candidates = close_timed_media_candidates(resolved, produced.evidence)
            artifacts = self._persist_artifacts(
                resolved, produced.evidence, plans, candidates, admission
            )
            source.close()
            source = None
        except TimedMediaEvidenceProducerError as error:
            return PrepareRuntimeTimedMediaEvidenceResult(
                self._reject(claimed, error.code, error.detail, outcome=error.outcome)
            )
        except MaterializationError as error:
            return PrepareRuntimeTimedMediaEvidenceResult(
                self._reject(claimed, error.code, error.detail, outcome=error.outcome)
            )
        except (
            RuntimeTimedMediaEvidenceCommandError,
            RuntimeTimedSpeechProjectionError,
            TimedMediaEvidenceCommandError,
            ValueError,
        ) as error:
            return PrepareRuntimeTimedMediaEvidenceResult(
                self._reject(claimed, "RUNTIME_TIMED_MEDIA_EVIDENCE_INVALID", str(error))
            )
        except Exception:
            return PrepareRuntimeTimedMediaEvidenceResult(
                self._reject(
                    claimed,
                    "RUNTIME_TIMED_MEDIA_EVIDENCE_INFRASTRUCTURE_FAILED",
                    "runtime timed-media infrastructure failed",
                    outcome="failed",
                )
            )
        finally:
            if source is not None:
                source.close()
        # A lost acknowledgement here is indeterminate: do not overwrite a
        # possibly committed success with a rejection. The caller/reconciler
        # must re-read the immutable command slot instead.
        outcome = self._store.commit_command_success(
            CommandSuccess(claimed.command_slot_id, artifact_set_hash(artifacts), artifacts)
        )
        return PrepareRuntimeTimedMediaEvidenceResult(
            outcome, produced.evidence.root_bundle.canonical_hash, len(candidates)
        )

    def _persist_artifacts(
        self,
        request: ResolvedPrepareTimedMediaEvidenceRequest,
        produced: ProducedTimedMediaEvidence,
        plans: tuple[CandidateEvidenceWindowPlan, ...],
        candidates: tuple[CandidateTimedEvidenceSet, ...],
        admission: RuntimeTimedSpeechCapabilityAdmission,
    ) -> tuple[ArtifactMember, ...]:
        root_blob = self._put_evidence_blob(
            request, produced.root_bundle, "application/vnd.autocut.root-media-evidence+json"
        )
        plan_payload = {
            "plans": [item.to_mapping() for item in plans],
            "schema_version": "candidate-evidence-window-plans-v1",
        }
        plan_blob = self._put_mapping_blob(
            request, plan_payload, "application/vnd.autocut.candidate-window-plans+json"
        )
        candidate_blobs = tuple(
            self._put_evidence_blob(
                request, item, "application/vnd.autocut.candidate-timed-evidence+json"
            )
            for item in candidates
        )
        provenance_blob = self._put_mapping_blob(
            request,
            json.loads(produced.producer_provenance_json),
            "application/vnd.autocut.local-media-producer-provenance+json",
        )
        policy_blob = self._put_mapping_blob(
            request,
            json.loads(produced.producer_policy_json),
            "application/vnd.autocut.local-media-preflight-policy+json",
        )
        try:
            audio_binding = next(
                item
                for item in produced.calibration_bindings
                if item.producer_id
                == produced.root_bundle.audio_sample_boundaries.context.producer_id
            )
            probe, certificate = derive_presentation_timeline_facts(
                produced.root_bundle,
                probe=request.presentation_timeline_probe,
                source_manifest_sha256=request.source_manifest_sha256,
                audio_snap_calibration=audio_binding,
            )
        except (Stage4PredecessorError, StopIteration) as error:
            raise RuntimeTimedMediaEvidenceCommandError(
                "Stage 4 predecessor facts do not close against runtime evidence"
            ) from error
        root_payload = {
            "blob": _blob_mapping(root_blob),
            "calibration_bindings": [item.to_mapping() for item in produced.calibration_bindings],
            "episode_index": request.episode_index,
            "producer_provenance_blob": _blob_mapping(provenance_blob),
            "producer_provenance_sha256": produced.producer_provenance_sha256,
            "producer_policy_blob": _blob_mapping(policy_blob),
            "producer_policy_sha256": produced.producer_policy_sha256,
            "root_bundle_sha256": produced.root_bundle.canonical_hash,
            "source_manifest_sha256": request.source_manifest_sha256,
            "source_provenance_sha256": request.source_provenance_sha256,
            "video_to_audio_presentation_map_sha256": certificate.canonical_hash,
        }
        index_payload = {
            "candidate_blobs": [_blob_mapping(item) for item in candidate_blobs],
            "candidate_count": len(candidate_blobs),
            "candidate_index_state": "populated" if candidate_blobs else "empty",
            "candidate_set_sha256": [item.canonical_hash for item in candidates],
            "episode_index": request.episode_index,
            "plan_blob": _blob_mapping(plan_blob),
            "plan_set_sha256": canonical_sha256(plan_payload),
            "schema_version": "candidate-timed-evidence-index-v1",
            "semantic_pack_sha256": request.semantic_pack.canonical_hash,
            "presentation_map_facts_sha256": request.presentation_timeline_probe.canonical_hash,
            "presentation_timeline_probe_sha256": request.presentation_timeline_probe.canonical_hash,
            "video_to_audio_presentation_map_sha256": certificate.canonical_hash,
        }
        return (
            _artifact(request, "root_media_evidence_bundle", "root_media_evidence", root_payload),
            _artifact(
                request, "candidate_timed_evidence_index", "candidate_timed_evidence", index_payload
            ),
            _artifact(
                request,
                "runtime_timed_speech_capability_admission",
                "runtime_timed_speech_capability_admission",
                admission.to_mapping(),
            ),
            _artifact(
                request,
                "presentation_timeline_probe",
                "presentation_timeline_probe",
                probe.to_mapping(),
            ),
            _artifact(
                request,
                "committed_video_to_audio_clock_map_certificate",
                "video_to_audio_clock_map",
                certificate.to_mapping(),
            ),
        )

    def _put_evidence_blob(
        self,
        request: ResolvedPrepareTimedMediaEvidenceRequest,
        evidence: CanonicalEvidence,
        media_type: str,
    ) -> BlobRef:
        return self._put_mapping_blob(request, evidence.to_mapping(), media_type)

    def _put_mapping_blob(
        self, request: ResolvedPrepareTimedMediaEvidenceRequest, value: object, media_type: str
    ) -> BlobRef:
        content = _json(value).encode("utf-8")
        return self._store.put_immutable_blob(
            request.job,
            content=content,
            content_hash="sha256:" + hashlib.sha256(content).hexdigest(),
            media_type=media_type,
        )

    def _reject(
        self,
        claimed: CommandOutcome,
        code: str,
        detail: str,
        *,
        outcome: Literal["denied", "failed"] = "denied",
    ) -> CommandOutcome:
        return self._store.commit_command_rejection(
            CommandRejection(
                claimed.command_slot_id,
                code,
                _json({"code": code, "detail": detail}),
                outcome=outcome,
            )
        )


def _validate_projection_against_request(
    projection: RuntimeTimedSpeechProjection,
    request: PrepareTimedMediaEvidenceRequest,
) -> None:
    """Close the Store-derived CUDA authority to this exact source request."""
    audio = request.audio_sample_boundaries.context
    if (
        projection.runtime_capability_id != PC_CUDA_RUNTIME_CAPABILITY_ID
        or projection.device_class != "cuda"
        or projection.source_clock_id != audio.clock_id
        or projection.source_time_base != audio.time_base
    ):
        raise RuntimeTimedMediaEvidenceCommandError(
            "resolved CUDA authority does not close the committed source audio clock"
        )


def _validate_runtime_produced_evidence(
    request: ResolvedPrepareTimedMediaEvidenceRequest,
    produced: ProducedRuntimeTimedMediaEvidence,
    projection: RuntimeTimedSpeechProjection,
    *,
    expected_static_policy_sha256: str,
) -> None:
    """Require the producer to echo the command-resolved CUDA authority.

    The unchanged generic validator closes Source/VLM/physical evidence and the
    runtime admission below closes the ASR/VAD bindings.  This extra comparison
    prevents an adapter from accepting a projection for one request then
    returning output it attributes to another one.
    """
    if produced.runtime_projection != projection:
        raise RuntimeTimedMediaEvidenceCommandError(
            "runtime producer evidence differs from the command-resolved CUDA authority"
        )
    validate_produced_timed_media_evidence(
        request,
        produced.evidence,
        expected_provenance_schema=RUNTIME_CUDA_MEDIA_PRODUCER_PROVENANCE_SCHEMA,
    )
    if (
        produced.evidence.producer_policy_sha256 != request.request.producer_policy_sha256
        or produced.evidence.producer_policy_sha256
        != canonical_sha256(produced.runtime_authority_mapping)
    ):
        raise RuntimeTimedMediaEvidenceCommandError(
            "runtime producer policy differs from the request-bound CUDA policy"
        )
    try:
        provenance = json.loads(produced.evidence.producer_provenance_json)
    except (TypeError, ValueError) as error:
        raise RuntimeTimedMediaEvidenceCommandError(
            "runtime producer provenance is unavailable"
        ) from error
    if type(provenance) is not dict:
        raise RuntimeTimedMediaEvidenceCommandError(
            "runtime producer provenance does not bind its CUDA authority"
        )
    provenance_mapping = cast(dict[str, object], provenance)
    if (
        provenance_mapping.get("schema_version") != RUNTIME_CUDA_MEDIA_PRODUCER_PROVENANCE_SCHEMA
        or provenance_mapping.get("runtime_timed_speech_authority")
        != produced.runtime_authority_mapping
    ):
        raise RuntimeTimedMediaEvidenceCommandError(
            "runtime producer provenance does not bind its CUDA authority"
        )
    _validate_runtime_authority_mapping(
        produced.runtime_authority_mapping,
        projection,
        expected_static_policy_sha256=expected_static_policy_sha256,
    )


def _runtime_admission(
    projection: RuntimeTimedSpeechProjection, produced: ProducedTimedMediaEvidence
) -> RuntimeTimedSpeechCapabilityAdmission:
    bindings = {item.producer_id: item for item in produced.calibration_bindings}
    try:
        pair = (
            bindings[projection.producers[0].producer_id],
            bindings[projection.producers[1].producer_id],
        )
    except KeyError as error:
        raise RuntimeTimedMediaEvidenceCommandError(
            "produced evidence lost the selected runtime ASR/VAD calibration"
        ) from error
    return RuntimeTimedSpeechCapabilityAdmission(projection, produced.root_bundle, pair)


def _validate_runtime_authority_mapping(
    authority: dict[str, object],
    projection: RuntimeTimedSpeechProjection,
    *,
    expected_static_policy_sha256: str,
) -> None:
    """Check the closed v2 app policy against the Store-derived projection.

    The application owns operational HTTP limits; the Kernel owns the
    capability acceptance.  The wire form must nevertheless be exact: a
    producer cannot omit a static-policy identity, smuggle a legacy endpoint,
    or add an unreviewed policy field under a valid projection hash.
    """
    expected_keys = {
        "schema_version",
        "static_policy_sha256",
        "runtime_capability_id",
        "device",
        "runtime_measurement_identity_sha256",
        "timing_compatibility_sha256",
        "runtime_projection_compatibility_sha256",
        "runtime",
        "profile_source_sha256",
        "registry_snapshot_sha256",
        "accepted_record_sha256",
        "validation_receipt_sha256",
        "native_port_identity_sha256",
        "source_clock",
        "timing",
        "operation",
        "producers",
        "build_audit_sha256",
        "runtime_projection_sha256",
    }
    if set(authority) != expected_keys:
        raise RuntimeTimedMediaEvidenceCommandError(
            "runtime producer authority schema is not closed"
        )
    if authority.get("schema_version") != "pc-cuda-runtime-timed-speech-policy-v1":
        raise RuntimeTimedMediaEvidenceCommandError("runtime producer authority version is invalid")
    timing = authority.get("timing")
    source_clock = authority.get("source_clock")
    producers = authority.get("producers")
    runtime = authority.get("runtime")
    operation = authority.get("operation")
    if (
        type(timing) is not dict
        or type(source_clock) is not dict
        or type(producers) is not list
        or type(runtime) is not dict
        or type(operation) is not dict
    ):
        raise RuntimeTimedMediaEvidenceCommandError("runtime producer authority schema is invalid")
    timing = cast(dict[str, object], timing)
    source_clock = cast(dict[str, object], source_clock)
    producers = cast(list[object], producers)
    runtime = cast(dict[str, object], runtime)
    operation = cast(dict[str, object], operation)
    if runtime != {
        "funasr_version": projection.funasr_version,
        "torch_version": projection.torch_version,
    }:
        raise RuntimeTimedMediaEvidenceCommandError(
            "runtime producer library versions differ from the accepted projection"
        )
    if source_clock != {
        "clock_id": projection.source_clock_id,
        "time_base": {
            "numerator": projection.source_time_base.numerator,
            "denominator": projection.source_time_base.denominator,
        },
    }:
        raise RuntimeTimedMediaEvidenceCommandError(
            "runtime producer source clock differs from the accepted projection"
        )
    if (
        authority.get("runtime_capability_id") != PC_CUDA_RUNTIME_CAPABILITY_ID
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
        raise RuntimeTimedMediaEvidenceCommandError(
            "runtime producer authority differs from the Store-derived CUDA projection"
        )
    expected_timing_hashes = {
        "timed_speech_policy_sha256": projection.timed_speech_policy_sha256,
        "word_gap_policy_sha256": projection.word_gap_policy_sha256,
        "vad_merge_policy_sha256": projection.vad_merge_policy_sha256,
        "alignment_policy_sha256": projection.alignment_policy_sha256,
        "acceptance_policy_sha256": projection.acceptance_policy_sha256,
    }
    if set(timing) != {
        *expected_timing_hashes,
        "utterance_gap_milliseconds",
        "vad_merge_gap_milliseconds",
    } or any(timing.get(name) != value for name, value in expected_timing_hashes.items()):
        raise RuntimeTimedMediaEvidenceCommandError(
            "runtime producer timing authority differs from the accepted projection"
        )
    if (
        type(timing["utterance_gap_milliseconds"]) is not int
        or type(timing["vad_merge_gap_milliseconds"]) is not int
        or timing["utterance_gap_milliseconds"] < 0
        or timing["vad_merge_gap_milliseconds"] < 0
    ):
        raise RuntimeTimedMediaEvidenceCommandError("runtime producer timing gaps are invalid")
    if authority.get("static_policy_sha256") != expected_static_policy_sha256:
        raise RuntimeTimedMediaEvidenceCommandError(
            "runtime producer static policy differs from the installed authority"
        )
    _validate_runtime_operation(operation)
    expected_producers = [
        {
            **item.to_mapping(),
            "calibration_record_sha256": record_sha256,
            "timing_error_bound_tick": bound_tick,
        }
        for item, record_sha256, bound_tick in zip(
            projection.producers,
            (
                projection.asr_calibration_record_sha256,
                projection.vad_calibration_record_sha256,
            ),
            (
                projection.asr_timing_error_bound_tick,
                projection.vad_timing_error_bound_tick,
            ),
            strict=True,
        )
    ]
    if producers != expected_producers:
        raise RuntimeTimedMediaEvidenceCommandError(
            "runtime producer identities differ from the accepted projection"
        )


def _validate_runtime_operation(operation: dict[str, object]) -> None:
    if set(operation) != {
        "endpoint_url",
        "provider_id",
        "provider_version",
        "timeout_seconds",
        "max_response_bytes",
    }:
        raise RuntimeTimedMediaEvidenceCommandError(
            "runtime producer operation schema is not closed"
        )
    endpoint = operation["endpoint_url"]
    if type(endpoint) is not str:  # noqa: E721
        raise RuntimeTimedMediaEvidenceCommandError("runtime producer endpoint is invalid")
    try:
        parsed = urlparse(endpoint)
        port = parsed.port
    except ValueError as error:
        raise RuntimeTimedMediaEvidenceCommandError(
            "runtime producer endpoint port is invalid"
        ) from error
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1 <= port <= 65_535
        or parsed.path != "/v2/runtime-timed-speech-evidence"
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeTimedMediaEvidenceCommandError(
            "runtime producer operation cannot use a legacy or non-loopback endpoint"
        )
    for key in ("provider_id", "provider_version"):
        value = operation[key]
        if type(value) is not str or not value or value != value.strip():  # noqa: E721
            raise RuntimeTimedMediaEvidenceCommandError(
                "runtime producer provider identity is invalid"
            )
    for key in ("timeout_seconds", "max_response_bytes"):
        value = operation[key]
        if type(value) is not int or value <= 0:  # noqa: E721
            raise RuntimeTimedMediaEvidenceCommandError(
                "runtime producer operation limits are invalid"
            )
def _artifact(
    request: ResolvedPrepareTimedMediaEvidenceRequest,
    artifact_type: str,
    logical_prefix: str,
    payload: object,
) -> ArtifactMember:
    payload_json = _json(payload)
    return ArtifactMember(
        artifact_type,
        f"{logical_prefix}_episode_{request.episode_index:04d}",
        request.artifact_revision,
        request.artifact_scope,
        canonical_sha256(payload),
        payload_json,
    )


def _blob_mapping(reference: BlobRef) -> dict[str, object]:
    return {
        "byte_length": reference.byte_length,
        "content_hash": reference.content_hash,
        "media_type": reference.media_type,
        "object_id": str(reference.object_id),
    }


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


__all__ = [
    "PREPARE_RUNTIME_TIMED_MEDIA_EVIDENCE_COMMAND",
    "RUNTIME_TIMED_MEDIA_EVIDENCE_STRATEGY_VERSION",
    "PrepareRuntimeTimedMediaEvidenceCommand",
    "PrepareRuntimeTimedMediaEvidenceRequest",
    "PrepareRuntimeTimedMediaEvidenceResult",
    "ProducedRuntimeTimedMediaEvidence",
    "RuntimeTimedMediaEvidenceCommandError",
    "RuntimeTimedMediaEvidenceProducerPort",
    "RuntimeTimedMediaEvidenceStore",
]
