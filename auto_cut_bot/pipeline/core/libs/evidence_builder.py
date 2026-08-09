#!/usr/bin/env python3
"""Build deterministic, self-contained evidence packets for approved Story Scripts."""

from __future__ import annotations

import re
from typing import Any, Iterable

from autocut_core.io import stable_id
from autocut_core.schema.compat import validate_task_response
from autocut_core.contracts.cross_unit import (
    cross_unit_required,
    dependency_episode_range,
    dependency_event_ids,
    filter_events_by_episode_range,
    processing_unit_map,
)


def by_id(
    records: Iterable[dict[str, Any]], *, field: str = "id", where: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            raise ValueError(f"{where}[{index}] must be an object")
        item_id = item.get(field)
        if not isinstance(item_id, str) or not item_id:
            raise ValueError(f"{where}[{index}] is missing non-empty {field}")
        if item_id in result:
            raise ValueError(f"{where} contains duplicate {field}: {item_id}")
        result[item_id] = item
    return result


def checked_ids(values: Any, known: dict[str, Any], where: str) -> set[str]:
    if not isinstance(values, list):
        raise ValueError(f"{where} must be an array")
    result = {item for item in values if isinstance(item, str) and item}
    unknown = sorted(result - set(known))
    if unknown:
        raise ValueError(f"{where} contains unknown IDs: {unknown}")
    return result


def source_episode(event: dict[str, Any]) -> int | None:
    episode = event.get("episode")
    return episode if isinstance(episode, int) and not isinstance(episode, bool) else None


def filter_expanded_events(
    event_ids: set[str],
    *,
    events: dict[str, dict[str, Any]],
    anchor_episodes: set[int],
    lookback: str,
) -> set[str]:
    if lookback == "whole_series":
        return set(event_ids)
    if not anchor_episodes:
        return set()
    if lookback == "same_episode":
        return {
            event_id
            for event_id in event_ids
            if source_episode(events[event_id]) in anchor_episodes
        }
    latest_anchor = max(anchor_episodes)
    return {
        event_id
        for event_id in event_ids
        if source_episode(events[event_id]) is not None
        and int(events[event_id]["episode"]) <= latest_anchor
    }


def inferred_window_neighbors(
    manifest_windows: list[dict[str, Any]],
) -> dict[str, tuple[str | None, str | None]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in manifest_windows:
        source_id = item.get("source_id")
        if not isinstance(source_id, str):
            raise ValueError("window manifest contains a window without source_id")
        grouped.setdefault(source_id, []).append(item)
    result: dict[str, tuple[str | None, str | None]] = {}
    for source_id in sorted(grouped):
        ordered = sorted(
            grouped[source_id],
            key=lambda item: (
                float(item.get("start", 0)),
                float(item.get("end", 0)),
                str(item.get("id", "")),
            ),
        )
        for index, item in enumerate(ordered):
            window_id = item.get("id")
            if not isinstance(window_id, str) or not window_id:
                raise ValueError("window manifest contains a window without id")
            previous_id = item.get("previous_window_id")
            next_id = item.get("next_window_id")
            if not isinstance(previous_id, str):
                previous_id = ordered[index - 1]["id"] if index > 0 else None
            if not isinstance(next_id, str):
                next_id = ordered[index + 1]["id"] if index + 1 < len(ordered) else None
            result[window_id] = (previous_id, next_id)
    return result


def expand_adjacent_windows(
    seed_window_ids: set[str],
    *,
    neighbors: dict[str, tuple[str | None, str | None]],
    hops: int,
) -> set[str]:
    selected = set(seed_window_ids)
    frontier = set(seed_window_ids)
    for _ in range(hops):
        next_frontier: set[str] = set()
        for window_id in frontier:
            if window_id not in neighbors:
                raise ValueError(f"unknown evidence window ID: {window_id}")
            for neighbor in neighbors[window_id]:
                if isinstance(neighbor, str) and neighbor not in selected:
                    next_frontier.add(neighbor)
        selected.update(next_frontier)
        frontier = next_frontier
        if not frontier:
            break
    return selected


def event_range_refs(
    event: dict[str, Any],
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in event.get("source_ranges", []):
        start, end = item.get("start"), item.get("end")
        if (
            not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or float(end) <= float(start)
        ):
            continue
        refs.append(
            {
                "source_id": event["source_id"],
                "episode": event["episode"],
                "start": float(start),
                "end": float(end),
                "origin": "event",
                "origin_id": event["id"],
                "evidence_window_ids": sorted(
                    {
                        value
                        for value in item.get("evidence_window_ids", [])
                        if isinstance(value, str) and value
                    }
                ),
            }
        )
    return refs


def candidate_range_ref(candidate: dict[str, Any]) -> dict[str, Any] | None:
    start, end = candidate.get("start"), candidate.get("end")
    if (
        not isinstance(start, (int, float))
        or isinstance(start, bool)
        or not isinstance(end, (int, float))
        or isinstance(end, bool)
        or float(end) <= float(start)
    ):
        return None
    return {
        "source_id": candidate["source_id"],
        "episode": candidate["episode"],
        "start": float(start),
        "end": float(end),
        "origin": "candidate",
        "origin_id": candidate["id"],
        "evidence_window_ids": sorted(
            {
                value
                for value in candidate.get("evidence_window_ids", [])
                if isinstance(value, str) and value
            }
        ),
    }


def unique_range_refs(refs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in refs:
        key = (
            item["source_id"],
            float(item["start"]),
            float(item["end"]),
            item["origin"],
            item["origin_id"],
        )
        result[key] = item
    return sorted(
        result.values(),
        key=lambda item: (
            int(item["episode"]),
            item["source_id"],
            float(item["start"]),
            float(item["end"]),
            item["origin"],
            item["origin_id"],
        ),
    )


def unique_duration_seconds(refs: Iterable[dict[str, Any]]) -> float:
    grouped: dict[str, list[tuple[float, float]]] = {}
    for item in refs:
        start, end = float(item["start"]), float(item["end"])
        if end > start:
            grouped.setdefault(item["source_id"], []).append((start, end))
    total = 0.0
    for source_id in sorted(grouped):
        merged: list[list[float]] = []
        for start, end in sorted(grouped[source_id]):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        total += sum(end - start for start, end in merged)
    return round(total, 3)


def source_snapshot(source: dict[str, Any]) -> dict[str, Any]:
    if isinstance(source.get("path"), str):
        locator_type = "local_path"
        locator = source["path"]
    elif isinstance(source.get("url"), str):
        locator_type = "remote_url"
        locator = source["url"]
    else:
        locator_type = "unavailable"
        locator = ""
    return {
        "id": source["id"],
        "episode": source["episode"],
        "duration_seconds": float(source["duration_seconds"]),
        "locator_type": locator_type,
        "locator": locator,
    }


def packet_filename(story_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", story_id):
        return f"{story_id}.json"
    return f"{stable_id('story-evidence', story_id)}.json"


def build_packet(
    *,
    approval_item: dict[str, Any],
    approval_sha256: str,
    script: dict[str, Any],
    events: dict[str, dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
    characters: dict[str, dict[str, Any]],
    relationships: dict[str, dict[str, Any]],
    facts: dict[str, dict[str, Any]],
    threads: dict[str, dict[str, Any]],
    thread_beats: dict[str, dict[str, Any]],
    questions: dict[str, dict[str, Any]],
    sources: dict[str, dict[str, Any]],
    manifest_windows: dict[str, dict[str, Any]],
    window_summaries: dict[str, dict[str, Any]],
    neighbors: dict[str, tuple[str | None, str | None]],
    fingerprints: dict[str, str],
    adjacent_window_hops: int,
    units_by_episode: dict[int, set[str]],
) -> dict[str, Any]:
    selected_character_ids = checked_ids(
        script["character_ids"], characters, "script.character_ids"
    )
    selected_relationship_ids = checked_ids(
        script["relationship_ids"], relationships, "script.relationship_ids"
    )
    selected_thread_ids = checked_ids(
        script["story_thread_ids"], threads, "script.story_thread_ids"
    )
    selected_thread_beat_ids = checked_ids(
        script["selected_thread_beat_ids"],
        thread_beats,
        "script.selected_thread_beat_ids",
    )
    required_thread_beat_ids = checked_ids(
        script["required_thread_beat_ids"],
        thread_beats,
        "script.required_thread_beat_ids",
    )
    selected_fact_ids = checked_ids(
        script["required_fact_ids"], facts, "script.required_fact_ids"
    )
    selected_fact_ids.update(
        checked_ids(
            script["intentional_mystery_fact_ids"],
            facts,
            "script.intentional_mystery_fact_ids",
        )
    )
    selected_question_ids: set[str] = set()
    selected_event_ids: set[str] = set()
    selected_candidate_ids: set[str] = set()
    selected_window_ids: set[str] = set()
    all_range_refs: list[dict[str, Any]] = []
    beat_evidence: list[dict[str, Any]] = []
    status_buckets: dict[str, list[str]] = {
        "covered": [],
        "partial": [],
        "missing": [],
        "needs_video_review": [],
    }
    must_have_missing: list[str] = []
    cross_unit_records: list[dict[str, Any]] = []
    cross_unit_missing = False
    cross_unit_partial = False

    for beat_index, beat in enumerate(script["beats"]):
        where = f"beats[{beat_index}]"
        retrieval = beat["retrieval_requirements"]
        dependency_event_id_set, dependency_unknown_ids = dependency_event_ids(
            script,
            beat,
            events=events,
            facts=facts,
            relationships=relationships,
            thread_beats=thread_beats,
        )
        dependency_events = {
            event_id: events[event_id]
            for event_id in dependency_event_id_set
            if event_id in events
        }
        dependency_range = dependency_episode_range(beat, dependency_events)
        requested_character_ids = checked_ids(
            retrieval["character_ids"],
            characters,
            f"{where}.retrieval_requirements.character_ids",
        )
        requested_relationship_ids = checked_ids(
            retrieval["relationship_ids"],
            relationships,
            f"{where}.retrieval_requirements.relationship_ids",
        )
        requested_thread_ids = checked_ids(
            retrieval["story_thread_ids"],
            threads,
            f"{where}.retrieval_requirements.story_thread_ids",
        )
        requested_thread_beat_ids = checked_ids(
            retrieval["thread_beat_ids"],
            thread_beats,
            f"{where}.retrieval_requirements.thread_beat_ids",
        )
        requested_fact_ids = checked_ids(
            retrieval["fact_ids"],
            facts,
            f"{where}.retrieval_requirements.fact_ids",
        )
        requested_event_ids = checked_ids(
            retrieval["event_ids"],
            events,
            f"{where}.retrieval_requirements.event_ids",
        )
        requested_candidate_ids = checked_ids(
            retrieval["candidate_ids"],
            candidates,
            f"{where}.retrieval_requirements.candidate_ids",
        )
        direct_event_ids = checked_ids(
            beat["event_ids"], events, f"{where}.event_ids"
        )
        direct_event_ids.update(requested_event_ids)
        direct_candidate_ids = checked_ids(
            beat["candidate_suggestions"],
            candidates,
            f"{where}.candidate_suggestions",
        )
        direct_candidate_ids.update(requested_candidate_ids)
        missing_requirements: list[str] = []
        must_show_evidence: list[dict[str, Any]] = []
        must_show_fact_event_ids: set[str] = set()
        missing_must_show_ids: list[str] = []
        for show_index, must_show in enumerate(beat["must_show"]):
            show_where = f"{where}.must_show[{show_index}]"
            requested_show_events = checked_ids(
                must_show["evidence_event_ids"],
                events,
                f"{show_where}.evidence_event_ids",
            )
            requested_show_facts = checked_ids(
                must_show["evidence_fact_ids"],
                facts,
                f"{show_where}.evidence_fact_ids",
            )
            fact_context_events: set[str] = set()
            for fact_id in requested_show_facts:
                fact_context_events.update(
                    checked_ids(
                        facts[fact_id]["event_ids"],
                        events,
                        f"facts[{fact_id}].event_ids",
                    )
                )
            must_show_fact_event_ids.update(fact_context_events)
            has_range = any(
                event_range_refs(events[event_id])
                for event_id in requested_show_events
            )
            # A Fact is semantic context, not an observable shot.  A
            # must-show is covered only by its explicitly cited Event
            # evidence; Fact-linked Events remain available for context
            # recall but cannot independently prove visual/dialogue/action
            # coverage.
            show_status = (
                "covered"
                if requested_show_events and has_range
                else "missing"
            )
            if show_status == "missing":
                missing_must_show_ids.append(must_show["id"])
                missing_requirements.append(
                    f"must_show {must_show['id']} 缺少显式、可定位的 Event 原片范围；"
                    "Fact 关联 Event 只能作为 Context"
                )
            must_show_evidence.append(
                {
                    "must_show_id": must_show["id"],
                    "description": must_show["description"],
                    "observable_via": must_show["observable_via"],
                    "requested_event_ids": sorted(requested_show_events),
                    "requested_fact_ids": sorted(requested_show_facts),
                    "direct_event_ids": sorted(requested_show_events),
                    "fact_context_event_ids": sorted(fact_context_events),
                    "resolved_event_ids": sorted(
                        requested_show_events | fact_context_events
                    ),
                    "status": show_status,
                }
            )
            direct_event_ids.update(requested_show_events)
            selected_fact_ids.update(requested_show_facts)
        for candidate_id in direct_candidate_ids:
            direct_event_ids.update(
                checked_ids(
                    candidates[candidate_id].get("event_ids", []),
                    events,
                    f"candidates[{candidate_id}].event_ids",
                )
            )
        anchor_episodes = {
            int(events[event_id]["episode"])
            for event_id in direct_event_ids
            if isinstance(events[event_id].get("episode"), int)
        }
        anchor_episodes.update(
            int(candidates[candidate_id]["episode"])
            for candidate_id in direct_candidate_ids
            if isinstance(candidates[candidate_id].get("episode"), int)
        )
        expanded_pool: set[str] = set(must_show_fact_event_ids)
        expanded_pool.update(dependency_event_id_set)
        for fact_id in requested_fact_ids:
            expanded_pool.update(
                checked_ids(
                    facts[fact_id]["event_ids"],
                    events,
                    f"facts[{fact_id}].event_ids",
                )
            )
        for character_id in requested_character_ids:
            expanded_pool.update(
                checked_ids(
                    characters[character_id]["evidence_event_ids"],
                    events,
                    f"characters[{character_id}].evidence_event_ids",
                )
            )
        for relationship_id in requested_relationship_ids:
            expanded_pool.update(
                checked_ids(
                    [
                        item.get("event_id")
                        for item in relationships[relationship_id]["state_changes"]
                        if isinstance(item.get("event_id"), str)
                    ],
                    events,
                    f"relationships[{relationship_id}].state_changes",
                )
            )
        for thread_id in requested_thread_ids:
            expanded_pool.update(
                checked_ids(
                    threads[thread_id]["event_ids"],
                    events,
                    f"story_threads[{thread_id}].event_ids",
                )
            )
        for thread_beat_id in requested_thread_beat_ids:
            expanded_pool.update(
                checked_ids(
                    thread_beats[thread_beat_id]["event_ids"],
                    events,
                    f"thread_beats[{thread_beat_id}].event_ids",
                )
            )
        expanded_event_ids = set(direct_event_ids)
        expanded_event_ids.update(
            filter_expanded_events(
                expanded_pool,
                events=events,
                anchor_episodes=anchor_episodes,
                lookback=retrieval["lookback"],
            )
        )
        if dependency_event_id_set:
            # A declared causal ancestor range is authoritative for context
            # recall.  It is evaluated against the series Event index, never
            # against the current Chapter Digest or processing partition.
            if dependency_range is not None:
                expanded_event_ids.update(
                    filter_events_by_episode_range(
                        dependency_event_id_set,
                        events=events,
                        episode_range=dependency_range,
                    )
                )
            else:
                expanded_event_ids.update(
                    filter_expanded_events(
                        dependency_event_id_set,
                        events=events,
                        anchor_episodes=anchor_episodes,
                        lookback=retrieval["lookback"],
                    )
                )
        # Fact-linked events are context recall only.  Keep their scope on
        # the same lookback contract used by the evidence validator, rather
        # than inheriting the broader causal ancestor range used for general
        # context expansion.  This prevents a distant dependency event from
        # being promoted into a must-show context field.
        filtered_fact_context_event_ids = filter_expanded_events(
            must_show_fact_event_ids,
            events=events,
            anchor_episodes=anchor_episodes,
            lookback=retrieval["lookback"],
        )
        for item in must_show_evidence:
            item["fact_context_event_ids"] = sorted(
                set(item["fact_context_event_ids"])
                & filtered_fact_context_event_ids
            )
            item["resolved_event_ids"] = sorted(
                set(item["direct_event_ids"])
                | set(item["fact_context_event_ids"])
            )
        # Candidate provenance is intentionally one-hop and tiered:
        # explicitly requested/suggested Candidates are functional anchors;
        # Candidates merely discovered through an expanded/context Event are
        # context recall only.  Never promote the latter into
        # candidate_range_refs, otherwise the Span compiler can mistake an
        # entity-expansion hit for direct Beat evidence.
        inferred_context_candidate_ids: set[str] = set()
        for event_id in expanded_event_ids:
            inferred_context_candidate_ids.update(
                checked_ids(
                    events[event_id].get("candidate_ids", []),
                    candidates,
                    f"events[{event_id}].candidate_ids",
                )
            )
        inferred_context_candidate_ids.difference_update(direct_candidate_ids)
        related_candidate_ids = (
            set(direct_candidate_ids) | inferred_context_candidate_ids
        )
        for candidate_id in inferred_context_candidate_ids:
            expanded_event_ids.update(
                checked_ids(
                    candidates[candidate_id].get("event_ids", []),
                    events,
                    f"candidates[{candidate_id}].event_ids",
                )
            )
        direct_range_refs: list[dict[str, Any]] = []
        for event_id in direct_event_ids:
            direct_range_refs.extend(event_range_refs(events[event_id]))
        direct_range_refs = unique_range_refs(direct_range_refs)
        candidate_range_refs: list[dict[str, Any]] = []
        for candidate_id in direct_candidate_ids:
            ref = candidate_range_ref(candidates[candidate_id])
            if ref is not None:
                candidate_range_refs.append(ref)
        candidate_range_refs = unique_range_refs(candidate_range_refs)
        context_range_refs: list[dict[str, Any]] = []
        for event_id in expanded_event_ids - direct_event_ids:
            context_range_refs.extend(event_range_refs(events[event_id]))
        for candidate_id in inferred_context_candidate_ids:
            ref = candidate_range_ref(candidates[candidate_id])
            if ref is not None:
                context_range_refs.append(ref)
        context_range_refs = unique_range_refs(context_range_refs)
        # Backward-compatible union. Downstream compilers must consume the
        # tiered fields so entity expansion cannot silently become a tight
        # edit anchor.
        range_refs = unique_range_refs(
            [
                *direct_range_refs,
                *candidate_range_refs,
                *context_range_refs,
            ]
        )
        evidence_window_ids = {
            window_id
            for item in range_refs
            for window_id in item["evidence_window_ids"]
        }
        unknown_evidence_windows = sorted(evidence_window_ids - set(manifest_windows))
        if unknown_evidence_windows:
            raise ValueError(
                f"{where} references unknown evidence windows: {unknown_evidence_windows}"
            )
        expanded_window_ids = expand_adjacent_windows(
            evidence_window_ids,
            neighbors=neighbors,
            hops=adjacent_window_hops,
        )
        unknown_summaries = sorted(expanded_window_ids - set(window_summaries))
        if unknown_summaries:
            raise ValueError(
                f"{where} is missing adjacent window summaries: {unknown_summaries}"
            )
        context_window_ids = expanded_window_ids - evidence_window_ids
        source_ids = {
            item["source_id"] for item in range_refs if item["source_id"] in sources
        }
        unknown_sources = sorted(
            {item["source_id"] for item in range_refs} - set(sources)
        )
        if unknown_sources:
            raise ValueError(f"{where} references unknown sources: {unknown_sources}")
        if not range_refs:
            retrieval_status = "missing"
            missing_requirements.append("没有召回任何可定位的 Event/Candidate 原片范围")
        elif len(missing_must_show_ids) == len(beat["must_show"]):
            retrieval_status = "missing"
        elif missing_must_show_ids:
            retrieval_status = "partial"
        elif beat["evidence_status"] in {
            "partial",
            "conflicting",
            "needs_video_review",
        }:
            retrieval_status = "needs_video_review"
        else:
            retrieval_status = "covered"
        if retrieval_status == "missing" and beat["must_have"]:
            must_have_missing.append(beat["id"])
        status_buckets[retrieval_status].append(beat["id"])
        material_risks = list(
            dict.fromkeys(
                [
                    *[
                        item
                        for item in beat.get("material_risks", [])
                        if isinstance(item, str) and item
                    ],
                    *missing_requirements,
                ]
            )
        )
        beat_evidence.append(
            {
                "beat_id": beat["id"],
                "role": beat["role"],
                "must_have": beat["must_have"],
                "temporal_position": beat["temporal_position"],
                "search_intent": retrieval["search_intent"],
                "continuity": retrieval["continuity"],
                "lookback": retrieval["lookback"],
                "requested_ids": {
                    "character_ids": sorted(requested_character_ids),
                    "relationship_ids": sorted(requested_relationship_ids),
                    "story_thread_ids": sorted(requested_thread_ids),
                    "thread_beat_ids": sorted(requested_thread_beat_ids),
                    "fact_ids": sorted(requested_fact_ids),
                    "event_ids": sorted(requested_event_ids),
                    "candidate_ids": sorted(requested_candidate_ids),
                },
                "resolved_thread_beat_ids": sorted(requested_thread_beat_ids),
                "must_show_evidence": must_show_evidence,
                "direct_event_ids": sorted(direct_event_ids),
                "fact_context_event_ids": sorted(
                    filtered_fact_context_event_ids
                ),
                "expanded_event_ids": sorted(expanded_event_ids),
                "candidate_ids": sorted(related_candidate_ids),
                "evidence_window_ids": sorted(evidence_window_ids),
                "context_window_ids": sorted(context_window_ids),
                "source_ids": sorted(source_ids),
                "direct_range_refs": direct_range_refs,
                "candidate_range_refs": candidate_range_refs,
                "context_range_refs": context_range_refs,
                "range_refs": range_refs,
                "script_evidence_status": beat["evidence_status"],
                "retrieval_status": retrieval_status,
                "missing_requirements": missing_requirements,
                "material_risks": material_risks,
            }
        )
        if script.get("scope_policy", {}).get("story_scope_policy") == "series_global":
            anchor_candidate_id = script["teaser_contract"].get(
                "primary_highlight_candidate_id",
                "opening-highlight-unresolved",
            )
            dependency_ids = set(
                dependency_event_id_set
            ) | set(dependency_unknown_ids)
            source_episode_ids = sorted(
                {
                    int(events[event_id]["episode"])
                    for event_id in dependency_event_id_set
                    if isinstance(events.get(event_id, {}).get("episode"), int)
                }
            )
            source_unit_ids = sorted(
                {
                    unit_id
                    for episode in source_episode_ids
                    for unit_id in units_by_episode.get(
                        episode, {f"episode-{episode:03d}"}
                    )
                }
            )
            is_required = cross_unit_required(
                script=script,
                dependency_events=dependency_events,
                anchor_episodes=anchor_episodes,
                units_by_episode=units_by_episode,
            )
            is_explanation = bool(
                isinstance(beat.get("causal_dependency"), dict)
                and beat["causal_dependency"].get(
                    "explains_opening_highlight", False
                )
            )
            if is_explanation and not dependency_event_id_set:
                retrieval_status = "missing"
            elif dependency_unknown_ids:
                retrieval_status = "missing"
            elif is_explanation and not dependency_event_id_set.issubset(
                expanded_event_ids
            ):
                retrieval_status = "partial"
            elif is_explanation:
                retrieval_status = "covered"
            else:
                retrieval_status = "not_required"
            if is_explanation and retrieval_status == "missing":
                cross_unit_missing = True
            elif is_explanation and retrieval_status == "partial":
                cross_unit_partial = True
            cross_context = {
                "beat_id": beat["id"],
                "opening_candidate_id": anchor_candidate_id,
                "required_context_ids": sorted(dependency_ids),
                "required_event_ids": sorted(dependency_event_id_set),
                "ancestor_episode_range": dependency_range,
                "source_episode_ids": source_episode_ids,
                "source_unit_ids": source_unit_ids,
                "covered_event_ids": sorted(
                    dependency_event_id_set & expanded_event_ids
                ),
                "missing_event_ids": sorted(
                    dependency_event_id_set - expanded_event_ids
                ),
                "retrieval_status": retrieval_status,
                "cross_unit_required": is_required,
                "reason": (
                    "混剪开场高光的前因/关系/规则证据按全剧 Event 索引召回；"
                    "处理章节只作为缓存分区。"
                    if is_explanation
                    else "该 Beat 不承担开场高光的因果解释。"
                ),
            }
            cross_unit_records.append(cross_context)
            beat_evidence[-1]["causal_dependency"] = beat.get(
                "causal_dependency",
                {
                    "explains_opening_highlight": False,
                    "required_before_fact_ids": [],
                    "required_relationship_ids": [],
                    "required_event_ids": [],
                    "required_thread_beat_ids": [],
                    "causal_ancestor_episode_range": dependency_range
                    or {
                        "min_episode": 1,
                        "max_episode": 1,
                        "reason": "无声明的跨单元因果依赖",
                    },
                    "cross_unit_retrieval": {
                        "required": False,
                        "source_unit_ids": [],
                        "retrieval_status": "covered",
                    },
                },
            )
            beat_evidence[-1]["cross_unit_context"] = cross_context
        selected_character_ids.update(requested_character_ids)
        selected_relationship_ids.update(requested_relationship_ids)
        selected_thread_ids.update(requested_thread_ids)
        selected_thread_beat_ids.update(requested_thread_beat_ids)
        selected_fact_ids.update(requested_fact_ids)
        selected_fact_ids.update(
            checked_ids(
                beat["must_not_reveal_fact_ids"],
                facts,
                f"{where}.must_not_reveal_fact_ids",
            )
        )
        selected_fact_ids.update(
            checked_ids(
                beat["required_before_fact_ids"],
                facts,
                f"{where}.required_before_fact_ids",
            )
        )
        selected_fact_ids.update(
            checked_ids(
                beat["introduced_fact_ids"],
                facts,
                f"{where}.introduced_fact_ids",
            )
        )
        selected_question_ids.update(
            checked_ids(
                beat["resolved_question_ids"],
                questions,
                f"{where}.resolved_question_ids",
            )
        )
        selected_event_ids.update(expanded_event_ids)
        selected_candidate_ids.update(related_candidate_ids)
        selected_window_ids.update(expanded_window_ids)
        all_range_refs.extend(range_refs)

    hook = script["ending_hook_intent"]
    hook_event_ids = checked_ids(
        hook["event_ids"], events, "ending_hook_intent.event_ids"
    )
    hook_candidate_ids = checked_ids(
        hook["candidate_ids"], candidates, "ending_hook_intent.candidate_ids"
    )
    selected_thread_ids.update(
        checked_ids(
            hook["story_thread_ids"],
            threads,
            "ending_hook_intent.story_thread_ids",
        )
    )
    for candidate_id in hook_candidate_ids:
        hook_event_ids.update(
            checked_ids(
                candidates[candidate_id].get("event_ids", []),
                events,
                f"candidates[{candidate_id}].event_ids",
            )
        )
    selected_event_ids.update(hook_event_ids)
    selected_candidate_ids.update(hook_candidate_ids)
    hook_refs: list[dict[str, Any]] = []
    for event_id in hook_event_ids:
        hook_refs.extend(event_range_refs(events[event_id]))
    for candidate_id in hook_candidate_ids:
        ref = candidate_range_ref(candidates[candidate_id])
        if ref is not None:
            hook_refs.append(ref)
    hook_refs = unique_range_refs(hook_refs)
    hook_window_ids = {
        window_id
        for item in hook_refs
        for window_id in item["evidence_window_ids"]
    }
    selected_window_ids.update(
        expand_adjacent_windows(
            hook_window_ids,
            neighbors=neighbors,
            hops=adjacent_window_hops,
        )
    )
    all_range_refs.extend(hook_refs)

    for thread_id in list(selected_thread_ids):
        selected_character_ids.update(
            checked_ids(
                threads[thread_id]["character_ids"],
                characters,
                f"story_threads[{thread_id}].character_ids",
            )
        )
        selected_question_ids.update(
            checked_ids(
                threads[thread_id]["open_question_ids"],
                questions,
                f"story_threads[{thread_id}].open_question_ids",
            )
        )
    selected_source_ids = {
        events[event_id]["source_id"] for event_id in selected_event_ids
    }
    selected_source_ids.update(
        candidates[candidate_id]["source_id"]
        for candidate_id in selected_candidate_ids
    )
    selected_source_ids.update(
        manifest_windows[window_id]["source_id"]
        for window_id in selected_window_ids
    )
    unknown_sources = sorted(selected_source_ids - set(sources))
    if unknown_sources:
        raise ValueError(f"packet references unknown sources: {unknown_sources}")
    all_range_refs = unique_range_refs(all_range_refs)
    covered_thread_beat_ids = {
        thread_beat_id
        for thread_beat_id in selected_thread_beat_ids
        if set(thread_beats[thread_beat_id].get("event_ids", []))
        & selected_event_ids
    }
    missing_required_thread_beat_ids = sorted(
        required_thread_beat_ids - covered_thread_beat_ids
    )
    explanation_beats = [
        beat
        for beat in script.get("beats", [])
        if isinstance(beat.get("causal_dependency"), dict)
        and beat["causal_dependency"].get("explains_opening_highlight") is True
    ]
    if (
        script.get("scope_policy", {}).get("story_scope_policy") == "series_global"
        and script.get("edit_mode") == "montage"
        and not explanation_beats
    ):
        cross_unit_missing = True
        cross_unit_records.append(
            {
                "beat_id": script["beats"][0]["id"],
                "opening_candidate_id": script["teaser_contract"][
                    "primary_highlight_candidate_id"
                ],
                "required_context_ids": [],
                "required_event_ids": [],
                "ancestor_episode_range": None,
                "source_episode_ids": [],
                "source_unit_ids": [],
                "covered_event_ids": [],
                "missing_event_ids": [],
                "retrieval_status": "missing",
                "cross_unit_required": True,
                "reason": "montage 必须声明至少一个解释开场高光的 Causal Dependency Beat。",
            }
        )
    if must_have_missing or missing_required_thread_beat_ids or cross_unit_missing:
        packet_status = "incomplete"
    elif status_buckets["partial"] or status_buckets["needs_video_review"]:
        packet_status = "needs_video_review"
    else:
        packet_status = "ready"
    if cross_unit_missing:
        cross_unit_status = "missing"
    elif cross_unit_partial:
        cross_unit_status = "partial"
    elif cross_unit_records:
        cross_unit_status = "covered"
    else:
        cross_unit_status = "not_required"
    report = {
        "schema_version": "1.0",
        "method": "series-global-causal-routing-v1",
        "story_id": script["story_id"],
        "opening_candidate_id": script["teaser_contract"][
            "primary_highlight_candidate_id"
        ],
        "status": cross_unit_status,
        "may_continue_to_story_plan": cross_unit_status in {"not_required", "covered"},
        "analysis_unit_policy": "processing_only",
        "story_scope_policy": "series_global",
        "ancestor_episode_range": script.get("causal_ancestor_episode_range"),
        "source_unit_ids": sorted(
            {
                unit_id
                for record in cross_unit_records
                for unit_id in record.get("source_unit_ids", [])
            }
        ),
        "required_context_ids": sorted(
            {
                context_id
                for record in cross_unit_records
                for context_id in record.get("required_context_ids", [])
            }
        ),
        "covered_context_ids": sorted(
            {
                event_id
                for record in cross_unit_records
                for event_id in record.get("covered_event_ids", [])
            }
        ),
        "missing_context_ids": sorted(
            {
                context_id
                for record in cross_unit_records
                for context_id in (
                    record.get("missing_event_ids", [])
                    + (["causal_dependency"] if record.get("retrieval_status") == "missing" and not record.get("required_event_ids") else [])
                )
            }
        ),
        "beats": cross_unit_records,
    }
    packet = {
        "schema_version": "1.2",
        "method": "structured-thread-beat-recall-v3",
        "story_id": script["story_id"],
        "title": script["title"],
        "production_slot": script["portfolio"]["production_slot"],
        "teaser_contract": script["teaser_contract"],
        "status": packet_status,
        "approval_binding": {
            "story_script_sha256": approval_item["approved_script_sha256"],
            "portfolio_sha256": approval_item["portfolio_sha256"],
            "decided_at": approval_item["decided_at"],
            "accepted_material_risks": bool(
                approval_item.get("accepted_material_risks")
            ),
            "reviewer_notes": str(approval_item.get("notes", "")),
        },
        "input_fingerprints": {
            "story_approval_sha256": approval_sha256,
            **fingerprints,
        },
        "retrieval_policy": {
            "adjacent_window_hops": adjacent_window_hops,
            "semantic_search_used": False,
            "vector_search_used": False,
            "analysis_unit_policy": "processing_only",
            "story_scope_policy": "series_global",
            "cross_unit_retrieval_enabled": True,
        },
        "cross_unit_context_report": report,
        "coverage_summary": {
            "beat_count": len(beat_evidence),
            "covered_beat_ids": status_buckets["covered"],
            "partial_beat_ids": status_buckets["partial"],
            "missing_beat_ids": status_buckets["missing"],
            "needs_video_review_beat_ids": status_buckets["needs_video_review"],
            "must_have_missing_beat_ids": must_have_missing,
            "required_thread_beat_ids": sorted(required_thread_beat_ids),
            "covered_thread_beat_ids": sorted(covered_thread_beat_ids),
            "missing_required_thread_beat_ids": missing_required_thread_beat_ids,
            "source_count": len(selected_source_ids),
            "range_count": len(all_range_refs),
            "unique_evidence_duration_seconds": unique_duration_seconds(
                all_range_refs
            ),
        },
        "beat_evidence": beat_evidence,
        "evidence_catalog": {
            "sources": [
                source_snapshot(sources[item_id])
                for item_id in sorted(
                    selected_source_ids,
                    key=lambda value: (
                        int(sources[value]["episode"]),
                        value,
                    ),
                )
            ],
            "windows": [
                window_summaries[item_id]
                for item_id in sorted(
                    selected_window_ids,
                    key=lambda value: (
                        int(window_summaries[value]["episode"]),
                        float(window_summaries[value]["window"]["start"]),
                        value,
                    ),
                )
            ],
            "events": [
                events[item_id]
                for item_id in sorted(
                    selected_event_ids,
                    key=lambda value: (
                        int(events[value]["episode"]),
                        float(events[value]["source_ranges"][0]["start"]),
                        value,
                    ),
                )
            ],
            "candidates": [
                candidates[item_id]
                for item_id in sorted(
                    selected_candidate_ids,
                    key=lambda value: (
                        int(candidates[value]["episode"]),
                        float(candidates[value]["start"]),
                        value,
                    ),
                )
            ],
            "characters": [
                characters[item_id] for item_id in sorted(selected_character_ids)
            ],
            "relationships": [
                relationships[item_id]
                for item_id in sorted(selected_relationship_ids)
            ],
            "facts": [facts[item_id] for item_id in sorted(selected_fact_ids)],
            "story_threads": [
                threads[item_id] for item_id in sorted(selected_thread_ids)
            ],
            "thread_beats": [
                thread_beats[item_id]
                for item_id in sorted(selected_thread_beat_ids)
            ],
            "open_questions": [
                questions[item_id] for item_id in sorted(selected_question_ids)
            ],
        },
    }
    schema_errors = validate_task_response("story_evidence_packet", packet)
    if schema_errors:
        raise ValueError(
            "invalid Story Evidence Packet: " + "; ".join(schema_errors[:40])
        )
    return packet


def render_review(packets: list[dict[str, Any]]) -> str:
    lines = ["# Story Evidence Retrieval 复核", ""]
    for packet in packets:
        coverage = packet["coverage_summary"]
        lines.extend(
            [
                f"## 槽位 {packet['production_slot']}：{packet['title']}",
                "",
                f"- Story ID：`{packet['story_id']}`",
                f"- 检索状态：`{packet['status']}`",
                f"- 证据原片去重时长：{coverage['unique_evidence_duration_seconds']:.1f} 秒",
                f"- 证据范围：{coverage['range_count']} 个；来源：{coverage['source_count']} 集",
                f"- 跨处理单元因果检索：`{packet.get('cross_unit_context_report', {}).get('status', 'not_required')}`；"
                f"可进入 Story Plan：`{packet.get('cross_unit_context_report', {}).get('may_continue_to_story_plan', False)}`",
                (
                    "- 缺失 required Thread Beat："
                    + (
                        ", ".join(
                            coverage["missing_required_thread_beat_ids"]
                        )
                        or "无"
                    )
                ),
                "",
                "| Beat | 角色 | 检索状态 | Event | Candidate | 证据窗 | 相邻窗 |",
                "|---|---|---|---:|---:|---:|---:|",
            ]
        )
        for beat in packet["beat_evidence"]:
            lines.append(
                f"| `{beat['beat_id']}` | {beat['role']} | "
                f"`{beat['retrieval_status']}` | "
                f"{len(beat['expanded_event_ids'])} | "
                f"{len(beat['candidate_ids'])} | "
                f"{len(beat['evidence_window_ids'])} | "
                f"{len(beat['context_window_ids'])} |"
            )
        risks = [
            f"{beat['beat_id']}: {risk}"
            for beat in packet["beat_evidence"]
            for risk in beat["material_risks"]
        ]
        lines.extend(["", "### 素材缺口与复核风险", ""])
        if risks:
            lines.extend(f"- {item}" for item in dict.fromkeys(risks))
        else:
            lines.append("- 无。")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"