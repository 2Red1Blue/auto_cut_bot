"""CoverageAdmission is a strict pure value, never caller authorization."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from autocut_kernel.semantic_chain.coverage_admission import CoverageAdmission
from autocut_kernel.semantic_chain.dependency_projection import DependencyProjectionPolicy
from autocut_kernel.semantic_chain.dependency_proof import build_dependency_proof
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity
from autocut_kernel.semantic_chain.stage1_checks import KC_RULE_IDS, Stage1Check

from tests.semantic_chain.test_coverage_analysis import _clean_inputs
from tests.semantic_chain.test_coverage_compiler import _compile
from tests.semantic_chain.test_stage1_draft import POLICY

HASH = "sha256:" + "a" * 64


def _admission(*, statuses: dict[str, tuple[str, tuple[str, ...]]] | None = None) -> CoverageAdmission:
    inputs = _clean_inputs()
    compilation, ledger = _compile(inputs)
    dependency_policy = DependencyProjectionPolicy("semantic-dependencies-v1")
    proof = build_dependency_proof(
        inputs,
        graph_member=compilation.narrative.narrative_graph,
        event_card_member=compilation.narrative.event_cards,
        ledger_member=compilation.coverage_ledger,
        policy=dependency_policy,
        revision=1,
    )
    by_rule = statuses or {}
    checks = tuple(
        Stage1Check(rule, *by_rule.get(rule, ("pass", ()))) for rule in KC_RULE_IDS
    )
    return CoverageAdmission(
        "admission-v1",
        ledger.input_binding_sha256,
        HASH,
        ledger.draft_sha256,
        POLICY.canonical_hash,
        ledger.coverage_policy_sha256,
        dependency_policy.canonical_hash,
        "strict_global",
        "stage1-kc-v1",
        tuple(SemanticMemberIdentity.from_artifact_member(member) for member in (*compilation.members, proof)),
        checks,
    )


def test_real_seven_business_identity_subject_and_all_seventeen_results_round_trip():
    admission = _admission()
    assert {item.artifact_type for item in admission.business_members} == {
        "event_card_set", "episode_digest_set", "narrative_graph", "evidence_diagnostics",
        "conflict_diagnostics", "coverage_ledger", "dependency_closure_proof",
    }
    assert len(admission.rule_results) == 17
    assert admission.validation_status == "valid" and admission.next_action == "continue"
    assert not hasattr(admission, "authorize") and not hasattr(admission, "accepted")
    assert CoverageAdmission.from_mapping(json.loads(json.dumps(admission.to_mapping()))) == admission


@pytest.mark.parametrize(
    "rule,status,code,expected_status,expected_action",
    [
        ("KC-GRAPH-001", "fail", "graph_invalid", "invalid", "repair"),
        ("KC-DEP-003", "fail", "frontier_open", "invalid", "quarantine"),
        ("KC-GATE-001", "fail", "gate_closed", "valid", "quarantine"),
        ("KC-AUTH-001", "indeterminate", "store_unknown", "indeterminate", "quarantine"),
        ("KC-IN-001", "indeterminate", "store_unknown", "indeterminate", "stop"),
    ],
)
def test_fail_or_indeterminate_never_continue(rule, status, code, expected_status, expected_action):
    admission = _admission(statuses={rule: (status, (code,))})
    assert admission.validation_status == expected_status
    assert admission.next_action == expected_action != "continue"


@pytest.mark.parametrize("rule", KC_RULE_IDS)
@pytest.mark.parametrize("status", ("fail", "indeterminate"))
def test_every_nonpassing_rule_prevents_continue(rule: str, status: str):
    admission = _admission(statuses={rule: (status, ("checked_not_pass",))})
    assert admission.next_action != "continue"


def test_action_precedence_is_stop_then_repair_then_quarantine():
    assert _admission(statuses={
        "KC-GRAPH-001": ("fail", ("repairable",)), "KC-DEP-003": ("fail", ("tainted",)),
    }).next_action == "repair"
    assert _admission(statuses={
        "KC-IN-001": ("fail", ("missing_store",)), "KC-GRAPH-001": ("fail", ("repairable",)),
    }).next_action == "stop"


@pytest.mark.parametrize("change", ["subject", "derived", "rule_subject", "extra", "missing", "types"])
def test_from_mapping_is_closed_and_recomputes_all_derived_values(change):
    wire = _admission().to_mapping()
    if change == "subject":
        wire["subject_hash"] = HASH.replace("a", "b")
    elif change == "derived":
        wire["next_action"] = "continue" if wire["next_action"] != "continue" else "stop"
    elif change == "rule_subject":
        wire["rule_results"][0]["subject_hash"] = HASH
    elif change == "extra":
        wire["caller_authorized"] = True
    elif change == "missing":
        del wire["coverage_mode"]
    else:
        wire["business_members"] = tuple(wire["business_members"])
    with pytest.raises(ValueError):
        CoverageAdmission.from_mapping(wire)


@pytest.mark.parametrize("member_change", ["scope", "revision", "hash", "logical_id", "self"])
def test_business_subject_is_exact_and_admission_excludes_itself(member_change):
    admission = _admission()
    members = list(admission.business_members)
    if member_change == "scope":
        members[0] = replace(members[0], scope=replace(members[0].scope, key="foreign"))
    elif member_change == "revision":
        members[0] = replace(members[0], revision=2)
    elif member_change == "hash":
        wire = admission.to_mapping()
        wire["business_members"][0]["content_hash"] = "sha256:" + "b" * 64
        with pytest.raises(ValueError):
            CoverageAdmission.from_mapping(wire)
        return
    elif member_change == "logical_id":
        members[0] = replace(members[0], logical_id="wrong")
    else:
        members[-1] = replace(members[-1], artifact_type="coverage_admission", logical_id="coverage_admission")
    with pytest.raises(ValueError):
        replace(admission, business_members=tuple(members))


def test_member_identity_order_and_mapping_are_canonical_and_not_mutable():
    admission = _admission()
    reordered = replace(admission, business_members=tuple(reversed(admission.business_members)))
    assert reordered == admission and reordered.subject_hash == admission.subject_hash
    wire = admission.to_mapping()
    wire["business_members"].clear()
    assert len(admission.business_members) == 7
