"""Exact, bounded first-feasible portfolio search; no admission or I/O.

Inputs are already semantically eligible alternatives, not authoritative proof
of that eligibility. The caller must independently validate them against the
catalog, raw proposals and frozen policies. A source assignment is a feasibility
witness only, not a reservation of physical spans.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Literal, TypeVar, cast

from ..contracts.compiler.canonical import canonical_json_bytes
from .member_refs import SemanticObjectRef

_T = TypeVar("_T")


class PortfolioSearchError(ValueError):
    """Malformed finite search universe or assignment witness."""


def _integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value < 2**53:  # noqa: E721
        raise PortfolioSearchError("search count/index must be an exact safe integer")
    return value


def _text(value: object) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise PortfolioSearchError("search ID must be a nonempty UTF-8 string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PortfolioSearchError("search ID must be UTF-8") from error
    return value


def _tuple(value: object, kind: type[_T]) -> tuple[_T, ...]:
    if type(value) is not tuple:  # noqa: E721
        raise PortfolioSearchError("search collections must be immutable tuples")
    items = cast(tuple[object, ...], value)
    if any(type(item) is not kind for item in items):
        raise PortfolioSearchError("search collection contains a mistyped item")
    return cast(tuple[_T, ...], items)


def _ref(value: object, artifact_type: str, object_type: str) -> None:
    if (type(value) is not SemanticObjectRef  # noqa: E721
            or value.member_ref.artifact_type != artifact_type
            or value.object_type != object_type):
        raise PortfolioSearchError("search reference has the wrong exact owner/type")


@dataclass(frozen=True, slots=True)
class CandidateAlternative:
    candidate_ref: SemanticObjectRef
    source_ref: SemanticObjectRef

    def __post_init__(self) -> None:
        _ref(self.candidate_ref, "candidate_catalog", "candidate")
        _ref(self.source_ref, "whole_series_source_manifest", "source")

    def to_mapping(self) -> dict[str, object]:
        return {"candidate_ref": self.candidate_ref.to_mapping(), "source_ref": self.source_ref.to_mapping()}


@dataclass(frozen=True, slots=True)
class RequirementAlternatives:
    requirement_id: str
    alternatives: tuple[CandidateAlternative, ...]

    def __post_init__(self) -> None:
        _text(self.requirement_id)
        items = _tuple(self.alternatives, CandidateAlternative)
        keys = tuple(canonical_json_bytes(item.to_mapping()) for item in items)
        if keys != tuple(sorted(set(keys))) or len({item.candidate_ref for item in items}) != len(items):
            raise PortfolioSearchError("alternatives must have unique candidates in canonical order")


@dataclass(frozen=True, slots=True)
class ProposalAlternatives:
    proposal_index: int
    proposal_id: str
    requirements: tuple[RequirementAlternatives, ...]

    def __post_init__(self) -> None:
        _integer(self.proposal_index)
        _text(self.proposal_id)
        items = _tuple(self.requirements, RequirementAlternatives)
        if not items or len({item.requirement_id for item in items}) != len(items):
            raise PortfolioSearchError("a proposal requires nonempty uniquely identified requirements")


@dataclass(frozen=True, slots=True)
class RequirementAssignment:
    proposal_index: int
    requirement_id: str
    alternative: CandidateAlternative

    def __post_init__(self) -> None:
        _integer(self.proposal_index)
        _text(self.requirement_id)
        if type(self.alternative) is not CandidateAlternative:  # noqa: E721
            raise PortfolioSearchError("assignment requires an exact candidate alternative")

    def to_mapping(self) -> dict[str, object]:
        return {
            "proposal_index": self.proposal_index,
            "requirement_id": self.requirement_id,
            "alternative": self.alternative.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class PortfolioSearchResult:
    status: Literal["feasible", "infeasible", "indeterminate"]
    proposal_indexes: tuple[int, ...]
    assignment: tuple[RequirementAssignment, ...]
    visited_states: int


def _universe(
    proposals: tuple[ProposalAlternatives, ...], selected_story_count: int, source_reuse: str,
) -> None:
    items = _tuple(proposals, ProposalAlternatives)
    count = _integer(selected_story_count, minimum=1)
    if count > len(items) or tuple(item.proposal_index for item in items) != tuple(range(len(items))):
        raise PortfolioSearchError("proposal indexes/count must preserve the full draft universe")
    if len({item.proposal_id for item in items}) != len(items):
        raise PortfolioSearchError("proposal IDs must be unique")
    if type(source_reuse) is not str or source_reuse not in ("allow", "forbid"):  # noqa: E721
        raise PortfolioSearchError("source reuse policy is unsupported")
    sources: dict[SemanticObjectRef, SemanticObjectRef] = {}
    for proposal in items:
        for requirement in proposal.requirements:
            for alternative in requirement.alternatives:
                previous = sources.setdefault(alternative.candidate_ref, alternative.source_ref)
                if previous != alternative.source_ref:
                    raise PortfolioSearchError("one exact candidate cannot refer to different sources")


class _ExhaustedError(Exception):
    pass


@dataclass(slots=True)
class _Budget:
    limit: int
    used: int = 0

    def visit(self) -> None:
        if self.used == self.limit:
            raise _ExhaustedError
        self.used += 1


def _assign(
    proposals: tuple[ProposalAlternatives, ...], indexes: tuple[int, ...],
    source_reuse: str, budget: _Budget,
) -> tuple[RequirementAssignment, ...] | None:
    rows = tuple((pi, req) for pi in indexes for req in proposals[pi].requirements)
    # Explicit stack avoids a recursion limit hidden below the declared budget.
    cursors = [0] * len(rows)
    chosen: list[RequirementAssignment] = []
    owners: dict[SemanticObjectRef, tuple[int, int]] = {}
    while len(chosen) < len(rows):
        depth = len(chosen)
        pi, row = rows[depth]
        if cursors[depth] == len(row.alternatives):
            cursors[depth] = 0
            if not chosen:
                return None
            previous = chosen.pop()
            if source_reuse == "forbid":
                source = previous.alternative.source_ref
                owner, uses = owners[source]
                if uses == 1:
                    del owners[source]
                else:
                    owners[source] = (owner, uses - 1)
            continue
        budget.visit()
        alternative = row.alternatives[cursors[depth]]
        cursors[depth] += 1
        if source_reuse == "forbid":
            owner, uses = owners.get(alternative.source_ref, (pi, 0))
            if owner != pi:
                continue
            owners[alternative.source_ref] = (pi, uses + 1)
        chosen.append(RequirementAssignment(pi, row.requirement_id, alternative))
    return tuple(chosen)


def search_portfolio(
    proposals: tuple[ProposalAlternatives, ...], *, selected_story_count: int,
    source_reuse: str, max_search_states: int,
) -> PortfolioSearchResult:
    """First feasible index tuple and canonical assignment, or explicit unknown.

    Charge one state per tuple inspected and one per alternative edge inspected,
    including conflicting edges. Backtracking and a terminal decision cost no
    extra states. Never skip an unfinished tuple when the budget is exhausted.
    """
    _universe(proposals, selected_story_count, source_reuse)
    budget = _Budget(_integer(max_search_states, minimum=1))
    try:
        for indexes in combinations(range(len(proposals)), selected_story_count):
            budget.visit()
            assignment = _assign(proposals, indexes, source_reuse, budget)
            if assignment is not None:
                return PortfolioSearchResult("feasible", indexes, assignment, budget.used)
    except _ExhaustedError:
        return PortfolioSearchResult("indeterminate", (), (), budget.used)
    return PortfolioSearchResult("infeasible", (), (), budget.used)


def verify_assignment(
    proposals: tuple[ProposalAlternatives, ...], indexes: tuple[int, ...],
    assignment: tuple[RequirementAssignment, ...], *, selected_story_count: int,
    source_reuse: str,
) -> None:
    """Independent witness validity check, NOT canonicality or source admission.

    This checks every row against the supplied universe without calling the
    searcher. The Stage 2 evaluator must separately reconstruct that universe
    from exact evidence and establish first-feasible canonicality.
    """
    _universe(proposals, selected_story_count, source_reuse)
    selected = _tuple(indexes, int)
    if (len(selected) != selected_story_count or selected != tuple(sorted(set(selected)))
            or any(not 0 <= pi < len(proposals) for pi in selected)):
        raise PortfolioSearchError("assignment target indexes differ from selected count/order")
    expected = tuple((pi, req) for pi in selected for req in proposals[pi].requirements)
    witness = _tuple(assignment, RequirementAssignment)
    if len(expected) != len(witness):
        raise PortfolioSearchError("assignment must cover each required row exactly once")
    owners: dict[SemanticObjectRef, set[int]] = {}
    for (pi, req), actual in zip(expected, witness, strict=True):
        if (actual.proposal_index != pi or actual.requirement_id != req.requirement_id
                or actual.alternative not in req.alternatives):
            raise PortfolioSearchError("assignment row is foreign, reordered or missing")
        owners.setdefault(actual.alternative.source_ref, set()).add(pi)
    if source_reuse == "forbid" and any(len(values) > 1 for values in owners.values()):
        raise PortfolioSearchError("a source is assigned to different stories")
