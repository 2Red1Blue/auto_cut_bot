"""Pure lifecycle values over synthetic Source/VLM; no accepted local profile."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from uuid import UUID

import pytest
from autocut_kernel.media.local_audio_window import LocalAudioWindowSpec
from autocut_kernel.media.local_speech_window import LocalSpeechWindowPolicy
from autocut_kernel.media.timed_evidence import plan_candidate_evidence_window
from autocut_kernel.media.types import TickRange, canonical_sha256
from autocut_kernel.pipeline.local_speech_window_contract import (
    LocalSpeechWindowBusyHandle,
    LocalSpeechWindowLifecycle,
    LocalSpeechWindowReadLimits,
    LocalSpeechWindowSuccessHandle,
    PrepareLocalSpeechWindowChildRequest,
)
from autocut_kernel.pipeline.physical_media_contract import (
    PreparePhysicalMediaEvidenceRequest,
    ResolvedPreparePhysicalMediaEvidenceRequest,
)
from autocut_kernel.pipeline.prepare_physical_media_evidence_command import physical_member_layout
from autocut_kernel.pipeline.prepare_timed_media_evidence_command import (
    resolve_committed_timed_media_request,
)
from autocut_kernel.registry.timed_speech import (
    TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE,
    TIMED_SPEECH_PROFILE_REGISTRY_SCOPE,
)
from autocut_kernel.store.errors import StoreValidationError
from autocut_kernel.store.models import CommandOutcome, CommittedArtifactMemberReference

from tests.media.test_resolved_source_audio_facts import source_audio_facts_case

H, OTHER = "sha256:" + "a" * 64, "sha256:" + "b" * 64


def local_speech_contract_case():
    """Synthetic handles/profile expectations are not Store or calibration proof."""
    store, source, facts = source_audio_facts_case()
    resolved_source = resolve_committed_timed_media_request(store, source)
    parent = ResolvedPreparePhysicalMediaEvidenceRequest(
        PreparePhysicalMediaEvidenceRequest(source, H, 100000, 100000), resolved_source,
    )
    handle = LocalSpeechWindowSuccessHandle(*(UUID(int=i) for i in (1, 2, 3, 4)), parent.request_hash)
    refs = tuple(CommittedArtifactMemberReference(handle.receipt_id, handle.artifact_set_id, ordinal,
        source.artifact_scope, kind, logical_id, 1, H)
        for ordinal, (kind, logical_id) in enumerate(physical_member_layout(parent)))
    profile = CommittedArtifactMemberReference(UUID(int=5), UUID(int=6), 0,
        TIMED_SPEECH_PROFILE_REGISTRY_SCOPE, TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE,
        "timed-speech/synthetic-local-expectation/1", 1, H)
    plan = plan_candidate_evidence_window(source.semantic_pack.candidate_hypotheses[0], source.semantic_pack,
                                         source.window_manifest, source.frame_pts_index, source.adaptive_policy)
    life = LocalSpeechWindowLifecycle(parent, handle, refs, plan.vlm_candidate_sha256, facts, H, profile,
        H, OTHER, H, LocalSpeechWindowPolicy(H, "synthetic-asr", H, "synthetic-vad", OTHER, 700, 350),
        H, 3, 100, 1000, 100000, 1000000, LocalSpeechWindowReadLimits(10000, 20000, 100000, 500000))
    # This is a shape-valid extraction, not a substitute for the future resolver's map replay.
    extraction = LocalAudioWindowSpec(facts.source_id, facts.source_sha256, facts.stream_index,
        facts.clock_id, facts.time_base, TickRange(facts.origin_tick, facts.end_tick),
        TickRange(facts.origin_tick, facts.end_tick), facts.sample_rate, facts.channels,
        facts.audio_sample_boundary_set_sha256, life.decoder_identity_sha256,
        source.materialization_limits.effective_max_source_bytes, life.max_decode_frames,
        life.max_frame_bytes, life.max_pcm_bytes)
    return PrepareLocalSpeechWindowChildRequest(life, plan.final_window, extraction, 1, None, None)


@pytest.fixture(scope="module")
def child():
    return local_speech_contract_case()


def test_closed_roundtrip_hash_oracle_and_acyclic_wire(child):
    mapping = child.to_mapping()
    assert PrepareLocalSpeechWindowChildRequest.from_mapping(mapping, parent=child.lifecycle.parent) == child
    binding = {key: value for key, value in mapping.items() if key != "wire_request"}
    oracle = "sha256:" + hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    assert child.binding_sha256 == oracle
    assert child.wire_request.binding_sha256 == oracle
    assert child.request_hash == child.canonical_hash == canonical_sha256(mapping)
    assert child.request_hash != child.binding_sha256 != child.wire_request.canonical_hash
    assert child.idempotency_key == f"local-speech:{child.lifecycle.canonical_hash}:expansion:0:attempt:1"
    assert child.wire_request.max_response_bytes == child.lifecycle.read_limits.max_raw_response_bytes
    for forbidden in ("accepted", "retry_authorized", "admission", "source_path", "idempotency_key"):
        assert forbidden not in mapping
    assert len(child.lifecycle.physical_members) == 3


def test_frozen_values_and_fresh_deep_mappings(child):
    with pytest.raises(FrozenInstanceError):
        child.attempt_ordinal = 2
    raw = child.to_mapping()
    raw["lifecycle"]["parent"]["source"]["episode_index"] = 999
    raw["lifecycle"]["audio_stream_facts"]["sample_rate"] = 1
    raw["wire_request"]["policy"]["asr_producer_id"] = "other"
    assert child.to_mapping() != raw
    assert child.lifecycle.audio_stream_facts.sample_rate == 96000


@pytest.mark.parametrize("path", [(), ("lifecycle",), ("lifecycle", "read_limits"),
    ("lifecycle", "physical_predecessor"), ("lifecycle", "physical_members", 0),
    ("lifecycle", "expected_profile_reference"), ("lifecycle", "policy"),
    ("lifecycle", "audio_stream_facts"), ("window",), ("extraction",), ("wire_request",)])
@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_all_nested_objects_are_closed(child, path, mutation):
    raw = child.to_mapping()
    node = raw
    for key in path:
        node = node[key]
    if mutation == "extra":
        node["producer_pass"] = True
    else:
        node.pop(next(iter(node)))
    with pytest.raises((ValueError, TypeError, StoreValidationError)):
        PrepareLocalSpeechWindowChildRequest.from_mapping(raw, parent=child.lifecycle.parent)


@pytest.mark.parametrize("field", ["max_attempts", "max_decode_frames", "max_frame_bytes", "max_pcm_bytes"])
@pytest.mark.parametrize("value", [True, 1.0, "1", 0, -1, 2**53])
def test_frozen_limits_are_strict_direct_and_wire(child, field, value):
    with pytest.raises(ValueError):
        replace(child.lifecycle, **{field: value})
    raw = child.lifecycle.to_mapping()
    raw[field] = value
    with pytest.raises(ValueError):
        LocalSpeechWindowLifecycle.from_mapping(raw, parent=child.lifecycle.parent)


@pytest.mark.parametrize("field", ["max_raw_response_bytes", "max_projection_bytes", "max_metadata_bytes", "max_total_bytes"])
@pytest.mark.parametrize("value", [True, 1.0, "1", 0, -1, 2**53])
def test_read_limits_are_explicit_independent_budgets(child, field, value):
    limits = child.lifecycle.read_limits
    with pytest.raises(ValueError):
        replace(limits, **{field: value})
    raw = limits.to_mapping()
    raw[field] = value
    with pytest.raises(ValueError):
        LocalSpeechWindowReadLimits.from_mapping(raw)


@pytest.mark.parametrize("field,value", [("max_attempts", 4), ("max_decode_frames", 1001),
    ("max_frame_bytes", 100001), ("max_pcm_bytes", 1000001), ("max_outward_padding_audio_ticks", 101),
    ("expected_registry_sha256", OTHER), ("expected_native_port_identity_sha256", OTHER),
    ("decoder_identity_sha256", OTHER)])
def test_every_frozen_policy_change_changes_full_lifecycle_identity(child, field, value):
    changed = replace(child.lifecycle, **{field: value})
    assert changed.canonical_hash != child.lifecycle.canonical_hash
    assert changed.to_mapping()[field] == value


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("field,value", [("receipt_id", UUID(int=90)), ("artifact_set_id", UUID(int=91)),
    ("member_ordinal", 9), ("logical_id", "truncated"), ("artifact_type", "other"), ("revision", 2)])
def test_physical_members_exact_identity_and_order(child, index, field, value):
    refs = list(child.lifecycle.physical_members)
    refs[index] = replace(refs[index], **{field: value})
    with pytest.raises(ValueError):
        replace(child.lifecycle, physical_members=tuple(refs))


def test_physical_members_count_tuple_and_scope_and_request_binding(child):
    life = child.lifecycle
    for refs in (life.physical_members[:2], tuple(reversed(life.physical_members)), list(life.physical_members)):
        with pytest.raises(ValueError):
            replace(life, physical_members=refs)
    with pytest.raises(ValueError):
        replace(life, physical_predecessor=replace(life.physical_predecessor, request_hash=OTHER))
    with pytest.raises(ValueError):
        replace(life, expected_profile_reference=life.physical_members[0])


def test_one_record_can_carry_independently_checked_asr_and_vad_roles(child):
    life = replace(child.lifecycle, expected_vad_calibration_sha256=child.lifecycle.expected_asr_calibration_sha256)
    assert LocalSpeechWindowLifecycle.from_mapping(life.to_mapping(), parent=life.parent) == life
    assert life.policy.asr_producer_id != life.policy.vad_producer_id


def test_local_requires_nonempty_exact_resolved_audio_facts(child):
    life = child.lifecycle
    without = replace(life.parent, source=replace(life.parent.source, audio_stream_facts=None))
    assert without.canonical_payload() == life.parent.canonical_payload()
    with pytest.raises(ValueError, match="measured audio facts"):
        replace(life, parent=without)
    with pytest.raises(ValueError):
        replace(life, audio_stream_facts=None)
    with pytest.raises(ValueError):
        replace(life, vlm_candidate_sha256=OTHER)


def test_native_layout_changes_new_identity_without_changing_old_parent_payload(child):
    life = child.lifecycle
    metadata = replace(life.audio_stream_facts.selected_audio_metadata, channels=6)
    facts = replace(life.audio_stream_facts, channels=6, selected_audio_metadata=metadata,
                    selected_audio_metadata_sha256=metadata.canonical_hash)
    parent = replace(life.parent, source=replace(life.parent.source, audio_stream_facts=facts))
    changed = replace(life, parent=parent, audio_stream_facts=facts)
    assert parent.canonical_payload() == life.parent.canonical_payload()
    assert changed.canonical_hash != life.canonical_hash


def test_retry_and_expansion_are_separate_contiguous_shapes_not_permissions(child):
    busy = LocalSpeechWindowBusyHandle(child.lifecycle.physical_predecessor.job_id, UUID(int=10), UUID(int=11), child.request_hash)
    retry = replace(child, attempt_ordinal=2, previous_busy=busy)
    assert retry.idempotency_key.endswith(":expansion:0:attempt:2")
    assert retry.binding_sha256 != child.binding_sha256
    assert PrepareLocalSpeechWindowChildRequest.from_mapping(retry.to_mapping(), parent=child.lifecycle.parent) == retry
    expansion = replace(child, window=replace(child.window, expansion_ordinal=1),
                        previous_expansion=child.lifecycle.physical_predecessor)
    assert expansion.idempotency_key.endswith(":expansion:1:attempt:1")
    # Handle shape alone cannot prove the predecessor command or actual expansion.
    assert not hasattr(expansion, "admitted")
    for changes in ({"attempt_ordinal": 0}, {"attempt_ordinal": True}, {"attempt_ordinal": 2},
                    {"attempt_ordinal": 4, "previous_busy": busy}, {"previous_busy": busy},
                    {"previous_expansion": child.lifecycle.physical_predecessor},
                    {"window": replace(child.window, expansion_ordinal=1)},
                    {"attempt_ordinal": 2, "previous_busy": replace(busy, job_id=UUID(int=99))}):
        with pytest.raises(ValueError):
            replace(child, **changes)


@pytest.mark.parametrize("field,value", [("max_source_bytes", 1023), ("max_source_bytes", 1025),
    ("max_decode_frames", 999), ("max_frame_bytes", 99999), ("max_pcm_bytes", 999999),
    ("decoder_identity_sha256", OTHER), ("sample_rate", 48000), ("channels", 1),
    ("audio_stream_index", 0), ("audio_boundary_set_sha256", OTHER)])
def test_extraction_must_equal_frozen_native_facts_and_source_cap(child, field, value):
    with pytest.raises(ValueError):
        replace(child, extraction=replace(child.extraction, **{field: value}))


@pytest.mark.parametrize("mutation", ["binding", "response_limit", "parent_bool", "adaptive_float", "version"])
def test_recomputed_wire_and_parent_comparison_never_uses_bool_int_equality(child, mutation):
    raw = child.to_mapping()
    if mutation == "binding":
        raw["wire_request"]["binding_sha256"] = OTHER
    elif mutation == "response_limit":
        raw["wire_request"]["max_response_bytes"] += 1
    elif mutation == "parent_bool":
        raw["lifecycle"]["parent"]["source"]["artifact_revision"] = True
    elif mutation == "adaptive_float":
        raw["lifecycle"]["adaptive_policy"]["expansion_step_pts"] = 25.0
    else:
        raw["schema_version"] = "old"
    with pytest.raises(ValueError):
        PrepareLocalSpeechWindowChildRequest.from_mapping(raw, parent=child.lifecycle.parent)


def test_handle_strict_roundtrip_and_succeeded_outcome_projection(child):
    handle = child.lifecycle.physical_predecessor
    assert LocalSpeechWindowSuccessHandle.from_mapping(handle.to_mapping()) == handle
    outcome = CommandOutcome(handle.command_slot_id, "succeeded", receipt_id=handle.receipt_id,
                             artifact_set_id=handle.artifact_set_id, job_id=handle.job_id)
    assert LocalSpeechWindowSuccessHandle.from_outcome(outcome, request_hash=handle.request_hash) == handle
    for field, value in (("state", "running"), ("job_id", None), ("artifact_set_id", None), ("failure_code", "bad")):
        with pytest.raises(ValueError):
            LocalSpeechWindowSuccessHandle.from_outcome(replace(outcome, **{field: value}), request_hash=handle.request_hash)
    busy = LocalSpeechWindowBusyHandle(handle.job_id, handle.command_slot_id, handle.receipt_id, handle.request_hash)
    assert LocalSpeechWindowBusyHandle.from_mapping(busy.to_mapping()) == busy
    for value in (str(handle.job_id), True, None):
        with pytest.raises(ValueError):
            replace(handle, job_id=value)
        with pytest.raises(ValueError):
            replace(busy, job_id=value)
