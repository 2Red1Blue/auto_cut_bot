"""Validate Story-first artifacts and any materialized downstream stages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autocut_core.io import load_json, load_jsonl, sha256_file
from autocut_core.libs._common import REPRISE_SCENE_IOU_THRESHOLD, _reprise_matches, _scene_equivalent
from autocut_core.contracts.genre_router import has_explicit_genre_contract, route_bible
from autocut_core.schema.compat import validate_task_response
from autocut_core.contracts.span_validation import validate as validate_span_candidates
from autocut_core.contracts.evidence_validation import validate as validate_story_evidence
from autocut_core.contracts.plan_validation import validate as validate_story_plans
from autocut_core.contracts.teaser_contract import (
    TEASER_STITCH_MAX_GAP_SECONDS,
    event_can_stitch_to_primary,
    resolve_must_show_event_ids,
)


# rule 5 (Teaser stitch): must-show 可落在 primary Highlight ±STITCH_GAP 秒内。
# 与 preflight_story_scripts.TEASER_STITCH_GAP_SECONDS / compile_span_candidates
# .TEASER_ATOMIC_STITCH_MAX_GAP_SECONDS 保持一致。
# Bug #2: stitch 窗与 Compiler 实际能拼到的范围对齐。
TEASER_STITCH_GAP_SECONDS = TEASER_STITCH_MAX_GAP_SECONDS


from autocut_core.libs.editorial_knowledge import load_knowledge_section

ABSTRACT_ONLY_PHRASES: set[str]
_abstract_phrases = (load_knowledge_section("abstract_only_phrases") or [])
if _abstract_phrases:
    ABSTRACT_ONLY_PHRASES = set(_abstract_phrases)
else:
    ABSTRACT_ONLY_PHRASES = {
        "矛盾升级",
        "女主反击",
        "男主反击",
        "关系破裂",
        "发现背叛",
        "真相揭晓",
        "冲突升级",
        "完成反转",
        "留下悬念",
    }


def is_abstract_only(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    compact = "".join(value.split()).strip("。！!？?，,；;：:")
    return not compact or compact in ABSTRACT_ONLY_PHRASES or (
        len(compact) < 12
        and any(phrase in compact for phrase in ABSTRACT_ONLY_PHRASES)
    )


def unique_ids(
    records: list[dict[str, Any]], field: str, where: str, errors: list[str]
) -> set[str]:
    ids: set[str] = set()
    for index, item in enumerate(records):
        value = item.get(field)
        if not isinstance(value, str) or not value:
            errors.append(f"{where}[{index}].{field} must be non-empty")
        elif value in ids:
            errors.append(f"{where}: duplicate {field} {value}")
        else:
            ids.add(value)
    return ids


def check_refs(
    values: Any,
    known: set[str],
    where: str,
    errors: list[str],
) -> None:
    if not isinstance(values, list):
        errors.append(f"{where} must be an array")
        return
    unknown = sorted(
        {item for item in values if isinstance(item, str)} - known
    )
    if unknown:
        errors.append(f"{where} contains unknown IDs: {unknown}")


def validate(
    job_root: Path, *, include_downstream: bool = True
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    required = {
        "project": job_root / "project.json",
        "source_manifest": job_root / "source_manifest.json",
        "window_manifest": job_root / "window_manifest.json",
        "window_summaries": job_root / "window-summaries.jsonl",
        "event_cards": job_root / "event-cards.jsonl",
        "candidate_catalog": job_root / "highlight-hook-catalog.json",
        "episode_digests": job_root / "episode-digests.jsonl",
        "chapter_digests": job_root / "chapter-digests.jsonl",
        "series_bible": job_root / "series-bible.json",
        "story_catalog": job_root / "story-catalog.json",
        "story_portfolio": job_root / "story-portfolio.json",
        "story_index": job_root / "story-scripts" / "index.json",
        "story_feasibility": job_root / "story-feasibility.json",
    }
    for label, path in required.items():
        if not path.is_file():
            errors.append(f"missing {label}: {path}")
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}
    project = load_json(required["project"])
    source_manifest = load_json(required["source_manifest"])
    window_manifest = load_json(required["window_manifest"])
    windows = load_jsonl(required["window_summaries"])
    events = load_jsonl(required["event_cards"])
    candidate_catalog = load_json(required["candidate_catalog"])
    episode_digests = load_jsonl(required["episode_digests"])
    chapter_digests = load_jsonl(required["chapter_digests"])
    bible = load_json(required["series_bible"])
    catalog = load_json(required["story_catalog"])
    portfolio = load_json(required["story_portfolio"])
    story_index = load_json(required["story_index"])
    feasibility_summary = load_json(required["story_feasibility"])
    genre_route = route_bible(bible)
    typed_genre_route = has_explicit_genre_contract(bible)
    if typed_genre_route:
        if genre_route["status"] != "ready":
            errors.append(
                "series_bible genre route is not ready; human review is required"
            )
        if bible.get("golden_case_ids", []) != genre_route.get("golden_case_ids", []):
            errors.append("series_bible golden_case_ids are stale or inconsistent")
    sources = source_manifest.get("sources", [])
    manifest_windows = window_manifest.get("windows", [])
    source_ids = unique_ids(sources, "id", "sources", errors)
    window_ids = unique_ids(manifest_windows, "id", "windows", errors)
    result_window_ids: set[str] = set()
    for index, window in enumerate(windows):
        schema_errors = validate_task_response("window_analysis", window)
        errors.extend(f"window_summaries[{index}]: {item}" for item in schema_errors)
        if isinstance(window.get("window_id"), str):
            result_window_ids.add(window["window_id"])
        if window.get("source_id") not in source_ids:
            errors.append(f"window_summaries[{index}] has unknown source_id")
    if result_window_ids != window_ids:
        missing = sorted(window_ids - result_window_ids)
        extra = sorted(result_window_ids - window_ids)
        errors.append(f"window coverage mismatch; missing={missing}, extra={extra}")
    event_ids = unique_ids(events, "id", "events", errors)
    for index, event in enumerate(events):
        if event.get("source_id") not in source_ids:
            errors.append(f"events[{index}] has unknown source_id")
        for range_index, source_range in enumerate(event.get("source_ranges", [])):
            check_refs(
                source_range.get("evidence_window_ids"),
                window_ids,
                f"events[{index}].source_ranges[{range_index}].evidence_window_ids",
                errors,
            )
    candidates = candidate_catalog.get("candidates", [])
    candidate_ids = unique_ids(candidates, "id", "candidates", errors)
    candidate_by_id = {
        item["id"]: item
        for item in candidates
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for index, candidate in enumerate(candidates):
        if candidate.get("source_id") not in source_ids:
            errors.append(f"candidates[{index}] has unknown source_id")
        check_refs(
            candidate.get("event_ids", []),
            event_ids,
            f"candidates[{index}].event_ids",
            errors,
        )
    source_episodes = {
        item.get("episode")
        for item in sources
        if isinstance(item, dict) and isinstance(item.get("episode"), int)
    }
    digest_episodes: set[int] = set()
    for index, digest in enumerate(episode_digests):
        schema_errors = validate_task_response("episode_digest", digest)
        errors.extend(f"episode_digests[{index}]: {item}" for item in schema_errors)
        episode = digest.get("episode")
        if isinstance(episode, int):
            digest_episodes.add(episode)
        check_refs(
            digest.get("window_ids", []),
            window_ids,
            f"episode_digests[{index}].window_ids",
            errors,
        )
        check_refs(
            digest.get("event_ids", []),
            event_ids,
            f"episode_digests[{index}].event_ids",
            errors,
        )
        check_refs(
            digest.get("highlight_candidate_ids", []),
            candidate_ids,
            f"episode_digests[{index}].highlight_candidate_ids",
            errors,
        )
        check_refs(
            digest.get("hook_candidate_ids", []),
            candidate_ids,
            f"episode_digests[{index}].hook_candidate_ids",
            errors,
        )
    if digest_episodes != source_episodes:
        errors.append(
            f"episode digest coverage mismatch: expected={sorted(source_episodes)}, "
            f"actual={sorted(digest_episodes)}"
        )
    for index, chapter in enumerate(chapter_digests):
        schema_errors = validate_task_response("chapter_digest", chapter)
        errors.extend(f"chapter_digests[{index}]: {item}" for item in schema_errors)
        if not set(chapter.get("episodes", [])).issubset(source_episodes):
            errors.append(f"chapter_digests[{index}] contains unknown episode")
        check_refs(
            chapter.get("event_ids", []),
            event_ids,
            f"chapter_digests[{index}].event_ids",
            errors,
        )
    errors.extend(
        f"series_bible: {item}"
        for item in validate_task_response("series_bible", bible)
    )
    coverage = bible.get("coverage", {})
    expected_ingestion_coverage = {
        "source_count": len(sources),
        "episode_count": len(source_episodes),
        "window_count": len(manifest_windows),
        "episode_digest_count": len(episode_digests),
        "missing_episode_ids": [],
    }
    if coverage.get("ingestion_coverage") != expected_ingestion_coverage:
        errors.append(
            "series_bible.coverage.ingestion_coverage mismatch: "
            f"expected {expected_ingestion_coverage}, "
            f"got {coverage.get('ingestion_coverage')}"
        )
    character_ids = unique_ids(bible.get("characters", []), "id", "characters", errors)
    relationship_ids = unique_ids(
        bible.get("relationships", []), "id", "relationships", errors
    )
    fact_ids = unique_ids(bible.get("facts", []), "id", "facts", errors)
    fact_event_ids = {
        item["id"]: {
            event_id
            for event_id in item.get("event_ids", [])
            if isinstance(event_id, str)
        }
        for item in bible.get("facts", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    thread_ids = unique_ids(
        bible.get("story_threads", []), "id", "story_threads", errors
    )
    thread_beat_ids = unique_ids(
        bible.get("thread_beats", []), "id", "thread_beats", errors
    )
    thread_beat_by_id = {
        item["id"]: item
        for item in bible.get("thread_beats", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    question_ids = unique_ids(
        bible.get("open_questions", []), "id", "open_questions", errors
    )
    for index, item in enumerate(bible.get("characters", [])):
        check_refs(
            item.get("evidence_event_ids"),
            event_ids,
            f"characters[{index}].evidence_event_ids",
            errors,
        )
    for index, item in enumerate(bible.get("facts", [])):
        check_refs(
            item.get("event_ids"), event_ids, f"facts[{index}].event_ids", errors
        )
    for index, item in enumerate(bible.get("relationships", [])):
        check_refs(
            item.get("character_ids"),
            character_ids,
            f"relationships[{index}].character_ids",
            errors,
        )
        for change_index, change in enumerate(item.get("state_changes", [])):
            check_refs(
                [change.get("event_id")],
                event_ids,
                f"relationships[{index}].state_changes[{change_index}].event_id",
                errors,
            )
    for index, item in enumerate(bible.get("story_threads", [])):
        check_refs(
            item.get("character_ids"),
            character_ids,
            f"story_threads[{index}].character_ids",
            errors,
        )
        check_refs(
            item.get("event_ids"),
            event_ids,
            f"story_threads[{index}].event_ids",
            errors,
        )
        for field in (
            "setup_event_ids",
            "escalation_event_ids",
            "reveal_event_ids",
            "payoff_event_ids",
        ):
            check_refs(
                item.get(field),
                event_ids,
                f"story_threads[{index}].{field}",
                errors,
            )
        check_refs(
            item.get("open_question_ids"),
            question_ids,
            f"story_threads[{index}].open_question_ids",
            errors,
        )
        check_refs(
            item.get("thread_beat_ids"),
            thread_beat_ids,
            f"story_threads[{index}].thread_beat_ids",
            errors,
        )
        phases = {
            thread_beat_by_id[beat_id].get("phase")
            for beat_id in item.get("thread_beat_ids", [])
            if beat_id in thread_beat_by_id
        }
        if item.get("status") == "resolved" and not {
            "setup",
            "payoff",
        } <= phases:
            errors.append(
                f"story_threads[{index}] is resolved but lacks setup/payoff Thread Beats"
            )
    event_by_id = {
        item["id"]: item
        for item in events
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    covered_episode_ids: set[int] = set()
    for index, item in enumerate(bible.get("thread_beats", [])):
        check_refs(
            [item.get("thread_id")],
            thread_ids,
            f"thread_beats[{index}].thread_id",
            errors,
        )
        check_refs(
            item.get("event_ids"),
            event_ids,
            f"thread_beats[{index}].event_ids",
            errors,
        )
        check_refs(
            item.get("requires_beat_ids"),
            thread_beat_ids,
            f"thread_beats[{index}].requires_beat_ids",
            errors,
        )
        episode = item.get("episode")
        if isinstance(episode, int):
            covered_episode_ids.add(episode)
            wrong_episode = sorted(
                event_id
                for event_id in item.get("event_ids", [])
                if event_id in event_by_id
                and event_by_id[event_id].get("episode") != episode
            )
            if wrong_episode:
                errors.append(
                    f"thread_beats[{index}] binds Events from another episode: "
                    f"{wrong_episode}"
                )
    narrative_coverage = coverage.get("narrative_coverage", {})
    excluded_episodes = narrative_coverage.get("excluded_episodes", [])
    excluded_episode_ids = {
        item.get("episode")
        for item in excluded_episodes
        if isinstance(item, dict) and isinstance(item.get("episode"), int)
    }
    expected_unassigned = sorted(
        source_episodes - covered_episode_ids - excluded_episode_ids
    )
    if narrative_coverage.get("covered_episode_ids") != sorted(covered_episode_ids):
        errors.append(
            "series_bible narrative covered_episode_ids are not derived from Thread Beats"
        )
    if narrative_coverage.get("unassigned_episode_ids") != expected_unassigned:
        errors.append(
            "series_bible narrative unassigned_episode_ids are inconsistent"
        )
    if expected_unassigned:
        errors.append(
            f"series_bible has unassigned narrative episodes: {expected_unassigned}"
        )
    for index, item in enumerate(bible.get("open_questions", [])):
        check_refs(
            item.get("event_ids"),
            event_ids,
            f"open_questions[{index}].event_ids",
            errors,
        )
    errors.extend(
        f"story_catalog: {item}"
        for item in validate_task_response("story_catalog", catalog)
    )
    errors.extend(
        f"story_portfolio: {item}"
        for item in validate_task_response("story_portfolio", portfolio)
    )
    if typed_genre_route and genre_route["status"] == "ready":
        expected_profile = genre_route["genre_profile"]
        expected_cases = set(genre_route["golden_case_ids"])
        if catalog.get("genre_profile") != expected_profile:
            errors.append("story_catalog genre_profile does not match Series Bible")
        if set(catalog.get("golden_case_ids", [])) != expected_cases:
            errors.append("story_catalog golden_case_ids do not match genre route")
        if portfolio.get("genre_profile") != expected_profile:
            errors.append("story_portfolio genre_profile does not match Series Bible")
        if set(portfolio.get("golden_case_ids", [])) != expected_cases:
            errors.append("story_portfolio golden_case_ids do not match genre route")
    stories = catalog.get("stories", [])
    story_ids = unique_ids(stories, "story_id", "catalog.stories", errors)
    story_by_id = {
        item["story_id"]: item
        for item in stories
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }
    primary_story_ids = {
        item
        for item in portfolio.get("primary_story_ids", [])
        if isinstance(item, str)
    }
    reserve_story_ids = {
        item
        for item in portfolio.get("reserve_story_ids", [])
        if isinstance(item, str)
    }
    if len(primary_story_ids) != len(portfolio.get("primary_story_ids", [])):
        errors.append("Story Portfolio contains duplicate Primary Story IDs")
    if len(reserve_story_ids) != len(portfolio.get("reserve_story_ids", [])):
        errors.append("Story Portfolio contains duplicate Reserve Story IDs")
    if primary_story_ids & reserve_story_ids:
        errors.append("Story Portfolio Primary and Reserve sets overlap")
    if primary_story_ids | reserve_story_ids != story_ids:
        errors.append("Story Portfolio does not classify every Catalog Story")
    coverage_summary = portfolio.get("coverage_summary", {})
    check_refs(
        coverage_summary.get("covered_story_thread_ids"),
        thread_ids,
        "story_portfolio.coverage_summary.covered_story_thread_ids",
        errors,
    )
    check_refs(
        coverage_summary.get("uncovered_major_thread_ids"),
        thread_ids,
        "story_portfolio.coverage_summary.uncovered_major_thread_ids",
        errors,
    )
    check_refs(
        coverage_summary.get("covered_character_ids"),
        character_ids,
        "story_portfolio.coverage_summary.covered_character_ids",
        errors,
    )
    check_refs(
        coverage_summary.get("covered_payoff_event_ids"),
        event_ids,
        "story_portfolio.coverage_summary.covered_payoff_event_ids",
        errors,
    )
    for pair_index, pair in enumerate(
        portfolio.get("pairwise_similarity_checks", [])
    ):
        check_refs(
            [pair.get("left_story_id"), pair.get("right_story_id")],
            story_ids,
            f"story_portfolio.pairwise_similarity_checks[{pair_index}]",
            errors,
        )
    if portfolio.get("status") == "ready_for_scripts" and not primary_story_ids:
        errors.append("ready Story Portfolio must contain at least one Primary Story")
    slots = portfolio.get("production_slots", [])
    slot_by_story = {
        item.get("story_id"): item.get("slot")
        for item in slots
        if isinstance(item, dict)
    }
    slot_story_ids = {
        item.get("story_id") for item in slots if isinstance(item, dict)
    }
    slot_numbers = [
        item.get("slot") for item in slots if isinstance(item, dict)
    ]
    if slot_story_ids != primary_story_ids:
        errors.append("production slots must cover Primary Story IDs exactly")
    if len(slot_story_ids) != len(slots):
        errors.append("production slots contain duplicate Story IDs")
    if slot_numbers != list(range(1, len(slot_numbers) + 1)):
        errors.append("production slots must be contiguous starting at 1")
    portfolio_sha256 = sha256_file(required["story_portfolio"])
    if feasibility_summary.get("portfolio_sha256") != portfolio_sha256:
        errors.append("story-feasibility.json portfolio SHA-256 is stale")
    for index, story in enumerate(stories):
        check_refs(
            story.get("character_ids"),
            character_ids,
            f"catalog.stories[{index}].character_ids",
            errors,
        )
        check_refs(
            story.get("relationship_ids"),
            relationship_ids,
            f"catalog.stories[{index}].relationship_ids",
            errors,
        )
        check_refs(
            story.get("story_thread_ids"),
            thread_ids,
            f"catalog.stories[{index}].story_thread_ids",
            errors,
        )
        source_thread_beat_ids = {
            item
            for item in story.get("source_thread_beat_ids", [])
            if isinstance(item, str)
        }
        check_refs(
            story.get("source_thread_beat_ids"),
            thread_beat_ids,
            f"catalog.stories[{index}].source_thread_beat_ids",
            errors,
        )
        check_refs(
            [story.get("subarc_start_beat_id")],
            thread_beat_ids,
            f"catalog.stories[{index}].subarc_start_beat_id",
            errors,
        )
        check_refs(
            [story.get("subarc_end_beat_id")],
            thread_beat_ids,
            f"catalog.stories[{index}].subarc_end_beat_id",
            errors,
        )
        check_refs(
            story.get("required_bridge_beat_ids"),
            thread_beat_ids,
            f"catalog.stories[{index}].required_bridge_beat_ids",
            errors,
        )
        required_arc_ids = {
            story.get("subarc_start_beat_id"),
            story.get("subarc_end_beat_id"),
            *story.get("required_bridge_beat_ids", []),
        }
        if not required_arc_ids <= source_thread_beat_ids:
            errors.append(
                f"catalog.stories[{index}] has required arc Beats outside "
                "source_thread_beat_ids"
            )
        check_refs(
            story.get("required_fact_ids"),
            fact_ids,
            f"catalog.stories[{index}].required_fact_ids",
            errors,
        )
        check_refs(
            story.get("evidence_event_ids"),
            event_ids,
            f"catalog.stories[{index}].evidence_event_ids",
            errors,
        )
        check_refs(
            story.get("suggested_highlight_candidate_ids"),
            candidate_ids,
            f"catalog.stories[{index}].suggested_highlight_candidate_ids",
            errors,
        )
        check_refs(
            story.get("suggested_hook_candidate_ids"),
            candidate_ids,
            f"catalog.stories[{index}].suggested_hook_candidate_ids",
            errors,
        )
    indexed = story_index.get("stories", [])
    indexed_ids = {
        item.get("story_id") for item in indexed if isinstance(item, dict)
    }
    if indexed_ids != primary_story_ids:
        errors.append(
            f"story script coverage mismatch: expected={sorted(primary_story_ids)}, "
            f"actual={sorted(item for item in indexed_ids if isinstance(item, str))}"
        )
    feasibility_by_story = {
        item.get("story_id"): item
        for item in feasibility_summary.get("stories", [])
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }
    if set(feasibility_by_story) != primary_story_ids:
        errors.append(
            "story feasibility coverage mismatch: "
            f"expected={sorted(primary_story_ids)}, "
            f"actual={sorted(feasibility_by_story)}"
        )
    fulfillment = project.get("fulfillment", {})
    if fulfillment.get("proposal_count") != len(story_ids):
        errors.append("project.fulfillment.proposal_count is stale")
    if fulfillment.get("primary_script_count") != len(indexed_ids):
        errors.append("project.fulfillment.primary_script_count is stale")
    for index, entry in enumerate(indexed):
        path_value = entry.get("path")
        if not isinstance(path_value, str):
            errors.append(f"story_index.stories[{index}].path is missing")
            continue
        path = Path(path_value).expanduser().resolve()
        if not path.is_file():
            errors.append(f"missing story script: {path}")
            continue
        script = load_json(path)
        errors.extend(
            f"{path.name}: {item}"
            for item in validate_task_response("story_script", script)
        )
        if script.get("story_id") != entry.get("story_id"):
            errors.append(f"{path.name}: story_id does not match index")
        if typed_genre_route and genre_route["status"] == "ready":
            if script.get("genre_profile") != genre_route["genre_profile"]:
                errors.append(f"{path.name}: genre_profile does not match Series Bible")
            if set(script.get("golden_case_ids", [])) != set(
                genre_route.get("golden_case_ids", [])
            ):
                errors.append(f"{path.name}: golden_case_ids do not match genre route")
        portfolio_binding = script.get("portfolio", {})
        if portfolio_binding.get("portfolio_sha256") != portfolio_sha256:
            errors.append(f"{path.name}: portfolio SHA-256 is stale")
        expected_slot = slot_by_story.get(script.get("story_id"))
        if portfolio_binding.get("production_slot") != expected_slot:
            errors.append(f"{path.name}: production slot does not match Portfolio")
        if entry.get("production_slot") != expected_slot:
            errors.append(f"{path.name}: index production slot is stale")
        if entry.get("portfolio_sha256") != portfolio_sha256:
            errors.append(f"{path.name}: index portfolio SHA-256 is stale")
        scope_policy = script.get("scope_policy")
        if isinstance(scope_policy, dict):
            if scope_policy.get("analysis_unit_policy") != "processing_only":
                errors.append(
                    f"{path.name}: analysis units cannot be used as story boundaries"
                )
            if scope_policy.get("story_scope_policy") != "series_global":
                errors.append(
                    f"{path.name}: Story Script scope must be series_global"
                )
            if scope_policy.get("cross_unit_retrieval_allowed") is not True:
                errors.append(
                    f"{path.name}: cross-unit causal retrieval is disabled"
                )
            ancestor_range = script.get("causal_ancestor_episode_range")
            if not isinstance(ancestor_range, dict):
                errors.append(
                    f"{path.name}: missing causal_ancestor_episode_range"
                )
            elif not (
                isinstance(ancestor_range.get("min_episode"), int)
                and isinstance(ancestor_range.get("max_episode"), int)
                and ancestor_range["min_episode"] <= ancestor_range["max_episode"]
            ):
                errors.append(
                    f"{path.name}: invalid causal_ancestor_episode_range"
                )
            check_refs(
                script.get("required_context_ids"),
                character_ids
                | relationship_ids
                | fact_ids
                | event_ids
                | thread_ids
                | thread_beat_ids
                | question_ids,
                f"{path.name}.required_context_ids",
                errors,
            )
            if script.get("edit_mode") == "montage":
                explanation_beats = [
                    beat
                    for beat in script.get("beats", [])
                    if isinstance(beat.get("causal_dependency"), dict)
                    and beat["causal_dependency"].get(
                        "explains_opening_highlight"
                    ) is True
                ]
                if not explanation_beats:
                    errors.append(
                        f"{path.name}: montage lacks an explicit opening causal dependency Beat"
                    )
                for beat in explanation_beats:
                    dependency = beat["causal_dependency"]
                    check_refs(
                        dependency.get("required_before_fact_ids"),
                        fact_ids,
                        f"{path.name}.beats[{beat.get('id')}].causal_dependency.required_before_fact_ids",
                        errors,
                    )
                    check_refs(
                        dependency.get("required_relationship_ids"),
                        relationship_ids,
                        f"{path.name}.beats[{beat.get('id')}].causal_dependency.required_relationship_ids",
                        errors,
                    )
                    check_refs(
                        dependency.get("required_event_ids"),
                        event_ids,
                        f"{path.name}.beats[{beat.get('id')}].causal_dependency.required_event_ids",
                        errors,
                    )
                    check_refs(
                        dependency.get("required_thread_beat_ids"),
                        thread_beat_ids,
                        f"{path.name}.beats[{beat.get('id')}].causal_dependency.required_thread_beat_ids",
                        errors,
                    )
                    retrieval = dependency.get("cross_unit_retrieval", {})
                    if retrieval.get("required") is not True:
                        errors.append(
                            f"{path.name}.beats[{beat.get('id')}]: opening causal dependency must require cross-unit retrieval"
                        )
        roles = [beat.get("role") for beat in script.get("beats", [])]
        for required_role in ("teaser_intent", "escalation", "payoff"):
            if required_role not in roles:
                errors.append(f"{path.name}: missing required beat {required_role}")
        if not ({"orientation", "setup"} & set(roles)):
            errors.append(f"{path.name}: requires orientation or setup")
        if roles and roles[0] != "teaser_intent":
            errors.append(f"{path.name}: first beat must be teaser_intent")
        hook = script.get("ending_hook_intent", {})
        if hook.get("may_be_empty") is False and (not roles or roles[-1] != "end_hook"):
            errors.append(f"{path.name}: last beat must be end_hook")
        check_refs(
            script.get("character_ids"),
            character_ids,
            f"{path.name}.character_ids",
            errors,
        )
        check_refs(
            script.get("relationship_ids"),
            relationship_ids,
            f"{path.name}.relationship_ids",
            errors,
        )
        check_refs(
            script.get("story_thread_ids"),
            thread_ids,
            f"{path.name}.story_thread_ids",
            errors,
        )
        check_refs(
            script.get("selected_thread_beat_ids"),
            thread_beat_ids,
            f"{path.name}.selected_thread_beat_ids",
            errors,
        )
        check_refs(
            script.get("required_thread_beat_ids"),
            thread_beat_ids,
            f"{path.name}.required_thread_beat_ids",
            errors,
        )
        omitted_thread_beat_ids = {
            item.get("thread_beat_id")
            for item in script.get("omitted_thread_beats", [])
            if isinstance(item, dict)
            and isinstance(item.get("thread_beat_id"), str)
        }
        check_refs(
            sorted(omitted_thread_beat_ids),
            thread_beat_ids,
            f"{path.name}.omitted_thread_beats",
            errors,
        )
        selected_thread_beat_ids = {
            item
            for item in script.get("selected_thread_beat_ids", [])
            if isinstance(item, str)
        }
        required_thread_beat_ids = {
            item
            for item in script.get("required_thread_beat_ids", [])
            if isinstance(item, str)
        }
        catalog_story = story_by_id.get(script.get("story_id"), {})
        source_thread_beat_ids = {
            item
            for item in catalog_story.get("source_thread_beat_ids", [])
            if isinstance(item, str)
        }
        expected_required_thread_beats = {
            catalog_story.get("subarc_start_beat_id"),
            catalog_story.get("subarc_end_beat_id"),
            *catalog_story.get("required_bridge_beat_ids", []),
            *[
                beat_id
                for beat_id in source_thread_beat_ids
                if thread_beat_by_id.get(beat_id, {}).get("importance") == "required"
            ],
        }
        expected_required_thread_beats.discard(None)
        if required_thread_beat_ids != expected_required_thread_beats:
            errors.append(
                f"{path.name}: required Thread Beats differ from Catalog contract"
            )
        if selected_thread_beat_ids | omitted_thread_beat_ids != source_thread_beat_ids:
            errors.append(
                f"{path.name}: selected/omitted Thread Beats do not account for "
                "the Catalog subarc"
            )
        if selected_thread_beat_ids & omitted_thread_beat_ids:
            errors.append(
                f"{path.name}: a Thread Beat cannot be both selected and omitted"
            )
        if not required_thread_beat_ids <= selected_thread_beat_ids:
            errors.append(f"{path.name}: required Thread Beats were omitted")
        check_refs(
            script.get("required_fact_ids"),
            fact_ids,
            f"{path.name}.required_fact_ids",
            errors,
        )
        check_refs(
            script.get("intentional_mystery_fact_ids"),
            fact_ids,
            f"{path.name}.intentional_mystery_fact_ids",
            errors,
        )
        check_refs(
            script.get("evidence_event_ids"),
            event_ids,
            f"{path.name}.evidence_event_ids",
            errors,
        )
        script_evidence_events = {
            item
            for item in script.get("evidence_event_ids", [])
            if isinstance(item, str)
        }
        known_facts: set[str] = set()
        introduced_facts: set[str] = set()
        referenced_evidence_events: set[str] = set()
        beat_ids: set[str] = set()
        causal_roles: set[str] = set()
        observed_statuses: dict[str, set[str]] = {
            "covered": set(),
            "partial": set(),
            "missing": set(),
            "conflicting": set(),
            "needs_video_review": set(),
        }
        # rule 1: reprise 白名单包含 end_hook（短剧常见开头挂钩→结尾兑现）。
        _reprise_roles = {"escalation", "turn_or_reveal", "payoff", "end_hook"}
        downstream_highlight_event_ids = {
            event_id
            for later_beat in script.get("beats", [])[1:]
            if later_beat.get("role") in _reprise_roles
            for event_id in later_beat.get("event_ids", [])
            if isinstance(event_id, str)
        }
        downstream_highlight_event_ids.update(
            event_id
            for later_beat in script.get("beats", [])[1:]
            if later_beat.get("role") in _reprise_roles
            for candidate_id in later_beat.get(
                "candidate_suggestions", []
            )
            if candidate_id in candidate_by_id
            for event_id in candidate_by_id[candidate_id].get(
                "event_ids", []
            )
            if isinstance(event_id, str)
        )
        teaser_contract = script.get("teaser_contract", {})
        opening_strategy = teaser_contract.get(
            "opening_strategy", "future_preview_reprise"
        )
        primary_teaser_id = teaser_contract.get(
            "primary_highlight_candidate_id"
        )
        primary_teaser = candidate_by_id.get(primary_teaser_id, {})
        retrieved_thread_beat_ids: set[str] = set()
        for beat_index, beat in enumerate(script.get("beats", [])):
            beat_id = beat.get("id")
            if isinstance(beat_id, str):
                if beat_id in beat_ids:
                    errors.append(f"{path.name}: duplicate beat id {beat_id}")
                beat_ids.add(beat_id)
            causal_role = beat.get("causal_role")
            if isinstance(causal_role, str):
                causal_roles.add(causal_role)
            if is_abstract_only(beat.get("concrete_story_content")):
                errors.append(
                    f"{path.name}.beats[{beat_index}].concrete_story_content "
                    "is abstract or too vague"
                )
            check_refs(
                beat.get("required_before_fact_ids"),
                fact_ids,
                f"{path.name}.beats[{beat_index}].required_before_fact_ids",
                errors,
            )
            check_refs(
                beat.get("introduced_fact_ids"),
                fact_ids,
                f"{path.name}.beats[{beat_index}].introduced_fact_ids",
                errors,
            )
            check_refs(
                beat.get("must_not_reveal_fact_ids"),
                fact_ids,
                f"{path.name}.beats[{beat_index}].must_not_reveal_fact_ids",
                errors,
            )
            check_refs(
                beat.get("resolved_question_ids"),
                question_ids,
                f"{path.name}.beats[{beat_index}].resolved_question_ids",
                errors,
            )
            check_refs(
                beat.get("event_ids"),
                event_ids,
                f"{path.name}.beats[{beat_index}].event_ids",
                errors,
            )
            check_refs(
                beat.get("candidate_suggestions"),
                candidate_ids,
                f"{path.name}.beats[{beat_index}].candidate_suggestions",
                errors,
            )
            retrieval = beat.get("retrieval_requirements", {})
            for field, known in (
                ("character_ids", character_ids),
                ("relationship_ids", relationship_ids),
                ("story_thread_ids", thread_ids),
                ("thread_beat_ids", thread_beat_ids),
                ("fact_ids", fact_ids),
                ("event_ids", event_ids),
                ("candidate_ids", candidate_ids),
            ):
                check_refs(
                    retrieval.get(field),
                    known,
                    f"{path.name}.beats[{beat_index}].retrieval_requirements.{field}",
                    errors,
                )
            retrieved_thread_beat_ids.update(
                item
                for item in retrieval.get("thread_beat_ids", [])
                if isinstance(item, str)
            )
            beat_event_ids = {
                item for item in beat.get("event_ids", []) if isinstance(item, str)
            }
            referenced_evidence_events.update(beat_event_ids)
            beat_grounded_events = set(beat_event_ids)
            must_show_ids: set[str] = set()
            must_show_events: dict[str, set[str]] = {}
            # Bug #1: 单独保留每个 must_show 的 direct evidence，用于 teaser
            # cross-source / stitch 窗判定；fact-expanded 事件不进入这个集合。
            must_show_direct_events: dict[str, set[str]] = {}
            missing_must_show = 0
            for item_index, must_show in enumerate(beat.get("must_show", [])):
                must_show_id = must_show.get("id")
                if isinstance(must_show_id, str):
                    if must_show_id in must_show_ids:
                        errors.append(
                            f"{path.name}.beats[{beat_index}] duplicate must_show id "
                            f"{must_show_id}"
                        )
                    must_show_ids.add(must_show_id)
                check_refs(
                    must_show.get("evidence_event_ids"),
                    event_ids,
                    f"{path.name}.beats[{beat_index}].must_show[{item_index}]"
                    ".evidence_event_ids",
                    errors,
                )
                check_refs(
                    must_show.get("evidence_fact_ids"),
                    fact_ids,
                    f"{path.name}.beats[{beat_index}].must_show[{item_index}]"
                    ".evidence_fact_ids",
                    errors,
                )
                (
                    item_direct_events,
                    _item_fact_events,
                    item_events,
                ) = resolve_must_show_event_ids(
                    must_show, fact_event_ids
                )
                referenced_evidence_events.update(item_events)
                beat_grounded_events.update(item_events)
                if isinstance(must_show_id, str):
                    must_show_events[must_show_id] = item_events
                    must_show_direct_events[must_show_id] = item_direct_events
                if not item_events:
                    missing_must_show += 1
            evidence_status = beat.get("evidence_status")
            if isinstance(evidence_status, str) and isinstance(beat_id, str):
                if evidence_status in observed_statuses:
                    observed_statuses[evidence_status].add(beat_id)
            if missing_must_show and evidence_status not in {"partial", "missing"}:
                errors.append(
                    f"{path.name}.beats[{beat_index}] has ungrounded must_show "
                    f"but status is {evidence_status}"
                )
            if (
                beat.get("must_have")
                and not beat_grounded_events
                and evidence_status not in {"missing", "partial"}
            ):
                errors.append(
                    f"{path.name}.beats[{beat_index}] must-have Beat lacks Event evidence"
                )
            estimate = beat.get("estimated_source_duration_seconds", {})
            minimum = estimate.get("minimum")
            maximum = estimate.get("maximum")
            if (
                isinstance(minimum, (int, float))
                and isinstance(maximum, (int, float))
                and minimum > maximum
            ):
                errors.append(
                    f"{path.name}.beats[{beat_index}] duration minimum exceeds maximum"
                )
            hidden = {
                item
                for item in beat.get("must_not_reveal_fact_ids", [])
                if isinstance(item, str)
            }
            introduced_now = {
                item
                for item in beat.get("introduced_fact_ids", [])
                if isinstance(item, str)
            }
            if hidden & introduced_now:
                errors.append(
                    f"{path.name}.beats[{beat_index}] hides and introduces the same facts: "
                    f"{sorted(hidden & introduced_now)}"
                )
            if beat.get("role") == "teaser_intent":
                teaser_candidates = beat.get("candidate_suggestions", [])
                if teaser_candidates != [primary_teaser_id]:
                    errors.append(
                        f"{path.name}.beats[{beat_index}] "
                        "teaser_multiple_highlights"
                    )
                expected_opening_position = (
                    "mainline"
                    if opening_strategy == "original_chronological_opening"
                    else "future_preview"
                )
                if beat.get("temporal_position") != expected_opening_position:
                    errors.append(
                        f"{path.name}.beats[{beat_index}] teaser must use "
                        f"temporal_position={expected_opening_position}"
                    )
                if not teaser_candidates:
                    errors.append(
                        f"{path.name}.beats[{beat_index}] teaser must bind "
                        "a highlight Candidate"
                    )
                teaser_candidate_events: set[str] = set()
                usable_teaser_duration = False
                for candidate_id in teaser_candidates:
                    if candidate_by_id.get(candidate_id, {}).get("type") != "highlight":
                        errors.append(
                            f"{path.name}.beats[{beat_index}] teaser candidate "
                            f"{candidate_id} is not highlight"
                        )
                    candidate = candidate_by_id.get(candidate_id, {})
                    start, end = candidate.get("start"), candidate.get("end")
                    if (
                        isinstance(start, (int, float))
                        and isinstance(end, (int, float))
                        and 0 < float(end) - float(start) <= 30.0
                    ):
                        usable_teaser_duration = True
                    teaser_candidate_events.update(
                        event_id
                        for event_id in candidate.get("event_ids", [])
                        if isinstance(event_id, str)
                    )
                if teaser_candidates and not usable_teaser_duration:
                    errors.append(
                        f"{path.name}.beats[{beat_index}] "
                        "teaser_atomic_interval_over_limit"
                    )
                primary_source_id = primary_teaser.get("source_id")
                primary_start = primary_teaser.get("start")
                primary_end = primary_teaser.get("end")
                primary_event_ids = {
                    item
                    for item in primary_teaser.get("event_ids", [])
                    if isinstance(item, str)
                }
                # Bug #1: 只对 direct evidence 做 teaser 源片位置校验；fact-expanded
                # 事件天然可以跨集，不代表 must-show 位置在别的 source。
                for must_show_id, must_show_event_ids in must_show_direct_events.items():
                    for event_id in must_show_event_ids:
                        event = event_by_id.get(event_id, {})
                        if event.get("source_id") != primary_source_id:
                            errors.append(
                                f"{path.name}.beats[{beat_index}]."
                                f"{must_show_id} teaser_cross_source_evidence"
                            )
                            continue
                        adjacent, _ = event_can_stitch_to_primary(
                            event,
                            primary_source_id=primary_source_id,
                            primary_start=primary_start,
                            primary_end=primary_end,
                        )
                        if event_id not in primary_event_ids and not adjacent:
                            errors.append(
                                f"{path.name}.beats[{beat_index}]."
                                f"{must_show_id} "
                                "teaser_must_show_outside_stitch_window"
                            )
                if (
                    opening_strategy == "future_preview_reprise"
                    and teaser_candidates
                    and not _reprise_matches(
                    teaser_candidate_events,
                    downstream_highlight_event_ids,
                    event_by_id,
                    )
                ):
                    errors.append(
                        f"{path.name}.beats[{beat_index}] "
                        "teaser_not_reprised_in_body"
                    )
            if beat.get("role") == "end_hook":
                for candidate_id in beat.get("candidate_suggestions", []):
                    if candidate_by_id.get(candidate_id, {}).get("type") != "hook":
                        errors.append(
                            f"{path.name}.beats[{beat_index}] hook candidate "
                            f"{candidate_id} is not hook"
                        )
            required_before = {
                item
                for item in beat.get("required_before_fact_ids", [])
                if isinstance(item, str)
            }
            missing_before = sorted(required_before - known_facts)
            if missing_before and beat.get("role") != "teaser_intent":
                errors.append(
                    f"{path.name}.beats[{beat_index}] requires facts not yet introduced: "
                    f"{missing_before}"
                )
            newly_introduced = {
                item
                for item in beat.get("introduced_fact_ids", [])
                if isinstance(item, str)
            }
            introduced_facts.update(newly_introduced)
            known_facts.update(newly_introduced)
        if not selected_thread_beat_ids <= retrieved_thread_beat_ids:
            errors.append(
                f"{path.name}: selected Thread Beats are not all referenced by "
                "retrieval requirements"
            )
        for required_causal_role in ("cause", "escalation", "payoff"):
            if required_causal_role not in causal_roles:
                errors.append(
                    f"{path.name}: missing causal role {required_causal_role}"
                )
        required_story_facts = {
            item for item in script.get("required_fact_ids", []) if isinstance(item, str)
        }
        never_introduced = sorted(required_story_facts - introduced_facts)
        if never_introduced:
            errors.append(
                f"{path.name}: required story facts are never introduced: {never_introduced}"
            )
        check_refs(
            hook.get("story_thread_ids"),
            thread_ids,
            f"{path.name}.ending_hook_intent.story_thread_ids",
            errors,
        )
        check_refs(
            hook.get("event_ids"),
            event_ids,
            f"{path.name}.ending_hook_intent.event_ids",
            errors,
        )
        check_refs(
            hook.get("candidate_ids"),
            candidate_ids,
            f"{path.name}.ending_hook_intent.candidate_ids",
            errors,
        )
        referenced_evidence_events.update(
            item for item in hook.get("event_ids", []) if isinstance(item, str)
        )
        uncovered_events = sorted(referenced_evidence_events - script_evidence_events)
        if uncovered_events:
            errors.append(
                f"{path.name}.evidence_event_ids does not cover Beat/Hook evidence: "
                f"{uncovered_events}"
            )
        feasibility = script.get("feasibility", {})
        status_fields = {
            "covered": "covered_beat_ids",
            "partial": "partial_beat_ids",
            "missing": "missing_beat_ids",
            "conflicting": "conflicting_beat_ids",
            "needs_video_review": "needs_video_review_beat_ids",
        }
        listed_status_ids: set[str] = set()
        for status_name, field in status_fields.items():
            values = {
                item
                for item in feasibility.get(field, [])
                if isinstance(item, str)
            }
            unknown_beats = sorted(values - beat_ids)
            if unknown_beats:
                errors.append(
                    f"{path.name}.feasibility.{field} has unknown Beat IDs: "
                    f"{unknown_beats}"
                )
            if values != observed_statuses[status_name]:
                errors.append(
                    f"{path.name}.feasibility.{field} does not match Beat statuses"
                )
            duplicate_status = sorted(listed_status_ids & values)
            if duplicate_status:
                errors.append(
                    f"{path.name}.feasibility assigns Beat more than once: "
                    f"{duplicate_status}"
                )
            listed_status_ids.update(values)
        if listed_status_ids != beat_ids:
            errors.append(
                f"{path.name}.feasibility does not classify every Beat exactly once"
            )
        feasibility_minimum = feasibility.get(
            "estimated_source_duration_min_seconds"
        )
        feasibility_maximum = feasibility.get(
            "estimated_source_duration_max_seconds"
        )
        if (
            isinstance(feasibility_minimum, (int, float))
            and isinstance(feasibility_maximum, (int, float))
            and feasibility_minimum > feasibility_maximum
        ):
            errors.append(f"{path.name}: feasibility duration minimum exceeds maximum")
        if isinstance(feasibility_maximum, (int, float)):
            # `meets_5_minimum` is retained for artifact compatibility; this
            # project variant has no Story Plan duration lower bound.
            if feasibility.get("meets_5_minimum") != (feasibility_maximum >= 0):
                errors.append(f"{path.name}: meets_5_minimum is inconsistent")
            if feasibility.get("meets_10_preferred") != (
                feasibility_maximum >= 600
            ):
                errors.append(f"{path.name}: meets_10_preferred is inconsistent")
        must_have_unusable = [
            beat.get("id")
            for beat in script.get("beats", [])
            if beat.get("must_have")
            and beat.get("evidence_status") in {"missing", "conflicting"}
        ]
        if must_have_unusable and feasibility.get("status") != "not_feasible":
            errors.append(
                f"{path.name}: missing/conflicting must-have Beat requires "
                "not_feasible status"
            )
        summary = feasibility_by_story.get(script.get("story_id"))
        if isinstance(summary, dict):
            expected_summary = {
                key: value for key, value in summary.items() if key != "story_id"
            }
            if expected_summary != feasibility:
                errors.append(
                    f"{path.name}: feasibility differs from story-feasibility.json"
                )
        if entry.get("feasibility_status") != feasibility.get("status"):
            errors.append(f"{path.name}: index feasibility_status is stale")
        if entry.get("estimated_source_duration_min_seconds") != feasibility_minimum:
            errors.append(f"{path.name}: index minimum duration is stale")
        if entry.get("estimated_source_duration_max_seconds") != feasibility_maximum:
            errors.append(f"{path.name}: index maximum duration is stale")
    if not candidates:
        warnings.append("candidate catalog is empty; scripts may have no teaser/hook candidates")
    if (
        include_downstream
        and (job_root / "story-evidence" / "index.json").is_file()
    ):
        evidence_report = validate_story_evidence(job_root)
        errors.extend(
            f"story_evidence: {item}" for item in evidence_report["errors"]
        )
        warnings.extend(
            f"story_evidence: {item}" for item in evidence_report["warnings"]
        )
    if (
        include_downstream
        and (job_root / "span-candidates" / "index.json").is_file()
    ):
        span_report = validate_span_candidates(job_root)
        errors.extend(
            f"span_candidates: {item}" for item in span_report["errors"]
        )
        warnings.extend(
            f"span_candidates: {item}" for item in span_report["warnings"]
        )
    if (
        include_downstream
        and (job_root / "story-plans" / "index.json").is_file()
    ):
        plan_report = validate_story_plans(job_root)
        errors.extend(
            f"story_plans: {item}" for item in plan_report["errors"]
        )
        warnings.extend(
            f"story_plans: {item}" for item in plan_report["warnings"]
        )
    return {"ok": not errors, "errors": errors, "warnings": warnings}
