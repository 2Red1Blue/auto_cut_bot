"""Dependency proof content over pending members, never a KC admission token.

Structural checks recompute only the graph supplied by this value. Independent
verification must reconstruct the complete projection from exact inputs before
trusting a producer's bounded/unbounded claim. Ledger-local windows are expanded
only after the Ledger has a content hash; neither payload refers to itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, TypeVar, cast

from ..contracts.compiler.canonical import (
    canonical_json_bytes,
    canonical_json_hash,
    load_canonical_json_bytes,
)
from ..store.models import ArtifactMember, CommittedSemanticInputs
from .dependency_graph import (
    DependencyArc,
    DependencyCondensationArc,
    DependencyGraphAnalysis,
    DependencyGraphError,
    DependencyScc,
    DependencySeed,
    DependencySeedClosure,
    analyze_dependency_graph,
)
from .dependency_projection import DependencyProjectionPolicy, project_dependencies
from .ledger_models import CoverageLedger
from .member_refs import SemanticMemberIdentity, SemanticObjectRef

_T = TypeVar("_T")
_MEMBERS = ("source_member_ref", "graph_member_ref", "event_card_member_ref", "ledger_member_ref")
_TYPES = ("whole_series_source_manifest", "narrative_graph", "event_card_set", "coverage_ledger")
_HASHES = (
    "input_binding_sha256",
    "canonical_draft_sha256",
    "coverage_policy_sha256",
    "dependency_policy_sha256",
)


class DependencyProofError(ValueError):
    """Malformed proof content or an invalid pending-member projection."""


def _text(value: object) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise DependencyProofError("proof text must be non-empty UTF-8")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DependencyProofError("proof text must be valid UTF-8") from error
    return value


def _hash(value: object) -> str:
    result = _text(value)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", result) is None:
        raise DependencyProofError("proof hash must be lowercase sha256")
    return result


def _closed(value: object, keys: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise DependencyProofError("proof wire value must be a closed object")
    item = cast(dict[str, object], value)
    if any(type(key) is not str for key in item) or set(item) != set(keys):  # noqa: E721
        raise DependencyProofError("proof object has missing or unknown fields")
    return item


def _array(value: object, parse: Callable[[object], _T]) -> tuple[_T, ...]:
    if type(value) is not list:  # noqa: E721
        raise DependencyProofError("proof wire collection must be an array")
    return tuple(parse(item) for item in cast(list[object], value))


def _member_ref(value: object) -> SemanticMemberIdentity:
    try:
        return SemanticMemberIdentity.from_mapping(value)
    except ValueError as error:
        raise DependencyProofError("proof member identity is malformed") from error


def _ref(value: object) -> SemanticObjectRef:
    try:
        return SemanticObjectRef.from_mapping(value)
    except ValueError as error:
        raise DependencyProofError("proof object reference is malformed") from error


def _arc(value: object) -> DependencyArc:
    item = _closed(value, ("from_ref", "to_ref", "kind", "source_ref"))
    return DependencyArc(
        _ref(item["from_ref"]), _ref(item["to_ref"]), _text(item["kind"]), _ref(item["source_ref"])
    )


def _scc(value: object) -> DependencyScc:
    item = _closed(value, ("scc_sha256", "node_refs"))
    return DependencyScc(_hash(item["scc_sha256"]), _array(item["node_refs"], _ref))


def _condensation(value: object) -> DependencyCondensationArc:
    item = _closed(value, ("from_scc_sha256", "to_scc_sha256"))
    return DependencyCondensationArc(_hash(item["from_scc_sha256"]), _hash(item["to_scc_sha256"]))


def _seed_mapping(seed: DependencySeedClosure) -> dict[str, object]:
    return {
        **seed.to_mapping(),
        "isolation_status": "unbounded" if seed.frontier_refs else "bounded",
        "closure_hash": canonical_json_hash([ref.to_mapping() for ref in seed.affected_refs]),
    }


def _seed(value: object) -> DependencySeedClosure:
    item = _closed(
        value,
        (
            "seed_id",
            "root_refs",
            "affected_refs",
            "frontier_refs",
            "reachable_scc_sha256s",
            "isolation_status",
            "closure_hash",
        ),
    )
    seed = DependencySeedClosure(
        _text(item["seed_id"]),
        _array(item["root_refs"], _ref),
        _array(item["affected_refs"], _ref),
        _array(item["frontier_refs"], _ref),
        _array(item["reachable_scc_sha256s"], _hash),
    )
    derived = _seed_mapping(seed)
    if (
        _text(item["isolation_status"]) != derived["isolation_status"]
        or _hash(item["closure_hash"]) != derived["closure_hash"]
    ):
        raise DependencyProofError(
            "seed derived isolation or closure hash differs from its content"
        )
    return seed


@dataclass(frozen=True, slots=True)
class DependencyClosureProof:
    dependency_closure_proof_id: str
    input_binding_sha256: str
    canonical_draft_sha256: str
    coverage_policy_sha256: str
    dependency_policy_sha256: str
    source_member_ref: SemanticMemberIdentity
    graph_member_ref: SemanticMemberIdentity
    event_card_member_ref: SemanticMemberIdentity
    ledger_member_ref: SemanticMemberIdentity
    analysis: DependencyGraphAnalysis

    def __post_init__(self) -> None:
        _text(self.dependency_closure_proof_id)
        for name in _HASHES:
            _hash(getattr(self, name))
        identities: dict[str, SemanticMemberIdentity] = {}
        for name, kind in zip(_MEMBERS, _TYPES, strict=True):
            identity = getattr(self, name)
            if type(identity) is not SemanticMemberIdentity or identity.artifact_type != kind:
                raise DependencyProofError("proof metadata must bind the exact member types")
            identities[kind] = identity
        if len({identity.scope for identity in identities.values()}) != 1:
            raise DependencyProofError("proof members must share one exact scope")
        if type(self.analysis) is not DependencyGraphAnalysis:  # noqa: E721
            raise DependencyProofError("proof analysis must be an exact DependencyGraphAnalysis")

        # Reuse the single registered projection vocabulary, not a second table.
        contract = DependencyProjectionPolicy("semantic-dependencies-v1").to_mapping()
        owners = cast(dict[str, str], contract["canonical_owner_by_object_type"])
        edge_projections = cast(dict[str, str], contract["edge_projections"])
        arc_kinds = {
            kind
            for kind, projection in edge_projections.items()
            if projection in ("from_to", "to_from")
        }
        arc_kinds.update(cast(list[str], contract["attribute_projections"]))
        arc_kinds.update(cast(list[str], contract["external_root_projections"]))

        def owned(ref: SemanticObjectRef, *, edge: bool = False) -> None:
            owner = (
                "narrative_graph"
                if edge and ref.object_type == "edge"
                else owners.get(ref.object_type)
            )
            if owner is None or ref.member_ref != identities[owner]:
                raise DependencyProofError("proof reference has a foreign or noncanonical owner")
            if ref.object_type == "source_window":
                _hash(ref.object_id)

        for ref in self.analysis.node_refs:
            owned(ref)
        nodes = set(self.analysis.node_refs)
        for arc in self.analysis.arcs:
            owned(arc.from_ref)
            owned(arc.to_ref)
            owned(arc.source_ref, edge=True)
            if arc.kind not in arc_kinds:
                raise DependencyProofError("proof arc kind is not a registered propagation")
            if arc.source_ref.object_type != "edge" and arc.source_ref not in nodes:
                raise DependencyProofError("proof arc source is absent from its node universe")
        for scc in self.analysis.sccs:
            _hash(scc.scc_sha256)
        for arc in self.analysis.condensation_arcs:
            _hash(arc.from_scc_sha256)
            _hash(arc.to_scc_sha256)
        for seed in self.analysis.seed_closures:
            for ref in (*seed.root_refs, *seed.affected_refs, *seed.frontier_refs):
                owned(ref)
                if ref not in nodes:
                    raise DependencyProofError("proof seed references an absent node")
            for value in seed.reachable_scc_sha256s:
                _hash(value)
        try:
            recomputed = analyze_dependency_graph(
                self.analysis.node_refs,
                self.analysis.arcs,
                tuple(
                    DependencySeed(seed.seed_id, seed.root_refs, seed.frontier_refs)
                    for seed in self.analysis.seed_closures
                ),
            )
        except DependencyGraphError as error:
            raise DependencyProofError("proof graph or seed structure is not closed") from error
        if self.analysis != recomputed:
            raise DependencyProofError(
                "proof analysis is not the canonical recomputed graph closure"
            )

    @property
    def arc_set_hash(self) -> str:
        return canonical_json_hash([arc.to_mapping() for arc in self.analysis.arcs])

    @property
    def scc_set_hash(self) -> str:
        return canonical_json_hash([scc.to_mapping() for scc in self.analysis.sccs])

    def to_mapping(self) -> dict[str, object]:
        return {
            "dependency_closure_proof_id": self.dependency_closure_proof_id,
            "input_binding_sha256": self.input_binding_sha256,
            "canonical_draft_sha256": self.canonical_draft_sha256,
            "coverage_policy_sha256": self.coverage_policy_sha256,
            "dependency_policy_sha256": self.dependency_policy_sha256,
            "source_member_ref": self.source_member_ref.to_mapping(),
            "graph_member_ref": self.graph_member_ref.to_mapping(),
            "event_card_member_ref": self.event_card_member_ref.to_mapping(),
            "ledger_member_ref": self.ledger_member_ref.to_mapping(),
            "node_refs": [ref.to_mapping() for ref in self.analysis.node_refs],
            "dependency_arcs": [arc.to_mapping() for arc in self.analysis.arcs],
            "sccs": [scc.to_mapping() for scc in self.analysis.sccs],
            "condensation_arcs": [arc.to_mapping() for arc in self.analysis.condensation_arcs],
            "seed_proofs": [_seed_mapping(seed) for seed in self.analysis.seed_closures],
            "arc_set_hash": self.arc_set_hash,
            "scc_set_hash": self.scc_set_hash,
        }

    @classmethod
    def from_mapping(cls, value: object) -> DependencyClosureProof:
        item = _closed(
            value,
            (
                "dependency_closure_proof_id",
                *_HASHES,
                *_MEMBERS,
                "node_refs",
                "dependency_arcs",
                "sccs",
                "condensation_arcs",
                "seed_proofs",
                "arc_set_hash",
                "scc_set_hash",
            ),
        )
        try:
            analysis = DependencyGraphAnalysis(
                _array(item["node_refs"], _ref),
                _array(item["dependency_arcs"], _arc),
                _array(item["sccs"], _scc),
                _array(item["condensation_arcs"], _condensation),
                _array(item["seed_proofs"], _seed),
            )
            result = cls(
                _text(item["dependency_closure_proof_id"]),
                _hash(item["input_binding_sha256"]),
                _hash(item["canonical_draft_sha256"]),
                _hash(item["coverage_policy_sha256"]),
                _hash(item["dependency_policy_sha256"]),
                _member_ref(item["source_member_ref"]),
                _member_ref(item["graph_member_ref"]),
                _member_ref(item["event_card_member_ref"]),
                _member_ref(item["ledger_member_ref"]),
                analysis,
            )
        except DependencyGraphError as error:
            raise DependencyProofError("proof analysis contains malformed graph values") from error
        if (
            _hash(item["arc_set_hash"]) != result.arc_set_hash
            or _hash(item["scc_set_hash"]) != result.scc_set_hash
        ):
            raise DependencyProofError("proof set hashes differ from the supplied collections")
        return result

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


def build_dependency_proof(
    inputs: CommittedSemanticInputs,
    *,
    graph_member: ArtifactMember,
    event_card_member: ArtifactMember,
    ledger_member: ArtifactMember,
    policy: DependencyProjectionPolicy,
    revision: int,
) -> ArtifactMember:
    """Build pending proof content; the independent verifier decides completeness."""
    if (
        type(inputs) is not CommittedSemanticInputs
        or type(policy) is not DependencyProjectionPolicy
    ):
        raise DependencyProofError(
            "proof requires exact committed input values and explicit policy"
        )
    if type(revision) is not int or not 1 <= revision <= 2**53 - 1:  # noqa: E721
        raise DependencyProofError("proof revision must be a positive safe integer")
    members = (graph_member, event_card_member, ledger_member)
    if any(type(member) is not ArtifactMember or member.revision != revision for member in members):
        raise DependencyProofError("pending proof members must share the requested revision")
    try:
        # Strict byte parsing rejects duplicate keys/floats before sibling
        # projectors inspect JSON; formatting itself need not be canonical.
        payloads: list[object] = []
        for member in members:
            SemanticMemberIdentity.from_artifact_member(member)
            payload, _ = load_canonical_json_bytes(
                member.payload_json.encode("utf-8"), origin="dependency proof input"
            )
            payloads.append(payload)
        ledger = CoverageLedger.from_mapping(payloads[2])
        projected = project_dependencies(
            inputs,
            graph_member=graph_member,
            event_card_member=event_card_member,
            ledger_member=ledger_member,
            policy=policy,
        )
        ledger_ref = SemanticMemberIdentity.from_artifact_member(ledger_member)

        def expand(
            refs: tuple[SemanticObjectRef, ...], windows: tuple[str, ...]
        ) -> tuple[SemanticObjectRef, ...]:
            return tuple(
                sorted(
                    (
                        *refs,
                        *(SemanticObjectRef(ledger_ref, "coverage_window", key) for key in windows),
                    ),
                    key=lambda ref: canonical_json_bytes(ref.to_mapping()),
                )
            )

        seeds = tuple(
            DependencySeed(
                seed.seed_id,
                expand(seed.root_refs, seed.root_window_ids),
                expand(seed.frontier_refs, seed.frontier_window_ids),
            )
            for seed in ledger.taint_seeds
        )
        analysis = analyze_dependency_graph(projected.nodes, projected.arcs, seeds)
        source = inputs.source_manifest.reference
        proof = DependencyClosureProof(
            canonical_json_hash(
                {
                    "schema_version": "stage1-dependency-proof-id-v1",
                    "input_binding_sha256": ledger.input_binding_sha256,
                    "canonical_draft_sha256": ledger.draft_sha256,
                    "dependency_policy_sha256": policy.canonical_hash,
                }
            ),
            ledger.input_binding_sha256,
            ledger.draft_sha256,
            ledger.coverage_policy_sha256,
            policy.canonical_hash,
            SemanticMemberIdentity(
                source.artifact_type,
                source.logical_id,
                source.revision,
                source.scope,
                source.content_hash,
            ),
            SemanticMemberIdentity.from_artifact_member(graph_member),
            SemanticMemberIdentity.from_artifact_member(event_card_member),
            ledger_ref,
            analysis,
        )
        raw = canonical_json_bytes(proof.to_mapping())
        return ArtifactMember(
            "dependency_closure_proof",
            "dependency_closure_proof",
            revision,
            source.scope,
            proof.canonical_hash,
            raw.decode("utf-8"),
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        raise DependencyProofError(
            "pending dependency proof inputs are invalid or inconsistent"
        ) from error
