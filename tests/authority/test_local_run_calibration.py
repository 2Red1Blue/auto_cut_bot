"""Binding tests use a named fake reader, never real calibration acceptance.

The positive source context is built from two real synthetic Git chains. Record
payloads are self-consistent unit fixtures, not measured model/Store evidence.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from authority.errors import GateViolation
from authority.local_run_calibration import bind_local_run_calibration
from authority.local_run_context import LockedLocalRunSourceContext, build_locked_local_run_context
from authority.shadow_context import LockedShadowSourceContext, build_locked_shadow_context
from autocut_kernel.media.calibration_record import (
    CalibrationEvidenceMember,
    CalibrationRecordArtifactSet,
    CalibrationRecordIdentity,
    CalibrationRecordProducerIdentity,
    CalibrationRecordRole,
    build_calibration_record_candidate,
    validator_internal_assemble_accepted_artifact_set,
)
from autocut_kernel.media.types import TimeBase
from autocut_kernel.registry.authority_profiles import (
    RuntimeCalibrationCapabilityPolicy,
    RuntimeCalibrationPolicySource,
)
from autocut_kernel.registry.calibration_binding import (
    CalibrationBindingError,
    bind_profile_calibration,
    bind_runtime_calibration_capability,
)
from autocut_kernel.store.models import (
    ArtifactScope,
    CommittedArtifactMemberReference,
    PersistedCalibrationRecordAnchor,
    PersistedCommittedArtifactMember,
    PersistedRuntimeCalibrationCapability,
)

from tests.authority.test_authority_profile_sources import _hash
from tests.authority.test_local_run_context import _local_sources
from tests.authority.test_shadow_context import Sources
from tests.media.test_calibration_record_persistence import _child, _proof, _runtime_measurement


class FakeAcceptedAnchorReader:
    """Explicit unit seam: cannot prove that any database accepted these bytes."""

    def __init__(self, anchor: PersistedCalibrationRecordAnchor) -> None:
        self.anchor = anchor
        self.calls: list[tuple[object, ...]] = []

    def read_calibration_record_anchor(
        self, aggregate_reference: CommittedArtifactMemberReference,
        validation_reference: CommittedArtifactMemberReference, *,
        expected_profile_source_sha256: str, expected_registry_snapshot_sha256: str,
    ) -> PersistedCalibrationRecordAnchor:
        self.calls.append((aggregate_reference, validation_reference,
                           expected_profile_source_sha256, expected_registry_snapshot_sha256))
        return self.anchor


class FakeRuntimeCapabilityReader:
    def __init__(self, capability: PersistedRuntimeCalibrationCapability) -> None:
        self.capability = capability
        self.calls: list[tuple[object, ...]] = []

    def read_runtime_calibration_capability(self, **kwargs) -> PersistedRuntimeCalibrationCapability:
        self.calls.append(tuple(kwargs.values()))
        return self.capability


def _fixture_anchor(record: CalibrationRecordArtifactSet) -> PersistedCalibrationRecordAnchor:
    def persisted(ordinal: int) -> PersistedCommittedArtifactMember:
        member = record.members[ordinal]
        reference = CommittedArtifactMemberReference(
            UUID(int=1), UUID(int=2), ordinal,
            ArtifactScope(member.scope.namespace, member.scope.kind, member.scope.key),
            member.artifact_type, member.logical_id, member.revision, member.content_hash,
        )
        return PersistedCommittedArtifactMember(reference, member.payload_json, UUID(int=3))
    return PersistedCalibrationRecordAnchor(record, persisted(0), persisted(3))


def _fixture_record(shadow_context: LockedShadowSourceContext) -> CalibrationRecordArtifactSet:
    shadow = shadow_context.profiles.shadow
    policy, clock = shadow.timing_policies, shadow.source_clock_policy
    identity = CalibrationRecordIdentity(
        shadow.source_sha256, shadow_context.compilation.registry_sha256,
        shadow.calibration_corpus.corpus_set_sha256,
        shadow.native_timed_speech.native_port_identity_sha256, clock.clock_id, clock.time_base,
        policy.timed_speech_policy_sha256, policy.word_gap_policy_sha256,
        policy.vad_merge_policy_sha256, policy.alignment_policy_sha256, policy.acceptance_policy_sha256,
    )
    evidence = tuple(CalibrationEvidenceMember(
        index, member.corpus_member_reference_sha256, member.expected_anchor_reference_sha256,
        _hash(f"fixture-raw-{index}"), _hash(f"fixture-projection-{index}"),
    ) for index, member in enumerate(shadow.calibration_corpus.members))
    children = []
    for native in shadow.native_timed_speech.producers:
        fields = dict(native.common_mapping())
        role = CalibrationRecordRole(fields.pop("producer_kind"))
        producer = CalibrationRecordProducerIdentity(role=role, **fields)
        children.append(replace(_child(role, identity=identity, producer=producer), evidence_members=evidence))
    candidate = build_calibration_record_candidate(
        profile_version=shadow.profile_version, identity=identity,
        measurement_manifest_sha256=_hash("fixture-manifest"),
        measurement_results_sha256=_hash("fixture-results"), asr=children[0], vad=children[1],
    )
    return validator_internal_assemble_accepted_artifact_set(_proof(candidate))


def _project(run: dict[str, Any], anchor: PersistedCalibrationRecordAnchor) -> None:
    run["calibration"] = {
        "record_ref": anchor.aggregate.reference.to_mapping(),
        "validation_receipt_ref": anchor.validation.reference.to_mapping(),
        "asr_producer_record_sha256": anchor.record.asr.content_hash,
        "vad_producer_record_sha256": anchor.record.vad.content_hash,
        "asr_timing_error_bound_tick": anchor.record.asr.accepted_bound_tick,
        "vad_timing_error_bound_tick": anchor.record.vad.accepted_bound_tick,
    }
    for index, (child, requirement) in enumerate(zip(
        (anchor.record.asr, anchor.record.vad), ("transcript_requirement", "vad_requirement"), strict=True,
    )):
        run["native_timed_speech"]["producers"][index].update({
            "producer_record_sha256": child.content_hash,
            "timing_error_bound_tick": child.accepted_bound_tick,
        })
        run["timed_speech_registry_entry"][requirement]["calibration_record_sha256"] = child.content_hash


@pytest.fixture(scope="module")
def accepted_context(tmp_path_factory: pytest.TempPathFactory):
    captured: list[PersistedCalibrationRecordAnchor] = []

    def customize(run: dict[str, Any], sources: Sources) -> None:
        anchor = _fixture_anchor(_fixture_record(build_locked_shadow_context(**sources.options)))
        captured.append(anchor)
        _project(run, anchor)

    fixture = _local_sources(tmp_path_factory.mktemp("local-calibration"), customize_run=customize)
    return build_locked_local_run_context(**fixture.options), captured[0]


def _with_record(context: LockedLocalRunSourceContext, record: CalibrationRecordArtifactSet):
    """Pure comparison negative: revise refs to another self-consistent fake set."""
    anchor = _fixture_anchor(record)
    calibration = replace(
        context.local_run.calibration,
        record_ref=anchor.aggregate.reference, validation_receipt_ref=anchor.validation.reference,
        asr_producer_record_sha256=record.asr.content_hash, vad_producer_record_sha256=record.vad.content_hash,
        asr_timing_error_bound_tick=record.asr.accepted_bound_tick,
        vad_timing_error_bound_tick=record.vad.accepted_bound_tick,
    )
    return replace(context, local_run=replace(context.local_run, calibration=calibration)), anchor


def _replace_record(record: CalibrationRecordArtifactSet, *, identity=None, asr=None, vad=None):
    identity = identity or record.aggregate.identity
    candidate = build_calibration_record_candidate(
        profile_version="1", identity=identity,
        measurement_manifest_sha256=record.aggregate.measurement_manifest_sha256,
        measurement_results_sha256=record.aggregate.measurement_results_sha256,
        asr=asr or replace(record.asr, identity=identity), vad=vad or replace(record.vad, identity=identity),
    )
    return validator_internal_assemble_accepted_artifact_set(_proof(candidate))


def test_two_git_chains_bind_exact_accepted_refs_using_old_not_new_registry(accepted_context) -> None:
    context, anchor = accepted_context
    reader = FakeAcceptedAnchorReader(anchor)
    assert bind_local_run_calibration(context=context, store=reader) is anchor
    assert context.compilation.registry_sha256 != context.predecessor.compilation.registry_sha256
    assert reader.calls == [(context.local_run.calibration.record_ref,
                             context.local_run.calibration.validation_receipt_ref,
                             context.predecessor.profiles.shadow.source_sha256,
                             context.predecessor.compilation.registry_sha256)]
    assert (anchor.aggregate.reference.member_ordinal, anchor.validation.reference.member_ordinal) == (0, 3)
    assert not hasattr(anchor, "bootstrap_request") and not hasattr(anchor, "snapshot")


@pytest.mark.parametrize("field", list(CalibrationRecordIdentity.__dataclass_fields__))
def test_every_accepted_identity_field_must_equal_locked_predecessor(accepted_context, field: str) -> None:
    context, anchor = accepted_context
    value = TimeBase(1, 2000) if field == "source_time_base" else (
        "other-clock" if field == "source_clock_id" else _hash("different-identity")
    )
    record = _replace_record(anchor.record, identity=replace(anchor.record.aggregate.identity, **{field: value}))
    context, changed = _with_record(context, record)
    with pytest.raises(GateViolation, match="identity differs"):
        bind_local_run_calibration(context=context, store=FakeAcceptedAnchorReader(changed))


@pytest.mark.parametrize("role", ["asr", "vad"])
@pytest.mark.parametrize("field", ["producer_id", "producer_version", "generation_policy_sha256", "detector_sha256",
                                   "calibration_policy_sha256", "model_revision", "model_sha256", "service_sha256"])
def test_producer_identity_cannot_be_substituted_by_a_self_consistent_record(accepted_context, role, field) -> None:
    context, anchor = accepted_context
    child = getattr(anchor.record, role)
    value = _hash("foreign-producer") if field.endswith("sha256") else "foreign-producer"
    child = replace(child, producer_identity=replace(child.producer_identity, **{field: value}))
    context, changed = _with_record(context, _replace_record(anchor.record, **{role: child}))
    with pytest.raises(GateViolation, match="producer, bound or corpus"):
        bind_local_run_calibration(context=context, store=FakeAcceptedAnchorReader(changed))


@pytest.mark.parametrize("field", ["corpus_member_reference_sha256", "expected_anchor_reference_sha256"])
def test_exact_ordered_corpus_references_are_not_just_a_claimed_set_hash(accepted_context, field) -> None:
    context, anchor = accepted_context
    evidence = list(anchor.record.asr.evidence_members)
    evidence[0] = replace(evidence[0], **{field: _hash("foreign-corpus")})
    record = _replace_record(anchor.record, asr=replace(anchor.record.asr, evidence_members=tuple(evidence)),
                             vad=replace(anchor.record.vad, evidence_members=tuple(evidence)))
    context, changed = _with_record(context, record)
    with pytest.raises(GateViolation, match="producer, bound or corpus"):
        bind_local_run_calibration(context=context, store=FakeAcceptedAnchorReader(changed))


@pytest.mark.parametrize("field", ["asr_producer_record_sha256", "vad_producer_record_sha256",
                                   "asr_timing_error_bound_tick", "vad_timing_error_bound_tick"])
def test_local_run_child_hashes_and_bounds_are_measured_not_guessed(accepted_context, field) -> None:
    context, anchor = accepted_context
    value = _hash("wrong-child") if field.endswith("sha256") else 99
    context = replace(context, local_run=replace(context.local_run,
        calibration=replace(context.local_run.calibration, **{field: value})))
    with pytest.raises(GateViolation, match="producer, bound or corpus"):
        bind_local_run_calibration(context=context, store=FakeAcceptedAnchorReader(anchor))


@pytest.mark.parametrize("field", ["aggregate", "validation"])
def test_reader_cannot_return_another_receipt_or_artifact_set(accepted_context, field) -> None:
    context, anchor = accepted_context
    member = getattr(anchor, field)
    foreign = replace(member, reference=replace(member.reference, receipt_id=UUID(int=9)))
    with pytest.raises(GateViolation, match="exact local-run references"):
        bind_local_run_calibration(context=context, store=FakeAcceptedAnchorReader(replace(anchor, **{field: foreign})))


def test_absent_or_denied_store_anchor_is_not_retried_or_replaced(accepted_context) -> None:
    context, _ = accepted_context
    error = LookupError("fixture unavailable accepted anchor")

    class UnavailableReader:
        calls = 0

        def read_calibration_record_anchor(self, *args, **kwargs):
            self.calls += 1
            raise error

    reader = UnavailableReader()
    with pytest.raises(LookupError) as caught:
        bind_local_run_calibration(context=context, store=reader)
    assert caught.value is error and reader.calls == 1


def test_kernel_binding_matches_build_adapter_without_tools_dependency(accepted_context) -> None:
    context, anchor = accepted_context
    reader = FakeAcceptedAnchorReader(anchor)
    assert bind_profile_calibration(
        local_run=context.local_run,
        shadow=context.predecessor.profiles.shadow,
        predecessor_registry_sha256=context.predecessor.compilation.registry_sha256,
        store=reader,
    ) is anchor
    assert len(reader.calls) == 1


@pytest.mark.parametrize("identity", [None, "", "sha256:" + "0" * 64, "sha256:bad"])
def test_kernel_binding_rejects_invalid_registry_before_store_read(accepted_context, identity) -> None:
    context, anchor = accepted_context
    reader = FakeAcceptedAnchorReader(anchor)
    with pytest.raises(CalibrationBindingError):
        bind_profile_calibration(
            local_run=context.local_run, shadow=context.predecessor.profiles.shadow,
            predecessor_registry_sha256=identity, store=reader,
        )
    assert reader.calls == []


@pytest.mark.parametrize("field", ["local_run", "shadow"])
def test_kernel_binding_requires_typed_sources_before_store_read(accepted_context, field) -> None:
    context, anchor = accepted_context
    reader = FakeAcceptedAnchorReader(anchor)
    arguments = {
        "local_run": context.local_run, "shadow": context.predecessor.profiles.shadow,
        "predecessor_registry_sha256": context.predecessor.compilation.registry_sha256,
        "store": reader,
    }
    arguments[field] = {}
    with pytest.raises(CalibrationBindingError):
        bind_profile_calibration(**arguments)
    assert reader.calls == []


def test_v2_runtime_binding_requires_static_device_policy_and_never_falls_back_to_v1(
    accepted_context,
) -> None:
    context, anchor = accepted_context
    identity = _runtime_measurement()
    policy = RuntimeCalibrationPolicySource(
        context.predecessor.profiles.shadow.source_sha256,
        context.predecessor.compilation.registry_sha256,
        _hash("runtime-policy-source"),
        _hash("runtime-policy-canonical"),
        (RuntimeCalibrationCapabilityPolicy("pc_cuda", "cuda"),),
    )
    capability = PersistedRuntimeCalibrationCapability(identity, anchor)
    reader = FakeRuntimeCapabilityReader(capability)
    assert bind_runtime_calibration_capability(
        policy=policy, measurement_identity=identity, store=reader
    ) == capability
    assert len(reader.calls) == 1
    with pytest.raises(CalibrationBindingError, match="not allowed"):
        bind_runtime_calibration_capability(
            policy=policy,
            measurement_identity=_runtime_measurement(capability_id="mac_cpu"),
            store=reader,
        )
    assert len(reader.calls) == 1
