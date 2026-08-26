"""Eight-member Stage 1 structural codec tests, without Store admission."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.semantic_chain.coverage_admission import CoverageAdmission
from autocut_kernel.semantic_chain.dependency_projection import DependencyProjectionPolicy
from autocut_kernel.semantic_chain.dependency_proof import build_dependency_proof
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity
from autocut_kernel.semantic_chain.stage1_checks import Stage1Check
from autocut_kernel.semantic_chain.stage1_evaluation import evaluate_stage1_business_members
from autocut_kernel.semantic_chain.stage1_members import decode_coverage_members
from autocut_kernel.semantic_chain.stage1_result import STAGE1_MEMBER_TYPES, decode_stage1_members
from autocut_kernel.store import ArtifactMember
from autocut_kernel.store.models import canonical_payload_hash

from tests.semantic_chain.test_coverage_analysis import _clean_inputs
from tests.semantic_chain.test_coverage_compiler import COVERAGE, _compile
from tests.semantic_chain.test_stage1_draft import POLICY, _draft


def _member(artifact_type: str, scope, payload: object) -> ArtifactMember:
    raw = canonical_json_bytes(payload).decode("utf-8")
    return ArtifactMember(artifact_type, artifact_type, 1, scope, canonical_payload_hash(raw), raw)


def _case(*, gate_indeterminate: bool = False):
    inputs = _clean_inputs()
    draft = _draft(inputs)
    draft["merge_proposals"] = []
    raw = canonical_json_bytes(draft)
    compilation, _ = _compile(inputs, raw=raw)
    dependency_policy = DependencyProjectionPolicy("semantic-dependencies-v1")
    proof = build_dependency_proof(
        inputs,
        graph_member=compilation.narrative.narrative_graph,
        event_card_member=compilation.narrative.event_cards,
        ledger_member=compilation.coverage_ledger,
        policy=dependency_policy,
        revision=1,
    )
    business = (*compilation.members, proof)
    coverage = decode_coverage_members(compilation.members, scope=inputs.source_manifest.reference.scope)
    checks = evaluate_stage1_business_members(
        inputs, raw, members=business, draft_policy=POLICY, coverage_policy=COVERAGE,
        dependency_policy=dependency_policy,
    )
    input_check = Stage1Check(
        "KC-IN-001", "indeterminate", ("store_read_not_performed",)
    )
    all_checks = (*checks, input_check)
    admission = CoverageAdmission(
        "admission-v1", coverage.coverage_ledger.input_binding_sha256,
        "sha256:" + hashlib.sha256(raw).hexdigest(), coverage.coverage_ledger.draft_sha256,
        POLICY.canonical_hash, coverage.coverage_ledger.coverage_policy_sha256,
        dependency_policy.canonical_hash, "strict_global", "stage1-kc-v1",
        tuple(SemanticMemberIdentity.from_artifact_member(item) for item in business),
        all_checks,
    )
    result = (*business, _member("coverage_admission", inputs.source_manifest.reference.scope, admission.to_mapping()))
    return inputs, result


def test_real_evaluated_eight_members_decode_but_indeterminate_admission_is_not_authority():
    inputs, members = _case()
    assert tuple(member.artifact_type for member in members) == STAGE1_MEMBER_TYPES
    result = decode_stage1_members(members, scope=inputs.source_manifest.reference.scope)
    assert result.admission.validation_status == "indeterminate"
    assert result.admission.next_action == "stop"
    assert not hasattr(result, "accepted") and not hasattr(result, "authorize")


@pytest.mark.parametrize("change", ["missing", "reordered", "scope", "revision", "hash", "logical"])
def test_exact_store_ordinal_and_member_binding(change):
    inputs, members = _case()
    if change == "missing":
        members = members[:-1]
    elif change == "reordered":
        members = (members[1], members[0], *members[2:])
    else:
        fields = {
            "scope": {"scope": replace(members[-1].scope, key="foreign")},
            "revision": {"revision": 2},
            "hash": {"content_hash": "sha256:" + "f" * 64},
            "logical": {"logical_id": "foreign"},
        }
        members = (*members[:-1], replace(members[-1], **fields[change]))
    with pytest.raises(ValueError):
        decode_stage1_members(members, scope=inputs.source_manifest.reference.scope)


@pytest.mark.parametrize("nested", [False, True])
def test_strict_loader_rejects_duplicate_keys_even_when_store_hash_is_unchanged(nested):
    inputs, members = _case()
    proof = members[6]
    needle = '"dependency_closure_proof_id":' if not nested else '"source_member_ref":{"artifact_type":'
    replacement = (
        '"dependency_closure_proof_id":"shadowed","dependency_closure_proof_id":'
        if not nested else '"source_member_ref":{"artifact_type":"whole_series_source_manifest","artifact_type":'
    )
    raw = proof.payload_json.replace(needle, replacement, 1)
    assert canonical_payload_hash(raw) == proof.content_hash
    members = (*members[:6], replace(proof, payload_json=raw), members[7])
    with pytest.raises(ValueError):
        decode_stage1_members(members, scope=inputs.source_manifest.reference.scope)


@pytest.mark.parametrize("change", ["input", "draft", "raw", "coverage_policy", "dependency_policy", "admission_subject"])
def test_cross_member_bindings_cannot_be_rehashed_or_rebound(change):
    inputs, members = _case()
    admission_member = members[7]
    wire = json.loads(admission_member.payload_json)
    if change == "input":
        wire["input_binding_sha256"] = "sha256:" + "b" * 64
    elif change == "draft":
        wire["canonical_draft_sha256"] = "sha256:" + "b" * 64
    elif change == "raw":
        wire["raw_draft_sha256"] = "sha256:" + "b" * 64
    elif change == "coverage_policy":
        wire["coverage_policy_sha256"] = "sha256:" + "b" * 64
    elif change == "dependency_policy":
        wire["dependency_policy_sha256"] = "sha256:" + "b" * 64
    else:
        wire["business_members"][0]["content_hash"] = "sha256:" + "b" * 64
    # The Admission's own decoder requires its internal subject/derived values;
    # rebuild it after mutation so the Stage 1 codec, rather than a stale wire,
    # exercises the cross-member binding.
    if change == "admission_subject":
        subject = wire["business_members"]
        from autocut_kernel.contracts.compiler.canonical import canonical_json_hash
        wire["subject_hash"] = canonical_json_hash(subject)
        for rule in wire["rule_results"]:
            rule["subject_hash"] = wire["subject_hash"]
    changed = _member("coverage_admission", admission_member.scope, wire)
    members = (*members[:7], changed)
    with pytest.raises(ValueError):
        decode_stage1_members(members, scope=inputs.source_manifest.reference.scope)
