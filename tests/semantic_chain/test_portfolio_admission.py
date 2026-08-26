"""Test-only decision claims; no generated pass row is production evidence."""

from dataclasses import replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity
from autocut_kernel.semantic_chain.portfolio_admission import (
    SD_RULE_IDS,
    PortfolioAdmission,
    Stage2Check,
)
from autocut_kernel.semantic_chain.story_design_compiler import compose_story_design_members
from autocut_kernel.semantic_chain.story_design_members import decode_story_design_business_members
from autocut_kernel.semantic_chain.story_design_result import decode_story_design_members
from autocut_kernel.store.models import ArtifactMember, canonical_payload_hash

from tests.semantic_chain.test_story_design_compiler import composition_case


def admission_case():
    projection, support, job = composition_case()
    compiled = compose_story_design_members(projection, support, job_policy=job)
    business = decode_story_design_business_members(compiled.business_members, scope=projection.member.scope)
    admission = PortfolioAdmission(
        support.input_binding_sha256, "sha256:" + "6" * 64, support.draft_sha256,
        "sha256:" + "7" * 64, projection.catalog.policy_sha256, job.story_design_policy_sha256,
        job.canonical_hash, "stage2-sd-v1",
        tuple(SemanticMemberIdentity.from_artifact_member(member) for member in compiled.business_members),
        business.portfolio.target_story_ids, tuple(Stage2Check(rule, "pass", ()) for rule in SD_RULE_IDS),
    )
    return compiled.business_members, admission


def members_for(business, admission):
    raw = canonical_json_bytes(admission.to_mapping()).decode()
    return (*business, ArtifactMember("portfolio_admission", "portfolio_admission", business[0].revision,
                                      business[0].scope, canonical_payload_hash(raw), raw))


def test_complete_closed_admission_and_five_members_roundtrip():
    business, admission = admission_case()
    assert PortfolioAdmission.from_mapping(admission.to_mapping()) == admission
    assert len(admission.rule_results) == 19
    assert admission.next_action == "continue"
    assert all(member.artifact_type != "portfolio_admission" for member in admission.business_members)
    result = decode_story_design_members(members_for(business, admission), scope=business[0].scope)
    assert result.admission == admission
    assert result.business.members == business


@pytest.mark.parametrize("rule,status,action", [
    ("SD-IN-001", "indeterminate", "stop"), ("SD-IN-002", "fail", "stop"),
    ("SD-MAT-001", "fail", "stop"), ("SD-MAT-002", "indeterminate", "quarantine"),
    ("SD-PORT-003", "fail", "repair"), ("SD-PORT-003", "indeterminate", "quarantine"),
    ("SD-PROP-001", "fail", "repair"), ("SD-OBJ-001", "indeterminate", "stop"),
])
def test_nonpass_never_becomes_continue_and_remains_decodable(rule, status, action):
    business, admission = admission_case()
    checks = tuple(Stage2Check(item.rule_id, status, ("test_reason",)) if item.rule_id == rule else item
                   for item in admission.rule_results)
    changed = replace(admission, rule_results=checks)
    assert changed.validation_status == ("invalid" if status == "fail" else "indeterminate")
    assert changed.next_action == action
    assert decode_story_design_members(members_for(business, changed), scope=business[0].scope).admission == changed


@pytest.mark.parametrize("target", ["missing_rule", "duplicate_rule", "foreign_rule", "self_subject", "decision", "subject", "rule_subject", "target_hash", "unknown"])
def test_forged_incomplete_or_cyclic_decisions_are_rejected(target):
    _, admission = admission_case()
    wire = admission.to_mapping()
    if target == "missing_rule":
        wire["rule_results"].pop()
    elif target == "duplicate_rule":
        wire["rule_results"][-1] = wire["rule_results"][0]
    elif target == "foreign_rule":
        wire["rule_results"][0]["rule_id"] = "KC-IN-001"
    elif target == "self_subject":
        wire["business_members"][0]["artifact_type"] = "portfolio_admission"
    elif target == "decision":
        wire["next_action"] = "publish"
    elif target == "subject":
        wire["subject_hash"] = "sha256:" + "8" * 64
    elif target == "rule_subject":
        wire["rule_results"][0]["subject_hash"] = "sha256:" + "8" * 64
    elif target == "target_hash":
        wire["target_story_ids_hash"] = "sha256:" + "8" * 64
    else:
        wire["publish_decision"] = "allow"
    with pytest.raises(ValueError):
        PortfolioAdmission.from_mapping(wire)


def test_every_admission_field_is_required():
    _, admission = admission_case()
    for key in admission.to_mapping():
        wire = admission.to_mapping()
        del wire[key]
        with pytest.raises(ValueError):
            PortfolioAdmission.from_mapping(wire)


@pytest.mark.parametrize("field", ["input_binding_sha256", "canonical_draft_sha256", "candidate_policy_sha256", "job_policy_sha256", "target_story_ids", "business_members"])
def test_rehashed_admission_cannot_name_other_inputs_policies_targets_or_members(field):
    business, admission = admission_case()
    if field == "target_story_ids":
        changed = replace(admission, target_story_ids=("sha256:" + "9" * 64,))
    elif field == "business_members":
        changed = replace(admission, business_members=tuple(replace(item, content_hash="sha256:" + "9" * 64)
                                                          for item in admission.business_members))
    else:
        changed = replace(admission, **{field: "sha256:" + "9" * 64})
    with pytest.raises(ValueError):
        decode_story_design_members(members_for(business, changed), scope=business[0].scope)


@pytest.mark.parametrize("status,codes", [("pass", ("test",)), ("fail", ()), ("indeterminate", ()), (True, ()), ("pass", [])])
def test_check_status_cannot_default_or_hide_violations(status, codes):
    with pytest.raises(ValueError):
        Stage2Check("SD-IN-001", status, codes)
