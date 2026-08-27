"""Independent, provider-free validation of committed native timing measurements.

The profile is a deployment injection, not a request-selected policy. Measurement
projections are comparison targets only: every accepted bound is derived again
from immutable native bytes and profile-bound independent anchors.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from ..media.calibration import CalibrationRecordError, ProducerCalibrationMeasurement
from ..media.calibration_record import (
    CALIBRATION_VALIDATION_CHECKS,
    CALIBRATION_VALIDATOR_COMMAND,
    CALIBRATION_VALIDATOR_PRINCIPAL,
    CalibrationEvidenceMember,
    CalibrationMatchEvidence,
    CalibrationRecordArtifactSet,
    CalibrationRecordIdentity,
    CalibrationRecordMemberPayload,
    CalibrationRecordProducerIdentity,
    CalibrationRecordRole,
    IndependentlyRecomputedCalibrationResult,
    build_calibration_record_candidate,
    calibration_validation_input_hash,
    calibration_validation_result_hash,
    validator_internal_assemble_accepted_artifact_set,
)
from ..media.shadow_calibration_raw import (
    ShadowCalibrationProjection,
    ShadowCalibrationRawBlob,
    decode_shadow_calibration_invocation,
    decode_shadow_calibration_raw_context,
    derive_shadow_calibration_raw_response,
    shadow_calibration_anchor_reference_sha256,
    shadow_calibration_projection_mapping,
)
from ..registry.authority_profiles import (
    RuntimeCalibrationPolicySource,
    ShadowCalibrationProfileSource,
    Stage1NarrativeProfileSource,
    decode_shadow_calibration_profile_source,
)
from ..store import (
    ArtifactMember,
    ArtifactScope,
    BlobIntegrityError,
    BlobRef,
    CalibrationValidationBinding,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
    PersistedShadowCalibrationMeasurement,
    RuntimeStoreError,
    SemanticInputIntegrityError,
    StoreValidationError,
)
from .measure_shadow_calibration_command import (
    MeasureShadowCalibrationRequest,
    ShadowCalibrationCorpusMember,
    ShadowCalibrationInputs,
)


class CalibrationValidationError(ValueError):
    """A deterministic discrepancy prevents authority acceptance."""


@dataclass(frozen=True, slots=True)
class CalibrationValidationLimits:
    """Deployment resource ceilings; never copied from untrusted invocation data."""

    max_response_bytes: int
    max_total_response_bytes: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (
                self.max_response_bytes,
                self.max_total_response_bytes,
            )
        ):
            raise CalibrationValidationError("validator byte limits must be positive integers")
        if self.max_response_bytes > self.max_total_response_bytes:
            raise CalibrationValidationError("validator per-member limit exceeds its total limit")


class CalibrationValidationStore(Protocol):
    def claim_command(self, claim: CommandClaim) -> CommandOutcome: ...

    def read_committed_shadow_calibration_measurement(
        self,
        binding: CalibrationValidationBinding,
    ) -> PersistedShadowCalibrationMeasurement: ...

    def read_immutable_blob(self, job: Job, reference: BlobRef) -> bytes: ...

    def commit_calibration_record_validation_success(
        self,
        success: CommandSuccess,
        binding: CalibrationValidationBinding,
        record: CalibrationRecordArtifactSet,
    ) -> CommandOutcome: ...

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome: ...


def _object(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(cast(dict[str, object], value)) != fields:  # noqa: E721
        raise CalibrationValidationError(f"{label} does not match its closed schema")
    return cast(dict[str, object], value)


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list or not value:  # noqa: E721
        raise CalibrationValidationError(f"{label} must be a nonempty array")
    return cast(list[object], value)


def _equal(actual: object, expected: object, label: str) -> None:
    # Never Python mapping equality: True == 1 and 0.0 == 0.
    if canonical_json_bytes(actual) != canonical_json_bytes(expected):
        raise CalibrationValidationError(f"{label} disagrees with independent recomputation")


def _blob(value: object) -> BlobRef:
    mapping = _object(
        value, {"object_id", "content_hash", "byte_length", "media_type"}, "raw BlobRef"
    )
    if type(mapping["object_id"]) is not str:  # noqa: E721
        raise CalibrationValidationError("raw BlobRef object_id must be canonical text")
    object_id = UUID(mapping["object_id"])
    if str(object_id) != mapping["object_id"]:
        raise CalibrationValidationError("raw BlobRef object_id is not canonical")
    return BlobRef(
        object_id,
        cast(str, mapping["content_hash"]),
        cast(int, mapping["byte_length"]),
        cast(str, mapping["media_type"]),
    )


def _aggregate(
    projections: tuple[ShadowCalibrationProjection, ...],
    references: list[str],
    role: str,
) -> dict[str, object]:
    measurements = tuple(
        item.summary.asr if role == "asr" else item.summary.vad for item in projections
    )
    first = measurements[0]
    count = sum(len(item.matches) for item in measurements)
    return {
        "aggregation": "member-bound-calibration-statistics-v1",
        "absolute_maximum_tick": max(item.absolute_maximum_tick for item in measurements),
        "clock_id": first.clock_id,
        "corpus_member_count": len(projections),
        "corpus_member_references": references,
        "early_maximum_tick": max(item.early_maximum_tick for item in measurements),
        "eligible_anchor_count": count,
        "inference_kind": first.inference_kind,
        "invalid_or_indeterminate_member_count": 0,
        "late_maximum_tick": max(item.late_maximum_tick for item in measurements),
        "matched_anchor_count": count,
        "producer": role,
        "producer_id": first.producer_id,
        "time_base": {
            "numerator": first.time_base.numerator,
            "denominator": first.time_base.denominator,
        },
    }


def _matches(
    reference: str, measurement: ProducerCalibrationMeasurement
) -> tuple[CalibrationMatchEvidence, ...]:
    # Native observation IDs are local to a source. Qualify both sides with the
    # fixed-length corpus hash so unrelated sources cannot collide or alias.
    return tuple(
        CalibrationMatchEvidence.from_ranges(
            f"{reference}/{item.anchor.anchor_id}",
            f"{reference}/{item.observation.observation_id}",
            item.anchor.expected_range,
            item.observation.observed_range,
        )
        for item in measurement.matches
    )


def _success(slot: UUID, record: CalibrationRecordArtifactSet) -> CommandSuccess:
    artifacts = tuple(
        ArtifactMember(
            item.artifact_type,
            item.logical_id,
            item.revision,
            ArtifactScope(item.scope.namespace, item.scope.kind, item.scope.key),
            item.content_hash,
            item.payload_json,
        )
        for item in record.members
    )
    members = [
        {
            "artifact_type": item.artifact_type,
            "content_hash": item.content_hash,
            "logical_id": item.logical_id,
            "payload_json": json.loads(item.payload_json),
            "revision": item.revision,
            "scope": {
                "namespace": item.scope.namespace,
                "kind": item.scope.kind,
                "key": item.scope.key,
            },
        }
        for item in artifacts
    ]
    return CommandSuccess(slot, canonical_json_hash(members), artifacts)


@dataclass(frozen=True, slots=True)
class ValidateCalibrationRecordCommand:
    store: CalibrationValidationStore
    profile: ShadowCalibrationProfileSource
    registry_snapshot_sha256: str
    narrative: Stage1NarrativeProfileSource
    expected_shadow_profile_contract_sha256: str
    limits: CalibrationValidationLimits
    runtime_calibration_policy: RuntimeCalibrationPolicySource | None = None

    def __post_init__(self) -> None:
        if type(self.profile) is not ShadowCalibrationProfileSource:  # noqa: E721
            raise CalibrationValidationError(
                "validator requires a deployment-injected shadow profile"
            )
        _equal(
            canonical_json_hash(self.profile.to_mapping()),
            self.profile.canonical_sha256,
            "profile source",
        )
        if type(self.limits) is not CalibrationValidationLimits:  # noqa: E721
            raise CalibrationValidationError("validator requires explicit deployment byte limits")
        if self.runtime_calibration_policy is not None and type(self.runtime_calibration_policy) is not RuntimeCalibrationPolicySource:  # noqa: E721
            raise CalibrationValidationError("validator requires an exact static runtime calibration policy")
        # Recheck grammar even for typed deployment injections. Canonical source
        # identity is checked here; the authority loader still owns Git/raw-byte
        # provenance and the original (possibly formatted) source SHA-256.
        decode_shadow_calibration_profile_source(
            canonical_json_bytes(self.profile.to_mapping()),
            narrative=self.narrative,
            expected_profile_contract_sha256=self.expected_shadow_profile_contract_sha256,
        )

    def execute(self, binding: CalibrationValidationBinding) -> CommandOutcome:
        if type(binding) is not CalibrationValidationBinding:  # noqa: E721
            raise CalibrationValidationError("validator requires an exact immutable input binding")
        if (binding.runtime_measurement_identity is None) != (self.runtime_calibration_policy is None):
            raise CalibrationValidationError(
                "v2 validation requires both static runtime policy and measured runtime identity"
            )
        if self.runtime_calibration_policy is not None:
            policy = self.runtime_calibration_policy
            identity = binding.runtime_measurement_identity
            if (
                identity is None
                or not policy.accepts(identity)
                or (binding.profile_source_sha256, binding.registry_snapshot_sha256)
                != (policy.profile_source_sha256, policy.registry_snapshot_sha256)
            ):
                raise CalibrationValidationError(
                    "v2 validation identity does not match its exact static policy/profile"
                )
        claimed = self.store.claim_command(binding.claim)
        if not claimed.is_fresh_claim:
            return claimed
        try:
            _equal(
                [
                    binding.profile_version,
                    binding.profile_source_sha256,
                    binding.registry_snapshot_sha256,
                ],
                [
                    self.profile.profile_version,
                    self.profile.source_sha256,
                    self.registry_snapshot_sha256,
                ],
                "deployment profile binding",
            )
            measured = self.store.read_committed_shadow_calibration_measurement(binding)
            record = self._recompute(binding, measured)
        except (
            ValueError,
            CalibrationRecordError,
            BlobIntegrityError,
            SemanticInputIntegrityError,
            StoreValidationError,
        ) as error:
            return self.store.commit_command_rejection(
                CommandRejection(
                    claimed.command_slot_id,
                    "CALIBRATION_RECORD_INVALID",
                    canonical_json_bytes(
                        {"reason": str(error), "stage": "calibration_validation"}
                    ).decode(),
                    "denied",
                )
            )
        except (RuntimeStoreError, OSError) as error:
            return self.store.commit_command_rejection(
                CommandRejection(
                    claimed.command_slot_id,
                    "CALIBRATION_RECORD_VALIDATION_INDETERMINATE",
                    canonical_json_bytes(
                        {"reason": type(error).__name__, "stage": "calibration_validation"}
                    ).decode(),
                    "failed",
                )
            )
        # Commit failures/ambiguous outcomes are not rewritten into rejection:
        # the next call must reconcile/replay the Store's authoritative receipt.
        return self.store.commit_calibration_record_validation_success(
            _success(claimed.command_slot_id, record),
            binding,
            record,
        )

    def _recompute(
        self,
        binding: CalibrationValidationBinding,
        measured: PersistedShadowCalibrationMeasurement,
    ) -> CalibrationRecordArtifactSet:
        _equal(
            measured.manifest.reference.to_mapping(),
            binding.manifest_reference.to_mapping(),
            "manifest reference",
        )
        _equal(
            measured.results.reference.to_mapping(),
            binding.results_reference.to_mapping(),
            "results reference",
        )
        manifest = _object(
            json.loads(measured.manifest.payload_json),
            {
                "alignment_policy_sha256",
                "acceptance_policy_sha256",
                "calibration_corpus_set_sha256",
                "measurement_request_sha256",
                "native_invocations",
                "native_port_identity_sha256",
                "registry_snapshot_sha256",
                "schema_version",
                "shadow_profile_source_sha256",
                "vad_merge_policy_sha256",
                "word_gap_policy_sha256",
            },
            "measurement manifest",
        )
        results = _object(
            json.loads(measured.results.payload_json),
            {
                "measurement_manifest_sha256",
                "members",
                "per_producer_measurements",
                "schema_version",
            },
            "measurement results",
        )
        _equal(
            manifest["schema_version"],
            "shadow-calibration-measurement-manifest-v3",
            "manifest schema",
        )
        _equal(
            results["schema_version"], "shadow-calibration-measurement-results-v2", "results schema"
        )
        _equal(
            results["measurement_manifest_sha256"],
            binding.manifest_reference.content_hash,
            "results predecessor",
        )
        inputs = self._inputs()
        expected_fields = {
            "alignment_policy_sha256": inputs.alignment_policy_sha256,
            "acceptance_policy_sha256": inputs.acceptance_policy_sha256,
            "calibration_corpus_set_sha256": inputs.calibration_corpus_set_sha256,
            "native_port_identity_sha256": inputs.native_port_identity_sha256,
            "registry_snapshot_sha256": inputs.registry_snapshot_sha256,
            "shadow_profile_source_sha256": inputs.profile_source_sha256,
            "vad_merge_policy_sha256": inputs.vad_merge_policy_sha256,
            "word_gap_policy_sha256": inputs.word_gap_policy_sha256,
        }
        _equal(
            {name: manifest[name] for name in expected_fields}, expected_fields, "manifest policy"
        )
        native = _array(manifest["native_invocations"], "native invocations")
        members = _array(results["members"], "measurement members")
        if len(native) != len(members) or len(native) != len(
            self.profile.calibration_corpus.members
        ):
            raise CalibrationValidationError("measurement corpus is incomplete")
        corpus: list[ShadowCalibrationCorpusMember] = []
        evidence: list[CalibrationEvidenceMember] = []
        projections: list[ShadowCalibrationProjection] = []
        raw_refs: list[BlobRef] = []
        claimed_projections: list[object] = []
        total_bytes = 0
        for ordinal, (raw_member, raw_result, locked) in enumerate(
            zip(native, members, self.profile.calibration_corpus.members, strict=True)
        ):
            member = _object(
                raw_member,
                {
                    "corpus_member_reference_sha256",
                    "expected_anchor_reference_sha256",
                    "native_invocation",
                    "native_response_blob",
                    "raw_context",
                },
                "manifest member",
            )
            result = _object(
                raw_result,
                {
                    "corpus_member_reference_sha256",
                    "expected_anchor_reference_sha256",
                    "native_invocation",
                    "native_response_blob",
                    "native_response_sha256",
                    "projection",
                },
                "results member",
            )
            _equal(
                [
                    member["corpus_member_reference_sha256"],
                    member["expected_anchor_reference_sha256"],
                ],
                [locked.corpus_member_reference_sha256, locked.expected_anchor_reference_sha256],
                "locked corpus order",
            )
            for key in (
                "corpus_member_reference_sha256",
                "expected_anchor_reference_sha256",
                "native_invocation",
                "native_response_blob",
            ):
                _equal(result[key], member[key], f"results member {key}")
            context = decode_shadow_calibration_raw_context(member["raw_context"])
            invocation = decode_shadow_calibration_invocation(
                member["native_invocation"], context=context
            )
            source = context.source
            _equal(
                [source.source_id, source.source_sha256, source.corpus_member_reference_sha256],
                [locked.source_id, locked.source_sha256, locked.corpus_member_reference_sha256],
                "locked source",
            )
            source_blob = {
                "object_id": source.blob_id,
                "content_hash": source.blob_sha256,
                "byte_length": source.blob_byte_length,
                "media_type": source.blob_media_type,
            }
            _equal(
                canonical_json_hash(source_blob),
                locked.source_blob_reference_sha256,
                "source BlobRef",
            )
            _equal(
                shadow_calibration_anchor_reference_sha256(context),
                locked.expected_anchor_reference_sha256,
                "anchor reference",
            )
            _equal(
                [item.to_mapping() for item in context.producer_identities],
                [item.common_mapping() for item in self.profile.native_timed_speech.producers],
                "locked producer identities",
            )
            policies = self.profile.timing_policies
            _equal(
                [
                    context.policies.timed_speech_policy_sha256,
                    context.policies.word_gap_ms,
                    context.policies.vad_merge_gap_ms,
                    context.source_byte_limits.service_max_request_bytes,
                ],
                [
                    policies.timed_speech_policy_sha256,
                    policies.word_gap_ms,
                    policies.vad_merge_gap_ms,
                    self.profile.native_timed_speech.max_request_bytes,
                ],
                "locked timing/native limits",
            )
            corpus.append(
                ShadowCalibrationCorpusMember(
                    locked.corpus_member_reference_sha256,
                    locked.expected_anchor_reference_sha256,
                    context,
                    invocation,
                )
            )
            # Validate locked policy/clock identity before any blob I/O.
            MeasureShadowCalibrationRequest(inputs, (corpus[-1],))
            reference = _blob(member["native_response_blob"])
            total_bytes += reference.byte_length
            if (
                reference.byte_length > invocation.request_mapping.max_response_bytes
                or reference.byte_length > self.limits.max_response_bytes
                or total_bytes > self.limits.max_total_response_bytes
            ):
                raise CalibrationValidationError("raw response exceeds committed response limit")
            _equal(reference.content_hash, result["native_response_sha256"], "raw response digest")
            raw_refs.append(reference)
            claimed_projections.append(result["projection"])
        request = MeasureShadowCalibrationRequest(inputs, tuple(corpus))
        _equal(
            [
                manifest["measurement_request_sha256"],
                measured.request_hash,
                measured.job.job_key,
                measured.job.profile,
            ],
            [request.request_hash, request.request_hash, request.job.job_key, "shadow"],
            "measurement producing request",
        )
        # Only after the ENTIRE corpus metadata, budget and producing request
        # closes may any raw response be loaded. Bad later members cannot cause
        # partial expensive reads; projection decoding remains per-member.
        for ordinal, (corpus_member, reference, claimed_projection) in enumerate(
            zip(corpus, raw_refs, claimed_projections, strict=True)
        ):
            context, invocation = corpus_member.raw_context, corpus_member.native_invocation
            raw = self.store.read_immutable_blob(measured.job, reference)
            decoded = derive_shadow_calibration_raw_response(
                ShadowCalibrationRawBlob(
                    raw, reference.media_type, reference.byte_length, reference.content_hash
                ),
                invocation,
                context,
            )
            projection = decoded.projection
            projection_mapping = shadow_calibration_projection_mapping(projection)
            _equal(claimed_projection, projection_mapping, "claimed projection")
            projections.append(projection)
            evidence.append(
                CalibrationEvidenceMember(
                    ordinal,
                    corpus_member.corpus_member_reference_sha256,
                    corpus_member.expected_anchor_reference_sha256,
                    reference.content_hash,
                    canonical_json_hash(projection_mapping),
                )
            )
        refs = [member.corpus_member_reference_sha256 for member in corpus]
        _equal(
            results["per_producer_measurements"],
            {role: _aggregate(tuple(projections), refs, role) for role in ("asr", "vad")},
            "claimed aggregate",
        )
        identity = CalibrationRecordIdentity(
            inputs.profile_source_sha256,
            inputs.registry_snapshot_sha256,
            inputs.calibration_corpus_set_sha256,
            inputs.native_port_identity_sha256,
            inputs.source_clock_id,
            inputs.source_time_base,
            self.profile.timing_policies.timed_speech_policy_sha256,
            inputs.word_gap_policy_sha256,
            inputs.vad_merge_policy_sha256,
            inputs.alignment_policy_sha256,
            inputs.acceptance_policy_sha256,
            (
                None
                if binding.runtime_measurement_identity is None
                else binding.runtime_measurement_identity.canonical_sha256
            ),
        )
        children: list[CalibrationRecordMemberPayload] = []
        for index, role in enumerate((CalibrationRecordRole.ASR, CalibrationRecordRole.VAD)):
            producer = self.profile.native_timed_speech.producers[index]
            producer_identity = CalibrationRecordProducerIdentity(
                role,
                producer.producer_id,
                producer.producer_version,
                producer.generation_policy_sha256,
                producer.detector_sha256,
                producer.calibration_policy_sha256,
                producer.model_id,
                producer.model_revision,
                producer.model_sha256,
                producer.inference_kind,
                producer.service_sha256,
            )
            matches = tuple(
                match
                for ref, projection in zip(refs, projections, strict=True)
                for match in _matches(
                    ref,
                    projection.summary.asr
                    if role is CalibrationRecordRole.ASR
                    else projection.summary.vad,
                )
            )
            children.append(
                CalibrationRecordMemberPayload.from_matches(
                    identity, producer_identity, tuple(evidence), matches
                )
            )
        candidate = build_calibration_record_candidate(
            profile_version=binding.profile_version,
            identity=identity,
            measurement_manifest_sha256=binding.manifest_reference.content_hash,
            measurement_results_sha256=binding.results_reference.content_hash,
            asr=children[0],
            vad=children[1],
            runtime_capability_id=(
                None
                if binding.runtime_measurement_identity is None
                else binding.runtime_measurement_identity.runtime_capability_id
            ),
        )
        input_hash = calibration_validation_input_hash(
            profile_key=candidate.profile_key,
            identity=identity,
            measurement_manifest_sha256=candidate.aggregate.measurement_manifest_sha256,
            measurement_results_sha256=candidate.aggregate.measurement_results_sha256,
            asr=candidate.asr,
            vad=candidate.vad,
        )
        return validator_internal_assemble_accepted_artifact_set(
            IndependentlyRecomputedCalibrationResult(
                candidate,
                CALIBRATION_VALIDATION_CHECKS,
                input_hash,
                calibration_validation_result_hash(
                    candidate.aggregate, candidate.asr, candidate.vad
                ),
                CALIBRATION_VALIDATOR_COMMAND,
                CALIBRATION_VALIDATOR_PRINCIPAL,
            )
        )

    def _inputs(self) -> ShadowCalibrationInputs:
        profile = self.profile
        policy, native, clock = (
            profile.timing_policies,
            profile.native_timed_speech,
            profile.source_clock_policy,
        )
        return ShadowCalibrationInputs(
            profile.source_sha256,
            self.registry_snapshot_sha256,
            profile.calibration_corpus.corpus_set_sha256,
            native.native_port_identity_sha256,
            policy.word_gap_policy_sha256,
            policy.vad_merge_policy_sha256,
            policy.alignment_policy_sha256,
            policy.acceptance_policy_sha256,
            native.producers[0].producer_id,
            native.producers[1].producer_id,
            clock.clock_id,
            clock.time_base,
        )
