#!/usr/bin/env python3
"""Compile stable, boundary-aware source-span candidates from Story Evidence."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Iterable

from autocut_core.io import normalize_text, stable_id
from autocut_core.libs._common import rounded
from autocut_core.schema.compat import validate_task_response
from autocut_core.contracts.teaser_contract import (
    TEASER_STITCH_MAX_DURATION_SECONDS,
    TEASER_STITCH_MAX_GAP_SECONDS,
    interval_stitch_diagnostics,
    minimum_stitched_union,
)


from autocut_core.libs.editorial_knowledge import load_knowledge_section

_span_compiler = load_knowledge_section("span_compiler") or {}
SEGMENT_KINDS = set(
    _span_compiler.get("segment_kinds")
    or {"story_beat", "dialogue", "screen_text", "visual_event"}
)
ATOMIC_CONTENT_KINDS = set(
    _span_compiler.get("atomic_content_kinds")
    or {"dialogue", "screen_text", "visual_event"}
)
SPAN_COMPILER_METHOD = "semantic-window-boundary-v4"
TEASER_ATOMIC_STITCH_MAX_GAP_SECONDS = TEASER_STITCH_MAX_GAP_SECONDS
TEASER_ATOMIC_STITCH_MAX_DURATION_SECONDS = (
    TEASER_STITCH_MAX_DURATION_SECONDS
)
SHORT_SOURCE_DURATION_SECONDS = 180.0
DENSE_SHORT_SOURCE_MIN_SEMANTIC_RATIO = 0.75
FULL_SOURCE_LIKE_THRESHOLD = 0.85
FULL_SOURCE_LIKE_RISK = "候选覆盖原集达到 85%，属于整集型 Candidate"


def semantic_density_ratio(
    segments: Iterable[dict[str, Any]], start: float, end: float
) -> float:
    """Measure how much of a Span contains extracted semantic material."""
    duration = float(end) - float(start)
    if duration <= 0:
        return 0.0
    intervals = sorted(
        (
            max(float(start), float(item.get("start", start))),
            min(float(end), float(item.get("end", end))),
        )
        for item in segments
        if isinstance(item, dict)
        and isinstance(item.get("start"), (int, float))
        and isinstance(item.get("end"), (int, float))
        and min(float(end), float(item["end"]))
        > max(float(start), float(item["start"]))
    )
    merged: list[list[float]] = []
    for left, right in intervals:
        if not merged or left > merged[-1][1] + 0.001:
            merged.append([left, right])
        else:
            merged[-1][1] = max(merged[-1][1], right)
    covered = sum(right - left for left, right in merged)
    return round(min(1.0, covered / duration), 3)


def is_full_source_like(
    source_coverage_ratio: float,
    source_duration_seconds: float,
    semantic_density: float,
) -> bool:
    """Avoid treating a short, dense single-scene episode as padding."""
    if float(source_coverage_ratio) < FULL_SOURCE_LIKE_THRESHOLD:
        return False
    short_dense_scene = (
        float(source_duration_seconds) < SHORT_SOURCE_DURATION_SECONDS
        and float(semantic_density)
        >= DENSE_SHORT_SOURCE_MIN_SEMANTIC_RATIO
    )
    return not short_dense_scene


def apply_full_source_like_classification(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Apply the shared classification after all same-range evidence merges."""
    semantic_density = semantic_density_ratio(
        candidate.get("semantic_segment_refs", []),
        float(candidate["start"]),
        float(candidate["end"]),
    )
    full_source_like = is_full_source_like(
        float(candidate["source_coverage_ratio"]),
        float(candidate["source_duration_seconds"]),
        semantic_density,
    )
    candidate["full_source_like"] = full_source_like
    risks = [
        item
        for item in candidate.get("material_risks", [])
        if item != FULL_SOURCE_LIKE_RISK
    ]
    if full_source_like:
        risks.append(FULL_SOURCE_LIKE_RISK)
    candidate["material_risks"] = list(dict.fromkeys(risks))
    return candidate


def _stitched_teaser_atomic_candidate(
    a: dict[str, Any],
    b: dict[str, Any],
    *,
    primary_teaser_id: str,
    gap_seconds: float,
) -> dict[str, Any]:
    """Combine two same-source atomic teaser candidates into a single stitched
    candidate covering ``[a.start, b.end]``. Both inputs must already have
    ``teaser_atomic=True`` and share the same primary highlight owner."""
    stitched_start = float(a["start"])
    stitched_end = float(b["end"])
    stitched_duration = rounded(stitched_end - stitched_start)
    source_duration = float(a["source_duration_seconds"])
    source_coverage_ratio = (
        min(1.0, rounded(stitched_duration / source_duration))
        if source_duration > 0
        else 0.0
    )
    stitched_segments = [
        *a.get("semantic_segment_refs", []),
        *b.get("semantic_segment_refs", []),
    ]
    semantic_density = semantic_density_ratio(
        stitched_segments, stitched_start, stitched_end
    )
    full_source_like = is_full_source_like(
        source_coverage_ratio, source_duration, semantic_density
    )
    stitched_review = (
        "Teaser 拼接原子候选：两段同源高光间隔"
        f" ≤ {TEASER_ATOMIC_STITCH_MAX_GAP_SECONDS:.1f}s，需用视频复核桥接可读性"
    )
    a_review = a.get("boundary_evidence", {}).get("review_reasons", []) or []
    b_review = b.get("boundary_evidence", {}).get("review_reasons", []) or []
    risks = list(
        dict.fromkeys(
            [
                *a.get("material_risks", []),
                *b.get("material_risks", []),
                stitched_review,
            ]
        )
    )
    if full_source_like:
        risks.append(FULL_SOURCE_LIKE_RISK)
    span_id = stable_id(
        "span",
        {"source_id": a["source_id"], "start": stitched_start, "end": stitched_end},
    )
    return {
        "span_candidate_id": span_id,
        "source_id": a["source_id"],
        "episode": a["episode"],
        "start": stitched_start,
        "end": stitched_end,
        "duration_seconds": stitched_duration,
        "source_duration_seconds": rounded(source_duration),
        "source_coverage_ratio": source_coverage_ratio,
        "full_source_like": full_source_like,
        "teaser_atomic": True,
        "teaser_atomic_owner_candidate_id": primary_teaser_id,
        "teaser_atomic_stitched": True,
        "teaser_atomic_stitched_from": sorted(
            [a["span_candidate_id"], b["span_candidate_id"]]
        ),
        "teaser_atomic_stitched_gap_seconds": rounded(gap_seconds),
        "variant_types": ["tight"],
        "provenance_tiers": sorted(
            set(a.get("provenance_tiers", []))
            | set(b.get("provenance_tiers", []))
        ),
        "supports_beat_ids": sorted(
            set(a["supports_beat_ids"]) | set(b["supports_beat_ids"])
        ),
        "supports_thread_beat_ids": sorted(
            set(a["supports_thread_beat_ids"])
            | set(b["supports_thread_beat_ids"])
        ),
        "supports_must_show_ids": sorted(
            set(a["supports_must_show_ids"])
            | set(b["supports_must_show_ids"])
        ),
        "content_roles": sorted(
            set(a["content_roles"]) | set(b["content_roles"])
        ),
        "temporal_positions": sorted(
            set(a["temporal_positions"]) | set(b["temporal_positions"])
        ),
        "continuity_modes": sorted(
            set(a["continuity_modes"]) | set(b["continuity_modes"])
        ),
        "event_ids": sorted(
            set(a["event_ids"]) | set(b["event_ids"])
        ),
        "candidate_ids": sorted(
            set(a["candidate_ids"]) | set(b["candidate_ids"])
        ),
        "anchor_refs": [
            *a.get("anchor_refs", []),
            *b.get("anchor_refs", []),
        ],
        "semantic_segment_refs": stitched_segments,
        "boundary_status": "needs_video_review",
        "boundary_evidence": {
            "start_basis": a.get("boundary_evidence", {}).get(
                "start_basis", "unknown"
            ),
            "end_basis": b.get("boundary_evidence", {}).get(
                "end_basis", "unknown"
            ),
            "starts_mid_sentence_risk": bool(
                a.get("boundary_evidence", {}).get(
                    "starts_mid_sentence_risk", False
                )
            ),
            "ends_mid_sentence_risk": bool(
                b.get("boundary_evidence", {}).get(
                    "ends_mid_sentence_risk", False
                )
            ),
            "starts_mid_scene_risk": bool(
                a.get("boundary_evidence", {}).get(
                    "starts_mid_scene_risk", False
                )
            ),
            "ends_mid_scene_risk": bool(
                b.get("boundary_evidence", {}).get(
                    "ends_mid_scene_risk", False
                )
            ),
            "review_reasons": list(
                dict.fromkeys(
                    [*a_review, *b_review, stitched_review]
                )
            ),
        },
        "material_risks": risks,
    }


def _emit_stitched_teaser_atomic_candidates(
    merged_candidates: dict[tuple[str, float, float], dict[str, Any]],
    beat_to_candidate_keys: dict[str, set[tuple[str, float, float]]],
    *,
    primary_teaser_id: str,
    maximum_span_seconds: float,
) -> int:
    """Add stitched teaser-atomic candidates for the primary highlight.

    从 primary anchor + 同 source 同 beat 的任意 span（不再要求
    对方也是 teaser_atomic）拼接。这样即便只有一个已存在的 atomic
    candidate（例如 3 秒 primary highlight），只要相邻 ±5 秒内有承载必要
    must-show 事件的普通 tight/scene span，也能合成一个覆盖多个 must-show
    的 atomic Span。合成范围仍受 30 秒硬顶和 GAP ≤ 5 秒约束。

    Returns the number of stitched candidates added.
    """
    if not primary_teaser_id:
        return 0

    # 1) 收集所有 primary anchor（teaser_atomic=True + owner=primary_teaser_id）
    anchors_by_beat_source: dict[
        tuple[str, str], list[dict[str, Any]]
    ] = {}
    # 2) 收集所有 same beat + same source 的普通 span 作为扩展池
    extension_pool_by_beat_source: dict[
        tuple[str, str], list[dict[str, Any]]
    ] = {}
    for candidate in merged_candidates.values():
        source_id = candidate.get("source_id")
        if not isinstance(source_id, str):
            continue
        for beat_id in candidate.get("supports_beat_ids", []):
            key = (beat_id, source_id)
            if (
                candidate.get("teaser_atomic")
                and candidate.get("teaser_atomic_owner_candidate_id")
                == primary_teaser_id
            ):
                anchors_by_beat_source.setdefault(key, []).append(candidate)
            extension_pool_by_beat_source.setdefault(key, []).append(
                candidate
            )

    added = 0
    for key, anchors in anchors_by_beat_source.items():
        pool = extension_pool_by_beat_source.get(key, [])
        # 对每个 anchor，尝试跟同池里的每个其他候选拼接
        for anchor in anchors:
            if anchor.get("teaser_atomic_stitched"):
                continue
            for other in pool:
                if other is anchor:
                    continue
                if other.get("teaser_atomic_stitched"):
                    continue
                # rule 5: 该扩展必须真的带来新的 must-show 覆盖，
                # 否则拼接没意义。
                anchor_must = set(anchor.get("supports_must_show_ids", []))
                other_must = set(other.get("supports_must_show_ids", []))
                if not (other_must - anchor_must):
                    continue
                # 拼接方向由时间序决定；保证 a.start <= b.start
                if float(anchor["start"]) <= float(other["start"]):
                    a, b = anchor, other
                else:
                    a, b = other, anchor
                stitch = interval_stitch_diagnostics(
                    float(a["start"]),
                    float(a["end"]),
                    float(b["start"]),
                    float(b["end"]),
                )
                if not stitch["stitchable"]:
                    continue
                gap = float(stitch["gap_seconds"])
                stitched_start = float(stitch["union_start"])
                stitched_end = float(stitch["union_end"])
                stitched_duration = float(stitch["union_duration_seconds"])
                if stitched_duration > maximum_span_seconds + 0.001:
                    continue
                stitch_key = (
                    a["source_id"],
                    float(stitched_start),
                    float(stitched_end),
                )
                stitched = _stitched_teaser_atomic_candidate(
                    a,
                    b,
                    primary_teaser_id=primary_teaser_id,
                    gap_seconds=gap,
                )
                # 确保 owner 始终是 primary_teaser_id（不管 a/b 顺序）
                stitched["teaser_atomic_owner_candidate_id"] = (
                    primary_teaser_id
                )
                was_existing = stitch_key in merged_candidates
                merged_candidates[stitch_key] = merge_candidate(
                    merged_candidates.get(stitch_key), stitched
                )
                for bid in stitched["supports_beat_ids"]:
                    beat_to_candidate_keys.setdefault(bid, set()).add(
                        stitch_key
                    )
                if not was_existing:
                    added += 1
    return added


def _emit_direct_event_teaser_atomic_candidate(
    merged_candidates: dict[tuple[str, float, float], dict[str, Any]],
    beat_to_candidate_keys: dict[str, set[tuple[str, float, float]]],
    *,
    teaser_beat: dict[str, Any],
    primary_teaser_id: str,
    candidate_by_id: dict[str, dict[str, Any]],
    source_by_id: dict[str, dict[str, Any]],
    maximum_span_seconds: float,
) -> int:
    """Compile the atomic Teaser from the same direct Event ranges as preflight.

    Semantic tight spans may trim a direct Event by a fraction of a second.
    Using those trimmed boundaries to decide stitchability makes Script
    preflight and Span compilation disagree.  This compiler therefore solves
    the physical union from the primary Candidate plus one direct Event range
    per must-show, then either creates that stable Span or enriches an existing
    Span with atomic provenance.
    """
    primary = candidate_by_id.get(primary_teaser_id)
    if not isinstance(primary, dict):
        return 0
    source_id = primary.get("source_id")
    primary_start = primary.get("start")
    primary_end = primary.get("end")
    if (
        not isinstance(source_id, str)
        or not isinstance(primary_start, (int, float))
        or not isinstance(primary_end, (int, float))
    ):
        return 0
    primary_refs = [
        item
        for item in teaser_beat.get("candidate_range_refs", [])
        if item.get("origin_id") == primary_teaser_id
        and item.get("source_id") == source_id
    ]
    if not primary_refs:
        return 0
    direct_refs = [
        item
        for item in teaser_beat.get("direct_range_refs", [])
        if item.get("origin") == "event"
        and item.get("source_id") == source_id
    ]
    refs_by_event: dict[str, list[dict[str, Any]]] = {}
    for ref in direct_refs:
        origin_id = ref.get("origin_id")
        if isinstance(origin_id, str):
            refs_by_event.setdefault(origin_id, []).append(ref)

    interval_groups: list[list[tuple[float, float, str]]] = []
    group_keys: set[tuple[tuple[float, float, str], ...]] = set()
    must_show_ids: list[str] = []
    primary_event_ids = {
        item
        for item in primary.get("event_ids", [])
        if isinstance(item, str)
    }
    for must_show in teaser_beat.get("must_show_evidence", []):
        must_show_id = must_show.get("must_show_id")
        requested_event_ids = [
            item
            for item in must_show.get("requested_event_ids", [])
            if isinstance(item, str)
        ]
        group_values = {
                (
                    float(ref["start"]),
                    float(ref["end"]),
                    event_id,
                )
                for event_id in requested_event_ids
                for ref in refs_by_event.get(event_id, [])
                if isinstance(ref.get("start"), (int, float))
                and isinstance(ref.get("end"), (int, float))
            }
        group_values.update(
            (
                float(primary_start),
                float(primary_end),
                event_id,
            )
            for event_id in requested_event_ids
            if event_id in primary_event_ids
        )
        group = sorted(group_values)
        if not group:
            return 0
        key = tuple(group)
        if key not in group_keys:
            group_keys.add(key)
            interval_groups.append(group)
        if isinstance(must_show_id, str):
            must_show_ids.append(must_show_id)

    union = minimum_stitched_union(
        (float(primary_start), float(primary_end)),
        interval_groups,
        maximum_duration_seconds=min(
            maximum_span_seconds, TEASER_ATOMIC_STITCH_MAX_DURATION_SECONDS
        ),
    )
    if union is None:
        return 0
    start = float(union["start"])
    end = float(union["end"])
    source = source_by_id.get(source_id, {})
    source_duration = float(source.get("duration_seconds", 0.0) or 0.0)
    duration = rounded(end - start)
    source_coverage_ratio = (
        min(1.0, rounded(duration / source_duration))
        if source_duration > 0
        else 0.0
    )
    selected_event_ids = {
        item["origin_id"] for item in union["selected_intervals"]
    }
    selected_refs = [
        ref
        for ref in direct_refs
        if ref.get("origin_id") in selected_event_ids
        and any(
            abs(float(ref["start"]) - float(item["start"])) <= 0.001
            and abs(float(ref["end"]) - float(item["end"])) <= 0.001
            and ref.get("origin_id") == item["origin_id"]
            for item in union["selected_intervals"]
        )
    ]
    contributors = [
        candidate
        for candidate in merged_candidates.values()
        if candidate.get("source_id") == source_id
        and float(candidate.get("end", 0.0)) > start
        and float(candidate.get("start", 0.0)) < end
        and (
            primary_teaser_id in candidate.get("candidate_ids", [])
            or selected_event_ids.intersection(
                candidate.get("event_ids", [])
            )
        )
    ]
    semantic_segments = {
        item["segment_id"]: item
        for candidate in contributors
        for item in candidate.get("semantic_segment_refs", [])
        if isinstance(item, dict) and isinstance(item.get("segment_id"), str)
    }
    stitched_from = sorted(
        {
            candidate["span_candidate_id"]
            for candidate in contributors
            if isinstance(candidate.get("span_candidate_id"), str)
        }
    )
    primary_ref = primary_refs[0]
    normalized_anchor_refs = [
        {
            "origin": item["origin"],
            "origin_id": item["origin_id"],
            "start": item["start"],
            "end": item["end"],
            "evidence_window_ids": item.get("evidence_window_ids", []),
        }
        for item in [primary_ref, *selected_refs]
    ]
    anchor_refs = {
        (
            item["origin"],
            item["origin_id"],
            item["start"],
            item["end"],
        ): item
        for item in normalized_anchor_refs
    }
    risk = (
        "Teaser direct-event atomic union：按 Script preflight 的直接 Event "
        "物理范围编译，需视频复核桥接与真实动作边界"
    )
    semantic_segment_values = sorted(
        semantic_segments.values(),
        key=lambda item: (
            item["start"],
            item["end"],
            item["kind"],
            item["segment_id"],
        ),
    )
    semantic_density = semantic_density_ratio(
        semantic_segment_values, start, end
    )
    full_source_like = is_full_source_like(
        source_coverage_ratio, source_duration, semantic_density
    )
    raw_candidate = {
        "span_candidate_id": stable_id(
            "span", {"source_id": source_id, "start": start, "end": end}
        ),
        "source_id": source_id,
        "episode": int(primary_ref.get("episode", primary.get("episode", 0))),
        "start": start,
        "end": end,
        "duration_seconds": duration,
        "source_duration_seconds": rounded(source_duration),
        "source_coverage_ratio": source_coverage_ratio,
        "full_source_like": full_source_like,
        "teaser_atomic": True,
        "teaser_atomic_owner_candidate_id": primary_teaser_id,
        "teaser_atomic_stitched": bool(
            abs(start - float(primary_start)) > 0.001
            or abs(end - float(primary_end)) > 0.001
        ),
        "teaser_atomic_stitched_from": stitched_from,
        "teaser_atomic_stitched_gap_seconds": rounded(
            float(union["maximum_gap_seconds"])
        ),
        "variant_types": ["tight"],
        "provenance_tiers": ["candidate", "direct"],
        "supports_beat_ids": [teaser_beat["beat_id"]],
        "supports_thread_beat_ids": sorted(
            item
            for item in teaser_beat.get("resolved_thread_beat_ids", [])
            if isinstance(item, str)
        ),
        "supports_must_show_ids": sorted(set(must_show_ids)),
        "content_roles": ["teaser_intent"],
        "temporal_positions": [
            teaser_beat.get("temporal_position", "future_preview")
        ],
        "continuity_modes": [teaser_beat.get("continuity", "continuous_scene")],
        "event_ids": sorted(selected_event_ids),
        "candidate_ids": [primary_teaser_id],
        "anchor_refs": sorted(
            anchor_refs.values(),
            key=lambda item: (
                item["start"],
                item["end"],
                item["origin"],
                item["origin_id"],
            ),
        ),
        "semantic_segment_refs": semantic_segment_values,
        "boundary_status": "needs_video_review",
        "boundary_evidence": {
            "start_basis": "anchor_padding",
            "end_basis": "anchor_padding",
            "starts_mid_sentence_risk": True,
            "ends_mid_sentence_risk": True,
            "starts_mid_scene_risk": True,
            "ends_mid_scene_risk": True,
            "review_reasons": [risk],
        },
        "material_risks": [risk],
    }
    key = (source_id, start, end)
    was_existing = key in merged_candidates
    merged_candidates[key] = merge_candidate(
        merged_candidates.get(key), raw_candidate
    )
    beat_to_candidate_keys.setdefault(teaser_beat["beat_id"], set()).add(key)
    return 0 if was_existing else 1


def span_filename(story_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", story_id):
        return f"{story_id}.json"
    return f"{stable_id('span-bundle', story_id)}.json"


def valid_range(start: Any, end: Any) -> bool:
    return (
        isinstance(start, (int, float))
        and not isinstance(start, bool)
        and isinstance(end, (int, float))
        and not isinstance(end, bool)
        and float(end) > float(start)
    )


def semantic_segments(packet: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for window in packet["evidence_catalog"]["windows"]:
        source_id = window["source_id"]
        window_id = window["window_id"]
        records: list[tuple[str, dict[str, Any], str]] = []
        for item in window.get("story_beats", []):
            records.append(
                (
                    "story_beat",
                    item,
                    normalize_text(item.get("summary"))
                    or normalize_text(item.get("function"))
                    or "剧情节拍",
                )
            )
        for item in window.get("dialogue_and_text", []):
            kind = item.get("kind")
            if kind in {"dialogue", "screen_text"}:
                records.append(
                    (
                        kind,
                        item,
                        normalize_text(item.get("text")) or "对白或屏幕文字",
                    )
                )
        for item in window.get("visual_events", []):
            records.append(
                (
                    "visual_event",
                    item,
                    normalize_text(item.get("description")) or "视觉动作",
                )
            )
        for kind, item, content in records:
            start, end = item.get("start"), item.get("end")
            if kind not in SEGMENT_KINDS or not valid_range(start, end):
                continue
            payload = {
                "source_id": source_id,
                "kind": kind,
                "start": rounded(float(start)),
                "end": rounded(float(end)),
                "content_summary": content,
            }
            segment_id = stable_id("segment", payload)
            current = grouped.get(segment_id)
            if current is None:
                grouped[segment_id] = {
                    "segment_id": segment_id,
                    "source_id": source_id,
                    "window_ids": [window_id],
                    "kind": kind,
                    "start": payload["start"],
                    "end": payload["end"],
                    "content_summary": content,
                }
            else:
                current["window_ids"] = sorted(
                    set(current["window_ids"]) | {window_id}
                )
    return sorted(
        grouped.values(),
        key=lambda item: (
            item["source_id"],
            float(item["start"]),
            float(item["end"]),
            item["kind"],
            item["segment_id"],
        ),
    )


def merge_ranges(
    ranges: Iterable[tuple[float, float]], *, gap: float = 0.0
) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if not merged or start > merged[-1][1] + gap:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(rounded(start), rounded(end)) for start, end in merged]


def allowed_components(packet: dict[str, Any]) -> dict[str, list[tuple[float, float]]]:
    grouped: dict[str, list[tuple[float, float]]] = {}
    for window in packet["evidence_catalog"]["windows"]:
        start = window["window"]["start"]
        end = window["window"]["end"]
        if valid_range(start, end):
            grouped.setdefault(window["source_id"], []).append(
                (float(start), float(end))
            )
    return {
        source_id: merge_ranges(ranges, gap=0.01)
        for source_id, ranges in grouped.items()
    }


def containing_component(
    source_id: str,
    start: float,
    end: float,
    *,
    components: dict[str, list[tuple[float, float]]],
    source_duration: float,
) -> tuple[float, float]:
    matches = [
        item
        for item in components.get(source_id, [])
        if min(end, item[1]) - max(start, item[0]) > 0
        or (item[0] <= start <= item[1])
        or (item[0] <= end <= item[1])
    ]
    if matches:
        return min(item[0] for item in matches), max(item[1] for item in matches)
    return 0.0, source_duration


def group_anchor_refs(
    range_refs: list[dict[str, Any]], *, merge_gap: float
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    ordered = sorted(
        range_refs,
        key=lambda item: (
            item["source_id"],
            float(item["start"]),
            float(item["end"]),
            item["origin"],
            item["origin_id"],
        ),
    )
    for item in ordered:
        if not valid_range(item.get("start"), item.get("end")):
            continue
        ref = {
            "origin": item["origin"],
            "origin_id": item["origin_id"],
            "start": rounded(float(item["start"])),
            "end": rounded(float(item["end"])),
            "evidence_window_ids": sorted(set(item["evidence_window_ids"])),
        }
        if (
            groups
            and groups[-1]["source_id"] == item["source_id"]
            and float(item["start"]) <= groups[-1]["end"] + merge_gap
        ):
            groups[-1]["start"] = min(groups[-1]["start"], float(item["start"]))
            groups[-1]["end"] = max(groups[-1]["end"], float(item["end"]))
            groups[-1]["refs"].append(ref)
        else:
            groups.append(
                {
                    "source_id": item["source_id"],
                    "episode": item["episode"],
                    "start": float(item["start"]),
                    "end": float(item["end"]),
                    "refs": [ref],
                }
            )
    for group in groups:
        group["start"] = rounded(group["start"])
        group["end"] = rounded(group["end"])
        unique_refs = {
            (
                item["origin"],
                item["origin_id"],
                item["start"],
                item["end"],
            ): item
            for item in group["refs"]
        }
        group["refs"] = sorted(
            unique_refs.values(),
            key=lambda item: (
                item["start"],
                item["end"],
                item["origin"],
                item["origin_id"],
            ),
        )
    return groups


def expand_with_segments(
    start: float,
    end: float,
    *,
    segments: list[dict[str, Any]],
    kinds: set[str],
    gap_before: float,
    gap_after: float,
    lower: float,
    upper: float,
) -> tuple[float, float]:
    # Use the immutable seed envelope for membership. Re-evaluating against
    # the growing range turns a chain of adjacent semantic records into an
    # accidental whole-episode span.
    seed_start = max(lower, start)
    seed_end = min(upper, end)
    selected = [
        segment
        for segment in segments
        if segment["kind"] in kinds
        and float(segment["end"]) >= seed_start - gap_before
        and float(segment["start"]) <= seed_end + gap_after
    ]
    if not selected:
        return rounded(seed_start), rounded(seed_end)
    return (
        rounded(
            max(
                lower,
                min(seed_start, *(float(item["start"]) for item in selected)),
            )
        ),
        rounded(
            min(
                upper,
                max(seed_end, *(float(item["end"]) for item in selected)),
            )
        ),
    )


def pad_range(
    start: float,
    end: float,
    *,
    padding: float,
    lower: float,
    upper: float,
    scene_cut_points: list[float] | None = None,
    lead_in: float = 0.0,
    lead_out: float = 0.0,
    anchor_start: float | None = None,
    anchor_end: float | None = None,
) -> tuple[float, float]:
    """向两侧各加 padding 秒，可选约束不跨越场景边界。

    当 scene_cut_points 不为空时，padding 不会越过 anchor 所在场景的边界。
    使用 anchor_start/anchor_end 进行 bisect 定位场景，避免语义扩展
    跨越切点后找到错误的场景边界。
    
    - 如果提供了 anchor_start/anchor_end，使用它们定位 anchor 所在场景，
      padding 不会越过该场景的边界（加 lead_in/lead_out 偏移）。
    - 如果未提供 anchor，使用 start/end 定位最近场景边界，允许 span 跨
      越多个场景，但在边缘处避免转场残影。

    Args:
        start: 起始时间
        end: 结束时间
        padding: 两侧 padding 秒数
        lower: 下界
        upper: 上界
        scene_cut_points: 场景切点列表（可选）
        lead_in: 场景切点后的偏移量，跳过转场残影（默认 0）
        lead_out: 场景切点前的偏移量（默认 0）
        anchor_start: anchor 起始时间，用于定位场景（可选，默认使用 start）
        anchor_end: anchor 结束时间，用于定位场景（可选，默认使用 end）

    Returns:
        (padded_start, padded_end) 元组
    """
    padded_start = max(lower, start - padding)
    padded_end = min(upper, end + padding)

    if scene_cut_points:
        import bisect
        # 使用 anchor 坐标定位场景（如果提供），否则使用当前 start/end
        ref_start = anchor_start if anchor_start is not None else start
        ref_end = anchor_end if anchor_end is not None else end
        
        # 定位ref_start和ref_end所在场景的切点索引
        idx_start = bisect.bisect_right(scene_cut_points, ref_start)
        idx_end = bisect.bisect_left(scene_cut_points, ref_end)
        
        # BUGFIX: 只有当anchor完全落在单个场景内（idx_start == idx_end）时，才约束padding不跨切点
        # 如果anchor本身跨场景切点（idx_start < idx_end），不做场景截断，避免错误切掉内容
        if idx_start == idx_end:
            # start 侧: 找 <= ref_start 的最近切点作为下界，加上 lead_in 避免转场残影
            if idx_start > 0:
                scene_lower = scene_cut_points[idx_start - 1]
                # 只有当ref_start不是恰好落在切点上时，才加lead_in偏移（避免切点上错误切掉0.3s开头）
                if abs(ref_start - scene_lower) > 0.05:
                    scene_lower += lead_in
                padded_start = max(padded_start, scene_lower)
            # end 侧: 找 >= ref_end 的最近切点作为上界，减去 lead_out
            if idx_end < len(scene_cut_points):
                scene_upper = scene_cut_points[idx_end]
                # 只有当ref_end不是恰好落在切点上时，才减lead_out偏移
                if abs(ref_end - scene_upper) > 0.05:
                    scene_upper -= lead_out
                padded_end = min(padded_end, scene_upper)

    return rounded(padded_start), rounded(padded_end)


def cap_span(
    start: float,
    end: float,
    *,
    anchor_start: float,
    anchor_end: float,
    maximum_seconds: float,
) -> tuple[float, float, bool]:
    if end - start <= maximum_seconds:
        return rounded(start), rounded(end), False
    anchor_duration = anchor_end - anchor_start
    if anchor_duration >= maximum_seconds:
        return rounded(anchor_start), rounded(anchor_end), True
    remaining = maximum_seconds - anchor_duration
    left_available = anchor_start - start
    right_available = end - anchor_end
    left = min(left_available, remaining / 2)
    right = min(right_available, remaining - left)
    if left + right < remaining:
        left = min(left_available, remaining - right)
    return (
        rounded(anchor_start - left),
        rounded(anchor_end + right),
        True,
    )


def variant_ranges(
    group: dict[str, Any],
    *,
    segments: list[dict[str, Any]],
    component: tuple[float, float],
    continuity: str,
    tight_padding: float,
    scene_padding: float,
    reaction_tail: float,
    maximum_context_extension: float,
    maximum_span_seconds: float,
    scene_cut_points: list[float] | None = None,
    lead_in: float = 0.0,
    lead_out: float = 0.0,
) -> list[tuple[str, float, float, bool]]:
    anchor_start = float(group["start"])
    anchor_end = float(group["end"])
    lower, upper = component
    tight_lower = max(lower, anchor_start - max(8.0, reaction_tail + 2.0))
    tight_upper = min(upper, anchor_end + max(8.0, reaction_tail + 2.0))
    tight_start, tight_end = expand_with_segments(
        anchor_start,
        anchor_end,
        segments=segments,
        kinds={"dialogue", "screen_text", "visual_event"},
        gap_before=1.5,
        # Do not absorb the next semantic unit merely because it begins
        # inside the reaction-tail window.  The tight span must end at the
        # current event boundary; reaction padding is applied only after the
        # membership pass and cannot pull in the next event.
        gap_after=0.0,
        lower=tight_lower,
        upper=tight_upper,
    )
    tight_start, tight_end = pad_range(
        tight_start,
        tight_end,
        padding=tight_padding,
        lower=lower,
        upper=upper,
        scene_cut_points=scene_cut_points,
        lead_in=lead_in,
        lead_out=lead_out,
        anchor_start=anchor_start,
        anchor_end=anchor_end,
    )
    tight_start, tight_end, tight_capped = cap_span(
        tight_start,
        tight_end,
        anchor_start=anchor_start,
        anchor_end=anchor_end,
        maximum_seconds=maximum_span_seconds,
    )

    scene_lower = max(lower, anchor_start - 30.0)
    scene_upper = min(upper, anchor_end + 30.0)
    scene_start, scene_end = expand_with_segments(
        tight_start,
        tight_end,
        segments=segments,
        kinds=SEGMENT_KINDS,
        gap_before=3.0,
        gap_after=max(3.0, reaction_tail),
        lower=scene_lower,
        upper=scene_upper,
    )
    scene_start, scene_end = pad_range(
        scene_start,
        scene_end,
        padding=scene_padding,
        lower=lower,
        upper=upper,
        scene_cut_points=scene_cut_points,
        lead_in=lead_in,
        lead_out=lead_out,
        anchor_start=anchor_start,
        anchor_end=anchor_end,
    )
    scene_start, scene_end, scene_capped = cap_span(
        scene_start,
        scene_end,
        anchor_start=anchor_start,
        anchor_end=anchor_end,
        maximum_seconds=maximum_span_seconds,
    )

    continuity_extension = {
        "continuous_scene": min(maximum_context_extension, 30.0),
        "causal_chain": maximum_context_extension,
        "montage_allowed": min(maximum_context_extension, 15.0),
    }[continuity]
    context_lower = max(lower, anchor_start - continuity_extension)
    context_upper = min(upper, anchor_end + continuity_extension)
    context_gap = {
        "continuous_scene": 6.0,
        "causal_chain": 12.0,
        "montage_allowed": 3.0,
    }[continuity]
    context_start, context_end = expand_with_segments(
        scene_start,
        scene_end,
        segments=segments,
        kinds=SEGMENT_KINDS,
        gap_before=context_gap,
        gap_after=context_gap,
        lower=context_lower,
        upper=context_upper,
    )
    context_start, context_end = pad_range(
        context_start,
        context_end,
        padding=scene_padding,
        lower=lower,
        upper=upper,
        scene_cut_points=scene_cut_points,
        lead_in=lead_in,
        lead_out=lead_out,
    )
    context_start, context_end, context_capped = cap_span(
        context_start,
        context_end,
        anchor_start=anchor_start,
        anchor_end=anchor_end,
        maximum_seconds=maximum_span_seconds,
    )
    return [
        ("tight", tight_start, tight_end, tight_capped),
        ("scene", scene_start, scene_end, scene_capped),
        ("context", context_start, context_end, context_capped),
    ]


def candidate_atomic_tight_range(
    group: dict[str, Any],
    *,
    segments: list[dict[str, Any]],
    component: tuple[float, float],
    tight_padding: float,
    reaction_tail: float,
    maximum_span_seconds: float,
    scene_cut_points: list[float] | None = None,
    lead_in: float = 0.0,
    lead_out: float = 0.0,
) -> tuple[str, float, float, bool]:
    """Compile a Highlight anchor without pulling in the next story unit."""
    anchor_start = float(group["start"])
    anchor_end = float(group["end"])
    lower, upper = component
    overlapping = [
        item
        for item in segments
        if item["kind"] in ATOMIC_CONTENT_KINDS
        and min(anchor_end, float(item["end"]))
        - max(anchor_start, float(item["start"]))
        > 0
    ]
    content_start = min(
        [anchor_start, *(float(item["start"]) for item in overlapping)]
    )
    content_end = max(
        [anchor_end, *(float(item["end"]) for item in overlapping)]
    )

    # Padding and a short reaction tail must not cross a known semantic
    # boundary into the next story unit. The anchor and every directly
    # overlapping content segment remain complete.
    previous_edges = [
        float(item["end"])
        for item in segments
        if float(item["end"]) <= content_start
    ]
    next_edges = [
        float(item["start"])
        for item in segments
        if float(item["start"]) >= content_end
    ]
    padding_lower = max(
        lower,
        max(previous_edges) if previous_edges else lower,
    )
    padding_upper = min(
        upper,
        min(next_edges) if next_edges else upper,
    )

    # 场景边界约束: padding 不跨越场景切点
    if scene_cut_points:
        import bisect
        idx = bisect.bisect_right(scene_cut_points, anchor_start)
        if idx > 0:
            padding_lower = max(padding_lower, scene_cut_points[idx - 1] + lead_in)
        idx2 = bisect.bisect_left(scene_cut_points, anchor_end)
        if idx2 < len(scene_cut_points):
            padding_upper = min(padding_upper, scene_cut_points[idx2] - lead_out)

    atomic_start = max(
        padding_lower,
        content_start - tight_padding,
    )
    atomic_end = min(
        padding_upper,
        content_end + tight_padding + min(2.0, reaction_tail),
    )
    atomic_start, atomic_end, capped = cap_span(
        atomic_start,
        atomic_end,
        anchor_start=anchor_start,
        anchor_end=anchor_end,
        maximum_seconds=maximum_span_seconds,
    )
    return "tight", atomic_start, atomic_end, capped


def segments_overlapping(
    segments: list[dict[str, Any]], start: float, end: float
) -> list[dict[str, Any]]:
    return [
        item
        for item in segments
        if min(end, float(item["end"])) - max(start, float(item["start"])) > 0
    ]


def boundary_basis(
    value: float,
    *,
    side: str,
    segments: list[dict[str, Any]],
    source_duration: float,
    component: tuple[float, float],
) -> str:
    tolerance = 0.25
    if side == "start" and abs(value) <= tolerance:
        return "source_boundary"
    if side == "end" and abs(value - source_duration) <= tolerance:
        return "source_boundary"
    edge = component[0] if side == "start" else component[1]
    if abs(value - edge) <= tolerance:
        return "window_limit"
    priority = {
        "dialogue": 0,
        "screen_text": 1,
        "story_beat": 2,
        "visual_event": 3,
    }
    matches = []
    for segment in segments:
        segment_edge = (
            float(segment["start"]) if side == "start" else float(segment["end"])
        )
        if abs(value - segment_edge) <= tolerance:
            matches.append(segment["kind"])
    if matches:
        kind = sorted(matches, key=lambda item: priority[item])[0]
        return f"{kind}_boundary"
    return "anchor_padding"


def boundary_analysis(
    *,
    start: float,
    end: float,
    segments: list[dict[str, Any]],
    packet_windows: list[dict[str, Any]],
    source_id: str,
    source_duration: float,
    component: tuple[float, float],
    beat: dict[str, Any],
    capped: bool,
) -> tuple[str, dict[str, Any], list[str]]:
    tolerance = 0.05
    dialogues = [
        item for item in segments if item["kind"] in {"dialogue", "screen_text"}
    ]
    starts_mid_sentence = any(
        float(item["start"]) + tolerance < start < float(item["end"]) - tolerance
        for item in dialogues
    )
    ends_mid_sentence = any(
        float(item["start"]) + tolerance < end < float(item["end"]) - tolerance
        for item in dialogues
    )
    starts_mid_scene = False
    ends_mid_scene = False
    for window in packet_windows:
        if window["source_id"] != source_id:
            continue
        window_start = float(window["window"]["start"])
        window_end = float(window["window"]["end"])
        context = window["boundary_context"]
        if abs(start - window_start) <= 0.25 and context["starts_mid_scene"]:
            starts_mid_scene = True
        if abs(end - window_end) <= 0.25 and context["ends_mid_scene"]:
            ends_mid_scene = True
    reasons: list[str] = []
    if starts_mid_sentence:
        reasons.append("候选起点仍落在对白或屏幕文字片段内部")
    if ends_mid_sentence:
        reasons.append("候选终点仍落在对白或屏幕文字片段内部")
    if starts_mid_scene:
        reasons.append("候选起点贴近被标记为场景中段的窗口边界")
    if ends_mid_scene:
        reasons.append("候选终点贴近被标记为场景中段的窗口边界")
    if not segments:
        reasons.append("锚点附近没有可用于扩边的对白、动作或 Story Beat")
    if any(item["kind"] == "screen_text" for item in segments):
        reasons.append("屏幕文字可读性和停留时长需要视频复核")
    if beat["retrieval_status"] in {
        "partial",
        "missing",
        "needs_video_review",
    }:
        reasons.append(
            f"继承 Beat 的检索状态：{beat['retrieval_status']}"
        )
    if beat["temporal_position"] in {"future_preview", "parallel"}:
        reasons.append("非线性时间位置需要复核进入点与返回点")
    if beat["continuity"] == "continuous_scene":
        reasons.append("continuous_scene 要求用连续视频确认真实场景头尾")
    if capped:
        reasons.append("候选超过最大 Span 时长，已在保留锚点的前提下截短")
    reasons.extend(
        f"继承 Evidence 风险：{item}"
        for item in beat.get("material_risks", [])
        if isinstance(item, str) and item
    )
    reasons = list(dict.fromkeys(reasons))
    evidence = {
        "start_basis": boundary_basis(
            start,
            side="start",
            segments=segments,
            source_duration=source_duration,
            component=component,
        ),
        "end_basis": boundary_basis(
            end,
            side="end",
            segments=segments,
            source_duration=source_duration,
            component=component,
        ),
        "starts_mid_sentence_risk": starts_mid_sentence,
        "ends_mid_sentence_risk": ends_mid_sentence,
        "starts_mid_scene_risk": starts_mid_scene,
        "ends_mid_scene_risk": ends_mid_scene,
        "review_reasons": reasons,
    }
    status = "needs_video_review" if reasons else "proposed"
    return status, evidence, reasons


def must_show_support(
    beat: dict[str, Any], event_ids: set[str]
) -> list[str]:
    return sorted(
        item["must_show_id"]
        for item in beat["must_show_evidence"]
        if event_ids
        & {
            value
            for value in item["direct_event_ids"]
            if isinstance(value, str)
        }
    )


def candidate_must_show_support(
    candidate: dict[str, Any],
    beats_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    if not set(candidate.get("provenance_tiers", [])).intersection(
        {"direct", "candidate"}
    ):
        return []
    event_ids = {
        item
        for item in candidate.get("event_ids", [])
        if isinstance(item, str)
    }
    return sorted(
        {
            must_show_id
            for beat_id in candidate.get("supports_beat_ids", [])
            if beat_id in beats_by_id
            for must_show_id in must_show_support(
                beats_by_id[beat_id], event_ids
            )
        }
    )


def thread_beat_support(
    beat: dict[str, Any],
    event_ids: set[str],
    thread_beats: dict[str, dict[str, Any]],
) -> list[str]:
    return sorted(
        thread_beat_id
        for thread_beat_id in beat.get("resolved_thread_beat_ids", [])
        if thread_beat_id in thread_beats
        and event_ids & set(thread_beats[thread_beat_id].get("event_ids", []))
    )


def anchor_identity_sets(
    refs: list[dict[str, Any]],
    *,
    candidates: dict[str, dict[str, Any]],
) -> tuple[set[str], set[str]]:
    event_ids = {
        item["origin_id"] for item in refs if item["origin"] == "event"
    }
    candidate_ids = {
        item["origin_id"] for item in refs if item["origin"] == "candidate"
    }
    for candidate_id in candidate_ids:
        event_ids.update(candidates[candidate_id].get("event_ids", []))
    return event_ids, candidate_ids


def merge_candidate(
    existing: dict[str, Any] | None, candidate: dict[str, Any]
) -> dict[str, Any]:
    if existing is None:
        return apply_full_source_like_classification(candidate)
    if candidate.get("teaser_atomic"):
        existing["teaser_atomic"] = True
        existing["teaser_atomic_owner_candidate_id"] = candidate.get(
            "teaser_atomic_owner_candidate_id", ""
        )
    if candidate.get("teaser_atomic_stitched"):
        existing["teaser_atomic_stitched"] = True
        existing["teaser_atomic_stitched_from"] = sorted(
            set(existing.get("teaser_atomic_stitched_from", []))
            | set(candidate.get("teaser_atomic_stitched_from", []))
        )
        candidate_gap = float(
            candidate.get("teaser_atomic_stitched_gap_seconds", 0.0) or 0.0
        )
        existing_gap = float(
            existing.get("teaser_atomic_stitched_gap_seconds", 0.0) or 0.0
        )
        existing["teaser_atomic_stitched_gap_seconds"] = rounded(
            max(existing_gap, candidate_gap)
        )
    for field in (
        "variant_types",
        "provenance_tiers",
        "supports_beat_ids",
        "supports_thread_beat_ids",
        "supports_must_show_ids",
        "content_roles",
        "temporal_positions",
        "continuity_modes",
        "event_ids",
        "candidate_ids",
        "material_risks",
    ):
        existing[field] = sorted(
            set(existing.get(field, [])) | set(candidate.get(field, []))
        )
    anchors = {
        (
            item["origin"],
            item["origin_id"],
            item["start"],
            item["end"],
        ): item
        for item in [*existing["anchor_refs"], *candidate["anchor_refs"]]
    }
    existing["anchor_refs"] = sorted(
        anchors.values(),
        key=lambda item: (
            item["start"],
            item["end"],
            item["origin"],
            item["origin_id"],
        ),
    )
    segments = {
        item["segment_id"]: item
        for item in [
            *existing["semantic_segment_refs"],
            *candidate["semantic_segment_refs"],
        ]
    }
    existing["semantic_segment_refs"] = sorted(
        segments.values(),
        key=lambda item: (
            item["start"],
            item["end"],
            item["kind"],
            item["segment_id"],
        ),
    )
    left = existing["boundary_evidence"]
    right = candidate["boundary_evidence"]
    for field in (
        "starts_mid_sentence_risk",
        "ends_mid_sentence_risk",
        "starts_mid_scene_risk",
        "ends_mid_scene_risk",
    ):
        left[field] = bool(left[field] or right[field])
    left["review_reasons"] = sorted(
        set(left["review_reasons"]) | set(right["review_reasons"])
    )
    if candidate["boundary_status"] == "needs_video_review":
        existing["boundary_status"] = "needs_video_review"
    return apply_full_source_like_classification(existing)


def continuous_scene_union_candidates(
    records: list[dict[str, Any]],
    *,
    maximum_span_seconds: float,
) -> list[dict[str, Any]]:
    """Aggregate overlapping continuous-scene groups without inventing claims."""

    eligible = [
        item
        for item in records
        if item["continuity"] == "continuous_scene"
        and set(item["candidate"]["variant_types"]) & {"scene", "context"}
    ]
    grouped: dict[
        tuple[str, tuple[float, float]], dict[str, dict[str, Any]]
    ] = {}
    for record in eligible:
        component = tuple(rounded(value) for value in record["component"])
        group_key = (record["source_id"], component)
        node = grouped.setdefault(group_key, {}).setdefault(
            record["anchor_group_id"],
            {
                "anchor_group_id": record["anchor_group_id"],
                "start": float(record["candidate"]["start"]),
                "end": float(record["candidate"]["end"]),
                "members": [],
            },
        )
        node["start"] = min(node["start"], float(record["candidate"]["start"]))
        node["end"] = max(node["end"], float(record["candidate"]["end"]))
        node["members"].append(record["candidate"])

    unions: list[dict[str, Any]] = []
    aggregation_reason = (
        "continuous_scene 聚合候选由多个重叠扩边锚点组编译，"
        "仍需连续视频确认真实场景头尾"
    )
    for (source_id, _component), nodes_by_id in sorted(grouped.items()):
        nodes = sorted(
            nodes_by_id.values(),
            key=lambda item: (
                float(item["start"]),
                float(item["end"]),
                item["anchor_group_id"],
            ),
        )
        unseen = set(range(len(nodes)))
        while unseen:
            pending = [min(unseen)]
            unseen.remove(pending[0])
            component_indexes: list[int] = []
            while pending:
                current_index = pending.pop()
                component_indexes.append(current_index)
                current = nodes[current_index]
                neighbors = sorted(
                    index
                    for index in unseen
                    if min(
                        float(current["end"]), float(nodes[index]["end"])
                    )
                    - max(
                        float(current["start"]), float(nodes[index]["start"])
                    )
                    > 0
                )
                for index in neighbors:
                    unseen.remove(index)
                    pending.append(index)
            component_nodes = [nodes[index] for index in component_indexes]
            if len(component_nodes) < 2:
                continue
            start = rounded(
                min(float(item["start"]) for item in component_nodes)
            )
            end = rounded(max(float(item["end"]) for item in component_nodes))
            if end - start > maximum_span_seconds:
                continue

            node_claims: list[set[tuple[str, str]]] = []
            for node in component_nodes:
                claims = {
                    ("beat", value)
                    for member in node["members"]
                    for value in member["supports_beat_ids"]
                }
                claims.update(
                    ("must_show", value)
                    for member in node["members"]
                    for value in member["supports_must_show_ids"]
                )
                claims.update(
                    ("thread_beat", value)
                    for member in node["members"]
                    for value in member.get(
                        "supports_thread_beat_ids", []
                    )
                )
                node_claims.append(claims)
            combined_claims = set().union(*node_claims)
            if not combined_claims or any(
                combined_claims <= claims for claims in node_claims
            ):
                continue

            members = sorted(
                (
                    member
                    for node in component_nodes
                    for member in node["members"]
                ),
                key=lambda item: (
                    float(item["start"]),
                    float(item["end"]),
                    item["span_candidate_id"],
                ),
            )

            def merged_values(field: str) -> list[str]:
                return sorted(
                    {
                        value
                        for member in members
                        for value in member.get(field, [])
                    }
                )

            anchors = {
                (
                    item["origin"],
                    item["origin_id"],
                    item["start"],
                    item["end"],
                ): item
                for member in members
                for item in member["anchor_refs"]
            }
            segments = {
                item["segment_id"]: item
                for member in members
                for item in member["semantic_segment_refs"]
            }
            left_edges = [
                item for item in members if float(item["start"]) == start
            ]
            right_edges = [
                item for item in members if float(item["end"]) == end
            ]
            review_reasons = sorted(
                {
                    reason
                    for member in members
                    for reason in member["boundary_evidence"][
                        "review_reasons"
                    ]
                }
                | {aggregation_reason}
            )
            material_risks = sorted(
                {
                    risk
                    for member in members
                    for risk in member["material_risks"]
                }
                | {aggregation_reason}
            )
            identity = {"source_id": source_id, "start": start, "end": end}
            source_duration = float(members[0]["source_duration_seconds"])
            source_coverage_ratio = min(
                1.0,
                rounded((end - start) / source_duration)
                if source_duration > 0
                else 0.0,
            )
            union_candidate = {
                    "span_candidate_id": stable_id("span", identity),
                    "source_id": source_id,
                    "episode": int(members[0]["episode"]),
                    "start": start,
                    "end": end,
                    "duration_seconds": rounded(end - start),
                    "source_duration_seconds": rounded(source_duration),
                    "source_coverage_ratio": source_coverage_ratio,
                    "full_source_like": False,
                    "teaser_atomic": False,
                    "teaser_atomic_owner_candidate_id": "",
                    "teaser_atomic_stitched": False,
                    "teaser_atomic_stitched_from": [],
                    "teaser_atomic_stitched_gap_seconds": 0.0,
                    "variant_types": merged_values("variant_types"),
                    "provenance_tiers": merged_values(
                        "provenance_tiers"
                    ),
                    "supports_beat_ids": merged_values(
                        "supports_beat_ids"
                    ),
                    "supports_thread_beat_ids": merged_values(
                        "supports_thread_beat_ids"
                    ),
                    "supports_must_show_ids": merged_values(
                        "supports_must_show_ids"
                    ),
                    "content_roles": merged_values("content_roles"),
                    "temporal_positions": merged_values(
                        "temporal_positions"
                    ),
                    "continuity_modes": merged_values("continuity_modes"),
                    "event_ids": merged_values("event_ids"),
                    "candidate_ids": merged_values("candidate_ids"),
                    "anchor_refs": sorted(
                        anchors.values(),
                        key=lambda item: (
                            item["start"],
                            item["end"],
                            item["origin"],
                            item["origin_id"],
                        ),
                    ),
                    "semantic_segment_refs": sorted(
                        segments.values(),
                        key=lambda item: (
                            item["start"],
                            item["end"],
                            item["kind"],
                            item["segment_id"],
                        ),
                    ),
                    "boundary_status": "needs_video_review",
                    "boundary_evidence": {
                        "start_basis": left_edges[0]["boundary_evidence"][
                            "start_basis"
                        ],
                        "end_basis": right_edges[-1]["boundary_evidence"][
                            "end_basis"
                        ],
                        "starts_mid_sentence_risk": any(
                            item["boundary_evidence"][
                                "starts_mid_sentence_risk"
                            ]
                            for item in left_edges
                        ),
                        "ends_mid_sentence_risk": any(
                            item["boundary_evidence"][
                                "ends_mid_sentence_risk"
                            ]
                            for item in right_edges
                        ),
                        "starts_mid_scene_risk": any(
                            item["boundary_evidence"][
                                "starts_mid_scene_risk"
                            ]
                            for item in left_edges
                        ),
                        "ends_mid_scene_risk": any(
                            item["boundary_evidence"][
                                "ends_mid_scene_risk"
                            ]
                            for item in right_edges
                        ),
                        "review_reasons": review_reasons,
                    },
                    "material_risks": material_risks,
                }
            unions.append(
                apply_full_source_like_classification(union_candidate)
            )
    return sorted(
        unions,
        key=lambda item: (
            int(item["episode"]),
            item["source_id"],
            float(item["start"]),
            float(item["end"]),
            item["span_candidate_id"],
        ),
    )


def build_bundle(
    packet: dict[str, Any],
    *,
    evidence_index_sha256: str,
    evidence_packet_sha256: str,
    anchor_merge_gap: float,
    tight_padding: float,
    scene_padding: float,
    reaction_tail: float,
    maximum_context_extension: float,
    maximum_span_seconds: float,
    scene_cut_points_by_source: dict[str, list[float]] | None = None,
    lead_in: float = 0.0,
    lead_out: float = 0.0,
) -> dict[str, Any]:
    source_by_id = {
        item["id"]: item for item in packet["evidence_catalog"]["sources"]
    }
    candidate_by_id = {
        item["id"]: item for item in packet["evidence_catalog"]["candidates"]
    }
    primary_teaser_id = packet["teaser_contract"][
        "primary_highlight_candidate_id"
    ]
    thread_beat_by_id = {
        item["id"]: item
        for item in packet["evidence_catalog"].get("thread_beats", [])
    }
    packet_segments = semantic_segments(packet)
    segments_by_source: dict[str, list[dict[str, Any]]] = {}
    for item in packet_segments:
        segments_by_source.setdefault(item["source_id"], []).append(item)
    components = allowed_components(packet)
    merged_candidates: dict[tuple[str, float, float], dict[str, Any]] = {}
    beat_to_candidate_keys: dict[str, set[tuple[str, float, float]]] = {}
    continuous_scene_records: list[dict[str, Any]] = []
    for beat in packet["beat_evidence"]:
        direct_refs = beat.get("direct_range_refs", [])
        candidate_refs = beat.get("candidate_range_refs", [])
        context_refs = beat.get("context_range_refs", [])
        group_specs: list[
            tuple[dict[str, Any], set[str], str, str, bool]
        ] = [
            (
                group,
                {"tight", "scene", "context"},
                "standard",
                "direct",
                True,
            )
            for group in group_anchor_refs(
                direct_refs, merge_gap=anchor_merge_gap
            )
        ]
        group_specs.extend(
            (
                group,
                {"tight", "scene", "context"},
                "standard",
                "candidate",
                True,
            )
            for group in group_anchor_refs(
                candidate_refs, merge_gap=anchor_merge_gap
            )
        )
        group_specs.extend(
            (
                group,
                {"scene", "context"},
                "standard",
                "context",
                False,
            )
            for group in group_anchor_refs(
                context_refs, merge_gap=anchor_merge_gap
            )
        )
        # Preserve each Highlight/Hook Candidate as its own fine-grained
        # tight anchor even when a coarser Event overlaps it.
        for candidate_ref in beat.get("candidate_range_refs", []):
            origin = candidate_by_id.get(candidate_ref.get("origin_id"))
            profile = (
                "highlight_atomic"
                if beat.get("role") == "teaser_intent"
                and isinstance(origin, dict)
                and origin.get("type") == "highlight"
                and origin.get("id") == primary_teaser_id
                else "standard"
            )
            group_specs.extend(
                (
                    group,
                    {"tight"},
                    profile,
                    "candidate",
                    True,
                )
                for group in group_anchor_refs(
                    [candidate_ref], merge_gap=0.0
                )
            )
        beat_to_candidate_keys[beat["beat_id"]] = set()
        for (
            group,
            allowed_variants,
            expansion_profile,
            provenance_tier,
            grants_functional_support,
        ) in group_specs:
            source_id = group["source_id"]
            source = source_by_id.get(source_id)
            if source is None:
                raise ValueError(
                    f"{packet['story_id']}: unknown source {source_id}"
                )
            source_duration = float(source["duration_seconds"])
            if (
                float(group["start"]) < 0
                or float(group["end"]) > source_duration + 0.001
            ):
                raise ValueError(
                    f"{packet['story_id']}: anchor range exceeds Source "
                    f"{source_id} duration"
                )
            component = containing_component(
                source_id,
                float(group["start"]),
                float(group["end"]),
                components=components,
                source_duration=source_duration,
            )
            source_segments = segments_by_source.get(source_id, [])
            # 获取当前 source 的场景切点（如果有）
            source_scene_cuts = None
            if scene_cut_points_by_source:
                source_scene_cuts = scene_cut_points_by_source.get(source_id)
            standard_variants = variant_ranges(
                group,
                segments=source_segments,
                component=component,
                continuity=beat["continuity"],
                tight_padding=tight_padding,
                scene_padding=scene_padding,
                reaction_tail=reaction_tail,
                maximum_context_extension=maximum_context_extension,
                maximum_span_seconds=maximum_span_seconds,
                scene_cut_points=source_scene_cuts,
                lead_in=lead_in,
                lead_out=lead_out,
            )
            variants = [
                (*item, "standard") for item in standard_variants
            ]
            if expansion_profile == "highlight_atomic":
                variants.insert(
                    0,
                    (
                        *candidate_atomic_tight_range(
                            group,
                            segments=source_segments,
                            component=component,
                            tight_padding=tight_padding,
                            reaction_tail=reaction_tail,
                            maximum_span_seconds=maximum_span_seconds,
                            scene_cut_points=source_scene_cuts,
                            lead_in=lead_in,
                            lead_out=lead_out,
                        ),
                        "highlight_atomic",
                    ),
                )
            anchor_group_id = stable_id(
                "anchor-group",
                {
                    "source_id": source_id,
                    "anchor_refs": group["refs"],
                },
            )
            emitted_ranges: set[tuple[float, float]] = set()
            for variant, start, end, capped, range_profile in variants:
                if variant not in allowed_variants:
                    continue
                if end <= start:
                    continue
                range_key = (start, end)
                if range_key in emitted_ranges:
                    existing = merged_candidates.get(
                        (source_id, start, end)
                    )
                    if existing is not None:
                        existing["variant_types"] = sorted(
                            set(existing["variant_types"]) | {variant}
                        )
                    continue
                emitted_ranges.add(range_key)
                overlap_segments = segments_overlapping(
                    source_segments, start, end
                )
                boundary_status, boundary_evidence, risks = boundary_analysis(
                    start=start,
                    end=end,
                    segments=overlap_segments,
                    packet_windows=packet["evidence_catalog"]["windows"],
                    source_id=source_id,
                    source_duration=source_duration,
                    component=component,
                    beat=beat,
                    capped=capped,
                )
                if range_profile == "highlight_atomic":
                    boundary_status = "needs_video_review"
                    atomic_review_reason = (
                        "Highlight 原子 tight 候选需用连续视频确认动作完整性"
                    )
                    boundary_evidence["review_reasons"] = list(
                        dict.fromkeys(
                            [
                                *boundary_evidence["review_reasons"],
                                atomic_review_reason,
                            ]
                        )
                    )
                    risks = list(dict.fromkeys([*risks, atomic_review_reason]))
                event_ids, candidate_ids = anchor_identity_sets(
                    group["refs"], candidates=candidate_by_id
                )
                identity = {
                    "source_id": source_id,
                    "start": start,
                    "end": end,
                }
                span_candidate_id = stable_id("span", identity)
                source_coverage_ratio = min(
                    1.0,
                    rounded((end - start) / source_duration)
                    if source_duration > 0
                    else 0.0,
                )
                semantic_density = semantic_density_ratio(
                    overlap_segments, start, end
                )
                full_source_like = is_full_source_like(
                    source_coverage_ratio,
                    source_duration,
                    semantic_density,
                )
                candidate_risks = list(dict.fromkeys(risks))
                if full_source_like:
                    candidate_risks.append(FULL_SOURCE_LIKE_RISK)
                raw_candidate = {
                    "span_candidate_id": span_candidate_id,
                    "source_id": source_id,
                    "episode": int(source["episode"]),
                    "start": start,
                    "end": end,
                    "duration_seconds": rounded(end - start),
                    "source_duration_seconds": rounded(source_duration),
                    "source_coverage_ratio": source_coverage_ratio,
                    "full_source_like": full_source_like,
                    "teaser_atomic": range_profile == "highlight_atomic",
                    "teaser_atomic_owner_candidate_id": (
                        primary_teaser_id
                        if range_profile == "highlight_atomic"
                        else ""
                    ),
                    "teaser_atomic_stitched": False,
                    "teaser_atomic_stitched_from": [],
                    "teaser_atomic_stitched_gap_seconds": 0.0,
                    "variant_types": [variant],
                    "provenance_tiers": [provenance_tier],
                    "supports_beat_ids": (
                        [beat["beat_id"]]
                        if grants_functional_support
                        else []
                    ),
                    "supports_thread_beat_ids": (
                        thread_beat_support(
                            beat, event_ids, thread_beat_by_id
                        )
                        if grants_functional_support
                        else []
                    ),
                    "supports_must_show_ids": (
                        must_show_support(beat, event_ids)
                        if grants_functional_support
                        else []
                    ),
                    "content_roles": [beat["role"]],
                    "temporal_positions": [beat["temporal_position"]],
                    "continuity_modes": [beat["continuity"]],
                    "event_ids": sorted(event_ids),
                    "candidate_ids": sorted(candidate_ids),
                    "anchor_refs": group["refs"],
                    "semantic_segment_refs": overlap_segments,
                    "boundary_status": boundary_status,
                    "boundary_evidence": boundary_evidence,
                    "material_risks": candidate_risks,
                }
                continuous_scene_records.append(
                    {
                        "source_id": source_id,
                        "component": component,
                        "anchor_group_id": anchor_group_id,
                        "continuity": beat["continuity"],
                        "candidate": deepcopy(raw_candidate),
                    }
                )
                key = (source_id, start, end)
                merged_candidates[key] = merge_candidate(
                    merged_candidates.get(key), raw_candidate
                )
                if grants_functional_support:
                    beat_to_candidate_keys[beat["beat_id"]].add(key)
    for union_candidate in continuous_scene_union_candidates(
        continuous_scene_records,
        maximum_span_seconds=maximum_span_seconds,
    ):
        key = (
            union_candidate["source_id"],
            union_candidate["start"],
            union_candidate["end"],
        )
        merged_candidates[key] = merge_candidate(
            merged_candidates.get(key), union_candidate
        )
        for beat_id in union_candidate["supports_beat_ids"]:
            beat_to_candidate_keys.setdefault(beat_id, set()).add(key)
    teaser_beat = next(
        (
            beat
            for beat in packet["beat_evidence"]
            if beat.get("role") == "teaser_intent"
        ),
        None,
    )
    if isinstance(teaser_beat, dict):
        _emit_direct_event_teaser_atomic_candidate(
            merged_candidates,
            beat_to_candidate_keys,
            teaser_beat=teaser_beat,
            primary_teaser_id=primary_teaser_id,
            candidate_by_id=candidate_by_id,
            source_by_id=source_by_id,
            maximum_span_seconds=maximum_span_seconds,
        )
    _emit_stitched_teaser_atomic_candidates(
        merged_candidates,
        beat_to_candidate_keys,
        primary_teaser_id=primary_teaser_id,
        maximum_span_seconds=maximum_span_seconds,
    )
    beats_by_id = {
        beat["beat_id"]: beat for beat in packet["beat_evidence"]
    }
    # Exact-range merges and continuous-scene unions may combine Event and
    # Beat provenance from multiple raw candidates.  Recompute every final
    # must-show claim from the merged candidate instead of trusting the
    # incremental union of pre-merge claims.
    for candidate in merged_candidates.values():
        candidate["supports_must_show_ids"] = (
            candidate_must_show_support(candidate, beats_by_id)
        )
    candidates = sorted(
        merged_candidates.values(),
        key=lambda item: (
            int(item["episode"]),
            item["source_id"],
            float(item["start"]),
            float(item["end"]),
            item["span_candidate_id"],
        ),
    )
    candidate_id_by_key = {
        (item["source_id"], item["start"], item["end"]): item[
            "span_candidate_id"
        ]
        for item in candidates
    }
    beat_coverage = []
    missing_must_have = False
    has_review = False
    for beat in packet["beat_evidence"]:
        candidate_ids = sorted(
            candidate_id_by_key[key]
            for key in beat_to_candidate_keys.get(beat["beat_id"], set())
            if key in candidate_id_by_key
        )
        related_candidates = [
            merged_candidates[key]
            for key in beat_to_candidate_keys.get(beat["beat_id"], set())
            if key in merged_candidates
        ]
        if not candidate_ids:
            status = "missing"
            if beat["must_have"]:
                missing_must_have = True
        elif all(
            item["boundary_status"] == "needs_video_review"
            for item in related_candidates
        ):
            status = "needs_video_review"
            has_review = True
        else:
            status = "covered"
        beat_coverage.append(
            {
                "beat_id": beat["beat_id"],
                "must_have": beat["must_have"],
                "candidate_ids": candidate_ids,
                "status": status,
            }
        )
    if missing_must_have:
        bundle_status = "incomplete"
    elif has_review:
        bundle_status = "needs_video_review"
    else:
        bundle_status = "ready"
    bundle = {
        "schema_version": "1.2",
        "method": SPAN_COMPILER_METHOD,
        "story_id": packet["story_id"],
        "title": packet["title"],
        "production_slot": packet["production_slot"],
        "status": bundle_status,
        "input_fingerprints": {
            "story_evidence_index_sha256": evidence_index_sha256,
            "story_evidence_packet_sha256": evidence_packet_sha256,
            "story_script_sha256": packet["approval_binding"][
                "story_script_sha256"
            ],
        },
        "compiler_policy": {
            "anchor_merge_gap_seconds": anchor_merge_gap,
            "tight_padding_seconds": tight_padding,
            "scene_padding_seconds": scene_padding,
            "reaction_tail_seconds": reaction_tail,
            "maximum_context_extension_seconds": maximum_context_extension,
            "maximum_span_seconds": maximum_span_seconds,
            "full_source_like_threshold": FULL_SOURCE_LIKE_THRESHOLD,
            "short_source_duration_seconds": SHORT_SOURCE_DURATION_SECONDS,
            "dense_short_source_min_semantic_ratio": (
                DENSE_SHORT_SOURCE_MIN_SEMANTIC_RATIO
            ),
            "highlight_atomic_tight_enabled": True,
            "emits_verified_boundaries": False,
        },
        "beat_coverage": beat_coverage,
        "candidates": candidates,
    }
    schema_errors = validate_task_response("span_candidate_bundle", bundle)
    if schema_errors:
        raise ValueError(
            "invalid Span Candidate Bundle: "
            + "; ".join(schema_errors[:50])
        )
    return bundle


def render_review(bundles: list[dict[str, Any]]) -> str:
    lines = ["# Span Candidate Compiler 复核", ""]
    for bundle in bundles:
        lines.extend(
            [
                f"## 槽位 {bundle['production_slot']}：{bundle['title']}",
                "",
                f"- Story ID：`{bundle['story_id']}`",
                f"- Bundle 状态：`{bundle['status']}`",
                f"- Span Candidate：{len(bundle['candidates'])} 个",
                "- `verified` 边界：0 个（当前编译器不做视频级验证）",
                "",
                "| Span | 原片范围 | 时长 | 变体 | 支撑 Beat | 边界 |",
                "|---|---|---:|---|---|---|",
            ]
        )
        for candidate in bundle["candidates"]:
            lines.append(
                f"| `{candidate['span_candidate_id']}` | "
                f"{candidate['source_id']} "
                f"{candidate['start']:.3f}–{candidate['end']:.3f} | "
                f"{candidate['duration_seconds']:.1f}s | "
                f"{', '.join(candidate['variant_types'])} | "
                f"{', '.join(candidate['supports_beat_ids'])} | "
                f"`{candidate['boundary_status']}` |"
            )
        lines.extend(["", "### Beat 覆盖", ""])
        for coverage in bundle["beat_coverage"]:
            lines.append(
                f"- `{coverage['beat_id']}`：`{coverage['status']}`，"
                f"{len(coverage['candidate_ids'])} 个候选。"
            )
        reasons = [
            f"{candidate['span_candidate_id']}: {reason}"
            for candidate in bundle["candidates"]
            for reason in candidate["boundary_evidence"]["review_reasons"]
        ]
        lines.extend(["", "### 定向视频复核理由", ""])
        if reasons:
            lines.extend(f"- {item}" for item in dict.fromkeys(reasons))
        else:
            lines.append("- 无。候选仍为 proposed，不等于视频级 verified。")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"