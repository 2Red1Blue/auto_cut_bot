"""Synthetic material evidence values; these fixtures are not Store authority."""

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from fractions import Fraction

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.media.stage4_predecessor import RationalPresentationInterval
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.pipeline.build_narrative_graph_command import BuildNarrativeGraphCommand
from autocut_kernel.semantic_chain.candidate_catalog import CandidateCatalogPolicy, CandidateSupport
from autocut_kernel.semantic_chain.candidate_duration import (
    ConservativeDuration,
    conservative_support_bounds,
    conservative_support_duration,
)
from autocut_kernel.semantic_chain.candidate_projection import project_candidate_catalog
from autocut_kernel.semantic_chain.material_support import (
    _Candidate,
    _candidate_policy_reasons,
    _Fact,
    _fact_context,
    _requirement,
    _source_context,
    evaluate_material_support,
)
from autocut_kernel.semantic_chain.material_support_models import (
    ExclusionReasonCount,
    FactCarryWitness,
    MaterialSupportError,
    MaterialSupportEvaluation,
    ProposalMaterialSupport,
    RequirementAlternativeProof,
    RequirementMaterialSupport,
)
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.story_design_context import story_design_input_binding
from autocut_kernel.semantic_chain.story_design_draft import ProposalDraftSet
from autocut_kernel.semantic_chain.story_design_models import (
    IntegerRange,
    MaterialRequirement,
    SourceConstraints,
)
from autocut_kernel.source_manifest import decode_source_manifest, identity_frame_index
from autocut_kernel.store.models import canonical_payload_hash
from autocut_kernel.vlm import parse_vlm_response
from autocut_kernel.vlm.models import VlmProxyInterval
from autocut_kernel.vlm.window import ProxyTimelineMap

from tests.semantic_chain.test_build_narrative_graph_command import (
    MemoryNarrativeGraphStore,
    ScriptedDraftProvider,
)
from tests.semantic_chain.test_candidate_projection import (
    _bind_payload_supports_to_manifest,
    _command_request,
    _draft_raw,
    _inputs,
    _make_payload_command_ready,
    _stage1,
)
from tests.semantic_chain.test_story_design_models import (
    GRAPH,
    SCOPE,
    SOURCE_REF,
    _job_policy,
    _proposal,
    _story_policy,
)
from tests.vlm.test_parser import _context, _payload, _raw

BINDING = "sha256:" + "c" * 64
CATALOG = SemanticMemberIdentity("candidate_catalog", "candidate_catalog", 1, SCOPE, "sha256:" + "d" * 64)
VLM = SemanticMemberIdentity("vlm_semantic_pack", "pack", 1, SCOPE, "sha256:" + "e" * 64)
CARDS = SemanticMemberIdentity("event_card_set", "event_card_set", 1, SCOPE, "sha256:" + "f" * 64)
LEDGER = SemanticMemberIdentity("coverage_ledger", "coverage_ledger", 1, SCOPE, "sha256:" + "1" * 64)
CANDIDATE = SemanticObjectRef(CATALOG, "candidate", "candidate-one")
FACT = _proposal().required_fact_refs[0]
WITNESS = FactCarryWitness(FACT, SemanticObjectRef(VLM, "vlm_fact", FACT.object_id),
                           (SemanticObjectRef(CARDS, "event", "event-one"),))
ALTERNATIVE = RequirementAlternativeProof(CANDIDATE, SOURCE_REF, (WITNESS,), ConservativeDuration(20, 1))


def _row():
    requirement = _proposal().material_requirements[0]
    return RequirementMaterialSupport(requirement.requirement_id, (FACT,), 12,
                                      requirement.physical_requirements_hash, (ALTERNATIVE,), (), (), 1)


def _proposal_row():
    return ProposalMaterialSupport(0, _proposal(), (_row(),), (), False)


def _evaluation():
    return MaterialSupportEvaluation(BINDING, ProposalDraftSet(BINDING, (_proposal(),)).canonical_hash,
                                     CATALOG, "sha256:" + "2" * 64, (_proposal_row(),))


def _long_material_inputs(payload_change=None):
    """Create a different synthetic ten-second Source, then reparse its VLM.

    All changed clocks, indexes, presentation facts, manifests, request and
    payload hashes are rebuilt and passed through the production Source codec.
    The reused Blob identities denote fake test media, not actual file probes.
    """
    inputs = _inputs(command_ready=True)
    source = inputs.source_manifest
    decoded = decode_source_manifest(source.payload_json, source.proxy_blobs)
    episode = decoded.episodes[0]
    base = TimeBase(1, 10)
    probe = replace(episode.media_probe, video_probe=replace(episode.media_probe.video_probe,
                    video_stream=replace(episode.media_probe.video_probe.video_stream, time_base=base)))
    frame_index = identity_frame_index(probe)
    manifest = replace(episode.manifest, source_time_base=base, frame_pts_index_set=frame_index,
                       timeline_map=ProxyTimelineMap.translation(time_base=base, proxy_range=TickRange(0, 100), source_start_pts=0))
    manifest_set = replace(episode.manifest_set, source_time_base=base, manifests=(manifest,))
    presentation = probe.presentation_timeline_probe
    assert presentation is not None
    video = replace(presentation.video, time_base=base, index_sha256=frame_index.canonical_hash,
                    segments=tuple(replace(segment, presentation_interval=RationalPresentationInterval.from_fractions(
                        Fraction(segment.stream_tick_range.start_pts, 10), Fraction(segment.stream_tick_range.end_pts, 10),
                    )) for segment in presentation.video.segments))
    presentation = replace(presentation, video=video, frame_pts_index_set_sha256=frame_index.canonical_hash,
                           source_proxy_timeline_map_sha256=manifest.timeline_map.canonical_hash,
                           window_manifest_sha256=manifest.canonical_hash)
    probe = replace(probe, presentation_timeline_probe=presentation)
    decoded = replace(decoded, episodes=(replace(episode, media_probe=probe, manifest=manifest, manifest_set=manifest_set),))
    payload_json = canonical_json_bytes(decoded.to_mapping()).decode()
    assert decode_source_manifest(payload_json, source.proxy_blobs) == decoded
    source = replace(source, payload_json=payload_json,
                     reference=replace(source.reference, content_hash=canonical_payload_hash(payload_json)))
    committed = inputs.inputs[0]
    identity = replace(committed.request_identity, window_manifest_sha256=manifest.canonical_hash,
                       window_manifest_set_sha256=manifest_set.canonical_hash,
                       frame_pts_index_set_sha256=frame_index.canonical_hash)
    identity.assert_manifest_binding(manifest, manifest_set)
    payload = _payload(manifest)
    _bind_payload_supports_to_manifest(payload, manifest)
    _make_payload_command_ready(payload)
    for candidate in payload["candidate_hypotheses"]:
        candidate["support"]["proxy_interval"] = {"start_pts": 10, "end_pts": 80, "uncertainty_pts": 0}
    if payload_change is not None:
        payload_change(payload)
    raw = _raw(payload)
    parse_policy = _context()[2]
    pack = parse_vlm_response(raw, manifest=manifest, manifest_set=manifest_set,
                              request_identity=identity, policy=parse_policy)
    old_child = committed.semantic_pack.source_child
    request_payload = json.loads(old_child.payload_json)
    changes = {"window_manifest_sha256": manifest.canonical_hash,
               "window_manifest_set_sha256": manifest_set.canonical_hash,
               "source_manifest_sha256": source.reference.content_hash,
               "source_provenance_sha256": source.canonical_hash,
               "request_identity_sha256": identity.canonical_hash}
    request_payload.update(changes)
    request_payload["request_identity"] = identity.to_mapping()
    request_json = canonical_json_bytes(request_payload).decode()
    child = replace(old_child, **changes, payload_json=request_json, reference=replace(old_child.reference,
                    logical_id=f"vlm_request_{manifest.canonical_hash[7:31]}", content_hash=canonical_payload_hash(request_json)))
    pack_json = canonical_json_bytes(pack.to_mapping()).decode()
    persisted = replace(committed.semantic_pack, semantic_pack=pack, source_child=child, payload_json=pack_json,
                        reference=replace(committed.semantic_pack.reference, logical_id=f"semantic_pack_{manifest.canonical_hash[7:39]}",
                                          content_hash=canonical_payload_hash(pack_json)))
    committed = replace(committed, semantic_pack=persisted, request_identity=identity,
                        source_window=replace(committed.source_window, window_manifest_sha256=manifest.canonical_hash,
                                              window_manifest_set_sha256=manifest_set.canonical_hash),
                        response_record=replace(committed.response_record, logical_id=f"vlm_response_{manifest.canonical_hash[7:31]}"),
                        raw_response=replace(committed.raw_response, content_hash=pack.raw_response_sha256, byte_length=len(raw)))
    return replace(inputs, source_manifest=source, inputs=(committed,))


def material_case(*, payload_change=None):
    """Synthetic decoded Source/VLM + actual Stage 1 Command, using only fake IO.

    This fixture is not evidence of a real Store, provider or media acceptance.
    All semantic algorithms, source decoders and member codecs run unmocked.
    """
    inputs = _long_material_inputs(payload_change)
    store = MemoryNarrativeGraphStore(inputs)
    result = BuildNarrativeGraphCommand(store, ScriptedDraftProvider(_draft_raw(inputs))).execute(_command_request(inputs))
    assert result.outcome.state == "succeeded" and result.committed is not None
    stage1 = result.committed.values
    assert stage1.admission.next_action == "continue"
    candidate_policy = CandidateCatalogPolicy("candidate-catalog-v1", "0.5", ("reveal_strength",))
    projection = project_candidate_catalog(inputs, stage1, scope=inputs.source_manifest.reference.scope,
                                           revision=1, policy=candidate_policy)
    graph = stage1.coverage.identity("narrative_graph")
    nodes = stage1.coverage.narrative_graph.nodes
    obligation = next(node for node in nodes if node.node_type == "obligation")
    thread = next(node for node in nodes if node.node_type == "story_thread")
    obligation_ref = SemanticObjectRef(graph, "obligation", obligation.node_id)
    facts = tuple(sorted((SemanticObjectRef(graph, "fact", fact_id)
                          for fact_id in obligation.attributes.required_fact_ids),
                         key=lambda ref: canonical_json_bytes(ref.to_mapping())))
    source = projection.catalog.candidates[0].source_ref
    constraints = SourceConstraints((source,), (), "render_source")
    story = _story_policy()
    job = replace(_job_policy(), story_design_policy_sha256=story.canonical_hash,
                  proposal_count=IntegerRange(2, 2), target_duration_seconds=IntegerRange(1, 10),
                  source_constraints=constraints)
    requirement = replace(_proposal().material_requirements[0], obligation_ref=obligation_ref,
                          minimum_usable_seconds=1, source_constraints=constraints)
    proposal = replace(_proposal(), thread_refs=(SemanticObjectRef(graph, "story_thread", thread.node_id),),
                       required_obligation_refs=(obligation_ref,), required_fact_refs=facts,
                       material_requirements=(requirement,), target_duration_seconds=IntegerRange(1, 10))
    binding = story_design_input_binding(stage1, projection, job_policy=job,
                                        story_policy=story, candidate_policy=candidate_policy)
    draft = ProposalDraftSet(binding, tuple(replace(proposal, proposal_id=f"proposal-{index}") for index in range(2)))
    return {"inputs": inputs, "stage1": stage1, "projection": projection, "draft": draft,
            "job_policy": job, "story_policy": story, "candidate_policy": candidate_policy}


def _values():
    return (WITNESS, ALTERNATIVE, ExclusionReasonCount("duration_insufficient", 1),
            _row(), _proposal_row(), _evaluation())


@pytest.mark.parametrize("value", _values())
def test_closed_immutable_models_roundtrip_without_authority(value):
    mapping = value.to_mapping()
    assert type(value).from_mapping(json.loads(json.dumps(mapping))) == value
    assert canonical_json_bytes(type(value).from_mapping(mapping).to_mapping()) == canonical_json_bytes(mapping)
    with pytest.raises(FrozenInstanceError):
        setattr(value, fields(value)[0].name, "changed")
    assert not hasattr(value, "accepted")
    assert not hasattr(value, "story_id")
    hash(value)


@pytest.mark.parametrize("value", _values())
def test_every_field_required_and_producer_claims_rejected(value):
    for key in value.to_mapping():
        mapping = value.to_mapping()
        del mapping[key]
        with pytest.raises(ValueError):
            type(value).from_mapping(mapping)
    for key in ("accepted", "pass", "story_id", "selected", "physical_fulfilled"):
        with pytest.raises(ValueError):
            type(value).from_mapping({**value.to_mapping(), key: True})
    with pytest.raises(ValueError):
        type(value).from_mapping([])


@pytest.mark.parametrize("value", (_row(), _proposal_row()))
def test_status_is_derived_not_a_producer_choice(value):
    assert value.status == "supported"
    for status in ("unsupported", "indeterminate", True, None):
        with pytest.raises(ValueError, match="status"):
            type(value).from_mapping({**value.to_mapping(), "status": status})


def test_unsupported_and_unknown_are_distinct_and_empty_universe_is_known_unsupported():
    unsupported = replace(_row(), alternatives=(), exclusion_reason_counts=(
        ExclusionReasonCount("fact_not_declared", 1),
    ))
    unknown = replace(_row(), alternatives=(), exclusion_reason_counts=(
        ExclusionReasonCount("dependency_frontier_unknown", 1),
    ))
    empty = replace(_row(), alternatives=(), examined_candidate_count=0)
    assert unsupported.status == empty.status == "unsupported"
    assert unknown.status == "indeterminate"
    assert replace(_proposal_row(), requirements=(unknown,), dependency_unknown=True).status == "indeterminate"
    # One proven alternative suffices even if a different candidate is unknown.
    mixed = replace(_row(), examined_candidate_count=2, exclusion_reason_counts=(
        ExclusionReasonCount("dependency_frontier_unknown", 1),
    ))
    assert mixed.status == "supported"


def test_no_primary_reason_count_can_disappear_or_double_count_candidate_disposition():
    for changes in ({"examined_candidate_count": 2}, {"alternatives": ()},
                    {"exclusion_reason_counts": (ExclusionReasonCount("duration_insufficient", 1),)}):
        with pytest.raises(ValueError, match="conserve"):
            replace(_row(), **changes)
    with pytest.raises(ValueError):
        ExclusionReasonCount("free_form_excuse", 1)
    with pytest.raises(ValueError):
        replace(_row(), alternatives=(), examined_candidate_count=2, exclusion_reason_counts=(
            ExclusionReasonCount("duration_insufficient", 1), ExclusionReasonCount("duration_insufficient", 1),
        ))


@pytest.mark.parametrize("bad", [True, False, 1.0, "1", -1, 2**53, None])
def test_count_and_index_fields_are_actual_safe_integers(bad):
    with pytest.raises(ValueError):
        ExclusionReasonCount("duration_insufficient", bad)
    with pytest.raises(ValueError):
        replace(_row(), examined_candidate_count=bad)
    with pytest.raises(ValueError):
        replace(_proposal_row(), proposal_index=bad)


def test_exact_owner_kind_scope_and_fact_identity_closure():
    for bad in (replace(WITNESS.graph_fact_ref, member_ref=VLM),
                replace(WITNESS.graph_fact_ref, object_type="event"),
                replace(WITNESS.graph_fact_ref, object_id="wrong-fact")):
        with pytest.raises(ValueError):
            replace(WITNESS, graph_fact_ref=bad)
    with pytest.raises(ValueError):
        replace(WITNESS, via_event_refs=())
    with pytest.raises(ValueError):
        replace(WITNESS, via_event_refs=(SemanticObjectRef(GRAPH, "event", "event-one"),))
    with pytest.raises(ValueError):
        replace(ALTERNATIVE, candidate_ref=replace(CANDIDATE, member_ref=VLM, object_type="vlm_candidate"))
    with pytest.raises(ValueError):
        replace(ALTERNATIVE, source_ref=replace(SOURCE_REF, member_ref=replace(
            SOURCE_REF.member_ref, scope=replace(SCOPE, key="foreign"),
        )))


def test_supported_witness_cannot_omit_a_fact_or_supply_insufficient_duration():
    with pytest.raises(ValueError, match="complete facts"):
        replace(_row(), alternatives=(replace(ALTERNATIVE, fact_witnesses=()),))
    with pytest.raises(ValueError, match="duration"):
        replace(_row(), alternatives=(replace(ALTERNATIVE, conservative_duration=ConservativeDuration(11, 1)),))
    with pytest.raises(ValueError):
        replace(_row(), required_fact_refs=())
    with pytest.raises(ValueError):
        replace(ALTERNATIVE, fact_witnesses=(WITNESS, WITNESS))


def test_tainted_candidates_cannot_appear_among_safe_alternatives():
    with pytest.raises(ValueError, match="tainted"):
        replace(_row(), excluded_tainted_candidate_refs=(CANDIDATE,))
    other = replace(CANDIDATE, object_id="candidate-two")
    row = replace(_row(), examined_candidate_count=2, excluded_tainted_candidate_refs=(other,),
                  exclusion_reason_counts=(ExclusionReasonCount("candidate_tainted", 1),))
    assert row.status == "supported"
    assert RequirementMaterialSupport.from_mapping(row.to_mapping()) == row


@pytest.mark.parametrize("excluded", [(), (CANDIDATE,)])
def test_primary_taint_count_requires_enough_explicit_tainted_refs_in_direct_and_wire(excluded):
    counts = (ExclusionReasonCount("candidate_tainted", len(excluded) + 1),)
    with pytest.raises(MaterialSupportError, match="primary taint count"):
        replace(_row(), alternatives=(), excluded_tainted_candidate_refs=excluded,
                exclusion_reason_counts=counts, examined_candidate_count=len(excluded) + 1)
    mapping = _row().to_mapping()
    mapping.update(alternatives=[], excluded_tainted_candidate_refs=[ref.to_mapping() for ref in excluded],
                   exclusion_reason_counts=[item.to_mapping() for item in counts],
                   examined_candidate_count=len(excluded) + 1, status="unsupported")
    with pytest.raises(MaterialSupportError, match="primary taint count"):
        RequirementMaterialSupport.from_mapping(mapping)


@pytest.mark.parametrize("reason", ["candidate_confidence_below_policy", "source_forbidden"])
def test_other_primary_reason_can_retain_a_tainted_reference(reason):
    row = replace(_row(), alternatives=(), excluded_tainted_candidate_refs=(CANDIDATE,),
                  exclusion_reason_counts=(ExclusionReasonCount(reason, 1),))
    assert row.status == "unsupported"
    assert RequirementMaterialSupport.from_mapping(row.to_mapping()) == row


def test_every_original_proposal_and_requirement_survives_with_bound_physical_hash():
    for changes in ({"requirements": ()}, {"requirements": (_row(), _row())},
                    {"requirements": (replace(_row(), requirement_id="other"),)},
                    {"requirements": (replace(_row(), physical_requirements_hash="sha256:" + "3" * 64),)}):
        with pytest.raises(ValueError):
            replace(_proposal_row(), **changes)
    assert _proposal_row().proposal == _proposal()
    assert _row().physical_requirements_hash == _proposal().material_requirements[0].physical_requirements_hash


def test_bound_draft_hash_original_indexes_and_catalog_identity_cannot_drift():
    value = _evaluation()
    for changes in ({"draft_sha256": "sha256:" + "4" * 64},
                    {"input_binding_sha256": "sha256:" + "5" * 64},
                    {"proposals": (replace(_proposal_row(), proposal_index=1),)},
                    {"candidate_catalog_ref": replace(CATALOG, revision=2)}):
        with pytest.raises(ValueError):
            replace(value, **changes)
    with pytest.raises(ValueError):
        replace(value, proposals=(replace(_proposal_row(), dependency_unknown=True),))


@pytest.mark.parametrize("value", _values())
def test_actual_tuple_types_utf8_and_fresh_deep_mapping(value):
    for field in fields(value):
        item = getattr(value, field.name)
        if type(item) is tuple:
            with pytest.raises(ValueError):
                replace(value, **{field.name: list(item)})
        elif type(item) is str:
            for bad in ("", " ", "\ud800", None, True):
                with pytest.raises(ValueError):
                    replace(value, **{field.name: bad})
    mapping = value.to_mapping()
    mapping.clear()
    assert value.to_mapping()


def test_serialization_is_compact_in_negative_catalog_cardinality():
    # A million inspected ineligible candidates produce one count, not a million
    # copied negative candidate records. This is a DTO size probe, not evaluation.
    row = replace(_row(), alternatives=(), examined_candidate_count=1_000_000,
                  exclusion_reason_counts=(ExclusionReasonCount("fact_not_declared", 1_000_000),))
    assert len(canonical_json_bytes(row.to_mapping())) < 1000
    assert "assessments" not in row.to_mapping()


@pytest.fixture(scope="module")
def predicate_case():
    """Isolated typed predicate fixture, NOT an accepted Catalog/Source fixture.

    Its time base is deliberately adjusted for readable 10-second examples;
    the public evaluator would reject that change against the original Source.
    No whole evaluator or timing algorithm is mocked.
    """
    inputs = _inputs()
    stage1 = _stage1(inputs)
    projection = project_candidate_catalog(inputs, stage1, scope=inputs.source_manifest.reference.scope,
                                           revision=1, policy=CandidateCatalogPolicy("candidate-catalog-v1", "0.01", ()))
    committed = inputs.inputs[0]
    pack = committed.semantic_pack.semantic_pack
    raw = pack.candidate_hypotheses[0]
    candidate = next(item for item in projection.catalog.candidates if item.candidate_id == raw.candidate_id)
    event = next(item for item in pack.events if item.event_id == raw.anchor_event_ref)
    fact = next(item for item in pack.facts if item.fact_id == event.fact_refs[0])
    timeline = ProxyTimelineMap.translation(time_base=TimeBase(1, 10), proxy_range=TickRange(0, 100), source_start_pts=0)

    def support(original, start, end, uncertainty=0):
        proxy = VlmProxyInterval(TickRange(start, end), uncertainty)
        return replace(original, proxy_interval=proxy, source_interval=timeline.map_interval(
            proxy.proxy_range, provider_uncertainty_proxy_pts=uncertainty,
        ))

    raw = replace(raw, support=support(raw.support, 0, 100))
    fact = replace(fact, support=support(fact.support, 20, 40))
    candidate = replace(candidate, support=CandidateSupport.from_vlm_support(
        raw.support, conservative_support_duration(raw.support, timeline),
    ))
    window_id = committed.source_window.window_manifest_sha256
    start, end = conservative_support_bounds(raw.support, timeline)
    item = _Candidate(candidate, raw, committed, {fact.fact_id: {event.event_id}}, frozenset(), start, end, False, ())
    graph_ref = projection.catalog.narrative_graph_member_ref
    fact_ref = SemanticObjectRef(graph_ref, "fact", fact.fact_id)
    obligation = next(node for node in stage1.coverage.narrative_graph.nodes if node.node_type == "obligation")
    constraints = SourceConstraints((), (), "render_source")
    requirement = MaterialRequirement("requirement-one", SemanticObjectRef(graph_ref, "obligation", obligation.node_id), 1, (), constraints)
    return requirement, {
        "facts": (fact_ref,), "candidates": (item,),
        "raw_facts": {fact.fact_id: _Fact(fact, SemanticObjectRef(candidate.candidate_ref.member_ref, "vlm_fact", fact.fact_id), window_id)},
        "maps": {window_id: timeline},
        "catalog_ref": SemanticMemberIdentity.from_artifact_member(projection.member),
        "card_ref": projection.catalog.event_card_member_ref,
        "job_policy": replace(_job_policy(), source_constraints=constraints), "dependency_unknown": False,
    }


def test_real_timing_predicate_requires_complete_fact_and_independent_duration(predicate_case):
    requirement, arguments = predicate_case
    result = _requirement(requirement, **arguments)
    assert result.status == "supported"
    assert len(result.alternatives) == 1
    proof = result.alternatives[0]
    assert proof.fact_witnesses[0].graph_fact_ref == arguments["facts"][0]
    assert proof.conservative_duration == ConservativeDuration(10, 1)
    assert result.physical_requirements_hash == requirement.physical_requirements_hash
    too_long = _requirement(replace(requirement, minimum_usable_seconds=11), **arguments)
    assert too_long.status == "unsupported"
    assert too_long.exclusion_reason_counts == (ExclusionReasonCount("duration_insufficient", 1),)


@pytest.mark.parametrize("interval", [(0, 30), (30, 60), (0, 100)])
def test_fact_outer_overlap_is_not_complete_carry(predicate_case, interval):
    requirement, arguments = predicate_case
    candidate = arguments["candidates"][0]
    narrowed = replace(candidate, inner_start=20, inner_end=40)
    raw_fact = next(iter(arguments["raw_facts"].values()))
    timeline = arguments["maps"][raw_fact.window_id]
    proxy = VlmProxyInterval(TickRange(*interval), 0)
    fact = replace(raw_fact.value, support=replace(raw_fact.value.support, proxy_interval=proxy,
                   source_interval=timeline.map_interval(proxy.proxy_range, provider_uncertainty_proxy_pts=0)))
    result = _requirement(requirement, **{**arguments, "candidates": (narrowed,),
                          "raw_facts": {fact.fact_id: replace(raw_fact, value=fact)}})
    assert result.status == "unsupported"
    assert result.exclusion_reason_counts == (ExclusionReasonCount("fact_outside_support", 1),)


def test_context_only_and_measurement_refs_never_substitute_for_direct_fact_set(predicate_case):
    requirement, arguments = predicate_case
    candidate = arguments["candidates"][0]
    fact_id = arguments["facts"][0].object_id
    for context, reason in ((frozenset({fact_id}), "fact_context_only"), (frozenset(), "fact_not_declared")):
        changed = replace(candidate, direct_fact_events={}, context_only_facts=context)
        result = _requirement(requirement, **{**arguments, "candidates": (changed,)})
        assert result.status == "unsupported"
        assert result.exclusion_reason_counts == (ExclusionReasonCount(reason, 1),)
    # The candidate's original measurement references are unchanged in both cases.
    assert candidate.value.measurements


@pytest.mark.parametrize("layer", ["job", "requirement"])
@pytest.mark.parametrize("mode", ["allow", "forbid"])
def test_both_source_constraint_layers_are_enforced(predicate_case, layer, mode):
    requirement, arguments = predicate_case
    source = arguments["candidates"][0].value.source_ref
    constraints = (SourceConstraints((replace(source, object_id="another-source"),), (), "render_source")
                   if mode == "allow" else SourceConstraints((), (source,), "render_source"))
    if layer == "job":
        arguments = {**arguments, "job_policy": replace(arguments["job_policy"], source_constraints=constraints)}
    else:
        requirement = replace(requirement, source_constraints=constraints)
    result = _requirement(requirement, **arguments)
    assert result.status == "unsupported"
    assert result.exclusion_reason_counts[0].reason_code == ("source_not_allowed" if mode == "allow" else "source_forbidden")


def test_taint_safe_alternatives_and_unknown_are_preserved_without_top_k(predicate_case):
    requirement, arguments = predicate_case
    original = arguments["candidates"][0]
    candidates = tuple(replace(original, value=replace(original.value,
                       candidate_ref=replace(original.value.candidate_ref, object_id=f"sha256:{index + 3:064x}")),
                       tainted=index == 0) for index in range(5))
    result = _requirement(requirement, **{**arguments, "candidates": candidates})
    assert result.examined_candidate_count == 5
    assert len(result.alternatives) == 4
    assert len(result.excluded_tainted_candidate_refs) == 1
    assert result.exclusion_reason_counts == (ExclusionReasonCount("candidate_tainted", 1),)
    unknown = _requirement(requirement, **{**arguments, "candidates": candidates, "dependency_unknown": True})
    assert not unknown.alternatives
    assert unknown.status == "indeterminate"
    assert unknown.exclusion_reason_counts == (ExclusionReasonCount("candidate_tainted", 1), ExclusionReasonCount("dependency_frontier_unknown", 4))
    known_bad = _requirement(replace(requirement, minimum_usable_seconds=11),
                             **{**arguments, "dependency_unknown": True})
    assert known_bad.status == "unsupported"


def test_public_evaluator_rejects_caller_value_substitutes_before_processing():
    with pytest.raises(MaterialSupportError, match="exact typed"):
        evaluate_material_support({}, {}, {}, {}, job_policy={}, story_policy={}, candidate_policy={})


@pytest.mark.parametrize("kind,reason", [
    ("candidate", "candidate_confidence_below_policy"),
    ("measurement", "measurement_confidence_below_policy"),
    ("missing", "required_measurement_missing"),
])
def test_policy_ineligible_candidate_does_not_remove_normal_alternative(predicate_case, kind, reason):
    requirement, arguments = predicate_case
    original = arguments["candidates"][0]
    policy = CandidateCatalogPolicy("candidate-catalog-v1", "0.5", ("reveal_strength",))
    good = replace(original.value, support=replace(original.value.support, confidence="0.9"),
                   measurements=tuple(replace(item, confidence="0.9") for item in original.value.measurements))
    bad = replace(good, candidate_ref=replace(good.candidate_ref, object_id="sha256:" + "9" * 64))
    if kind == "candidate":
        bad = replace(bad, support=replace(bad.support, confidence="0.49"))
    elif kind == "measurement":
        bad = replace(bad, measurements=tuple(replace(item, confidence="0.49") for item in bad.measurements))
    else:
        bad = replace(bad, measurements=tuple(replace(item, measurement_kind="hook_strength") for item in bad.measurements))
    assert _candidate_policy_reasons(good, policy) == ()
    assert _candidate_policy_reasons(bad, policy) == (reason,)
    candidates = tuple(replace(original, value=value, ineligibility_reasons=_candidate_policy_reasons(value, policy))
                       for value in (good, bad))
    row = _requirement(requirement, **{**arguments, "candidates": candidates})
    assert row.status == "supported"
    assert row.examined_candidate_count == 2
    assert [item.candidate_ref.object_id for item in row.alternatives] == [good.candidate_id]
    assert row.exclusion_reason_counts == (ExclusionReasonCount(reason, 1),)
    unknown = _requirement(requirement, **{**arguments, "candidates": (candidates[1],), "dependency_unknown": True})
    assert unknown.status == "unsupported"
    assert unknown.exclusion_reason_counts == (ExclusionReasonCount(reason, 1),)


def test_policy_threshold_is_inclusive_and_all_measurements_are_checked(predicate_case):
    candidate = predicate_case[1]["candidates"][0].value
    policy = CandidateCatalogPolicy("candidate-catalog-v1", "0.5", ())
    value = replace(candidate, support=replace(candidate.support, confidence="0.5"),
                    measurements=tuple(replace(item, confidence="0.5") for item in candidate.measurements))
    assert _candidate_policy_reasons(value, policy) == ()
    # Even a measurement kind not required by policy cannot carry a subthreshold score.
    low = replace(value, measurements=tuple(replace(item, confidence="0.499") for item in value.measurements))
    assert _candidate_policy_reasons(low, policy) == ("measurement_confidence_below_policy",)


def test_evaluation_has_independent_canonical_hash_oracle_and_fresh_nested_mappings():
    evaluation = _evaluation()
    raw = json.dumps(evaluation.to_mapping(), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode()
    assert evaluation.canonical_hash == "sha256:" + hashlib.sha256(raw).hexdigest()
    mapping = evaluation.to_mapping()
    mapping["proposals"][0]["requirements"][0]["alternatives"][0]["fact_witnesses"].clear()
    assert _evaluation() == evaluation
    with pytest.raises(ValueError):
        MaterialSupportEvaluation.from_mapping(mapping)


def test_same_candidate_cannot_change_source_between_proposal_requirements():
    second = replace(_proposal(), proposal_id="second")
    alternate = replace(ALTERNATIVE, source_ref=replace(SOURCE_REF, object_id="source-two"))
    second_row = ProposalMaterialSupport(1, second, (replace(_row(), alternatives=(alternate,)),), (), False)
    draft = ProposalDraftSet(BINDING, (_proposal(), second))
    with pytest.raises(ValueError, match="inconsistent Sources"):
        replace(_evaluation(), draft_sha256=draft.canonical_hash, proposals=(_proposal_row(), second_row))


def test_source_decoder_is_real_and_analyze_only_grant_is_not_authorization():
    inputs = _inputs()
    maps = _source_context(inputs)
    assert set(maps) == {inputs.inputs[0].source_window.window_manifest_sha256}
    grant = replace(inputs.source_grant, policy=replace(inputs.source_grant.policy,
                                                       authorized_purposes=("semantic_analysis",)))
    with pytest.raises(ValueError):
        _source_context(replace(inputs, source_grant=grant))


@pytest.mark.parametrize("field", ["confidence", "summary", "owner"])
def test_fact_binding_uses_original_raw_confidence_content_and_owner(field):
    # Real parsed VLM and compiled Graph values, but no Store acceptance claim.
    from autocut_kernel.semantic_chain.narrative_models import Confidence

    inputs = _inputs()
    stage1 = _stage1(inputs)
    assert len(_fact_context(inputs, stage1)) == len(inputs.inputs[0].semantic_pack.semantic_pack.facts)
    graph = stage1.coverage.narrative_graph
    node = next(node for node in graph.nodes if node.node_type == "fact")
    changes = {
        "confidence": {"confidence": Confidence("1", "model")},
        "summary": {"label": "A different factual assertion"},
        "owner": {"evidence_refs": (replace(node.evidence_refs[0], member_ref=replace(
            node.evidence_refs[0].member_ref, revision=2,
        )),)},
    }[field]
    changed_graph = replace(graph, nodes=tuple(replace(item, **changes) if item.node_id == node.node_id
                                              else item for item in graph.nodes))
    changed = replace(stage1, coverage=replace(stage1.coverage, narrative_graph=changed_graph))
    with pytest.raises(MaterialSupportError, match="raw fact/owner/confidence"):
        _fact_context(inputs, changed)


@pytest.fixture(scope="module")
def public_case():
    return material_case()


def _rebind_case(case):
    result = dict(case)
    binding = story_design_input_binding(result["stage1"], result["projection"],
                                        job_policy=result["job_policy"], story_policy=result["story_policy"],
                                        candidate_policy=result["candidate_policy"])
    result["draft"] = replace(result["draft"], input_binding_sha256=binding)
    return result


def test_public_evaluator_preserves_all_proposals_and_complete_physical_handoff(public_case):
    result = evaluate_material_support(**public_case)
    assert result == evaluate_material_support(**public_case)
    assert MaterialSupportEvaluation.from_mapping(json.loads(canonical_json_bytes(result.to_mapping()))) == result
    assert tuple(row.proposal for row in result.proposals) == public_case["draft"].proposals
    assert all(row.status == "supported" for row in result.proposals)
    assert all(row.requirements[0].alternatives[0].conservative_duration == ConservativeDuration(7, 1)
               for row in result.proposals)
    assert all(row.requirements[0].physical_requirements_hash == row.proposal.material_requirements[0].physical_requirements_hash
               for row in result.proposals)
    assert all(not row.narrative_taint_seed_refs and not row.dependency_unknown for row in result.proposals)
    assert result.source_grant_sha256 == public_case["inputs"].source_grant.canonical_hash
    assert all(row.requirements[0].examined_candidate_count == len(public_case["projection"].catalog.candidates)
               for row in result.proposals)


def test_public_evaluator_rejects_claimed_binding_and_altered_source_grant(public_case):
    with pytest.raises(MaterialSupportError, match="draft does not bind"):
        evaluate_material_support(**{**public_case, "draft": replace(public_case["draft"], input_binding_sha256=BINDING)})
    inputs = public_case["inputs"]
    denied = replace(inputs.source_grant, policy=replace(inputs.source_grant.policy, authorized_purposes=("semantic_analysis",)))
    with pytest.raises(MaterialSupportError, match="does not close"):
        evaluate_material_support(**{**public_case, "inputs": replace(inputs, source_grant=denied)})


@pytest.mark.parametrize("mutation", ["duration", "reason", "drop", "foreign_grant"])
def test_rehashed_catalog_and_rebound_draft_cannot_replace_actual_raw_vlm_projection(public_case, mutation):
    projection = public_case["projection"]
    candidate = projection.catalog.candidates[0]
    if mutation == "duration":
        candidates = (replace(candidate, support=replace(candidate.support, conservative_duration=ConservativeDuration(100, 1))),)
        catalog = replace(projection.catalog, candidates=candidates)
    elif mutation == "reason":
        catalog = replace(projection.catalog, candidates=(replace(candidate, reason="Not in the raw response"),))
    elif mutation == "drop":
        catalog = replace(projection.catalog, candidates=())
    else:
        catalog = replace(projection.catalog, source_grant_sha256=BINDING)
    member = replace(projection.member, content_hash=catalog.canonical_hash,
                     payload_json=canonical_json_bytes(catalog.to_mapping()).decode())
    case = _rebind_case({**public_case, "projection": replace(projection, member=member, catalog=catalog)})
    with pytest.raises(MaterialSupportError, match="recomputed committed VLM projection"):
        evaluate_material_support(**case)


@pytest.mark.parametrize("layer", ["job", "requirement"])
def test_public_evaluator_known_source_exclusion_keeps_every_original_proposal(public_case, layer):
    case = dict(public_case)
    source = case["projection"].catalog.candidates[0].source_ref
    denied = SourceConstraints((), (source,), "render_source")
    if layer == "job":
        case["job_policy"] = replace(case["job_policy"], source_constraints=denied)
    else:
        case["draft"] = replace(case["draft"], proposals=tuple(replace(proposal, material_requirements=(
            replace(proposal.material_requirements[0], source_constraints=denied),
        )) for proposal in case["draft"].proposals))
    result = evaluate_material_support(**_rebind_case(case))
    assert len(result.proposals) == 2
    assert all(row.status == "unsupported" for row in result.proposals)
    assert all(row.requirements[0].exclusion_reason_counts == (ExclusionReasonCount("source_forbidden", 1),)
               for row in result.proposals)


def test_public_evaluator_rejects_unadmitted_stage1_before_semantic_support(public_case):
    inputs = public_case["inputs"]
    stage1 = _stage1(inputs)
    assert stage1.admission.next_action == "stop"
    with pytest.raises(MaterialSupportError, match="does not close"):
        evaluate_material_support(**{**public_case, "stage1": stage1})


@pytest.mark.parametrize("kind,reason", [
    ("candidate", "candidate_confidence_below_policy"),
    ("measurement", "measurement_confidence_below_policy"),
    ("missing", "required_measurement_missing"),
])
def test_public_evaluator_keeps_low_eligibility_catalog_candidate_and_good_alternative(kind, reason):
    def change(payload):
        candidate = deepcopy(payload["candidate_hypotheses"][0])
        candidate["local_candidate_id"] = "candidate_2"
        if kind == "candidate":
            candidate["support"]["confidence"] = "0.49"
        elif kind == "measurement":
            candidate["measurements"][0]["confidence"] = "0.49"
        else:
            candidate["measurements"][0]["measurement_kind"] = "hook_strength"
        payload["candidate_hypotheses"].append(candidate)

    case = material_case(payload_change=change)
    catalog = case["projection"].catalog
    assert len(catalog.candidates) == 2
    good = next(candidate for candidate in catalog.candidates if candidate.local_candidate_id == "candidate_1")
    result = evaluate_material_support(**case)
    for proposal in result.proposals:
        row = proposal.requirements[0]
        assert row.status == "supported" and row.examined_candidate_count == 2
        assert row.exclusion_reason_counts == (ExclusionReasonCount(reason, 1),)
        assert [alternative.candidate_ref.object_id for alternative in row.alternatives] == [good.candidate_id]
    assert case["projection"].catalog == catalog


def test_public_evaluator_rejects_outer_overlap_as_a_complete_fact_support():
    def change(payload):
        payload["facts"][0]["support"]["proxy_interval"] = {"start_pts": 5, "end_pts": 40, "uncertainty_pts": 0}

    case = material_case(payload_change=change)
    result = evaluate_material_support(**case)
    assert all(row.status == "unsupported" for row in result.proposals)
    assert all(row.requirements[0].exclusion_reason_counts == (ExclusionReasonCount("fact_outside_support", 1),)
               for row in result.proposals)


def test_public_evaluator_does_not_promote_context_or_measurement_only_fact():
    def change(payload):
        fact = deepcopy(payload["facts"][0])
        fact["local_fact_id"] = "fact_2"
        fact["summary"] = "A separate directly carried action."
        payload["facts"].append(fact)
        event = deepcopy(payload["events"][0])
        event["local_event_id"] = "event_2"
        payload["events"].append(event)
        payload["events"][0]["fact_refs"] = ["fact_2"]
        payload["window_summary"]["fact_refs"] = ["fact_1", "fact_2"]
        payload["window_summary"]["event_refs"] = ["event_1", "event_2"]
        payload["candidate_hypotheses"][0]["context_event_refs"] = ["event_2"]

    case = material_case(payload_change=change)
    result = evaluate_material_support(**case)
    assert all(row.status == "unsupported" for row in result.proposals)
    assert all(row.requirements[0].exclusion_reason_counts == (ExclusionReasonCount("fact_context_only", 1),)
               for row in result.proposals)


def test_public_evaluator_minimum_seconds_is_not_sum_of_multiple_alternatives():
    def change(payload):
        candidate = deepcopy(payload["candidate_hypotheses"][0])
        candidate["local_candidate_id"] = "candidate_2"
        payload["candidate_hypotheses"].append(candidate)

    case = material_case(payload_change=change)
    assert len(case["projection"].catalog.candidates) == 2
    assert all(len(row.requirements[0].alternatives) == 2 for row in evaluate_material_support(**case).proposals)
    draft = case["draft"]
    proposals = tuple(replace(proposal, material_requirements=(replace(proposal.material_requirements[0],
                                                                     minimum_usable_seconds=8),))
                      for proposal in draft.proposals)
    result = evaluate_material_support(**{**case, "draft": replace(draft, proposals=proposals)})
    assert all(row.status == "unsupported" for row in result.proposals)
    assert all(row.requirements[0].exclusion_reason_counts == (ExclusionReasonCount("duration_insufficient", 2),)
               for row in result.proposals)
