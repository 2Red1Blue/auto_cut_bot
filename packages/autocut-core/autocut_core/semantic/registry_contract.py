#!/usr/bin/env python3
"""Pure local contract checks for model-generated Series Registry objects."""

from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any


POLICY_VERSION = "series-registry-contract-v2-thread-scoped-activity"
RELATIONSHIP_CLOSURE_THRESHOLD = 2

CANONICAL_NAME_COLLISION = "registry_canonical_name_collision"
ALIAS_CANONICAL_COLLISION = "registry_alias_canonical_collision"
ALIAS_OWNER_COLLISION = "registry_alias_owner_collision"
RELATIONSHIP_UNCOVERED = "registry_relationship_uncovered"
UNKNOWN_CHARACTER_REFERENCE = "registry_unknown_character_reference"
UNKNOWN_EVENT_REFERENCE = "registry_unknown_event_reference"
UNKNOWN_OPEN_QUESTION_REFERENCE = "registry_unknown_open_question_reference"


@dataclass(frozen=True)
class RegistryContractFinding:
    code: str
    message: str
    character_ids: tuple[str, ...]
    value: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "character_ids": list(self.character_ids),
        }
        if self.value is not None:
            payload["value"] = self.value
        return payload

    def as_error(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class RegistryContractResult:
    findings: tuple[RegistryContractFinding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def errors(self) -> list[str]:
        return [item.as_error() for item in self.findings]

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_version": POLICY_VERSION,
            "ok": self.ok,
            "findings": [item.as_dict() for item in self.findings],
        }


def normalize_character_name(name: Any) -> str:
    """Match the Series Bible v1.3 normalization contract."""

    stripped = " ".join(str(name).split())
    stripped = unicodedata.normalize("NFKC", stripped)
    stripped = "".join(
        char for char in stripped if not unicodedata.combining(char)
    )
    return stripped.casefold()


def validate_series_registry_contract(
    registry: dict[str, Any],
    *,
    known_event_ids: set[str] | None = None,
    event_index: list[dict[str, Any]] | None = None,
) -> RegistryContractResult:
    """Collect every cross-record Registry violation without mutating input."""

    characters = [
        item
        for item in registry.get("characters", [])
        if isinstance(item, dict)
    ]
    findings: list[RegistryContractFinding] = []
    character_ids = {
        str(item.get("id"))
        for item in characters
        if isinstance(item.get("id"), str)
    }
    question_ids = {
        str(item.get("id"))
        for item in registry.get("open_questions", []) or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }

    def record_unknown_refs(
        *,
        code: str,
        values: Any,
        known: set[str],
        where: str,
        refs_are_characters: bool = False,
    ) -> None:
        if not isinstance(values, list):
            return
        unknown = tuple(
            sorted(
                {
                    item
                    for item in values
                    if isinstance(item, str) and item not in known
                }
            )
        )
        if not unknown:
            return
        findings.append(
            RegistryContractFinding(
                code=code,
                message=f"{where} contains unknown IDs: {list(unknown)}",
                character_ids=unknown if refs_are_characters else (),
                value=where,
            )
        )

    for character in characters:
        character_id = str(character.get("id") or "?")
        if known_event_ids is not None:
            record_unknown_refs(
                code=UNKNOWN_EVENT_REFERENCE,
                values=[character.get("first_event_id")],
                known=known_event_ids,
                where=f"characters[{character_id}].first_event_id",
            )
            record_unknown_refs(
                code=UNKNOWN_EVENT_REFERENCE,
                values=character.get("evidence_event_ids"),
                known=known_event_ids,
                where=f"characters[{character_id}].evidence_event_ids",
            )

    for relationship in registry.get("relationships", []) or []:
        if not isinstance(relationship, dict):
            continue
        relationship_id = str(relationship.get("id") or "?")
        record_unknown_refs(
            code=UNKNOWN_CHARACTER_REFERENCE,
            values=relationship.get("character_ids"),
            known=character_ids,
            where=f"relationships[{relationship_id}].character_ids",
            refs_are_characters=True,
        )
        if known_event_ids is not None:
            record_unknown_refs(
                code=UNKNOWN_EVENT_REFERENCE,
                values=[
                    item.get("event_id")
                    for item in relationship.get("state_changes", []) or []
                    if isinstance(item, dict)
                ],
                known=known_event_ids,
                where=f"relationships[{relationship_id}].state_changes",
            )

    for fact in registry.get("facts", []) or []:
        if not isinstance(fact, dict) or known_event_ids is None:
            continue
        fact_id = str(fact.get("id") or "?")
        record_unknown_refs(
            code=UNKNOWN_EVENT_REFERENCE,
            values=fact.get("event_ids"),
            known=known_event_ids,
            where=f"facts[{fact_id}].event_ids",
        )

    for question in registry.get("open_questions", []) or []:
        if not isinstance(question, dict) or known_event_ids is None:
            continue
        question_id = str(question.get("id") or "?")
        record_unknown_refs(
            code=UNKNOWN_EVENT_REFERENCE,
            values=question.get("event_ids"),
            known=known_event_ids,
            where=f"open_questions[{question_id}].event_ids",
        )

    for thread in registry.get("story_threads", []) or []:
        if not isinstance(thread, dict):
            continue
        thread_id = str(thread.get("id") or "?")
        record_unknown_refs(
            code=UNKNOWN_CHARACTER_REFERENCE,
            values=thread.get("character_ids"),
            known=character_ids,
            where=f"story_threads[{thread_id}].character_ids",
            refs_are_characters=True,
        )
        record_unknown_refs(
            code=UNKNOWN_OPEN_QUESTION_REFERENCE,
            values=thread.get("open_question_ids"),
            known=question_ids,
            where=f"story_threads[{thread_id}].open_question_ids",
        )
        if known_event_ids is not None:
            record_unknown_refs(
                code=UNKNOWN_EVENT_REFERENCE,
                values=thread.get("anchor_event_ids"),
                known=known_event_ids,
                where=f"story_threads[{thread_id}].anchor_event_ids",
            )

    canonical_owners: dict[str, list[str]] = {}
    canonical_values: dict[str, str] = {}
    for character in characters:
        character_id = str(character.get("id") or "?")
        raw_name = str(character.get("canonical_name") or "")
        normalized = normalize_character_name(raw_name)
        if not normalized:
            continue
        canonical_owners.setdefault(normalized, []).append(character_id)
        canonical_values.setdefault(normalized, raw_name)
    for normalized, owners in sorted(canonical_owners.items()):
        unique_owners = tuple(sorted(set(owners)))
        if len(unique_owners) <= 1:
            continue
        value = canonical_values[normalized]
        findings.append(
            RegistryContractFinding(
                code=CANONICAL_NAME_COLLISION,
                message=(
                    f"canonical_name {value!r} resolves to multiple characters: "
                    + ", ".join(unique_owners)
                ),
                character_ids=unique_owners,
                value=value,
            )
        )

    canonical_owner = {
        normalized: owners[0]
        for normalized, owners in canonical_owners.items()
        if len(set(owners)) == 1
    }
    alias_owners: dict[str, set[str]] = {}
    alias_values: dict[str, str] = {}
    for character in characters:
        character_id = str(character.get("id") or "?")
        for alias in character.get("aliases", []) or []:
            normalized = normalize_character_name(alias)
            if not normalized:
                continue
            alias_values.setdefault(normalized, str(alias))
            owner = canonical_owner.get(normalized)
            if owner is not None and owner != character_id:
                findings.append(
                    RegistryContractFinding(
                        code=ALIAS_CANONICAL_COLLISION,
                        message=(
                            f"alias {str(alias)!r} on {character_id} is the "
                            f"canonical_name of {owner}"
                        ),
                        character_ids=tuple(sorted((character_id, owner))),
                        value=str(alias),
                    )
                )
            alias_owners.setdefault(normalized, set()).add(character_id)
    for normalized, owners in sorted(alias_owners.items()):
        unique_owners = tuple(sorted(owners))
        if len(unique_owners) <= 1:
            continue
        value = alias_values[normalized]
        findings.append(
            RegistryContractFinding(
                code=ALIAS_OWNER_COLLISION,
                message=(
                    f"alias {value!r} is owned by multiple characters: "
                    + ", ".join(unique_owners)
                ),
                character_ids=unique_owners,
                value=value,
            )
        )

    event_episode = {
        item["id"]: int(item["episode"])
        for item in event_index or []
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("episode"), int)
    }
    thread_participation: Counter[str] = Counter()
    for thread in registry.get("story_threads", []) or []:
        if not isinstance(thread, dict):
            continue
        for character_id in thread.get("character_ids", []) or []:
            if isinstance(character_id, str):
                thread_participation[character_id] += 1
    related_ids: set[str] = set()
    for relationship in registry.get("relationships", []) or []:
        if not isinstance(relationship, dict):
            continue
        for character_id in relationship.get("character_ids", []) or []:
            if isinstance(character_id, str):
                related_ids.add(character_id)
    uncovered: list[str] = []
    for character in characters:
        if character.get("entity_type") != "individual":
            continue
        character_id = str(character.get("id") or "?")
        evidence_event_ids = [
            item
            for item in character.get("evidence_event_ids", []) or []
            if isinstance(item, str)
        ]
        distinct_episode_count = len(
            {
                event_episode[event_id]
                for event_id in evidence_event_ids
                if event_id in event_episode
            }
        )
        sustained_evidence = (
            distinct_episode_count >= RELATIONSHIP_CLOSURE_THRESHOLD
            if event_episode
            else len(evidence_event_ids) >= RELATIONSHIP_CLOSURE_THRESHOLD
        )
        participates_in_thread = thread_participation.get(character_id, 0) > 0
        if (
            participates_in_thread
            and sustained_evidence
            and character_id not in related_ids
        ):
            uncovered.append(character_id)
    if uncovered:
        character_ids = tuple(sorted(uncovered))
        findings.append(
            RegistryContractFinding(
                code=RELATIONSHIP_UNCOVERED,
                message=(
                    "thread-scoped sustained-evidence individual characters "
                    "have no relationship: "
                    + ", ".join(character_ids)
                ),
                character_ids=character_ids,
            )
        )

    findings.sort(
        key=lambda item: (
            item.code,
            item.value or "",
            item.character_ids,
        )
    )
    return RegistryContractResult(tuple(findings))


def assert_series_registry_contract(
    registry: dict[str, Any],
    *,
    known_event_ids: set[str] | None = None,
    event_index: list[dict[str, Any]] | None = None,
) -> None:
    result = validate_series_registry_contract(
        registry,
        known_event_ids=known_event_ids,
        event_index=event_index,
    )
    if not result.ok:
        raise ValueError("; ".join(result.errors))