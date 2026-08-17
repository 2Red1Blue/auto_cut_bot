#!/usr/bin/env python3
"""Deterministic Series Assignment checks and safe canonicalization."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from autocut_core.io import json_sha256


POLICY_VERSION = "series-assignment-accounting-v5-typed-coda"
from autocut_core.libs.editorial_knowledge import load_knowledge_section

THREAD_KIND_ARC = "arc"
THREAD_KIND_CODA = "coda"

_ac = load_knowledge_section("assignment_contract") or {}
THREAD_KINDS = set(_ac.get("thread_kinds") or [THREAD_KIND_ARC, THREAD_KIND_CODA])
CODA_TERMINAL_PHASES = set(_ac.get("coda_terminal_phases") or {"payoff", "consequence", "coda"})
THREAD_BEAT_PHASE_ORDER = _ac.get("thread_beat_phase_order") or {
    "setup": 0,
    "escalation": 1,
    "turn": 2,
    "reveal": 3,
    "payoff": 4,
    "consequence": 5,
    "coda": 6,
}
REPAIR_DROP_LOCAL_CROSS_THREAD_DEPENDENCY = (
    "drop_local_cross_thread_dependency"
)
REPAIR_DROP_SELF_DEPENDENCY = "drop_self_dependency"
REPAIR_DROP_LOCAL_LATER_DEPENDENCY = "drop_local_later_dependency"
REPAIR_DEDUPLICATE_DEPENDENCY = "deduplicate_dependency"
REPAIR_RENAME_DUPLICATE_BEAT_ID = "rename_duplicate_beat_id"
REPAIR_REWRITE_COLLIDING_DEPENDENCY_ID = (
    "rewrite_colliding_dependency_id"
)
REPAIR_DROP_AMBIGUOUS_DEPENDENCY = "drop_ambiguous_dependency"
REPAIR_DROP_UNKNOWN_DEPENDENCY = "drop_unknown_dependency"
REPAIR_DROP_GLOBAL_CROSS_THREAD_DEPENDENCY = (
    "drop_global_cross_thread_dependency"
)
REPAIR_DROP_GLOBAL_LATER_DEPENDENCY = "drop_global_later_dependency"
REPAIR_DROP_CYCLIC_DEPENDENCY = "drop_cyclic_dependency"
REPAIR_DROP_REDUNDANT_INSUFFICIENT_EXCLUSION = (
    "drop_redundant_insufficient_evidence_exclusion"
)
REPAIR_ADD_REGISTRY_QUARANTINED_DEPENDENCY_EXCLUSION = (
    "add_registry_quarantined_dependency_exclusion"
)
REGISTRY_QUARANTINED_DEPENDENCY = "registry_quarantined_dependency"


@dataclass(frozen=True)
class AssignmentContractResult:
    """Result of validating and optionally canonicalizing one assignment."""

    effective_assignment: dict[str, Any]
    repairs: list[dict[str, Any]]
    errors: list[str]
    raw_sha256: str
    effective_sha256: str


@dataclass(frozen=True)
class GlobalAssignmentGraphResult:
    """Result of canonicalizing Beat identities and dependencies globally."""

    effective_assignments: list[dict[str, Any]]
    repairs: list[dict[str, Any]]
    errors: list[str]
    raw_sha256: str
    effective_sha256: str


def _error(code: str, detail: str) -> str:
    return f"{code}: {detail}"


def _known_episode(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    return None


def _valid_sha256(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _quarantine_explanation(language: str) -> str:
    if language == "en":
        return (
            "All known Events in this episode depend on quarantined Registry "
            "identities; the episode stays outside formal Story Threads until "
            "that quarantine is resolved."
        )
    return (
        "该集全部已知 Event 均依赖已隔离的 Registry 身份；身份隔离解除前，"
        "该集不进入正式 Story Thread。"
    )


def canonicalize_series_assignment(
    assignment: dict[str, Any],
    *,
    context_episodes: list[int] | set[int] | tuple[int, ...],
    event_by_id: dict[str, dict[str, Any]],
    registry_thread_ids: set[str],
    registry_thread_kinds: dict[str, str],
    repair_invalid_dependencies: bool = True,
    repair_redundant_exclusions: bool = True,
    registry_quarantined_event_ids: set[str] | None = None,
    registry_quarantine_sha256: str | None = None,
    registry_language: str = "zh",
    repair_quarantined_episodes: bool = True,
) -> AssignmentContractResult:
    """Validate an Assignment and apply deletion-only canonicalization."""

    raw_sha256 = json_sha256(assignment)
    effective = copy.deepcopy(assignment)
    errors: list[str] = []
    repairs: list[dict[str, Any]] = []
    missing_thread_kinds = sorted(
        thread_id
        for thread_id in registry_thread_ids
        if registry_thread_kinds.get(thread_id) not in THREAD_KINDS
    )
    if missing_thread_kinds:
        errors.append(
            _error(
                "assignment_registry_thread_kind_missing",
                "every admitted Registry Thread must declare thread_kind; "
                f"missing_or_invalid={missing_thread_kinds}",
            )
        )
    quarantined_event_ids = {
        item
        for item in registry_quarantined_event_ids or set()
        if isinstance(item, str) and item
    }
    quarantine_is_verified = _valid_sha256(registry_quarantine_sha256)
    if quarantined_event_ids and not quarantine_is_verified:
        errors.append(
            _error(
                "assignment_unverified_registry_quarantine",
                "quarantined Event IDs require a verified quarantine SHA-256",
            )
        )

    expected_episodes = {
        int(item)
        for item in context_episodes
        if isinstance(item, int) and not isinstance(item, bool)
    }
    declared_values = effective.get("episodes", [])
    declared_episodes = {
        int(item)
        for item in declared_values
        if isinstance(item, int) and not isinstance(item, bool)
    }
    if declared_episodes != expected_episodes or len(declared_values) != len(
        expected_episodes
    ):
        errors.append(
            _error(
                "assignment_episode_declaration_mismatch",
                "declared episodes must match context exactly; "
                f"declared={sorted(declared_episodes)}, "
                f"context={sorted(expected_episodes)}",
            )
        )

    beats = effective.get("thread_beats", [])
    exclusions = effective.get("excluded_episodes", [])
    if not isinstance(beats, list) or not isinstance(exclusions, list):
        errors.append(
            _error(
                "assignment_invalid_collections",
                "thread_beats and excluded_episodes must both be arrays",
            )
        )
        return AssignmentContractResult(
            effective_assignment=effective,
            repairs=repairs,
            errors=errors,
            raw_sha256=raw_sha256,
            effective_sha256=json_sha256(effective),
        )

    beat_by_id: dict[str, dict[str, Any]] = {}
    beat_ids_by_episode: dict[int, list[str]] = {}
    for index, beat in enumerate(beats):
        if not isinstance(beat, dict):
            errors.append(
                _error(
                    "assignment_invalid_beat",
                    f"thread_beats[{index}] must be an object",
                )
            )
            continue
        beat_id = beat.get("id")
        if not isinstance(beat_id, str) or not beat_id:
            errors.append(
                _error(
                    "assignment_invalid_beat_id",
                    f"thread_beats[{index}].id must be non-empty",
                )
            )
            continue
        if beat_id not in beat_by_id:
            beat_by_id[beat_id] = beat

        episode = _known_episode(beat.get("episode"))
        if episode is None or episode not in expected_episodes:
            errors.append(
                _error(
                    "assignment_unknown_episode",
                    f"{beat_id} uses episode {beat.get('episode')!r} "
                    f"outside {sorted(expected_episodes)}",
                )
            )
        else:
            beat_ids_by_episode.setdefault(episode, []).append(beat_id)

        thread_id = beat.get("thread_id")
        if not isinstance(thread_id, str) or thread_id not in registry_thread_ids:
            errors.append(
                _error(
                    "assignment_unknown_thread",
                    f"{beat_id} references unknown thread {thread_id!r}",
                )
            )
        elif (
            registry_thread_kinds.get(thread_id) == THREAD_KIND_CODA
            and beat.get("phase") not in CODA_TERMINAL_PHASES
        ):
            errors.append(
                _error(
                    "assignment_coda_phase_invalid",
                    f"{beat_id} belongs to typed coda Thread {thread_id} but "
                    f"uses non-terminal phase {beat.get('phase')!r}; use "
                    "payoff/consequence/coda and ensure the global coda "
                    "contains at least one phase=coda Beat",
                )
            )

        event_ids = beat.get("event_ids", [])
        if not isinstance(event_ids, list) or not event_ids:
            errors.append(
                _error(
                    "assignment_empty_beat_evidence",
                    f"{beat_id}.event_ids must contain at least one Event",
                )
            )
            continue
        if len(event_ids) != len(set(item for item in event_ids if isinstance(item, str))):
            errors.append(
                _error(
                    "assignment_duplicate_event_reference",
                    f"{beat_id}.event_ids contains duplicate IDs",
                )
            )
        for event_id in event_ids:
            event = event_by_id.get(event_id) if isinstance(event_id, str) else None
            if event is None:
                errors.append(
                    _error(
                        "assignment_unknown_event",
                        f"{beat_id} references unknown Event {event_id!r}",
                    )
                )
                continue
            if event_id in quarantined_event_ids:
                errors.append(
                    _error(
                        "assignment_quarantined_event_reference",
                        f"{beat_id} references quarantined Event {event_id}; "
                        "formal Thread Beats may use admitted evidence only",
                    )
                )
            if episode is not None and event.get("episode") != episode:
                errors.append(
                    _error(
                        "assignment_event_episode_mismatch",
                        f"{beat_id} declares episode {episode} but Event "
                        f"{event_id} belongs to episode {event.get('episode')!r}",
                    )
                )

    exclusion_by_episode: dict[int, dict[str, Any]] = {}
    for index, exclusion in enumerate(exclusions):
        if not isinstance(exclusion, dict):
            errors.append(
                _error(
                    "assignment_invalid_exclusion",
                    f"excluded_episodes[{index}] must be an object",
                )
            )
            continue
        episode = _known_episode(exclusion.get("episode"))
        if episode is None or episode not in expected_episodes:
            errors.append(
                _error(
                    "assignment_unknown_episode",
                    "excluded_episodes uses episode "
                    f"{exclusion.get('episode')!r} outside "
                    f"{sorted(expected_episodes)}",
                )
            )
            continue
        if episode in exclusion_by_episode:
            errors.append(
                _error(
                    "assignment_duplicate_exclusion",
                    f"episode {episode} is excluded more than once",
                )
            )
        else:
            exclusion_by_episode[episode] = exclusion

        event_ids = exclusion.get("event_ids", [])
        if not isinstance(event_ids, list):
            errors.append(
                _error(
                    "assignment_invalid_exclusion_events",
                    f"excluded episode {episode}.event_ids must be an array",
                )
            )
            continue
        reason_type = exclusion.get("reason_type")
        episode_event_ids = sorted(
            event_id
            for event_id, event in event_by_id.items()
            if event.get("episode") == episode
        )
        if reason_type == REGISTRY_QUARANTINED_DEPENDENCY:
            admitted_event_ids = sorted(
                set(episode_event_ids) - quarantined_event_ids
            )
            if not quarantine_is_verified:
                errors.append(
                    _error(
                        "assignment_invalid_registry_quarantine_exclusion",
                        f"episode {episode} uses {reason_type} without a "
                        "verified quarantine SHA-256",
                    )
                )
            if not episode_event_ids:
                errors.append(
                    _error(
                        "assignment_invalid_registry_quarantine_exclusion",
                        f"episode {episode} has no known Event evidence to "
                        "prove complete quarantine coverage",
                    )
                )
            if admitted_event_ids:
                errors.append(
                    _error(
                        "assignment_invalid_registry_quarantine_exclusion",
                        f"episode {episode} still has admitted Events: "
                        f"{admitted_event_ids}",
                    )
                )
            if event_ids:
                errors.append(
                    _error(
                        "assignment_quarantined_event_reference",
                        f"episode {episode} {reason_type} must keep event_ids "
                        "empty; evidence belongs only in the quarantine artifact",
                    )
                )
        if len(event_ids) != len(set(item for item in event_ids if isinstance(item, str))):
            errors.append(
                _error(
                    "assignment_duplicate_event_reference",
                    f"excluded episode {episode}.event_ids contains duplicate IDs",
                )
            )
        for event_id in event_ids:
            event = event_by_id.get(event_id) if isinstance(event_id, str) else None
            if event is None:
                errors.append(
                    _error(
                        "assignment_unknown_event",
                        "excluded episode "
                        f"{episode} references unknown Event {event_id!r}",
                    )
                )
                continue
            if event_id in quarantined_event_ids:
                errors.append(
                    _error(
                        "assignment_quarantined_event_reference",
                        f"excluded episode {episode} references quarantined "
                        f"Event {event_id}; formal exclusions may not leak "
                        "quarantine evidence",
                    )
                )
            if event.get("episode") != episode:
                errors.append(
                    _error(
                        "assignment_event_episode_mismatch",
                        f"excluded episode {episode} references Event {event_id} "
                        f"from episode {event.get('episode')!r}",
                    )
                )

    beat_id_counts = {
        beat_id: sum(
            1
            for item in beats
            if isinstance(item, dict) and item.get("id") == beat_id
        )
        for beat_id in beat_by_id
    }
    duplicate_beat_ids = sorted(
        beat_id for beat_id, count in beat_id_counts.items() if count > 1
    )
    if duplicate_beat_ids and not repair_invalid_dependencies:
        errors.extend(
            _error(
                "assignment_duplicate_beat_id",
                f"duplicate Thread Beat id {beat_id}",
            )
            for beat_id in duplicate_beat_ids
        )

    dependency_repair_candidates: list[dict[str, Any]] = []
    dependency_repair_errors: list[str] = []
    canonical_dependencies_by_index: dict[int, list[str]] = {}
    for beat_index, beat in enumerate(beats):
        if not isinstance(beat, dict):
            continue
        beat_id = beat.get("id")
        if not isinstance(beat_id, str) or not beat_id:
            continue
        dependencies = beat.get("requires_beat_ids", [])
        if not isinstance(dependencies, list):
            errors.append(
                _error(
                    "assignment_invalid_dependency",
                    f"{beat_id}.requires_beat_ids must be an array",
                )
            )
            continue
        canonical_dependencies: list[str] = []
        seen_dependencies: set[str] = set()
        for dependency_id in dependencies:
            if dependency_id in seen_dependencies:
                finding = _error(
                    "assignment_duplicate_dependency",
                    f"{beat_id}.requires_beat_ids contains duplicate ID "
                    f"{dependency_id!r}",
                )
                if repair_invalid_dependencies:
                    dependency_repair_candidates.append(
                        {
                            "code": REPAIR_DEDUPLICATE_DEPENDENCY,
                            "chapter_id": effective.get("chapter_id"),
                            "beat_id": beat_id,
                            "dependency_id": dependency_id,
                        }
                    )
                    dependency_repair_errors.append(finding)
                else:
                    errors.append(finding)
                continue
            if isinstance(dependency_id, str):
                seen_dependencies.add(dependency_id)

            if beat_id_counts.get(dependency_id, 0) > 1:
                canonical_dependencies.append(dependency_id)
                continue

            if dependency_id == beat_id:
                finding = _error(
                    "assignment_invalid_dependency",
                    f"{beat_id} cannot require itself",
                )
                if repair_invalid_dependencies:
                    dependency_repair_candidates.append(
                        {
                            "code": REPAIR_DROP_SELF_DEPENDENCY,
                            "chapter_id": effective.get("chapter_id"),
                            "beat_id": beat_id,
                            "dependency_id": dependency_id,
                            "thread_id": beat.get("thread_id"),
                        }
                    )
                    dependency_repair_errors.append(finding)
                else:
                    errors.append(finding)
                continue
            dependency = beat_by_id.get(dependency_id)
            if dependency is None:
                canonical_dependencies.append(dependency_id)
                continue
            dependency_thread_id = dependency.get("thread_id")
            beat_thread_id = beat.get("thread_id")
            if dependency_thread_id != beat_thread_id:
                finding = _error(
                    "assignment_invalid_dependency",
                    f"{beat_id} requires cross-thread Beat {dependency_id}",
                )
                can_repair = bool(
                    repair_invalid_dependencies
                    and beat_thread_id in registry_thread_ids
                    and dependency_thread_id in registry_thread_ids
                )
                if can_repair:
                    dependency_repair_candidates.append(
                        {
                            "code": (
                                REPAIR_DROP_LOCAL_CROSS_THREAD_DEPENDENCY
                            ),
                            "chapter_id": effective.get("chapter_id"),
                            "beat_id": beat_id,
                            "dependency_id": dependency_id,
                            "beat_thread_id": beat_thread_id,
                            "dependency_thread_id": dependency_thread_id,
                        }
                    )
                    dependency_repair_errors.append(finding)
                    continue
                errors.append(finding)
            dependency_episode = _known_episode(dependency.get("episode"))
            beat_episode = _known_episode(beat.get("episode"))
            if (
                dependency_episode is not None
                and beat_episode is not None
                and dependency_episode > beat_episode
            ):
                finding = _error(
                    "assignment_invalid_dependency",
                    f"{beat_id} requires later Beat {dependency_id}",
                )
                if repair_invalid_dependencies:
                    dependency_repair_candidates.append(
                        {
                            "code": REPAIR_DROP_LOCAL_LATER_DEPENDENCY,
                            "chapter_id": effective.get("chapter_id"),
                            "beat_id": beat_id,
                            "dependency_id": dependency_id,
                            "thread_id": beat.get("thread_id"),
                            "beat_episode": beat_episode,
                            "dependency_episode": dependency_episode,
                        }
                    )
                    dependency_repair_errors.append(finding)
                    continue
                errors.append(finding)
            canonical_dependencies.append(dependency_id)
        canonical_dependencies_by_index[beat_index] = canonical_dependencies

    if dependency_repair_candidates:
        if errors:
            errors.extend(dependency_repair_errors)
        else:
            for beat_index, dependency_ids in canonical_dependencies_by_index.items():
                beats[beat_index]["requires_beat_ids"] = dependency_ids
            repairs.extend(dependency_repair_candidates)

    if not errors:
        conflicts = sorted(set(beat_ids_by_episode) & set(exclusion_by_episode))
        removable_episodes: set[int] = set()
        for episode in conflicts:
            exclusion = exclusion_by_episode[episode]
            if (
                repair_redundant_exclusions
                and exclusion.get("reason_type") == "insufficient_evidence"
            ):
                removable_episodes.add(episode)
                repairs.append(
                    {
                        "code": REPAIR_DROP_REDUNDANT_INSUFFICIENT_EXCLUSION,
                        "chapter_id": effective.get("chapter_id"),
                        "episode": episode,
                        "reason_type": exclusion.get("reason_type"),
                        "preserved_thread_beat_ids": sorted(
                            beat_ids_by_episode[episode]
                        ),
                        "removed_exclusion_event_ids": list(
                            exclusion.get("event_ids", [])
                        ),
                    }
                )
            else:
                errors.append(
                    _error(
                        "assignment_episode_both_assigned_and_excluded",
                        f"episode {episode} has evidence-valid Thread Beats "
                        "and a whole-episode exclusion with reason_type="
                        f"{exclusion.get('reason_type')!r}; "
                        "excluded_episodes is not an unused-Event ledger",
                    )
                )
        if removable_episodes:
            effective["excluded_episodes"] = [
                item
                for item in exclusions
                if _known_episode(item.get("episode"))
                not in removable_episodes
            ]

    if not errors and repair_quarantined_episodes and quarantine_is_verified:
        covered = {
            int(item["episode"])
            for item in effective.get("thread_beats", [])
            if isinstance(item, dict)
            and _known_episode(item.get("episode")) is not None
        }
        excluded = {
            int(item["episode"])
            for item in effective.get("excluded_episodes", [])
            if isinstance(item, dict)
            and _known_episode(item.get("episode")) is not None
        }
        added_exclusions: list[dict[str, Any]] = []
        for episode in sorted(expected_episodes - covered - excluded):
            episode_event_ids = sorted(
                event_id
                for event_id, event in event_by_id.items()
                if event.get("episode") == episode
            )
            if not episode_event_ids or not set(episode_event_ids) <= quarantined_event_ids:
                continue
            added_exclusions.append(
                {
                    "episode": episode,
                    "reason_type": REGISTRY_QUARANTINED_DEPENDENCY,
                    "explanation": _quarantine_explanation(registry_language),
                    "event_ids": [],
                }
            )
            repairs.append(
                {
                    "code": (
                        REPAIR_ADD_REGISTRY_QUARANTINED_DEPENDENCY_EXCLUSION
                    ),
                    "chapter_id": effective.get("chapter_id"),
                    "episode": episode,
                    "reason_type": REGISTRY_QUARANTINED_DEPENDENCY,
                    "quarantine_sha256": registry_quarantine_sha256,
                    "quarantined_event_ids": episode_event_ids,
                }
            )
        if added_exclusions:
            effective["excluded_episodes"] = [
                *effective.get("excluded_episodes", []),
                *added_exclusions,
            ]

    if not errors:
        covered = {
            int(item["episode"])
            for item in effective.get("thread_beats", [])
            if isinstance(item, dict)
            and _known_episode(item.get("episode")) is not None
        }
        excluded = {
            int(item["episode"])
            for item in effective.get("excluded_episodes", [])
            if isinstance(item, dict)
            and _known_episode(item.get("episode")) is not None
        }
        unaccounted = sorted(expected_episodes - covered - excluded)
        if unaccounted:
            errors.append(
                _error(
                    "assignment_episode_unaccounted",
                    "episodes must have at least one Thread Beat or one typed "
                    f"whole-episode exclusion; missing={unaccounted}. "
                    "Do not synthesize insufficient_evidence locally.",
                )
            )

    effective_sha256 = json_sha256(effective)
    return AssignmentContractResult(
        effective_assignment=effective,
        repairs=repairs,
        errors=errors,
        raw_sha256=raw_sha256,
        effective_sha256=effective_sha256,
    )


def _entry_order_key(entry: dict[str, Any]) -> tuple[Any, ...]:
    beat = entry["beat"]
    return (
        int(beat.get("episode") or 0),
        THREAD_BEAT_PHASE_ORDER.get(str(beat.get("phase")), 10**6),
        str(entry.get("chapter_id") or ""),
        str(entry.get("canonical_id") or beat.get("id") or ""),
        int(entry.get("beat_index") or 0),
    )


def _path_exists(
    adjacency: dict[str, set[str]], start: str, target: str
) -> bool:
    pending = [start]
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current in visited:
            continue
        visited.add(current)
        pending.extend(adjacency.get(current, set()) - visited)
    return False


def _unique_collision_id(
    *,
    original_id: str,
    chapter_id: str,
    reserved_ids: set[str],
) -> str:
    base = f"{original_id}--{chapter_id}"
    candidate = base
    suffix = 2
    while candidate in reserved_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    reserved_ids.add(candidate)
    return candidate


def canonicalize_global_assignment_graph(
    assignments: list[dict[str, Any]],
    *,
    repair_invalid_dependencies: bool = True,
) -> GlobalAssignmentGraphResult:
    """Canonicalize Beat identity and dependency edges with complete scope."""

    raw_sha256 = json_sha256(assignments)
    effective = copy.deepcopy(assignments)
    repairs: list[dict[str, Any]] = []
    errors: list[str] = []
    entries: list[dict[str, Any]] = []
    groups_by_original_id: dict[str, list[dict[str, Any]]] = {}

    for assignment_index, assignment in enumerate(effective):
        chapter_id = str(assignment.get("chapter_id") or "")
        for beat_index, beat in enumerate(assignment.get("thread_beats", []) or []):
            if not isinstance(beat, dict):
                continue
            beat_id = beat.get("id")
            if not isinstance(beat_id, str) or not beat_id:
                continue
            entry = {
                "assignment_index": assignment_index,
                "beat_index": beat_index,
                "chapter_id": chapter_id,
                "original_id": beat_id,
                "canonical_id": beat_id,
                "beat": beat,
            }
            entries.append(entry)
            groups_by_original_id.setdefault(beat_id, []).append(entry)

    reserved_ids = set(groups_by_original_id)
    for beat_id, occurrences in sorted(groups_by_original_id.items()):
        if len(occurrences) <= 1:
            continue
        ordered = sorted(
            occurrences,
            key=lambda item: (
                str(item["chapter_id"]),
                int(item["assignment_index"]),
                int(item["beat_index"]),
            ),
        )
        finding = _error(
            "assignment_global_duplicate_beat_id",
            f"Thread Beat id {beat_id!r} appears {len(ordered)} times across "
            "accepted assignments",
        )
        if not repair_invalid_dependencies:
            errors.append(finding)
            continue
        for occurrence in ordered[1:]:
            canonical_id = _unique_collision_id(
                original_id=beat_id,
                chapter_id=str(occurrence["chapter_id"]),
                reserved_ids=reserved_ids,
            )
            occurrence["canonical_id"] = canonical_id
            occurrence["beat"]["id"] = canonical_id
            repairs.append(
                {
                    "code": REPAIR_RENAME_DUPLICATE_BEAT_ID,
                    "scope": "global_dependency_graph",
                    "chapter_id": occurrence["chapter_id"],
                    "beat_index": occurrence["beat_index"],
                    "raw_beat_id": beat_id,
                    "canonical_beat_id": canonical_id,
                }
            )

    canonical_entry_by_id = {
        str(entry["canonical_id"]): entry for entry in entries
    }
    dependencies_by_entry: dict[tuple[int, int], list[str]] = {}

    for entry in entries:
        beat = entry["beat"]
        source_id = str(entry["canonical_id"])
        source_key = (int(entry["assignment_index"]), int(entry["beat_index"]))
        canonical_dependencies: list[str] = []
        seen_dependencies: set[str] = set()
        dependencies = beat.get("requires_beat_ids", [])
        if not isinstance(dependencies, list):
            continue
        for dependency_id in dependencies:
            candidates = (
                groups_by_original_id.get(dependency_id, [])
                if isinstance(dependency_id, str)
                else []
            )
            target: dict[str, Any] | None = None
            if len(candidates) == 1:
                target = candidates[0]
            elif len(candidates) > 1:
                local_candidates = [
                    item
                    for item in candidates
                    if item["chapter_id"] == entry["chapter_id"]
                ]
                if len(local_candidates) == 1:
                    target = local_candidates[0]
                else:
                    finding = _error(
                        "assignment_ambiguous_dependency",
                        f"{source_id} references colliding Beat ID "
                        f"{dependency_id!r} without a unique chapter-local target",
                    )
                    if repair_invalid_dependencies:
                        repairs.append(
                            {
                                "code": REPAIR_DROP_AMBIGUOUS_DEPENDENCY,
                                "scope": "global_dependency_graph",
                                "chapter_id": entry["chapter_id"],
                                "beat_id": source_id,
                                "dependency_id": dependency_id,
                                "candidate_chapter_ids": sorted(
                                    str(item["chapter_id"])
                                    for item in candidates
                                ),
                            }
                        )
                    else:
                        errors.append(finding)
                    continue
            else:
                finding = _error(
                    "assignment_unknown_dependency",
                    f"{source_id} references unknown Beat {dependency_id!r}",
                )
                if repair_invalid_dependencies:
                    repairs.append(
                        {
                            "code": REPAIR_DROP_UNKNOWN_DEPENDENCY,
                            "scope": "global_dependency_graph",
                            "chapter_id": entry["chapter_id"],
                            "beat_id": source_id,
                            "dependency_id": dependency_id,
                            "thread_id": beat.get("thread_id"),
                        }
                    )
                else:
                    errors.append(finding)
                continue

            canonical_dependency_id = str(target["canonical_id"])
            if canonical_dependency_id in seen_dependencies:
                finding = _error(
                    "assignment_duplicate_dependency",
                    f"{source_id}.requires_beat_ids contains duplicate resolved "
                    f"ID {canonical_dependency_id!r}",
                )
                if repair_invalid_dependencies:
                    repairs.append(
                        {
                            "code": REPAIR_DEDUPLICATE_DEPENDENCY,
                            "scope": "global_dependency_graph",
                            "chapter_id": entry["chapter_id"],
                            "beat_id": source_id,
                            "dependency_id": canonical_dependency_id,
                        }
                    )
                else:
                    errors.append(finding)
                continue
            seen_dependencies.add(canonical_dependency_id)

            if canonical_dependency_id != dependency_id:
                if repair_invalid_dependencies:
                    repairs.append(
                        {
                            "code": REPAIR_REWRITE_COLLIDING_DEPENDENCY_ID,
                            "scope": "global_dependency_graph",
                            "chapter_id": entry["chapter_id"],
                            "beat_id": source_id,
                            "raw_dependency_id": dependency_id,
                            "canonical_dependency_id": canonical_dependency_id,
                            "dependency_chapter_id": target["chapter_id"],
                        }
                    )

            target_beat = target["beat"]
            if canonical_dependency_id == source_id:
                finding = _error(
                    "assignment_invalid_dependency",
                    f"{source_id} cannot require itself",
                )
                if repair_invalid_dependencies:
                    repairs.append(
                        {
                            "code": REPAIR_DROP_SELF_DEPENDENCY,
                            "scope": "global_dependency_graph",
                            "chapter_id": entry["chapter_id"],
                            "beat_id": source_id,
                            "dependency_id": canonical_dependency_id,
                            "thread_id": beat.get("thread_id"),
                        }
                    )
                else:
                    errors.append(finding)
                continue
            if target_beat.get("thread_id") != beat.get("thread_id"):
                finding = _error(
                    "assignment_invalid_dependency",
                    f"{source_id} requires cross-thread Beat "
                    f"{canonical_dependency_id}",
                )
                if repair_invalid_dependencies:
                    repairs.append(
                        {
                            "code": REPAIR_DROP_GLOBAL_CROSS_THREAD_DEPENDENCY,
                            "scope": "global_dependency_graph",
                            "chapter_id": entry["chapter_id"],
                            "beat_id": source_id,
                            "dependency_id": canonical_dependency_id,
                            "beat_thread_id": beat.get("thread_id"),
                            "dependency_thread_id": target_beat.get("thread_id"),
                        }
                    )
                else:
                    errors.append(finding)
                continue
            dependency_episode = _known_episode(target_beat.get("episode"))
            beat_episode = _known_episode(beat.get("episode"))
            if (
                dependency_episode is not None
                and beat_episode is not None
                and dependency_episode > beat_episode
            ):
                finding = _error(
                    "assignment_invalid_dependency",
                    f"{source_id} requires later Beat {canonical_dependency_id}",
                )
                if repair_invalid_dependencies:
                    repairs.append(
                        {
                            "code": REPAIR_DROP_GLOBAL_LATER_DEPENDENCY,
                            "scope": "global_dependency_graph",
                            "chapter_id": entry["chapter_id"],
                            "beat_id": source_id,
                            "dependency_id": canonical_dependency_id,
                            "thread_id": beat.get("thread_id"),
                            "beat_episode": beat_episode,
                            "dependency_episode": dependency_episode,
                        }
                    )
                else:
                    errors.append(finding)
                continue
            canonical_dependencies.append(canonical_dependency_id)
        dependencies_by_entry[source_key] = canonical_dependencies

    edge_entries: list[tuple[dict[str, Any], str]] = []
    for entry in entries:
        entry_key = (int(entry["assignment_index"]), int(entry["beat_index"]))
        for dependency_id in dependencies_by_entry.get(entry_key, []):
            edge_entries.append((entry, dependency_id))
    edge_entries.sort(
        key=lambda item: (
            0
            if _entry_order_key(canonical_entry_by_id[item[1]])
            < _entry_order_key(item[0])
            else 1,
            _entry_order_key(item[0]),
            _entry_order_key(canonical_entry_by_id[item[1]]),
        )
    )
    kept_adjacency: dict[str, set[str]] = {}
    cyclic_edges: set[tuple[str, str]] = set()
    for entry, dependency_id in edge_entries:
        source_id = str(entry["canonical_id"])
        if _path_exists(kept_adjacency, dependency_id, source_id):
            cyclic_edges.add((source_id, dependency_id))
            finding = _error(
                "assignment_cyclic_dependency",
                f"adding {source_id} -> {dependency_id} would create a cycle",
            )
            if repair_invalid_dependencies:
                repairs.append(
                    {
                        "code": REPAIR_DROP_CYCLIC_DEPENDENCY,
                        "scope": "global_dependency_graph",
                        "chapter_id": entry["chapter_id"],
                        "beat_id": source_id,
                        "dependency_id": dependency_id,
                        "thread_id": entry["beat"].get("thread_id"),
                    }
                )
            else:
                errors.append(finding)
            continue
        kept_adjacency.setdefault(source_id, set()).add(dependency_id)

    if repair_invalid_dependencies:
        for entry in entries:
            entry_key = (
                int(entry["assignment_index"]),
                int(entry["beat_index"]),
            )
            source_id = str(entry["canonical_id"])
            entry["beat"]["requires_beat_ids"] = [
                dependency_id
                for dependency_id in dependencies_by_entry.get(entry_key, [])
                if (source_id, dependency_id) not in cyclic_edges
            ]
    elif errors:
        effective = copy.deepcopy(assignments)

    return GlobalAssignmentGraphResult(
        effective_assignments=effective,
        repairs=repairs,
        errors=errors,
        raw_sha256=raw_sha256,
        effective_sha256=json_sha256(effective),
    )