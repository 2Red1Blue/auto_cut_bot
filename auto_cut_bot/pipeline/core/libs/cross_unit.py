#!/usr/bin/env python3
"""Deterministic routing for story evidence across processing partitions.

Chapter/analysis units are operational partitions only.  This module keeps
the policy in one place so Story Script preflight, Evidence Packet building,
and validation cannot silently apply different episode scopes.
"""

from __future__ import annotations

from typing import Any, Iterable


POLICY_VERSION = "series-global-causal-routing-v1"


def processing_unit_map(
    manifest_windows: Iterable[dict[str, Any]],
) -> dict[int, set[str]]:
    """Return episode -> processing-unit IDs without treating them as story bounds."""
    result: dict[int, set[str]] = {}
    for item in manifest_windows:
        episode = item.get("episode")
        if not isinstance(episode, int) or isinstance(episode, bool):
            continue
        unit_id = (
            item.get("processing_unit_id")
            or item.get("analysis_unit_id")
            or item.get("chapter_id")
            or item.get("unit_id")
        )
        if not isinstance(unit_id, str) or not unit_id:
            unit_id = f"episode-{episode:03d}"
        result.setdefault(episode, set()).add(unit_id)
    return result


def _ids(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    return {item for item in values if isinstance(item, str) and item}


def dependency_event_ids(
    script: dict[str, Any],
    beat: dict[str, Any],
    *,
    events: dict[str, dict[str, Any]],
    facts: dict[str, dict[str, Any]],
    relationships: dict[str, dict[str, Any]],
    thread_beats: dict[str, dict[str, Any]],
) -> tuple[set[str], set[str]]:
    """Resolve explicit causal-context IDs to Event IDs.

    The first return value is the resolved Event set.  The second is a set of
    unknown IDs, which is deliberately surfaced instead of being guessed.
    """
    dependency = beat.get("causal_dependency", {})
    if not isinstance(dependency, dict):
        dependency = {}
    event_ids = set()
    unknown: set[str] = set()
    direct = _ids(dependency.get("required_event_ids"))
    for event_id in direct:
        if event_id in events:
            event_ids.add(event_id)
        else:
            unknown.add(event_id)
    for fact_id in _ids(dependency.get("required_before_fact_ids")):
        fact = facts.get(fact_id)
        if fact is None:
            unknown.add(fact_id)
            continue
        event_ids.update(
            event_id for event_id in _ids(fact.get("event_ids")) if event_id in events
        )
    for relationship_id in _ids(dependency.get("required_relationship_ids")):
        relationship = relationships.get(relationship_id)
        if relationship is None:
            unknown.add(relationship_id)
            continue
        for change in relationship.get("state_changes", []):
            if not isinstance(change, dict):
                continue
            event_id = change.get("event_id")
            if isinstance(event_id, str) and event_id in events:
                event_ids.add(event_id)
    for beat_id in _ids(dependency.get("required_thread_beat_ids")):
        thread_beat = thread_beats.get(beat_id)
        if thread_beat is None:
            unknown.add(beat_id)
            continue
        event_ids.update(
            event_id
            for event_id in _ids(thread_beat.get("event_ids"))
            if event_id in events
        )
    return event_ids, unknown


def dependency_episode_range(
    beat: dict[str, Any],
    dependency_events: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    dependency = beat.get("causal_dependency", {})
    if isinstance(dependency, dict):
        explicit = dependency.get("causal_ancestor_episode_range")
        if isinstance(explicit, dict):
            low = explicit.get("min_episode")
            high = explicit.get("max_episode")
            if (
                isinstance(low, int)
                and isinstance(high, int)
                and not isinstance(low, bool)
                and not isinstance(high, bool)
                and low >= 1
                and high >= low
            ):
                return {
                    "min_episode": low,
                    "max_episode": high,
                    "reason": str(explicit.get("reason", "")),
                }
    episodes = sorted(
        int(event["episode"])
        for event in dependency_events.values()
        if isinstance(event.get("episode"), int)
    )
    if not episodes:
        return None
    return {
        "min_episode": episodes[0],
        "max_episode": episodes[-1],
        "reason": "由显式因果依赖 Event 的全剧证据范围确定",
    }


def filter_events_by_episode_range(
    event_ids: Iterable[str],
    *,
    events: dict[str, dict[str, Any]],
    episode_range: dict[str, Any] | None,
) -> set[str]:
    if not isinstance(episode_range, dict):
        return set(event_ids)
    low = episode_range.get("min_episode")
    high = episode_range.get("max_episode")
    if not isinstance(low, int) or not isinstance(high, int):
        return set()
    return {
        event_id
        for event_id in event_ids
        if event_id in events
        and isinstance(events[event_id].get("episode"), int)
        and low <= int(events[event_id]["episode"]) <= high
    }


def cross_unit_required(
    *,
    script: dict[str, Any],
    dependency_events: dict[str, dict[str, Any]],
    anchor_episodes: set[int],
    units_by_episode: dict[int, set[str]],
) -> bool:
    policy = script.get("scope_policy", {})
    if not isinstance(policy, dict):
        return False
    if script.get("edit_mode") != "montage":
        return False
    if not policy.get("cross_unit_retrieval_allowed", False):
        return False
    if policy.get("cross_unit_retrieval_required_for_montage", False):
        dep_episodes = {
            int(event["episode"])
            for event in dependency_events.values()
            if isinstance(event.get("episode"), int)
        }
        if dep_episodes and anchor_episodes:
            anchor_units = {
                unit
                for episode in anchor_episodes
                for unit in units_by_episode.get(episode, {f"episode-{episode:03d}"})
            }
            dep_units = {
                unit
                for episode in dep_episodes
                for unit in units_by_episode.get(episode, {f"episode-{episode:03d}"})
            }
            return dep_units != anchor_units or min(dep_episodes) < min(anchor_episodes)
    return False


def default_scope_policy() -> dict[str, Any]:
    return {
        "analysis_unit_policy": "processing_only",
        "story_scope_policy": "series_global",
        "cross_unit_retrieval_allowed": True,
        "cross_unit_retrieval_required_for_montage": True,
        "unresolved_dependency_action": "blocked",
        "policy_version": POLICY_VERSION,
    }
