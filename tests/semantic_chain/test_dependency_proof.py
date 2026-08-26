"""Real pure six-member compilation of synthetic inputs, not Store admission."""

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal

import pytest
from autocut_kernel.semantic_chain.dependency_graph import (
    DependencySeed,
    analyze_dependency_graph,
)
from autocut_kernel.semantic_chain.dependency_projection import DependencyProjectionPolicy
from autocut_kernel.semantic_chain.dependency_proof import (
    DependencyClosureProof,
    DependencyProofError,
    build_dependency_proof,
)
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.store.models import canonical_payload_hash

from tests.semantic_chain.test_coverage_analysis import _clean_inputs, _replace_pack
from tests.semantic_chain.test_coverage_compiler import _compile
from tests.semantic_chain.test_stage1_draft import _draft

POLICY = DependencyProjectionPolicy("semantic-dependencies-v1")


def _hash(mapping):
    raw = json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _build(inputs, compilation, **changes):
    kwargs = {
        "graph_member": compilation.narrative.narrative_graph,
        "event_card_member": compilation.narrative.event_cards,
        "ledger_member": compilation.coverage_ledger,
        "policy": POLICY,
        "revision": compilation.coverage_ledger.revision,
    }
    return build_dependency_proof(inputs, **{**kwargs, **changes})


@pytest.fixture(scope="module", params=["clean", "taint", "unknown"])
def compiled(request):
    inputs = _clean_inputs()
    if request.param == "taint":
        inputs = _replace_pack(
            inputs,
            0,
            lambda pack: replace(
                pack,
                entities=(
                    replace(
                        pack.entities[0],
                        support=replace(pack.entities[0].support, confidence=Decimal("0.1")),
                    ),
                ),
            ),
        )
    payload = _draft(inputs) if request.param == "unknown" else None
    compilation, ledger = _compile(inputs, payload=payload)
    member = _build(inputs, compilation)
    proof = DependencyClosureProof.from_mapping(json.loads(member.payload_json))
    return inputs, compilation, ledger, member, proof


@pytest.fixture(scope="module")
def tainted():
    inputs = _clean_inputs()
    compilation, ledger = _compile(inputs, payload=_draft(inputs))
    member = _build(inputs, compilation)
    return (
        inputs,
        compilation,
        ledger,
        member,
        DependencyClosureProof.from_mapping(json.loads(member.payload_json)),
    )


def test_real_compilation_produces_seventh_hash_bound_member_and_flat_roundtrip(compiled):
    inputs, compilation, ledger, member, proof = compiled
    assert len(compilation.members) == 6
    assert member.artifact_type == member.logical_id == "dependency_closure_proof"
    assert len({item.artifact_type for item in (*compilation.members, member)}) == 7
    assert member.revision == compilation.coverage_ledger.revision
    assert member.scope == inputs.source_manifest.reference.scope
    assert SemanticMemberIdentity.from_artifact_member(member).content_hash == proof.canonical_hash
    assert proof.canonical_hash == _hash(proof.to_mapping())
    assert proof.input_binding_sha256 == ledger.input_binding_sha256
    assert proof.canonical_draft_sha256 == ledger.draft_sha256
    assert proof.coverage_policy_sha256 == ledger.coverage_policy_sha256
    assert proof.dependency_policy_sha256 == POLICY.canonical_hash
    assert _build(inputs, compilation) == member
    assert "analysis" not in proof.to_mapping()
    assert not hasattr(proof, "admission") and not hasattr(proof, "passed")
    assert "isolation_status" not in compilation.coverage_ledger.payload_json
    assert member.content_hash not in member.payload_json


def test_all_set_scc_and_seed_derived_hashes_have_independent_oracles(compiled):
    _, _, ledger, _, proof = compiled
    wire = proof.to_mapping()
    assert wire["arc_set_hash"] == _hash(wire["dependency_arcs"])
    assert wire["scc_set_hash"] == _hash(wire["sccs"])
    for scc in wire["sccs"]:
        assert scc["scc_sha256"] == _hash(scc["node_refs"])
    assert {item["seed_id"] for item in wire["seed_proofs"]} == {
        seed.seed_id for seed in ledger.taint_seeds
    }
    for seed in wire["seed_proofs"]:
        assert seed["closure_hash"] == _hash(seed["affected_refs"])
        assert seed["isolation_status"] == ("unbounded" if seed["frontier_refs"] else "bounded")
    if ledger.taint_seeds:
        # A bounded graph closure does not make unresolved observations safe.
        assert any(row.resolution_status != "resolved" for row in ledger.rows)
    else:
        assert wire["seed_proofs"] == []


def test_ledger_local_roots_and_frontiers_expand_to_exact_hashed_owner(compiled):
    _, compilation, ledger, _, proof = compiled
    owner = SemanticMemberIdentity.from_artifact_member(compilation.coverage_ledger)
    assert proof.ledger_member_ref == owner
    closures = {seed.seed_id: seed for seed in proof.analysis.seed_closures}
    for seed in ledger.taint_seeds:
        closure = closures[seed.seed_id]
        roots = {
            *seed.root_refs,
            *(SemanticObjectRef(owner, "coverage_window", key) for key in seed.root_window_ids),
        }
        frontier = {
            *seed.frontier_refs,
            *(SemanticObjectRef(owner, "coverage_window", key) for key in seed.frontier_window_ids),
        }
        assert set(closure.root_refs) == roots
        assert set(closure.frontier_refs) == frontier
        assert roots <= set(closure.affected_refs) <= set(proof.analysis.node_refs)
    assert {
        ref.object_id for ref in proof.analysis.node_refs if ref.object_type == "coverage_window"
    } == {window.window_id for window in ledger.windows}


def test_frozen_deep_values_and_fresh_wire_do_not_alias(compiled):
    proof = compiled[-1]
    for field in fields(proof):
        with pytest.raises(FrozenInstanceError):
            setattr(proof, field.name, None)
    original = proof.to_mapping()
    wire = proof.to_mapping()
    wire["node_refs"][0]["member_ref"]["scope"]["key"] = "foreign"
    wire["dependency_arcs"].clear()
    wire["sccs"][0]["node_refs"].clear()
    assert proof.to_mapping() == original


@pytest.mark.parametrize("bad", [None, [], (), True, "{}"])
def test_wire_root_is_exact_object(compiled, bad):
    with pytest.raises(DependencyProofError):
        DependencyClosureProof.from_mapping(bad)


def test_all_flat_fields_required_without_extra_defaults(compiled):
    wire = compiled[-1].to_mapping()
    for key in wire:
        with pytest.raises(DependencyProofError):
            DependencyClosureProof.from_mapping(
                {name: value for name, value in wire.items() if name != key}
            )
    for field in ("accepted", "policy_ref", "graph_ref", "analysis", "artifact_id"):
        with pytest.raises(DependencyProofError):
            DependencyClosureProof.from_mapping({**wire, field: None})


@pytest.mark.parametrize(
    "field",
    [
        "input_binding_sha256",
        "canonical_draft_sha256",
        "coverage_policy_sha256",
        "dependency_policy_sha256",
    ],
)
@pytest.mark.parametrize("bad", [True, 1, "", "sha256:" + "A" * 64, "sha256:" + "a" * 63, "\ud800"])
def test_direct_binding_hash_types_and_grammar(tainted, field, bad):
    with pytest.raises(DependencyProofError):
        replace(tainted[-1], **{field: bad})


@pytest.mark.parametrize(
    "field", ["source_member_ref", "graph_member_ref", "event_card_member_ref", "ledger_member_ref"]
)
@pytest.mark.parametrize("change", ["dict", "type", "scope", "hash", "logical", "revision"])
def test_member_metadata_is_exact_and_every_reference_stays_bound(tainted, field, change):
    proof = tainted[-1]
    owner = getattr(proof, field)
    replacement = {
        "dict": owner.to_mapping(),
        "type": replace(owner, artifact_type="coverage_admission"),
        "scope": replace(owner, scope=replace(owner.scope, key="foreign")),
        "hash": replace(owner, content_hash=_hash("foreign")),
        "logical": replace(owner, logical_id="foreign"),
        "revision": replace(owner, revision=owner.revision + 1),
    }[change]
    with pytest.raises(DependencyProofError):
        replace(proof, **{field: replacement})


@pytest.mark.parametrize(
    "field", ["node_refs", "dependency_arcs", "sccs", "condensation_arcs", "seed_proofs"]
)
@pytest.mark.parametrize("change", ["reverse", "duplicate", "tuple", "wrong_member"])
def test_collections_must_be_canonical_unique_and_exact_arrays(tainted, field, change):
    wire = tainted[-1].to_mapping()
    values = wire[field]
    assert len(values) > 1
    wire[field] = {
        "reverse": list(reversed(values)),
        "duplicate": [*values, values[0]],
        "tuple": tuple(values),
        "wrong_member": [None],
    }[change]
    with pytest.raises(DependencyProofError):
        DependencyClosureProof.from_mapping(wire)


@pytest.mark.parametrize("kind", ["edge", "vlm_event", "diagnostic", "taint_seed", "admission"])
def test_dependency_nodes_cannot_alias_other_object_roles(tainted, kind):
    wire = tainted[-1].to_mapping()
    wire["node_refs"][0]["object_type"] = kind
    with pytest.raises(DependencyProofError):
        DependencyClosureProof.from_mapping(wire)


@pytest.mark.parametrize(
    "change",
    [
        "unknown_kind",
        "nonpropagating_kind",
        "missing_endpoint",
        "foreign_source",
        "graph_event_alias",
    ],
)
def test_arc_kind_endpoints_and_source_ownership(tainted, change):
    wire = tainted[-1].to_mapping()
    arc = wire["dependency_arcs"][0]
    if change == "unknown_kind":
        arc["kind"] = "automatic_identity_merge"
    elif change == "nonpropagating_kind":
        arc["kind"] = "involves"
    elif change == "missing_endpoint":
        arc["to_ref"]["object_id"] = "nonexistent"
    elif change == "foreign_source":
        arc["source_ref"]["member_ref"]["content_hash"] = _hash("foreign")
    else:
        event = next(ref for ref in wire["node_refs"] if ref["object_type"] == "event")
        event["member_ref"] = deepcopy(wire["graph_member_ref"])
    wire["arc_set_hash"] = _hash(wire["dependency_arcs"])
    with pytest.raises(DependencyProofError):
        DependencyClosureProof.from_mapping(wire)


@pytest.mark.parametrize(
    "change",
    [
        "scc_hash",
        "scc_partition",
        "condensation",
        "seed_root",
        "affected",
        "reachable",
        "frontier",
        "isolation",
        "closure_hash",
        "arc_hash",
        "scc_set_hash",
    ],
)
def test_graph_and_seed_derived_claims_are_recomputed_not_trusted(tainted, change):
    wire = tainted[-1].to_mapping()
    seed = wire["seed_proofs"][0]
    if change == "scc_hash":
        wire["sccs"][0]["scc_sha256"] = _hash("foreign")
    elif change == "scc_partition":
        wire["sccs"].pop()
        wire["scc_set_hash"] = _hash(wire["sccs"])
    elif change == "condensation":
        wire["condensation_arcs"].pop()
    elif change == "seed_root":
        seed["root_refs"] = []
    elif change == "affected":
        seed["affected_refs"] = seed["root_refs"]
        assert len(seed["affected_refs"]) < len(tainted[-1].analysis.seed_closures[0].affected_refs)
        seed["closure_hash"] = _hash(seed["affected_refs"])
    elif change == "reachable":
        seed["reachable_scc_sha256s"].pop()
    elif change == "frontier":
        seed["frontier_refs"][0]["object_id"] = "nonexistent"
    elif change == "isolation":
        seed["isolation_status"] = "bounded"
    elif change == "closure_hash":
        seed["closure_hash"] = _hash("foreign")
    elif change == "arc_hash":
        wire["arc_set_hash"] = _hash("foreign")
    else:
        wire["scc_set_hash"] = _hash("foreign")
    with pytest.raises(DependencyProofError):
        DependencyClosureProof.from_mapping(wire)


def test_nested_wire_objects_reject_unknown_fields(tainted):
    original = tainted[-1].to_mapping()
    paths = [
        ("source_member_ref",),
        ("graph_member_ref", "scope"),
        ("node_refs", 0),
        ("dependency_arcs", 0),
        ("sccs", 0),
        ("condensation_arcs", 0),
        ("seed_proofs", 0),
        ("seed_proofs", 0, "affected_refs", 0),
    ]
    for path in paths:
        wire = deepcopy(original)
        node = wire
        for key in path:
            node = node[key]
        node["accepted"] = True
        with pytest.raises(DependencyProofError):
            DependencyClosureProof.from_mapping(wire)


def test_direct_analysis_does_not_silently_normalize_supplied_collections(tainted):
    proof = tainted[-1]
    for field in ("node_refs", "arcs", "sccs", "condensation_arcs", "seed_closures"):
        analysis = replace(
            proof.analysis, **{field: tuple(reversed(getattr(proof.analysis, field)))}
        )
        with pytest.raises(DependencyProofError):
            replace(proof, analysis=analysis)
    with pytest.raises(DependencyProofError):
        replace(proof, analysis=proof.analysis.to_mapping())


def test_structural_consistency_alone_does_not_prove_external_ledger_completeness(tainted):
    proof = tainted[-1]
    # The decoder has no Ledger bytes. Removing every seed coherently is a
    # producer claim for the independent verifier to reject, not auto-admission.
    analysis = analyze_dependency_graph(proof.analysis.node_refs, proof.analysis.arcs, ())
    candidate = replace(proof, analysis=analysis)
    assert DependencyClosureProof.from_mapping(candidate.to_mapping()) == candidate
    assert candidate.ledger_member_ref == proof.ledger_member_ref
    assert not candidate.analysis.seed_closures and proof.analysis.seed_closures
    assert not hasattr(candidate, "accepted")


@pytest.mark.parametrize("bad", [True, 1.0, 0, -1, 2**53, "1", None, 2])
def test_builder_requires_exact_positive_safe_matching_revision(tainted, bad):
    with pytest.raises(DependencyProofError):
        _build(tainted[0], tainted[1], revision=bad)


@pytest.mark.parametrize("field", ["graph_member", "event_card_member", "ledger_member"])
@pytest.mark.parametrize("change", ["hash", "scope", "dict", "duplicate_json", "float_json"])
def test_builder_rejects_substituted_members_and_ambiguous_json_before_projection(
    tainted, field, change
):
    inputs, compilation, _, _, _ = tainted
    members = {
        "graph_member": compilation.narrative.narrative_graph,
        "event_card_member": compilation.narrative.event_cards,
        "ledger_member": compilation.coverage_ledger,
    }
    member = members[field]
    if change == "hash":
        bad = replace(member, content_hash=_hash("foreign"))
    elif change == "scope":
        bad = replace(member, scope=replace(member.scope, key="foreign"))
    elif change == "dict":
        bad = {"payload_json": member.payload_json}
    else:
        raw = (
            '{"duplicate":1,"duplicate":1,' if change == "duplicate_json" else '{"float":0.5,'
        ) + member.payload_json[1:]
        bad = replace(member, payload_json=raw, content_hash=canonical_payload_hash(raw))
    with pytest.raises(DependencyProofError):
        _build(inputs, compilation, **{field: bad})


def test_builder_does_not_accept_untyped_input_or_policy(tainted):
    inputs, compilation, _, _, _ = tainted
    with pytest.raises(DependencyProofError):
        _build({}, compilation)
    with pytest.raises(DependencyProofError):
        _build(inputs, compilation, policy=POLICY.to_mapping())


def test_shared_analysis_replay_retains_identical_seed_closures(tainted):
    proof = tainted[-1]
    seeds = tuple(
        DependencySeed(seed.seed_id, seed.root_refs, seed.frontier_refs)
        for seed in proof.analysis.seed_closures
    )
    assert (
        analyze_dependency_graph(proof.analysis.node_refs, proof.analysis.arcs, seeds)
        == proof.analysis
    )
