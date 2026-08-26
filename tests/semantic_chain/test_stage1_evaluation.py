"""Full business evaluation uses real compilers, not committed-input witnesses."""

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.semantic_chain import stage1_evaluation
from autocut_kernel.semantic_chain.stage1_checks import KC_RULE_IDS, Stage1Check
from autocut_kernel.semantic_chain.stage1_evaluation import evaluate_stage1_business_members
from autocut_kernel.store.models import canonical_payload_hash

from tests.semantic_chain.test_coverage_analysis import _clean_inputs, _replace_pack
from tests.semantic_chain.test_coverage_compiler import COVERAGE, _compile
from tests.semantic_chain.test_dependency_proof import POLICY as DEPENDENCY
from tests.semantic_chain.test_dependency_proof import _build
from tests.semantic_chain.test_stage1_draft import POLICY, _draft


def _case(*, taint=False, unknown=False):
    inputs = _clean_inputs()
    if taint:
        inputs = _replace_pack(inputs, 0, lambda pack: replace(
            pack, entities=(replace(pack.entities[0], support=replace(
                pack.entities[0].support, confidence=Decimal("0.1"),
            )),),
        ))
    draft = _draft(inputs)
    if not unknown:
        draft["merge_proposals"] = []
    raw = canonical_json_bytes(draft)
    compilation, _ledger = _compile(inputs, raw=raw)
    return inputs, raw, (*compilation.members, _build(inputs, compilation))


def _evaluate(inputs, raw, members):
    return evaluate_stage1_business_members(
        inputs, raw, members=members, draft_policy=POLICY,
        coverage_policy=COVERAGE, dependency_policy=DEPENDENCY,
    )


def test_complete_clean_business_verification_never_claims_store_read():
    inputs, raw, members = _case()
    checks = _evaluate(inputs, raw, members)
    assert len(checks) == 16
    assert {item.rule_id for item in checks} == set(KC_RULE_IDS) - {"KC-IN-001"}
    assert all(item.status == "pass" for item in checks)
    assert checks == _evaluate(inputs, raw, tuple(reversed(members)))


@pytest.mark.parametrize("taint,unknown", [(True, False), (False, True)])
def test_actual_taint_cannot_become_success_even_when_proof_is_well_formed(taint, unknown):
    checks = {item.rule_id: item for item in _evaluate(*_case(taint=taint, unknown=unknown))}
    assert checks["KC-GATE-001"].status == "fail"
    assert checks["KC-DEP-003"].status == ("fail" if unknown else "pass")


@pytest.mark.parametrize("change", ["missing", "duplicate", "list", "scope", "revision", "hash", "logical"])
def test_seven_business_member_boundary_is_exact(change):
    inputs, raw, members = _case()
    if change == "missing":
        members = members[:-1]
    elif change == "duplicate":
        members = (*members[:-1], members[0])
    elif change == "list":
        members = list(members)
    else:
        fields = {
            "scope": {"scope": replace(members[-1].scope, key="foreign")},
            "revision": {"revision": 2}, "hash": {"content_hash": "sha256:" + "a" * 64},
            "logical": {"logical_id": "foreign"},
        }
        members = (*members[:-1], replace(members[-1], **fields[change]))
    with pytest.raises(ValueError):
        _evaluate(inputs, raw, members)


def test_hash_closed_changed_event_is_evaluated_not_merely_deserialized():
    inputs, raw, members = _case()
    changed = []
    for member in members:
        if member.artifact_type == "event_card_set":
            payload = json.loads(member.payload_json)
            payload["events"][0]["content"] = "invented statement"
            content = canonical_json_bytes(payload).decode()
            member = replace(member, payload_json=content, content_hash=canonical_payload_hash(content))
        changed.append(member)
    checks = {item.rule_id: item for item in _evaluate(inputs, raw, tuple(changed))}
    assert checks["KC-EVENT-001"].status == "fail"


@pytest.mark.parametrize("change", ["missing", "duplicate", "pretend_input_read"])
def test_evaluator_registry_drift_never_auto_fills_pass(monkeypatch, change):
    inputs, raw, members = _case()
    original = stage1_evaluation.verify_factual_members

    def incomplete(*args, **kwargs):
        results = original(*args, **kwargs)
        if change == "missing":
            return results[:-1]
        if change == "duplicate":
            return (*results[:-1], results[0])
        return (*results, Stage1Check("KC-IN-001", "pass", ()))

    monkeypatch.setattr(stage1_evaluation, "verify_factual_members", incomplete)
    with pytest.raises(ValueError, match="did not perform exactly"):
        _evaluate(inputs, raw, members)


def test_evaluation_does_not_call_producer_compilers(monkeypatch):
    inputs, raw, members = _case()
    from autocut_kernel.semantic_chain import (
        coverage_compiler,
        dependency_projection,
        dependency_proof,
        narrative_projection,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("producer cannot be independent verifier oracle")

    monkeypatch.setattr(coverage_compiler, "compile_stage1_coverage", forbidden)
    monkeypatch.setattr(dependency_projection, "project_dependencies", forbidden)
    monkeypatch.setattr(dependency_proof, "build_dependency_proof", forbidden)
    monkeypatch.setattr(narrative_projection, "project_narrative", forbidden)
    assert all(item.status == "pass" for item in _evaluate(inputs, raw, members))
