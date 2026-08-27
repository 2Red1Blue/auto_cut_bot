"""Provider-free authority validation, including coherent untrusted mutations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.media import (
    CalibrationAnchor,
    CalibrationProducer,
    ShadowCalibrationAudioClock,
    ShadowCalibrationContainer,
    ShadowCalibrationInvocation,
    ShadowCalibrationPolicies,
    ShadowCalibrationProducerIdentity,
    ShadowCalibrationRawBlob,
    ShadowCalibrationRawContext,
    ShadowCalibrationRequestMapping,
    ShadowCalibrationSource,
    ShadowCalibrationSourceByteLimits,
    ShadowCalibrationTranscriptCapability,
    TickRange,
    TimeBase,
)
from autocut_kernel.media.calibration_record import CalibrationRecordArtifactSet
from autocut_kernel.media.shadow_calibration_raw import (
    SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE,
    SHADOW_CALIBRATION_RAW_RESPONSE_SCHEMA,
    derive_shadow_calibration_raw_response,
    shadow_calibration_anchor_reference_sha256,
    shadow_calibration_context_mapping,
    shadow_calibration_invocation_mapping,
    shadow_calibration_projection_mapping,
)
from autocut_kernel.pipeline.measure_shadow_calibration_command import (
    MeasureShadowCalibrationRequest,
    ShadowCalibrationCorpusMember,
    ShadowCalibrationInputs,
)
from autocut_kernel.pipeline.validate_calibration_record_command import (
    CalibrationValidationError,
    CalibrationValidationLimits,
    ValidateCalibrationRecordCommand,
)
from autocut_kernel.registry.authority_profiles import (
    AuthorityProfileCapabilities,
    CalibrationAcceptance,
    CalibrationCorpus,
    CalibrationCorpusMember,
    NativeTimedSpeechProducer,
    NativeTimedSpeechProfile,
    RuntimeCalibrationCapabilityPolicy,
    RuntimeCalibrationPolicySource,
    ShadowCalibrationProfileSource,
    SourceClockPolicy,
    TimingPolicies,
    decode_shadow_calibration_profile_source,
    decode_stage1_narrative_profile_source,
)
from autocut_kernel.store import (
    ArtifactScope,
    BlobIntegrityError,
    BlobRef,
    BlobUnavailableError,
    CalibrationValidationBinding,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    CommittedArtifactMemberReference,
    Job,
    PersistedCommittedArtifactMember,
    PersistedShadowCalibrationMeasurement,
    PostgresRuntimeStore,
    RuntimeStoreError,
    ShadowMeasurementMemberPlan,
    ShadowMeasurementPlan,
    ShadowMeasurementStagedResponse,
)

from tests.authority.test_authority_profile_sources import (
    _narrative_mapping,
    synthetic_stage1_command_policy,
)
from tests.media.test_calibration_record_persistence import _runtime_measurement


def _hash(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _blob_mapping(blob: BlobRef) -> dict[str, object]:
    return {
        "object_id": str(blob.object_id),
        "content_hash": blob.content_hash,
        "byte_length": blob.byte_length,
        "media_type": blob.media_type,
    }


def _producer(kind: CalibrationProducer) -> ShadowCalibrationProducerIdentity:
    return ShadowCalibrationProducerIdentity(
        kind,
        f"native-{kind.value}",
        "1",
        _hash(kind.value + "-generation"),
        _hash(kind.value + "-detector"),
        _hash(kind.value + "-calibration"),
        "SenseVoiceSmall" if kind is CalibrationProducer.ASR else "fsmn-vad",
        "revision-1",
        _hash(kind.value + "-model"),
        "sensevoice-word-timestamp" if kind is CalibrationProducer.ASR else "fsmn-vad-direct",
        _hash("service"),
    )


@dataclass
class FakeStore:
    measured: PersistedShadowCalibrationMeasurement
    blobs: dict[UUID, bytes]
    outcome: CommandOutcome = field(
        default_factory=lambda: CommandOutcome(uuid4(), "running", True)
    )
    reads: int = 0
    rejected: CommandRejection | None = None
    accepted: CalibrationRecordArtifactSet | None = None
    read_failure: Exception | None = None
    commit_failure: Exception | None = None
    blob_failure: Exception | None = None

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        assert claim.command_name == "ValidateCalibrationRecord@2.1.3"
        return self.outcome

    def read_committed_shadow_calibration_measurement(
        self, binding: CalibrationValidationBinding
    ) -> PersistedShadowCalibrationMeasurement:
        self.reads += 1
        if self.read_failure:
            raise self.read_failure
        return self.measured

    def read_immutable_blob(self, job: Job, reference: BlobRef) -> bytes:
        self.reads += 1
        assert job == self.measured.job
        if self.blob_failure:
            raise self.blob_failure
        return self.blobs[reference.object_id]

    def commit_calibration_record_validation_success(
        self,
        success: CommandSuccess,
        binding: CalibrationValidationBinding,
        record: CalibrationRecordArtifactSet,
    ) -> CommandOutcome:
        if self.commit_failure:
            raise self.commit_failure
        assert len(success.artifacts) == 4
        self.accepted = record
        self.outcome = CommandOutcome(
            success.command_slot_id, "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4()
        )
        return self.outcome

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome:
        self.rejected = rejection
        self.outcome = CommandOutcome(
            rejection.command_slot_id,
            rejection.outcome,
            receipt_id=uuid4(),
            failure_code=rejection.failure_code,
        )
        return self.outcome


def _fixture() -> tuple[ValidateCalibrationRecordCommand, CalibrationValidationBinding, FakeStore]:
    asr, vad = _producer(CalibrationProducer.ASR), _producer(CalibrationProducer.VAD)
    clock = ShadowCalibrationAudioClock("source-audio", TimeBase(1, 1000), 0, 1000)
    policy = ShadowCalibrationPolicies(
        _hash("speech"), _hash("word-gap"), _hash("vad-gap"), 700, 350
    )
    limits = ShadowCalibrationSourceByteLimits(8192, 8192, 8192)
    capability = ShadowCalibrationTranscriptCapability(
        "sensevoice_word_guard_v1",
        "complete",
        "utterance_gap_protected_range",
        "not_applicable",
        "complete",
        "required",
    )
    contexts, invocations, locked_members, blobs, projections, native, results_members = (
        [],
        [],
        [],
        {},
        [],
        [],
        [],
    )
    for number in range(2):
        source = ShadowCalibrationSource(
            f"source-{number}",
            _hash(f"source-{number}"),
            _hash(f"member-{number}"),
            str(uuid4()),
            _hash(f"source-{number}"),
            4096,
            "video/mp4",
        )
        context = ShadowCalibrationRawContext(
            source,
            limits,
            ShadowCalibrationContainer("video/mp4", ".mp4"),
            clock,
            policy,
            _hash("native"),
            capability,
            asr,
            vad,
            (
                CalibrationAnchor(
                    f"anchor-{number}",
                    CalibrationProducer.ASR,
                    asr.producer_id,
                    clock.clock_id,
                    clock.time_base,
                    TickRange(101, 201),
                ),
            ),
            (
                CalibrationAnchor(
                    f"anchor-{number}",
                    CalibrationProducer.VAD,
                    vad.producer_id,
                    clock.clock_id,
                    clock.time_base,
                    TickRange(82, 302),
                ),
            ),
        )
        mapping = ShadowCalibrationRequestMapping(
            source,
            limits,
            context.container,
            clock,
            clock.full_range,
            _hash("native"),
            65536,
            capability,
            policy.timed_speech_policy_sha256,
            policy.word_gap_policy_sha256,
            policy.vad_merge_policy_sha256,
            700,
            350,
            (asr, vad),
        )
        invocation = ShadowCalibrationInvocation(
            source.corpus_member_reference_sha256, mapping.sha256, mapping, mapping.sha256
        )
        anchor_hash = shadow_calibration_anchor_reference_sha256(context)
        source_blob = BlobRef(
            UUID(source.blob_id),
            source.blob_sha256,
            source.blob_byte_length,
            source.blob_media_type,
        )
        locked_members.append(
            CalibrationCorpusMember(
                f"member-{number}",
                source.corpus_member_reference_sha256,
                source.source_id,
                source.source_sha256,
                canonical_json_hash(_blob_mapping(source_blob)),
                anchor_hash,
            )
        )
        raw = canonical_json_bytes(
            {
                "schema_version": SHADOW_CALIBRATION_RAW_RESPONSE_SCHEMA,
                "request_identity_sha256": invocation.request_identity_sha256,
                "source": source.to_response_mapping(),
                "audio_clock": clock.to_mapping(),
                "requested_range": {"in_tick": 0, "out_tick": 1000},
                "timed_speech_policy_sha256": policy.timed_speech_policy_sha256,
                "word_gap_policy_sha256": policy.word_gap_policy_sha256,
                "vad_merge_policy_sha256": policy.vad_merge_policy_sha256,
                "native_profile_identity_sha256": _hash("native"),
                "producer_identities": [asr.to_mapping(), vad.to_mapping()],
                "asr_native_output": [{"text": "a", "words": ["a"], "timestamp": [[100, 200]]}],
                "vad_native_output": [{"value": [[80, 300]]}],
            }
        )
        blob = BlobRef(
            uuid4(),
            "sha256:" + hashlib.sha256(raw).hexdigest(),
            len(raw),
            SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE,
        )
        blobs[blob.object_id] = raw
        projection = derive_shadow_calibration_raw_response(
            ShadowCalibrationRawBlob(raw, blob.media_type, len(raw), blob.content_hash),
            invocation,
            context,
        ).projection
        projections.append(projection)
        common = {
            "corpus_member_reference_sha256": source.corpus_member_reference_sha256,
            "expected_anchor_reference_sha256": anchor_hash,
            "native_invocation": shadow_calibration_invocation_mapping(invocation),
            "native_response_blob": _blob_mapping(blob),
        }
        native.append({**common, "raw_context": shadow_calibration_context_mapping(context)})
        results_members.append(
            {
                **common,
                "native_response_sha256": blob.content_hash,
                "projection": shadow_calibration_projection_mapping(projection),
            }
        )
        contexts.append(context)
        invocations.append(invocation)
    corpus = CalibrationCorpus(
        canonical_json_hash([item.to_mapping() for item in locked_members]), tuple(locked_members)
    )
    native_producers = tuple(NativeTimedSpeechProducer(**item.to_mapping()) for item in (asr, vad))
    # Synthetic grammar fixture: the real decoder binds the complete explicit
    # policy and its canonical hash; this is not deployed/accepted authority.
    narrative = decode_stage1_narrative_profile_source(canonical_json_bytes(_narrative_mapping()))
    profile = ShadowCalibrationProfileSource(
        "1",
        _hash("contract"),
        _hash("profile"),
        _hash("placeholder"),
        narrative.reference,
        NativeTimedSpeechProfile(
            _hash("service"), "1.3.0", "2.8.0", 8192, _hash("native"), native_producers
        ),
        SourceClockPolicy(_hash("clock-policy"), clock.clock_id, clock.time_base),
        corpus,
        TimingPolicies(
            policy.timed_speech_policy_sha256,
            policy.word_gap_policy_sha256,
            policy.vad_merge_policy_sha256,
            _hash("alignment"),
            _hash("acceptance"),
            700,
            350,
        ),
        AuthorityProfileCapabilities(True, False, False, False, False, False, False, False, False),
        CalibrationAcceptance(1),
    )
    profile = decode_shadow_calibration_profile_source(
        canonical_json_bytes(profile.to_mapping()),
        narrative=narrative,
        expected_profile_contract_sha256=_hash("contract"),
    )
    inputs = ShadowCalibrationInputs(
        profile.source_sha256,
        _hash("registry"),
        corpus.corpus_set_sha256,
        _hash("native"),
        policy.word_gap_policy_sha256,
        policy.vad_merge_policy_sha256,
        _hash("alignment"),
        _hash("acceptance"),
        asr.producer_id,
        vad.producer_id,
        clock.clock_id,
        clock.time_base,
    )
    request = MeasureShadowCalibrationRequest(
        inputs,
        tuple(
            ShadowCalibrationCorpusMember(
                member.corpus_member_reference_sha256,
                member.expected_anchor_reference_sha256,
                context,
                invocation,
            )
            for member, context, invocation in zip(
                locked_members, contexts, invocations, strict=True
            )
        ),
    )
    manifest = {
        "schema_version": "shadow-calibration-measurement-manifest-v3",
        "native_invocations": native,
        "measurement_request_sha256": request.request_hash,
        "shadow_profile_source_sha256": profile.source_sha256,
        "registry_snapshot_sha256": _hash("registry"),
        "calibration_corpus_set_sha256": corpus.corpus_set_sha256,
        "native_port_identity_sha256": _hash("native"),
        "word_gap_policy_sha256": policy.word_gap_policy_sha256,
        "vad_merge_policy_sha256": policy.vad_merge_policy_sha256,
        "alignment_policy_sha256": _hash("alignment"),
        "acceptance_policy_sha256": _hash("acceptance"),
    }
    aggregates = {}
    for role, producer, bound in (("asr", asr, 1), ("vad", vad, 2)):
        aggregates[role] = {
            "aggregation": "member-bound-calibration-statistics-v1",
            "absolute_maximum_tick": bound,
            "clock_id": clock.clock_id,
            "corpus_member_count": 2,
            "corpus_member_references": [
                member.corpus_member_reference_sha256 for member in locked_members
            ],
            "early_maximum_tick": bound,
            "eligible_anchor_count": 2,
            "inference_kind": producer.inference_kind,
            "invalid_or_indeterminate_member_count": 0,
            "late_maximum_tick": 0,
            "matched_anchor_count": 2,
            "producer": role,
            "producer_id": producer.producer_id,
            "time_base": {"numerator": 1, "denominator": 1000},
        }
    results = {
        "schema_version": "shadow-calibration-measurement-results-v2",
        "measurement_manifest_sha256": canonical_json_hash(manifest),
        "members": results_members,
        "per_producer_measurements": aggregates,
    }
    receipt_id, set_id, slot_id = uuid4(), uuid4(), uuid4()
    scope = ArtifactScope("autocut_calibration", "shadow_run", request.job.job_key)
    refs = tuple(
        CommittedArtifactMemberReference(
            receipt_id,
            set_id,
            ordinal,
            scope,
            artifact_type,
            logical,
            1,
            canonical_json_hash(payload),
        )
        for ordinal, artifact_type, logical, payload in (
            (0, "calibration_measurement_manifest", "measurement-manifest", manifest),
            (1, "calibration_measurement_results", "measurement-results", results),
        )
    )
    persisted = tuple(
        PersistedCommittedArtifactMember(ref, canonical_json_bytes(payload).decode(), slot_id)
        for ref, payload in zip(refs, (manifest, results), strict=True)
    )
    binding = CalibrationValidationBinding(
        "1", profile.source_sha256, _hash("registry"), refs[0], refs[1], "validate:attempt-1"
    )
    store = FakeStore(
        PersistedShadowCalibrationMeasurement(
            job=request.job,
            request_hash=request.request_hash,
            command_slot_id=slot_id,
            manifest=persisted[0],
            results=persisted[1],
        ),
        blobs,
    )
    return (
        ValidateCalibrationRecordCommand(
            store,
            profile,
            _hash("registry"),
            narrative,
            _hash("contract"),
            CalibrationValidationLimits(65536, 131072),
        ),
        binding,
        store,
    )


def _mutate(
    store: FakeStore, binding: CalibrationValidationBinding, target: str, mutation: object
) -> CalibrationValidationBinding:
    manifest, results = (
        json.loads(store.measured.manifest.payload_json),
        json.loads(store.measured.results.payload_json),
    )
    mutation(manifest if target == "manifest" else results)
    results["measurement_manifest_sha256"] = canonical_json_hash(manifest)
    updated = []
    for old, payload in zip(
        (store.measured.manifest, store.measured.results), (manifest, results), strict=True
    ):
        ref = replace(old.reference, content_hash=canonical_json_hash(payload))
        updated.append(
            replace(old, reference=ref, payload_json=canonical_json_bytes(payload).decode())
        )
    store.measured = replace(store.measured, manifest=updated[0], results=updated[1])
    return replace(
        binding, manifest_reference=updated[0].reference, results_reference=updated[1].reference
    )


def test_synthetic_fixture_binds_decoded_v2_narrative_and_complete_command_policy() -> None:
    command, _, _ = _fixture()
    expected_policy = synthetic_stage1_command_policy()
    narrative = command.narrative
    assert narrative.to_mapping()["schema_version"] == "autocut-stage1-narrative-profile-v2"
    assert narrative.command_policy == expected_policy
    assert narrative.reference.stage1_command_policy_sha256 == expected_policy.canonical_hash
    assert narrative.canonical_sha256 == canonical_json_hash(narrative.to_mapping())
    assert command.profile.stage1_narrative_profile == narrative.reference
    assert decode_stage1_narrative_profile_source(
        canonical_json_bytes(narrative.to_mapping())
    ) == narrative


def test_v2_validator_requires_exact_static_policy_and_runtime_measurement_identity() -> None:
    command, binding, _ = _fixture()
    identity = _runtime_measurement()
    policy = RuntimeCalibrationPolicySource(
        binding.profile_source_sha256,
        binding.registry_snapshot_sha256,
        _hash("runtime-policy-source"),
        _hash("runtime-policy-canonical"),
        (RuntimeCalibrationCapabilityPolicy("pc_cuda", "cuda"),),
    )
    v2_command = replace(command, runtime_calibration_policy=policy)
    with pytest.raises(CalibrationValidationError, match="requires both"):
        v2_command.execute(binding)
    with pytest.raises(CalibrationValidationError, match="requires both"):
        command.execute(replace(binding, runtime_measurement_identity=identity))
    assert v2_command.execute(replace(binding, runtime_measurement_identity=identity)).state == "succeeded"


def test_pc_and_mac_v2_validation_create_distinct_accepted_record_closures() -> None:
    pc_command, pc_binding, pc_store = _fixture()
    mac_command, mac_binding, mac_store = _fixture()
    pc_identity = _runtime_measurement()
    mac_identity = _runtime_measurement(capability_id="mac_cpu")

    def configured(command, binding, capability_id: str, device_class: str):
        return replace(
            command,
            runtime_calibration_policy=RuntimeCalibrationPolicySource(
                binding.profile_source_sha256,
                binding.registry_snapshot_sha256,
                _hash(f"{capability_id}-policy-source"),
                _hash(f"{capability_id}-policy-canonical"),
                (RuntimeCalibrationCapabilityPolicy(capability_id, device_class),),
            ),
        )

    assert configured(pc_command, pc_binding, "pc_cuda", "cuda").execute(
        replace(pc_binding, runtime_measurement_identity=pc_identity)
    ).state == "succeeded"
    assert configured(mac_command, mac_binding, "mac_cpu", "cpu").execute(
        replace(mac_binding, runtime_measurement_identity=mac_identity)
    ).state == "succeeded"
    assert pc_store.accepted is not None and mac_store.accepted is not None
    assert pc_store.accepted.aggregate.content_hash != mac_store.accepted.aggregate.content_hash
    assert pc_store.accepted.validation.content_hash != mac_store.accepted.validation.content_hash
    assert pc_store.accepted.members[0].scope.key == "runtime_calibration@pc_cuda@1"
    assert mac_store.accepted.members[0].scope.key == "runtime_calibration@mac_cpu@1"


def test_acceptance_recomputes_positive_bounds_and_scopes_local_match_ids() -> None:
    command, binding, store = _fixture()
    assert command.execute(binding).state == "succeeded", store.rejected
    record = store.accepted
    assert record is not None
    assert record.aggregate.asr_accepted_bound_tick == 1
    assert record.aggregate.vad_accepted_bound_tick == 2
    assert len({item.observation_id for item in record.asr.matches}) == 2
    assert len({item.anchor_id for item in record.asr.matches}) == 2
    assert record.validation.record_sha256 == record.aggregate.content_hash
    reads = store.reads
    assert command.execute(binding).state == "succeeded"
    assert store.reads == reads


@pytest.mark.parametrize(
    "target,mutation",
    [
        (
            "manifest",
            lambda m: m.update(schema_version="shadow-calibration-measurement-manifest-v2"),
        ),
        ("manifest", lambda m: m["native_invocations"].reverse()),
        ("manifest", lambda m: m["native_invocations"].pop()),
        (
            "manifest",
            lambda m: m["native_invocations"][0]["raw_context"]["asr_anchors"][0][
                "expected_range"
            ].update(in_tick=100),
        ),
        ("manifest", lambda m: m.update(extra="not permitted")),
        (
            "results",
            lambda m: m["members"][0]["projection"]["summary"]["asr"].update(
                absolute_maximum_tick=True
            ),
        ),
        (
            "results",
            lambda m: m["per_producer_measurements"]["asr"].update(absolute_maximum_tick=10),
        ),
        ("results", lambda m: m["members"][0].update(native_response_sha256=_hash("forged"))),
    ],
)
def test_coherent_tampering_never_creates_accepted_artifacts(target: str, mutation: object) -> None:
    command, binding, store = _fixture()
    binding = _mutate(store, binding, target, mutation)
    outcome = command.execute(binding)
    assert outcome.state == "denied"
    assert outcome.failure_code == "CALIBRATION_RECORD_INVALID"
    assert store.accepted is None


def test_unavailable_measurement_is_receipt_only_and_replays() -> None:
    command, binding, store = _fixture()
    store.read_failure = RuntimeStoreError("unavailable")
    assert command.execute(binding).state == "failed"
    assert command.execute(binding).state == "failed"
    assert store.reads == 1
    assert store.accepted is None
    assert store.rejected.failure_code == "CALIBRATION_RECORD_VALIDATION_INDETERMINATE"


def test_raw_hash_tampering_denies() -> None:
    command, binding, store = _fixture()
    key = next(iter(store.blobs))
    store.blobs[key] += b" "
    assert command.execute(binding).state == "denied"
    assert store.accepted is None


def test_profile_drift_denies_before_reading_evidence() -> None:
    command, binding, store = _fixture()
    assert (
        command.execute(replace(binding, registry_snapshot_sha256=_hash("other"))).state == "denied"
    )
    assert store.reads == 0


def test_ambiguous_commit_is_not_rewritten_as_a_rejection() -> None:
    command, binding, store = _fixture()
    store.commit_failure = RuntimeStoreError("connection lost during commit")
    with pytest.raises(RuntimeStoreError):
        command.execute(binding)
    assert store.rejected is None


def test_later_member_and_request_drift_are_rejected_before_any_raw_read() -> None:
    for mutation in (
        lambda m: m["native_invocations"][1].update(
            expected_anchor_reference_sha256=_hash("other")
        ),
        lambda m: m.update(measurement_request_sha256=_hash("other-request")),
    ):
        command, binding, store = _fixture()
        binding = _mutate(store, binding, "manifest", mutation)
        assert command.execute(binding).state == "denied"
        assert store.reads == 1  # The exact measurement pair only; zero raw reads.


@pytest.mark.parametrize("budget", ("member", "total"))
def test_deployment_byte_budgets_deny_before_any_raw_read(budget: str) -> None:
    command, binding, store = _fixture()
    largest = max(len(raw) for raw in store.blobs.values())
    ceiling = largest - 1 if budget == "member" else largest
    command = replace(command, limits=CalibrationValidationLimits(ceiling, ceiling))
    assert command.execute(binding).state == "denied"
    assert store.reads == 1
    assert store.accepted is None


@pytest.mark.parametrize(
    "mutation",
    (
        lambda p: replace(p, capabilities=replace(p.capabilities, runtime_profile_selection=True)),
        lambda p: replace(p, calibration_acceptance=CalibrationAcceptance(999)),
    ),
)
def test_invalid_typed_profile_cannot_self_certify_with_a_new_hash(mutation: object) -> None:
    command, _, _ = _fixture()
    profile = mutation(command.profile)
    profile = replace(profile, canonical_sha256=canonical_json_hash(profile.to_mapping()))
    with pytest.raises(ValueError):
        replace(command, profile=profile)


def test_raw_integrity_failure_is_not_retryable_unavailability() -> None:
    command, binding, store = _fixture()
    store.blob_failure = BlobIntegrityError("raw bytes have wrong digest")
    assert command.execute(binding).state == "denied"
    assert store.rejected.failure_code == "CALIBRATION_RECORD_INVALID"


def test_proven_blob_unavailable_is_failed_and_replay_does_not_read_again() -> None:
    command, binding, store = _fixture()
    store.blob_failure = BlobUnavailableError("known object temporarily unavailable")
    outcome = command.execute(binding)
    assert outcome.state == "failed"
    assert outcome.failure_code == "CALIBRATION_RECORD_VALIDATION_INDETERMINATE"
    reads = store.reads
    assert command.execute(binding).receipt_id == outcome.receipt_id
    assert store.reads == reads
    assert store.accepted is None


def test_postgres_measurement_to_independent_validation_and_replay() -> None:
    """Real transaction path with synthetic raw fixtures, not a native inference claim."""
    psycopg = pytest.importorskip("psycopg")
    dsn = "postgresql://ac_user:ac_password_2026@127.0.0.1:5433/ac_autocut_verify"
    try:
        connection = psycopg.connect(dsn, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("disposable calibration verification PostgreSQL is unavailable")
    with connection, connection.cursor() as cursor:
        if connection.info.dbname != "ac_autocut_verify":
            pytest.fail("integration fixture may reset only ac_autocut_verify")
        cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
        cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
        for migration in sorted(Path("packages/autocut-kernel/migrations").glob("*.sql")):
            cursor.execute(migration.read_text(encoding="utf-8"))
    command, old_binding, fixture = _fixture()
    manifest = json.loads(fixture.measured.manifest.payload_json)
    result = json.loads(fixture.measured.results.payload_json)
    plan_members = tuple(
        ShadowMeasurementMemberPlan(
            item["corpus_member_reference_sha256"],
            ordinal,
            canonical_json_bytes(item["native_invocation"]).decode(),
            canonical_json_bytes(item["raw_context"]).decode(),
            item["expected_anchor_reference_sha256"],
        )
        for ordinal, item in enumerate(manifest["native_invocations"])
    )
    payload = {
        "command": "MeasureShadowCalibrationCommand@2.1.3",
        "measurement_protocol": "shadow-calibration-measurement-v1",
        "shadow_inputs": command._inputs().to_mapping(),
        "corpus_members": [
            {key: value for key, value in item.items() if key != "native_response_blob"}
            for item in manifest["native_invocations"]
        ],
    }
    claim = CommandClaim(
        fixture.measured.job,
        f"shadow-calibration:{fixture.measured.job.job_key}",
        "MeasureShadowCalibrationCommand@2.1.3",
        fixture.measured.request_hash,
        execution_kind="deterministic",
    )
    plan = ShadowMeasurementPlan(claim, canonical_json_bytes(payload).decode(), plan_members)
    store = PostgresRuntimeStore(lambda: psycopg.connect(dsn))
    attempt = store.claim_or_read_shadow_measurement_attempt(claim, plan)
    for ordinal, item in enumerate(result["members"]):
        member = attempt.members[ordinal]
        lease = store.acquire_shadow_measurement_member_lease(
            attempt.attempt_id,
            member.corpus_member_reference_sha256,
            expected_version=member.version,
        )
        assert lease is not None
        raw = fixture.blobs[UUID(item["native_response_blob"]["object_id"])]
        attempt = store.stage_shadow_measurement_member_response(
            attempt.attempt_id,
            member.corpus_member_reference_sha256,
            expected_version=lease.member.version,
            lease_token=lease.lease_token,
            staged=ShadowMeasurementStagedResponse(
                raw,
                item["native_response_sha256"],
                SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE,
                canonical_json_bytes(item["projection"]).decode(),
            ),
        )
    measured_outcome = store.finalize_shadow_measurement_success(
        attempt.attempt_id, expected_version=attempt.version
    )
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT member.ordinal, artifact.content_hash FROM runtime.artifact_set_members member
            JOIN runtime.artifacts artifact ON artifact.artifact_id = member.artifact_id
            WHERE member.artifact_set_id = %s ORDER BY member.ordinal""",
            (measured_outcome.artifact_set_id,),
        )
        hashes = [
            row[1].decode() if isinstance(row[1], bytes) else row[1] for row in cursor.fetchall()
        ]
    binding = replace(
        old_binding,
        manifest_reference=replace(
            old_binding.manifest_reference,
            receipt_id=measured_outcome.receipt_id,
            artifact_set_id=measured_outcome.artifact_set_id,
            content_hash=hashes[0],
        ),
        results_reference=replace(
            old_binding.results_reference,
            receipt_id=measured_outcome.receipt_id,
            artifact_set_id=measured_outcome.artifact_set_id,
            content_hash=hashes[1],
        ),
    )
    command = replace(command, store=store)
    accepted = command.execute(binding)
    assert accepted.state == "succeeded", accepted
    replay = command.execute(binding)
    assert replay.receipt_id == accepted.receipt_id
    assert replay.artifact_set_id == accepted.artifact_set_id
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM runtime.calibration_record_anchors")
        assert cursor.fetchone() == (1,)
        cursor.execute("SELECT state FROM runtime.jobs WHERE job_key = %s", (binding.job.job_key,))
        state = cursor.fetchone()[0]
        assert state in ("succeeded", b"succeeded")
