"""Bounded exact subset-cover assignment over a complete editorial batch.

Keys are opaque identities, not evidence. The feasibility owner must construct
this universe from committed evidence. Positive verification is independent of
search; neither function proves physical capacity, canonicality or commitment.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import combinations
from typing import Literal, cast

from .editorial_models import (
    editorial_array,
    editorial_integer,
    editorial_mapping,
    editorial_text,
    editorial_tuple,
)


def _keys(value: object, *, nonempty: bool = False) -> tuple[str, ...]:
    keys = editorial_tuple(value, str, nonempty=nonempty)
    for key in keys:
        editorial_text(key)
    if keys != tuple(sorted(set(keys))):
        raise ValueError("search keys must be unique and sorted")
    return keys


@dataclass(frozen=True, slots=True)
class MaterialSearchCandidate:
    candidate_key: str
    source_key: str
    event_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        editorial_text(self.candidate_key)
        editorial_text(self.source_key)
        _keys(self.event_keys)

    def to_mapping(self) -> dict[str, object]:
        return {"candidate_key": self.candidate_key, "source_key": self.source_key,
                "event_keys": list(self.event_keys)}

    @classmethod
    def from_mapping(cls, value: object) -> MaterialSearchCandidate:
        item = editorial_mapping(value, ("candidate_key", "source_key", "event_keys"))
        return cls(editorial_text(item["candidate_key"]), editorial_text(item["source_key"]),
                   editorial_array(item["event_keys"], editorial_text))


@dataclass(frozen=True, slots=True)
class MaterialSearchAlternative:
    alternative_key: str
    required_event_keys: tuple[str, ...]
    candidates: tuple[MaterialSearchCandidate, ...]

    def __post_init__(self) -> None:
        editorial_text(self.alternative_key)
        _keys(self.required_event_keys, nonempty=True)
        values = editorial_tuple(self.candidates, MaterialSearchCandidate, nonempty=True)
        _keys(tuple(item.candidate_key for item in values), nonempty=True)

    def to_mapping(self) -> dict[str, object]:
        return {"alternative_key": self.alternative_key,
                "required_event_keys": list(self.required_event_keys),
                "candidates": [candidate.to_mapping() for candidate in self.candidates]}

    @classmethod
    def from_mapping(cls, value: object) -> MaterialSearchAlternative:
        item = editorial_mapping(value, ("alternative_key", "required_event_keys", "candidates"))
        return cls(editorial_text(item["alternative_key"]),
                   editorial_array(item["required_event_keys"], editorial_text),
                   editorial_array(item["candidates"], MaterialSearchCandidate.from_mapping))


@dataclass(frozen=True, slots=True)
class MaterialSearchRequirement:
    story_id: str
    requirement_id: str
    satisfaction: Literal["one_of", "all_of"]
    alternatives: tuple[MaterialSearchAlternative, ...]

    def __post_init__(self) -> None:
        editorial_text(self.story_id)
        editorial_text(self.requirement_id)
        if editorial_text(self.satisfaction) not in ("one_of", "all_of"):
            raise ValueError("unsupported material satisfaction")
        values = editorial_tuple(self.alternatives, MaterialSearchAlternative, nonempty=True)
        _keys(tuple(item.alternative_key for item in values), nonempty=True)

    def to_mapping(self) -> dict[str, object]:
        return {"story_id": self.story_id, "requirement_id": self.requirement_id,
                "satisfaction": self.satisfaction,
                "alternatives": [alternative.to_mapping() for alternative in self.alternatives]}

    @classmethod
    def from_mapping(cls, value: object) -> MaterialSearchRequirement:
        item = editorial_mapping(value, ("story_id", "requirement_id", "satisfaction", "alternatives"))
        # The constructor checks the closed enum; cast is not validation.
        return cls(editorial_text(item["story_id"]), editorial_text(item["requirement_id"]),
                   cast(Literal["one_of", "all_of"], item["satisfaction"]),
                   editorial_array(item["alternatives"], MaterialSearchAlternative.from_mapping))


@dataclass(frozen=True, slots=True)
class MaterialSearchChoice:
    story_id: str
    requirement_id: str
    alternative_key: str
    candidate_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        for value in (self.story_id, self.requirement_id, self.alternative_key):
            editorial_text(value)
        _keys(self.candidate_keys, nonempty=True)

    def to_mapping(self) -> dict[str, object]:
        return {"story_id": self.story_id, "requirement_id": self.requirement_id,
                "alternative_key": self.alternative_key, "candidate_keys": list(self.candidate_keys)}

    @classmethod
    def from_mapping(cls, value: object) -> MaterialSearchChoice:
        item = editorial_mapping(value, ("story_id", "requirement_id", "alternative_key", "candidate_keys"))
        return cls(editorial_text(item["story_id"]), editorial_text(item["requirement_id"]),
                   editorial_text(item["alternative_key"]),
                   editorial_array(item["candidate_keys"], editorial_text))


@dataclass(frozen=True, slots=True)
class MaterialSearchResult:
    status: Literal["feasible", "infeasible", "indeterminate"]
    choices: tuple[MaterialSearchChoice, ...]
    examined_states: int

    def __post_init__(self) -> None:
        if editorial_text(self.status) not in ("feasible", "infeasible", "indeterminate"):
            raise ValueError("unsupported material search status")
        editorial_tuple(self.choices, MaterialSearchChoice)
        editorial_integer(self.examined_states)
        if (self.status == "feasible") != bool(self.choices):
            raise ValueError("only complete feasible results carry choices")
        if self.status == "feasible" and self.examined_states < len(self.choices):
            raise ValueError("each selected subset must have been examined")

    def to_mapping(self) -> dict[str, object]:
        return {"status": self.status, "choices": [choice.to_mapping() for choice in self.choices],
                "examined_states": self.examined_states}

    @classmethod
    def from_mapping(cls, value: object) -> MaterialSearchResult:
        item = editorial_mapping(value, ("status", "choices", "examined_states"))
        return cls(cast(Literal["feasible", "infeasible", "indeterminate"], item["status"]),
                   editorial_array(item["choices"], MaterialSearchChoice.from_mapping),
                   editorial_integer(item["examined_states"]))


def _universe(requirements: tuple[MaterialSearchRequirement, ...], source_reuse: str) -> None:
    values = editorial_tuple(requirements, MaterialSearchRequirement, nonempty=True)
    if editorial_text(source_reuse) not in ("allow", "forbid"):
        raise ValueError("unsupported Source reuse policy")
    identities: set[tuple[str, str]] = set()
    seen_stories: set[str] = set()
    previous_story: str | None = None
    sources: dict[str, str] = {}
    for requirement in values:
        identity = (requirement.story_id, requirement.requirement_id)
        if identity in identities:
            raise ValueError("duplicate Story requirement")
        identities.add(identity)
        if requirement.story_id != previous_story:
            if requirement.story_id in seen_stories:
                raise ValueError("Story requirements must be contiguous in frozen order")
            seen_stories.add(requirement.story_id)
            previous_story = requirement.story_id
        for alternative in requirement.alternatives:
            for candidate in alternative.candidates:
                if sources.setdefault(candidate.candidate_key, candidate.source_key) != candidate.source_key:
                    raise ValueError("one exact candidate cannot name different Sources")


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


def _options(
    alternatives: tuple[MaterialSearchAlternative, ...], budget: _Budget,
) -> Iterator[tuple[MaterialSearchAlternative, tuple[MaterialSearchCandidate, ...]]]:
    for alternative in alternatives:
        required = set(alternative.required_event_keys)
        for size in range(1, len(alternative.candidates) + 1):
            for subset in combinations(alternative.candidates, size):
                budget.visit()
                if required.issubset(event for candidate in subset for event in candidate.event_keys):
                    yield alternative, subset


def search_editorial_materials(
    requirements: tuple[MaterialSearchRequirement, ...], *,
    source_reuse: Literal["allow", "forbid"], max_search_states: int,
) -> MaterialSearchResult:
    """First complete feasible assignment, with no eager powerset or recursion.

    Inspect alternatives in key order, subsets by cardinality then key tuple.
    Charge every inspected subset, including coverage failures and Source
    conflicts. Backtracking and terminal decisions are free. In particular an
    exact-budget final successful/exhaustive step can decide, not time out.
    """
    _universe(requirements, source_reuse)
    budget = _Budget(editorial_integer(max_search_states, minimum=1))
    slots = tuple((requirement, alternatives) for requirement in requirements
                  for alternatives in ((requirement.alternatives,) if requirement.satisfaction == "one_of"
                                       else tuple((alternative,) for alternative in requirement.alternatives)))
    cursors = [iter(_options(alternatives, budget)) for _, alternatives in slots]
    chosen: list[MaterialSearchChoice] = []
    chosen_sources: list[set[str]] = []
    owners: dict[str, tuple[str, int]] = {}
    try:
        while len(chosen) < len(slots):
            depth = len(chosen)
            requirement, alternatives = slots[depth]
            option = next(cursors[depth], None)
            if option is None:
                cursors[depth] = iter(_options(alternatives, budget))
                if not chosen:
                    return MaterialSearchResult("infeasible", (), budget.used)
                chosen.pop()
                for source in chosen_sources.pop():
                    if source_reuse == "forbid":
                        story, uses = owners[source]
                        if uses == 1:
                            del owners[source]
                        else:
                            owners[source] = (story, uses - 1)
                continue
            alternative, subset = option
            sources = {candidate.source_key for candidate in subset}
            if source_reuse == "forbid":
                if any(source in owners and owners[source][0] != requirement.story_id for source in sources):
                    continue
                for source in sources:
                    _, uses = owners.get(source, (requirement.story_id, 0))
                    owners[source] = (requirement.story_id, uses + 1)
            chosen.append(MaterialSearchChoice(requirement.story_id, requirement.requirement_id,
                                                alternative.alternative_key,
                                                tuple(candidate.candidate_key for candidate in subset)))
            chosen_sources.append(sources)
    except _ExhaustedError:
        return MaterialSearchResult("indeterminate", (), budget.used)
    return MaterialSearchResult("feasible", tuple(chosen), budget.used)


def verify_editorial_material_assignment(
    requirements: tuple[MaterialSearchRequirement, ...], choices: tuple[MaterialSearchChoice, ...], *,
    source_reuse: Literal["allow", "forbid"],
) -> None:
    """Direct complete positive witness check; does not call/replay search.

    This does not verify first-feasible canonicality, examined counts or a
    negative conclusion. Admission must separately reconstruct and recompute.
    """
    _universe(requirements, source_reuse)
    values = editorial_tuple(choices, MaterialSearchChoice, nonempty=True)
    cursor = 0
    owners: dict[str, set[str]] = {}
    for requirement in requirements:
        count = 1 if requirement.satisfaction == "one_of" else len(requirement.alternatives)
        rows = values[cursor:cursor + count]
        if len(rows) != count:
            raise ValueError("assignment omits a required alternative")
        expected_keys = tuple(alternative.alternative_key for alternative in requirement.alternatives)
        if requirement.satisfaction == "all_of" and tuple(row.alternative_key for row in rows) != expected_keys:
            raise ValueError("all_of must cover every alternative in canonical order")
        alternatives = {alternative.alternative_key: alternative for alternative in requirement.alternatives}
        for row in rows:
            if ((row.story_id, row.requirement_id) != (requirement.story_id, requirement.requirement_id)
                    or row.alternative_key not in alternatives):
                raise ValueError("assignment has a foreign or reordered requirement/alternative")
            alternative = alternatives[row.alternative_key]
            candidates = {candidate.candidate_key: candidate for candidate in alternative.candidates}
            if any(key not in candidates for key in row.candidate_keys):
                raise ValueError("assignment candidate is outside its whole alternative pool")
            events = {event for key in row.candidate_keys for event in candidates[key].event_keys}
            if not set(alternative.required_event_keys).issubset(events):
                raise ValueError("assignment does not cover the complete alternative event set")
            for key in row.candidate_keys:
                owners.setdefault(candidates[key].source_key, set()).add(requirement.story_id)
        cursor += count
    if cursor != len(values):
        raise ValueError("assignment contains extra choices")
    if source_reuse == "forbid" and any(len(stories) > 1 for stories in owners.values()):
        raise ValueError("assignment reuses a Source across Stories")
