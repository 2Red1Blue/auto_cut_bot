#!/usr/bin/env python3
"""Preflight validation of Story Scripts — check structure, consistency, timing, and content quality."""

from __future__ import annotations

from typing import Any, Iterable

from autocut_core.libs._common import REPRISE_SCENE_IOU_THRESHOLD, _reprise_matches, _scene_equivalent
from autocut_core.libs.editorial_knowledge import story_coherence_diagnostics
from autocut_core.contracts.cross_unit import default_scope_policy, dependency_episode_range, dependency_event_ids
from autocut_core.schema.compat import validate_task_response
from autocut_core.contracts.teaser_contract import (
    TEASER_REPRISE_MAX_REPEAT_SECONDS,
    TEASER_STITCH_MAX_DURATION_SECONDS,
    TEASER_STITCH_MAX_GAP_SECONDS,
    event_can_stitch_to_primary,
    minimum_stitched_union,
    resolve_must_show_event_ids,
)


from autocut_core.libs.editorial_knowledge import load_knowledge_section

_script_preflight = load_knowledge_section("script_preflight") or {}
COMPUTED_BEAT_FIELDS = set(
    _script_preflight.get("computed_beat_fields")
    or {
        "estimated_source_duration_seconds",
        "evidence_status",
        "material_risks",
    }
)

# rule 5: 与 Span Compiler 共用精确区间合同：
# same source + interval gap≤5s + physical union≤30s。
TEASER_STITCH_GAP_SECONDS = TEASER_STITCH_MAX_GAP_SECONDS

# Treatment strategy constants (mirrored from compile_story_treatments.py)
STRATEGY_CHRONOLOGICAL = "chronological_compression"
STRATEGY_NO_REPRISE = "cold_open_no_reprise"
STRATEGY_DELAYED_REPRISE = "cold_open_delayed_reprise"


def _materialize_cross_unit_contract(
    script: dict[str, Any],
    *,
    beats: list[dict[str, Any]],
    events: dict[str, dict[str, Any]],
    facts: dict[str, dict[str, Any]],
    relationships: dict[str, dict[str, Any]],
    thread_beats: dict[str, dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
    edit_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Materialize the series-global dependency contract deterministically."""
    policy = default_scope_policy()
    opening_candidate_id = script.get("teaser_contract", {}).get(
        "primary_highlight_candidate_id"
    )
    opening_episode = candidates.get(opening_candidate_id, {}).get("episode", 1)
    if not isinstance(opening_episode, int):
        opening_episode = 1
    context_ids: set[str] = set()
    dependency_ranges: list[dict[str, Any]] = []
    materialized_beats: list[dict[str, Any]] = []
    for index, beat in enumerate(beats):
        retrieval = beat.get("retrieval_requirements", {})
        existing = beat.get("causal_dependency", {})
        if not isinstance(existing, dict):
            existing = {}
        explanation = bool(
            existing.get("explains_opening_highlight")
            or beat.get("temporal_position") == "earlier_context"
            or beat.get("explains_opening_highlight")
            or (
                isinstance(beat.get("causal_transition"), dict)
                and beat["causal_transition"].get("from_beat_id")
                == (beats[0].get("id") if beats else None)
            )
        )
        fact_ids = set(beat.get("required_before_fact_ids", []))
        fact_ids.update(retrieval.get("fact_ids", []))
        relationship_ids = set(retrieval.get("relationship_ids", []))
        event_ids = set(beat.get("event_ids", []))
        event_ids.update(retrieval.get("event_ids", []))
        for must_show in beat.get("must_show", []):
            if isinstance(must_show, dict):
                event_ids.update(must_show.get("evidence_event_ids", []))
        thread_beat_ids = set(retrieval.get("thread_beat_ids", []))
        if not explanation:
            dependency = {
                "explains_opening_highlight": False,
                "required_before_fact_ids": [],
                "required_relationship_ids": [],
                "required_event_ids": [],
                "required_thread_beat_ids": [],
                "causal_ancestor_episode_range": {
                    "min_episode": opening_episode,
                    "max_episode": opening_episode,
                    "reason": "该 Beat 不承担开场高光的因果解释",
                },
                "cross_unit_retrieval": {
                    "required": False,
                    "source_unit_ids": [],
                    "retrieval_status": "covered",
                },
            }
        else:
            dependency_seed = {
                "causal_dependency": {
                    "explains_opening_highlight": True,
                    "required_before_fact_ids": sorted(fact_ids),
                    "required_relationship_ids": sorted(relationship_ids),
                    "required_event_ids": sorted(event_ids),
                    "required_thread_beat_ids": sorted(thread_beat_ids),
                }
            }
            dependency_event_id_set, _ = dependency_event_ids(
                dependency_seed,
                dependency_seed,
                events=events,
                facts=facts,
                relationships=relationships,
                thread_beats=thread_beats,
            )
            dependency_range = dependency_episode_range(
                dependency_seed["causal_dependency"],
                {event_id: events[event_id] for event_id in dependency_event_id_set},
            )
            if dependency_range is None:
                dependency_range = {
                    "min_episode": opening_episode,
                    "max_episode": opening_episode,
                    "reason": "未找到可定位的因果祖先 Event，后续证据门禁必须阻断",
                }
            dependency_ranges.append(dependency_range)
            context_ids.update(fact_ids | relationship_ids | event_ids | thread_beat_ids)
            dependency = {
                "explains_opening_highlight": True,
                "required_before_fact_ids": sorted(fact_ids),
                "required_relationship_ids": sorted(relationship_ids),
                "required_event_ids": sorted(event_ids),
                "required_thread_beat_ids": sorted(thread_beat_ids),
                "causal_ancestor_episode_range": dependency_range,
                "cross_unit_retrieval": {
                    "required": edit_mode == "montage",
                    "source_unit_ids": [],
                    "retrieval_status": "pending",
                },
            }
        materialized_beats.append({**beat, "causal_dependency": dependency})
    if dependency_ranges:
        ancestor_range = {
            "min_episode": min(item["min_episode"] for item in dependency_ranges),
            "max_episode": max(item["max_episode"] for item in dependency_ranges),
            "reason": "由开场高光解释 Beat 的真实前因 Event 共同确定",
        }
    else:
        ancestor_range = {
            "min_episode": opening_episode,
            "max_episode": opening_episode,
            "reason": "原剧情顺剪或未声明跨单元回溯",
        }
    return (
        {
            "scope_policy": policy,
            "causal_ancestor_episode_range": ancestor_range,
            "required_context_ids": sorted(context_ids),
        },
        materialized_beats,
    )


def by_id(records: Iterable[dict[str, Any]], field: str = "id") -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in records:
        value = item.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"record is missing non-empty {field}")
        if value in result:
            raise ValueError(f"duplicate {field}: {value}")
        result[value] = item
    return result


def require_ids(values: Any, known: dict[str, Any], where: str) -> set[str]:
    if not isinstance(values, list):
        raise ValueError(f"{where} must be an array")
    result = {item for item in values if isinstance(item, str) and item}
    unknown = sorted(result - set(known))
    if unknown:
        raise ValueError(f"{where} contains unknown IDs: {unknown}")
    return result


def draft_view(script: dict[str, Any]) -> dict[str, Any]:
    value = dict(script)
    value.pop("feasibility", None)
    value["status"] = "draft"
    value["beats"] = [
        {
            key: item
            for key, item in beat.items()
            if key not in COMPUTED_BEAT_FIELDS
        }
        for beat in script.get("beats", [])
    ]
    return value


def merge_ranges(
    ranges: Iterable[tuple[str, float, float]],
) -> list[tuple[str, float, float]]:
    grouped: dict[str, list[tuple[float, float]]] = {}
    for source_id, start, end in ranges:
        if end <= start:
            continue
        grouped.setdefault(source_id, []).append((start, end))
    merged: list[tuple[str, float, float]] = []
    for source_id in sorted(grouped):
        current: list[list[float]] = []
        for start, end in sorted(grouped[source_id]):
            if not current or start > current[-1][1]:
                current.append([start, end])
            else:
                current[-1][1] = max(current[-1][1], end)
        merged.extend((source_id, start, end) for start, end in current)
    return merged


def duration_seconds(ranges: Iterable[tuple[str, float, float]]) -> float:
    return round(sum(end - start for _, start, end in merge_ranges(ranges)), 3)


def event_ranges(
    event: dict[str, Any],
    *,
    padding: float,
    source_durations: dict[str, float],
) -> list[tuple[str, float, float]]:
    source_id = event.get("source_id")
    if not isinstance(source_id, str):
        return []
    result = []
    for item in event.get("source_ranges", []):
        start, end = item.get("start"), item.get("end")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        left = max(0.0, float(start) - padding)
        right = float(end) + padding
        duration = source_durations.get(source_id)
        if duration is not None:
            right = min(duration, right)
        if right > left:
            result.append((source_id, left, right))
    return result


def candidate_ranges(
    candidate: dict[str, Any],
    *,
    padding: float,
    source_durations: dict[str, float],
) -> list[tuple[str, float, float]]:
    source_id = candidate.get("source_id")
    start, end = candidate.get("start"), candidate.get("end")
    if (
        not isinstance(source_id, str)
        or not isinstance(start, (int, float))
        or not isinstance(end, (int, float))
    ):
        return []
    left = max(0.0, float(start) - padding)
    right = float(end) + padding
    duration = source_durations.get(source_id)
    if duration is not None:
        right = min(duration, right)
    return [(source_id, left, right)] if right > left else []


def entity_event_ids(
    retrieval: dict[str, Any],
    *,
    characters: dict[str, dict[str, Any]],
    relationships: dict[str, dict[str, Any]],
    facts: dict[str, dict[str, Any]],
    threads: dict[str, dict[str, Any]],
    thread_beats: dict[str, dict[str, Any]],
    events: dict[str, dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
    where: str,
) -> tuple[set[str], set[str], set[str]]:
    """Split option-bearing evidence from broad context-only recall.

    Explicit Event, Candidate and Thread Beat references can become functional
    Plan evidence. Character, relationship, Fact and whole-Thread expansion is
    useful for recall, but counting all of it as editable duration is the source
    of multi-thousand-second false feasibility estimates.
    """
    functional_event_ids = require_ids(
        retrieval.get("event_ids"), events, f"{where}.event_ids"
    )
    context_event_ids: set[str] = set()
    fact_ids = require_ids(retrieval.get("fact_ids"), facts, f"{where}.fact_ids")
    character_ids = require_ids(
        retrieval.get("character_ids"), characters, f"{where}.character_ids"
    )
    relationship_ids = require_ids(
        retrieval.get("relationship_ids"),
        relationships,
        f"{where}.relationship_ids",
    )
    thread_ids = require_ids(
        retrieval.get("story_thread_ids"), threads, f"{where}.story_thread_ids"
    )
    thread_beat_ids = require_ids(
        retrieval.get("thread_beat_ids"),
        thread_beats,
        f"{where}.thread_beat_ids",
    )
    candidate_ids = require_ids(
        retrieval.get("candidate_ids"), candidates, f"{where}.candidate_ids"
    )
    for fact_id in fact_ids:
        context_event_ids.update(facts[fact_id].get("event_ids", []))
    for character_id in character_ids:
        context_event_ids.update(
            characters[character_id].get("evidence_event_ids", [])
        )
    for relationship_id in relationship_ids:
        context_event_ids.update(
            item.get("event_id")
            for item in relationships[relationship_id].get("state_changes", [])
            if isinstance(item.get("event_id"), str)
        )
    for thread_id in thread_ids:
        context_event_ids.update(threads[thread_id].get("event_ids", []))
    for thread_beat_id in thread_beat_ids:
        functional_event_ids.update(
            thread_beats[thread_beat_id].get("event_ids", [])
        )
    for candidate_id in candidate_ids:
        functional_event_ids.update(candidates[candidate_id].get("event_ids", []))
    require_ids(
        sorted(functional_event_ids | context_event_ids),
        events,
        f"{where}.resolved_event_ids",
    )
    return functional_event_ids, context_event_ids, candidate_ids


def preflight_script(
    raw_script: dict[str, Any],
    *,
    events: dict[str, dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
    characters: dict[str, dict[str, Any]],
    relationships: dict[str, dict[str, Any]],
    facts: dict[str, dict[str, Any]],
    threads: dict[str, dict[str, Any]],
    thread_beats: dict[str, dict[str, Any]],
    questions: dict[str, dict[str, Any]],
    source_durations: dict[str, float],
    context_padding_seconds: float,
    usable_ratio: float,
) -> dict[str, Any]:
    script = draft_view(raw_script)
    draft_errors = validate_task_response("story_script_draft", script)
    if draft_errors:
        raise ValueError("invalid Story Script draft: " + "; ".join(draft_errors[:30]))
    require_ids(script["character_ids"], characters, "character_ids")
    require_ids(script["relationship_ids"], relationships, "relationship_ids")
    require_ids(script["story_thread_ids"], threads, "story_thread_ids")
    selected_thread_beat_ids = require_ids(
        script["selected_thread_beat_ids"],
        thread_beats,
        "selected_thread_beat_ids",
    )
    required_thread_beat_ids = require_ids(
        script["required_thread_beat_ids"],
        thread_beats,
        "required_thread_beat_ids",
    )
    omitted_thread_beat_ids = require_ids(
        [
            item.get("thread_beat_id")
            for item in script.get("omitted_thread_beats", [])
            if isinstance(item, dict)
        ],
        thread_beats,
        "omitted_thread_beats",
    )
    if selected_thread_beat_ids & omitted_thread_beat_ids:
        raise ValueError("a Thread Beat cannot be both selected and omitted")
    if not required_thread_beat_ids <= selected_thread_beat_ids:
        raise ValueError("required Thread Beats cannot be omitted")
    require_ids(script["required_fact_ids"], facts, "required_fact_ids")
    require_ids(
        script["intentional_mystery_fact_ids"],
        facts,
        "intentional_mystery_fact_ids",
    )
    explicit_story_event_ids: set[str] = set()
    story_candidate_ids: set[str] = set()
    story_ranges: list[tuple[str, float, float]] = []
    review_event_ids: set[str] = set()
    finalized_beats = []
    status_buckets: dict[str, list[str]] = {
        "covered": [],
        "partial": [],
        "missing": [],
        "conflicting": [],
        "needs_video_review": [],
    }
    story_risks: list[str] = []
    seen_beat_ids: set[str] = set()
    teaser_contract_failed = False
    teaser_failure_codes: set[str] = set()
    teaser_outside_must_show_ids: set[str] = set()
    teaser_must_show_ids: set[str] = set()
    teaser_direct_event_ids: set[str] = set()
    teaser_direct_event_groups: list[set[str]] = []
    teaser_contract = script["teaser_contract"]
    opening_strategy = teaser_contract.get(
        "opening_strategy", "future_preview_reprise"
    )
    primary_teaser_id = teaser_contract["primary_highlight_candidate_id"]
    primary_teaser = candidates.get(primary_teaser_id, {})
    primary_source_id = primary_teaser.get("source_id")
    primary_start = primary_teaser.get("start")
    primary_end = primary_teaser.get("end")
    primary_duration = (
        float(primary_end) - float(primary_start)
        if isinstance(primary_start, (int, float))
        and isinstance(primary_end, (int, float))
        else 0.0
    )
    # rule 1: reprise 白名单包含 end_hook。短剧常见 "开头挂钩→结尾兑现"
    # 结构里，Teaser 与 end_hook 引用同一事件是合法呼应。事实级 event_id 硬
    # 匹配 + 场景级等价（见 _scene_equivalent）双通道。
    REPRISE_ROLES = {"escalation", "turn_or_reveal", "payoff", "end_hook"}
    downstream_highlight_event_ids = {
        event_id
        for later_beat in script["beats"][1:]
        if later_beat["role"] in REPRISE_ROLES
        for event_id in later_beat.get("event_ids", [])
        if isinstance(event_id, str)
    }
    downstream_highlight_event_ids.update(
        event_id
        for later_beat in script["beats"][1:]
        if later_beat["role"] in REPRISE_ROLES
        for candidate_id in later_beat.get("candidate_suggestions", [])
        if candidate_id in candidates
        for event_id in candidates[candidate_id].get("event_ids", [])
        if isinstance(event_id, str)
    )
    retrieved_thread_beat_ids: set[str] = set()
    for index, beat in enumerate(script["beats"]):
        beat_id = beat["id"]
        where = f"beats[{index}]"
        if beat_id in seen_beat_ids:
            raise ValueError(f"duplicate beat id: {beat_id}")
        seen_beat_ids.add(beat_id)
        direct_event_ids = require_ids(beat["event_ids"], events, f"{where}.event_ids")
        direct_candidate_ids = require_ids(
            beat["candidate_suggestions"],
            candidates,
            f"{where}.candidate_suggestions",
        )
        (
            retrieval_event_ids,
            _context_retrieval_event_ids,
            retrieval_candidate_ids,
        ) = entity_event_ids(
            beat["retrieval_requirements"],
            characters=characters,
            relationships=relationships,
            facts=facts,
            threads=threads,
            thread_beats=thread_beats,
            events=events,
            candidates=candidates,
            where=f"{where}.retrieval_requirements",
        )
        beat_thread_beat_ids = require_ids(
            beat["retrieval_requirements"].get("thread_beat_ids"),
            thread_beats,
            f"{where}.retrieval_requirements.thread_beat_ids",
        )
        retrieved_thread_beat_ids.update(beat_thread_beat_ids)
        exact_evidence_ids = set(direct_event_ids)
        missing_must_show: list[str] = []
        must_show_ids: set[str] = set()
        for item_index, item in enumerate(beat["must_show"]):
            must_show_id = item["id"]
            if must_show_id in must_show_ids:
                raise ValueError(f"{where}: duplicate must_show id {must_show_id}")
            must_show_ids.add(must_show_id)
            require_ids(
                item["evidence_event_ids"],
                events,
                f"{where}.must_show[{item_index}].evidence_event_ids",
            )
            # Bug #1: fact 天然跨集，其 event_ids 不进入 Teaser 的
            # cross-source / stitch 时窗判定；只用于覆盖率与 evidence retrieval。
            item_fact_ids = require_ids(
                item["evidence_fact_ids"],
                facts,
                f"{where}.must_show[{item_index}].evidence_fact_ids",
            )
            (
                item_direct_event_ids,
                _item_fact_event_ids,
                item_event_ids,
            ) = resolve_must_show_event_ids(item, facts)
            require_ids(
                sorted(item_event_ids),
                events,
                f"{where}.must_show[{item_index}].resolved_event_ids",
            )
            if not item_event_ids:
                missing_must_show.append(must_show_id)
            if beat["role"] == "teaser_intent":
                teaser_must_show_ids.add(must_show_id)
                teaser_direct_event_ids.update(item_direct_event_ids)
                if item_direct_event_ids:
                    teaser_direct_event_groups.append(
                        set(item_direct_event_ids)
                    )
                candidate_event_ids = {
                    value
                    for value in primary_teaser.get("event_ids", [])
                    if isinstance(value, str)
                }
                # Bug #1: 只对 direct 事件做 teaser 源片位置校验；不检查 fact-expanded。
                for item_event_id in item_direct_event_ids:
                    event = events[item_event_id]
                    if event.get("source_id") != primary_source_id:
                        teaser_failure_codes.add("teaser_cross_source_evidence")
                        teaser_outside_must_show_ids.add(must_show_id)
                        continue
                    ranges_are_adjacent, _ = event_can_stitch_to_primary(
                        event,
                        primary_source_id=primary_source_id,
                        primary_start=primary_start,
                        primary_end=primary_end,
                    )
                    if (
                        item_event_id not in candidate_event_ids
                        and not ranges_are_adjacent
                    ):
                        teaser_failure_codes.add(
                            "teaser_must_show_outside_stitch_window"
                        )
                        teaser_outside_must_show_ids.add(must_show_id)
            # Fact-expanded Event IDs remain valid coverage/recall evidence but
            # are not direct option-bearing source duration.
            exact_evidence_ids.update(item_direct_event_ids)
        hidden = require_ids(
            beat["must_not_reveal_fact_ids"],
            facts,
            f"{where}.must_not_reveal_fact_ids",
        )
        introduced = require_ids(
            beat["introduced_fact_ids"], facts, f"{where}.introduced_fact_ids"
        )
        require_ids(
            beat["required_before_fact_ids"],
            facts,
            f"{where}.required_before_fact_ids",
        )
        if hidden & introduced:
            raise ValueError(
                f"{where} both hides and introduces facts: {sorted(hidden & introduced)}"
            )
        require_ids(
            beat["resolved_question_ids"],
            questions,
            f"{where}.resolved_question_ids",
        )
        resolved_event_ids = exact_evidence_ids | retrieval_event_ids
        require_ids(
            sorted(resolved_event_ids), events, f"{where}.all_resolved_event_ids"
        )
        candidate_ids = direct_candidate_ids | retrieval_candidate_ids
        ranges: list[tuple[str, float, float]] = []
        for event_id in resolved_event_ids:
            ranges.extend(
                event_ranges(
                    events[event_id],
                    padding=context_padding_seconds,
                    source_durations=source_durations,
                )
            )
        for candidate_id in candidate_ids:
            ranges.extend(
                candidate_ranges(
                    candidates[candidate_id],
                    padding=context_padding_seconds / 2,
                    source_durations=source_durations,
                )
            )
        maximum = duration_seconds(ranges)
        minimum = round(maximum * usable_ratio, 3)
        risks: list[str] = []
        if len(missing_must_show) == len(beat["must_show"]):
            evidence_status = "missing"
            risks.append("全部 must_show 缺少可解析的 Event/Fact 证据")
        elif missing_must_show:
            evidence_status = "partial"
            risks.append(f"must_show 证据缺失：{', '.join(missing_must_show)}")
        else:
            evidence_status = "covered"
        if beat["role"] in {"teaser_intent", "end_hook"}:
            expected_type = (
                "highlight"
                if beat["role"] == "teaser_intent"
                else "hook"
            )
            matching = [
                candidate_id
                for candidate_id in direct_candidate_ids
                if candidates[candidate_id].get("type") == expected_type
            ]
            if not matching:
                if beat["role"] == "teaser_intent":
                    evidence_status = "missing"
                    teaser_contract_failed = True
                    risks.append(
                        "Teaser 未直接绑定明确的 highlight Candidate"
                    )
                elif evidence_status == "covered":
                    evidence_status = "needs_video_review"
                    risks.append(f"未找到明确的 {expected_type} Candidate")
            if beat["role"] == "teaser_intent":
                if beat["candidate_suggestions"] != [primary_teaser_id]:
                    evidence_status = "missing"
                    teaser_contract_failed = True
                    teaser_failure_codes.add("teaser_multiple_highlights")
                    risks.append(
                        "teaser_multiple_highlights: Teaser 必须只绑定 "
                        "primary Highlight Candidate"
                    )
                if primary_teaser.get("type") != "highlight":
                    evidence_status = "missing"
                    teaser_contract_failed = True
                    teaser_failure_codes.add("teaser_multiple_highlights")
                    risks.append(
                        "teaser_multiple_highlights: primary Candidate 不是 highlight"
                    )
                expected_opening_position = (
                    "mainline"
                    if opening_strategy == "original_chronological_opening"
                    else "future_preview"
                )
                if beat["temporal_position"] != expected_opening_position:
                    evidence_status = "missing"
                    teaser_contract_failed = True
                    risks.append(
                        "Teaser 开场时间位置必须是 "
                        f"{expected_opening_position}，与 opening_strategy 匹配"
                    )
                matching_durations = [
                    float(candidates[item]["end"])
                    - float(candidates[item]["start"])
                    for item in matching
                    if isinstance(candidates[item].get("start"), (int, float))
                    and isinstance(
                        candidates[item].get("end"), (int, float)
                    )
                ]
                if matching and not any(
                    duration <= 30.0 for duration in matching_durations
                ):
                    evidence_status = "missing"
                    teaser_contract_failed = True
                    risks.append(
                        "teaser_atomic_interval_over_limit: Teaser Highlight "
                        "Candidate 超过 30 秒硬上限"
                    )
                    teaser_failure_codes.add(
                        "teaser_atomic_interval_over_limit"
                    )
                elif matching_durations and not any(
                    8.0 <= duration <= 20.0
                    for duration in matching_durations
                ):
                    risks.append(
                        "Teaser Highlight 建议使用 8–20 秒局部高光"
                    )
                matching_event_ids = {
                    event_id
                    for candidate_id in matching
                    for event_id in candidates[candidate_id].get(
                        "event_ids", []
                    )
                }
                if (
                    opening_strategy == "future_preview_reprise"
                    and matching
                    and not _reprise_matches(
                    matching_event_ids, downstream_highlight_event_ids, events
                    )
                ):
                    evidence_status = "missing"
                    teaser_contract_failed = True
                    risks.append(
                        "teaser_not_reprised_in_body: Teaser 高光未与后续 "
                        "escalation/turn/payoff/end_hook Event 建立重现关系"
                        "（含同 source scene-level IoU>0.2 fallback）"
                    )
                    teaser_failure_codes.add("teaser_not_reprised_in_body")
                if teaser_outside_must_show_ids:
                    evidence_status = "missing"
                    teaser_contract_failed = True
                    risks.append(
                        "Teaser must_show 不满足 primary Highlight "
                        f"gap≤{TEASER_STITCH_GAP_SECONDS:.1f}s 且并集≤"
                        f"{TEASER_STITCH_MAX_DURATION_SECONDS:.1f}s："
                        + ", ".join(sorted(teaser_outside_must_show_ids))
                    )
        if any(
            item["observable_via"] == "screen_text" for item in beat["must_show"]
        ):
            if evidence_status == "covered":
                evidence_status = "needs_video_review"
            risks.append("屏幕文字可读性需要视频复核")
        if beat["temporal_position"] in {"future_preview", "parallel"}:
            if evidence_status == "covered":
                evidence_status = "needs_video_review"
            risks.append("非线性时间位置需要复核进入和返回边界")
        if maximum <= 0 and evidence_status not in {"missing", "conflicting"}:
            evidence_status = "missing"
            risks.append("没有可估算的原片范围")
        if evidence_status == "needs_video_review":
            review_event_ids.update(exact_evidence_ids)
        status_buckets[evidence_status].append(beat_id)
        explicit_story_event_ids.update(exact_evidence_ids)
        story_candidate_ids.update(candidate_ids)
        story_ranges.extend(ranges)
        finalized_beats.append(
            {
                **beat,
                "event_ids": sorted(direct_event_ids),
                "candidate_suggestions": sorted(direct_candidate_ids),
                "estimated_source_duration_seconds": {
                    "minimum": minimum,
                    "maximum": maximum,
                },
                "evidence_status": evidence_status,
                "material_risks": risks,
            }
        )
        story_risks.extend(f"{beat_id}: {risk}" for risk in risks)
    if not selected_thread_beat_ids <= retrieved_thread_beat_ids:
        raise ValueError(
            "selected Thread Beats are not all referenced by Script Beat retrieval"
        )
    editorial_diagnostics = story_coherence_diagnostics(
        {**script, "beats": finalized_beats},
        candidates=candidates,
    )
    for item in editorial_diagnostics.get("findings", []):
        story_risks.append(
            f"{item['code']}: {item['description']}"
        )
    teaser_obligation_duration = round(primary_duration, 3)
    teaser_repeat_contract_status = "feasible"
    teaser_interval_groups: list[list[tuple[float, float, str]]] = []
    if isinstance(primary_source_id, str):
        seen_teaser_groups: set[tuple[tuple[float, float, str], ...]] = set()
        primary_candidate_event_ids = {
            item
            for item in primary_teaser.get("event_ids", [])
            if isinstance(item, str)
        }
        for event_ids in teaser_direct_event_groups:
            group = [
                (
                    float(source_range["start"]),
                    float(source_range["end"]),
                    event_id,
                )
                for event_id in sorted(event_ids)
                for source_range in events[event_id].get("source_ranges", [])
                if events[event_id].get("source_id") == primary_source_id
                and isinstance(source_range.get("start"), (int, float))
                and isinstance(source_range.get("end"), (int, float))
            ]
            if (
                isinstance(primary_start, (int, float))
                and isinstance(primary_end, (int, float))
            ):
                group.extend(
                    (
                        float(primary_start),
                        float(primary_end),
                        event_id,
                    )
                    for event_id in sorted(
                        event_ids & primary_candidate_event_ids
                    )
                )
            group_key = tuple(sorted(group))
            if group and group_key not in seen_teaser_groups:
                seen_teaser_groups.add(group_key)
                teaser_interval_groups.append(group)
    teaser_union = (
        minimum_stitched_union(
            (float(primary_start), float(primary_end)),
            teaser_interval_groups,
        )
        if isinstance(primary_start, (int, float))
        and isinstance(primary_end, (int, float))
        and len(teaser_interval_groups)
        == len(
            {
                tuple(sorted(group))
                for group in teaser_direct_event_groups
                if group
            }
        )
        else None
    )
    if teaser_direct_event_ids:
        if teaser_union is None:
            teaser_contract_failed = True
            teaser_repeat_contract_status = "revision_required"
            teaser_failure_codes.add("teaser_direct_union_over_limit")
            story_risks.append(
                "Teaser 全部 direct must-show 与 primary Highlight 的联合物理范围"
                "无法同时满足 gap≤5s 且总长≤30s"
            )
        else:
            teaser_obligation_duration = float(
                teaser_union["duration_seconds"]
            )
            selected_teaser_event_ids = {
                item["origin_id"]
                for item in teaser_union["selected_intervals"]
            }
            all_direct_events_must_reprise = selected_teaser_event_ids <= (
                downstream_highlight_event_ids
            )
            if (
                all_direct_events_must_reprise
                and teaser_obligation_duration
                > TEASER_REPRISE_MAX_REPEAT_SECONDS + 0.001
            ):
                teaser_contract_failed = True
                teaser_repeat_contract_status = "revision_required"
                teaser_failure_codes.add(
                    "teaser_reprise_exceeds_repeat_budget"
                )
                story_risks.append(
                    "Teaser direct must-show 联合范围 "
                    f"{teaser_obligation_duration:.3f}s 将在正文完整重现，"
                    f"必然超过 {TEASER_REPRISE_MAX_REPEAT_SECONDS:.0f}s "
                    "全片重复预算；应把 Teaser 缩成 8–20s 核心高光，"
                    "反应/身份尾段留在正文"
                )
    hook = script["ending_hook_intent"]
    hook_event_ids = require_ids(hook["event_ids"], events, "ending_hook_intent.event_ids")
    hook_candidate_ids = require_ids(
        hook["candidate_ids"], candidates, "ending_hook_intent.candidate_ids"
    )
    require_ids(
        hook["story_thread_ids"], threads, "ending_hook_intent.story_thread_ids"
    )
    explicit_story_event_ids.update(hook_event_ids)
    story_candidate_ids.update(hook_candidate_ids)
    for event_id in hook_event_ids:
        story_ranges.extend(
            event_ranges(
                events[event_id],
                padding=context_padding_seconds,
                source_durations=source_durations,
            )
        )
    for candidate_id in hook_candidate_ids:
        story_ranges.extend(
            candidate_ranges(
                candidates[candidate_id],
                padding=context_padding_seconds / 2,
                source_durations=source_durations,
            )
        )
    maximum_total = duration_seconds(story_ranges)
    minimum_total = round(maximum_total * usable_ratio, 3)
    must_have_bad = {
        beat["id"]
        for beat in finalized_beats
        if beat["must_have"]
        and beat["evidence_status"] in {"missing", "conflicting"}
    }
    payoff_usable = any(
        beat["role"] == "payoff"
        and beat["evidence_status"] not in {"missing", "conflicting"}
        for beat in finalized_beats
    )
    # Story planning is coherence-first. Functional evidence is still measured
    # for auditability, but no lower-bound, preferred target, or duration-only
    # scope expansion may influence Story selection.
    HARD_MINIMUM_SECONDS = 0.0
    SOFT_TARGET_SECONDS = 0.0
    AUTO_MERGE_THRESHOLD_SECONDS = 0.0
    meets_soft_target = maximum_total >= SOFT_TARGET_SECONDS
    soft_target_gap_seconds = round(
        max(0.0, SOFT_TARGET_SECONDS - maximum_total), 3
    )
    auto_merge_hint: dict[str, Any] | None = None
    if must_have_bad:
        story_risks.append(
            "must-have Beat 不可覆盖：" + ", ".join(sorted(must_have_bad))
        )
    if not payoff_usable:
        story_risks.append("局部 Payoff 没有可用证据")
    teaser_contract_failed = teaser_contract_failed or bool(
        teaser_failure_codes
    )
    if teaser_contract_failed or must_have_bad or not payoff_usable:
        feasibility_status = "not_feasible"
    elif editorial_diagnostics["status"] == "blocked":
        feasibility_status = "not_feasible"
    elif status_buckets["partial"] or status_buckets["needs_video_review"]:
        feasibility_status = "partial"
    else:
        feasibility_status = "feasible"
    primary_story_thread_id = (
        script.get("primary_story_thread_id")
        or editorial_diagnostics.get("primary_story_thread_id")
        or (script.get("story_thread_ids") or [""])[0]
    )
    primary_story_thread_id_source = (
        script.get("primary_story_thread_id_source")
        or ("model" if script.get("primary_story_thread_id") else "preflight_inferred")
    )
    hook_type = script.get("ending_hook_intent", {}).get("hook_type")
    if not hook_type and script.get("ending_hook_intent", {}).get("question"):
        # A question-only hook is normalized to the safest unresolved
        # landing. New prompts ask the model to state this explicitly.
        hook_type = "unresolved_outcome"
    secondary_thread_ids = [
        item for item in script.get("story_thread_ids", [])
        if isinstance(item, str) and item and item != primary_story_thread_id
    ]
    integrated_support_thread_ids = list(
        dict.fromkeys(
            item
            for item in (
                script.get("integrated_support_thread_ids", [])
                or script.get("editorial_contract", {}).get(
                    "integrated_support_thread_ids", []
                )
            )
            if isinstance(item, str) and item
        )
    )
    genre_profile = script.get("genre_profile") or "project_specific"
    if not integrated_support_thread_ids:
        integrated_support_thread_ids = list(
            dict.fromkeys(
                thread_id
                for beat in finalized_beats
                if beat.get("thread_role") == "integrated_support"
                for thread_id in beat.get("retrieval_requirements", {}).get(
                    "story_thread_ids", []
                )
                if isinstance(thread_id, str) and thread_id != primary_story_thread_id
            )
        )
    resolved_edit_mode = script.get("edit_mode")
    if resolved_edit_mode not in {"montage", "original_chronological"}:
        resolved_edit_mode = (
            "original_chronological"
            if opening_strategy == "original_chronological_opening"
            else "montage"
        )
    edit_mode_reason = script.get("edit_mode_reason")
    if not isinstance(edit_mode_reason, str) or not edit_mode_reason.strip():
        edit_mode_reason = (
            "首段来自原片 mainline 自然高光，按严格源顺序推进。"
            if resolved_edit_mode == "original_chronological"
            else "首段高光能显著提高观看承诺，正文用同线因果解释并继续推进。"
        )
    cross_unit_contract, finalized_beats = _materialize_cross_unit_contract(
        script,
        beats=finalized_beats,
        events=events,
        facts=facts,
        relationships=relationships,
        thread_beats=thread_beats,
        candidates=candidates,
        edit_mode=resolved_edit_mode,
    )
    highlight_ids = sorted(
        candidate_id
        for candidate_id in story_candidate_ids
        if candidates[candidate_id].get("type") == "highlight"
    )
    hook_ids = sorted(
        candidate_id
        for candidate_id in story_candidate_ids
        if candidates[candidate_id].get("type") == "hook"
    )
    final_script = {
        **script,
        "primary_story_thread_id": primary_story_thread_id,
        "primary_story_thread_id_source": primary_story_thread_id_source,
        "genre_profile": genre_profile,
        **(
            {
                "edit_mode": resolved_edit_mode,
                "edit_mode_reason": edit_mode_reason,
            }
            if (
                "opening_strategy" in script.get("teaser_contract", {})
                or script.get("edit_mode") in {"montage", "original_chronological"}
            )
            else {}
        ),
        "golden_case_ids": list(script.get("golden_case_ids", [])),
        **cross_unit_contract,
        "integrated_support_thread_ids": integrated_support_thread_ids,
        "editorial_contract": {
            "primary_story_thread_id": primary_story_thread_id,
            "secondary_thread_ids": secondary_thread_ids,
            "integrated_support_thread_ids": integrated_support_thread_ids,
            "mainline_type": genre_profile,
            "required_bridge_beat_ids": sorted(
                set(script.get("required_bridge_beat_ids", []))
            ),
            "same_line_extension_only": True,
            "future_arc_injection_forbidden": True,
            "continuity_contract": {
                "same_primary_thread_across_opening_body_ending": True,
                "cross_segment_bridge_required": True,
                "allowed_bridge_types": [
                    "causal",
                    "time",
                    "same_scene",
                    "theme",
                    "source_transition",
                    "visual_transition",
                ],
                "lookback_allowed_only_for": [
                    "opening_cause",
                    "character_relationship",
                    "story_background",
                    "rule_or_motivation",
                ],
                "lookback_must_return_to_mainline": True,
                "future_complete_arc_injection_forbidden": True,
                "unexplained_jump_status": "blocked",
            },
            "ending_policy": {
                "preferred_landing": "same_primary_thread_hook",
                "hook_types": [
                    "unresolved_outcome",
                    "identity_reveal_before_cut",
                    "unresolved_choice",
                ],
                "no_hook_fallback": "current_story_line_episode_tail",
                "no_hook_is_allowed": True,
                "invented_hook_forbidden": True,
                "future_arc_after_hook_forbidden": True,
            },
            "duration_extension_policy": {
                "trigger": "below_minimum_duration",
                "minimum_seconds": 300,
                "order": [
                    "continue_from_last_selected_source_point",
                    "current_episode_tail",
                    "next_episode_zero_seconds",
                ],
                "after_threshold": "continue_to_threshold_episode_tail",
                "same_primary_thread_only": True,
                "must_be_forward_chronological": True,
                "no_cross_thread_fill": True,
                "no_duplicate_or_functionless_fill": True,
                "stop_without_evidence": True,
            },
            "ending_hook_type": hook_type or "unresolved_outcome",
            "golden_sample_reference": (
                script.get("editorial_contract", {}).get(
                    "golden_sample_reference"
                )
                or "generic-editorial-contract"
            ),
        },
        "beats": finalized_beats,
        "evidence_event_ids": sorted(explicit_story_event_ids),
        "feasibility": {
            "status": feasibility_status,
            "method": "functional-evidence-duration-v3-story-coherence",
            "assumptions": {
                "context_padding_seconds": context_padding_seconds,
                "usable_ratio": usable_ratio,
                "context_entity_expansion_counts_toward_duration": False,
            },
            "estimated_source_duration_min_seconds": minimum_total,
            "estimated_source_duration_max_seconds": maximum_total,
            "meets_5_minimum": maximum_total >= HARD_MINIMUM_SECONDS,
            "meets_10_preferred": maximum_total >= 600,
            "soft_target_seconds": SOFT_TARGET_SECONDS,
            "meets_soft_target": meets_soft_target,
            "soft_target_gap_seconds": soft_target_gap_seconds,
            **(
                {"auto_merge_hint": auto_merge_hint}
                if auto_merge_hint is not None
                else {}
            ),
            "covered_beat_ids": status_buckets["covered"],
            "partial_beat_ids": status_buckets["partial"],
            "missing_beat_ids": status_buckets["missing"],
            "conflicting_beat_ids": status_buckets["conflicting"],
            "needs_video_review_beat_ids": status_buckets["needs_video_review"],
            "review_event_ids": sorted(review_event_ids),
            "highlight_candidate_ids": highlight_ids,
            "hook_candidate_ids": hook_ids,
            "material_risks": list(dict.fromkeys(story_risks)),
            "editorial_diagnostics": editorial_diagnostics,
            "teaser_diagnostics": {
                "mode": "single_highlight",
                **(
                    {
                        "opening_strategy": opening_strategy,
                        "edit_mode": resolved_edit_mode,
                    }
                    if (
                        "opening_strategy" in script.get("teaser_contract", {})
                        or script.get("edit_mode")
                        in {"montage", "original_chronological"}
                    )
                    else {}
                ),
                "primary_highlight_candidate_id": primary_teaser_id,
                "source_id": (
                    primary_source_id
                    if isinstance(primary_source_id, str)
                    else ""
                ),
                "candidate_duration_seconds": round(primary_duration, 3),
                "physical_obligation_duration_seconds": round(
                    teaser_obligation_duration, 3
                ),
                "mandatory_reprise_event_ids": sorted(
                    (
                        {
                            item["origin_id"]
                            for item in teaser_union["selected_intervals"]
                        }
                        if teaser_union is not None
                        else teaser_direct_event_ids
                    )
                    & downstream_highlight_event_ids
                ),
                "maximum_repeat_seconds": (
                    TEASER_REPRISE_MAX_REPEAT_SECONDS
                ),
                "repeat_contract_status": teaser_repeat_contract_status,
                "must_show_ids": sorted(teaser_must_show_ids),
                "outside_candidate_must_show_ids": sorted(
                    teaser_outside_must_show_ids
                ),
                "status": (
                    "revision_required"
                    if teaser_failure_codes
                    else "feasible"
                ),
                "failure_codes": sorted(teaser_failure_codes),
                "repair_route": "story_script",
            },
        },
        "status": "awaiting_approval",
    }
    # Re-run the editorial gate after preflight materializes the continuity,
    # ending and duration-extension contracts.  Running only on the raw model
    # draft would leave the newly added cross-segment rules unenforced.
    final_editorial_diagnostics = story_coherence_diagnostics(
        final_script,
        candidates=candidates,
    )
    final_script["feasibility"]["editorial_diagnostics"] = final_editorial_diagnostics
    if final_editorial_diagnostics.get("status") == "blocked":
        final_script["feasibility"]["status"] = "not_feasible"
        final_script["feasibility"]["material_risks"] = list(
            dict.fromkeys(
                final_script["feasibility"].get("material_risks", [])
                + [
                    item["description"]
                    for item in final_editorial_diagnostics.get("findings", [])
                    if item.get("status") == "blocked"
                ]
            )
        )
    errors = validate_task_response("story_script", final_script)
    if errors:
        raise ValueError("invalid preflight Story Script: " + "; ".join(errors[:30]))
    return final_script


def treatment_structure_findings(
    script: dict[str, Any],
) -> list[tuple[str, str]]:
    """Validate Treatment ordering and primary/support Thread ownership."""
    findings: list[tuple[str, str]] = []
    contract = script.get("teaser_contract", {})
    strategy = contract.get("strategy")
    mode = contract.get("mode")
    policy = contract.get("reprise_policy")
    beats = [
        item for item in script.get("beats", []) if isinstance(item, dict)
    ]
    beat_by_id = {
        item["id"]: item
        for item in beats
        if isinstance(item.get("id"), str)
    }
    primary_thread_id = script.get("primary_story_thread_id")
    story_thread_ids = {
        item
        for item in script.get("story_thread_ids", [])
        if isinstance(item, str)
    }
    if primary_thread_id not in story_thread_ids:
        findings.append(
            (
                "primary_story_thread_invalid",
                "primary_story_thread_id 必须属于当前 Story 的 story_thread_ids。",
            )
        )

    expected = {
        STRATEGY_CHRONOLOGICAL: ("none", "not_applicable"),
        STRATEGY_NO_REPRISE: ("single_highlight", "forbidden"),
        STRATEGY_DELAYED_REPRISE: ("single_highlight", "delayed"),
    }
    if strategy not in expected:
        findings.append(
            ("treatment_strategy_unknown", f"未知 Treatment strategy={strategy!r}。")
        )
        return findings
    if (mode, policy) != expected[strategy]:
        findings.append(
            (
                "treatment_strategy_contract_mismatch",
                "Treatment strategy 与 teaser mode/reprise policy 不一致。",
            )
        )
    if not beats:
        findings.append(("treatment_beats_missing", "Story Script 没有 Beat。"))
        return findings
    first = beats[0]
    if strategy == STRATEGY_CHRONOLOGICAL:
        if first.get("role") == "teaser_intent":
            findings.append(
                (
                    "chronological_teaser_forbidden",
                    "chronological_compression 不得包含 teaser_intent。",
                )
            )
        if first.get("temporal_position") != "mainline":
            findings.append(
                (
                    "chronological_opening_not_mainline",
                    "chronological_compression 必须从 mainline 开始。",
                )
            )
    elif (
        first.get("role") != "teaser_intent"
        or first.get("temporal_position") != "future_preview"
    ):
        findings.append(
            (
                "cold_open_definition_invalid",
                "冷开场 Treatment 必须以 future_preview teaser_intent 起手。",
            )
        )

    explanation_ids = [
        item
        for item in contract.get("explanation_beat_ids", [])
        if isinstance(item, str)
    ]
    reprise_ids = [
        item
        for item in contract.get("reprise_beat_ids", [])
        if isinstance(item, str)
    ]
    unknown = sorted((set(explanation_ids) | set(reprise_ids)) - set(beat_by_id))
    if unknown:
        findings.append(
            (
                "treatment_unknown_beat_ids",
                f"Treatment 引用了未知 Beat：{unknown}",
            )
        )
    if strategy in {STRATEGY_NO_REPRISE, STRATEGY_DELAYED_REPRISE}:
        if not explanation_ids:
            findings.append(
                (
                    "opening_explanation_missing",
                    "冷开场必须声明至少一个 explanation Beat。",
                )
            )
        elif any(beat_by_id.get(item) is first for item in explanation_ids):
            findings.append(
                (
                    "opening_explanation_is_teaser",
                    "Teaser 自身不能作为前因解释 Beat。",
                )
            )
    if strategy == STRATEGY_NO_REPRISE and reprise_ids:
        findings.append(
            (
                "no_reprise_declares_reprise",
                "cold_open_no_reprise 的 reprise_beat_ids 必须为空。",
            )
        )
    if strategy == STRATEGY_DELAYED_REPRISE:
        if not reprise_ids:
            findings.append(
                (
                    "delayed_reprise_beat_missing",
                    "cold_open_delayed_reprise 必须声明 reprise Beat。",
                )
            )
        positions = {
            beat["id"]: index
            for index, beat in enumerate(beats)
            if isinstance(beat.get("id"), str)
        }
        explanation_positions = [
            positions[item] for item in explanation_ids if item in positions
        ]
        reprise_positions = [
            positions[item] for item in reprise_ids if item in positions
        ]
        if explanation_positions and reprise_positions:
            last_explanation = max(explanation_positions)
            first_reprise = min(reprise_positions)
            if first_reprise <= last_explanation:
                findings.append(
                    (
                        "delayed_reprise_before_explanation",
                        "Delayed reprise 必须出现在全部 explanation Beat 之后。",
                    )
                )
            progression_roles = {
                "escalation",
                "turn_or_reveal",
                "payoff",
                "end_hook",
            }
            progression_count = sum(
                beat.get("role") in progression_roles
                and beat.get("thread_role") == "primary"
                for beat in beats[last_explanation + 1 : first_reprise]
            )
            minimum = int(
                contract.get(
                    "reprise_delay_minimum_progression_beats", 1
                )
            )
            if progression_count < minimum:
                findings.append(
                    (
                        "delayed_reprise_progression_missing",
                        "Delayed reprise 前没有完成要求数量的主线新推进。",
                    )
                )

    for beat in beats:
        retrieval = beat.get("retrieval_requirements", {})
        beat_thread_ids = {
            item
            for item in retrieval.get("story_thread_ids", [])
            if isinstance(item, str)
        }
        thread_role = beat.get("thread_role")
        if thread_role == "primary" and primary_thread_id not in beat_thread_ids:
            findings.append(
                (
                    "primary_beat_thread_mismatch",
                    f"{beat.get('id')}: thread_role=primary 但未检索主 Thread。",
                )
            )
        if (
            thread_role == "independent_secondary"
            and beat.get("role")
            in {"escalation", "turn_or_reveal", "payoff", "end_hook"}
        ):
            findings.append(
                (
                    "independent_secondary_owns_primary_turn",
                    f"{beat.get('id')}: 独立次线不得承担主转折、Payoff 或 Hook。",
                )
            )
        if (
            beat.get("role") in {"payoff", "end_hook"}
            and primary_thread_id not in beat_thread_ids
        ):
            findings.append(
                (
                    "ending_not_on_primary_thread",
                    f"{beat.get('id')}: Story 结尾必须回到 primary_story_thread_id。",
                )
            )
    return findings


def fact_contract_findings(
    script: dict[str, Any],
    *,
    facts: dict[str, dict[str, Any]],
) -> list[tuple[str, str]]:
    """Validate Beat-local Fact visibility and cross-Beat ordering.

    These checks deliberately run before Approval.  The Plan compiler has a
    defensive Viewer Knowledge gate, but an internally contradictory Script is
    not a Span/Plan repair problem and must never be admitted to cache as a
    valid model response.
    """

    findings: list[tuple[str, str]] = []
    known_facts: set[str] = set()
    fact_event_ids = {
        fact_id: {
            event_id
            for event_id in fact.get("event_ids", [])
            if isinstance(event_id, str)
        }
        for fact_id, fact in facts.items()
        if isinstance(fact, dict)
    }
    for index, beat in enumerate(script.get("beats", [])):
        if not isinstance(beat, dict):
            continue
        beat_id = str(beat.get("id") or f"beats[{index}]")
        hidden = {
            item
            for item in beat.get("must_not_reveal_fact_ids", [])
            if isinstance(item, str)
        }
        required_before = {
            item
            for item in beat.get("required_before_fact_ids", [])
            if isinstance(item, str)
        }
        introduced = {
            item
            for item in beat.get("introduced_fact_ids", [])
            if isinstance(item, str)
        }
        same_required_and_introduced = sorted(required_before & introduced)
        if same_required_and_introduced:
            findings.append(
                (
                    "fact_required_and_introduced_same_beat",
                    f"{beat_id}: Fact must be introduced by an earlier Beat, "
                    "not introduced in the same Beat that requires it: "
                    f"{same_required_and_introduced}",
                )
            )
        same_hidden_and_introduced = sorted(hidden & introduced)
        if same_hidden_and_introduced:
            findings.append(
                (
                    "fact_hidden_and_introduced_same_beat",
                    f"{beat_id}: the same Fact cannot be both withheld and "
                    f"introduced: {same_hidden_and_introduced}",
                )
            )
        missing = sorted(required_before - known_facts)
        if missing and beat.get("role") != "teaser_intent":
            findings.append(
                (
                    "required_fact_not_previously_introduced",
                    f"{beat_id}: required-before Facts were not introduced by "
                    f"an earlier Beat: {missing}",
                )
            )

        hidden_event_ids = {
            event_id
            for fact_id in hidden
            for event_id in fact_event_ids.get(fact_id, set())
        }
        for must_show in beat.get("must_show", []) or []:
            if not isinstance(must_show, dict):
                continue
            direct_event_ids = {
                item
                for item in must_show.get("evidence_event_ids", [])
                if isinstance(item, str)
            }
            conflicting_event_ids = sorted(
                direct_event_ids & hidden_event_ids
            )
            if conflicting_event_ids:
                conflicting_fact_ids = sorted(
                    fact_id
                    for fact_id in hidden
                    if fact_event_ids.get(fact_id, set())
                    & set(conflicting_event_ids)
                )
                findings.append(
                    (
                        "beat_fact_visibility_conflict",
                        f"{beat_id}/{must_show.get('id', '?')}: direct Must-show "
                        "Events belong to Facts withheld by the same Beat; "
                        f"events={conflicting_event_ids}, "
                        f"facts={conflicting_fact_ids}",
                    )
                )
        known_facts.update(introduced)
    return findings


def treatment_viability_findings(
    script: dict[str, Any],
    *,
    events: dict[str, dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
    facts: dict[str, dict[str, Any]],
) -> list[tuple[str, str]]:
    """Reject deterministically impossible selected Treatment semantics.

    This is intentionally conservative: it only calls a Treatment infeasible
    when the Script itself proves the conflict.  Span-level uncertainty stays
    with the downstream Legal Option Compiler.
    """

    contract = script.get("teaser_contract", {})
    strategy = contract.get("strategy")
    if strategy == STRATEGY_CHRONOLOGICAL:
        return []
    beats = [
        item for item in script.get("beats", []) if isinstance(item, dict)
    ]
    if not beats:
        return []
    primary_candidate = candidates.get(
        contract.get("primary_highlight_candidate_id"), {}
    )
    highlight_event_ids = {
        item
        for item in primary_candidate.get("event_ids", [])
        if isinstance(item, str)
    }
    teaser = beats[0]
    teaser_direct_event_ids = {
        event_id
        for must_show in teaser.get("must_show", []) or []
        if isinstance(must_show, dict)
        for event_id in must_show.get("evidence_event_ids", [])
        if isinstance(event_id, str)
    }
    teaser_event_ids = highlight_event_ids | teaser_direct_event_ids
    primary_source_id = primary_candidate.get("source_id")
    primary_start = primary_candidate.get("start")
    primary_end = primary_candidate.get("end")

    def event_forces_highlight_range(event_id: str) -> bool:
        event = events.get(event_id, {})
        if (
            event.get("source_id") != primary_source_id
            or not isinstance(primary_start, (int, float))
            or not isinstance(primary_end, (int, float))
        ):
            return False
        ranges = [
            item
            for item in event.get("source_ranges", [])
            if isinstance(item, dict)
            and isinstance(item.get("start"), (int, float))
            and isinstance(item.get("end"), (int, float))
            and float(item["end"]) > float(item["start"])
        ]
        if not ranges:
            return False
        # Event identity alone is not physical replay.  Only call it proven
        # mandatory when every legal Event range is wholly inside the opening
        # Highlight; a broad scene range may still yield a non-overlapping body
        # Span and must remain a downstream compiler question.
        return all(
            float(item["start"]) >= float(primary_start) - 0.05
            and float(item["end"]) <= float(primary_end) + 0.05
            for item in ranges
        )

    body_mandatory_events: dict[str, set[str]] = {}
    for beat in beats[1:]:
        direct_ids = {
            event_id
            for must_show in beat.get("must_show", []) or []
            if isinstance(must_show, dict)
            for event_id in must_show.get("evidence_event_ids", [])
            if isinstance(event_id, str)
        }
        overlap = {
            event_id
            for event_id in direct_ids & teaser_event_ids
            if event_forces_highlight_range(event_id)
        }
        if overlap:
            body_mandatory_events[str(beat.get("id") or "?")] = overlap

    findings: list[tuple[str, str]] = []
    if strategy == STRATEGY_NO_REPRISE and body_mandatory_events:
        findings.append(
            (
                "no_reprise_mandatory_body_replay",
                "cold_open_no_reprise is infeasible because the opening "
                "Highlight Events are mandatory Must-show evidence in the "
                f"body: { {key: sorted(value) for key, value in body_mandatory_events.items()} }",
            )
        )

    hidden_fact_ids = {
        item
        for item in teaser.get("must_not_reveal_fact_ids", [])
        if isinstance(item, str)
    }
    hidden_event_ids = {
        event_id
        for fact_id in hidden_fact_ids
        for event_id in facts.get(fact_id, {}).get("event_ids", [])
        if isinstance(event_id, str)
    }
    leaked_highlight_events = sorted(highlight_event_ids & hidden_event_ids)
    if leaked_highlight_events:
        findings.append(
            (
                "teaser_highlight_withheld_fact_conflict",
                "Selected Highlight directly reveals a Fact that the Teaser "
                f"must withhold: events={leaked_highlight_events}, "
                f"facts={sorted(hidden_fact_ids)}",
            )
        )
    return findings


def story_script_model_findings(
    script: dict[str, Any],
    *,
    events: dict[str, dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
    facts: dict[str, dict[str, Any]] | None = None,
    thread_beats: dict[str, dict[str, Any]] | None = None,
) -> list[tuple[str, str]]:
    """Validate model-time Script semantics before accepting a response.

    JSON Schema cannot express that Treatment arrays reference sibling
    ``beats[].id`` values.  Keep that self-reference gate, delayed-reprise
    evidence, Teaser stitch safety, and viewer-fact ordering in the semantic
    batch so an invalid model response receives machine feedback and can be
    regenerated before it is cached.
    """

    fact_records = facts or {}
    findings = treatment_structure_findings(script)
    findings.extend(
        treatment_viability_findings(
            script,
            events=events,
            candidates=candidates,
            facts=fact_records,
        )
    )
    findings.extend(fact_contract_findings(script, facts=fact_records))
    beats = [
        item for item in script.get("beats", []) if isinstance(item, dict)
    ]
    if not beats:
        return findings

    thread_beat_records = thread_beats or {}
    for beat_index, beat in enumerate(beats):
        retrieval = beat.get("retrieval_requirements", {})
        if not isinstance(retrieval, dict):
            continue
        referenced_thread_beat_ids = {
            item
            for item in retrieval.get("thread_beat_ids", []) or []
            if isinstance(item, str) and item in thread_beat_records
        }
        if not referenced_thread_beat_ids:
            continue
        allowed_event_ids = {
            event_id
            for thread_beat_id in referenced_thread_beat_ids
            for event_id in thread_beat_records[thread_beat_id].get(
                "event_ids", []
            )
            if isinstance(event_id, str) and event_id
        }
        direct_event_ids = {
            event_id
            for event_id in beat.get("event_ids", []) or []
            if isinstance(event_id, str) and event_id
        }
        direct_event_ids.update(
            event_id
            for event_id in retrieval.get("event_ids", []) or []
            if isinstance(event_id, str) and event_id
        )
        direct_event_ids.update(
            event_id
            for must_show in beat.get("must_show", []) or []
            if isinstance(must_show, dict)
            for event_id in must_show.get("evidence_event_ids", []) or []
            if isinstance(event_id, str) and event_id
        )
        mismatched_event_ids = sorted(direct_event_ids - allowed_event_ids)
        if mismatched_event_ids:
            beat_id = str(beat.get("id") or f"beats[{beat_index}]")
            findings.append(
                (
                    "beat_event_thread_beat_mismatch",
                    f"{beat_id}: 直接 Event {mismatched_event_ids} 不属于该 "
                    "Editorial Beat 引用的 Thread Beat "
                    f"{sorted(referenced_thread_beat_ids)}。",
                )
            )

    contract = script.get("teaser_contract", {})
    primary_candidate_id = contract.get(
        "primary_highlight_candidate_id"
    )
    primary_candidate = candidates.get(primary_candidate_id, {})
    primary_event_ids = {
        item
        for item in primary_candidate.get("event_ids", [])
        if isinstance(item, str)
    }
    if contract.get("mode") == "single_highlight":
        primary_source_id = primary_candidate.get("source_id")
        primary_start = primary_candidate.get("start")
        primary_end = primary_candidate.get("end")
        teaser = beats[0]
        for must_show in teaser.get("must_show", []) or []:
            if not isinstance(must_show, dict):
                continue
            must_show_id = str(must_show.get("id") or "?")
            for event_id in must_show.get("evidence_event_ids", []) or []:
                event = events.get(event_id)
                if event is None:
                    continue
                if event.get("source_id") != primary_source_id:
                    findings.append(
                        (
                            "teaser_cross_source_evidence",
                            f"{must_show_id}: Teaser direct evidence "
                            "与 primary Highlight 不同源。",
                        )
                    )
                    continue
                adjacent, _ = event_can_stitch_to_primary(
                    event,
                    primary_source_id=primary_source_id,
                    primary_start=primary_start,
                    primary_end=primary_end,
                )
                if event_id not in primary_event_ids and not adjacent:
                    findings.append(
                        (
                            "teaser_must_show_outside_stitch_window",
                            f"{must_show_id}: Teaser direct evidence "
                            "不在 primary Highlight 的合法 stitch 窗内。",
                        )
                    )

    if contract.get("reprise_policy") == "delayed":
        declared_reprise_ids = {
            item
            for item in contract.get("reprise_beat_ids", [])
            if isinstance(item, str)
        }
        known_beat_ids = {
            item.get("id")
            for item in beats
            if isinstance(item.get("id"), str)
        }
        # Avoid a redundant evidence error until the self-reference error is
        # fixed; the next semantic attempt can then be judged precisely.
        if declared_reprise_ids and declared_reprise_ids <= known_beat_ids:
            reprise_event_ids = {
                event_id
                for beat in beats[1:]
                if beat.get("id") in declared_reprise_ids
                for event_id in beat.get("event_ids", []) or []
                if isinstance(event_id, str)
            }
            reprise_event_ids.update(
                event_id
                for beat in beats[1:]
                if beat.get("id") in declared_reprise_ids
                for candidate_id in beat.get(
                    "candidate_suggestions", []
                )
                if candidate_id in candidates
                for event_id in candidates[candidate_id].get(
                    "event_ids", []
                )
                if isinstance(event_id, str)
            )
            if not _reprise_matches(
                primary_event_ids,
                reprise_event_ids,
                events,
            ):
                findings.append(
                    (
                        "teaser_delayed_reprise_missing",
                        "声明的 delayed reprise Editorial Beat 没有重现 "
                        "primary Highlight 或同场景等价 Event。",
                    )
                )

    if (
        beats[-1].get("role") != "end_hook"
        and script.get("ending_hook_intent", {}).get("may_be_empty")
        is not True
    ):
        findings.append(
            (
                "last_beat_role_violation",
                "最后一个 Editorial Beat 必须是 end_hook；只有 "
                "ending_hook_intent.may_be_empty=true 时才可用 payoff 结尾。",
            )
        )
    return findings

def render_story_review(scripts: list[dict[str, Any]]) -> str:
    """Render a human-readable Markdown review of story scripts.

    Migrated from _legacy_v4/scripts/assemble_story_artifacts.py.
    """
    finalized = all(
        item.get("status") == "awaiting_approval" and isinstance(item.get("feasibility"), dict)
        for item in scripts
    )
    lines = [
        "# 故事脚本人工审批",
        "",
        (
            "> 当前状态：等待人工确认。未经批准的故事不得进入原片编排。"
            if finalized
            else "> 当前状态：Story Script 草稿，尚未完成素材可行性预检，不得审批。"
        ),
        "",
    ]
    for script in sorted(scripts, key=lambda item: item["story_id"]):
        target = script["target_duration"]
        feasibility = script.get("feasibility", {})
        portfolio = script["portfolio"]
        lines.extend(
            [
                f"## {script['story_id']} · {script['title']}",
                "",
                f"**生产槽位：** {portfolio['production_slot']}（Primary）",
                "",
                f"**一句话故事：** {script['logline']}",
                "",
                f"**故事承诺：** {script['story_promise']}",
                "",
                f"**中心问题：** {script['central_question']}",
                "",
                f"**开始状态：** {script['start_state']}",
                "",
                f"**结束状态：** {script['end_state']}",
                "",
                f"**局部兑现：** {script['local_payoff']}",
                "",
                (
                    "**必需 Thread Beat：** "
                    + ", ".join(script["required_thread_beat_ids"])
                ),
                "",
                (
                    "**已选择 Thread Beat：** "
                    + ", ".join(script["selected_thread_beat_ids"])
                ),
                "",
                (
                    "**时长意图：** 连贯性优先，不设总时长下限；"
                    f"源片总时长上限 {target['maximum_seconds']:.0f} 秒"
                ),
                "",
            ]
        )
        if feasibility:
            lines.extend(
                [
                    "### 素材可行性摘要",
                    "",
                    f"- 状态：`{feasibility['status']}`",
                    (
                        "- 预计可用原片："
                        f"{feasibility['estimated_source_duration_min_seconds']:.0f}–"
                        f"{feasibility['estimated_source_duration_max_seconds']:.0f} 秒"
                    ),
                    "- 时长字段：仅作素材规模审计，不作为 Story Plan 门禁",
                    (
                        "- 待视频复核 Beat："
                        + (
                            ", ".join(feasibility["needs_video_review_beat_ids"])
                            or "无"
                        )
                    ),
                    (
                        "- 缺失 Beat："
                        + (", ".join(feasibility["missing_beat_ids"]) or "无")
                    ),
                    (
                        "- 主要风险："
                        + ("；".join(feasibility["material_risks"]) or "无")
                    ),
                    "",
                ]
            )
        lines.extend(
            [
                "### 剧情节拍",
                "",
            ]
        )
        for index, beat in enumerate(script["beats"], start=1):
            estimate = beat.get("estimated_source_duration_seconds")
            evidence_status = beat.get("evidence_status", "not_checked")
            lines.extend(
                [
                    f"#### {index}. `{beat['role']}` — {beat['dramatic_purpose']}",
                    "",
                    f"**编辑说明：** {beat['narrative_description']}",
                    "",
                    f"**具体故事内容：** {beat['concrete_story_content']}",
                    "",
                    f"**因果位置：** `{beat['causal_role']}`；"
                    f"**时间位置：** `{beat['temporal_position']}`；"
                    f"**素材状态：** `{evidence_status}`",
                    "",
                    "**必须展示：**",
                    "",
                ]
            )
            for item in beat["must_show"]:
                refs = sorted(
                    set(item["evidence_event_ids"]) | set(item["evidence_fact_ids"])
                )
                lines.append(
                    f"- {item['description']}（{item['observable_via']}；"
                    f"证据：{', '.join(refs) or '缺失'}）"
                )
            lines.extend(
                [
                    "",
                    (
                        "**观众状态：** "
                        + "；".join(beat["viewer_state_before"])
                        + " → "
                        + "；".join(beat["viewer_state_after"])
                    ),
                    "",
                    f"**Beat Event：** {', '.join(beat['event_ids']) or '无'}",
                    "",
                    (
                        "**Thread Beat：** "
                        + (
                            ", ".join(
                                beat["retrieval_requirements"]["thread_beat_ids"]
                            )
                            or "无"
                        )
                    ),
                    "",
                    (
                        "**候选建议：** "
                        + (", ".join(beat["candidate_suggestions"]) or "无")
                    ),
                    "",
                ]
            )
            if estimate:
                lines.extend(
                    [
                        (
                            "**预计原片：** "
                            f"{estimate['minimum']:.0f}–{estimate['maximum']:.0f} 秒"
                        ),
                        "",
                    ]
                )
            if beat.get("material_risks"):
                lines.extend(
                    [
                        f"**风险：** {'；'.join(beat['material_risks'])}",
                        "",
                    ]
                )
        hook = script["ending_hook_intent"]
        lines.extend(
            [
                "",
                f"**结尾钩子意图：** {hook['question'] or '无强制 Hook'}",
                "",
                "审批操作：`approved` / `rejected` / `revision_requested` / "
                "`merge_with` / `split_requested`",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def collect_batch_outputs(manifest: dict[str, Any], task: str) -> list[dict[str, Any]]:
    """Collect validated outputs from a batch manifest for a given task.

    Migrated from _legacy_v4/scripts/assemble_story_artifacts.py.
    """
    from autocut_core.io import load_json
    from autocut_core.schema.compat import validate_task_response

    records = []
    for index, job in enumerate(manifest.get("jobs", [])):
        if not isinstance(job, dict) or job.get("task") != task:
            continue
        output = job.get("output")
        if not isinstance(output, str):
            raise ValueError(f"jobs[{index}].output is missing")
        path = Path(output).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"missing batch output: {path}")
        value = load_json(path)
        errors = validate_task_response(task, value)
        if errors:
            raise ValueError(f"{path}: {'; '.join(errors[:20])}")
        records.append(value)
    if not records:
        raise ValueError(f"manifest contains no completed {task} outputs")
    return records


def assemble_scripts_index(
    manifest: dict[str, Any],
    output_path: Path,
    *,
    review_path: Path | None = None,
) -> None:
    """Assemble story scripts from batch manifest into a deterministic index.

    Replaces ``assemble_story_artifacts.py scripts`` CLI invocation.
    Migrated from _legacy_v4/scripts/assemble_story_artifacts.py.
    """
    from autocut_core.io import atomic_write_json, atomic_write_text, load_json

    records = collect_batch_outputs(manifest, "story_script_draft")
    records.sort(key=lambda item: item["story_id"])
    atomic_write_json(
        output_path,
        {
            "schema_version": "1.1",
            "stories": [
                {
                    "story_id": item["story_id"],
                    "title": item["title"],
                    "production_slot": item["portfolio"]["production_slot"],
                    "portfolio_sha256": item["portfolio"]["portfolio_sha256"],
                    "path": next(
                        str(Path(job["output"]).expanduser().resolve())
                        for job in manifest["jobs"]
                        if job.get("task") == "story_script_draft"
                        and Path(job["output"]).is_file()
                        and load_json(Path(job["output"])).get("story_id")
                        == item["story_id"]
                    ),
                }
                for item in records
            ],
        },
    )
    if review_path:
        atomic_write_text(review_path, render_story_review(records))
