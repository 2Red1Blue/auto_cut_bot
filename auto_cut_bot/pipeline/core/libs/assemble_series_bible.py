#!/usr/bin/env python3
"""Deterministically assemble Series Registry and chapter assignments into Bible v2.

Migrated from _legacy_v4/scripts/assemble_series_bible.py (pure functions only).
"""

from __future__ import annotations

from typing import Any

from autocut_core.contracts.genre_router import route_bible
from autocut_core.schema.compat import validate_task_response


PHASE_ORDER = {
    "setup": 0,
    "escalation": 1,
    "turn": 2,
    "reveal": 3,
    "payoff": 4,
    "consequence": 5,
    "coda": 6,
}


def _ids(
    records: list[dict[str, Any]], field: str, where: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(records):
        value = item.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{where}[{index}].{field} must be non-empty")
        if value in result:
            raise ValueError(f"{where} contains duplicate {field}: {value}")
        result[value] = item
    return result


def _known_refs(values: Any, known: set[str], where: str) -> set[str]:
    if not isinstance(values, list):
        raise ValueError(f"{where} must be an array")
    selected = {item for item in values if isinstance(item, str) and item}
    unknown = sorted(selected - known)
    if unknown:
        raise ValueError(f"{where} contains unknown IDs: {unknown}")
    return selected


def _event_sort_key(event_id: str, events: dict[str, dict[str, Any]]) -> tuple[Any, ...]:
    event = events[event_id]
    ranges = event.get("source_ranges", [])
    start = (
        float(ranges[0].get("start", 0))
        if ranges and isinstance(ranges[0], dict)
        else 0.0
    )
    return int(event["episode"]), start, event_id


def assemble_bible(
    *,
    registry: dict[str, Any],
    assignments: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    manifest_windows: list[dict[str, Any]],
    episode_digests: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    registry_errors = validate_task_response("series_registry", registry)
    if registry_errors:
        raise ValueError("invalid Series Registry: " + "; ".join(registry_errors[:40]))
    event_by_id = _ids(events, "id", "events")
    known_event_ids = set(event_by_id)
    source_episode_ids = {
        int(item["episode"])
        for item in sources
        if isinstance(item, dict) and isinstance(item.get("episode"), int)
    }
    digest_episode_ids = {
        int(item["episode"])
        for item in episode_digests
        if isinstance(item, dict) and isinstance(item.get("episode"), int)
    }
    missing_digest_ids = sorted(source_episode_ids - digest_episode_ids)
    if missing_digest_ids:
        raise ValueError(
            f"cannot assemble Series Bible with missing Episode Digests: "
            f"{missing_digest_ids}"
        )

    character_by_id = _ids(registry.get("characters", []), "id", "characters")
    relationship_by_id = _ids(
        registry.get("relationships", []), "id", "relationships"
    )
    fact_by_id = _ids(registry.get("facts", []), "id", "facts")
    thread_by_id = _ids(registry.get("story_threads", []), "id", "story_threads")
    question_by_id = _ids(
        registry.get("open_questions", []), "id", "open_questions"
    )
    for character_id, character in character_by_id.items():
        _known_refs(
            character.get("evidence_event_ids"),
            known_event_ids,
            f"characters[{character_id}].evidence_event_ids",
        )
        _known_refs(
            [character.get("first_event_id")],
            known_event_ids,
            f"characters[{character_id}].first_event_id",
        )
    for relationship_id, relationship in relationship_by_id.items():
        _known_refs(
            relationship.get("character_ids"),
            set(character_by_id),
            f"relationships[{relationship_id}].character_ids",
        )
        _known_refs(
            [
                item.get("event_id")
                for item in relationship.get("state_changes", [])
                if isinstance(item, dict)
            ],
            known_event_ids,
            f"relationships[{relationship_id}].state_changes",
        )
    for fact_id, fact in fact_by_id.items():
        _known_refs(
            fact.get("event_ids"),
            known_event_ids,
            f"facts[{fact_id}].event_ids",
        )
    for question_id, question in question_by_id.items():
        _known_refs(
            question.get("event_ids"),
            known_event_ids,
            f"open_questions[{question_id}].event_ids",
        )
    for thread_id, thread in thread_by_id.items():
        _known_refs(
            thread.get("character_ids"),
            set(character_by_id),
            f"story_threads[{thread_id}].character_ids",
        )
        _known_refs(
            thread.get("anchor_event_ids"),
            known_event_ids,
            f"story_threads[{thread_id}].anchor_event_ids",
        )
        thread["open_question_ids"] = [
            question_id
            for question_id in thread.get("open_question_ids", [])
            if question_id in question_by_id
        ]

    all_beats: list[dict[str, Any]] = []
    all_exclusions: list[dict[str, Any]] = []
    chapter_ids: set[str] = set()
    declared_assignment_episodes: set[int] = set()
    for assignment_index, assignment in enumerate(assignments):
        schema_errors = validate_task_response("series_assignment", assignment)
        if schema_errors:
            raise ValueError(
                f"invalid Series Assignment[{assignment_index}]: "
                + "; ".join(schema_errors[:40])
            )
        chapter_id = assignment["chapter_id"]
        if chapter_id in chapter_ids:
            raise ValueError(f"duplicate Series Assignment chapter_id: {chapter_id}")
        chapter_ids.add(chapter_id)
        assignment_episodes = set(assignment["episodes"])
        duplicate_episode_declarations = sorted(
            declared_assignment_episodes & assignment_episodes
        )
        if duplicate_episode_declarations:
            raise ValueError(
                "episodes assigned by multiple chapters: "
                f"{duplicate_episode_declarations}"
            )
        declared_assignment_episodes.update(assignment_episodes)
        if not assignment_episodes <= source_episode_ids:
            raise ValueError(
                f"{chapter_id} contains unknown episodes: "
                f"{sorted(assignment_episodes - source_episode_ids)}"
            )
        for beat in assignment["thread_beats"]:
            if beat["episode"] not in assignment_episodes:
                raise ValueError(
                    f"{beat['id']} episode {beat['episode']} is outside {chapter_id}"
                )
            if beat["thread_id"] not in thread_by_id:
                raise ValueError(
                    f"{beat['id']} references unknown thread {beat['thread_id']}"
                )
            beat_event_ids = _known_refs(
                beat["event_ids"], known_event_ids, f"{beat['id']}.event_ids"
            )
            wrong_episode = sorted(
                event_id
                for event_id in beat_event_ids
                if event_by_id[event_id].get("episode") != beat["episode"]
            )
            if wrong_episode:
                raise ValueError(
                    f"{beat['id']} binds Events from another episode: {wrong_episode}"
                )
            all_beats.append(dict(beat))
        for exclusion in assignment["excluded_episodes"]:
            if exclusion["episode"] not in assignment_episodes:
                raise ValueError(
                    f"{chapter_id} exclusion episode {exclusion['episode']} "
                    "is outside the chapter"
                )
            exclusion_events = _known_refs(
                exclusion["event_ids"],
                known_event_ids,
                f"{chapter_id}.excluded_episodes[{exclusion['episode']}].event_ids",
            )
            wrong_episode = sorted(
                event_id
                for event_id in exclusion_events
                if event_by_id[event_id].get("episode") != exclusion["episode"]
            )
            if wrong_episode:
                raise ValueError(
                    f"excluded episode {exclusion['episode']} binds Events from "
                    f"another episode: {wrong_episode}"
                )
            all_exclusions.append(dict(exclusion))

    if declared_assignment_episodes != source_episode_ids:
        raise ValueError(
            "Series Assignments do not declare every source episode exactly once: "
            f"missing={sorted(source_episode_ids - declared_assignment_episodes)}, "
            f"extra={sorted(declared_assignment_episodes - source_episode_ids)}"
        )
    beat_by_id = _ids(all_beats, "id", "thread_beats")
    for beat in all_beats:
        beat["requires_beat_ids"] = [
            dependency_id
            for dependency_id in beat["requires_beat_ids"]
            if beat_by_id[dependency_id]["thread_id"] == beat["thread_id"]
        ]
    excluded_by_episode: dict[int, dict[str, Any]] = {}
    for exclusion in all_exclusions:
        episode = int(exclusion["episode"])
        if episode in excluded_by_episode:
            raise ValueError(f"episode {episode} is excluded more than once")
        excluded_by_episode[episode] = exclusion
    covered_episode_ids = {int(item["episode"]) for item in all_beats}
    both = sorted(covered_episode_ids & set(excluded_by_episode))
    if both:
        raise ValueError(f"episodes cannot be both assigned and excluded: {both}")
    accounted_episode_ids = covered_episode_ids | set(excluded_by_episode)
    unassigned_episode_ids = sorted(source_episode_ids - accounted_episode_ids)
    if unassigned_episode_ids:
        raise ValueError(
            f"narrative coverage has unassigned episodes: {unassigned_episode_ids}"
        )
    for beat_id, beat in beat_by_id.items():
        dependencies = _known_refs(
            beat["requires_beat_ids"],
            set(beat_by_id),
            f"thread_beats[{beat_id}].requires_beat_ids",
        )
        if beat_id in dependencies:
            raise ValueError(f"{beat_id} cannot require itself")
        for dependency_id in dependencies:
            dependency = beat_by_id[dependency_id]
            if dependency["thread_id"] != beat["thread_id"]:
                raise ValueError(
                    f"{beat_id} requires cross-thread Beat {dependency_id}"
                )
            if int(dependency["episode"]) > int(beat["episode"]):
                raise ValueError(
                    f"{beat_id} requires later Beat {dependency_id}"
                )

    sorted_beats = sorted(
        all_beats,
        key=lambda item: (
            int(item["episode"]),
            PHASE_ORDER[item["phase"]],
            item["thread_id"],
            item["id"],
        ),
    )
    final_threads = []
    for thread_id, registry_thread in thread_by_id.items():
        thread_beats = [
            item for item in sorted_beats if item["thread_id"] == thread_id
        ]
        if not thread_beats:
            raise ValueError(f"global Story Thread has no assigned Beat: {thread_id}")
        phases = {item["phase"] for item in thread_beats}
        if registry_thread["status"] == "resolved":
            if "setup" not in phases or "payoff" not in phases:
                raise ValueError(
                    f"resolved Story Thread {thread_id} must contain setup and payoff"
                )
        event_ids = sorted(
            {
                event_id
                for beat in thread_beats
                for event_id in beat["event_ids"]
            },
            key=lambda value: _event_sort_key(value, event_by_id),
        )
        phase_events = {
            phase: sorted(
                {
                    event_id
                    for beat in thread_beats
                    if beat["phase"] == phase
                    for event_id in beat["event_ids"]
                },
                key=lambda value: _event_sort_key(value, event_by_id),
            )
            for phase in PHASE_ORDER
        }
        final_threads.append(
            {
                "id": thread_id,
                "title": registry_thread["title"],
                "premise": registry_thread["premise"],
                "character_ids": registry_thread["character_ids"],
                "event_ids": event_ids,
                "setup_event_ids": phase_events["setup"],
                "escalation_event_ids": phase_events["escalation"],
                "reveal_event_ids": sorted(
                    set(phase_events["turn"]) | set(phase_events["reveal"]),
                    key=lambda value: _event_sort_key(value, event_by_id),
                ),
                "payoff_event_ids": phase_events["payoff"],
                "open_question_ids": registry_thread["open_question_ids"],
                "thread_beat_ids": [item["id"] for item in thread_beats],
                "episode_ids": sorted({int(item["episode"]) for item in thread_beats}),
                "status": registry_thread["status"],
            }
        )
    final_threads.sort(key=lambda item: item["id"])
    bible = {
        "schema_version": "1.2",
        "series_summary": registry["series_summary"],
        "characters": registry["characters"],
        "relationships": registry["relationships"],
        "facts": registry["facts"],
        "story_threads": final_threads,
        "thread_beats": sorted_beats,
        "open_questions": registry["open_questions"],
        "unresolved_identity_conflicts": registry[
            "unresolved_identity_conflicts"
        ],
        "coverage": {
            "ingestion_coverage": {
                "source_count": len(sources),
                "episode_count": len(source_episode_ids),
                "window_count": len(manifest_windows),
                "episode_digest_count": len(digest_episode_ids),
                "missing_episode_ids": missing_digest_ids,
            },
            "narrative_coverage": {
                "covered_episode_ids": sorted(covered_episode_ids),
                "unassigned_episode_ids": unassigned_episode_ids,
                "excluded_episodes": [
                    excluded_by_episode[item] for item in sorted(excluded_by_episode)
                ],
            },
        },
    }
    if any(
        key in registry
        for key in (
            "genre_profile",
            "genre_confidence",
            "genre_evidence_event_ids",
            "genre_review_status",
        )
    ):
        _known_refs(
            registry.get("genre_evidence_event_ids", []),
            known_event_ids,
            "series_registry.genre_evidence_event_ids",
        )
        bible["genre_profile"] = registry.get("genre_profile", "unknown")
        bible["genre_confidence"] = registry.get("genre_confidence", 0.0)
        bible["genre_evidence_event_ids"] = registry.get(
            "genre_evidence_event_ids", []
        )
        bible["genre_review_status"] = registry.get(
            "genre_review_status", "human_review_required"
        )
        route = route_bible(bible)
        bible["genre_profile"] = route["genre_profile"]
        bible["genre_confidence"] = route["confidence"]
        bible["genre_evidence_event_ids"] = route["genre_evidence_event_ids"]
        bible["genre_review_status"] = route["status"]
        bible["golden_case_ids"] = route.get("golden_case_ids", [])
    schema_errors = validate_task_response("series_bible", bible)
    if schema_errors:
        raise ValueError("assembled Series Bible is invalid: " + "; ".join(schema_errors[:40]))
    return bible