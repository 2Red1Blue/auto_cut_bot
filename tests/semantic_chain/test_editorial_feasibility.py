"""Actual semantic compilers/Commands with synthetic Source and in-memory I/O.

No real database, provider or media runs. Rehashed negative predecessor values
are labelled attacks, never evidence of an accepted Stage 2 execution.
"""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from fractions import Fraction

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.pipeline.build_narrative_graph_command import BuildNarrativeGraphCommand
from autocut_kernel.pipeline.compile_story_portfolio_command import CompileStoryPortfolioCommand
from autocut_kernel.pipeline.compile_story_portfolio_request import CompileStoryPortfolioRequest
from autocut_kernel.pipeline.editorial_blueprint_inputs import (
    read_committed_editorial_blueprint_inputs,
)
from autocut_kernel.semantic_chain import editorial_feasibility as owner
from autocut_kernel.semantic_chain.dependency_graph import DependencySeed, analyze_dependency_graph
from autocut_kernel.semantic_chain.editorial_blueprint import project_editorial_blueprints
from autocut_kernel.semantic_chain.editorial_context import build_editorial_contexts
from autocut_kernel.semantic_chain.editorial_draft import EditorialBlueprintDraft
from autocut_kernel.semantic_chain.editorial_feasibility import (
    EditorialFeasibilityPolicy,
    EditorialFeasibilityResult,
    EditorialTimingWitness,
    evaluate_editorial_feasibility,
    verify_editorial_feasibility,
)
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
from autocut_kernel.semantic_chain.story_design_context import story_design_input_binding
from autocut_kernel.semantic_chain.story_design_models import IntegerRange
from autocut_kernel.semantic_chain.story_design_result import decode_story_design_members
from autocut_kernel.store.models import canonical_payload_hash
from autocut_kernel.vlm.models import VlmNarrativeFunction
from autocut_kernel.vlm.retry_policy import GenerationRetryPolicy

from tests.semantic_chain.test_build_narrative_graph_command import (
    MemoryNarrativeGraphStore,
    ScriptedDraftProvider,
)
from tests.semantic_chain.test_candidate_projection import _command_request, _draft_raw
from tests.semantic_chain.test_compile_story_portfolio_command import MemoryStoryPortfolioStore
from tests.semantic_chain.test_editorial_members import CONTEXT_POLICY, editorial_case
from tests.semantic_chain.test_material_support import material_case
from tests.semantic_chain.test_story_design_draft import POLICY as DRAFT_POLICY

POLICY = EditorialFeasibilityPolicy("editorial-material-feasibility-v1", 1000)


@pytest.fixture(scope="module")
def case():
    return editorial_case()


def _evaluate(case, projection=None, **changes):
    inputs, contexts, _, original = case
    args = {"semantic": inputs.semantic, "job_policy": contexts.job_policy, "policy": POLICY}
    args.update(changes)
    return evaluate_editorial_feasibility(inputs.narrative.values, inputs.portfolio.values, projection or original, **args)


def _verify(case, result, projection=None, **changes):
    inputs, contexts, _, original = case
    args = {"semantic": inputs.semantic, "job_policy": contexts.job_policy, "policy": POLICY}
    args.update(changes)
    return verify_editorial_feasibility(inputs.narrative.values, inputs.portfolio.values, projection or original, result, **args)


def _project(case, draft):
    inputs, contexts, _, _ = case
    return project_editorial_blueprints(inputs.narrative.values, inputs.portfolio.values, draft,
        expected_input_binding_sha256=contexts.input_binding_sha256, strategy_version=contexts.policy.strategy)


def _change_first_beat(case, **changes):
    draft = case[2]
    story = draft.stories[0]
    return _project(case, replace(draft, stories=(replace(story, beats=(replace(story.beats[0], **changes),)), *draft.stories[1:])))


def _custom_case(change, *, selected_story_count=2):
    material = material_case(payload_change=change)
    semantic = material["inputs"]
    predecessor = MemoryNarrativeGraphStore(semantic)
    stage1_request = _command_request(semantic)
    outcome = BuildNarrativeGraphCommand(predecessor, ScriptedDraftProvider(_draft_raw(semantic))).execute(stage1_request).outcome
    job = replace(material["job_policy"], selected_story_count=selected_story_count, source_reuse_policy="allow")
    request = CompileStoryPortfolioRequest(stage1_request, outcome, "feasibility-synthetic", 1, stage1_request.generation,
        2_000_000, DRAFT_POLICY, material["candidate_policy"], job, material["story_policy"], GenerationRetryPolicy("generation-retry-v1", 1, ()))
    binding = story_design_input_binding(material["stage1"], material["projection"], job_policy=job,
                                        story_policy=material["story_policy"], candidate_policy=material["candidate_policy"])
    raw = canonical_json_bytes(replace(material["draft"], input_binding_sha256=binding).to_mapping())
    store = MemoryStoryPortfolioStore(semantic, predecessor)
    result = CompileStoryPortfolioCommand(store, ScriptedDraftProvider(raw)).execute(request)
    assert result.outcome.state == "succeeded", result.outcome.failure_detail_json
    inputs = read_committed_editorial_blueprint_inputs(store, stage2_request=request, stage2_outcome=result.outcome)
    stage1, stage2 = inputs.narrative.values, inputs.portfolio.values
    contexts = build_editorial_contexts(semantic, stage1, stage2, policy=CONTEXT_POLICY, scope=request.artifact_scope,
        revision=1, job_policy=job, story_policy=material["story_policy"], candidate_policy=material["candidate_policy"])
    stories = []
    for selection in stage2.business.portfolio.selections:
        supported = stage2.business.proposal_set.proposals[selection.proposal_index]
        original, row = supported.proposal.material_requirements[0], supported.requirements[0]
        candidate = next(item for item in stage2.business.candidate_catalog.candidates
                         if item.candidate_id == row.alternatives[0].candidate_ref.object_id)
        beat = EditorialBeatDraft("reveal", VlmNarrativeFunction.REVEAL, "完整承载事实", (original.obligation_ref,), row.required_fact_refs,
            (EvidenceRequirementDraft(original.requirement_id, "one_of", (EvidenceAlternative("direct", (candidate.anchor_event.event_card_ref,),
                (row.alternatives[0].candidate_ref,)),)),), (), SpanPolicy("tight", ("tight",), ("tight",)), DurationRange(1, 1, 10))
        stories.append(StoryBlueprintDraft(selection.story_id, selection.proposal_ref, (beat,), (), DurationRange(1, 1, 10),
            EditingIntent("balanced", "high"), TeaserIntent(supported.proposal.teaser_strategy, IntegerRange(1, 1))))
    draft = EditorialBlueprintDraft(contexts.input_binding_sha256, tuple(stories))
    projection = project_editorial_blueprints(stage1, stage2, draft, expected_input_binding_sha256=contexts.input_binding_sha256,
                                               strategy_version=contexts.policy.strategy)
    return inputs, contexts, draft, projection


def _two_direct_events(payload):
    event = deepcopy(payload["events"][0])
    event["local_event_id"] = "event_2"
    payload["events"].append(event)
    payload["window_summary"]["event_refs"] = ["event_1", "event_2"]
    candidate = deepcopy(payload["candidate_hypotheses"][0])
    candidate["local_candidate_id"] = "candidate_2"
    candidate["anchor_event_ref"] = "event_2"
    candidate["supporting_event_refs"] = candidate["payoff_event_refs"] = ["event_2"]
    for measurement in candidate["measurements"]:
        measurement["event_refs"] = ["event_2"]
    payload["candidate_hypotheses"].append(candidate)


@pytest.fixture(scope="module")
def two_events():
    return _custom_case(_two_direct_events)


def _full_pool_projection(case, *, satisfaction="one_of", split_alternatives=False):
    catalog = case[0].portfolio.values.business.candidate_catalog
    catalog_owner = SemanticMemberIdentity.from_artifact_member(case[0].portfolio.values.members[0])
    refs = tuple(SemanticObjectRef(catalog_owner, "candidate", item.candidate_id) for item in catalog.candidates)
    events = tuple(dict.fromkeys(item.anchor_event.event_card_ref for item in catalog.candidates))
    first = case[2].stories[0].beats[0].evidence_requirements[0]
    alternatives = tuple(EvidenceAlternative(f"choice-{index}", (event,), (ref,))
                         for index, (event, ref) in enumerate(zip(events, refs, strict=True))) if split_alternatives else (
        EvidenceAlternative("whole", events, refs),)
    return _change_first_beat(case, evidence_requirements=(replace(first, satisfaction=satisfaction, alternative_sets=alternatives),))


def test_actual_two_story_material_and_rational_timing_witnesses(case):
    result = _evaluate(case)
    assert result.status == "feasible" and len(result.timing_witnesses) == 2
    assert tuple(row.story_id for row in result.timing_witnesses) == case[0].portfolio.values.business.portfolio.target_story_ids
    assert all(row.durations == (Fraction(1),) for row in result.timing_witnesses)
    assert len(result.material_search.choices) == 2
    _verify(case, result)
    assert _evaluate(case) == result
    assert EditorialFeasibilityResult.from_mapping(result.to_mapping()) == result
    assert result.canonical_hash == canonical_json_hash(result.to_mapping())
    assert "physical_safety" not in result.to_mapping()


def test_positive_verifier_uses_no_evaluator_search_or_timing_solver(case, monkeypatch):
    result = _evaluate(case)

    def forbidden(*args, **kwargs):
        raise RuntimeError("independent verification called a solver")

    for name in ("evaluate_editorial_feasibility", "search_editorial_materials", "solve_editorial_timing"):
        monkeypatch.setattr(owner, name, forbidden)
    _verify(case, result)


@pytest.mark.parametrize("bad", [True, False, 0, -1, 1.0, "2", None, 1_000_001, 2**53])
def test_search_policy_requires_explicit_bounded_integer(bad):
    with pytest.raises(ValueError):
        EditorialFeasibilityPolicy("editorial-material-feasibility-v1", bad)


def test_closed_policy_result_and_timing_values_are_immutable_and_fresh(case):
    result = _evaluate(case)
    for value in (POLICY, result, result.timing_witnesses[0]):
        assert type(value).from_mapping(value.to_mapping()) == value
        mapping = value.to_mapping()
        for key in mapping:
            with pytest.raises(ValueError):
                type(value).from_mapping({name: item for name, item in mapping.items() if name != key})
        with pytest.raises(ValueError):
            type(value).from_mapping({**mapping, "fulfilled": True})
    with pytest.raises(TypeError):
        EditorialFeasibilityPolicy()
    with pytest.raises(ValueError):
        replace(POLICY, strategy_version="other")
    with pytest.raises(FrozenInstanceError):
        result.policy_sha256 = "sha256:" + "f" * 64
    mapping = result.to_mapping()
    mapping["timing_witnesses"][0]["durations"][0]["numerator_decimal"] = "999"
    assert result.to_mapping() != mapping


@pytest.mark.parametrize("value", ["01", "0", "-1", "+1", "1.0", "NaN", "١", True, 1, "1" * 65_537])
def test_exact_rational_wire_rejects_noncanonical_and_unbounded_components(value):
    with pytest.raises(ValueError):
        EditorialTimingWitness.from_mapping({"story_id": "sha256:" + "a" * 64,
            "durations": [{"numerator_decimal": value, "denominator_decimal": "1"}]})


def test_rational_wire_handles_denominators_beyond_json_and_python_conversion_limits():
    value = EditorialTimingWitness("sha256:" + "a" * 64, (Fraction(10**5000 + 1, 10**5000 + 3),))
    assert EditorialTimingWitness.from_mapping(value.to_mapping()) == value
    with pytest.raises(ValueError):
        EditorialTimingWitness.from_mapping({"story_id": value.story_id, "durations": [
            {"numerator_decimal": "2", "denominator_decimal": "4"}]})
    for bad in ((1,), (1.0,), (), [Fraction(1)], (Fraction(0),), (Fraction(-1),)):
        with pytest.raises(ValueError):
            EditorialTimingWitness(value.story_id, bad)


def test_subset_union_covers_distinct_overlapping_events_without_summing_duration(two_events):
    projection = _full_pool_projection(two_events)
    result = _evaluate(two_events, projection)
    assert result.status == "feasible"
    assert len(result.material_search.choices[0].candidate_keys) == 2
    _verify(two_events, result, projection)
    # Output intent may exceed each candidate and their summed coarse duration:
    # the witness never claims footprint capacity or final physical allocation.
    long = _change_first_beat(two_events, duration_seconds=DurationRange(9, 9, 10))
    assert _evaluate(two_events, long).timing_witnesses[0].durations == (Fraction(9),)


@pytest.mark.parametrize("satisfaction,count", [("one_of", 1), ("all_of", 2)])
def test_whole_alternative_semantics_are_preserved(two_events, satisfaction, count):
    projection = _full_pool_projection(two_events, satisfaction=satisfaction, split_alternatives=True)
    result = _evaluate(two_events, projection)
    first_story = projection.blueprints[0].story_id
    assert len([choice for choice in result.material_search.choices if choice.story_id == first_story]) == count
    _verify(two_events, result, projection)


def test_one_of_cannot_borrow_coverage_across_incomplete_alternatives(two_events):
    base = _full_pool_projection(two_events)
    beat = base.blueprints[0].beats[0]
    whole = beat.evidence_requirements[0].alternatives[0]
    incomplete = tuple(replace(whole, alternative_id=f"incomplete-{index}", candidate_refs=(ref,))
                       for index, ref in enumerate(whole.candidate_refs))
    projection = _change_first_beat(two_events, evidence_requirements=(replace(
        two_events[2].stories[0].beats[0].evidence_requirements[0], alternative_sets=incomplete),))
    assert _evaluate(two_events, projection).status == "infeasible"


def test_budget_exhaustion_is_indeterminate_without_partial_choices(case):
    result = _evaluate(case, policy=replace(POLICY, max_search_states=1))
    assert result.status == "indeterminate" and result.material_search.choices == ()
    with pytest.raises(ValueError, match="positive witness"):
        _verify(case, result, policy=replace(POLICY, max_search_states=1))


def test_timing_infeasibility_is_not_material_or_physical_success(case):
    draft = case[2]
    story = draft.stories[0]
    impossible = replace(story, story_duration_seconds=DurationRange(1, 1, 1),
                         beats=(replace(story.beats[0], duration_seconds=DurationRange(2, 2, 2)),))
    projection = _project(case, replace(draft, stories=(impossible, *draft.stories[1:])))
    result = _evaluate(case, projection)
    assert result.status == "infeasible" and result.timing_witnesses[0].durations is None
    with pytest.raises(ValueError, match="positive witness"):
        _verify(case, result, projection)


@pytest.mark.parametrize("field", ["input_binding_sha256", "projection_sha256", "policy_sha256"])
def test_result_hash_tampering_rejected_by_independent_verifier(case, field):
    with pytest.raises(ValueError, match="positive witness"):
        _verify(case, replace(_evaluate(case), **{field: "sha256:" + "f" * 64}))


def test_independent_verifier_rejects_bad_duration_target_order_candidate_and_partial_choice(case):
    result = _evaluate(case)
    bad_duration = replace(result.timing_witnesses[0], durations=(Fraction(100),))
    choice = result.material_search.choices[0]
    bad_choice = replace(choice, candidate_keys=("sha256:" + "f" * 64,))
    for changed in (
        replace(result, timing_witnesses=(bad_duration, result.timing_witnesses[1])),
        replace(result, timing_witnesses=tuple(reversed(result.timing_witnesses))),
        replace(result, material_search=replace(result.material_search, choices=(bad_choice, result.material_search.choices[1]))),
        replace(result, material_search=replace(result.material_search, choices=result.material_search.choices[:1])),
    ):
        with pytest.raises(ValueError):
            _verify(case, changed)


def test_preferences_are_not_catalog_wide_permissions_or_search_order(two_events):
    beat = two_events[2].stories[0].beats[0]
    current = beat.evidence_requirements[0].alternative_sets[0].candidate_refs[0]
    catalog = two_events[0].portfolio.values.business.candidate_catalog
    foreign_pool = next(item for item in catalog.candidates if item.candidate_id != current.object_id)
    ref = replace(current, object_id=foreign_pool.candidate_id)
    with pytest.raises(ValueError, match="preferences"):
        projection = _change_first_beat(two_events, candidate_preferences=(ref,))
        _evaluate(two_events, projection)
    full = _full_pool_projection(two_events)
    choices = _evaluate(two_events, full).material_search.choices
    story = full.blueprints[0]
    changed = replace(full, blueprints=(replace(story, beats=(replace(story.beats[0], candidate_preferences=tuple(reversed(
        story.beats[0].evidence_requirements[0].alternatives[0].candidate_refs))),)), *full.blueprints[1:]))
    assert _evaluate(two_events, changed).material_search.choices == choices


def test_unsupported_narrative_function_and_unknown_candidate_refs_reject_entire_declaration(case):
    projection = _change_first_beat(case, narrative_function=VlmNarrativeFunction.AFTERMATH)
    with pytest.raises(ValueError, match="narrative function"):
        _evaluate(case, projection)
    projection = case[3]
    story = projection.blueprints[0]
    row = story.beats[0].evidence_requirements[0]
    alt = row.alternatives[0]
    bad = replace(alt, candidate_refs=(replace(alt.candidate_refs[0], object_id="unknown"),))
    beat = replace(story.beats[0], candidate_preferences=(), evidence_requirements=(replace(row, alternatives=(bad,)),))
    with pytest.raises(ValueError):
        _evaluate(case, replace(projection, blueprints=(replace(story, beats=(beat,)), *projection.blueprints[1:])))


def test_caller_cannot_relax_frozen_source_reuse_policy(case):
    with pytest.raises(ValueError, match="JobPolicy"):
        _evaluate(case, job_policy=replace(case[1].job_policy, source_reuse_policy="forbid"))


def _rewrite(member, value):
    raw = canonical_json_bytes(value.to_mapping()).decode()
    return replace(member, payload_json=raw, content_hash=canonical_payload_hash(raw))


def test_rehashed_predecessor_claim_cannot_hide_joint_source_conflict(case):
    # Deliberately forged Stage 2 control claim, not an actual accepted run.
    # Rehash all affected DAG members so only the real joint domain can reject.
    inputs, contexts, _, projection = case
    stage2 = inputs.portfolio.values
    job = replace(contexts.job_policy, source_reuse_policy="forbid")
    members = list(stage2.members)
    members[2] = _rewrite(members[2], replace(stage2.business.portfolio, job_policy_sha256=job.canonical_hash))
    members[3] = _rewrite(members[3], replace(stage2.business.source_usage_ledger,
        portfolio_ref=SemanticMemberIdentity.from_artifact_member(members[2])))
    admission = replace(stage2.admission, job_policy_sha256=job.canonical_hash,
                        business_members=tuple(SemanticMemberIdentity.from_artifact_member(item) for item in members[:4]))
    members[4] = _rewrite(members[4], admission)
    forged = decode_story_design_members(tuple(members), scope=members[0].scope)
    result = evaluate_editorial_feasibility(inputs.narrative.values, forged, projection,
        semantic=inputs.semantic, job_policy=job, policy=POLICY)
    assert result.status == "infeasible" and not result.material_search.choices


def test_exact_typed_predecessor_view_and_copied_material_fields_cannot_drift(case):
    inputs, contexts, _, projection = case
    changed = replace(inputs.narrative.values, admission=replace(inputs.narrative.values.admission,
        raw_draft_sha256="sha256:" + "f" * 64))
    with pytest.raises(ValueError, match="predecessors"):
        evaluate_editorial_feasibility(changed, inputs.portfolio.values, projection,
            semantic=inputs.semantic, job_policy=contexts.job_policy, policy=POLICY)
    story = projection.blueprints[0]
    beat = story.beats[0]
    row = replace(beat.evidence_requirements[0], minimum_usable_seconds=2)
    changed = replace(projection, blueprints=(replace(story, beats=(replace(beat, evidence_requirements=(row,)),)), *projection.blueprints[1:]))
    with pytest.raises(ValueError, match="selected Proposal"):
        _evaluate(case, changed)


@pytest.mark.parametrize("one_complete", [False, True])
def test_two_partial_event_intervals_cannot_be_unioned_into_complete_support(one_complete):
    def change(payload):
        payload["events"][0]["support"]["proxy_interval"] = {"start_pts": 10, "end_pts": 90, "uncertainty_pts": 0}
        first = payload["candidate_hypotheses"][0]
        first["support"]["proxy_interval"] = {"start_pts": 10, "end_pts": 60, "uncertainty_pts": 0}
        second = deepcopy(first)
        second["local_candidate_id"] = "candidate_2"
        second["support"]["proxy_interval"] = {"start_pts": 10 if one_complete else 20, "end_pts": 90, "uncertainty_pts": 0}
        payload["candidate_hypotheses"].append(second)

    case = _custom_case(change, selected_story_count=1)
    # Both satisfy every Stage2 Fact/minimum; even their duration lower bounds
    # cannot turn two partial Event ranges into one whole Event-support edge.
    assert case[0].narrative.values.admission.next_action == "continue"
    support = case[0].portfolio.values.business.proposal_set.proposals[0].requirements[0]
    assert len(support.alternatives) == 2
    projection = _full_pool_projection(case)
    result = _evaluate(case, projection)
    assert result.status == ("feasible" if one_complete else "infeasible")
    if one_complete:
        assert len(result.material_search.choices[0].candidate_keys) == 1
        _verify(case, result, projection)
    else:
        assert result.material_search.choices == ()


def test_context_only_event_is_not_complete_direct_support():
    def change(payload):
        _two_direct_events(payload)
        payload["candidate_hypotheses"][0]["context_event_refs"] = ["event_2"]

    case = _custom_case(change, selected_story_count=1)
    catalog = case[0].portfolio.values.business.candidate_catalog
    candidate = next(item for item in catalog.candidates if item.local_candidate_id == "candidate_1")
    catalog_owner = SemanticMemberIdentity.from_artifact_member(case[0].portfolio.values.members[0])
    candidate_ref = SemanticObjectRef(catalog_owner, "candidate", candidate.candidate_id)
    row = case[2].stories[0].beats[0].evidence_requirements[0]
    alternative = EvidenceAlternative("context-is-not-direct", (candidate.context_events[0].event_card_ref,), (candidate_ref,))
    projection = _change_first_beat(case, evidence_requirements=(replace(row, alternative_sets=(alternative,)),))
    assert _evaluate(case, projection).status == "infeasible"


@pytest.mark.parametrize("bad_kind", ["ineligible", "function"])
def test_bad_declared_pool_member_rejected_even_when_another_whole_alternative_is_feasible(bad_kind):
    def change(payload):
        _two_direct_events(payload)
        bad = payload["candidate_hypotheses"][1]
        if bad_kind == "ineligible":
            bad["support"]["confidence"] = "0.1"
        else:
            bad["narrative_functions"] = ["aftermath"]

    case = _custom_case(change, selected_story_count=1)
    projection = _full_pool_projection(case, split_alternatives=True)
    with pytest.raises(ValueError, match="eligible|narrative function"):
        _evaluate(case, projection)


def test_missing_render_source_grant_never_reaches_search(case, monkeypatch):
    inputs = case[0].semantic
    grant = replace(inputs.source_grant, policy=replace(inputs.source_grant.policy, authorized_purposes=("semantic_analysis",)))

    def forbidden(*args, **kwargs):
        raise RuntimeError("invalid Source grant reached search")

    monkeypatch.setattr(owner, "search_editorial_materials", forbidden)
    with pytest.raises(ValueError, match="render_source"):
        _evaluate(case, semantic=replace(inputs, source_grant=grant))


@pytest.mark.parametrize("unknown", [False, True])
def test_material_domain_rejects_actual_dependency_reachability_or_unknown_frontier(case, unknown):
    # Focused pure-domain probe: recompute the real graph with an injected seed.
    # This is deliberately not an admitted predecessor, and the public entry
    # also rejects its mismatch with committed members before reaching this seam.
    inputs, contexts, _, _ = case
    stage1, stage2 = inputs.narrative.values, inputs.portfolio.values
    candidates = owner._candidate_domain(inputs.semantic, stage1, stage2)
    support = stage2.business.proposal_set.proposals[0].requirements[0]
    proof = support.alternatives[0]
    candidate = candidates[proof.candidate_ref]
    event = candidate.candidate.anchor_event.event_card_ref
    analysis = stage1.dependency_proof.analysis
    recomputed = analyze_dependency_graph(analysis.node_refs, analysis.arcs,
        (DependencySeed("synthetic-taint", (event,), (event,) if unknown else ()),))
    tainted = replace(stage1, dependency_proof=replace(stage1.dependency_proof, analysis=recomputed))
    with pytest.raises(ValueError, match="tainted or unknown"):
        owner._material_proof(candidate, proof, support, tainted, job_policy=contexts.job_policy,
            constraints=stage2.business.proposal_set.proposals[0].proposal.material_requirements[0].source_constraints)
    with pytest.raises(ValueError, match="predecessors"):
        evaluate_editorial_feasibility(tainted, stage2, case[3], semantic=inputs.semantic,
                                      job_policy=contexts.job_policy, policy=POLICY)
