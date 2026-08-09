#!/usr/bin/env python3
"""Select every distinct, evidence-backed Primary Story deterministically.

Migrated from _legacy_v4/scripts/build_story_portfolio.py (pure functions only).
"""

from __future__ import annotations

from itertools import combinations
from typing import Any

from autocut_core.contracts.genre_router import has_explicit_genre_contract, route_bible
from autocut_core.io import normalize_text
from autocut_core.schema.compat import validate_task_response


def jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def similarity(
    left: dict[str, Any], right: dict[str, Any]
) -> tuple[float, bool, list[str]]:
    same_question = (
        normalize_text(left.get("central_question")).casefold()
        == normalize_text(right.get("central_question")).casefold()
    )
    same_payoff = (
        normalize_text(left.get("payoff_summary")).casefold()
        == normalize_text(right.get("payoff_summary")).casefold()
    )
    thread_overlap = jaccard(
        set(left.get("story_thread_ids", [])),
        set(right.get("story_thread_ids", [])),
    )
    event_overlap = jaccard(
        set(left.get("evidence_event_ids", [])),
        set(right.get("evidence_event_ids", [])),
    )
    score = round(
        0.35 * float(same_question)
        + 0.30 * float(same_payoff)
        + 0.20 * thread_overlap
        + 0.15 * event_overlap,
        4,
    )
    reasons = []
    if same_question:
        reasons.append("same_central_question")
    if same_payoff:
        reasons.append("same_local_payoff")
    if thread_overlap >= 0.8:
        reasons.append("high_story_thread_overlap")
    if event_overlap >= 0.8:
        reasons.append("high_event_overlap")
    near_duplicate = same_question or same_payoff or score >= 0.72
    return score, near_duplicate, reasons


def rank_score(story: dict[str, Any]) -> float:
    scores = story.get("scores", {})
    positive = sum(
        float(scores.get(field, 0))
        for field in (
            "story_completeness",
            "independent_clarity",
            "highlight_relevance",
            "source_sufficiency",
            "causal_clarity",
            "hook_alignment",
        )
    )
    background_cost = float(scores.get("background_cost", 10))
    duration_bonus = {
        "strong": 3.0,
        "viable": 1.5,
        "short": 0.0,
        "insufficient": -10.0,
    }.get(story.get("duration_feasibility"), -10.0)
    return positive - background_cost + duration_bonus


def build_portfolio(
    catalog: dict[str, Any], bible: dict[str, Any]
) -> dict[str, Any]:
    schema_errors = validate_task_response("story_catalog", catalog)
    if schema_errors:
        raise ValueError("invalid Story Catalog: " + "; ".join(schema_errors[:30]))
    genre_route = route_bible(bible)
    typed_route = has_explicit_genre_contract(bible)
    if typed_route:
        if genre_route["status"] != "ready":
            raise ValueError(
                "cannot build Story Portfolio before genre review is complete"
            )
        if catalog.get("genre_profile") != genre_route["genre_profile"]:
            raise ValueError("Story Catalog genre_profile does not match Series Bible")
        if set(catalog.get("golden_case_ids", [])) != set(
            genre_route.get("golden_case_ids", [])
        ):
            raise ValueError("Story Catalog golden_case_ids do not match genre route")
        for item in catalog.get("stories", []):
            if item.get("genre_profile") != genre_route["genre_profile"]:
                raise ValueError(
                    f"{item.get('story_id')} genre_profile does not match Series Bible"
                )
            if set(item.get("golden_case_ids", [])) != set(
                genre_route.get("golden_case_ids", [])
            ):
                raise ValueError(
                    f"{item.get('story_id')} golden_case_ids do not match genre route"
                )
    stories = list(catalog["stories"])
    story_by_id = {item["story_id"]: item for item in stories}
    if len(story_by_id) != len(stories):
        raise ValueError("Story Catalog contains duplicate story_id")
    checks = []
    near_duplicate_pairs: set[frozenset[str]] = set()
    for left, right in combinations(stories, 2):
        score, near_duplicate, reasons = similarity(left, right)
        checks.append(
            {
                "left_story_id": left["story_id"],
                "right_story_id": right["story_id"],
                "similarity_score": score,
                "near_duplicate": near_duplicate,
                "reasons": reasons,
            }
        )
        if near_duplicate:
            near_duplicate_pairs.add(
                frozenset((left["story_id"], right["story_id"]))
            )
    ranked = sorted(
        stories,
        key=lambda item: (-rank_score(item), item["story_id"]),
    )
    primary: list[str] = []
    reserve: list[str] = []
    for story in ranked:
        story_id = story["story_id"]
        eligible = story.get("duration_feasibility") != "insufficient"
        duplicates_primary = any(
            frozenset((story_id, selected)) in near_duplicate_pairs
            for selected in primary
        )
        if eligible and not duplicates_primary:
            primary.append(story_id)
        else:
            reserve.append(story_id)
    if not primary:
        raise ValueError(
            "Story Catalog contains no distinct Story with sufficient source material"
        )
    status = "ready_for_scripts"
    insufficiency_reasons = []
    if reserve:
        insufficiency_reasons.append(
            f"{len(reserve)} 个候选因素材不足或与更高排名 Story 近重复而进入 Reserve"
        )
    selected_stories = [story_by_id[story_id] for story_id in primary]
    covered_threads = sorted(
        {
            thread_id
            for story in selected_stories
            for thread_id in story.get("story_thread_ids", [])
        }
    )
    covered_characters = sorted(
        {
            character_id
            for story in selected_stories
            for character_id in story.get("character_ids", [])
        }
    )
    thread_by_id = {
        item["id"]: item
        for item in bible.get("story_threads", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    covered_payoffs = sorted(
        {
            event_id
            for thread_id in covered_threads
            for event_id in thread_by_id.get(thread_id, {}).get(
                "payoff_event_ids", []
            )
        }
    )
    major_threads = {
        thread_id
        for thread_id, item in thread_by_id.items()
        if item.get("status") in {"open", "partially_resolved", "resolved"}
    }
    portfolio = {
        "schema_version": "1.0",
        "status": status,
        **(
            {
                "genre_profile": genre_route["genre_profile"],
                "golden_case_ids": list(genre_route.get("golden_case_ids", [])),
            }
            if typed_route
            else {}
        ),
        "primary_story_ids": primary,
        "reserve_story_ids": reserve,
        "production_slots": [
            {"slot": index, "story_id": story_id}
            for index, story_id in enumerate(primary, start=1)
        ],
        "coverage_summary": {
            "covered_story_thread_ids": covered_threads,
            "covered_character_ids": covered_characters,
            "covered_payoff_event_ids": covered_payoffs,
            "uncovered_major_thread_ids": sorted(major_threads - set(covered_threads)),
        },
        "pairwise_similarity_checks": checks,
        "insufficiency_reasons": insufficiency_reasons,
    }
    errors = validate_task_response("story_portfolio", portfolio)
    if errors:
        raise ValueError("invalid Story Portfolio: " + "; ".join(errors[:30]))
    return portfolio