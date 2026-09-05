"""Closed material-support evidence values, not Admission or Store authority.

Derived statuses validate only the supplied structural evidence. Independent
evaluation must reconstruct all candidates, facts, timing and authorization.
No field here claims physical safety or reserves a Source interval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Literal, TypeVar, cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from .candidate_duration import ConservativeDuration
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .story_design_compact_models import ProposalDraftSetV2, ProposalDraftV2
from .story_design_draft import ProposalDraftSet
from .story_design_models import ProposalDraft

MaterialStatus = Literal["supported", "unsupported", "indeterminate"]
MATERIAL_SUPPORT_SCHEMA_VERSION = "stage2-material-support-v1"
MATERIAL_SUPPORT_V2_SCHEMA_VERSION = "stage2-material-support-v2"
MATERIAL_REASON_CODES = frozenset({
    "source_forbidden", "source_not_allowed", "fact_not_declared", "fact_context_only",
    "fact_outside_support", "duration_insufficient", "candidate_tainted",
    "dependency_frontier_unknown",
    "candidate_confidence_below_policy", "measurement_confidence_below_policy",
    "required_measurement_missing",
})
_T = TypeVar("_T")


class MaterialSupportError(ValueError):
    """Material evidence or its actual input binding is malformed."""


def _text(value: object) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise MaterialSupportError("material text must be nonempty UTF-8")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise MaterialSupportError("material text must be UTF-8") from error
    return value


def _hash(value: object) -> str:
    text = _text(value)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", text) is None or text == "sha256:" + "0" * 64:
        raise MaterialSupportError("material hash must be nonzero lowercase sha256")
    return text


def _integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value < 2**53:  # noqa: E721
        raise MaterialSupportError("material count must be an exact safe integer")
    return value


def _closed(value: object, keys: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise MaterialSupportError("material wire value must be a closed object")
    data = cast(dict[str, object], value)
    if any(type(key) is not str for key in data) or set(data) != set(keys):  # noqa: E721
        raise MaterialSupportError("material object has missing or unknown fields")
    return data


def _array(value: object, decode: Callable[[object], _T]) -> tuple[_T, ...]:
    if type(value) is not list:  # noqa: E721
        raise MaterialSupportError("material wire collection must be an array")
    return tuple(decode(item) for item in cast(list[object], value))


def _tuple(value: object, kind: type[_T]) -> tuple[_T, ...]:
    if type(value) is not tuple:  # noqa: E721
        raise MaterialSupportError("material collection must be immutable")
    items = cast(tuple[object, ...], value)
    if any(type(item) is not kind for item in items):
        raise MaterialSupportError("material collection has invalid item types")
    return cast(tuple[_T, ...], items)


def _ref(value: object, artifact_type: str, object_type: str) -> SemanticObjectRef:
    if (type(value) is not SemanticObjectRef or value.member_ref.artifact_type != artifact_type  # noqa: E721
            or value.object_type != object_type):
        raise MaterialSupportError("material reference has a foreign owner/type")
    return value


def _refs(value: object, artifact_type: str, object_type: str) -> tuple[SemanticObjectRef, ...]:
    refs = _tuple(value, SemanticObjectRef)
    for ref in refs:
        _ref(ref, artifact_type, object_type)
    keys = tuple(canonical_json_bytes(ref.to_mapping()) for ref in refs)
    if keys != tuple(sorted(set(keys))):
        raise MaterialSupportError("material references must be canonical and unique")
    if len({ref.member_ref for ref in refs}) > 1:
        raise MaterialSupportError("material references mix exact owners")
    return refs


def _status(value: object, expected: MaterialStatus) -> None:
    if type(value) is not str or value != expected:  # noqa: E721
        raise MaterialSupportError("claimed material status differs from derived evidence")


@dataclass(frozen=True, slots=True)
class FactCarryWitness:
    graph_fact_ref: SemanticObjectRef
    vlm_fact_ref: SemanticObjectRef
    via_event_refs: tuple[SemanticObjectRef, ...]

    def __post_init__(self) -> None:
        graph = _ref(self.graph_fact_ref, "narrative_graph", "fact")
        raw = _ref(self.vlm_fact_ref, "vlm_semantic_pack", "vlm_fact")
        events = _refs(self.via_event_refs, "event_card_set", "event")
        if not events or graph.object_id != raw.object_id:
            raise MaterialSupportError("fact witness needs exact raw identity and direct events")
        if len({ref.member_ref.scope for ref in (graph, raw, *events)}) != 1:
            raise MaterialSupportError("fact witness mixes scopes")

    def to_mapping(self) -> dict[str, object]:
        return {"graph_fact_ref": self.graph_fact_ref.to_mapping(),
                "vlm_fact_ref": self.vlm_fact_ref.to_mapping(),
                "via_event_refs": [ref.to_mapping() for ref in self.via_event_refs]}

    @classmethod
    def from_mapping(cls, value: object) -> FactCarryWitness:
        data = _closed(value, ("graph_fact_ref", "vlm_fact_ref", "via_event_refs"))
        return cls(SemanticObjectRef.from_mapping(data["graph_fact_ref"]),
                   SemanticObjectRef.from_mapping(data["vlm_fact_ref"]),
                   _array(data["via_event_refs"], SemanticObjectRef.from_mapping))


@dataclass(frozen=True, slots=True)
class RequirementAlternativeProof:
    candidate_ref: SemanticObjectRef
    source_ref: SemanticObjectRef
    fact_witnesses: tuple[FactCarryWitness, ...]
    conservative_duration: ConservativeDuration

    def __post_init__(self) -> None:
        _ref(self.candidate_ref, "candidate_catalog", "candidate")
        _ref(self.source_ref, "whole_series_source_manifest", "source")
        witnesses = _tuple(self.fact_witnesses, FactCarryWitness)
        keys = tuple(canonical_json_bytes(item.graph_fact_ref.to_mapping()) for item in witnesses)
        if keys != tuple(sorted(set(keys))):
            raise MaterialSupportError("fact witnesses must be canonical and unique")
        for owners in (
            {item.graph_fact_ref.member_ref for item in witnesses},
            {item.vlm_fact_ref.member_ref for item in witnesses},
            {ref.member_ref for item in witnesses for ref in item.via_event_refs},
        ):
            if len(owners) > 1:
                raise MaterialSupportError("candidate witnesses mix exact fact/event owners")
        if type(self.conservative_duration) is not ConservativeDuration:  # noqa: E721
            raise MaterialSupportError("material duration must be an exact rational value")
        refs = (self.candidate_ref, self.source_ref,
                *(item.graph_fact_ref for item in witnesses))
        if len({ref.member_ref.scope for ref in refs}) != 1:
            raise MaterialSupportError("candidate material evidence mixes scopes")

    def to_mapping(self) -> dict[str, object]:
        return {
            "candidate_ref": self.candidate_ref.to_mapping(), "source_ref": self.source_ref.to_mapping(),
            "fact_witnesses": [item.to_mapping() for item in self.fact_witnesses],
            "conservative_duration": self.conservative_duration.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> RequirementAlternativeProof:
        data = _closed(value, ("candidate_ref", "source_ref", "fact_witnesses",
                              "conservative_duration"))
        return cls(
            SemanticObjectRef.from_mapping(data["candidate_ref"]),
            SemanticObjectRef.from_mapping(data["source_ref"]),
            _array(data["fact_witnesses"], FactCarryWitness.from_mapping),
            ConservativeDuration.from_mapping(data["conservative_duration"]),
        )


@dataclass(frozen=True, slots=True)
class ExclusionReasonCount:
    """One primary reason per excluded candidate; counts are not truth proofs."""

    reason_code: str
    count: int

    def __post_init__(self) -> None:
        if _text(self.reason_code) not in MATERIAL_REASON_CODES:
            raise MaterialSupportError("unsupported material exclusion reason")
        _integer(self.count, minimum=1)

    def to_mapping(self) -> dict[str, object]:
        return {"reason_code": self.reason_code, "count": self.count}

    @classmethod
    def from_mapping(cls, value: object) -> ExclusionReasonCount:
        data = _closed(value, ("reason_code", "count"))
        return cls(_text(data["reason_code"]), _integer(data["count"], minimum=1))


@dataclass(frozen=True, slots=True)
class RequirementMaterialSupport:
    requirement_id: str
    required_fact_refs: tuple[SemanticObjectRef, ...]
    minimum_usable_seconds: int
    physical_requirements_hash: str
    alternatives: tuple[RequirementAlternativeProof, ...]
    excluded_tainted_candidate_refs: tuple[SemanticObjectRef, ...]
    exclusion_reason_counts: tuple[ExclusionReasonCount, ...]
    examined_candidate_count: int

    def __post_init__(self) -> None:
        _text(self.requirement_id)
        facts = _refs(self.required_fact_refs, "narrative_graph", "fact")
        if not facts:
            raise MaterialSupportError("material obligation requires explicit facts")
        _integer(self.minimum_usable_seconds, minimum=1)
        _hash(self.physical_requirements_hash)
        alternatives = _tuple(self.alternatives, RequirementAlternativeProof)
        keys = tuple(canonical_json_bytes(item.candidate_ref.to_mapping()) for item in alternatives)
        if keys != tuple(sorted(set(keys))):
            raise MaterialSupportError("candidate alternatives must be canonical and unique")
        for item in alternatives:
            actual = tuple(witness.graph_fact_ref for witness in item.fact_witnesses)
            if actual != facts or item.conservative_duration.fraction < self.minimum_usable_seconds:
                raise MaterialSupportError("supported candidate lacks complete facts or duration")
        excluded = _refs(self.excluded_tainted_candidate_refs, "candidate_catalog", "candidate")
        if len({ref.member_ref for ref in (*excluded, *(item.candidate_ref for item in alternatives))}) > 1:
            raise MaterialSupportError("material requirement mixes Catalog owners")
        if set(excluded) & {item.candidate_ref for item in alternatives}:
            raise MaterialSupportError("tainted candidate cannot be a supported alternative")
        counts = _tuple(self.exclusion_reason_counts, ExclusionReasonCount)
        reasons = tuple(item.reason_code for item in counts)
        if reasons != tuple(sorted(set(reasons))):
            raise MaterialSupportError("exclusion reason counts must be canonical and unique")
        count = _integer(self.examined_candidate_count)
        if sum(item.count for item in counts) + len(alternatives) != count:
            raise MaterialSupportError("candidate disposition counts do not conserve examined universe")
        if len(excluded) > count - len(alternatives):
            raise MaterialSupportError("tainted exclusion set exceeds excluded universe")
        primary_tainted_count = sum(item.count for item in counts if item.reason_code == "candidate_tainted")
        if primary_tainted_count > len(excluded):
            raise MaterialSupportError("primary taint count exceeds explicit tainted candidate references")

    @property
    def status(self) -> MaterialStatus:
        if self.alternatives:
            return "supported"
        return "indeterminate" if any(
            item.reason_code == "dependency_frontier_unknown" for item in self.exclusion_reason_counts
        ) else "unsupported"

    def to_mapping(self) -> dict[str, object]:
        return {
            "requirement_id": self.requirement_id,
            "required_fact_refs": [ref.to_mapping() for ref in self.required_fact_refs],
            "minimum_usable_seconds": self.minimum_usable_seconds,
            "physical_requirements_hash": self.physical_requirements_hash,
            "alternatives": [item.to_mapping() for item in self.alternatives],
            "excluded_tainted_candidate_refs": [ref.to_mapping() for ref in self.excluded_tainted_candidate_refs],
            "exclusion_reason_counts": [item.to_mapping() for item in self.exclusion_reason_counts],
            "examined_candidate_count": self.examined_candidate_count, "status": self.status,
        }

    @classmethod
    def from_mapping(cls, value: object) -> RequirementMaterialSupport:
        data = _closed(value, ("requirement_id", "required_fact_refs", "minimum_usable_seconds",
                              "physical_requirements_hash", "alternatives", "excluded_tainted_candidate_refs",
                              "exclusion_reason_counts", "examined_candidate_count", "status"))
        result = cls(
            _text(data["requirement_id"]), _array(data["required_fact_refs"], SemanticObjectRef.from_mapping),
            _integer(data["minimum_usable_seconds"], minimum=1), _hash(data["physical_requirements_hash"]),
            _array(data["alternatives"], RequirementAlternativeProof.from_mapping),
            _array(data["excluded_tainted_candidate_refs"], SemanticObjectRef.from_mapping),
            _array(data["exclusion_reason_counts"], ExclusionReasonCount.from_mapping),
            _integer(data["examined_candidate_count"]),
        )
        _status(data["status"], result.status)
        return result


@dataclass(frozen=True, slots=True)
class ProposalMaterialSupport:
    proposal_index: int
    proposal: ProposalDraft | ProposalDraftV2
    requirements: tuple[RequirementMaterialSupport, ...]
    narrative_taint_seed_refs: tuple[SemanticObjectRef, ...]
    dependency_unknown: bool

    def __post_init__(self) -> None:
        _integer(self.proposal_index)
        if type(self.proposal) not in (ProposalDraft, ProposalDraftV2):
            raise MaterialSupportError("material row must preserve its typed proposal")
        rows = _tuple(self.requirements, RequirementMaterialSupport)
        if not rows or len(rows) != len(self.proposal.material_requirements):
            raise MaterialSupportError("material rows must cover all original requirements")
        for row, original in zip(rows, self.proposal.material_requirements, strict=True):
            if (row.requirement_id, row.minimum_usable_seconds, row.physical_requirements_hash) != (
                original.requirement_id, original.minimum_usable_seconds, original.physical_requirements_hash
            ):
                raise MaterialSupportError("material row changed original requirement identity/order")
        required = {ref for row in rows for ref in row.required_fact_refs}
        if required != set(self.proposal.required_fact_refs):
            raise MaterialSupportError("material rows do not conserve proposal required facts")
        _refs(self.narrative_taint_seed_refs, "coverage_ledger", "taint_seed")
        if type(self.dependency_unknown) is not bool:  # noqa: E721
            raise MaterialSupportError("dependency unknown must be an explicit boolean")

    @property
    def status(self) -> MaterialStatus:
        states = {row.status for row in self.requirements}
        if "unsupported" in states:
            return "unsupported"
        return "indeterminate" if "indeterminate" in states else "supported"

    def to_mapping(self) -> dict[str, object]:
        return {
            "proposal_index": self.proposal_index, "proposal": self.proposal.to_mapping(),
            "requirements": [row.to_mapping() for row in self.requirements],
            "narrative_taint_seed_refs": [ref.to_mapping() for ref in self.narrative_taint_seed_refs],
            "dependency_unknown": self.dependency_unknown, "status": self.status,
        }

    @classmethod
    def from_mapping(cls, value: object, *, proposal_version: int = 1) -> ProposalMaterialSupport:
        data = _closed(value, ("proposal_index", "proposal", "requirements",
                              "narrative_taint_seed_refs", "dependency_unknown", "status"))
        if type(proposal_version) is not int or proposal_version not in (1, 2):  # noqa: E721
            raise MaterialSupportError("unsupported material proposal version")
        proposal = (ProposalDraft.from_mapping(data["proposal"]) if proposal_version == 1
                    else ProposalDraftV2.from_mapping(data["proposal"]))
        result = cls(
            _integer(data["proposal_index"]), proposal,
            _array(data["requirements"], RequirementMaterialSupport.from_mapping),
            _array(data["narrative_taint_seed_refs"], SemanticObjectRef.from_mapping),
            cast(bool, data["dependency_unknown"]),
        )
        _status(data["status"], result.status)
        return result


@dataclass(frozen=True, slots=True)
class MaterialSupportEvaluation:
    input_binding_sha256: str
    draft_sha256: str
    candidate_catalog_ref: SemanticMemberIdentity
    source_grant_sha256: str
    proposals: tuple[ProposalMaterialSupport, ...]

    def __post_init__(self) -> None:
        for value in (self.input_binding_sha256, self.draft_sha256, self.source_grant_sha256):
            _hash(value)
        if (type(self.candidate_catalog_ref) is not SemanticMemberIdentity  # noqa: E721
                or self.candidate_catalog_ref.artifact_type != "candidate_catalog"):
            raise MaterialSupportError("material evaluation requires exact Catalog identity")
        rows = _tuple(self.proposals, ProposalMaterialSupport)
        if tuple(row.proposal_index for row in rows) != tuple(range(len(rows))):
            raise MaterialSupportError("material evaluation changed original proposal indexes")
        if len({row.proposal.proposal_id for row in rows}) != len(rows):
            raise MaterialSupportError("material evaluation duplicates proposal IDs")
        proposal_types = {type(row.proposal) for row in rows}
        if len(proposal_types) > 1:
            raise MaterialSupportError("material evaluation mixes v1 and v2 proposal codecs")
        if proposal_types == {ProposalDraftV2}:
            draft_hash = ProposalDraftSetV2(self.input_binding_sha256, tuple(cast(ProposalDraftV2, row.proposal) for row in rows)).canonical_hash
        else:
            draft_hash = ProposalDraftSet(self.input_binding_sha256, tuple(cast(ProposalDraft, row.proposal) for row in rows)).canonical_hash
        if draft_hash != self.draft_sha256:
            raise MaterialSupportError("material evaluation does not retain the exact bound draft")
        universe_counts: set[int] = set()
        candidate_sources: dict[SemanticObjectRef, SemanticObjectRef] = {}
        fact_sources: dict[SemanticObjectRef, SemanticObjectRef] = {}
        source_owners: set[SemanticMemberIdentity] = set()
        card_owners: set[SemanticMemberIdentity] = set()
        ledger_owners: set[SemanticMemberIdentity] = set()
        for proposal in rows:
            source_owners.update(ref.member_ref for ref in proposal.proposal.source_refs)
            ledger_owners.update(ref.member_ref for ref in proposal.narrative_taint_seed_refs)
            for row in proposal.requirements:
                universe_counts.add(row.examined_candidate_count)
                candidate_refs = (*row.excluded_tainted_candidate_refs,
                                  *(item.candidate_ref for item in row.alternatives))
                for ref in candidate_refs:
                    if ref.member_ref != self.candidate_catalog_ref:
                        raise MaterialSupportError("assessment names a different Catalog")
                if proposal.dependency_unknown and row.alternatives:
                    raise MaterialSupportError("unknown dependency frontier cannot supply safe alternatives")
                if not proposal.dependency_unknown and any(
                    item.reason_code == "dependency_frontier_unknown" for item in row.exclusion_reason_counts
                ):
                    raise MaterialSupportError("exclusion changed the dependency unknown state")
                for alternative in row.alternatives:
                    source_owners.add(alternative.source_ref.member_ref)
                    if candidate_sources.setdefault(alternative.candidate_ref, alternative.source_ref) != alternative.source_ref:
                        raise MaterialSupportError("one Candidate maps to inconsistent Sources")
                    for fact in alternative.fact_witnesses:
                        card_owners.update(ref.member_ref for ref in fact.via_event_refs)
                        if fact_sources.setdefault(fact.graph_fact_ref, fact.vlm_fact_ref) != fact.vlm_fact_ref:
                            raise MaterialSupportError("one Graph Fact maps to inconsistent raw owners")
            refs = (*proposal.proposal.narrative_refs, *proposal.narrative_taint_seed_refs)
            if any(ref.member_ref.scope != self.candidate_catalog_ref.scope for ref in refs):
                raise MaterialSupportError("material evaluation mixes scopes")
        if len(universe_counts) > 1:
            raise MaterialSupportError("requirements do not retain one complete candidate universe")
        if any(len(owners) > 1 for owners in (source_owners, card_owners, ledger_owners)):
            raise MaterialSupportError("material evaluation mixes exact Source/Card/Ledger owners")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": (MATERIAL_SUPPORT_V2_SCHEMA_VERSION if self.proposals and type(self.proposals[0].proposal) is ProposalDraftV2
                               else MATERIAL_SUPPORT_SCHEMA_VERSION),
            "input_binding_sha256": self.input_binding_sha256, "draft_sha256": self.draft_sha256,
            "candidate_catalog_ref": self.candidate_catalog_ref.to_mapping(),
            "source_grant_sha256": self.source_grant_sha256,
            "proposals": [row.to_mapping() for row in self.proposals],
        }

    @classmethod
    def from_mapping(cls, value: object) -> MaterialSupportEvaluation:
        data = _closed(value, ("schema_version", "input_binding_sha256", "draft_sha256",
                              "candidate_catalog_ref", "source_grant_sha256", "proposals"))
        version = data["schema_version"]
        if type(version) is not str or version not in (MATERIAL_SUPPORT_SCHEMA_VERSION, MATERIAL_SUPPORT_V2_SCHEMA_VERSION):  # noqa: E721
            raise MaterialSupportError("material evaluation has unsupported version")
        result = cls(
            _hash(data["input_binding_sha256"]), _hash(data["draft_sha256"]),
            SemanticMemberIdentity.from_mapping(data["candidate_catalog_ref"]),
            _hash(data["source_grant_sha256"]), _array(data["proposals"], lambda row: ProposalMaterialSupport.from_mapping(
                row, proposal_version=2 if version == MATERIAL_SUPPORT_V2_SCHEMA_VERSION else 1)),
        )
        if result.to_mapping()["schema_version"] != version:
            raise MaterialSupportError("material evaluation schema does not match proposal codec")
        return result

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())
