"""Actual pure compiler/evaluator over parsed media evidence and MemoryStore.

The source bytes are synthetic; no database, ffmpeg or model is executed.
No compiler/material/selection/evaluation algorithm is mocked on the happy path.
"""

from dataclasses import replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.semantic_chain.material_support_models import ExclusionReasonCount
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.portfolio_values import InitialSourceUsageLedger, StorySelection
from autocut_kernel.semantic_chain.story_design_compiler import (
    _member,
    compile_story_design,
    compose_story_design_members,
)
from autocut_kernel.semantic_chain.story_design_evaluation import (
    evaluate_story_design_business_members,
)
from autocut_kernel.semantic_chain.story_design_members import decode_story_design_business_members

from tests.semantic_chain.test_material_support import material_case
from tests.semantic_chain.test_story_design_draft import POLICY


@pytest.fixture(scope="module")
def case():
    context = material_case()
    kwargs = {key: context[key] for key in ("candidate_policy", "job_policy", "story_policy")}
    kwargs["draft_policy"] = POLICY
    raw = canonical_json_bytes(context["draft"].to_mapping())
    compiled = compile_story_design(context["inputs"], context["stage1"], raw,
                                    scope=context["projection"].member.scope, revision=1, **kwargs)
    assert compiled.search.status == "feasible"
    return context, kwargs, raw, compiled


def evaluate(case, *, members=None, raw=None):
    context, kwargs, original_raw, compiled = case
    return {result.rule_id: result for result in evaluate_story_design_business_members(
        context["inputs"], context["stage1"], original_raw if raw is None else raw,
        members=compiled.business_members if members is None else members, **kwargs,
    )}


def test_real_pure_compiler_and_all_seventeen_business_checks(case):
    context, kwargs, raw, compiled = case
    assert context["stage1"].admission.next_action == "continue"
    checks = evaluate(case)
    assert len(checks) == 17
    assert "SD-IN-001" not in checks and "SD-IN-002" not in checks
    assert all(result.status == "pass" for result in checks.values())
    assert compile_story_design(context["inputs"], context["stage1"], raw,
                                scope=context["projection"].member.scope, revision=1, **kwargs) == compiled


def test_structurally_feasible_later_portfolio_is_not_canonical(case):
    context, _, _, compiled = case
    scope = context["projection"].member.scope
    values = decode_story_design_business_members(compiled.business_members, scope=scope)
    assert compiled.search.proposal_indexes == (0,)
    later = values.proposal_set.proposals[1]
    selected = StorySelection(1, SemanticObjectRef(values.portfolio.proposal_set_ref, "proposal", later.proposal.proposal_id))
    changed = replace(values.portfolio, selections=(selected,), requirement_assignments=tuple(
        replace(row, proposal_index=1) for row in values.portfolio.requirement_assignments
    ))
    portfolio_member = _member("portfolio", changed.to_mapping(), scope=scope, revision=1)
    usage = InitialSourceUsageLedger(SemanticMemberIdentity.from_artifact_member(portfolio_member), changed.target_story_ids)
    members = (*compiled.business_members[:2], portfolio_member,
               _member("source_usage_ledger", usage.to_mapping(), scope=scope, revision=1))
    # Pure structural closure and source assignment remain valid. Only actual
    # canonical replay proves that the earlier candidate should have won.
    decode_story_design_business_members(members, scope=scope)
    checks = evaluate(case, members=members)
    assert checks["SD-PORT-002"].status == checks["SD-PORT-003"].status == "pass"
    assert checks["SD-OBJ-001"].status == "fail"


def test_rehashed_unsupported_claim_cannot_hide_a_better_proposal(case):
    _, kwargs, _, compiled = case
    first = compiled.proposal_set.proposals[0]
    requirement = first.requirements[0]
    fake = replace(requirement, alternatives=(), exclusion_reason_counts=(
        ExclusionReasonCount("duration_insufficient", requirement.examined_candidate_count),
    ))
    support = replace(compiled.proposal_set, proposals=(replace(first, requirements=(fake,)),
                      *compiled.proposal_set.proposals[1:]))
    forged = compose_story_design_members(compiled.projection, support, job_policy=kwargs["job_policy"])
    assert forged.search.proposal_indexes == (1,)
    checks = evaluate(case, members=forged.business_members)
    assert checks["SD-MAT-001"].status == "fail"
    assert checks["SD-OBJ-001"].status == "fail"


def test_exact_raw_draft_is_redecoded_and_cannot_be_replaced_by_output_claims(case):
    context = case[0]
    draft = context["draft"]
    changed = replace(draft, proposals=(replace(draft.proposals[0], title="Different actual raw title"), *draft.proposals[1:]))
    checks = evaluate(case, raw=canonical_json_bytes(changed.to_mapping()))
    assert checks["SD-PROP-001"].status == checks["SD-MAT-001"].status == "fail"
    invalid = draft.to_mapping()
    invalid["proposals"][0]["physical_pass"] = True
    with pytest.raises(ValueError):
        evaluate(case, raw=canonical_json_bytes(invalid))


def test_independent_evaluator_never_calls_business_compiler(case, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("evaluator cannot approve by calling business compiler")
    monkeypatch.setattr("autocut_kernel.semantic_chain.story_design_compiler.compile_story_design", forbidden)
    monkeypatch.setattr("autocut_kernel.semantic_chain.story_design_compiler.compose_story_design_members", forbidden)
    assert all(result.status == "pass" for result in evaluate(case).values())
