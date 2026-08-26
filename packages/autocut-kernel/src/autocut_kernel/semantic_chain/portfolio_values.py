"""Acyclic Portfolio and initial Usage values, not persistence or admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ..contracts.compiler.canonical import canonical_json_hash
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .portfolio_search import CandidateAlternative, RequirementAssignment


class PortfolioValueError(ValueError):
    """Portfolio/initial ledger wire or internal reference identity is invalid."""


def _closed(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise PortfolioValueError("portfolio wire must be a closed mapping")
    item = cast(dict[str, object], value)
    if set(item) != keys:
        raise PortfolioValueError("portfolio wire has missing or unknown fields")
    return item


def _array(value: object) -> list[object]:
    if type(value) is not list:  # noqa: E721
        raise PortfolioValueError("portfolio wire collection must be an array")
    return cast(list[object], value)


def _integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value < 2**53:  # noqa: E721
        raise PortfolioValueError("portfolio integer must be JSON-safe")
    return value


def _hash(value: object) -> str:
    # Validate wire syntax only, without implying Store reads.
    if type(value) is not str or len(value) != 71 or not value.startswith("sha256:"):  # noqa: E721
        raise PortfolioValueError("portfolio hash must be lowercase sha256")
    if any(ch not in "0123456789abcdef" for ch in value[7:]):
        raise PortfolioValueError("portfolio hash must be lowercase sha256")
    return value


@dataclass(frozen=True, slots=True)
class StorySelection:
    proposal_index: int
    proposal_ref: SemanticObjectRef

    def __post_init__(self) -> None:
        _integer(self.proposal_index)
        if (type(self.proposal_ref) is not SemanticObjectRef  # noqa: E721
                or self.proposal_ref.member_ref.artifact_type != "proposal_set"
                or self.proposal_ref.object_type != "proposal"):
            raise PortfolioValueError("selected proposal requires its exact ProposalSet owner")

    @property
    def story_id(self) -> str:
        return canonical_json_hash({
            "schema_version": "stage2-story-id-v1",
            "proposal_set_ref": self.proposal_ref.member_ref.to_mapping(),
            "proposal_id": self.proposal_ref.object_id,
        })

    def to_mapping(self) -> dict[str, object]:
        return {"proposal_index": self.proposal_index, "proposal_ref": self.proposal_ref.to_mapping(), "story_id": self.story_id}

    @classmethod
    def from_mapping(cls, value: object) -> StorySelection:
        item = _closed(value, {"proposal_index", "proposal_ref", "story_id"})
        selection = cls(_integer(item["proposal_index"]), SemanticObjectRef.from_mapping(item["proposal_ref"]))
        if _hash(item["story_id"]) != selection.story_id:
            raise PortfolioValueError("Story ID differs from its completed ProposalSet identity")
        return selection


def _assignment(value: object) -> RequirementAssignment:
    item = _closed(value, {"proposal_index", "requirement_id", "alternative"})
    alternative = _closed(item["alternative"], {"candidate_ref", "source_ref"})
    if type(item["requirement_id"]) is not str:  # noqa: E721
        raise PortfolioValueError("requirement ID must be a string")
    return RequirementAssignment(
        _integer(item["proposal_index"]), item["requirement_id"],
        CandidateAlternative(SemanticObjectRef.from_mapping(alternative["candidate_ref"]), SemanticObjectRef.from_mapping(alternative["source_ref"])),
    )


@dataclass(frozen=True, slots=True)
class StoryPortfolio:
    proposal_set_ref: SemanticMemberIdentity
    job_policy_sha256: str
    selections: tuple[StorySelection, ...]
    requirement_assignments: tuple[RequirementAssignment, ...]
    visited_states: int

    def __post_init__(self) -> None:
        if type(self.proposal_set_ref) is not SemanticMemberIdentity or self.proposal_set_ref.artifact_type != "proposal_set":  # noqa: E721
            raise PortfolioValueError("portfolio requires its exact ProposalSet identity")
        _hash(self.job_policy_sha256)
        _integer(self.visited_states, minimum=1)
        if type(self.selections) is not tuple or not self.selections or any(type(item) is not StorySelection for item in self.selections):  # noqa: E721
            raise PortfolioValueError("portfolio selections must be nonempty exact tuple values")
        indexes = tuple(item.proposal_index for item in self.selections)
        if indexes != tuple(sorted(set(indexes))) or len(set(self.target_story_ids)) != len(indexes):
            raise PortfolioValueError("portfolio selected indexes/Story IDs must be unique and ordered")
        if any(item.proposal_ref.member_ref != self.proposal_set_ref for item in self.selections):
            raise PortfolioValueError("portfolio selections refer to different ProposalSets")
        if type(self.requirement_assignments) is not tuple or any(type(item) is not RequirementAssignment for item in self.requirement_assignments):  # noqa: E721
            raise PortfolioValueError("portfolio assignments must be an exact tuple")
        rows = tuple((item.proposal_index, item.requirement_id) for item in self.requirement_assignments)
        if len(set(rows)) != len(rows) or {pi for pi, _ in rows} != set(indexes):
            raise PortfolioValueError("every selected Story needs distinct material assignments")
        if tuple(pi for pi, _ in rows) != tuple(sorted(pi for pi, _ in rows)):
            raise PortfolioValueError("assignments must preserve selected proposal order")
        candidates = {item.alternative.candidate_ref.member_ref for item in self.requirement_assignments}
        sources = {item.alternative.source_ref.member_ref for item in self.requirement_assignments}
        if len(candidates) != 1 or len(sources) != 1 or any(ref.scope != self.proposal_set_ref.scope for ref in candidates | sources):
            raise PortfolioValueError("portfolio assignments have mixed Catalog/Source owners or scopes")
        candidate_sources: dict[SemanticObjectRef, SemanticObjectRef] = {}
        for row in self.requirement_assignments:
            alternative = row.alternative
            previous = candidate_sources.setdefault(alternative.candidate_ref, alternative.source_ref)
            if previous != alternative.source_ref:
                raise PortfolioValueError("one exact candidate cannot refer to different sources")

    @property
    def target_story_ids(self) -> tuple[str, ...]:
        return tuple(item.story_id for item in self.selections)

    @property
    def target_story_ids_hash(self) -> str:
        return canonical_json_hash(list(self.target_story_ids))

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": "stage2-story-portfolio-v1",
            "proposal_set_ref": self.proposal_set_ref.to_mapping(),
            "job_policy_sha256": self.job_policy_sha256,
            "completion_policy": "all_or_nothing", "search_status": "feasible",
            "selection_records": [item.to_mapping() for item in self.selections],
            "requirement_assignments": [item.to_mapping() for item in self.requirement_assignments],
            "target_story_ids": list(self.target_story_ids), "target_story_ids_hash": self.target_story_ids_hash,
            "visited_states": self.visited_states,
        }

    @classmethod
    def from_mapping(cls, value: object) -> StoryPortfolio:
        item = _closed(value, {"schema_version", "proposal_set_ref", "job_policy_sha256", "completion_policy", "search_status", "selection_records", "requirement_assignments", "target_story_ids", "target_story_ids_hash", "visited_states"})
        if item["schema_version"] != "stage2-story-portfolio-v1" or item["completion_policy"] != "all_or_nothing" or item["search_status"] != "feasible":
            raise PortfolioValueError("portfolio wire version/state is unsupported")
        result = cls(SemanticMemberIdentity.from_mapping(item["proposal_set_ref"]), _hash(item["job_policy_sha256"]),
                     tuple(StorySelection.from_mapping(row) for row in _array(item["selection_records"])),
                     tuple(_assignment(row) for row in _array(item["requirement_assignments"])), _integer(item["visited_states"], minimum=1))
        if tuple(_hash(value) for value in _array(item["target_story_ids"])) != result.target_story_ids or _hash(item["target_story_ids_hash"]) != result.target_story_ids_hash:
            raise PortfolioValueError("portfolio target set/order/hash differs from its selections")
        return result

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class InitialSourceUsageLedger:
    """Stage 2 pending-only ledger; never invents Stage 4 reservations."""

    portfolio_ref: SemanticMemberIdentity
    target_story_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.portfolio_ref) is not SemanticMemberIdentity or self.portfolio_ref.artifact_type != "portfolio":  # noqa: E721
            raise PortfolioValueError("initial usage requires the exact completed Portfolio identity")
        if type(self.target_story_ids) is not tuple or not self.target_story_ids:  # noqa: E721
            raise PortfolioValueError("initial usage needs a nonempty frozen target tuple")
        for story in self.target_story_ids:
            _hash(story)
        if len(set(self.target_story_ids)) != len(self.target_story_ids):
            raise PortfolioValueError("initial usage targets must be unique")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": "stage2-initial-source-usage-v1", "portfolio_ref": self.portfolio_ref.to_mapping(),
            "target_story_ids_hash": canonical_json_hash(list(self.target_story_ids)),
            "rows": [{"story_id": story, "priority_index": index, "status": "pending", "reservations": []} for index, story in enumerate(self.target_story_ids)],
            "next_priority_index": 0, "finalized": False,
        }

    @classmethod
    def from_mapping(cls, value: object) -> InitialSourceUsageLedger:
        item = _closed(value, {"schema_version", "portfolio_ref", "target_story_ids_hash", "rows", "next_priority_index", "finalized"})
        if item["schema_version"] != "stage2-initial-source-usage-v1" or type(item["next_priority_index"]) is not int or item["next_priority_index"] != 0 or item["finalized"] is not False:  # noqa: E721
            raise PortfolioValueError("initial ledger cannot claim an advanced or finalized state")
        targets: list[str] = []
        for index, value in enumerate(_array(item["rows"])):
            row = _closed(value, {"story_id", "priority_index", "status", "reservations"})
            if _integer(row["priority_index"]) != index or row["status"] != "pending" or _array(row["reservations"]):
                raise PortfolioValueError("initial usage row must be pending, ordered and unreserved")
            targets.append(_hash(row["story_id"]))
        result = cls(SemanticMemberIdentity.from_mapping(item["portfolio_ref"]), tuple(targets))
        if _hash(item["target_story_ids_hash"]) != canonical_json_hash(targets):
            raise PortfolioValueError("initial usage target hash is not its complete ordered target set")
        return result

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())
