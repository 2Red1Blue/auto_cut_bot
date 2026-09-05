"""Synthetic derived DTOs exercise pure consumers, not durable acceptance."""

import json
from dataclasses import replace
from uuid import UUID

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.semantic_chain.authority import _committed_input_binding
from autocut_kernel.semantic_chain.candidate_projection import decode_candidate_source_context
from autocut_kernel.semantic_chain.derived_input_binding import bind_derived_input
from autocut_kernel.semantic_chain.editorial_context import _raw_packs, _raw_pool, _request
from autocut_kernel.semantic_chain.editorial_context_models import ExactContextMember
from autocut_kernel.semantic_chain.member_refs import SemanticObjectRef
from autocut_kernel.semantic_chain.stage1_draft import _member_binding, stage1_draft_prompt_inputs
from autocut_kernel.store.models import (
    ArtifactMember,
    CommittedArtifactMemberReference,
    PersistedReprocessedVlmChild,
    PersistedVlmSemanticPackV4,
    canonical_payload_hash,
)
from autocut_kernel.vlm.enum_normalization import normalize_vlm_enum_sets
from autocut_kernel.vlm.semantic_pack_v4 import VlmSemanticPackV4
from autocut_kernel.vlm.semantic_parser_v4 import parse_vlm_response_v4

from tests.semantic_chain.test_candidate_projection import POLICY, _inputs
from tests.vlm.test_parser import _context
from tests.vlm.test_semantic_pack_v4 import _wire


def _derived_inputs(*, version=2):
    base = _inputs(command_ready=True)
    original, source = base.inputs[0], base.source_manifest
    episode = decode_candidate_source_context(base).episodes[0]
    manifest, manifest_set = episode.manifest, episode.manifest_set
    identity = replace(original.request_identity, prompt_version="semantic-pack-v4-video")
    policy = _context()[2]
    wire = _wire()
    timeline = manifest.timeline_map
    duration_ms = timeline.proxy_range.duration_pts * timeline.proxy_time_base.numerator * 1000 // timeline.proxy_time_base.denominator
    for collection in (wire["entities"], wire["facts"], wire["events"], wire["candidate_hypotheses"], wire["continuity"]["temporal_segments"]):
        for item in collection:
            item["support"]["interval_ms"] = {"start_ms": 0, "end_ms": duration_ms, "uncertainty_ms": 0}
    raw = canonical_json_bytes(wire)
    normalized = normalize_vlm_enum_sets(raw, policy)
    pack = parse_vlm_response_v4(normalized.normalized_response, manifest=manifest, manifest_set=manifest_set,
                                 request_identity=identity, policy=policy)
    pack = replace(pack, raw_response_sha256=normalized.raw_response_sha256)
    parent = original.semantic_pack.source_child
    request = {
        "strategy_version": f"reprocess-vlm-evidence-v{version}", "target_parser_strategy": "normalized-semantic-pack-v4-v1",
        "target_parser_contract_sha256": "sha256:" + "a" * 64,
        "job": {"job_key": source.source_job.job_key, "profile": source.source_job.profile},
        "parent_command_slot_id": str(parent.command_slot_id), "parent_receipt_id": str(parent.receipt_id),
        "parent_attempt_id": str(parent.attempt_id), "parent_request_hash": parent.request_hash,
        "parent_request_payload_sha256": identity.request_payload_sha256,
        "parent_raw_response_sha256": pack.raw_response_sha256, "source_artifact_set_id": str(source.artifact_set_id),
        "episode_index": 0, "parent_artifact_revision": 1, "provider_call_budget": 0,
    }
    if version == 2:
        request["projection_version"] = 2
    request_hash = canonical_sha256(request)
    record = {
        "schema_version": f"reprocessed-vlm-evidence-v{version}", "request": request,
        "parent_parser_strategy": "strict-semantic-pack-v4", "parent_parser_contract_sha256": "sha256:" + "b" * 64,
        "parent_provider_request_id": "synthetic-parent", "source_manifest_sha256": source.reference.content_hash,
        "source_provenance_sha256": source.canonical_hash, "window_manifest_sha256": identity.window_manifest_sha256,
        "window_manifest_set_sha256": identity.window_manifest_set_sha256,
        "normalization": normalized.to_mapping(), "semantic_pack": pack.to_mapping(),
    }
    if version == 2:
        record.update(request_identity=identity.to_mapping(), parse_policy=policy.to_mapping(),
                      proxy_blob=manifest.proxy_blob_ref.to_mapping())
    payload = canonical_json_bytes(record).decode()
    ref = CommittedArtifactMemberReference(UUID(int=501), UUID(int=502), 0, source.reference.scope,
                                           "reprocessed_vlm_evidence", "reprocessed_vlm_" + request_hash[7:],
                                           1, canonical_payload_hash(payload))
    child = PersistedReprocessedVlmChild(ref, payload, source.source_job, source.job_id, UUID(int=503), request_hash,
                                        parent.attempt_id, parent.receipt_id, identity, parent.request_payload,
                                        source.reference.content_hash, source.canonical_hash, 0)
    pack_json = canonical_json_bytes(pack.to_mapping()).decode()
    persisted = PersistedVlmSemanticPackV4(replace(original.semantic_pack.reference,
        logical_id="reprocessed_semantic_pack_" + request_hash[7:], content_hash=canonical_payload_hash(pack_json)),
        pack_json, pack, child)
    item = replace(original, request_identity=identity, semantic_pack=persisted, response_record=ref,
                   raw_response=replace(original.raw_response, content_hash=pack.raw_response_sha256, byte_length=len(raw)))
    return replace(base, inputs=(item,), vlm_aggregate_policy=child.request_policy,
                   vlm_batch_strategy_version="vlm-semantic-pack-set-derived-v1")


def _exact(reference, payload):
    return ExactContextMember.from_artifact_member(ArtifactMember(reference.artifact_type, reference.logical_id,
        reference.revision, reference.scope, reference.content_hash, payload))


def _raw_context_pool(inputs):
    item, source = inputs.inputs[0], inputs.source_manifest
    source_member = _exact(source.reference, source.payload_json)
    raw = (source_member, _exact(item.semantic_pack.source_child.reference, item.semantic_pack.source_child.payload_json),
           _exact(item.semantic_pack.reference, item.semantic_pack.payload_json))
    # The raw-pool seam excludes the 13 Stage1/2 slots; these placeholders do
    # not pretend to be admitted Stage members or a complete context batch.
    return (*raw, *((source_member,) * 13))


@pytest.mark.parametrize("version", [1, 2])
def test_derived_stage1_prompt_and_authority_preserve_real_owner_not_generation(version):
    inputs = _derived_inputs(version=version)
    item = inputs.inputs[0]
    binding = _member_binding(item, inputs)
    assert "generation_child" not in binding and "generation_owner" not in binding and "request_record" not in binding
    assert binding["derivation_child"]["reference"] == item.response_record.to_mapping()
    assert binding["semantic_pack"]["member_ordinal"] == 1
    assert stage1_draft_prompt_inputs(inputs, policy=POLICY)["input_binding_sha256"]
    assert _committed_input_binding(inputs) == _committed_input_binding(inputs)
    bad = replace(item, raw_response=replace(item.raw_response, content_hash="sha256:" + "f" * 64))
    with pytest.raises(ValueError, match="provenance"):
        bind_derived_input(bad, inputs)


def test_stage3_decodes_full_derived_v4_and_retains_exact_fact_and_candidate_owners():
    inputs = _derived_inputs()
    pool = _raw_context_pool(inputs)
    packs = _raw_packs(pool)
    assert type(packs[0]) is VlmSemanticPackV4
    assert packs[0].to_mapping() == inputs.inputs[0].semantic_pack.semantic_pack.to_mapping()
    refs = _raw_pool(pool, packs, inputs.source_grant.canonical_hash)
    assert SemanticObjectRef(pool[2].member_ref, "vlm_fact", packs[0].facts[0].fact_id) in refs
    assert SemanticObjectRef(pool[2].member_ref, "vlm_candidate", packs[0].candidate_hypotheses[0].candidate_id) in refs
    assert pool[1].member_ref.artifact_type == "reprocessed_vlm_evidence"


def test_stage3_v1_projection_is_explicitly_unavailable_not_guessed():
    with pytest.raises(ValueError, match="reprocess locally to v2"):
        _raw_packs(_raw_context_pool(_derived_inputs(version=1)))


def test_unicode_job_uses_request_contract_hash_for_both_derived_logical_ids():
    """Only a pure content-join regression; this is not a Store acceptance."""
    inputs = _derived_inputs()
    pool = list(_raw_context_pool(inputs))
    record = json.loads(pool[1].payload_json)
    record["request"]["job"]["job_key"] = "派生证据结构测试"
    request_hash = canonical_sha256(record["request"])
    other_hash = canonical_json_hash(record["request"])
    assert request_hash != other_hash
    payload = canonical_json_bytes(record).decode()
    pool[1] = _exact(replace(pool[1].member_ref, logical_id="reprocessed_vlm_" + request_hash[7:],
                             content_hash=canonical_payload_hash(payload)), payload)
    pool[2] = replace(pool[2], member_ref=replace(pool[2].member_ref,
                                                logical_id="reprocessed_semantic_pack_" + request_hash[7:]))
    packs = _raw_packs(tuple(pool))
    refs = _raw_pool(tuple(pool), packs, inputs.source_grant.canonical_hash)
    assert SemanticObjectRef(pool[2].member_ref, "vlm_fact", packs[0].facts[0].fact_id) in refs

    bad_record = replace(pool[1], member_ref=replace(pool[1].member_ref,
                                                    logical_id="reprocessed_vlm_" + other_hash[7:]))
    with pytest.raises(ValueError, match="exact request identity"):
        _request(bad_record)
    pool[2] = replace(pool[2], member_ref=replace(pool[2].member_ref,
                                                logical_id="reprocessed_semantic_pack_" + other_hash[7:]))
    with pytest.raises(ValueError, match="pair does not close"):
        _raw_pool(tuple(pool), packs, inputs.source_grant.canonical_hash)


@pytest.mark.parametrize("field", ["request_identity", "parse_policy", "proxy_blob"])
def test_stage3_rejects_rehashed_missing_or_substituted_derived_projection(field):
    pool = _raw_context_pool(_derived_inputs())
    record = json.loads(pool[1].payload_json)
    del record[field]
    payload = canonical_json_bytes(record).decode()
    member = _exact(replace(pool[1].member_ref, content_hash=canonical_payload_hash(payload)), payload)
    with pytest.raises(ValueError):
        _request(member)
