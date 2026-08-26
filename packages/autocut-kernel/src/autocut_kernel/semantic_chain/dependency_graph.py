"""Deterministic dependency-graph analysis over already-projected references.

This module deliberately has no admission, evidence, or policy authority.  A
Stage 1 owner must first project its verified graph/attribute inputs into exact
``SemanticObjectRef`` arcs and decide whether a frontier is closed.  The primitive only
computes the reproducible SCC and reachability facts needed by that owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ..contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from .member_refs import SemanticObjectRef


class DependencyGraphError(ValueError):
    """Raised when a typed dependency graph is not closed enough to analyze."""


def _ref_key(value: SemanticObjectRef) -> bytes:
    return canonical_json_bytes(value.to_mapping())


def _require_tuple(value: object, label: str) -> tuple[object, ...]:
    if type(value) is not tuple:  # noqa: E721 - closed public value boundary.
        raise DependencyGraphError(f"{label} must be a tuple")
    return cast(tuple[object, ...], value)


def _require_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise DependencyGraphError(f"{label} must be non-empty UTF-8 text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise DependencyGraphError(f"{label} must be non-empty UTF-8 text") from error
    return value


def _require_domain_refs(
    value: object,
    label: str,
    *,
    nonempty: bool = False,
    canonical: bool = False,
) -> tuple[SemanticObjectRef, ...]:
    raw = _require_tuple(value, label)
    refs = tuple(raw)
    if any(type(item) is not SemanticObjectRef for item in refs):  # noqa: E721
        raise DependencyGraphError(f"{label} must contain only SemanticObjectRef values")
    typed = tuple(item for item in refs if type(item) is SemanticObjectRef)
    if nonempty and not typed:
        raise DependencyGraphError(f"{label} must not be empty")
    keys = tuple(_ref_key(item) for item in typed)
    if len(set(keys)) != len(keys):
        raise DependencyGraphError(f"{label} must not contain duplicate SemanticObjectRef values")
    if canonical and keys != tuple(sorted(keys)):
        raise DependencyGraphError(f"{label} must be sorted by canonical SemanticObjectRef bytes")
    return typed


@dataclass(frozen=True, slots=True)
class DependencyArc:
    """One already-authorized directional dependency projection."""

    from_ref: SemanticObjectRef
    to_ref: SemanticObjectRef
    kind: str
    source_ref: SemanticObjectRef

    def __post_init__(self) -> None:
        if type(self.from_ref) is not SemanticObjectRef or type(self.to_ref) is not SemanticObjectRef:  # noqa: E721
            raise DependencyGraphError("dependency arc endpoints must be exact SemanticObjectRef values")
        _require_text(self.kind, "dependency arc kind")
        if type(self.source_ref) is not SemanticObjectRef:  # noqa: E721
            raise DependencyGraphError("dependency arc source_ref must be an exact SemanticObjectRef")

    @property
    def canonical_key(self) -> tuple[bytes, bytes, str, bytes]:
        return (_ref_key(self.from_ref), _ref_key(self.to_ref), self.kind, _ref_key(self.source_ref))

    def to_mapping(self) -> dict[str, object]:
        return {
            "from_ref": self.from_ref.to_mapping(),
            "kind": self.kind,
            "source_ref": self.source_ref.to_mapping(),
            "to_ref": self.to_ref.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class DependencySeed:
    """A precomputed taint/uncertainty seed and its owner-supplied frontier."""

    seed_id: str
    root_refs: tuple[SemanticObjectRef, ...]
    frontier_refs: tuple[SemanticObjectRef, ...]

    def __post_init__(self) -> None:
        _require_text(self.seed_id, "dependency seed_id")
        _require_domain_refs(self.root_refs, "dependency seed root_refs", nonempty=True)
        # The frontier is an owner decision, not an analysis by-product.  Keep
        # that canonical supplied sequence byte-for-byte in the output.
        _require_domain_refs(self.frontier_refs, "dependency seed frontier_refs", canonical=True)

    def to_mapping(self) -> dict[str, object]:
        return {
            "frontier_refs": [item.to_mapping() for item in self.frontier_refs],
            "root_refs": [item.to_mapping() for item in sorted(self.root_refs, key=_ref_key)],
            "seed_id": self.seed_id,
        }


@dataclass(frozen=True, slots=True)
class DependencyScc:
    """One canonical strongly connected component."""

    scc_sha256: str
    node_refs: tuple[SemanticObjectRef, ...]

    def __post_init__(self) -> None:
        refs = _require_domain_refs(self.node_refs, "SCC node_refs", nonempty=True, canonical=True)
        expected = sha256_bytes(canonical_json_bytes([item.to_mapping() for item in refs]))
        if self.scc_sha256 != expected:
            raise DependencyGraphError("SCC sha256 does not bind its canonical SemanticObjectRef members")

    def to_mapping(self) -> dict[str, object]:
        return {
            "node_refs": [item.to_mapping() for item in self.node_refs],
            "scc_sha256": self.scc_sha256,
        }


@dataclass(frozen=True, slots=True)
class DependencyCondensationArc:
    """One distinct edge in the SCC condensation DAG."""

    from_scc_sha256: str
    to_scc_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.from_scc_sha256) is not str  # noqa: E721
            or type(self.to_scc_sha256) is not str  # noqa: E721
            or not self.from_scc_sha256
            or not self.to_scc_sha256
            or self.from_scc_sha256 == self.to_scc_sha256
        ):
            raise DependencyGraphError("condensation arc must join two distinct non-empty SCC hashes")

    def to_mapping(self) -> dict[str, str]:
        return {
            "from_scc_sha256": self.from_scc_sha256,
            "to_scc_sha256": self.to_scc_sha256,
        }


@dataclass(frozen=True, slots=True)
class DependencySeedClosure:
    """The graph-only forward closure for one seed."""

    seed_id: str
    root_refs: tuple[SemanticObjectRef, ...]
    affected_refs: tuple[SemanticObjectRef, ...]
    frontier_refs: tuple[SemanticObjectRef, ...]
    reachable_scc_sha256s: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.seed_id, "seed closure seed_id")
        roots = _require_domain_refs(self.root_refs, "seed closure root_refs", nonempty=True, canonical=True)
        affected = _require_domain_refs(
            self.affected_refs, "seed closure affected_refs", nonempty=True, canonical=True
        )
        _require_domain_refs(self.frontier_refs, "seed closure frontier_refs", canonical=True)
        if not set(roots).issubset(affected):
            raise DependencyGraphError("seed closure affected_refs must include every root")
        hashes = _require_tuple(self.reachable_scc_sha256s, "seed closure reachable_scc_sha256s")
        if not hashes or any(type(item) is not str or not item for item in hashes):  # noqa: E721
            raise DependencyGraphError("seed closure must name at least one SCC hash")
        typed_hashes = tuple(cast(str, item) for item in hashes)
        if typed_hashes != tuple(sorted(typed_hashes)) or len(set(typed_hashes)) != len(typed_hashes):
            raise DependencyGraphError("seed closure SCC hashes must be sorted and unique")

    def to_mapping(self) -> dict[str, object]:
        return {
            "affected_refs": [item.to_mapping() for item in self.affected_refs],
            "frontier_refs": [item.to_mapping() for item in self.frontier_refs],
            "reachable_scc_sha256s": list(self.reachable_scc_sha256s),
            "root_refs": [item.to_mapping() for item in self.root_refs],
            "seed_id": self.seed_id,
        }


@dataclass(frozen=True, slots=True)
class DependencyGraphAnalysis:
    """Immutable canonical SCC and seed reachability facts for projected inputs."""

    node_refs: tuple[SemanticObjectRef, ...]
    arcs: tuple[DependencyArc, ...]
    sccs: tuple[DependencyScc, ...]
    condensation_arcs: tuple[DependencyCondensationArc, ...]
    seed_closures: tuple[DependencySeedClosure, ...]

    def __post_init__(self) -> None:
        _require_domain_refs(self.node_refs, "analysis node_refs")
        raw_arcs = _require_tuple(self.arcs, "analysis arcs")
        if any(type(item) is not DependencyArc for item in raw_arcs):  # noqa: E721
            raise DependencyGraphError("analysis arcs must contain only DependencyArc values")
        raw_sccs = _require_tuple(self.sccs, "analysis SCCs")
        if any(type(item) is not DependencyScc for item in raw_sccs):  # noqa: E721
            raise DependencyGraphError("analysis SCCs must contain only DependencyScc values")
        raw_condensation = _require_tuple(self.condensation_arcs, "analysis condensation_arcs")
        if any(type(item) is not DependencyCondensationArc for item in raw_condensation):  # noqa: E721
            raise DependencyGraphError(
                "analysis condensation_arcs must contain only DependencyCondensationArc values"
            )
        raw_closures = _require_tuple(self.seed_closures, "analysis seed_closures")
        if any(type(item) is not DependencySeedClosure for item in raw_closures):  # noqa: E721
            raise DependencyGraphError(
                "analysis seed_closures must contain only DependencySeedClosure values"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "arcs": [item.to_mapping() for item in self.arcs],
            "condensation_arcs": [item.to_mapping() for item in self.condensation_arcs],
            "node_refs": [item.to_mapping() for item in self.node_refs],
            "sccs": [item.to_mapping() for item in self.sccs],
            "seed_closures": [item.to_mapping() for item in self.seed_closures],
        }


def analyze_dependency_graph(
    nodes: tuple[SemanticObjectRef, ...],
    arcs: tuple[DependencyArc, ...],
    seeds: tuple[DependencySeed, ...],
) -> DependencyGraphAnalysis:
    """Compute canonical SCC condensation and forward reachability iteratively.

    This is intentionally only an algorithmic analysis.  In particular, a
    supplied frontier is carried through unchanged and never becomes a bounded
    admission claim here.
    """

    supplied_nodes = _require_domain_refs(nodes, "dependency graph nodes")
    node_keys = tuple(_ref_key(item) for item in supplied_nodes)
    if len(set(node_keys)) != len(node_keys):
        raise DependencyGraphError("dependency graph nodes must not contain duplicates")
    ordered_nodes = tuple(sorted(supplied_nodes, key=_ref_key))
    node_set = frozenset(ordered_nodes)

    raw_arcs = _require_tuple(arcs, "dependency graph arcs")
    if any(type(item) is not DependencyArc for item in raw_arcs):  # noqa: E721
        raise DependencyGraphError("dependency graph arcs must contain only DependencyArc values")
    typed_arcs = tuple(item for item in raw_arcs if type(item) is DependencyArc)
    if any(item.from_ref not in node_set or item.to_ref not in node_set for item in typed_arcs):
        raise DependencyGraphError("dependency graph nodes must include every arc endpoint")
    ordered_arcs = tuple(
        arc
        for _key, arc in sorted(
            ((item.canonical_key, item) for item in typed_arcs), key=lambda item: item[0]
        )
    )
    deduplicated_arcs = tuple(
        arc for index, arc in enumerate(ordered_arcs) if index == 0 or arc != ordered_arcs[index - 1]
    )

    raw_seeds = _require_tuple(seeds, "dependency graph seeds")
    if any(type(item) is not DependencySeed for item in raw_seeds):  # noqa: E721
        raise DependencyGraphError("dependency graph seeds must contain only DependencySeed values")
    typed_seeds = tuple(item for item in raw_seeds if type(item) is DependencySeed)
    if len({item.seed_id for item in typed_seeds}) != len(typed_seeds):
        raise DependencyGraphError("dependency graph seed_id values must be unique")
    if any(root not in node_set for seed in typed_seeds for root in seed.root_refs):
        raise DependencyGraphError("dependency graph nodes must include every seed root")
    ordered_seeds = tuple(sorted(typed_seeds, key=lambda item: item.seed_id))

    adjacency: dict[SemanticObjectRef, tuple[SemanticObjectRef, ...]] = {
        node: tuple() for node in ordered_nodes
    }
    reverse_adjacency: dict[SemanticObjectRef, tuple[SemanticObjectRef, ...]] = {
        node: tuple() for node in ordered_nodes
    }
    outgoing: dict[SemanticObjectRef, list[SemanticObjectRef]] = {node: [] for node in ordered_nodes}
    incoming: dict[SemanticObjectRef, list[SemanticObjectRef]] = {node: [] for node in ordered_nodes}
    for arc in deduplicated_arcs:
        outgoing[arc.from_ref].append(arc.to_ref)
        incoming[arc.to_ref].append(arc.from_ref)
    adjacency.update({node: tuple(sorted(items, key=_ref_key)) for node, items in outgoing.items()})
    reverse_adjacency.update(
        {node: tuple(sorted(items, key=_ref_key)) for node, items in incoming.items()}
    )

    components = _iterative_sccs(ordered_nodes, adjacency, reverse_adjacency)
    sccs = tuple(
        sorted(
            (
                DependencyScc(
                    sha256_bytes(canonical_json_bytes([node.to_mapping() for node in component])),
                    component,
                )
                for component in components
            ),
            key=lambda item: item.scc_sha256,
        )
    )
    scc_by_node = {
        node: scc.scc_sha256 for scc in sccs for node in scc.node_refs
    }
    condensation_pairs = {
        (scc_by_node[arc.from_ref], scc_by_node[arc.to_ref])
        for arc in deduplicated_arcs
        if scc_by_node[arc.from_ref] != scc_by_node[arc.to_ref]
    }
    condensation_arcs = tuple(
        DependencyCondensationArc(from_hash, to_hash)
        for from_hash, to_hash in sorted(condensation_pairs)
    )
    scc_adjacency: dict[str, tuple[str, ...]] = {scc.scc_sha256: tuple() for scc in sccs}
    scc_outgoing: dict[str, list[str]] = {scc.scc_sha256: [] for scc in sccs}
    for arc in condensation_arcs:
        scc_outgoing[arc.from_scc_sha256].append(arc.to_scc_sha256)
    scc_adjacency.update(
        {key: tuple(sorted(value)) for key, value in scc_outgoing.items()}
    )
    refs_by_scc = {scc.scc_sha256: scc.node_refs for scc in sccs}
    closures = tuple(
        _seed_closure(seed, scc_by_node, scc_adjacency, refs_by_scc) for seed in ordered_seeds
    )
    return DependencyGraphAnalysis(
        node_refs=ordered_nodes,
        arcs=deduplicated_arcs,
        sccs=sccs,
        condensation_arcs=condensation_arcs,
        seed_closures=closures,
    )


def _iterative_sccs(
    nodes: tuple[SemanticObjectRef, ...],
    adjacency: dict[SemanticObjectRef, tuple[SemanticObjectRef, ...]],
    reverse_adjacency: dict[SemanticObjectRef, tuple[SemanticObjectRef, ...]],
) -> tuple[tuple[SemanticObjectRef, ...], ...]:
    """Kosaraju's algorithm without recursion, in canonical traversal order."""

    visited: set[SemanticObjectRef] = set()
    finish_order: list[SemanticObjectRef] = []
    for start in nodes:
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[SemanticObjectRef, int]] = [(start, 0)]
        while stack:
            node, index = stack[-1]
            neighbors = adjacency[node]
            if index == len(neighbors):
                finish_order.append(node)
                stack.pop()
                continue
            neighbor = neighbors[index]
            stack[-1] = (node, index + 1)
            if neighbor not in visited:
                visited.add(neighbor)
                stack.append((neighbor, 0))

    components: list[tuple[SemanticObjectRef, ...]] = []
    assigned: set[SemanticObjectRef] = set()
    for start in reversed(finish_order):
        if start in assigned:
            continue
        assigned.add(start)
        component: list[SemanticObjectRef] = []
        reverse_stack: list[SemanticObjectRef] = [start]
        while reverse_stack:
            node = reverse_stack.pop()
            component.append(node)
            for neighbor in reversed(reverse_adjacency[node]):
                if neighbor not in assigned:
                    assigned.add(neighbor)
                    reverse_stack.append(neighbor)
        components.append(tuple(sorted(component, key=_ref_key)))
    return tuple(components)


def _seed_closure(
    seed: DependencySeed,
    scc_by_node: dict[SemanticObjectRef, str],
    scc_adjacency: dict[str, tuple[str, ...]],
    refs_by_scc: dict[str, tuple[SemanticObjectRef, ...]],
) -> DependencySeedClosure:
    roots = tuple(sorted(seed.root_refs, key=_ref_key))
    root_sccs = sorted({scc_by_node[item] for item in roots})
    reached: set[str] = set(root_sccs)
    pending = list(reversed(root_sccs))
    while pending:
        current = pending.pop()
        for successor in scc_adjacency[current]:
            if successor not in reached:
                reached.add(successor)
                pending.append(successor)
    hashes = tuple(sorted(reached))
    affected = tuple(sorted((item for key in hashes for item in refs_by_scc[key]), key=_ref_key))
    return DependencySeedClosure(
        seed_id=seed.seed_id,
        root_refs=roots,
        affected_refs=affected,
        frontier_refs=seed.frontier_refs,
        reachable_scc_sha256s=hashes,
    )
