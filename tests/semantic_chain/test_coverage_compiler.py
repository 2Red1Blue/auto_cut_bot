"""Six business members from parsed VLM + synthetic Store DTOs, not DB proof."""

import hashlib
import json
from dataclasses import replace
from decimal import Decimal

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.semantic_chain.coverage_analysis import Stage1CoveragePolicy
from autocut_kernel.semantic_chain.coverage_compiler import compile_stage1_coverage
from autocut_kernel.semantic_chain.dependency_graph import DependencySeed, analyze_dependency_graph
from autocut_kernel.semantic_chain.dependency_projection import (
    DependencyProjectionPolicy,
    project_dependencies,
)
from autocut_kernel.semantic_chain.diagnostic_models import ConflictDiagnostics, EvidenceDiagnostics
from autocut_kernel.semantic_chain.ledger_models import CoverageLedger, LocalCoverageWindowRef
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.narrative_models import NarrativeGraph
from autocut_kernel.semantic_chain.stage1_draft import stage1_draft_prompt_inputs
from autocut_kernel.store import ArtifactScope
from autocut_kernel.vlm.models import derive_vlm_global_id

from tests.semantic_chain.test_continuity_analysis import _inputs
from tests.semantic_chain.test_coverage_analysis import _clean_inputs, _replace_pack
from tests.semantic_chain.test_stage1_draft import POLICY, _draft

COVERAGE = Stage1CoveragePolicy("0.5", "strict_global")


def _compile(inputs=None, *, payload=None, raw=None, revision=1, policy=COVERAGE):
    if inputs is None:
        inputs = _clean_inputs()
    if raw is None:
        if payload is None:
            payload = _draft(inputs)
            payload["merge_proposals"] = []
        raw = canonical_json_bytes(payload)
    result = compile_stage1_coverage(
        inputs, raw, draft_policy=POLICY, coverage_policy=policy,
        scope=inputs.source_manifest.reference.scope, revision=revision,
    )
    return result, CoverageLedger.from_mapping(json.loads(result.coverage_ledger.payload_json))


def _diagnostics(result):
    return (
        EvidenceDiagnostics.from_mapping(json.loads(result.evidence_diagnostics.payload_json)),
        ConflictDiagnostics.from_mapping(json.loads(result.conflict_diagnostics.payload_json)),
    )


def test_clean_compilation_resolves_real_assignments_and_all_six_members():
    result, ledger = _compile()
    assert len(result.members) == 6
    assert len({member.artifact_type for member in result.members}) == 6
    identities = {member.artifact_type: SemanticMemberIdentity.from_artifact_member(member)
                  for member in result.members}
    assert all(row.resolution_status == "resolved" for row in ledger.rows)
    assert not ledger.taint_seeds
    evidence, conflicts = _diagnostics(result)
    assert not evidence.items and not conflicts.items
    graph = NarrativeGraph.from_mapping(json.loads(result.narrative.narrative_graph.payload_json))
    nodes = {(node.node_type, node.node_id) for node in graph.nodes}
    assert ledger.actual_counts.to_mapping() == {"fact": 2, "event": 2, "source_window": 2, "obligation": 1}
    for row in ledger.rows:
        assert row.assignment_refs
        for ref in row.assignment_refs:
            assert ref.member_ref == identities[ref.member_ref.artifact_type]
            if ref.member_ref.artifact_type == "narrative_graph":
                assert (ref.object_type, ref.object_id) in nodes
        if row.unit_type == "event":
            assert row.unit_ref.member_ref == identities["event_card_set"]
        if row.unit_type == "source_window":
            assert type(row.unit_ref) is LocalCoverageWindowRef
    assert not hasattr(result, "admission")


def test_low_confidence_retains_origin_and_links_every_affected_row():
    inputs = _clean_inputs()
    inputs = _replace_pack(inputs, 0, lambda pack: replace(
        pack, entities=(replace(pack.entities[0], support=replace(pack.entities[0].support, confidence=Decimal("0.1"))),),
    ))
    result, ledger = _compile(inputs)
    evidence, conflicts = _diagnostics(result)
    assert not conflicts.items
    assert len(evidence.items) == 1
    diagnostic = evidence.items[0]
    assert diagnostic.measurement.observation_kind == "entity"
    assert diagnostic.measurement.value == "0.1"
    assert diagnostic.scope_ref.object_type == "entity"
    assert diagnostic.measurement.observation_ref.object_id == inputs.inputs[0].semantic_pack.semantic_pack.entities[0].entity_id
    assert len(ledger.taint_seeds) == 4
    for row in ledger.rows:
        if row.resolution_status == "unresolved":
            assert [ref.object_id for ref in row.diagnostic_refs] == [diagnostic.diagnostic_id]
    assert all(not seed.frontier_refs and not seed.frontier_window_ids for seed in ledger.taint_seeds)


def test_merge_claims_are_lossless_unknown_frontier_not_accepted_identity():
    inputs = _clean_inputs()
    payload = _draft(inputs)
    result, ledger = _compile(inputs, payload=payload)
    evidence, conflicts = _diagnostics(result)
    assert not evidence.items
    assert len(conflicts.items) == len(conflicts.merge_causes) == 1
    cause = conflicts.merge_causes[0]
    proposal = payload["merge_proposals"][0]
    assert cause.rationale == proposal["rationale"]
    assert {ref.object_id for ref in cause.entity_refs} == {ref["object_id"] for ref in proposal["entity_refs"]}
    assert {claim.payload.observation_ref for claim in conflicts.claims} == set(cause.entity_refs)
    assert len(ledger.taint_seeds) == len(ledger.rows) == 7
    assert all(seed.frontier_refs or seed.frontier_window_ids for seed in ledger.taint_seeds)
    graph = NarrativeGraph.from_mapping(json.loads(result.narrative.narrative_graph.payload_json))
    assert sum(node.node_type == "entity" for node in graph.nodes) == 2
    assert not any(node.node_type == "character" for node in graph.nodes)


def test_unassigned_origins_are_not_multiplied_when_inherited_by_windows():
    inputs = _replace_pack(_clean_inputs(), 1, lambda pack: replace(
        pack, window_summary=replace(pack.window_summary, fact_refs=(), event_refs=()),
    ))
    result, ledger = _compile(inputs)
    evidence, _ = _diagnostics(result)
    assert sorted(item.reason_code for item in evidence.items) == [
        "summary_evidence_missing", "unassigned", "unassigned",
    ]
    origins = {item.scope_ref.object_type for item in evidence.items if item.reason_code == "unassigned"}
    assert origins == {"fact", "event"}
    bad_window = next(row for row in ledger.rows if row.unit_type == "source_window" and row.resolution_status == "unresolved")
    assert len(bad_window.diagnostic_refs) == 3


@pytest.mark.parametrize("conflict", [False, True])
def test_continuity_retains_real_claims_and_unknown_frontier(conflict):
    inputs = _inputs({"start": 1000, "following": True}, {"start": 1100 if conflict else 1300})
    result, ledger = _compile(inputs, policy=Stage1CoveragePolicy("0", "strict_global"))
    evidence, conflicts = _diagnostics(result)
    if conflict:
        assert len(conflicts.items) == 1 and len(conflicts.claims) == 2
        assert {claim.payload.continues for claim in conflicts.claims} == {True, False}
        assert not evidence.items
        assert all(row.resolution_status == "conflicted" for row in ledger.rows)
    else:
        assert len(evidence.items) == 1 and not conflicts.claims
        assert evidence.items[0].continuity_claim.continues is True
        assert all(seed.frontier_window_ids for seed in ledger.taint_seeds)


def test_raw_draft_hash_is_not_confused_with_canonical_hash():
    inputs = _clean_inputs()
    payload = _draft(inputs)
    payload["merge_proposals"] = []
    compact = canonical_json_bytes(payload)
    spaced = json.dumps(payload, ensure_ascii=False, indent=2).encode()
    result, ledger = _compile(inputs, raw=compact)
    other, second = _compile(inputs, raw=spaced)
    first_evidence, _ = _diagnostics(result)
    second_evidence, _ = _diagnostics(other)
    assert first_evidence.raw_draft_sha256 == "sha256:" + hashlib.sha256(compact).hexdigest()
    assert second_evidence.raw_draft_sha256 == "sha256:" + hashlib.sha256(spaced).hexdigest()
    assert first_evidence.raw_draft_sha256 != second_evidence.raw_draft_sha256
    assert first_evidence.canonical_draft_sha256 == second_evidence.canonical_draft_sha256
    assert ledger == second  # no diagnostic refs needed in this clean Ledger
    assert result.narrative == other.narrative


def test_revision_rebinds_all_pending_output_refs_without_self_hash():
    inputs = _clean_inputs()
    result, ledger = _compile(inputs, payload=_draft(inputs), revision=3)
    for member in result.members:
        assert member.revision == 3
        if member.artifact_type == "coverage_ledger":
            assert member.content_hash not in member.payload_json
    for seed in ledger.taint_seeds:
        for ref in (*seed.root_refs, *seed.frontier_refs):
            if ref.member_ref.artifact_type in {member.artifact_type for member in result.members}:
                assert ref.member_ref.revision == 3


def test_missing_summary_and_eventless_window_keep_fact_and_diagnostic():
    inputs = _clean_inputs()
    payload = _draft(inputs)
    payload["merge_proposals"] = []
    inputs = _replace_pack(inputs, 1, lambda pack: replace(
        pack, events=(), candidate_hypotheses=(),
        window_summary=replace(pack.window_summary, event_refs=(), fact_refs=()),
    ))
    payload["input_binding_sha256"] = stage1_draft_prompt_inputs(inputs, policy=POLICY)["input_binding_sha256"]
    result, ledger = _compile(inputs, payload=payload)
    evidence, _ = _diagnostics(result)
    assert ledger.input_counts.event == 1 and ledger.input_counts.fact == 2
    gap = next(item for item in evidence.items if item.reason_code == "summary_evidence_missing")
    assert gap.summary == inputs.inputs[1].semantic_pack.semantic_pack.window_summary.summary


def test_ledger_local_seeds_feed_full_dependency_graph_without_self_reference():
    inputs = _clean_inputs()
    result, ledger = _compile(inputs, payload=_draft(inputs))
    projected = project_dependencies(
        inputs, graph_member=result.narrative.narrative_graph,
        event_card_member=result.narrative.event_cards, ledger_member=result.coverage_ledger,
        policy=DependencyProjectionPolicy("semantic-dependencies-v1"),
    )
    owner = SemanticMemberIdentity.from_artifact_member(result.coverage_ledger)
    seeds = tuple(DependencySeed(
        seed.seed_id,
        (*seed.root_refs, *(SemanticObjectRef(owner, "coverage_window", key) for key in seed.root_window_ids)),
        tuple(sorted(
            (*seed.frontier_refs, *(SemanticObjectRef(owner, "coverage_window", key) for key in seed.frontier_window_ids)),
            key=lambda ref: canonical_json_bytes(ref.to_mapping()),
        )),
    ) for seed in ledger.taint_seeds)
    closure = analyze_dependency_graph(projected.nodes, projected.arcs, seeds)
    assert len(closure.seed_closures) == len(ledger.taint_seeds)
    assert all(item.frontier_refs for item in closure.seed_closures)


def test_directly_resolved_event_is_still_reachable_from_a_tainted_cause():
    inputs = _clean_inputs()

    def causal_pair(pack):
        fact_b = replace(pack.facts[0], local_fact_id="fact_b", fact_id=derive_vlm_global_id("fact", "fact_b", pack.request_identity_sha256))
        event_a = replace(pack.events[0], support=replace(pack.events[0].support, confidence=Decimal("0.1")))
        event_b = replace(pack.events[0], local_event_id="event_b", event_id=derive_vlm_global_id("event", "event_b", pack.request_identity_sha256), fact_refs=(fact_b.fact_id,), cause_event_refs=(event_a.event_id,))
        event_a = replace(event_a, effect_event_refs=(event_b.event_id,))
        return replace(pack, facts=(*pack.facts, fact_b), events=(event_a, event_b),
                       window_summary=replace(pack.window_summary, event_refs=tuple(sorted((event_a.event_id, event_b.event_id)))))

    inputs = _replace_pack(inputs, 1, causal_pair)
    result, ledger = _compile(inputs)
    event_a, event_b = inputs.inputs[1].semantic_pack.semantic_pack.events
    row_a = next(row for row in ledger.rows if row.unit_type == "event" and row.unit_ref.object_id == event_a.event_id)
    row_b = next(row for row in ledger.rows if row.unit_type == "event" and row.unit_ref.object_id == event_b.event_id)
    assert row_a.resolution_status == "unresolved"
    assert row_b.resolution_status == "resolved"
    projected = project_dependencies(
        inputs, graph_member=result.narrative.narrative_graph,
        event_card_member=result.narrative.event_cards, ledger_member=result.coverage_ledger,
        policy=DependencyProjectionPolicy("semantic-dependencies-v1"),
    )
    # Isolate the actual Event cause; do not let broad window roots conceal a
    # missing causes edge. Resolved is not a transitive isolation certificate.
    closure = analyze_dependency_graph(projected.nodes, projected.arcs, (
        DependencySeed("event-cause", (row_a.unit_ref,), ()),
    ))
    assert row_b.unit_ref in closure.seed_closures[0].affected_refs


def test_compiler_redecodes_draft_and_rejects_scope_or_hidden_authority():
    inputs = _clean_inputs()
    payload = _draft(inputs)
    payload["admission"] = "pass"
    with pytest.raises(ValueError):
        _compile(inputs, payload=payload)
    payload.pop("admission")
    with pytest.raises(ValueError, match="scope"):
        compile_stage1_coverage(
            inputs, canonical_json_bytes(payload), draft_policy=POLICY, coverage_policy=COVERAGE,
            scope=ArtifactScope("pipeline", "job", "different-job"), revision=1,
        )
