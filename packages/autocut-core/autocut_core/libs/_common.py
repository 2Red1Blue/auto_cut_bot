#!/usr/bin/env python3
"""Shared utility functions consolidated from libs/ modules.

Functions that were duplicated across multiple libs/ modules with identical
implementations are defined here once.
"""

from __future__ import annotations

from typing import Any


# rule 2: reprise scene-level fallback. When a Teaser highlight event_id
# fails hard match against downstream event_ids, if both are same source_id
# source_range with time window IoU > threshold, they are treated as different
# event cards of the same scene, constituting a valid reprise. Threshold is
# conservative at 0.2 -- same scene split into two events usually has large
# overlap. Cross-source is still rejected.
REPRISE_SCENE_IOU_THRESHOLD = 0.2


def rounded(value: float) -> float:
    return round(float(value), 3)


def _scene_equivalent(
    event_a: dict[str, Any],
    event_b: dict[str, Any],
) -> bool:
    """rule 2: Determine if two Events belong to the same scene -- same
    source_id + any source_range pair IoU > REPRISE_SCENE_IOU_THRESHOLD."""
    if event_a.get("source_id") != event_b.get("source_id"):
        return False
    a_ranges = event_a.get("source_ranges", []) or []
    b_ranges = event_b.get("source_ranges", []) or []
    for a in a_ranges:
        if not isinstance(a.get("start"), (int, float)) or not isinstance(
            a.get("end"), (int, float)
        ):
            continue
        a_start = float(a["start"])
        a_end = float(a["end"])
        a_dur = a_end - a_start
        if a_dur <= 0:
            continue
        for b in b_ranges:
            if not isinstance(b.get("start"), (int, float)) or not isinstance(
                b.get("end"), (int, float)
            ):
                continue
            b_start = float(b["start"])
            b_end = float(b["end"])
            b_dur = b_end - b_start
            if b_dur <= 0:
                continue
            lo = max(a_start, b_start)
            hi = min(a_end, b_end)
            if hi <= lo:
                continue
            overlap = hi - lo
            iou = overlap / max(a_dur, b_dur)
            if iou > REPRISE_SCENE_IOU_THRESHOLD:
                return True
    return False


def _reprise_matches(
    teaser_event_ids: set[str],
    downstream_event_ids: set[str],
    events: dict[str, dict[str, Any]],
) -> bool:
    """rule 1+2: First try hard event_id match; fall back to scene-level IoU."""
    if teaser_event_ids & downstream_event_ids:
        return True
    for te in teaser_event_ids:
        event_a = events.get(te)
        if event_a is None:
            continue
        for de in downstream_event_ids:
            event_b = events.get(de)
            if event_b is None:
                continue
            if _scene_equivalent(event_a, event_b):
                return True
    return False