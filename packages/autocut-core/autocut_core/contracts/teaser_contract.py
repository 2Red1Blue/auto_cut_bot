#!/usr/bin/env python3
"""Shared deterministic contracts for single-highlight teaser stitching."""

from __future__ import annotations

from typing import Any


TEASER_STITCH_MAX_GAP_SECONDS = 5.0
TEASER_STITCH_MAX_DURATION_SECONDS = 30.0
TEASER_REPRISE_MAX_REPEAT_SECONDS = 20.0
TEASER_PREFERRED_MINIMUM_SECONDS = 8.0
TEASER_MAXIMUM_SECONDS = 15.0


def resolve_must_show_event_ids(
    must_show: dict[str, Any],
    fact_event_ids: dict[str, Any],
) -> tuple[set[str], set[str], set[str]]:
    """Separate physical direct evidence from Fact-expanded retrieval evidence."""
    direct = {
        item
        for item in must_show.get("evidence_event_ids", [])
        if isinstance(item, str)
    }
    expanded: set[str] = set()
    for fact_id in must_show.get("evidence_fact_ids", []):
        if not isinstance(fact_id, str):
            continue
        value = fact_event_ids.get(fact_id, [])
        if isinstance(value, dict):
            value = value.get("event_ids", [])
        expanded.update(
            item for item in value if isinstance(item, str)
        )
    return direct, expanded, direct | expanded


def interval_stitch_diagnostics(
    a_start: float,
    a_end: float,
    b_start: float,
    b_end: float,
    *,
    maximum_gap_seconds: float = TEASER_STITCH_MAX_GAP_SECONDS,
    maximum_duration_seconds: float = TEASER_STITCH_MAX_DURATION_SECONDS,
) -> dict[str, Any]:
    """Return exact gap/union diagnostics for two source-time intervals."""
    a_start = float(a_start)
    a_end = float(a_end)
    b_start = float(b_start)
    b_end = float(b_end)
    if a_end <= a_start or b_end <= b_start:
        return {
            "stitchable": False,
            "reason": "invalid_interval",
            "gap_seconds": None,
            "union_start": None,
            "union_end": None,
            "union_duration_seconds": None,
        }
    if b_start > a_end:
        gap = b_start - a_end
    elif a_start > b_end:
        gap = a_start - b_end
    else:
        gap = 0.0
    union_start = min(a_start, b_start)
    union_end = max(a_end, b_end)
    union_duration = union_end - union_start
    if gap > maximum_gap_seconds + 0.001:
        reason = "gap_exceeds_limit"
    elif union_duration > maximum_duration_seconds + 0.001:
        reason = "union_exceeds_limit"
    else:
        reason = "ok"
    return {
        "stitchable": reason == "ok",
        "reason": reason,
        "gap_seconds": round(gap, 3),
        "union_start": round(union_start, 3),
        "union_end": round(union_end, 3),
        "union_duration_seconds": round(union_duration, 3),
    }


def minimum_stitched_union(
    primary: tuple[float, float],
    interval_groups: list[list[tuple[float, float, str]]],
    *,
    maximum_gap_seconds: float = TEASER_STITCH_MAX_GAP_SECONDS,
    maximum_duration_seconds: float = TEASER_STITCH_MAX_DURATION_SECONDS,
) -> dict[str, Any] | None:
    """Return the shortest legal union covering one interval from every group.

    ``interval_groups`` is normally one group per directly required Event.  An
    Event may have several source ranges, so the compiler must choose one range
    per Event jointly; validating every range independently against the primary
    is insufficient because the final union can still exceed the hard limit.
    """
    primary_start, primary_end = (float(primary[0]), float(primary[1]))
    if primary_end <= primary_start:
        return None
    states: list[dict[str, Any]] = [
        {
            "start": primary_start,
            "end": primary_end,
            "selected_intervals": [],
            "maximum_gap_seconds": 0.0,
        }
    ]
    for group in interval_groups:
        next_states: dict[tuple[float, float], dict[str, Any]] = {}
        for state in states:
            for start, end, origin_id in group:
                diagnostics = interval_stitch_diagnostics(
                    state["start"],
                    state["end"],
                    float(start),
                    float(end),
                    maximum_gap_seconds=maximum_gap_seconds,
                    maximum_duration_seconds=maximum_duration_seconds,
                )
                if not diagnostics["stitchable"]:
                    continue
                union_start = float(diagnostics["union_start"])
                union_end = float(diagnostics["union_end"])
                key = (union_start, union_end)
                candidate = {
                    "start": union_start,
                    "end": union_end,
                    "selected_intervals": [
                        *state["selected_intervals"],
                        {
                            "start": round(float(start), 3),
                            "end": round(float(end), 3),
                            "origin_id": origin_id,
                        },
                    ],
                    "maximum_gap_seconds": max(
                        float(state["maximum_gap_seconds"]),
                        float(diagnostics["gap_seconds"]),
                    ),
                }
                incumbent = next_states.get(key)
                if incumbent is None or len(candidate["selected_intervals"]) < len(
                    incumbent["selected_intervals"]
                ):
                    next_states[key] = candidate
        if not next_states:
            return None
        states = sorted(
            next_states.values(),
            key=lambda item: (
                float(item["end"]) - float(item["start"]),
                float(item["start"]),
                float(item["end"]),
            ),
        )[:128]
    best = min(
        states,
        key=lambda item: (
            float(item["end"]) - float(item["start"]),
            float(item["maximum_gap_seconds"]),
            float(item["start"]),
        ),
    )
    return {
        **best,
        "duration_seconds": round(
            float(best["end"]) - float(best["start"]), 3
        ),
    }


def event_can_stitch_to_primary(
    event: dict[str, Any],
    *,
    primary_source_id: str | None,
    primary_start: float | int | None,
    primary_end: float | int | None,
) -> tuple[bool, list[dict[str, Any]]]:
    """Return whether any direct Event range can join the primary highlight."""
    if event.get("source_id") != primary_source_id:
        return False, [
            {
                "stitchable": False,
                "reason": "cross_source",
                "source_id": event.get("source_id"),
            }
        ]
    if not isinstance(primary_start, (int, float)) or not isinstance(
        primary_end, (int, float)
    ):
        return False, [
            {"stitchable": False, "reason": "invalid_primary_interval"}
        ]
    diagnostics: list[dict[str, Any]] = []
    for source_range in event.get("source_ranges", []) or []:
        start = source_range.get("start")
        end = source_range.get("end")
        if not isinstance(start, (int, float)) or not isinstance(
            end, (int, float)
        ):
            diagnostics.append(
                {"stitchable": False, "reason": "invalid_event_interval"}
            )
            continue
        item = interval_stitch_diagnostics(
            float(primary_start),
            float(primary_end),
            float(start),
            float(end),
        )
        item["event_start"] = round(float(start), 3)
        item["event_end"] = round(float(end), 3)
        diagnostics.append(item)
    return any(item.get("stitchable") for item in diagnostics), diagnostics


def is_teaser_eligible_highlight(candidate: dict[str, Any]) -> bool:
    """Return True if the candidate is a highlight eligible for Teaser use.

    A candidate is eligible if it is a highlight (kind/type or allowed_roles
    includes 'highlight') and its duration does not exceed TEASER_MAXIMUM_SECONDS.
    """
    kind = candidate.get("kind") or candidate.get("type") or ""
    allowed = {
        item
        for item in candidate.get("allowed_roles", [])
        if isinstance(item, str) and item
    }
    if "highlight" not in allowed and kind != "highlight":
        return False
    start = candidate.get("start")
    end = candidate.get("end")
    if not isinstance(start, (int, float)) or isinstance(start, bool):
        return False
    if not isinstance(end, (int, float)) or isinstance(end, bool):
        return False
    duration = float(end) - float(start)
    return duration <= TEASER_MAXIMUM_SECONDS
