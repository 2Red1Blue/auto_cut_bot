"""Closed decision values over synthetic real-command predecessor content.

The three explicit unit audit checks below are NOT Store/audit authority.
"""

from dataclasses import FrozenInstanceError, replace

import pytest
from autocut_kernel.semantic_chain.editorial_admission import (
    SS_BATCH_RULE_IDS,
    SS_COMMAND_RULE_IDS,
    SS_EVALUATION_STRATEGY,
    EditorialCheck,
    EditorialStoryDecision,
    SemanticFeasibilityAdmission,
)
from autocut_kernel.semantic_chain.editorial_material_search import MaterialSearchResult
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity

from tests.semantic_chain.test_editorial_evaluation import evaluate_case, evaluation_case


def admission_for(evaluation, request):
    checks = {check.rule_id: check for check in evaluation.batch_checks}
    checks.update({rule: EditorialCheck(rule, "pass", ()) for rule in SS_COMMAND_RULE_IDS})
    return SemanticFeasibilityAdmission(
        evaluation.contexts.input_binding_sha256, evaluation.raw_draft_sha256, evaluation.canonical_draft_sha256,
        request.command_policy.canonical_hash, request.stage2_request.command_policy.canonical_hash,
        evaluation.feasibility,
        tuple(SemanticMemberIdentity.from_artifact_member(member) for member in evaluation.expected_business.members),
        evaluation.story_checks, tuple(checks[rule] for rule in SS_BATCH_RULE_IDS), SS_EVALUATION_STRATEGY,
    )


@pytest.fixture(scope="module")
def admission():
    case = evaluation_case()
    return admission_for(evaluate_case(case), case[1])


def test_complete_ordered_batch_roundtrip_and_fresh_deep_mapping(admission):
    assert SemanticFeasibilityAdmission.from_mapping(admission.to_mapping()) == admission
    assert admission.next_action == "continue"
    assert admission.validation_status == "valid"
    assert len(admission.business_members) == 6
    assert len(admission.stories) == 2
    mapping = admission.to_mapping()
    mapping["stories"][0]["checks"][0]["status"] = "fail"
    mapping["business_members"][0]["scope"]["key"] = "foreign"
    assert admission.stories[0].checks[0].status == "pass"
    assert admission.to_mapping() != mapping
    with pytest.raises(FrozenInstanceError):
        admission.checks = ()


@pytest.mark.parametrize("field", ["input_binding_sha256", "raw_draft_sha256", "canonical_draft_sha256", "command_policy_sha256", "stage2_policy_sha256"])
@pytest.mark.parametrize("value", [None, 1, True, "sha256:" + "0" * 64, "sha256:" + "A" * 64])
def test_identity_hashes_are_exact_nonzero_lowercase(admission, field, value):
    with pytest.raises(ValueError):
        replace(admission, **{field: value})


@pytest.mark.parametrize("change", ["missing_batch", "extra_batch", "reordered_batch", "missing_story_check", "reordered_story_check",
                                     "duplicate_story", "reordered_story", "missing_subject", "reordered_subject", "scope", "revision", "kind"])
def test_every_rule_subject_and_story_order_is_closed(admission, change):
    with pytest.raises(ValueError):
        if change == "missing_batch":
            replace(admission, checks=admission.checks[:-1])
        elif change == "extra_batch":
            replace(admission, checks=admission.checks + admission.checks[:1])
        elif change == "reordered_batch":
            replace(admission, checks=tuple(reversed(admission.checks)))
        elif change == "missing_story_check":
            replace(admission.stories[0], checks=admission.stories[0].checks[:-1])
        elif change == "reordered_story_check":
            replace(admission.stories[0], checks=tuple(reversed(admission.stories[0].checks)))
        elif change == "duplicate_story":
            replace(admission, stories=admission.stories[:1] * 2)
        elif change == "reordered_story":
            replace(admission, stories=tuple(reversed(admission.stories)))
        elif change == "missing_subject":
            replace(admission, business_members=admission.business_members[:-1])
        elif change == "reordered_subject":
            replace(admission, business_members=tuple(reversed(admission.business_members)))
        else:
            ref = admission.business_members[0]
            altered = replace(ref, **{
                "scope": {"scope": replace(ref.scope, key="foreign")},
                "revision": {"revision": ref.revision + 1},
                "kind": {"artifact_type": "coverage_admission"},
            }[change])
            replace(admission, business_members=(altered, *admission.business_members[1:]))


@pytest.mark.parametrize("field", ["subject_hash", "feasibility_sha256", "target_story_ids", "validation_status", "next_action", "schema_version"])
def test_wire_cannot_self_claim_derived_identity_or_decision(admission, field):
    mapping = admission.to_mapping()
    mapping[field] = [] if field == "target_story_ids" else "forged"
    with pytest.raises(ValueError):
        SemanticFeasibilityAdmission.from_mapping(mapping)


def test_every_wire_field_is_required_and_extra_fields_rejected(admission):
    for field in admission.to_mapping():
        mapping = admission.to_mapping()
        del mapping[field]
        with pytest.raises(ValueError):
            SemanticFeasibilityAdmission.from_mapping(mapping)
    mapping = admission.to_mapping()
    mapping["accepted"] = True
    with pytest.raises(ValueError):
        SemanticFeasibilityAdmission.from_mapping(mapping)


@pytest.mark.parametrize("rule,status,codes", [
    ("SS-BUDGET-001", "pass", ()), ("SS-IN-001", True, ()), ("SS-IN-001", "pass", ("reason",)),
    ("SS-IN-001", "fail", ()), ("SS-IN-001", "fail", ("b", "a")), ("SS-IN-001", "fail", ("a", "a")),
    ("SS-IN-001", "fail", ["reason"]), ("SS-IN-001", "fail", (True,)),
])
def test_no_unregistered_rules_or_implicit_pass(rule, status, codes):
    with pytest.raises(ValueError):
        EditorialCheck(rule, status, codes)


@pytest.mark.parametrize("rule,status,expected", [
    ("SS-PHYS-DEFER-001", "fail", "stop"), ("SS-HASH-001", "fail", "stop"),
    ("SS-EV-001", "fail", "repair"), ("SS-DUR-002", "indeterminate", "quarantine"),
    ("SS-IN-001", "indeterminate", "stop"), ("SS-SEARCH-001", "indeterminate", "quarantine"),
])
def test_next_action_is_diagnostic_derived_and_never_provider_retry(admission, rule, status, expected):
    check = EditorialCheck(rule, status, ("synthetic_failure",))
    if rule in SS_BATCH_RULE_IDS:
        changed = replace(admission, checks=tuple(check if old.rule_id == rule else old for old in admission.checks))
    else:
        story = admission.stories[0]
        changed = replace(admission, stories=(EditorialStoryDecision(story.story_id,
            tuple(check if old.rule_id == rule else old for old in story.checks)), *admission.stories[1:]))
    assert changed.next_action == expected
    assert changed.canonical_hash != admission.canonical_hash
    assert changed.subject_hash == admission.subject_hash


@pytest.mark.parametrize("status", ["infeasible", "indeterminate"])
def test_negative_search_cannot_retain_passing_check_direct_or_wire(admission, status):
    feasibility = replace(admission.feasibility, material_search=MaterialSearchResult(status, (), 0))
    with pytest.raises(ValueError, match="passing search"):
        replace(admission, feasibility=feasibility)
    mapping = admission.to_mapping()
    mapping["feasibility"] = feasibility.to_mapping()
    mapping["feasibility_sha256"] = feasibility.canonical_hash
    with pytest.raises(ValueError, match="passing search"):
        SemanticFeasibilityAdmission.from_mapping(mapping)


def test_absent_timing_cannot_retain_passing_check_direct_or_wire(admission):
    timing = admission.feasibility.timing_witnesses
    feasibility = replace(admission.feasibility, timing_witnesses=(replace(timing[0], durations=None), *timing[1:]))
    with pytest.raises(ValueError, match="passing duration"):
        replace(admission, feasibility=feasibility)
    mapping = admission.to_mapping()
    mapping["feasibility"] = feasibility.to_mapping()
    mapping["feasibility_sha256"] = feasibility.canonical_hash
    with pytest.raises(ValueError, match="passing duration"):
        SemanticFeasibilityAdmission.from_mapping(mapping)


@pytest.mark.parametrize("change", ["missing", "foreign"])
def test_feasible_choices_close_exact_frozen_story_targets(admission, change):
    choices = admission.feasibility.material_search.choices
    choices = choices[:-1] if change == "missing" else (replace(choices[0], story_id="sha256:" + "f" * 64), *choices[1:])
    search = replace(admission.feasibility.material_search, choices=choices)
    feasibility = replace(admission.feasibility, material_search=search)
    with pytest.raises(ValueError, match="frozen Story targets"):
        replace(admission, feasibility=feasibility)
    mapping = admission.to_mapping()
    mapping["feasibility"] = feasibility.to_mapping()
    mapping["feasibility_sha256"] = feasibility.canonical_hash
    with pytest.raises(ValueError, match="frozen Story targets"):
        SemanticFeasibilityAdmission.from_mapping(mapping)
