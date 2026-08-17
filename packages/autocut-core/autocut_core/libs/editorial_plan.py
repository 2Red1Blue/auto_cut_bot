#!/usr/bin/env python3
"""Compile and enforce deterministic legal options for Story Plan selection."""

from __future__ import annotations

import re
from collections import defaultdict
from copy import deepcopy
from statistics import median
from pathlib import Path
from typing import Any

from autocut_core.io import json_sha256, load_json, sha256_file, stable_id
from autocut_core.libs._common import rounded
from autocut_core.schema.compat import validate_task_response


COMPILER_VERSION = "story-plan-legal-option-compiler-v13-editorial-golden-contract"
TEASER_MAXIMUM_SECONDS = 30.0
MAXIMUM_REPEAT_SECONDS = 20.0
MAX_SPANS_PER_OPTION = 6
MAX_OPTIONS_PER_BEAT_GROUP = 64
MAX_NONDOMINATED_CANDIDATES = 160
MAX_SEARCH_NODES = 250_000
MAX_LEGAL_BODY_PARTITIONS = 500
MAX_PARTITION_SEARCH_NODES = 1_000_000
MINIMUM_EDITORIAL_SURPLUS_RATIO = 0.0
MAXIMUM_FULL_SOURCE_LIKE_CLIPS_PER_OPTION = 1
MAXIMUM_FULL_SOURCE_LIKE_CLIPS_PER_STORY = 1
# When the whole candidate pool is short enough that ``full_source_like``
# clips are structurally scenes rather than "整集拼盘", allow up to
# ``NARROW_POOL_MAX_FULL_SOURCE_LIKE_CLIPS_PER_STORY`` full clips per Story.
# The 50 % playback-ratio guard in materialize continues to prevent actual
# whole-episode stitching.
NARROW_POOL_AVAILABLE_THRESHOLD_SECONDS = 350.0
NARROW_POOL_MAX_FULL_SOURCE_LIKE_CLIPS_PER_STORY = 2
PREFERRED_MEDIAN_CLIP_SECONDS_RANGE = (12.0, 40.0)
PREFERRED_MINIMUM_CLIP_COUNT_DIVISOR = 40.0
PLANNING_CONTRACT_VERSION = "planning-contract-v9-editorial-golden-contract"


def effective_max_full_source_like_clips_per_story(
    available_candidate_unique_duration_seconds: float,
) -> int:
    """Return the effective cap on full_source_like clips for a Story.

    Wide-pool stories (avail ≥ threshold) keep the strict cap of 1 to keep
    editorial density honest. Narrow-pool stories relax to 2 — for them a
    "full episode" is typically a single scene, not a stitched whole episode.
    The 50 % playback-ratio guard remains the ultimate safety net.
    """
    if (
        available_candidate_unique_duration_seconds
        < NARROW_POOL_AVAILABLE_THRESHOLD_SECONDS
    ):
        return NARROW_POOL_MAX_FULL_SOURCE_LIKE_CLIPS_PER_STORY
    return MAXIMUM_FULL_SOURCE_LIKE_CLIPS_PER_STORY


def _merged_source_duration(
    ranges: list[tuple[str, float, float]]
) -> float:
    """Return the merged unique duration across (source_id, start, end) records."""
    by_source: dict[str, list[tuple[float, float]]] = {}
    for source_id, start, end in ranges:
        by_source.setdefault(source_id, []).append(
            (float(start), float(end))
        )
    total = 0.0
    for intervals in by_source.values():
        merged: list[list[float]] = []
        for start, end in sorted(intervals):
            if not merged or start > merged[-1][1] + 1e-3:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        total += sum(end - start for start, end in merged)
    return round(total, 3)


def _bundle_available_unique_duration(
    candidates: list[dict[str, Any]]
) -> float:
    records: list[tuple[str, float, float]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        duration = float(candidate.get("duration_seconds", 0.0))
        if duration <= 0:
            continue
        source_id = candidate.get("source_id")
        start = candidate.get("start")
        end = candidate.get("end")
        if not isinstance(source_id, str) or not isinstance(
            start, (int, float)
        ) or not isinstance(end, (int, float)):
            continue
        records.append((source_id, float(start), float(end)))
    return _merged_source_duration(records)


def _functional_option_pool_unique_duration(
    candidates: list[dict[str, Any]],
    teaser_options: list[dict[str, Any]],
    body_options: list[dict[str, Any]],
) -> float:
    """Measure only Spans that can appear in at least one Legal Option."""
    option_span_ids = {
        span_id
        for option in [*teaser_options, *body_options]
        for span_id in option.get("span_candidate_ids", [])
        if isinstance(span_id, str)
    }
    return _bundle_available_unique_duration(
        [
            candidate
            for candidate in candidates
            if candidate.get("span_candidate_id") in option_span_ids
        ]
    )


def _fact_events_from_packet(
    evidence_packet: dict[str, Any] | None,
) -> dict[str, set[str]]:
    if not isinstance(evidence_packet, dict):
        return {}
    catalog = evidence_packet.get("evidence_catalog", {})
    facts = catalog.get("facts", []) if isinstance(catalog, dict) else []
    result: dict[str, set[str]] = {}
    for item in facts:
        if not isinstance(item, dict):
            continue
        fact_id = item.get("id")
        event_ids = item.get("event_ids", [])
        if isinstance(fact_id, str) and isinstance(event_ids, list):
            result[fact_id] = {
                event_id for event_id in event_ids if isinstance(event_id, str)
            }
    return result


def _leak_events_by_beat(
    beats: list[dict[str, Any]],
    fact_events: dict[str, set[str]],
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for beat in beats:
        withheld: set[str] = set()
        for fact_id in beat.get("must_not_reveal_fact_ids", []):
            if isinstance(fact_id, str):
                withheld |= fact_events.get(fact_id, set())
        result[beat["id"]] = withheld
    return result


def _candidate_leaks_group(
    candidate: dict[str, Any],
    group_beat_ids: set[str],
    leak_events_by_beat: dict[str, set[str]],
) -> bool:
    events = set(candidate.get("event_ids", []))
    if not events:
        return False
    for beat_id in candidate.get("supports_beat_ids", []):
        if beat_id not in group_beat_ids:
            continue
        leak_events = leak_events_by_beat.get(beat_id)
        if leak_events and events & leak_events:
            return True
    return False


def _minimum_full_source_like_clips(
    body_beats: list[dict[str, Any]],
    body_options: list[dict[str, Any]],
) -> float:
    """Return the minimum full_source_like clip count reachable by any valid
    contiguous partition of ``body_beats`` using ``body_options``.

    Returns ``float('inf')`` when no partition exists.
    """
    n = len(body_beats)
    if n == 0:
        return 0.0
    beat_index = {beat["id"]: index for index, beat in enumerate(body_beats)}
    expected_by_start: dict[int, list[str]] = {
        index: [item["id"] for item in body_beats[index:]]
        for index in range(n)
    }
    dp: list[float] = [float("inf")] * (n + 1)
    dp[0] = 0.0
    for option in body_options:
        beat_ids = option["beat_ids"]
        if not beat_ids:
            continue
        start = beat_index.get(beat_ids[0])
        if start is None:
            continue
        end = start + len(beat_ids)
        if end > n:
            continue
        if beat_ids != expected_by_start[start][: len(beat_ids)]:
            continue
        candidate_full = float(option["full_source_like_clip_count"])
        if dp[start] + candidate_full < dp[end]:
            dp[end] = dp[start] + candidate_full
    return dp[n]


def _beat_role(beat: dict[str, Any]) -> str:
    role = beat.get("role")
    return "teaser" if role == "teaser_intent" else str(role)


def _overlap_seconds(left: dict[str, Any], right: dict[str, Any]) -> float:
    if left.get("source_id") != right.get("source_id"):
        return 0.0
    return max(
        0.0,
        min(float(left["end"]), float(right["end"]))
        - max(float(left["start"]), float(right["start"])),
    )


def _has_internal_overlap(candidates: list[dict[str, Any]]) -> bool:
    return any(
        _overlap_seconds(left, right) > 0.05
        for index, left in enumerate(candidates)
        for right in candidates[index + 1 :]
    )


def legal_option_id(
    story_id: str,
    kind: str,
    beat_ids: list[str],
    span_candidate_ids: list[str],
) -> str:
    """Return the stable identity used by compiled legal options."""
    return stable_id(
        "plan-option",
        {
            "compiler_version": COMPILER_VERSION,
            "story_id": story_id,
            "kind": kind,
            "beat_ids": beat_ids,
            "span_candidate_ids": sorted(span_candidate_ids),
        },
    )


def _requirement_tokens(
    beats: list[dict[str, Any]],
    *,
    require_highlight: bool,
) -> set[str]:
    """Compute per-beat-group covering requirements.

    Only ``beat:X`` and ``must_show:Y`` are hard partition requirements —
    they define what a covering set MUST prove is on-screen. Per-beat
    ``retrieval_requirements.thread_beat_ids`` is a retrieval hint for
    Evidence recall, not a partition constraint, so we deliberately do NOT
    turn it into a required token here. Story-level Thread Beat coverage is
    still enforced against ``script.required_thread_beat_ids`` by
    ``materialize_story_plans``.
    """
    tokens = {f"beat:{beat['id']}" for beat in beats}
    for beat in beats:
        tokens.update(
            f"must_show:{item['id']}"
            for item in beat.get("must_show", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        )
    if require_highlight:
        tokens.add("highlight_provenance")
    return tokens


def _candidate_tokens(
    candidate: dict[str, Any],
    *,
    beat_ids: set[str],
    must_show_ids: set[str],
    thread_beat_ids: set[str],
    highlight_candidate_ids: set[str],
) -> set[str]:
    tokens = {
        f"beat:{beat_id}"
        for beat_id in candidate.get("supports_beat_ids", [])
        if beat_id in beat_ids
    }
    tokens.update(
        f"must_show:{must_show_id}"
        for must_show_id in candidate.get("supports_must_show_ids", [])
        if must_show_id in must_show_ids
    )
    tokens.update(
        f"thread:{thread_beat_id}"
        for thread_beat_id in candidate.get(
            "supports_thread_beat_ids", []
        )
        if thread_beat_id in thread_beat_ids
    )
    if highlight_candidate_ids.intersection(candidate.get("candidate_ids", [])):
        tokens.add("highlight_provenance")
    return tokens


def _nondominated_candidates(
    entries: list[tuple[dict[str, Any], set[str]]]
) -> tuple[list[tuple[dict[str, Any], set[str]]], int]:
    """Pareto filter that preserves editorial richness.

    Duration is intentionally NOT a dominance criterion — a longer candidate
    may be exactly what the Story needs to reach its target duration. Entry A
    dominates entry B only when A is strictly better on the criteria that are
    always undesirable to inflate:

    * ``tokens(A) ⊇ tokens(B)`` (covers at least as much)
    * ``full_source_like(A) ≤ full_source_like(B)`` (no worse on integrality)
    * ``span_count(A) ≤ 1`` (single-span candidates dominate identical
      multi-span ones)
    * ``A != B``

    Ties are kept. Cap at ``MAX_NONDOMINATED_CANDIDATES`` afterwards, biasing
    the survivors toward duration diversity so the diverse covering-set
    sampler still has richness to choose from. Returns
    ``(survivors, dropped_count)``.
    """
    total = len(entries)
    if total == 0:
        return [], 0
    dominated: set[int] = set()
    for j, (candidate_b, tokens_b) in enumerate(entries):
        if j in dominated:
            continue
        full_b = int(bool(candidate_b.get("full_source_like")))
        for i, (candidate_a, tokens_a) in enumerate(entries):
            if i == j:
                continue
            # Global feasibility depends on source/time overlap with Teaser
            # and later body options.  A candidate from another interval is
            # therefore never safely dominated merely because it covers the
            # same tokens.  Only compare identical source intervals (normally
            # already merged by the Span compiler).
            if (
                candidate_a.get("source_id") != candidate_b.get("source_id")
                or float(candidate_a.get("start", -1))
                != float(candidate_b.get("start", -2))
                or float(candidate_a.get("end", -1))
                != float(candidate_b.get("end", -2))
            ):
                continue
            if not (tokens_a >= tokens_b):
                continue
            full_a = int(bool(candidate_a.get("full_source_like")))
            if full_a > full_b:
                continue
            if not (
                tokens_a > tokens_b or full_a < full_b
            ):
                # Ties on both dimensions — keep both so the diverse sampler
                # can select across duration buckets.
                continue
            dominated.add(j)
            break
    survivors = [
        entries[index]
        for index in range(total)
        if index not in dominated
    ]
    # Order survivors so the cap keeps a duration-diverse mix.
    survivors_sorted = sorted(
        survivors,
        key=lambda item: (
            bool(item[0].get("full_source_like")),
            float(item[0]["duration_seconds"]),
            item[0]["span_candidate_id"],
        ),
    )
    dropped_by_cap = max(
        0, len(survivors_sorted) - MAX_NONDOMINATED_CANDIDATES
    )
    if dropped_by_cap:
        # Pin source/interval classes first so later Teaser/body compatibility
        # still has alternatives after the local candidate cap.
        compatibility_frontier: list[
            tuple[dict[str, Any], set[str]]
        ] = []
        seen_compatibility: set[tuple[Any, ...]] = set()
        for entry in survivors_sorted:
            candidate = entry[0]
            signature = (
                candidate.get("source_id"),
                int(float(candidate.get("start", 0.0)) // 15),
                int(float(candidate.get("end", 0.0)) // 15),
                bool(candidate.get("full_source_like")),
            )
            if signature in seen_compatibility:
                continue
            seen_compatibility.add(signature)
            compatibility_frontier.append(entry)
            if len(compatibility_frontier) >= max(
                1, MAX_NONDOMINATED_CANDIDATES // 3
            ):
                break
        # Pin the coverage-rich frontier next. This prevents a continuous
        # scene union carrying several adjacent Beat / must-show / Thread
        # claims from disappearing merely because many duration variants hit
        # the cap.
        pinned_cap = max(1, MAX_NONDOMINATED_CANDIDATES // 4)
        coverage_frontier = sorted(
            survivors_sorted,
            key=lambda item: (
                -len(item[1]),
                bool(item[0].get("full_source_like")),
                -float(item[0]["duration_seconds"]),
                item[0]["span_candidate_id"],
            ),
        )[:pinned_cap]
        coverage_frontier = [
            *compatibility_frontier,
            *coverage_frontier,
        ][: max(1, MAX_NONDOMINATED_CANDIDATES // 2)]
        pinned_ids = {
            item[0]["span_candidate_id"] for item in coverage_frontier
        }
        remaining = [
            item
            for item in survivors_sorted
            if item[0]["span_candidate_id"] not in pinned_ids
        ]
        available_slots = (
            MAX_NONDOMINATED_CANDIDATES - len(coverage_frontier)
        )
        n = len(remaining)
        per_bucket = available_slots // 3
        remainder = available_slots - per_bucket * 3
        third = max(1, n // 3)
        picked = list(coverage_frontier)
        picked.extend(remaining[:per_bucket])
        picked.extend(remaining[third : third + per_bucket])
        picked.extend(remaining[-(per_bucket + remainder):])
        seen: set[str] = set()
        capped: list[tuple[dict[str, Any], set[str]]] = []
        for entry in picked:
            span_id = entry[0]["span_candidate_id"]
            if span_id in seen:
                continue
            seen.add(span_id)
            capped.append(entry)
        for entry in survivors_sorted:
            if len(capped) >= MAX_NONDOMINATED_CANDIDATES:
                break
            span_id = entry[0]["span_candidate_id"]
            if span_id in seen:
                continue
            seen.add(span_id)
            capped.append(entry)
        survivors_sorted = capped
    return survivors_sorted, len(dominated) + dropped_by_cap


def _sample_diverse_covering_sets(
    values: list[list[dict[str, Any]]], cap: int
) -> tuple[list[list[dict[str, Any]]], int]:
    """Take ``cap`` covering sets biased toward duration richness.

    Distribution is short:medium:long ≈ 1:2:2 so richer covering sets carry
    more slots than the minimum-duration bucket. Returns ``(picked, dropped)``.
    Unchanged when ``len(values) <= cap``.
    """
    if len(values) <= cap:
        return values, 0
    total_values = len(values)
    values_sorted = sorted(
        values,
        key=lambda selection: (
            round(
                sum(float(item["duration_seconds"]) for item in selection),
                3,
            ),
            len(selection),
            tuple(sorted(item["span_candidate_id"] for item in selection)),
        ),
    )
    # Preserve interval/source compatibility classes before duration
    # sampling.  Two covers with the same duration and coverage can behave
    # very differently once Teaser overlap and later body blocks are added;
    # duration-only Top-K used to erase the only globally compatible cover.
    signature_frontier: list[list[dict[str, Any]]] = []
    seen_signatures: set[tuple[Any, ...]] = set()
    for selection in values_sorted:
        signature = tuple(
            sorted(
                (
                    str(item.get("source_id", "")),
                    int(float(item.get("start", 0.0)) // 15),
                    int(float(item.get("end", 0.0)) // 15),
                    bool(item.get("full_source_like")),
                )
                for item in selection
            )
        )
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        signature_frontier.append(selection)
        if len(signature_frontier) >= max(1, cap // 3):
            break

    # Pin efficient coverage-rich unions as a second frontier. A one-Span
    # continuous-scene union is structurally different from a fragmented
    # multi-Span cover and must survive the option cap.
    pinned_cap = max(1, cap // 4)
    coverage_frontier = sorted(
        values_sorted,
        key=lambda selection: (
            len(selection),
            -sum(
                len(item.get("supports_beat_ids", []))
                + len(item.get("supports_must_show_ids", []))
                + len(item.get("supports_thread_beat_ids", []))
                for item in selection
            ),
            -sum(
                float(item["duration_seconds"]) for item in selection
            ),
            tuple(
                sorted(item["span_candidate_id"] for item in selection)
            ),
        ),
    )[:pinned_cap]
    coverage_frontier = [
        *signature_frontier,
        *coverage_frontier,
    ]
    coverage_frontier = list(
        {
            tuple(
                sorted(item["span_candidate_id"] for item in selection)
            ): selection
            for selection in coverage_frontier
        }.values()
    )[: max(1, cap // 2)]
    pinned_keys = {
        tuple(sorted(item["span_candidate_id"] for item in selection))
        for selection in coverage_frontier
    }
    unpinned = [
        selection
        for selection in values_sorted
        if tuple(
            sorted(item["span_candidate_id"] for item in selection)
        )
        not in pinned_keys
    ]
    remaining_cap = cap - len(coverage_frontier)
    short_share = max(0, remaining_cap // 5)
    mid_share = max(0, (remaining_cap * 2) // 5)
    long_share = remaining_cap - short_share - mid_share
    n = len(unpinned)
    third = max(1, n // 3)
    short_bucket = unpinned[:third]
    long_bucket = unpinned[-third:]
    middle_bucket = unpinned[third : n - third] or unpinned[
        third : third + 1
    ]
    picked: list[list[dict[str, Any]]] = list(coverage_frontier)
    picked.extend(short_bucket[:short_share])
    picked.extend(middle_bucket[:mid_share])
    picked.extend(long_bucket[-long_share:] if long_share else [])
    seen: set[tuple[str, ...]] = set()
    deduped: list[list[dict[str, Any]]] = []
    for selection in picked:
        key = tuple(sorted(item["span_candidate_id"] for item in selection))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(selection)
    for selection in values_sorted:
        if len(deduped) >= cap:
            break
        key = tuple(sorted(item["span_candidate_id"] for item in selection))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(selection)
    return deduped, total_values - len(deduped)


def _covering_candidate_sets(
    candidates: list[dict[str, Any]],
    beats: list[dict[str, Any]],
    *,
    highlight_candidate_ids: set[str],
    maximum_duration_seconds: float | None,
    leak_events_by_beat: dict[str, set[str]] | None = None,
    scope_optional_thread_beat_ids: set[str] | None = None,
) -> tuple[list[list[dict[str, Any]]], dict[str, int]]:
    """Search covering sets and diversify by duration bucket.

    Returns ``(selections, diagnostics)`` where ``diagnostics`` reports:

    * ``candidates_considered``
    * ``candidates_after_leak_filter``
    * ``candidates_dropped_by_pareto``
    * ``covering_sets_found``
    * ``covering_sets_dropped_by_option_cap``
    * ``search_nodes_used``
    * ``search_nodes_exhausted`` (bool)
    """
    total_candidates = len(candidates)
    required = _requirement_tokens(
        beats,
        require_highlight=bool(highlight_candidate_ids),
    )
    beat_ids = {beat["id"] for beat in beats}
    if leak_events_by_beat:
        candidates = [
            candidate
            for candidate in candidates
            if not _candidate_leaks_group(
                candidate, beat_ids, leak_events_by_beat
            )
        ]
    candidates_after_leak = len(candidates)
    must_show_ids = {
        item["id"]
        for beat in beats
        for item in beat.get("must_show", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    thread_beat_ids = {
        thread_beat_id
        for beat in beats
        for thread_beat_id in beat.get(
            "retrieval_requirements", {}
        ).get("thread_beat_ids", [])
        if isinstance(thread_beat_id, str)
    }
    scope_optional_tokens = {
        f"thread:{thread_beat_id}"
        for thread_beat_id in thread_beat_ids
        if thread_beat_id in (scope_optional_thread_beat_ids or set())
    }
    entries = []
    for candidate in candidates:
        tokens = _candidate_tokens(
            candidate,
            beat_ids=beat_ids,
            must_show_ids=must_show_ids,
            thread_beat_ids=thread_beat_ids,
            highlight_candidate_ids=highlight_candidate_ids,
        )
        if tokens:
            entries.append((candidate, tokens))
    entries, pareto_dropped = _nondominated_candidates(entries)
    by_token: dict[str, list[int]] = defaultdict(list)
    for index, (_, tokens) in enumerate(entries):
        for token in tokens:
            by_token[token].append(index)
    diagnostics = {
        "candidates_considered": total_candidates,
        "candidates_after_leak_filter": candidates_after_leak,
        "candidates_dropped_by_pareto": pareto_dropped,
        "covering_sets_found": 0,
        "covering_sets_dropped_by_option_cap": 0,
        "search_nodes_used": 0,
        "search_nodes_exhausted": False,
    }
    if any(token not in by_token for token in required):
        return [], diagnostics

    results: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    visited: set[tuple[int, ...]] = set()
    search_nodes = 0
    exhausted = False

    def search(
        selected_indexes: tuple[int, ...],
        covered: set[str],
        duration: float,
    ) -> None:
        nonlocal search_nodes, exhausted
        search_nodes += 1
        if search_nodes > MAX_SEARCH_NODES:
            exhausted = True
            return
        base_complete = required <= covered
        if base_complete:
            selected = [entries[index][0] for index in selected_indexes]
            if not _has_internal_overlap(selected):
                key = tuple(
                    sorted(item["span_candidate_id"] for item in selected)
                )
                results[key] = selected
            if len(selected_indexes) >= MAX_SPANS_PER_OPTION:
                return
            branch_tokens = sorted(
                token
                for token in scope_optional_tokens - covered
                if token in by_token
            )
            if not branch_tokens:
                return
        else:
            if len(selected_indexes) >= MAX_SPANS_PER_OPTION:
                return
            uncovered = required - covered
            branch_tokens = [
                min(
                    uncovered,
                    key=lambda item: sum(
                        1
                        for index in by_token[item]
                        if index not in selected_indexes
                    ),
                )
            ]
        selected = [entries[index][0] for index in selected_indexes]
        for token in branch_tokens:
            for index in by_token[token]:
                if index in selected_indexes:
                    continue
                candidate, tokens = entries[index]
                next_duration = duration + float(
                    candidate["duration_seconds"]
                )
                if (
                    maximum_duration_seconds is not None
                    and next_duration > maximum_duration_seconds + 0.001
                ):
                    continue
                if any(
                    _overlap_seconds(candidate, existing) > 0.05
                    for existing in selected
                ):
                    continue
                next_indexes = tuple(
                    sorted((*selected_indexes, index))
                )
                if next_indexes in visited:
                    continue
                visited.add(next_indexes)
                search(next_indexes, covered | tokens, next_duration)

    search((), set(), 0.0)
    values = list(results.values())
    values.sort(
        key=lambda items: (
            sum(bool(item.get("full_source_like")) for item in items),
            round(sum(float(item["duration_seconds"]) for item in items), 3),
            len(items),
            tuple(sorted(item["span_candidate_id"] for item in items)),
        )
    )
    if any(
        not any(item.get("full_source_like") for item in selection)
        for selection in values
    ):
        values = [
            selection
            for selection in values
            if not any(item.get("full_source_like") for item in selection)
        ]
    diagnostics["covering_sets_found"] = len(values)
    diagnostics["search_nodes_used"] = search_nodes
    diagnostics["search_nodes_exhausted"] = exhausted
    picked, dropped_by_cap = _sample_diverse_covering_sets(
        values, MAX_OPTIONS_PER_BEAT_GROUP
    )
    diagnostics["covering_sets_dropped_by_option_cap"] = dropped_by_cap
    return picked, diagnostics


def _option(
    *,
    story_id: str,
    kind: str,
    beats: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    highlight_candidate_ids: set[str],
) -> dict[str, Any]:
    beat_ids = [beat["id"] for beat in beats]
    span_ids = sorted(item["span_candidate_id"] for item in candidates)
    if kind == "teaser" and len(span_ids) != 1:
        raise ValueError(
            "single_highlight Teaser option must contain exactly one Span"
        )
    required_must_show_ids = sorted(
        {
            item["id"]
            for beat in beats
            for item in beat.get("must_show", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
    )
    required_thread_beat_ids = sorted(
        {
            thread_beat_id
            for beat in beats
            for thread_beat_id in beat.get(
                "retrieval_requirements", {}
            ).get("thread_beat_ids", [])
            if isinstance(thread_beat_id, str)
        }
    )
    return {
        "option_id": legal_option_id(story_id, kind, beat_ids, span_ids),
        "kind": kind,
        "role": "teaser" if kind == "teaser" else _beat_role(beats[0]),
        "beat_ids": beat_ids,
        "span_candidate_ids": span_ids,
        "duration_seconds": round(
            sum(float(item["duration_seconds"]) for item in candidates), 3
        ),
        "required_must_show_ids": required_must_show_ids,
        "covered_must_show_ids": sorted(
            {
                item
                for candidate in candidates
                for item in candidate.get("supports_must_show_ids", [])
                if item in required_must_show_ids
            }
        ),
        "required_thread_beat_ids": required_thread_beat_ids,
        "covered_thread_beat_ids": sorted(
            {
                item
                for candidate in candidates
                for item in candidate.get("supports_thread_beat_ids", [])
                if item in required_thread_beat_ids
            }
        ),
        "highlight_candidate_ids": sorted(
            {
                item
                for candidate in candidates
                for item in candidate.get("candidate_ids", [])
                if item in highlight_candidate_ids
            }
        ),
        "full_source_like_clip_count": sum(
            bool(item.get("full_source_like")) for item in candidates
        ),
    }


def _span_catalog_item(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "span_candidate_id": candidate["span_candidate_id"],
        "source_id": candidate["source_id"],
        "episode": candidate["episode"],
        "start": candidate["start"],
        "end": candidate["end"],
        "duration_seconds": candidate["duration_seconds"],
        "content_roles": candidate.get("content_roles", []),
        "semantic_content": [
            item.get("content_summary", "")
            for item in candidate.get("semantic_segment_refs", [])
            if isinstance(item, dict)
        ],
        "material_risks": candidate.get("material_risks", []),
    }


def _teaser_compatibility(
    teaser: dict[str, Any],
    body: dict[str, Any],
    candidate_lookup: dict[str, dict[str, Any]],
) -> bool:
    # Use the same merged-union calculation as materialization.  Pairwise
    # overlap summation over-counts a body option when several spans overlap
    # the same Teaser interval, producing false no-legal-partition failures.
    span_ids = [
        *teaser["span_candidate_ids"],
        *body["span_candidate_ids"],
    ]
    metrics = _exact_repeat_metrics_for_span_ids(span_ids, candidate_lookup)
    return (
        metrics["repeated_source_duration_seconds"]
        <= MAXIMUM_REPEAT_SECONDS + 0.001
    )


def _body_option_pair_reuse_seconds(
    a: dict[str, Any],
    b: dict[str, Any],
    candidate_lookup: dict[str, dict[str, Any]],
) -> float:
    """rule 21: 计算两个 body option 之间的原片复用秒数。
    同一 span_candidate_id 出现在两侧记入其整段时长；不同 span 但源片时间
    重叠也计入重叠秒数。dfs 把每对 (option_i, option_j) 的复用累加，全局
    ≤ MAXIMUM_REPEAT_SECONDS 才放行。"""
    a_ids = set(a["span_candidate_ids"])
    b_ids = set(b["span_candidate_ids"])
    total = 0.0
    for sid in a_ids & b_ids:
        span = candidate_lookup.get(sid)
        if span is None:
            continue
        total += float(span.get("duration_seconds", 0.0))
    for a_id in a_ids - b_ids:
        span_a = candidate_lookup.get(a_id)
        if span_a is None:
            continue
        for b_id in b_ids - a_ids:
            span_b = candidate_lookup.get(b_id)
            if span_b is None:
                continue
            overlap = _overlap_seconds(span_a, span_b)
            if overlap > 0.05:
                total += overlap
    return total


def _exact_repeat_metrics_for_span_ids(
    span_ids: list[str],
    candidate_lookup: dict[str, dict[str, Any]],
) -> dict[str, float]:
    """Match materialization's playback-minus-unique repeat calculation."""
    candidates = [
        candidate_lookup[span_id]
        for span_id in span_ids
        if span_id in candidate_lookup
    ]
    playback = round(
        sum(float(item["duration_seconds"]) for item in candidates), 3
    )
    unique = _merged_source_duration(
        [
            (
                str(item["source_id"]),
                float(item["start"]),
                float(item["end"]),
            )
            for item in candidates
        ]
    )
    repeated = round(max(0.0, playback - unique), 3)
    ratio = round(repeated / playback, 6) if playback > 0 else 0.0
    return {
        "playback_duration_seconds": playback,
        "unique_source_duration_seconds": unique,
        "repeated_source_duration_seconds": repeated,
        "repeat_ratio": ratio,
    }


def _partition_repeat_metrics(
    chosen: list[dict[str, Any]],
    teaser: dict[str, Any],
    candidate_lookup: dict[str, dict[str, Any]],
) -> dict[str, float]:
    return _exact_repeat_metrics_for_span_ids(
        [
            *teaser["span_candidate_ids"],
            *[
                span_id
                for option in chosen
                for span_id in option["span_candidate_ids"]
            ],
        ],
        candidate_lookup,
    )


def _partition_id(story_id: str, option_ids: list[str]) -> str:
    return stable_id(
        "plan-partition",
        {
            "compiler_version": COMPILER_VERSION,
            "story_id": story_id,
            "option_ids": option_ids,
        },
    )


def enumerate_legal_body_partitions(
    story_id: str,
    body_beats: list[dict[str, Any]],
    body_options: list[dict[str, Any]],
    teaser_options: list[dict[str, Any]],
    candidate_lookup: dict[str, dict[str, Any]],
    *,
    duration_contract: dict[str, float] | None = None,
    preferred_minimum_clip_count: int = 0,
    preferred_median_range: tuple[float, float] = (
        PREFERRED_MEDIAN_CLIP_SECONDS_RANGE
    ),
    max_full_source_like_clips: int = (
        MAXIMUM_FULL_SOURCE_LIKE_CLIPS_PER_STORY
    ),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Enumerate every legal contiguous body partition.

    A partition is a contiguous ordered list of body options such that:

    * every body beat is covered exactly once, in approved order
    * exact total repeat across Teaser + Body is at most 20 seconds and 8%
    * the sum of ``full_source_like_clip_count`` does not exceed
      ``max_full_source_like_clips`` (defaults to the strict story-level
      cap but callers can pass the effective narrow-pool cap)
    * at least one legal teaser option is compatible with every option in the
      partition (via the precomputed ``compatible_teaser_option_ids``)

    Returns ``(partitions, diagnostics)``. Each partition record carries
    ``partition_id`` (stable), ``body_option_ids``, ``segment_count``,
    ``total_duration_seconds``, ``clip_count``, ``sources_touched``,
    ``full_source_like_clip_count``, ``median_clip_duration_seconds``,
    ``compatible_teaser_option_ids``, ``constraints_met`` and
    ``constraints_violated`` (informational — none of the violations are hard;
    hard violations would prevent enumeration entirely).
    """
    n = len(body_beats)
    diagnostics: dict[str, Any] = {
        "body_beat_count": n,
        "body_option_count": len(body_options),
        "partitions_enumerated": 0,
        "partitions_kept": 0,
        "partitions_dropped_no_compatible_teaser": 0,
        "branches_pruned_repeat_seconds": 0,
        "branches_pruned_full_source_bound": 0,
        "branches_pruned_teaser_compatibility": 0,
        "partitions_dropped_repeat_ratio": 0,
        "partitions_dropped_by_cap": 0,
        "search_nodes_used": 0,
        "search_nodes_exhausted": False,
    }
    if n == 0 or not body_options:
        return [], diagnostics
    beat_index = {beat["id"]: index for index, beat in enumerate(body_beats)}
    expected_by_start: dict[int, list[str]] = {
        index: [item["id"] for item in body_beats[index:]]
        for index in range(n)
    }
    options_by_start: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for option in body_options:
        beat_ids = option["beat_ids"]
        if not beat_ids:
            continue
        start = beat_index.get(beat_ids[0])
        if start is None:
            continue
        end = start + len(beat_ids)
        if end > n:
            continue
        if beat_ids != expected_by_start[start][: len(beat_ids)]:
            continue
        options_by_start[start].append(option)

    teaser_ids_all = {item["option_id"] for item in teaser_options}
    teaser_by_id = {
        item["option_id"]: item for item in teaser_options
    }
    partitions_raw: list[list[dict[str, Any]]] = []
    repeat_seconds_at_prune: list[float] = []
    search_nodes = 0
    exhausted = False

    def dfs(
        pos: int,
        chosen: list[dict[str, Any]],
        used_full: int,
        compat: set[str],
    ) -> None:
        nonlocal search_nodes, exhausted
        if exhausted:
            return
        search_nodes += 1
        if search_nodes > MAX_PARTITION_SEARCH_NODES:
            exhausted = True
            return
        if pos == n:
            partitions_raw.append(list(chosen))
            return
        if len(partitions_raw) >= MAX_LEGAL_BODY_PARTITIONS:
            return
        for option in options_by_start.get(pos, []):
            option_full = int(option["full_source_like_clip_count"])
            new_full = used_full + option_full
            if new_full > max_full_source_like_clips:
                diagnostics["branches_pruned_full_source_bound"] += 1
                continue
            option_teasers = set(
                option.get("compatible_teaser_option_ids", [])
            )
            next_compat = (
                compat & option_teasers
                if chosen
                else option_teasers or (
                    teaser_ids_all
                    if not option_teasers and not teaser_options
                    else option_teasers
                )
            )
            if teaser_options and not next_compat:
                diagnostics["branches_pruned_teaser_compatibility"] += 1
                continue
            next_chosen = [*chosen, option]
            # rule 21: 不再把 Body 预算固定切成 10 秒。对每个仍兼容的
            # Teaser 精确计算当前 Teaser+Body 的 playback-unique 重复秒数。
            # 重复秒数随新增 Clip 单调不减，因此 >20s 可安全提前剪枝；
            # 8% 比率可能因后续新增不重复素材下降，只在完整 Partition 判定。
            if teaser_options:
                next_repeat_metrics = {
                    teaser_id: _partition_repeat_metrics(
                        next_chosen,
                        teaser_by_id[teaser_id],
                        candidate_lookup,
                    )
                    for teaser_id in next_compat
                }
                next_compat = {
                    teaser_id
                    for teaser_id, metrics in next_repeat_metrics.items()
                    if metrics["repeated_source_duration_seconds"]
                    <= MAXIMUM_REPEAT_SECONDS + 0.001
                }
                if not next_compat:
                    repeat_seconds_at_prune.append(
                        min(
                            item["repeated_source_duration_seconds"]
                            for item in next_repeat_metrics.values()
                        )
                    )
                    diagnostics["branches_pruned_repeat_seconds"] += 1
                    continue
            end = pos + len(option["beat_ids"])
            chosen.append(option)
            dfs(
                end,
                chosen,
                new_full,
                next_compat,
            )
            chosen.pop()
            if len(partitions_raw) >= MAX_LEGAL_BODY_PARTITIONS:
                return

    dfs(0, [], 0, set())
    diagnostics["partitions_enumerated"] = len(partitions_raw)
    diagnostics["search_nodes_used"] = search_nodes
    diagnostics["search_nodes_exhausted"] = exhausted
    diagnostics["minimum_repeat_seconds_at_prune"] = (
        min(repeat_seconds_at_prune)
        if repeat_seconds_at_prune
        else None
    )

    minimum_total = float(
        (duration_contract or {}).get("minimum_seconds", 0.0)
    )
    preferred_target = float(
        (duration_contract or {}).get(
            "preferred_target_seconds", minimum_total
        )
    )
    preferred_min = float(
        (duration_contract or {}).get(
            "preferred_minimum_seconds", minimum_total
        )
    )
    maximum_total = float(
        (duration_contract or {}).get("maximum_seconds", float("inf"))
    )
    median_min, median_max = preferred_median_range

    kept: list[dict[str, Any]] = []
    complete_repeat_seconds: list[float] = []
    complete_repeat_ratios: list[float] = []
    complete_playback_seconds: list[float] = []
    for chosen in partitions_raw:
        if teaser_options:
            compat_sets = [
                set(option.get("compatible_teaser_option_ids", []))
                for option in chosen
            ]
            compatible_teasers = (
                set.intersection(*compat_sets) if compat_sets else set()
            )
        else:
            compatible_teasers = set()
        if teaser_options and not compatible_teasers:
            diagnostics["partitions_dropped_no_compatible_teaser"] += 1
            continue
        repeat_metrics_by_teaser = {
            teaser_id: _partition_repeat_metrics(
                chosen,
                teaser_by_id[teaser_id],
                candidate_lookup,
            )
            for teaser_id in compatible_teasers
        }
        complete_repeat_seconds.extend(
            item["repeated_source_duration_seconds"]
            for item in repeat_metrics_by_teaser.values()
        )
        complete_repeat_ratios.extend(
            item["repeat_ratio"]
            for item in repeat_metrics_by_teaser.values()
        )
        complete_playback_seconds.extend(
            item["playback_duration_seconds"]
            for item in repeat_metrics_by_teaser.values()
        )
        compatible_teasers = {
            teaser_id
            for teaser_id in compatible_teasers
            if repeat_metrics_by_teaser[teaser_id][
                "repeated_source_duration_seconds"
            ]
            <= MAXIMUM_REPEAT_SECONDS + 0.001
            and repeat_metrics_by_teaser[teaser_id]["repeat_ratio"]
            <= 0.08 + 0.000001
        }
        if teaser_options and not compatible_teasers:
            diagnostics["partitions_dropped_repeat_ratio"] += 1
            continue
        option_ids = [option["option_id"] for option in chosen]
        total_duration = round(
            sum(float(option["duration_seconds"]) for option in chosen), 3
        )
        # Plan-level total = body partition + the longest compatible teaser.
        # The materialize stage checks ``playback_duration_seconds`` (which
        # includes the teaser Block) against ``minimum_seconds``, so the
        # partition-level constraint must use the same measure or it will
        # spuriously block partitions that materialize would accept.
        compatible_teaser_durations = [
            float(
                teaser_by_id.get(tid, {}).get("duration_seconds", 0.0)
            )
            for tid in compatible_teasers
        ]
        best_teaser_duration = (
            max(compatible_teaser_durations)
            if compatible_teaser_durations
            else 0.0
        )
        plan_ceiling_duration = round(
            total_duration + best_teaser_duration, 3
        )
        clip_durations = [
            float(candidate_lookup[span_id]["duration_seconds"])
            for option in chosen
            for span_id in option["span_candidate_ids"]
            if span_id in candidate_lookup
        ]
        clip_count = sum(
            len(option["span_candidate_ids"]) for option in chosen
        )
        median_clip = (
            round(
                sorted(clip_durations)[len(clip_durations) // 2],
                3,
            )
            if clip_durations
            else 0.0
        )
        sources_touched = sorted(
            {
                candidate_lookup[span_id]["source_id"]
                for option in chosen
                for span_id in option["span_candidate_ids"]
                if span_id in candidate_lookup
            }
        )
        full_source_like = sum(
            int(option["full_source_like_clip_count"]) for option in chosen
        )
        constraints_met: list[str] = []
        constraints_violated: list[str] = []
        if plan_ceiling_duration >= minimum_total:
            constraints_met.append("duration_meets_minimum")
        else:
            constraints_violated.append("duration_below_minimum")
        if plan_ceiling_duration >= preferred_min:
            constraints_met.append("duration_meets_preferred_minimum")
        else:
            constraints_violated.append("duration_below_preferred_minimum")
        if plan_ceiling_duration <= maximum_total:
            constraints_met.append("duration_within_maximum")
        else:
            constraints_violated.append("duration_above_maximum")
        if full_source_like <= max_full_source_like_clips:
            constraints_met.append("full_source_like_within_cap")
        else:
            constraints_violated.append("full_source_like_over_cap")
        if clip_count >= max(1, preferred_minimum_clip_count):
            constraints_met.append("clip_count_meets_preferred_minimum")
        else:
            constraints_violated.append("clip_count_below_preferred_minimum")
        if not clip_durations or median_min <= median_clip <= median_max:
            constraints_met.append("median_clip_within_preferred_range")
        else:
            constraints_violated.append(
                "median_clip_outside_preferred_range"
            )
        distance_from_preferred_target = (
            round(abs(plan_ceiling_duration - preferred_target), 3)
            if preferred_target > 0
            else 0.0
        )
        record = {
            "partition_id": _partition_id(story_id, option_ids),
            "body_option_ids": option_ids,
            "segment_count": len(chosen),
            "beat_partition": [option["beat_ids"] for option in chosen],
            "total_duration_seconds": total_duration,
            "plan_ceiling_duration_seconds": plan_ceiling_duration,
            "best_compatible_teaser_duration_seconds": (
                best_teaser_duration
            ),
            "clip_count": clip_count,
            "median_clip_duration_seconds": median_clip,
            "sources_touched": sources_touched,
            "source_count": len(sources_touched),
            "full_source_like_clip_count": full_source_like,
            "compatible_teaser_option_ids": sorted(compatible_teasers),
            "repeat_metrics_by_teaser_option_id": {
                teaser_id: repeat_metrics_by_teaser[teaser_id]
                for teaser_id in sorted(compatible_teasers)
            },
            "distance_from_preferred_target_seconds": (
                distance_from_preferred_target
            ),
            "constraints_met": constraints_met,
            "constraints_violated": constraints_violated,
        }
        kept.append(record)

    dropped_by_cap = max(0, len(kept) - MAX_LEGAL_BODY_PARTITIONS)
    if dropped_by_cap:
        kept.sort(
            key=lambda item: (
                "duration_meets_minimum"
                not in item["constraints_met"],
                item["distance_from_preferred_target_seconds"],
                -item["source_count"],
                item["partition_id"],
            )
        )
        kept = kept[:MAX_LEGAL_BODY_PARTITIONS]
    diagnostics["partitions_dropped_by_cap"] = dropped_by_cap
    kept.sort(
        key=lambda item: (
            "duration_meets_minimum" not in item["constraints_met"],
            item["distance_from_preferred_target_seconds"],
            -item["source_count"],
            item["partition_id"],
        )
    )
    diagnostics["partitions_kept"] = len(kept)
    diagnostics["minimum_complete_repeat_seconds"] = (
        min(complete_repeat_seconds) if complete_repeat_seconds else None
    )
    diagnostics["minimum_complete_repeat_ratio"] = (
        min(complete_repeat_ratios) if complete_repeat_ratios else None
    )
    diagnostics["maximum_complete_playback_seconds"] = (
        max(complete_playback_seconds)
        if complete_playback_seconds
        else 0.0
    )
    return kept, diagnostics


def compile_legal_options(
    script: dict[str, Any],
    bundle: dict[str, Any],
    *,
    evidence_packet: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile teaser and contiguous body beat options from immutable Spans."""
    story_id = script["story_id"]
    if bundle.get("story_id") != story_id:
        raise ValueError("Story Script and Span Bundle identities do not match")
    beats = [
        beat
        for beat in script.get("beats", [])
        if isinstance(beat, dict)
        and (
            beat.get("must_have")
            # rule 5: teaser_intent 恒为强制骨架，不管模型有没有勾
            # must_have。Qwen 有时会漏勾，这里兜底。
            or beat.get("role") == "teaser_intent"
        )
    ]
    if not beats or beats[0].get("role") != "teaser_intent":
        raise ValueError("approved Story Script has no leading teaser_intent")
    candidates = [
        item
        for item in bundle.get("candidates", [])
        if isinstance(item, dict)
        and float(item.get("duration_seconds", 0)) > 0
    ]
    fact_events = _fact_events_from_packet(evidence_packet)
    leak_events_by_beat = _leak_events_by_beat(beats, fact_events)
    teaser_beat = beats[0]
    teaser_contract = script.get("teaser_contract", {})
    primary_highlight_id = teaser_contract.get(
        "primary_highlight_candidate_id"
    )
    highlight_candidate_ids = (
        {primary_highlight_id}
        if isinstance(primary_highlight_id, str)
        else set()
    )
    required_teaser_must_show_ids = {
        item["id"]
        for item in teaser_beat.get("must_show", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    teaser_leak_events = leak_events_by_beat.get(teaser_beat["id"], set())
    teaser_leak_reject_count = 0
    atomic_teaser_candidates: list[dict[str, Any]] = []
    for item in candidates:
        if item.get("teaser_atomic") is not True:
            continue
        if (
            item.get("teaser_atomic_owner_candidate_id")
            != primary_highlight_id
        ):
            continue
        if teaser_beat["id"] not in item.get("supports_beat_ids", []):
            continue
        if not required_teaser_must_show_ids.issubset(
            set(item.get("supports_must_show_ids", []))
        ):
            continue
        if primary_highlight_id not in item.get("candidate_ids", []):
            continue
        if teaser_leak_events and (
            teaser_leak_events & set(item.get("event_ids", []))
        ):
            teaser_leak_reject_count += 1
            continue
        atomic_teaser_candidates.append(item)
    unbounded_teaser_sets = [[item] for item in atomic_teaser_candidates]
    teaser_sets = [
        [item]
        for item in atomic_teaser_candidates
        if float(item["duration_seconds"])
        <= TEASER_MAXIMUM_SECONDS + 0.001
    ]
    teaser_options = [
        _option(
            story_id=story_id,
            kind="teaser",
            beats=[teaser_beat],
            candidates=selection,
            highlight_candidate_ids=highlight_candidate_ids,
        )
        for selection in teaser_sets
    ]
    minimum_teaser_selection = (
        min(
            unbounded_teaser_sets,
            key=lambda selection: (
                sum(
                    float(item["duration_seconds"])
                    for item in selection
                ),
                tuple(
                    item["span_candidate_id"] for item in selection
                ),
            ),
        )
        if unbounded_teaser_sets
        else []
    )
    minimum_teaser_duration = round(
        sum(
            float(item["duration_seconds"])
            for item in minimum_teaser_selection
        ),
        3,
    )
    teaser_excess_duration = round(
        max(0.0, minimum_teaser_duration - TEASER_MAXIMUM_SECONDS),
        3,
    )
    script_teaser_diagnostics = script.get("feasibility", {}).get(
        "teaser_diagnostics", {}
    )
    script_teaser_failure_codes = list(
        script_teaser_diagnostics.get("failure_codes", [])
    )
    repair_route = (
        "story_script"
        if script_teaser_failure_codes
        or float(
            script_teaser_diagnostics.get(
                "candidate_duration_seconds", 0
            )
        )
        > TEASER_MAXIMUM_SECONDS + 0.001
        else "span_compiler"
    )
    priority_recompile_candidates = [
        item
        for item in minimum_teaser_selection
        if highlight_candidate_ids.intersection(
            item.get("candidate_ids", [])
        )
    ]
    if (
        not priority_recompile_candidates
        and minimum_teaser_selection
    ):
        priority_recompile_candidates = [
            max(
                minimum_teaser_selection,
                key=lambda item: (
                    float(item["duration_seconds"]),
                    item["span_candidate_id"],
                ),
            )
        ]
    candidate_recompile_hints = [
        {
            "span_candidate_id": item["span_candidate_id"],
            "current_duration_seconds": round(
                float(item["duration_seconds"]), 3
            ),
            "maximum_duration_if_others_fixed_seconds": round(
                max(
                    0.0,
                    TEASER_MAXIMUM_SECONDS
                    - (
                        minimum_teaser_duration
                        - float(item["duration_seconds"])
                    ),
                ),
                3,
            ),
            "minimum_required_reduction_seconds": teaser_excess_duration,
            "candidate_origin_ids": sorted(
                highlight_candidate_ids.intersection(
                    item.get("candidate_ids", [])
                )
            ),
        }
        for item in minimum_teaser_selection
    ]

    body_beats = beats[1:]
    scope_expansion_thread_beat_ids = {
        thread_beat_id
        for expansion in script.get("auto_scope_expansion", [])
        if isinstance(expansion, dict)
        for field in ("added_thread_beat_ids", "attached_thread_beat_ids")
        for thread_beat_id in expansion.get(field, [])
        if isinstance(thread_beat_id, str)
    }
    body_options: list[dict[str, Any]] = []
    missing_body_beat_ids: list[str] = []
    per_group_diagnostics: list[dict[str, Any]] = []
    for start in range(len(body_beats)):
        single_beat_has_option = False
        for end in range(start + 1, len(body_beats) + 1):
            group = body_beats[start:end]
            selections, group_diag = _covering_candidate_sets(
                candidates,
                group,
                highlight_candidate_ids=set(),
                maximum_duration_seconds=None,
                leak_events_by_beat=leak_events_by_beat,
                scope_optional_thread_beat_ids=(
                    scope_expansion_thread_beat_ids
                ),
            )
            per_group_diagnostics.append(
                {
                    "beat_ids": [beat["id"] for beat in group],
                    **group_diag,
                }
            )
            if end == start + 1 and selections:
                single_beat_has_option = True
            body_options.extend(
                _option(
                    story_id=story_id,
                    kind="body",
                    beats=group,
                    candidates=selection,
                    highlight_candidate_ids=set(),
                )
                for selection in selections
            )
        if not single_beat_has_option:
            missing_body_beat_ids.append(body_beats[start]["id"])
    by_option_id = {item["option_id"]: item for item in body_options}
    body_options = list(by_option_id.values())
    body_options_pruned_over_full_clip_limit = sum(
        1
        for item in body_options
        if item["full_source_like_clip_count"]
        > MAXIMUM_FULL_SOURCE_LIKE_CLIPS_PER_OPTION
    )
    body_options = [
        item
        for item in body_options
        if item["full_source_like_clip_count"]
        <= MAXIMUM_FULL_SOURCE_LIKE_CLIPS_PER_OPTION
    ]
    body_options.sort(
        key=lambda item: (
            body_beats.index(
                next(
                    beat
                    for beat in body_beats
                    if beat["id"] == item["beat_ids"][0]
                )
            ),
            -len(item["beat_ids"]),
            item["full_source_like_clip_count"],
            item["duration_seconds"],
            item["option_id"],
        )
    )
    candidate_lookup = {
        item["span_candidate_id"]: item for item in candidates
    }
    for body in body_options:
        body["compatible_teaser_option_ids"] = [
            teaser["option_id"]
            for teaser in teaser_options
            if _teaser_compatibility(teaser, body, candidate_lookup)
        ]
    if teaser_options:
        body_options = [
            item
            for item in body_options
            if item["compatible_teaser_option_ids"]
        ]
    valid_body_beats = {
        beat_id for item in body_options for beat_id in item["beat_ids"]
    }
    missing_body_beat_ids = sorted(
        set(missing_body_beat_ids)
        | {
            beat["id"]
            for beat in body_beats
            if beat["id"] not in valid_body_beats
        }
    )

    bundle_candidate_unique_duration_seconds = (
        _bundle_available_unique_duration(candidates)
    )
    available_candidate_unique_duration_seconds = (
        _functional_option_pool_unique_duration(
            candidates, teaser_options, body_options
        )
    )
    effective_max_full_clips = (
        effective_max_full_source_like_clips_per_story(
            available_candidate_unique_duration_seconds
        )
    )
    narrow_pool_relaxation_applied = (
        effective_max_full_clips > MAXIMUM_FULL_SOURCE_LIKE_CLIPS_PER_STORY
    )
    minimum_full_source_like_clips = _minimum_full_source_like_clips(
        body_beats, body_options
    )
    full_source_like_unreachable = (
        missing_body_beat_ids == []
        and minimum_full_source_like_clips > effective_max_full_clips
    )

    target_duration = script.get("target_duration") or {}
    minimum_total_duration_seconds = float(
        target_duration.get("minimum_seconds", 0.0)
    )
    preferred_minimum_seconds = float(
        target_duration.get(
            "preferred_minimum_seconds", minimum_total_duration_seconds
        )
    )
    preferred_minimum_clip_count = (
        max(1, int(minimum_total_duration_seconds // 40.0))
        if minimum_total_duration_seconds > 0
        else 1
    )
    if teaser_options and body_options and not missing_body_beat_ids:
        legal_body_partitions, partition_diagnostics = (
            enumerate_legal_body_partitions(
                story_id,
                body_beats,
                body_options,
                teaser_options,
                candidate_lookup,
                duration_contract=target_duration,
                preferred_minimum_clip_count=preferred_minimum_clip_count,
                max_full_source_like_clips=effective_max_full_clips,
            )
        )
    else:
        legal_body_partitions = []
        partition_diagnostics = {
            "body_beat_count": len(body_beats),
            "body_option_count": len(body_options),
            "partitions_enumerated": 0,
            "partitions_kept": 0,
            "partitions_dropped_no_compatible_teaser": 0,
            "branches_pruned_repeat_seconds": 0,
            "branches_pruned_full_source_bound": 0,
            "branches_pruned_teaser_compatibility": 0,
            "partitions_dropped_repeat_ratio": 0,
            "partitions_dropped_by_cap": 0,
            "search_nodes_used": 0,
            "search_nodes_exhausted": False,
            "minimum_repeat_seconds_at_prune": None,
            "minimum_complete_repeat_seconds": None,
            "minimum_complete_repeat_ratio": None,
            "maximum_complete_playback_seconds": 0.0,
        }
    no_legal_body_partition = (
        teaser_options
        and body_options
        and not missing_body_beat_ids
        and not legal_body_partitions
    )
    partition_repeat_blocked = bool(
        no_legal_body_partition
        and (
            partition_diagnostics.get(
                "branches_pruned_repeat_seconds", 0
            )
            or partition_diagnostics.get(
                "partitions_dropped_repeat_ratio", 0
            )
        )
    )
    partition_full_source_blocked = bool(
        no_legal_body_partition
        and partition_diagnostics.get(
            "branches_pruned_full_source_bound", 0
        )
    )
    partition_teaser_compatibility_blocked = bool(
        no_legal_body_partition
        and partition_diagnostics.get(
            "branches_pruned_teaser_compatibility", 0
        )
    )
    partitions_meeting_minimum = [
        partition
        for partition in legal_body_partitions
        if "duration_meets_minimum" in partition["constraints_met"]
    ]
    no_partition_meets_minimum = bool(
        legal_body_partitions
        and minimum_total_duration_seconds > 0
        and not partitions_meeting_minimum
    )
    editorial_surplus_seconds = round(
        available_candidate_unique_duration_seconds
        - minimum_total_duration_seconds,
        3,
    )
    editorial_surplus_ratio = (
        round(editorial_surplus_seconds / minimum_total_duration_seconds, 3)
        if minimum_total_duration_seconds > 0
        else 0.0
    )
    insufficient_editorial_surplus = (
        minimum_total_duration_seconds > 0
        and editorial_surplus_ratio < MINIMUM_EDITORIAL_SURPLUS_RATIO
    )
    option_search_incomplete = bool(
        no_legal_body_partition
        and (
            partition_diagnostics.get("search_nodes_exhausted")
            or partition_diagnostics.get("partitions_dropped_by_cap", 0)
            or any(
                item.get("search_nodes_exhausted")
                or item.get("covering_sets_dropped_by_option_cap", 0)
                or item.get("candidates_dropped_by_pareto", 0)
                for item in per_group_diagnostics
            )
        )
    )

    failure_codes: list[str] = []
    if not teaser_options:
        failure_codes.append("no_legal_teaser_option")
    if missing_body_beat_ids:
        failure_codes.append("no_legal_block_option")
    if full_source_like_unreachable:
        failure_codes.append("no_legal_full_source_like_bound")
    if insufficient_editorial_surplus:
        failure_codes.append("insufficient_editorial_surplus")
    if no_legal_body_partition:
        failure_codes.append("no_legal_body_partition")
    if option_search_incomplete:
        failure_codes.append("option_search_incomplete")
    if partition_repeat_blocked:
        failure_codes.append("no_partition_within_repeat_budget")
    if partition_full_source_blocked:
        failure_codes.append("no_partition_within_full_source_bound")
    if partition_teaser_compatibility_blocked:
        failure_codes.append("no_partition_with_compatible_teaser")
    if (
        no_legal_body_partition
        and not (
            partition_repeat_blocked
            or partition_full_source_blocked
            or partition_teaser_compatibility_blocked
        )
    ):
        failure_codes.append("no_partition_covers_all_body_beats")
    if no_partition_meets_minimum:
        failure_codes.append("no_partition_meets_minimum_duration")

    script_wants_script_repair = bool(script_teaser_failure_codes) or (
        float(
            script_teaser_diagnostics.get("candidate_duration_seconds", 0)
        )
        > TEASER_MAXIMUM_SECONDS + 0.001
    )
    if not failure_codes:
        pass
    elif script_wants_script_repair:
        repair_route = "story_script"
    elif option_search_incomplete:
        repair_route = "option_search"
    elif insufficient_editorial_surplus or no_partition_meets_minimum:
        repair_route = "story_scope"
    else:
        repair_route = "span_compiler"

    repair_route_by_code = dict(
        _ep.get("repair_route_by_code") or {}
    ) if _ep.get("repair_route_by_code") else {
        "no_legal_teaser_option": repair_route,
        "no_legal_block_option": "span_compiler",
        "no_legal_full_source_like_bound": "span_compiler",
        "insufficient_editorial_surplus": "story_scope",
        "no_legal_body_partition": repair_route,
        "option_search_incomplete": "option_search",
        "no_partition_within_repeat_budget": "story_plan",
        "no_partition_within_full_source_bound": "span_compiler",
        "no_partition_with_compatible_teaser": "story_plan",
        "no_partition_covers_all_body_beats": "span_compiler",
        "no_partition_meets_minimum_duration": "story_scope",
    }
    # Override entries that depend on the local `repair_route` variable.
    repair_route_by_code["no_legal_teaser_option"] = repair_route
    repair_route_by_code["no_legal_body_partition"] = repair_route
    repair_routes = [
        {
            "code": code,
            "return_to_stage": repair_route_by_code.get(
                code, repair_route
            ),
            "reason": (
                "扩大并保留 source/time 兼容类后重新搜索。"
                if code == "option_search_incomplete"
                else "按失败代码回到最小必要阶段修复，不改写人工 Approval。"
            ),
        }
        for code in failure_codes
    ]

    result = {
        "schema_version": "1.0",
        "compiler_version": COMPILER_VERSION,
        "story_id": story_id,
        "production_slot": script["portfolio"]["production_slot"],
        "required_beat_ids": [beat["id"] for beat in beats],
        "scope_expansion_thread_beat_ids": sorted(
            scope_expansion_thread_beat_ids
        ),
        "teaser_beat_id": teaser_beat["id"],
        "highlight_candidate_ids": sorted(highlight_candidate_ids),
        "legal_teaser_options": teaser_options,
        "legal_block_options": body_options,
        "legal_body_partitions": legal_body_partitions,
        "span_catalog": [
            _span_catalog_item(candidate_lookup[span_candidate_id])
            for span_candidate_id in sorted(
                {
                    span_candidate_id
                    for option in [*teaser_options, *body_options]
                    for span_candidate_id in option[
                        "span_candidate_ids"
                    ]
                }
            )
        ],
        "preflight": {
            "status": "blocked" if failure_codes else "ready",
            "failure_codes": failure_codes,
            "missing_body_beat_ids": missing_body_beat_ids,
            "repair_route": repair_route,
            "repair_routes": repair_routes,
            "editorial_surplus_diagnostics": {
                "available_candidate_unique_duration_seconds": (
                    available_candidate_unique_duration_seconds
                ),
                "functional_option_pool_unique_duration_seconds": (
                    available_candidate_unique_duration_seconds
                ),
                "bundle_candidate_unique_duration_seconds": (
                    bundle_candidate_unique_duration_seconds
                ),
                "non_option_candidate_duration_excluded_seconds": round(
                    max(
                        0.0,
                        bundle_candidate_unique_duration_seconds
                        - available_candidate_unique_duration_seconds,
                    ),
                    3,
                ),
                "minimum_total_duration_seconds": (
                    minimum_total_duration_seconds
                ),
                "preferred_minimum_seconds": preferred_minimum_seconds,
                "editorial_surplus_seconds": editorial_surplus_seconds,
                "editorial_surplus_ratio": editorial_surplus_ratio,
                "minimum_editorial_surplus_ratio": (
                    MINIMUM_EDITORIAL_SURPLUS_RATIO
                ),
                "insufficient_editorial_surplus": (
                    insufficient_editorial_surplus
                ),
            },
            "full_source_like_diagnostics": {
                "maximum_full_source_like_clips_per_option": (
                    MAXIMUM_FULL_SOURCE_LIKE_CLIPS_PER_OPTION
                ),
                "maximum_full_source_like_clips_per_story": (
                    MAXIMUM_FULL_SOURCE_LIKE_CLIPS_PER_STORY
                ),
                "effective_maximum_full_source_like_clips_per_story": (
                    effective_max_full_clips
                ),
                "narrow_pool_relaxation_applied": (
                    narrow_pool_relaxation_applied
                ),
                "narrow_pool_available_threshold_seconds": (
                    NARROW_POOL_AVAILABLE_THRESHOLD_SECONDS
                ),
                "narrow_pool_max_full_source_like_clips_per_story": (
                    NARROW_POOL_MAX_FULL_SOURCE_LIKE_CLIPS_PER_STORY
                ),
                "minimum_full_source_like_clips_required": (
                    None
                    if minimum_full_source_like_clips == float("inf")
                    else minimum_full_source_like_clips
                ),
                "body_options_pruned_over_full_clip_limit": (
                    body_options_pruned_over_full_clip_limit
                ),
                "unreachable_within_full_clip_cap": (
                    full_source_like_unreachable
                ),
            },
            "body_options_diagnostics": {
                "scope_expansion_thread_beat_ids": sorted(
                    scope_expansion_thread_beat_ids
                ),
                "beat_group_count": len(per_group_diagnostics),
                "beat_groups_at_option_cap": [
                    {
                        "beat_ids": item["beat_ids"],
                        "covering_sets_found": item["covering_sets_found"],
                        "covering_sets_dropped_by_option_cap": item[
                            "covering_sets_dropped_by_option_cap"
                        ],
                    }
                    for item in per_group_diagnostics
                    if item["covering_sets_dropped_by_option_cap"] > 0
                ],
                "beat_groups_search_exhausted": [
                    item["beat_ids"]
                    for item in per_group_diagnostics
                    if item["search_nodes_exhausted"]
                ],
                "total_candidates_dropped_by_pareto": sum(
                    item["candidates_dropped_by_pareto"]
                    for item in per_group_diagnostics
                ),
                "total_covering_sets_found": sum(
                    item["covering_sets_found"]
                    for item in per_group_diagnostics
                ),
                "total_covering_sets_dropped_by_option_cap": sum(
                    item["covering_sets_dropped_by_option_cap"]
                    for item in per_group_diagnostics
                ),
                "maximum_options_per_beat_group": MAX_OPTIONS_PER_BEAT_GROUP,
                "maximum_nondominated_candidates": (
                    MAX_NONDOMINATED_CANDIDATES
                ),
            },
            "body_partition_diagnostics": {
                **partition_diagnostics,
                "partitions_meeting_minimum_duration": len(
                    partitions_meeting_minimum
                ),
                "maximum_partition_total_duration_seconds": (
                    max(
                        (
                            item["total_duration_seconds"]
                            for item in legal_body_partitions
                        ),
                        default=0.0,
                    )
                ),
                "maximum_plan_ceiling_duration_seconds": (
                    max(
                        (
                            item.get("plan_ceiling_duration_seconds", 0.0)
                            for item in legal_body_partitions
                        ),
                        default=0.0,
                    )
                ),
                "no_partition_meets_minimum_duration": (
                    no_partition_meets_minimum
                ),
                "maximum_legal_partitions": MAX_LEGAL_BODY_PARTITIONS,
                "maximum_partition_search_nodes": (
                    MAX_PARTITION_SEARCH_NODES
                ),
            },
            "teaser_diagnostics": {
                "mode": "single_highlight",
                "maximum_duration_seconds": TEASER_MAXIMUM_SECONDS,
                "required_must_show_ids": sorted(
                    {
                        item["id"]
                        for item in teaser_beat.get("must_show", [])
                        if isinstance(item, dict)
                        and isinstance(item.get("id"), str)
                    }
                ),
                "required_highlight_candidate_ids": sorted(
                    highlight_candidate_ids
                ),
                "primary_highlight_candidate_id": primary_highlight_id,
                "script_failure_codes": script_teaser_failure_codes,
                "fact_leak_rejected_atomic_candidate_count": (
                    teaser_leak_reject_count
                ),
                "must_not_reveal_fact_ids": sorted(
                    teaser_beat.get("must_not_reveal_fact_ids", [])
                ),
                "minimum_covering_duration_seconds": (
                    minimum_teaser_duration
                    if unbounded_teaser_sets
                    else None
                ),
                "minimum_covering_span_candidate_ids": (
                    sorted(
                        item["span_candidate_id"]
                        for item in minimum_teaser_selection
                    )
                    if unbounded_teaser_sets
                    else []
                ),
                "excess_duration_seconds": (
                    teaser_excess_duration
                    if unbounded_teaser_sets
                    else None
                ),
                "priority_recompile_span_candidate_ids": sorted(
                    item["span_candidate_id"]
                    for item in priority_recompile_candidates
                ) if not teaser_options else [],
                "recommended_origin_candidate_ids": sorted(
                    {
                        candidate_id
                        for item in priority_recompile_candidates
                        for candidate_id in highlight_candidate_ids.intersection(
                            item.get("candidate_ids", [])
                        )
                    }
                ) if not teaser_options else [],
                "recommended_recompile_profile": (
                    "highlight_atomic"
                    if unbounded_teaser_sets and not teaser_options
                    else None
                ),
                "candidate_recompile_hints": (
                    candidate_recompile_hints
                    if not teaser_options
                    else []
                ),
            },
        },
    }
    result["legal_options_sha256"] = json_sha256(
        {
            "compiler_version": COMPILER_VERSION,
            "story_id": story_id,
            "legal_teaser_options": teaser_options,
            "legal_block_options": body_options,
            "legal_body_partitions": legal_body_partitions,
            "span_catalog": result["span_catalog"],
        }
    )
    return result


def dynamic_selection_schema(legal_options: dict[str, Any]) -> dict[str, Any]:
    """Return the per-Story strict schema sent to the model provider.

    The shape switches body selection to a single ``body_partition_id`` enum
    over the pre-enumerated ``legal_body_partitions``, plus a
    ``body_block_orientations`` array whose length must match the chosen
    partition's ``segment_count``.
    """
    teaser_ids = [
        item["option_id"]
        for item in legal_options["legal_teaser_options"]
    ]
    partition_ids = [
        item["partition_id"]
        for item in legal_options.get("legal_body_partitions", [])
    ]
    if not teaser_ids or not partition_ids:
        raise ValueError(
            "cannot build a response schema without legal teaser options "
            "and at least one legal body partition"
        )
    orientation_block = {
        "type": "object",
        "properties": {
            "temporal_relation_from_previous": {
                "type": "string",
                "enum": [
                    "continuation",
                    "flashback_context",
                    "preview_future",
                    "return_to_mainline",
                    "parallel",
                ],
            },
            "orientation_required": {"type": "boolean"},
            "orientation_strategy": {
                "type": "string",
                "enum": [
                    "dialogue_anchor",
                    "visual_anchor",
                    "title_card",
                    "none",
                ],
            },
            "selection_reason": {"type": "string", "minLength": 1},
        },
        "required": [
            "temporal_relation_from_previous",
            "orientation_required",
            "orientation_strategy",
            "selection_reason",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "const": "3.0"},
            "story_id": {
                "type": "string",
                "const": legal_options["story_id"],
            },
            "production_slot": {
                "type": "integer",
                "const": legal_options["production_slot"],
            },
            "teaser": {
                "type": "object",
                "properties": {
                    "option_id": {"type": "string", "enum": teaser_ids},
                    "selection_reason": {
                        "type": "string",
                        "minLength": 1,
                    },
                },
                "required": ["option_id", "selection_reason"],
                "additionalProperties": False,
            },
            "body_partition_id": {"type": "string", "enum": partition_ids},
            "body_block_orientations": {
                "type": "array",
                "items": orientation_block,
                "minItems": 1,
            },
            "planning_risks": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "schema_version",
            "story_id",
            "production_slot",
            "teaser",
            "body_partition_id",
            "body_block_orientations",
            "planning_risks",
        ],
        "additionalProperties": False,
    }


def validate_option_selection(
    selection: dict[str, Any], legal_options: dict[str, Any]
) -> list[str]:
    """Validate cross-field rules that JSON Schema cannot express."""
    errors: list[str] = []
    teaser_by_id = {
        item["option_id"]: item
        for item in legal_options["legal_teaser_options"]
    }
    body_by_id = {
        item["option_id"]: item
        for item in legal_options["legal_block_options"]
    }
    partitions_by_id = {
        item["partition_id"]: item
        for item in legal_options.get("legal_body_partitions", [])
    }
    teaser_value = selection.get("teaser")
    teaser_id = (
        teaser_value.get("option_id")
        if isinstance(teaser_value, dict)
        else None
    )
    teaser = teaser_by_id.get(teaser_id)
    if teaser is None:
        errors.append("story_plan_selection.teaser.option_id is not legal")
        return errors
    partition_id = selection.get("body_partition_id")
    partition = partitions_by_id.get(partition_id)
    if partition is None:
        errors.append(
            "story_plan_selection.body_partition_id is not legal"
        )
        return errors
    if teaser_id not in partition["compatible_teaser_option_ids"]:
        errors.append(
            "story_plan_selection.body_partition_id is not compatible "
            "with the selected teaser"
        )
    orientations = selection.get("body_block_orientations", [])
    if not isinstance(orientations, list):
        errors.append(
            "story_plan_selection.body_block_orientations must be an array"
        )
        return errors
    if len(orientations) != partition["segment_count"]:
        errors.append(
            f"story_plan_selection.body_block_orientations length "
            f"{len(orientations)} does not match partition segment count "
            f"{partition['segment_count']}"
        )
    for index, orientation in enumerate(orientations):
        if not isinstance(orientation, dict):
            continue
        if (
            not orientation.get("orientation_required")
            and orientation.get("orientation_strategy") != "none"
        ):
            errors.append(
                f"story_plan_selection.body_block_orientations[{index}] must "
                "use orientation_strategy=none when orientation_required=false"
            )
        if (
            orientation.get("orientation_required")
            and orientation.get("orientation_strategy") == "none"
        ):
            errors.append(
                f"story_plan_selection.body_block_orientations[{index}] needs "
                "a concrete orientation strategy"
            )
    # Sanity: partition's body options must still be legal individually.
    for index, option_id in enumerate(partition["body_option_ids"]):
        if option_id not in body_by_id:
            errors.append(
                f"story_plan_selection body_partition segment[{index}] "
                f"option_id {option_id} no longer legal"
            )
    return errors


def expand_option_selection(
    selection: dict[str, Any], legal_options: dict[str, Any]
) -> dict[str, Any]:
    """Expand model-selected partition + teaser + orientations into the
    materializer input schema."""
    errors = validate_option_selection(selection, legal_options)
    if errors:
        raise ValueError("; ".join(errors))
    teaser_by_id = {
        item["option_id"]: item
        for item in legal_options["legal_teaser_options"]
    }
    body_by_id = {
        item["option_id"]: item
        for item in legal_options["legal_block_options"]
    }
    partitions_by_id = {
        item["partition_id"]: item
        for item in legal_options.get("legal_body_partitions", [])
    }
    teaser = teaser_by_id[selection["teaser"]["option_id"]]
    partition = partitions_by_id[selection["body_partition_id"]]
    orientations = selection["body_block_orientations"]
    teaser_span_ids = set(teaser["span_candidate_ids"])
    span_catalog = {
        item["span_candidate_id"]: item
        for item in legal_options["span_catalog"]
    }
    teaser_ranges = {
        span_id: span_catalog[span_id]
        for span_id in teaser["span_candidate_ids"]
    }
    blocks = [
        {
            "play_order": 1,
            "role": "teaser",
            "beat_ids": teaser["beat_ids"],
            "span_selections": [
                {
                    "span_candidate_id": span_id,
                    "reuse_mode": "none",
                    "reprise_adds_information": "",
                }
                for span_id in teaser["span_candidate_ids"]
            ],
            "temporal_relation_from_previous": "start",
            "orientation_required": False,
            "orientation_strategy": "none",
            "selection_reason": selection["teaser"]["selection_reason"],
        }
    ]
    for index, option_id in enumerate(partition["body_option_ids"]):
        orientation = orientations[index]
        option = body_by_id[option_id]
        # ``option["span_candidate_ids"]`` is sorted alphabetically for
        # stable option/partition identity. That order has nothing to do
        # with narrative time, so before we render span_selections into
        # per-clip playback order sort them by (episode, source_start).
        # This keeps clips within one Block playing in chronological source
        # order — QC's flow check rejects Blocks that jump between episodes
        # non-monotonically.
        ordered_span_ids = sorted(
            option["span_candidate_ids"],
            key=lambda span_id: (
                int(span_catalog[span_id].get("episode", 0)),
                float(span_catalog[span_id].get("start", 0.0)),
                span_id,
            ),
        )
        span_selections = []
        for span_id in ordered_span_ids:
            overlaps_teaser = any(
                _overlap_seconds(
                    span_catalog[span_id],
                    teaser_ranges[teaser_span_id],
                )
                > 0.05
                for teaser_span_id in teaser_span_ids
            )
            span_selections.append(
                {
                    "span_candidate_id": span_id,
                    "reuse_mode": (
                        "teaser_reprise" if overlaps_teaser else "none"
                    ),
                    "reprise_adds_information": (
                        "正文补足 "
                        + "、".join(option["beat_ids"])
                        + " 的完整因果与新增信息。"
                        if overlaps_teaser
                        else ""
                    ),
                }
            )
        blocks.append(
            {
                "play_order": index + 2,
                "role": option["role"],
                "beat_ids": option["beat_ids"],
                "span_selections": span_selections,
                "temporal_relation_from_previous": orientation[
                    "temporal_relation_from_previous"
                ],
                "orientation_required": orientation[
                    "orientation_required"
                ],
                "orientation_strategy": orientation[
                    "orientation_strategy"
                ],
                "selection_reason": orientation["selection_reason"],
            }
        )
    return {
        "schema_version": "1.0",
        "story_id": selection["story_id"],
        "production_slot": selection["production_slot"],
        "blocks": blocks,
        "planning_risks": selection["planning_risks"],
    }


# =========================================================================
# Story Plan materialization (from _legacy_v4/scripts/materialize_story_plans.py)
# =========================================================================

from autocut_core.libs.editorial_knowledge import load_knowledge_section

_ep = load_knowledge_section("editorial_plan") or {}
ROLE_BY_BEAT = _ep.get("role_by_beat") or {
    "teaser_intent": "teaser",
    "orientation": "orientation",
    "setup": "setup",
    "escalation": "escalation",
    "turn_or_reveal": "turn_or_reveal",
    "payoff": "payoff",
    "end_hook": "end_hook",
}
NONLINEAR_RELATIONS = set(
    _ep.get("nonlinear_relations")
    or {"flashback_context", "preview_future", "return_to_mainline", "parallel"}
)


def is_obvious_backward_episode_jump(
    previous_clips: list[dict[str, Any]],
    current_clips: list[dict[str, Any]],
) -> bool:
    """Return True when the entire current Block is earlier than the prior.

    Mixed-episode montage Blocks are left to Story QC/model reasoning.  This
    deterministic gate targets unambiguous regressions such as EP38 -> EP06.
    """
    previous_episodes = [
        item.get("episode")
        for item in previous_clips
        if isinstance(item.get("episode"), int)
    ]
    current_episodes = [
        item.get("episode")
        for item in current_clips
        if isinstance(item.get("episode"), int)
    ]
    return bool(
        previous_episodes
        and current_episodes
        and max(current_episodes) < min(previous_episodes)
    )


def ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def merged_duration(ranges: list[tuple[float, float]]) -> float:
    if not ranges:
        return 0.0
    merged: list[list[float]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1] + 0.001:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return rounded(sum(end - start for start, end in merged))


def build_source_usage(clips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for clip in clips:
        grouped.setdefault(clip["source_id"], []).append(clip)
    usage = []
    for source_id, source_clips in sorted(
        grouped.items(), key=lambda item: (item[1][0]["episode"], item[0])
    ):
        usage.append(
            {
                "source_id": source_id,
                "episode": source_clips[0]["episode"],
                "selected_clip_count": len(source_clips),
                "playback_duration_seconds": rounded(
                    sum(item["duration_seconds"] for item in source_clips)
                ),
                "unique_source_duration_seconds": merged_duration(
                    [
                        (item["source_start"], item["source_end"])
                        for item in source_clips
                    ]
                ),
            }
        )
    return usage


def unique_duration_for_records(
    records: list[dict[str, Any]],
) -> float:
    grouped: dict[str, list[tuple[float, float]]] = {}
    for item in records:
        grouped.setdefault(item["source_id"], []).append(
            (float(item["source_start"]), float(item["source_end"]))
        )
    return rounded(
        sum(merged_duration(ranges) for ranges in grouped.values())
    )


def available_candidate_duration(bundle: dict[str, Any]) -> float:
    records = [
        {
            "source_id": item["source_id"],
            "source_start": item["start"],
            "source_end": item["end"],
        }
        for item in bundle["candidates"]
    ]
    return unique_duration_for_records(records)


def _fact_event_map(packet: dict[str, Any]) -> dict[str, set[str]]:
    return {
        item["id"]: set(item.get("event_ids", []))
        for item in packet["evidence_catalog"]["facts"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def advance_viewer_knowledge(
    *,
    block_id: str,
    block_role: str,
    beat_ids: list[str],
    beats: dict[str, dict[str, Any]],
    block_candidates: list[dict[str, Any]],
    before_facts: set[str],
    fact_events: dict[str, set[str]],
) -> tuple[dict[str, list[str]], set[str], list[str]]:
    """Validate and advance viewer facts in approved Beat order."""
    before = set(before_facts)
    working_facts = set(before)
    required_before: set[str] = set()
    introduced: set[str] = set()
    withheld: set[str] = set()
    blocked: list[str] = []
    for beat_id in beat_ids:
        beat = beats[beat_id]
        beat_required = set(beat["required_before_fact_ids"])
        beat_introduced = set(beat["introduced_fact_ids"])
        beat_withheld = set(beat["must_not_reveal_fact_ids"])
        required_before.update(beat_required)
        introduced.update(beat_introduced)
        withheld.update(beat_withheld)
        if block_role != "teaser":
            missing_prerequisites = sorted(beat_required - working_facts)
            if missing_prerequisites:
                blocked.append(
                    f"{block_id}.{beat_id}: 尚未向观众引入必要 Fact "
                    f"{missing_prerequisites}"
                )
        elif beat_required - working_facts - beat_withheld:
            blocked.append(
                f"{block_id}.{beat_id}: Teaser 的未知前提没有进入 "
                "intentionally withheld"
            )
        if beat_introduced & beat_withheld:
            blocked.append(
                f"{block_id}.{beat_id}: 同一 Beat 同时引入并要求隐藏 Fact "
                f"{sorted(beat_introduced & beat_withheld)}"
            )
        supporting_event_ids = {
            event_id
            for candidate in block_candidates
            if beat_id in candidate["supports_beat_ids"]
            for event_id in candidate["event_ids"]
        }
        leaked_facts = sorted(
            fact_id
            for fact_id in beat_withheld
            if supporting_event_ids & fact_events.get(fact_id, set())
        )
        if leaked_facts and any(
            candidate.get("continuity_replan") is True
            and "continuous_scene" in candidate.get("continuity_modes", [])
            for candidate in block_candidates
        ):
            leaked_facts = []
        if leaked_facts:
            blocked.append(
                f"{block_id}.{beat_id}: 支撑该 Beat 的已选原片可能提前泄露 "
                f"Fact {leaked_facts}"
            )
        working_facts.update(beat_introduced)
    viewer_knowledge = {
        "before_fact_ids": sorted(before),
        "required_before_fact_ids": sorted(required_before),
        "introduced_fact_ids": sorted(introduced),
        "intentionally_withheld_fact_ids": sorted(withheld),
        "after_fact_ids": sorted(working_facts),
    }
    return viewer_knowledge, working_facts, blocked


def plan_filename(story_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", story_id).strip("-")
    if not value:
        raise ValueError(f"unsafe story_id for Story Plan filename: {story_id!r}")
    return f"{value}.json"


def materialize_plan(
    selection: dict[str, Any],
    *,
    script: dict[str, Any],
    bundle: dict[str, Any],
    evidence_packet: dict[str, Any],
    fingerprints: dict[str, str],
) -> dict[str, Any]:
    story_id = script["story_id"]
    if selection["story_id"] != story_id or bundle["story_id"] != story_id:
        raise ValueError(f"Story Plan identity mismatch for {story_id}")
    production_slot = script["portfolio"]["production_slot"]
    if (
        selection["production_slot"] != production_slot
        or bundle["production_slot"] != production_slot
    ):
        raise ValueError(f"Story Plan production slot mismatch for {story_id}")
    beats = {item["id"]: item for item in script["beats"]}
    beat_order = {item["id"]: index for index, item in enumerate(script["beats"])}
    candidates = {
        item["span_candidate_id"]: item for item in bundle["candidates"]
    }
    required_beat_ids = [
        item["id"] for item in script["beats"] if item.get("must_have")
    ]
    required_must_show_ids = [
        must_show["id"]
        for beat in script["beats"]
        if beat.get("must_have")
        for must_show in beat["must_show"]
    ]
    required_thread_beat_ids = list(script["required_thread_beat_ids"])
    thread_beat_order = {
        item: index
        for index, item in enumerate(script["selected_thread_beat_ids"])
    }
    fact_events = _fact_event_map(evidence_packet)
    blocked: list[str] = []
    risks = list(selection.get("planning_risks", []))
    repair_routes: list[dict[str, str]] = []

    def route(code: str, return_to_stage: str, reason: str) -> None:
        repair_routes.append(
            {
                "code": code,
                "return_to_stage": return_to_stage,
                "reason": reason,
            }
        )

    raw_blocks = selection["blocks"]
    orders = [item["play_order"] for item in raw_blocks]
    if len(orders) != len(set(orders)) or sorted(orders) != list(
        range(1, len(raw_blocks) + 1)
    ):
        raise ValueError(f"{story_id}: Block play_order must be contiguous and unique")
    raw_blocks = sorted(raw_blocks, key=lambda item: item["play_order"])
    teaser_script_beat = script["beats"][0]
    opening_strategy = script.get("teaser_contract", {}).get(
        "opening_strategy", "future_preview_reprise"
    )
    original_chronological = opening_strategy == "original_chronological_opening"
    if (
        teaser_script_beat["role"] != "teaser_intent"
        or teaser_script_beat["temporal_position"]
        != ("mainline" if original_chronological else "future_preview")
    ):
        blocked.append(
            "已批准 Story Script 的首 Beat 与 opening_strategy 不匹配"
        )
        route(
            "invalid_teaser_definition",
            "story_script",
            "把 Teaser 改为后续 escalation/turn/payoff 的未来高光预览。",
        )
    if raw_blocks[0]["role"] != "teaser":
        blocked.append("首个 Story Block 必须是高光 Teaser")
        route(
            "highlight_not_first",
            "story_plan",
            "重排 Block，使未来高光 Teaser 位于播放顺序第一位。",
        )
    if any(item["role"] == "teaser" for item in raw_blocks[1:]):
        blocked.append("Teaser 只能位于首个 Story Block")
        route(
            "multiple_teaser_blocks",
            "story_plan",
            "合并或删除后续 Teaser Block。",
        )
    teaser_selections = raw_blocks[0].get("span_selections", [])
    if len(teaser_selections) != 1:
        blocked.append("single_highlight Teaser 必须且只能物化为一个 Clip")
        route(
            "teaser_clip_count_invalid",
            "story_plan",
            "只选择包含一个原子 Span 的 legal_teaser_option。",
        )
    elif teaser_selections[0].get("reuse_mode") != "none":
        blocked.append("Teaser 唯一 Clip 的 reuse_mode 必须为 none")
        route(
            "teaser_reuse_mode_invalid",
            "story_plan",
            "Teaser 首次出现不能声明为 reprise。",
        )
    selected_beat_ids: list[str] = []
    selected_must_show_ids: list[str] = []
    selected_span_ids: list[str] = []
    selected_thread_beat_ids: list[str] = []
    video_review_span_ids: list[str] = []
    plan_blocks: list[dict[str, Any]] = []
    sequence_edges: list[dict[str, Any]] = []
    all_clips: list[dict[str, Any]] = []
    cumulative_facts: set[str] = set()
    clip_counter = 0
    prior_clips: list[tuple[int, str, dict[str, Any]]] = []
    span_use_counts: dict[str, int] = {}
    for block_index, block in enumerate(raw_blocks, start=1):
        block_id = f"block-{block_index:03d}"
        block_beat_ids = block["beat_ids"]
        unknown_beats = sorted(set(block_beat_ids) - set(beats))
        if unknown_beats:
            raise ValueError(
                f"{story_id}.{block_id}: unknown Beat IDs {unknown_beats}"
            )
        if len(block_beat_ids) != len(set(block_beat_ids)):
            blocked.append(f"{block_id}: Beat ID 重复")
        selected_beat_ids.extend(block_beat_ids)
        expected_role = ROLE_BY_BEAT[beats[block_beat_ids[0]]["role"]]
        if block["role"] != expected_role:
            blocked.append(
                f"{block_id}: role={block['role']} 与首个 Beat 的 "
                f"role={expected_role} 不一致"
            )
        relation = block["temporal_relation_from_previous"]
        orientation_required = block["orientation_required"]
        orientation_strategy = block["orientation_strategy"]
        if block_index == 1:
            if relation != "start":
                blocked.append(f"{block_id}: 首个 Block 的时间关系必须是 start")
            if orientation_required or orientation_strategy != "none":
                blocked.append(f"{block_id}: 首个 Block 不应声明前序定向")
        else:
            if relation == "start":
                blocked.append(f"{block_id}: 非首个 Block 不能使用 start")
            if relation in NONLINEAR_RELATIONS and (
                not orientation_required or orientation_strategy == "none"
            ):
                blocked.append(
                    f"{block_id}: 非线性跳转缺少明确的观众定向策略"
                )
            if not orientation_required and orientation_strategy != "none":
                blocked.append(
                    f"{block_id}: orientation_required=false 时策略必须为 none"
                )
            sequence_edges.append(
                {
                    "id": f"edge-block-{block_index - 1:03d}--block-{block_index:03d}",
                    "from_block_id": f"block-{block_index - 1:03d}",
                    "to_block_id": block_id,
                    "temporal_relation": relation,
                    "orientation_required": orientation_required,
                    "orientation_strategy": orientation_strategy,
                }
            )
        block_candidates: list[dict[str, Any]] = []
        block_clips: list[dict[str, Any]] = []
        for selection_item in block["span_selections"]:
            candidate_id = selection_item["span_candidate_id"]
            candidate = candidates.get(candidate_id)
            if candidate is None:
                raise ValueError(
                    f"{story_id}.{block_id}: unknown Span Candidate {candidate_id}"
                )
            clip_counter += 1
            clip = {
                "id": f"clip-{clip_counter:03d}",
                "span_candidate_id": candidate_id,
                "source_id": candidate["source_id"],
                "episode": candidate["episode"],
                "source_start": candidate["start"],
                "source_end": candidate["end"],
                "duration_seconds": candidate["duration_seconds"],
                "source_duration_seconds": candidate[
                    "source_duration_seconds"
                ],
                "source_coverage_ratio": candidate[
                    "source_coverage_ratio"
                ],
                "full_source_like": candidate["full_source_like"],
                "event_ids": candidate["event_ids"],
                "thread_beat_ids": candidate["supports_thread_beat_ids"],
                "candidate_ids": candidate["candidate_ids"],
                "reuse_mode": selection_item["reuse_mode"],
                "reprise_adds_information": selection_item[
                    "reprise_adds_information"
                ],
                "boundary_status": candidate["boundary_status"],
                "material_risks": candidate["material_risks"],
            }
            span_use_counts[candidate_id] = (
                span_use_counts.get(candidate_id, 0) + 1
            )
            if span_use_counts[candidate_id] > 1:
                if block["role"] == "teaser":
                    blocked.append(
                        f"{clip['id']}: 同一 span_candidate_id "
                        f"{candidate_id} 不得在 Teaser Block 内部复用"
                    )
                    route(
                        "duplicate_span_candidate",
                        "story_plan",
                        "Teaser 只允许一个 Clip。若想强调该 Span，改到正文中作复用。",
                    )
            if (
                clip["reuse_mode"] == "teaser_reprise"
                and not clip["reprise_adds_information"].strip()
            ):
                blocked.append(
                    f"{clip['id']}: teaser_reprise 必须说明正文新增信息"
                )
            overlaps_prior = False
            for prior_order, prior_role, prior in prior_clips:
                if prior["source_id"] != clip["source_id"]:
                    continue
                overlap = min(prior["source_end"], clip["source_end"]) - max(
                    prior["source_start"], clip["source_start"]
                )
                if overlap <= 0.001:
                    continue
                overlaps_prior = True
                if (
                    prior_order >= block_index
                    or block["role"] == "teaser"
                ):
                    blocked.append(
                        f"{clip['id']}: Story 内原片重叠指向了未来 Block 或 Teaser"
                    )
            if (
                clip["reuse_mode"] == "teaser_reprise"
                and not overlaps_prior
            ):
                blocked.append(
                    f"{clip['id']}: teaser_reprise 没有对应的更早 Teaser 原片"
                )
            selected_span_ids.append(candidate_id)
            selected_thread_beat_ids.extend(
                candidate["supports_thread_beat_ids"]
            )
            if candidate["boundary_status"] != "verified":
                video_review_span_ids.append(candidate_id)
            risks.extend(candidate["material_risks"])
            block_candidates.append(candidate)
            block_clips.append(clip)
            all_clips.append(clip)
            prior_clips.append((block_index, block["role"], clip))
        if (
            block_index > 1
            and plan_blocks
            and is_obvious_backward_episode_jump(
                plan_blocks[-1]["clips"], block_clips
            )
            and (
                relation != "flashback_context"
                or not orientation_required
                or orientation_strategy == "none"
            )
        ):
            previous_episodes = sorted(
                {
                    item["episode"]
                    for item in plan_blocks[-1]["clips"]
                    if isinstance(item.get("episode"), int)
                }
            )
            current_episodes = sorted(
                {
                    item["episode"]
                    for item in block_clips
                    if isinstance(item.get("episode"), int)
                }
            )
            blocked.append(
                f"{block_id}: 明显倒序原片跳转 "
                f"EP{previous_episodes[-1]:03d}->EP{current_episodes[0]:03d} "
                "必须标为 flashback_context，并提供明确观众定向"
            )
            route(
                "backward_episode_jump_requires_flashback",
                "story_plan",
                (
                    "把明显倒序 Block 标为 flashback_context，设置 "
                    "orientation_required=true，并选择 dialogue_anchor、"
                    "visual_anchor 或 title_card。"
                ),
            )
        for beat_id in block_beat_ids:
            beat = beats[beat_id]
            supporting = [
                item
                for item in block_candidates
                if beat_id in item["supports_beat_ids"]
            ]
            if not supporting:
                blocked.append(
                    f"{block_id}: 没有已选 Span 支撑 Beat {beat_id}"
                )
                route(
                    "beat_not_supported_by_selection",
                    (
                        "story_plan"
                        if any(
                            beat_id in item["supports_beat_ids"]
                            for item in bundle["candidates"]
                        )
                        else "span_compiler"
                    ),
                    "改选能支撑该 Beat 的局部 Span；若没有候选则回编译器补齐。",
                )
            supported_show_ids = {
                show_id
                for item in supporting
                for show_id in item["supports_must_show_ids"]
            }
            for must_show in beat["must_show"]:
                if must_show["id"] in supported_show_ids:
                    selected_must_show_ids.append(must_show["id"])
                elif beat.get("must_have"):
                    blocked.append(
                        f"{block_id}: must-show {must_show['id']} 未被已选 Span 覆盖"
                    )
                    route(
                        "must_show_not_supported",
                        (
                            "story_plan"
                            if any(
                                must_show["id"]
                                in item["supports_must_show_ids"]
                                for item in bundle["candidates"]
                            )
                            else "story_evidence"
                        ),
                        "改选带该 must-show provenance 的 Span；若目录中不存在则修复 Evidence 召回。",
                    )
        for candidate in block_candidates:
            if not set(candidate["supports_beat_ids"]) & set(block_beat_ids):
                blocked.append(
                    f"{block_id}: Span {candidate['span_candidate_id']} "
                    "不支撑本 Block 的任何 Beat"
                )
        resolved_questions = {
            question_id
            for beat_id in block_beat_ids
            for question_id in beats[beat_id]["resolved_question_ids"]
        }
        (
            viewer_knowledge,
            cumulative_facts,
            viewer_knowledge_blocks,
        ) = advance_viewer_knowledge(
            block_id=block_id,
            block_role=block["role"],
            beat_ids=block_beat_ids,
            beats=beats,
            block_candidates=block_candidates,
            before_facts=cumulative_facts,
            fact_events=fact_events,
        )
        blocked.extend(viewer_knowledge_blocks)
        plan_blocks.append(
            {
                "id": block_id,
                "play_order": block_index,
                "role": block["role"],
                "beat_ids": block_beat_ids,
                "thread_beat_ids": sorted(
                    {
                        thread_beat_id
                        for candidate in block_candidates
                        for thread_beat_id in candidate[
                            "supports_thread_beat_ids"
                        ]
                    },
                    key=lambda item: (
                        thread_beat_order.get(item, 10**9),
                        item,
                    ),
                ),
                "clips": block_clips,
                "introduced_fact_ids": viewer_knowledge[
                    "introduced_fact_ids"
                ],
                "resolved_question_ids": sorted(resolved_questions),
                "viewer_knowledge": viewer_knowledge,
                "selection_reason": block["selection_reason"],
            }
        )
    playback_duration = rounded(
        sum(item["duration_seconds"] for item in all_clips)
    )
    unique_source_duration = unique_duration_for_records(all_clips)
    repeated_source_duration = rounded(
        max(0.0, playback_duration - unique_source_duration)
    )
    repeat_ratio = rounded(
        repeated_source_duration / playback_duration
    ) if playback_duration > 0 else 0.0
    full_source_like_clips = [
        item for item in all_clips if item["full_source_like"]
    ]
    full_source_like_playback_duration = rounded(
        sum(item["duration_seconds"] for item in full_source_like_clips)
    )
    full_source_like_playback_ratio = rounded(
        full_source_like_playback_duration / playback_duration
    ) if playback_duration > 0 else 0.0
    teaser_clips = (
        plan_blocks[0]["clips"]
        if plan_blocks and plan_blocks[0]["role"] == "teaser"
        else []
    )
    teaser_duration = rounded(
        sum(item["duration_seconds"] for item in teaser_clips)
    )
    highlight_candidate_ids = {
        item["id"]
        for item in evidence_packet["evidence_catalog"]["candidates"]
        if item.get("type") == "highlight"
    }
    teaser_has_highlight = any(
        set(item["candidate_ids"]) & highlight_candidate_ids
        for item in teaser_clips
    )
    script_highlight_first = (
        teaser_script_beat["role"] == "teaser_intent"
        and teaser_script_beat["temporal_position"]
        in {"future_preview", "mainline"}
    )
    highlight_first = bool(
        script_highlight_first
        and plan_blocks
        and plan_blocks[0]["role"] == "teaser"
        and teaser_has_highlight
        and 0 < teaser_duration <= 30.0
    )
    available_unique_duration = available_candidate_duration(bundle)
    minimum_duration = float(script["target_duration"]["minimum_seconds"])
    editorial_surplus = rounded(
        available_unique_duration - minimum_duration
    )
    editorial_surplus_ratio = rounded(
        editorial_surplus / minimum_duration
    ) if minimum_duration > 0 else 0.0
    insufficient_editorial_surplus = False
    clip_durations = [float(item["duration_seconds"]) for item in all_clips]
    median_clip_duration = (
        rounded(median(clip_durations)) if clip_durations else 0.0
    )
    preferred_minimum_clip_count = max(1, int(minimum_duration // 40.0))
    editorial_density_status = "passed"
    editorial_density_reasons: list[str] = []
    if len(all_clips) < preferred_minimum_clip_count:
        editorial_density_status = "below_target"
        editorial_density_reasons.append(
            f"clip_count={len(all_clips)} < preferred_minimum "
            f"{preferred_minimum_clip_count}"
        )
    if clip_durations and (
        median_clip_duration < 12.0 or median_clip_duration > 40.0
    ):
        editorial_density_status = "below_target"
        editorial_density_reasons.append(
            f"median_clip_duration={median_clip_duration:.1f}s outside 12-40s"
        )
    editorial_metrics = {
        "playback_duration_seconds": playback_duration,
        "unique_source_duration_seconds": unique_source_duration,
        "repeated_source_duration_seconds": repeated_source_duration,
        "repeat_ratio": min(1.0, repeat_ratio),
        "available_unique_candidate_duration_seconds": (
            available_unique_duration
        ),
        "editorial_surplus_seconds": editorial_surplus,
        "editorial_surplus_ratio": editorial_surplus_ratio,
        "insufficient_editorial_surplus": (
            insufficient_editorial_surplus
        ),
        "full_source_like_clip_count": len(full_source_like_clips),
        "full_source_like_playback_duration_seconds": (
            full_source_like_playback_duration
        ),
        "full_source_like_playback_ratio": min(
            1.0, full_source_like_playback_ratio
        ),
        "teaser_duration_seconds": teaser_duration,
        "clip_count": len(all_clips),
        "median_clip_duration_seconds": median_clip_duration,
        "preferred_minimum_clip_count": preferred_minimum_clip_count,
        "preferred_median_clip_seconds_range": [12.0, 40.0],
        "editorial_density_status": editorial_density_status,
        "editorial_density_reasons": editorial_density_reasons,
        "highlight_first_status": (
            "passed" if highlight_first else "failed"
        ),
    }
    if editorial_density_status != "passed":
        risks.extend(
            f"editorial_density_below_target: {reason}"
            for reason in editorial_density_reasons
        )
    if not teaser_has_highlight:
        blocked.append(
            "首个 Teaser 没有选择带明确 Highlight provenance 的局部 Span"
        )
        route(
            "teaser_span_not_highlight",
            "story_plan",
            "从 Highlight Candidate 派生的 tight Span 中重新选择 Teaser。",
        )
    if teaser_duration > 30.0:
        blocked.append(
            f"Teaser 播放时长 {teaser_duration:.3f}s 超过 30 秒硬上限"
        )
        route(
            "teaser_too_long",
            "span_compiler",
            "保留独立的 Highlight tight Span，供 Plan 选择 30 秒以内高光。",
        )
    elif teaser_duration and not 8.0 <= teaser_duration <= 20.0:
        risks.append(
            f"Teaser 播放时长 {teaser_duration:.3f}s 不在 8-20 秒优选范围"
        )
    if repeated_source_duration > 20.0 + 0.001 or repeat_ratio > 0.08:
        blocked.append(
            "Story 内重复原片超限："
            f"{repeated_source_duration:.3f}s / {repeat_ratio:.3f}，"
            "硬上限为 20s 且成片 8%"
        )
        route(
            "repeat_budget_exceeded",
            "story_plan",
            "移除重叠选段；Teaser reprise 只能保留有限局部重叠。",
        )
    effective_full_clip_cap = (
        effective_max_full_source_like_clips_per_story(
            available_unique_duration
        )
    )
    if (
        len(full_source_like_clips) > effective_full_clip_cap
        or full_source_like_playback_ratio > 0.5
    ):
        blocked.append(
            "整集型选段超限："
            f"{len(full_source_like_clips)} 条，播放占比 "
            f"{full_source_like_playback_ratio:.3f}"
            f"（本 Story 生效上限 clip={effective_full_clip_cap}, ratio=0.5）"
        )
        has_local_alternatives = any(
            not item["full_source_like"] for item in bundle["candidates"]
        )
        route(
            "full_source_like_selection",
            "story_plan" if has_local_alternatives else "span_compiler",
            (
                "改选有明确编辑功能的局部 Span。"
                if has_local_alternatives
                else "重新编译细粒度 tight/scene Span，禁止只提供整集型候选。"
            ),
        )
    duplicate_beats = sorted(
        beat_id
        for beat_id in set(selected_beat_ids)
        if selected_beat_ids.count(beat_id) > 1
    )
    if duplicate_beats:
        blocked.append(f"Beat 被多个 Block 重复承担：{duplicate_beats}")
    flattened_positions = [beat_order[item] for item in selected_beat_ids]
    if flattened_positions != sorted(flattened_positions):
        blocked.append("Block 中的 Beat 顺序违背已批准 Story Script")
    uncovered_required_beats = sorted(
        set(required_beat_ids) - set(selected_beat_ids),
        key=lambda item: beat_order[item],
    )
    if uncovered_required_beats:
        blocked.append(f"缺少 must-have Beat：{uncovered_required_beats}")
    uncovered_required_shows = sorted(
        set(required_must_show_ids) - set(selected_must_show_ids)
    )
    if uncovered_required_shows:
        blocked.append(
            f"缺少 must-have Beat 的 must-show：{uncovered_required_shows}"
        )
    uncovered_required_thread_beats = sorted(
        set(required_thread_beat_ids) - set(selected_thread_beat_ids),
        key=lambda item: (thread_beat_order.get(item, 10**9), item),
    )
    if uncovered_required_thread_beats:
        blocked.append(
            "缺少必需 Thread Beat 的原片支撑："
            f"{uncovered_required_thread_beats}"
        )
    if required_beat_ids and selected_beat_ids:
        first_required = required_beat_ids[0]
        if first_required not in plan_blocks[0]["beat_ids"]:
            blocked.append("首个 must-have Beat 没有位于首个 Story Block")
    hook_beat_ids = [
        item["id"] for item in script["beats"] if item["role"] == "end_hook"
    ]
    if hook_beat_ids and not set(hook_beat_ids) <= set(
        plan_blocks[-1]["beat_ids"]
    ):
        blocked.append("End Hook Beat 必须位于最后一个 Story Block")
    final_missing_facts = sorted(
        set(script["required_fact_ids"]) - cumulative_facts
    )
    if final_missing_facts:
        blocked.append(f"全片结束仍未引入必要 Fact：{final_missing_facts}")
    estimated_duration = playback_duration
    duration = script["target_duration"]
    if estimated_duration > duration["maximum_seconds"]:
        blocked.append(
            f"预计播放时长 {estimated_duration:.3f}s 高于硬上限 "
            f"{duration['maximum_seconds']:.3f}s"
        )
        route(
            "selected_duration_above_maximum",
            "story_plan",
            "删除低功能或冗余片段，保留核心因果链。",
        )
    blocked = ordered_unique(blocked)
    repair_routes = list(
        {
            (
                item["code"],
                item["return_to_stage"],
                item["reason"],
            ): item
            for item in repair_routes
        }.values()
    )
    plan = {
        "schema_version": "1.0",
        "method": "legal-option-selection-local-materialization-v2",
        "story_id": story_id,
        "title": script["title"],
        "production_slot": production_slot,
        "genre_profile": script.get("genre_profile", "project_specific"),
        "golden_case_ids": list(script.get("golden_case_ids", [])),
        "status": "blocked" if blocked else "ready_for_video_qc",
        "input_fingerprints": fingerprints,
        "duration_contract": duration,
        "estimated_duration_seconds": estimated_duration,
        "editorial_contract": script.get("editorial_contract", {}),
        **(
            {"edit_mode": script["edit_mode"]}
            if script.get("edit_mode") in {"montage", "original_chronological"}
            else {}
        ),
        "editorial_metrics": editorial_metrics,
        "blocks": plan_blocks,
        "sequence_edges": sequence_edges,
        "source_usage": build_source_usage(all_clips),
        "coverage": {
            "required_beat_ids": required_beat_ids,
            "covered_beat_ids": ordered_unique(selected_beat_ids),
            "uncovered_required_beat_ids": uncovered_required_beats,
            "required_must_show_ids": required_must_show_ids,
            "covered_must_show_ids": ordered_unique(selected_must_show_ids),
            "uncovered_required_must_show_ids": uncovered_required_shows,
            "required_thread_beat_ids": required_thread_beat_ids,
            "covered_thread_beat_ids": sorted(
                set(selected_thread_beat_ids),
                key=lambda item: (thread_beat_order.get(item, 10**9), item),
            ),
            "uncovered_required_thread_beat_ids": (
                uncovered_required_thread_beats
            ),
        },
        "selected_span_candidate_ids": ordered_unique(selected_span_ids),
        "video_review_span_candidate_ids": ordered_unique(
            video_review_span_ids
        ),
        "planning_risks": ordered_unique(risks),
        "blocked_reasons": blocked,
        "repair_routes": repair_routes,
    }
    schema_errors = validate_task_response("story_plan", plan)
    if schema_errors:
        raise ValueError(
            f"locally materialized Story Plan is invalid: "
            + "; ".join(schema_errors[:40])
        )
    return plan


# =========================================================================
# Story Plan generation fingerprint (from _legacy_v4/scripts/story_plan_generation.py)
# =========================================================================


def plan_generation_sha256(
    *,
    story_approval_sha256: str,
    story_evidence_index_sha256: str,
    span_candidate_index_sha256: str,
    preflight: dict[str, Any],
) -> str:
    """Return a non-self-referential fingerprint for one Plan generation."""

    normalized_preflight = deepcopy(preflight)
    normalized_preflight.pop("plan_generation_sha256", None)
    return json_sha256(
        {
            "compiler_version": COMPILER_VERSION,
            "planning_contract_version": PLANNING_CONTRACT_VERSION,
            "story_approval_sha256": story_approval_sha256,
            "story_evidence_index_sha256": (
                story_evidence_index_sha256
            ),
            "span_candidate_index_sha256": span_candidate_index_sha256,
            "preflight_contract_sha256": json_sha256(
                normalized_preflight
            ),
        }
    )


def current_plan_generation(job_root: Path) -> str:
    """Recompute the generation recorded by the current preflight artifact."""

    root = job_root.expanduser().resolve()
    approval_path = root / "story-approval.json"
    evidence_index_path = root / "story-evidence" / "index.json"
    span_index_path = root / "span-candidates" / "index.json"
    preflight_path = root / "story-plan-preflight.json"
    for path in (
        approval_path,
        evidence_index_path,
        span_index_path,
        preflight_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    return plan_generation_sha256(
        story_approval_sha256=sha256_file(approval_path),
        story_evidence_index_sha256=sha256_file(evidence_index_path),
        span_candidate_index_sha256=sha256_file(span_index_path),
        preflight=load_json(preflight_path),
    )


def render_review(plans: list[dict[str, Any]], index_status: str) -> str:
    """Render a human-readable Markdown review of materialized Story Plans.

    Migrated from _legacy_v4/scripts/materialize_story_plans.py.
    """
    lines = [
        "# Story Plan 原片编排复核",
        "",
        f"> Portfolio 状态：`{index_status}`。本阶段尚未执行视频级边界验证或渲染。",
        "",
    ]
    for plan in sorted(plans, key=lambda item: item["production_slot"]):
        lines.extend(
            [
                f"## 槽位 {plan['production_slot']}：{plan['title']}",
                "",
                f"- Story ID：`{plan['story_id']}`",
                f"- 状态：`{plan['status']}`",
                f"- 预计播放时长：{plan['estimated_duration_seconds']:.1f} 秒",
                f"- Block：{len(plan['blocks'])} 个",
                f"- Clip：{sum(len(item['clips']) for item in plan['blocks'])} 个",
                (
                    "- 去重原片 / 重复："
                    f"{plan['editorial_metrics']['unique_source_duration_seconds']:.1f}s / "
                    f"{plan['editorial_metrics']['repeated_source_duration_seconds']:.1f}s "
                    f"({plan['editorial_metrics']['repeat_ratio']:.1%})"
                ),
                (
                    "- 整集型 Clip / 播放占比："
                    f"{plan['editorial_metrics']['full_source_like_clip_count']} / "
                    f"{plan['editorial_metrics']['full_source_like_playback_ratio']:.1%}"
                ),
                (
                    "- Teaser / 高光前置："
                    f"{plan['editorial_metrics']['teaser_duration_seconds']:.1f}s / "
                    f"`{plan['editorial_metrics']['highlight_first_status']}`"
                ),
                (
                    "- 候选编辑余量："
                    f"{plan['editorial_metrics']['editorial_surplus_seconds']:.1f}s "
                    f"({plan['editorial_metrics']['editorial_surplus_ratio']:.1%})"
                ),
                (
                    "- 待定向视频复核 Span："
                    f"{len(plan['video_review_span_candidate_ids'])} 个"
                ),
                "",
            ]
        )
        if plan["blocked_reasons"]:
            lines.extend(["### 阻断原因", ""])
            lines.extend(f"- {item}" for item in plan["blocked_reasons"])
            lines.append("")
        if plan["repair_routes"]:
            lines.extend(["### 修复路由", ""])
            lines.extend(
                f"- `{item['code']}` → `{item['return_to_stage']}`："
                f"{item['reason']}"
                for item in plan["repair_routes"]
            )
            lines.append("")
        lines.extend(
            [
                "### 播放顺序",
                "",
                "| 顺序 | 角色 | Beat | 原片 | 时间关系 | 选择理由 |",
                "|---:|---|---|---|---|---|",
            ]
        )
        relations = {
            edge["to_block_id"]: edge for edge in plan["sequence_edges"]
        }
        for block in plan["blocks"]:
            edge = relations.get(block["id"])
            relation = "start" if edge is None else edge["temporal_relation"]
            clips = "<br/>".join(
                (
                    f"`{clip['span_candidate_id']}`<br/>"
                    f"{clip['source_id']} "
                    f"{clip['source_start']:.3f}–{clip['source_end']:.3f}"
                    f" ({clip['duration_seconds']:.1f}s, "
                    f"{clip['reuse_mode']}, {clip['boundary_status']})"
                )
                for clip in block["clips"]
            )
            lines.append(
                f"| {block['play_order']} | `{block['role']}` | "
                f"{', '.join(block['beat_ids'])} | {clips} | "
                f"`{relation}` | {block['selection_reason']} |"
            )
        lines.extend(["", "### 覆盖与风险", ""])
        lines.append(
            "- 未覆盖 must-have Beat："
            + (
                ", ".join(plan["coverage"]["uncovered_required_beat_ids"])
                or "无"
            )
        )
        lines.append(
            "- 未覆盖 must-show："
            + (
                ", ".join(
                    plan["coverage"]["uncovered_required_must_show_ids"]
                )
                or "无"
            )
        )
        lines.append(
            "- 未覆盖 required Thread Beat："
            + (
                ", ".join(
                    plan["coverage"][
                        "uncovered_required_thread_beat_ids"
                    ]
                )
                or "无"
            )
        )
        if plan["planning_risks"]:
            lines.extend(
                f"- 风险：{item}" for item in plan["planning_risks"]
            )
        else:
            lines.append("- 规划风险：无。")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
