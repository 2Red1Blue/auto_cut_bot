"""Independent business checks over real Commands with scripted in-memory I/O.

All media/LLM facts are synthetic; these tests are not real calibration or
provider acceptance. Upstream Admissions are produced by the actual Commands.
"""

import json
from dataclasses import replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.pipeline.build_editorial_blueprint_request import prepare_stage3_request
from autocut_kernel.semantic_chain.editorial_admission import (
    SS_BUSINESS_BATCH_RULE_IDS,
    SS_COMMAND_RULE_IDS,
    SS_STORY_RULE_IDS,
)
from autocut_kernel.semantic_chain.editorial_blueprint import project_editorial_blueprints
from autocut_kernel.semantic_chain.editorial_draft import EditorialBlueprintDraft
from autocut_kernel.semantic_chain.editorial_evaluation import evaluate_editorial_business_members
from autocut_kernel.semantic_chain.editorial_members import compose_editorial_business_members
from autocut_kernel.semantic_chain.editorial_models import (
    DurationRange,
    EditingIntent,
    EditorialBeatDraft,
    EvidenceAlternative,
    EvidenceRequirementDraft,
    SpanPolicy,
    StoryBlueprintDraft,
    TeaserIntent,
)
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.story_design_models import IntegerRange
from autocut_kernel.store.models import canonical_payload_hash
from autocut_kernel.vlm.models import VlmNarrativeFunction

from tests.semantic_chain.test_build_editorial_blueprint_request import stage3_case


def evaluation_case():
    inputs, request = stage3_case()
    prepared = prepare_stage3_request(request, inputs)
    contexts = prepared.contexts
    stage1, stage2 = inputs.narrative.values, inputs.portfolio.values
    catalog = stage2.business.candidate_catalog
    catalog_ref = SemanticMemberIdentity.from_artifact_member(stage2.members[0])
    stories = []
    for selected in stage2.business.portfolio.selections:
        support = stage2.business.proposal_set.proposals[selected.proposal_index]
        proposal = support.proposal
        beats = []
        for material, supported in zip(proposal.material_requirements, support.requirements, strict=True):
            ref = supported.alternatives[0].candidate_ref
            candidate = next(value for value in catalog.candidates if value.candidate_id == ref.object_id)
            assert ref == SemanticObjectRef(catalog_ref, "candidate", candidate.candidate_id)
            beats.append(EditorialBeatDraft(
                "reveal", VlmNarrativeFunction(candidate.narrative_functions[0]), "synthetic complete fact duty",
                (material.obligation_ref,), supported.required_fact_refs,
                (EvidenceRequirementDraft(material.requirement_id, "one_of", (
                    EvidenceAlternative("direct", (candidate.anchor_event.event_card_ref,), (ref,)),
                )),), (ref,), SpanPolicy("tight", ("tight",), ("tight",)),
                DurationRange(material.minimum_usable_seconds, material.minimum_usable_seconds,
                              max(material.minimum_usable_seconds, proposal.target_duration_seconds.maximum)),
            ))
        bounds = proposal.target_duration_seconds
        stories.append(StoryBlueprintDraft(selected.story_id, selected.proposal_ref, tuple(beats), (),
            DurationRange(bounds.minimum, bounds.minimum, bounds.maximum), EditingIntent("balanced", "high"),
            TeaserIntent(proposal.teaser_strategy, IntegerRange(1, 1))))
    draft = EditorialBlueprintDraft(contexts.input_binding_sha256, tuple(stories))
    projection = project_editorial_blueprints(stage1, stage2, draft,
        expected_input_binding_sha256=contexts.input_binding_sha256, strategy_version=request.blueprint_strategy_version)
    business = compose_editorial_business_members(contexts, projection)
    return inputs, request, canonical_json_bytes(draft.to_mapping()), business


def evaluate_case(case, *, raw=None, members=None, policy=None):
    inputs, request, original, business = case
    return evaluate_editorial_business_members(inputs.semantic, inputs.narrative.values, inputs.portfolio.values,
        original if raw is None else raw, members=business.members if members is None else members,
        command_policy=request.command_policy if policy is None else policy,
        stage2_policy=request.stage2_request.command_policy)


@pytest.fixture(scope="module")
def case():
    return evaluation_case()


def rewrite(member, payload):
    raw = canonical_json_bytes(payload).decode()
    return replace(member, payload_json=raw, content_hash=canonical_payload_hash(raw))


def test_actual_evidence_evaluation_has_no_fabricated_command_checks(case):
    result = evaluate_case(case)
    assert result.expected_business == case[3]
    assert result.feasibility.status == "feasible"
    assert tuple(check.rule_id for check in result.batch_checks) == SS_BUSINESS_BATCH_RULE_IDS
    assert not set(SS_COMMAND_RULE_IDS).intersection(check.rule_id for check in result.batch_checks)
    assert len(result.story_checks) == 2
    for row in result.story_checks:
        assert tuple(check.rule_id for check in row.checks) == SS_STORY_RULE_IDS
        assert row.next_action == "continue"
    assert result.feasibility.input_binding_sha256 != result.contexts.input_binding_sha256


@pytest.mark.parametrize("change", ["summary", "role", "duration", "span", "preference"])
def test_rehashed_schema_valid_blueprint_cannot_replace_actual_raw(case, change):
    members = list(case[3].members)
    payload = json.loads(members[0].payload_json)
    beat = payload["blueprint"]["beats"][0]
    if change == "summary":
        beat["summary"] = "rewritten but rehashed"
    elif change == "role":
        beat["narrative_role"] = "setup"
    elif change == "duration":
        beat["duration_seconds"]["target"] += 1
    elif change == "span":
        beat["span_policy"] = {"preferred": "scene", "allowed": ["scene"], "fallback_order": ["scene"]}
    else:
        beat["candidate_preferences"] = []
    members[0] = rewrite(members[0], payload)
    result = evaluate_case(case, members=tuple(members))
    checks = {check.rule_id: check.status for check in result.story_checks[0].checks}
    assert checks["SS-HASH-001"] == "fail"
    assert result.story_checks[0].next_action == "stop"
    assert result.story_checks[1].next_action == "continue"


@pytest.mark.parametrize("change", ["missing_story", "reorder", "physical", "candidate", "unknown", "duplicate"])
def test_invalid_actual_business_is_causal_denial_not_replacement(case, change):
    members = list(case[3].members)
    if change == "missing_story":
        members = members[:3]
    elif change == "reorder":
        members = members[3:] + members[:3]
    else:
        payload = json.loads(members[0].payload_json)
        row = payload["blueprint"]["beats"][0]["evidence_requirements"][0]
        if change == "physical":
            row["physical_requirements_hash"] = "sha256:" + "e" * 64
        elif change == "candidate":
            row["alternatives"][0]["candidate_refs"][0]["object_id"] = "foreign"
        elif change == "unknown":
            payload["pass"] = True
        else:
            raw = members[0].payload_json
            members[0] = replace(members[0], payload_json=raw[:-1] + ',"schema_version":"duplicate"}')
        if change != "duplicate":
            members[0] = rewrite(members[0], payload)
    with pytest.raises(ValueError):
        evaluate_case(case, members=tuple(members))


@pytest.mark.parametrize("raw", [b"{}", b'{"x":1,"x":2}', b'{"x":1.2}', b'NaN', b'\xff'])
def test_strict_actual_raw_is_never_replaced_by_producer_projection(case, raw):
    with pytest.raises(ValueError):
        evaluate_case(case, raw=raw)


def test_exhaustion_does_not_falsely_fail_local_event_support(case):
    policy = replace(case[1].command_policy, feasibility_policy=replace(case[1].feasibility_policy, max_search_states=1))
    result = evaluate_case(case, policy=policy)
    assert result.feasibility.material_search.status == "indeterminate"
    assert {row.rule_id: row.status for row in result.batch_checks}["SS-SEARCH-001"] == "indeterminate"
    assert all(next(check for check in row.checks if check.rule_id == "SS-EV-001").status == "pass"
               for row in result.story_checks)


def test_context_overflow_fails_closed_before_replacing_any_content(case):
    policy = replace(case[1].command_policy, context_policy=replace(case[1].context_policy, max_story_context_bytes=1))
    with pytest.raises(ValueError):
        evaluate_case(case, policy=policy)


def test_positive_witness_is_directly_verified_and_search_is_recomputed(case, monkeypatch):
    from autocut_kernel.semantic_chain import editorial_evaluation as owner

    original = owner.verify_editorial_feasibility
    calls = []

    def verify(*args, **kwargs):
        calls.append(args[3])
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, "verify_editorial_feasibility", verify)
    result = evaluate_case(case)
    assert calls == [result.feasibility]
    assert result.feasibility.material_search.examined_states > 0


def test_fully_rehashed_physical_requirement_rewrite_is_hard_stop(case):
    members = list(case[3].members)
    payload = json.loads(members[0].payload_json)
    row = payload["blueprint"]["beats"][0]["evidence_requirements"][0]
    row["physical_requirements"] = []
    row["physical_requirements_hash"] = canonical_json_hash([])
    members[0] = rewrite(members[0], payload)
    result = evaluate_case(case, members=tuple(members))
    checks = {check.rule_id: check.status for check in result.story_checks[0].checks}
    assert checks["SS-PHYS-DEFER-001"] == "fail"
    assert result.story_checks[0].next_action == "stop"


@pytest.mark.parametrize("one_complete", [False, True])
def test_actual_full_event_edges_not_two_half_intervals_drive_local_rule(case, one_complete):
    from copy import deepcopy

    from tests.semantic_chain.test_editorial_feasibility import _custom_case

    def change(payload):
        payload["events"][0]["support"]["proxy_interval"] = {"start_pts": 10, "end_pts": 90, "uncertainty_pts": 0}
        first = payload["candidate_hypotheses"][0]
        first["support"]["proxy_interval"] = {"start_pts": 10, "end_pts": 60, "uncertainty_pts": 0}
        second = deepcopy(first)
        second["local_candidate_id"] = "candidate_2"
        second["support"]["proxy_interval"] = {"start_pts": 10 if one_complete else 20, "end_pts": 90, "uncertainty_pts": 0}
        payload["candidate_hypotheses"].append(second)

    inputs, contexts, draft, _ = _custom_case(change, selected_story_count=1)
    assert inputs.narrative.values.admission.next_action == inputs.portfolio.values.admission.next_action == "continue"
    support = inputs.portfolio.values.business.proposal_set.proposals[0].requirements[0]
    assert len(support.alternatives) == 2
    beat = draft.stories[0].beats[0]
    row = beat.evidence_requirements[0]
    alternative = replace(row.alternative_sets[0], candidate_refs=tuple(item.candidate_ref for item in support.alternatives))
    changed_beat = replace(beat, evidence_requirements=(replace(row, alternative_sets=(alternative,)),))
    draft = replace(draft, stories=(replace(draft.stories[0], beats=(changed_beat,)),))
    command_policy = replace(case[1].command_policy, context_policy=contexts.policy)
    stage2_policy = replace(case[1].stage2_request.command_policy, job_policy=contexts.job_policy,
                            story_policy=contexts.story_policy, candidate_policy=contexts.candidate_policy)
    projection = project_editorial_blueprints(inputs.narrative.values, inputs.portfolio.values, draft,
        expected_input_binding_sha256=contexts.input_binding_sha256, strategy_version=command_policy.blueprint_strategy_version)
    members = compose_editorial_business_members(contexts, projection).members
    result = evaluate_editorial_business_members(inputs.semantic, inputs.narrative.values, inputs.portfolio.values,
        canonical_json_bytes(draft.to_mapping()), members=members, command_policy=command_policy, stage2_policy=stage2_policy)
    event_check = next(check for check in result.story_checks[0].checks if check.rule_id == "SS-EV-001")
    assert event_check.status == ("pass" if one_complete else "fail")
    assert result.feasibility.status == ("feasible" if one_complete else "infeasible")
