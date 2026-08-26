"""Pure deterministic coverage for the projected dependency graph primitive."""

import hashlib
import random

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.semantic_chain.dependency_graph import (
    DependencyArc,
    DependencyGraphError,
    DependencySeed,
    analyze_dependency_graph,
)
from autocut_kernel.semantic_chain.member_refs import (
    SemanticMemberIdentity,
    SemanticObjectRef,
)
from autocut_kernel.store import ArtifactScope


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _ref(label: str, *, owner: str = "narrative_graph") -> SemanticObjectRef:
    member = SemanticMemberIdentity(
        artifact_type=owner,
        logical_id=f"{owner}-{label}",
        revision=1,
        scope=ArtifactScope("pipeline", "job", "dependency-graph-test"),
        content_hash=_digest(f"{owner}:{label}"),
    )
    return SemanticObjectRef(member_ref=member, object_type="event", object_id=label)


def _arc(from_ref: SemanticObjectRef, to_ref: SemanticObjectRef, kind: str = "supports") -> DependencyArc:
    return DependencyArc(from_ref, to_ref, kind, _ref("graph-source", owner="dependency_proof"))


def _affected(analysis: object, seed_id: str) -> tuple[SemanticObjectRef, ...]:
    closures = getattr(analysis, "seed_closures")
    return next(item.affected_refs for item in closures if item.seed_id == seed_id)


def test_cycle_scc_and_diamond_closure_are_canonical_under_input_reordering() -> None:
    a, b, c, d, e = (_ref(item) for item in "abcde")
    arcs = (
        _arc(a, b),
        _arc(b, c),
        _arc(c, a),
        _arc(c, d),
        _arc(c, e),
        _arc(d, e),
    )
    seed = DependencySeed("cycle", (a,), ())

    first = analyze_dependency_graph((e, d, c, b, a), tuple(reversed(arcs)), (seed,))
    second = analyze_dependency_graph((a, b, c, d, e), arcs, (seed,))

    assert first.to_mapping() == second.to_mapping()
    assert set(_affected(first, "cycle")) == {a, b, c, d, e}
    assert sorted(len(item.node_refs) for item in first.sccs) == [1, 1, 3]
    assert tuple(item.scc_sha256 for item in first.sccs) == tuple(
        sorted(item.scc_sha256 for item in first.sccs)
    )


def test_duplicate_arcs_are_deduplicated_by_full_canonical_identity() -> None:
    a, b = _ref("a"), _ref("b")
    duplicate = _arc(a, b)
    different_source = DependencyArc(a, b, "supports", _ref("other-source", owner="dependency_proof"))

    analysis = analyze_dependency_graph((b, a), (duplicate, different_source, duplicate), ())

    assert analysis.arcs == tuple(sorted((duplicate, different_source), key=lambda item: item.canonical_key))


def test_isolated_and_external_seed_projection_are_preserved_without_bounded_claim() -> None:
    isolated = _ref("isolated")
    external = _ref("external", owner="coverage_ledger")
    seed = DependencySeed("external-root", (isolated,), (external,))

    analysis = analyze_dependency_graph((isolated,), (), (seed,))
    closure = analysis.seed_closures[0]

    assert closure.affected_refs == (isolated,)
    assert closure.root_refs == (isolated,)
    assert closure.frontier_refs == (external,)
    assert "bounded" not in analysis.to_mapping()


def test_multiple_seeds_remain_independent_and_owner_hash_is_part_of_identity() -> None:
    event_card = _ref("same", owner="event_card_set")
    graph = _ref("same", owner="narrative_graph")
    downstream = _ref("downstream")

    analysis = analyze_dependency_graph(
        (event_card, graph, downstream),
        (_arc(event_card, downstream),),
        (
            DependencySeed("graph", (graph,), ()),
            DependencySeed("event-card", (event_card,), ()),
        ),
    )

    assert _affected(analysis, "graph") == (graph,)
    assert set(_affected(analysis, "event-card")) == {event_card, downstream}
    assert event_card != graph


def test_missing_nodes_and_noncanonical_frontier_fail_closed() -> None:
    a, b = _ref("a"), _ref("b")
    with pytest.raises(DependencyGraphError, match="arc endpoint"):
        analyze_dependency_graph((a,), (_arc(a, b),), ())
    with pytest.raises(DependencyGraphError, match="seed root"):
        analyze_dependency_graph((a,), (), (DependencySeed("missing", (b,), ()),))
    with pytest.raises(DependencyGraphError, match="frontier_refs must be sorted"):
        DependencySeed(
            "bad-frontier",
            (a,),
            tuple(sorted((a, b), key=lambda item: canonical_json_bytes(item.to_mapping()), reverse=True)),
        )


def test_mapping_is_fresh_and_contains_only_canonical_projected_values() -> None:
    a, b = _ref("a"), _ref("b")
    analysis = analyze_dependency_graph((a, b), (_arc(a, b),), (DependencySeed("seed", (a,), ()),))

    mapping = analysis.to_mapping()
    mapping["node_refs"].clear()  # type: ignore[index]

    assert len(analysis.node_refs) == 2
    assert analysis.to_mapping()["node_refs"]


def test_deep_chain_and_random_graph_match_independent_iterative_bfs_oracle() -> None:
    deep = tuple(_ref(f"deep-{index}") for index in range(1200))
    deep_arcs = tuple(_arc(deep[index], deep[index + 1]) for index in range(len(deep) - 1))
    deep_analysis = analyze_dependency_graph(deep, deep_arcs, (DependencySeed("deep", (deep[0],), ()),))
    assert set(_affected(deep_analysis, "deep")) == set(deep)

    rng = random.Random(214)
    nodes = tuple(_ref(f"random-{index}") for index in range(31))
    arcs = tuple(
        _arc(nodes[from_index], nodes[to_index])
        for from_index in range(len(nodes))
        for to_index in range(len(nodes))
        if from_index != to_index and rng.random() < 0.08
    )
    seed = nodes[4]
    analysis = analyze_dependency_graph(
        tuple(reversed(nodes)), tuple(reversed(arcs)), (DependencySeed("random", (seed,), ()),)
    )

    adjacency = {node: set() for node in nodes}
    for arc in arcs:
        adjacency[arc.from_ref].add(arc.to_ref)
    expected = {seed}
    pending = [seed]
    while pending:
        current = pending.pop()
        for successor in adjacency[current]:
            if successor not in expected:
                expected.add(successor)
                pending.append(successor)
    assert set(_affected(analysis, "random")) == expected


def test_every_three_node_digraph_matches_all_root_reachability_and_scc_oracle() -> None:
    """Exhaust all 512 graphs, including self-loops; oracle uses only BFS."""
    nodes = tuple(_ref(label) for label in "abc")
    possible_edges = tuple((source, target) for source in nodes for target in nodes)
    seeds = tuple(DependencySeed(str(index), (node,), ()) for index, node in enumerate(nodes))
    for mask in range(1 << len(possible_edges)):
        edges = tuple(edge for index, edge in enumerate(possible_edges) if mask & (1 << index))
        adjacency = {node: set() for node in nodes}
        for source, target in edges:
            adjacency[source].add(target)
        reachable = {}
        for root in nodes:
            reached, pending = {root}, [root]
            while pending:
                for target in adjacency[pending.pop()]:
                    if target not in reached:
                        reached.add(target)
                        pending.append(target)
            reachable[root] = reached
        expected_groups = {
            frozenset(other for other in nodes if other in reachable[root] and root in reachable[other])
            for root in nodes
        }
        analysis = analyze_dependency_graph(nodes, tuple(_arc(*edge) for edge in edges), seeds)
        assert {frozenset(scc.node_refs) for scc in analysis.sccs} == expected_groups, mask
        scc_by_node = {}
        for group in expected_groups:
            ordered = sorted(group, key=lambda ref: canonical_json_bytes(ref.to_mapping()))
            digest = "sha256:" + hashlib.sha256(
                canonical_json_bytes([ref.to_mapping() for ref in ordered])
            ).hexdigest()
            for node in group:
                scc_by_node[node] = digest
        assert {scc.scc_sha256 for scc in analysis.sccs} == set(scc_by_node.values()), mask
        assert {
            (arc.from_scc_sha256, arc.to_scc_sha256) for arc in analysis.condensation_arcs
        } == {
            (scc_by_node[source], scc_by_node[target])
            for source, target in edges if scc_by_node[source] != scc_by_node[target]
        }, mask
        for index, root in enumerate(nodes):
            assert set(_affected(analysis, str(index))) == reachable[root], mask
