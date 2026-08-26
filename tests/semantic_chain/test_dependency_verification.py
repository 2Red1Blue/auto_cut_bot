"""Closed result-value tests; proof fixtures land with the proof owner."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from autocut_kernel.semantic_chain.dependency_graph import (
    DependencyArc,
    DependencySeed,
    analyze_dependency_graph,
)
from autocut_kernel.semantic_chain.dependency_projection import DependencyProjectionPolicy
from autocut_kernel.semantic_chain.dependency_proof import (
    DependencyClosureProof,
    build_dependency_proof,
)
from autocut_kernel.semantic_chain.dependency_verification import (
    DependencyCheckResult,
    DependencyVerificationError,
    verify_dependency_proof,
)
from autocut_kernel.semantic_chain.ledger_models import CoverageLedger
from autocut_kernel.store import ArtifactMember
from autocut_kernel.store.models import canonical_payload_hash

from tests.semantic_chain.test_coverage_analysis import _clean_inputs, _replace_pack
from tests.semantic_chain.test_coverage_compiler import _compile
from tests.semantic_chain.test_stage1_draft import _draft


def _member(proof: DependencyClosureProof, scope) -> ArtifactMember:
    payload = proof.to_mapping()
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return ArtifactMember("dependency_closure_proof", "dependency_closure_proof", 1, scope, canonical_payload_hash(raw), raw)


def _artifact(artifact_type: str, scope: object, payload: object, revision: int = 1) -> ArtifactMember:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return ArtifactMember(artifact_type, artifact_type, revision, scope, canonical_payload_hash(raw), raw)


def _proof(kind: str = "clean") -> tuple[object, object, object, ArtifactMember, DependencyProjectionPolicy]:
    inputs = _clean_inputs()
    payload = None
    if kind == "taint":
        inputs = _replace_pack(inputs, 0, lambda pack: replace(
            pack,
            entities=(replace(pack.entities[0], support=replace(pack.entities[0].support, confidence=Decimal("0.1"))),),
        ))
    elif kind == "unknown":
        payload = _draft(inputs)
    compilation, _ledger = _compile(inputs, payload=payload)
    policy = DependencyProjectionPolicy("semantic-dependencies-v1")
    proof_member = build_dependency_proof(
        inputs,
        graph_member=compilation.narrative.narrative_graph,
        event_card_member=compilation.narrative.event_cards,
        ledger_member=compilation.coverage_ledger,
        policy=policy,
        revision=1,
    )
    return inputs, compilation, compilation.coverage_ledger, proof_member, policy


def test_result_is_frozen_closed_and_canonical():
    result = DependencyCheckResult("KC-DEP-001", "fail", ("projection_missing_arc",))
    assert result.to_mapping()["violation_codes"] == ["projection_missing_arc"]
    with pytest.raises(DependencyVerificationError):
        DependencyCheckResult("KC-DEP-001", "pass", ("projection_missing_arc",))
    with pytest.raises(DependencyVerificationError):
        DependencyCheckResult("KC-DEP-001", "fail", ())
    with pytest.raises(DependencyVerificationError):
        DependencyCheckResult("KC-DEP-001", "fail", ("unknown",))


def test_valid_proof_passes_all_four_independent_rules():
    inputs, compilation, ledger, proof, policy = _proof()
    checks = verify_dependency_proof(
        inputs,
        graph_member=compilation.narrative.narrative_graph,
        event_card_member=compilation.narrative.event_cards,
        ledger_member=ledger,
        proof_member=proof,
        policy=policy,
    )
    assert [(item.rule_id, item.status, item.violation_codes) for item in checks] == [
        ("KC-DEP-001", "pass", ()),
        ("KC-DEP-002", "pass", ()),
        ("KC-DEP-003", "pass", ()),
        ("KC-ISO-001", "pass", ()),
    ]


def test_member_type_and_malformed_proof_fail_closed():
    inputs, compilation, ledger, proof, policy = _proof()
    bad = ArtifactMember(
        "not_a_proof", proof.logical_id, proof.revision, proof.scope, proof.content_hash, proof.payload_json
    )
    checks = verify_dependency_proof(
        inputs, graph_member=compilation.narrative.narrative_graph, event_card_member=compilation.narrative.event_cards,
        ledger_member=ledger, proof_member=bad, policy=policy,
    )
    assert all("proof_decode_invalid" in item.violation_codes for item in checks)


@pytest.mark.parametrize(
    "needle,replacement",
    (
        ('"dependency_closure_proof_id":', '"dependency_closure_proof_id":"ignored","dependency_closure_proof_id":'),
        ('"source_member_ref":{"artifact_type":', '"source_member_ref":{"artifact_type":"whole_series_source_manifest","artifact_type":'),
    ),
)
def test_duplicate_json_keys_are_rejected_before_proof_decode(needle: str, replacement: str):
    inputs, compilation, ledger, member, policy = _proof()
    payload = member.payload_json.replace(needle, replacement, 1)
    assert canonical_payload_hash(payload) == member.content_hash
    duplicate = ArtifactMember(
        member.artifact_type,
        member.logical_id,
        member.revision,
        member.scope,
        canonical_payload_hash(payload),
        payload,
    )
    checks = verify_dependency_proof(
        inputs,
        graph_member=compilation.narrative.narrative_graph,
        event_card_member=compilation.narrative.event_cards,
        ledger_member=ledger,
        proof_member=duplicate,
        policy=policy,
    )
    assert all("proof_decode_invalid" in item.violation_codes for item in checks)


def test_independent_arc_universe_detects_a_structurally_valid_missing_arc():
    inputs, compilation, ledger, member, policy = _proof()
    original = DependencyClosureProof.from_mapping(json.loads(member.payload_json))
    analysis = analyze_dependency_graph(
        original.analysis.node_refs,
        original.analysis.arcs[1:],
        tuple(DependencySeed(item.seed_id, item.root_refs, item.frontier_refs) for item in original.analysis.seed_closures),
    )
    tampered = replace(original, analysis=analysis)
    checks = verify_dependency_proof(
        inputs, graph_member=compilation.narrative.narrative_graph, event_card_member=compilation.narrative.event_cards,
        ledger_member=ledger, proof_member=_member(tampered, member.scope), policy=policy,
    )
    assert "projection_missing_arc" in checks[0].violation_codes


def test_policy_tamper_is_detected_without_trusting_proof_decoder():
    inputs, compilation, ledger, member, policy = _proof()
    original = DependencyClosureProof.from_mapping(json.loads(member.payload_json))
    tampered = replace(original, dependency_policy_sha256="sha256:" + "f" * 64)
    checks = verify_dependency_proof(
        inputs, graph_member=compilation.narrative.narrative_graph, event_card_member=compilation.narrative.event_cards,
        ledger_member=ledger, proof_member=_member(tampered, member.scope), policy=policy,
    )
    assert "policy_mismatch" in checks[0].violation_codes


def _checks(inputs, compilation, ledger, member, policy):
    return verify_dependency_proof(
        inputs,
        graph_member=compilation.narrative.narrative_graph,
        event_card_member=compilation.narrative.event_cards,
        ledger_member=ledger,
        proof_member=member,
        policy=policy,
    )


def _reanalysis(proof: DependencyClosureProof, *, arcs=None, seeds=None):
    source_arcs = proof.analysis.arcs if arcs is None else arcs
    source_seeds = proof.analysis.seed_closures if seeds is None else seeds
    return analyze_dependency_graph(
        proof.analysis.node_refs,
        source_arcs,
        tuple(DependencySeed(item.seed_id, item.root_refs, item.frontier_refs) for item in source_seeds),
    )


def test_bounded_seed_set_cannot_be_coherently_erased_from_proof():
    inputs, compilation, ledger, member, policy = _proof("taint")
    original = DependencyClosureProof.from_mapping(json.loads(member.payload_json))
    tampered = replace(original, analysis=_reanalysis(original, seeds=()))
    checks = _checks(inputs, compilation, ledger, _member(tampered, member.scope), policy)
    assert "seed_set_mismatch" in checks[2].violation_codes
    assert "closure_mismatch" in checks[3].violation_codes


def test_root_frontier_and_closure_tampering_recomputed_by_verifier():
    inputs, compilation, ledger, member, policy = _proof("taint")
    original = DependencyClosureProof.from_mapping(json.loads(member.payload_json))
    closure = original.analysis.seed_closures[0]
    alternate = next(ref for ref in original.analysis.node_refs if ref not in closure.root_refs)
    altered = replace(closure, root_refs=(alternate,), affected_refs=tuple(sorted((alternate,), key=lambda ref: json.dumps(ref.to_mapping(), sort_keys=True))))
    tampered = replace(original, analysis=_reanalysis(original, seeds=(altered, *original.analysis.seed_closures[1:])))
    checks = _checks(inputs, compilation, ledger, _member(tampered, member.scope), policy)
    assert "seed_root_mismatch" in checks[2].violation_codes
    assert "closure_mismatch" in checks[1].violation_codes
    assert "closure_mismatch" in checks[3].violation_codes


def test_frontier_tampering_is_not_an_accepted_bounded_claim():
    inputs, compilation, ledger, member, policy = _proof("taint")
    original = DependencyClosureProof.from_mapping(json.loads(member.payload_json))
    closure = original.analysis.seed_closures[0]
    alternate = next(ref for ref in original.analysis.node_refs if ref not in closure.frontier_refs)
    altered = replace(closure, frontier_refs=(alternate,))
    tampered = replace(original, analysis=_reanalysis(original, seeds=(altered, *original.analysis.seed_closures[1:])))
    checks = _checks(inputs, compilation, ledger, _member(tampered, member.scope), policy)
    assert "seed_frontier_mismatch" in checks[2].violation_codes
    assert "unbounded_frontier" not in checks[2].violation_codes
    assert "closure_mismatch" in checks[3].violation_codes


def test_unknown_frontier_is_preserved_but_strict_global_fails_dependency_rule():
    inputs, compilation, ledger, member, policy = _proof("unknown")
    checks = _checks(inputs, compilation, ledger, member, policy)
    assert checks[0].status == checks[1].status == checks[3].status == "pass"
    assert checks[2].status == "fail"
    assert checks[2].violation_codes == ("unbounded_frontier",)


def test_scc_and_reversed_arc_are_checked_against_independent_universe():
    inputs, compilation, ledger, member, policy = _proof()
    original = DependencyClosureProof.from_mapping(json.loads(member.payload_json))
    arc = original.analysis.arcs[0]
    altered_arcs = (replace(arc, from_ref=arc.to_ref, to_ref=arc.from_ref), *original.analysis.arcs[1:])
    tampered = replace(original, analysis=_reanalysis(original, arcs=altered_arcs))
    checks = _checks(inputs, compilation, ledger, _member(tampered, member.scope), policy)
    assert "projection_reversed_arc" in checks[0].violation_codes
    assert "scc_mismatch" in checks[1].violation_codes or "condensation_mismatch" in checks[1].violation_codes


def test_extra_arc_is_detected_even_when_its_proof_analysis_is_self_consistent():
    inputs, compilation, ledger, member, policy = _proof()
    original = DependencyClosureProof.from_mapping(json.loads(member.payload_json))
    node = original.analysis.node_refs[0]
    extra = DependencyArc(node, node, "subject_to_fact", node)
    tampered = replace(original, analysis=_reanalysis(original, arcs=(*original.analysis.arcs, extra)))
    checks = _checks(inputs, compilation, ledger, _member(tampered, member.scope), policy)
    assert "projection_extra_arc" in checks[0].violation_codes


def test_wrong_revision_and_reconstruction_failure_cannot_leave_other_rules_passing():
    inputs, compilation, ledger, member, policy = _proof()
    wrong_revision = ArtifactMember(
        member.artifact_type, member.logical_id, 2, member.scope, member.content_hash, member.payload_json
    )
    checks = _checks(inputs, compilation, ledger, wrong_revision, policy)
    assert "member_identity_invalid" in checks[0].violation_codes
    original_ledger = CoverageLedger.from_mapping(json.loads(ledger.payload_json))
    first_window = original_ledger.windows[0]
    broken_ledger = _artifact(
        "coverage_ledger",
        member.scope,
        replace(original_ledger, windows=(replace(first_window, fact_refs=()), *original_ledger.windows[1:])).to_mapping(),
    )
    checks = verify_dependency_proof(
        inputs, graph_member=compilation.narrative.narrative_graph, event_card_member=compilation.narrative.event_cards,
        ledger_member=broken_ledger, proof_member=member, policy=policy,
    )
    assert checks[0].status == "fail"
    assert all(item.status == "fail" for item in checks[1:])
