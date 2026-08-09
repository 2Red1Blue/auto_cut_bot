#!/usr/bin/env python3
"""Automate scope expansion for Stories blocked before Story Plan Qwen call.

Two entry points share the same expansion logic:

* ``--source plan_preflight``: reads
  ``story-plan-preflight.json`` for Stories whose failure codes include
  ``insufficient_editorial_surplus``, ``no_partition_meets_minimum_duration``,
  or ``no_legal_body_partition``.
* ``--source script_preflight``: reads ``story-script-preflight.json`` for
  Stories flagged ``awaiting_scope_merge`` (Script preflight estimates
  ``maximum_total < 400s``, i.e. below the hard-minimum buffer). Script
  preflight emits the same ``failure_codes`` /
  ``editorial_surplus_diagnostics`` shape so ``_detect_trigger`` consumes
  either file unchanged.
* ``--source auto`` (default): prefers ``story-plan-preflight.json`` when
  present, else falls back to ``story-script-preflight.json``. Callers at the
  Script stage should pass ``--source script_preflight`` explicitly.

For each eligible Story, inspects the Series Bible for adjacent Thread
Beats in every Story Thread the Story touches, prefers those that pull
in **new** source episodes (to help ``no_legal_body_partition`` cases
where the current body beats concentrate in one episode), and emits
``story-scope-expansion.json`` with a concrete expansion plan.

With ``--apply``, mutates the approved ``story-scripts/<story-id>.json``
in place by appending the recommended Thread Beat IDs to
``selected_thread_beat_ids`` and to the last body Beat's
``retrieval_requirements.thread_beat_ids``, then resets that Story's
decision in ``story-approval.json`` to
``revision_requested_auto_scope_expansion``. Each ``auto_scope_expansion``
entry records ``trigger_source`` so downstream tools can distinguish
Script-preflight-initiated from Plan-preflight-initiated expansions.

The user must re-run ``preflight_story_scripts`` → approval → evidence →
span → plan preflight to pick up the expansion.

All functions are copied exactly as-is with only import paths updated.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any

from autocut_core.io import (
    atomic_write_json,
    load_json,
    load_jsonl,
    sha256_file,
    update_project_stage,
)


AUTO_SCOPE_EXPANSION_DECISION = "revision_requested_auto_scope_expansion"
AUTO_SCOPE_REVERT_DECISION = "revision_requested_auto_scope_revert"

TRIGGER_SOURCE_PLAN_PREFLIGHT = "plan_preflight"
TRIGGER_SOURCE_SCRIPT_PREFLIGHT = "script_preflight"
TRIGGER_SOURCE_AUTO = "auto"

VALID_TRIGGER_SOURCES = frozenset(
    {
        TRIGGER_SOURCE_PLAN_PREFLIGHT,
        TRIGGER_SOURCE_SCRIPT_PREFLIGHT,
        TRIGGER_SOURCE_AUTO,
    }
)

# Failure codes we know how to auto-expand for.
from autocut_core.libs.editorial_knowledge import load_knowledge_section

_scope_expansion = load_knowledge_section("scope_expansion") or {}
EXPANDABLE_FAILURE_CODES = frozenset(
    _scope_expansion.get("expandable_failure_codes")
    or {
        "insufficient_editorial_surplus",
        "no_partition_meets_minimum_duration",
        "no_legal_body_partition",
    }
)

SCOPE_EXPANSION_TARGET_ROLES = tuple(
    _scope_expansion.get("target_roles")
    or ("setup", "escalation", "turn_or_reveal")
)


def _events_by_thread_beat(
    thread_beats: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Return {thread_beat_id: [event, ...]} using thread_beats[].event_ids.

    ``events`` may be either the Series Bible's inline events list (rare) or
    the ``event-cards.jsonl`` records (usual). Both formats expose ``id`` and
    ``source_ranges``.
    """
    events_by_id: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = event.get("id")
        if isinstance(event_id, str):
            events_by_id[event_id] = event
    result: dict[str, list[dict[str, Any]]] = {}
    for beat in thread_beats:
        beat_id = beat.get("id")
        if not isinstance(beat_id, str):
            continue
        bucket = []
        for event_id in beat.get("event_ids", []):
            event = events_by_id.get(event_id)
            if event is not None:
                bucket.append(event)
        result[beat_id] = bucket
    return result


def _event_duration_estimate(event: dict[str, Any]) -> float:
    """Estimate the unique source duration an Event covers using its ranges."""
    ranges = event.get("source_ranges", [])
    total = 0.0
    for item in ranges:
        try:
            start = float(item.get("start", 0.0))
            end = float(item.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        if end > start:
            total += end - start
    return total


def _thread_beat_duration_estimate(
    events: list[dict[str, Any]],
) -> float:
    return round(sum(_event_duration_estimate(item) for item in events), 3)


def _story_thread_of(
    thread_beat_ids: list[str],
    thread_beats: list[dict[str, Any]],
) -> str | None:
    thread_id_by_beat = {
        item.get("id"): item.get("thread_id") for item in thread_beats
    }
    candidates = {
        thread_id_by_beat.get(beat_id) for beat_id in thread_beat_ids
    }
    candidates.discard(None)
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def _story_threads_of(
    thread_beat_ids: list[str],
    thread_beats: list[dict[str, Any]],
) -> list[str]:
    """Return every Story Thread id referenced by ``thread_beat_ids``."""
    thread_id_by_beat = {
        item.get("id"): item.get("thread_id") for item in thread_beats
    }
    threads: list[str] = []
    seen: set[str] = set()
    for beat_id in thread_beat_ids:
        thread_id = thread_id_by_beat.get(beat_id)
        if isinstance(thread_id, str) and thread_id not in seen:
            threads.append(thread_id)
            seen.add(thread_id)
    return threads


def _thread_beat_order(bible: dict[str, Any]) -> dict[str, int]:
    order: dict[str, int] = {}
    for index, beat in enumerate(bible.get("thread_beats", [])):
        beat_id = beat.get("id")
        if isinstance(beat_id, str):
            order[beat_id] = index
    return order


def _load_span_bundle(job_root: Path, story_id: str) -> dict[str, Any]:
    path = job_root / "span-candidates" / f"{story_id}.json"
    if not path.is_file():
        return {}
    return load_json(path)


def _current_source_footprint(
    span_bundle: dict[str, Any],
    body_beat_ids: set[str],
) -> dict[str, set[str]]:
    """Return ``{source_id: {body_beat_id, ...}}`` from the current bundle.

    Only counts candidates that support at least one body beat, so we can see
    which sources are hosting multiple beats (concentration hot-spots).
    """
    result: dict[str, set[str]] = defaultdict(set)
    for candidate in span_bundle.get("candidates", []):
        if not isinstance(candidate, dict):
            continue
        source_id = candidate.get("source_id")
        if not isinstance(source_id, str):
            continue
        for beat_id in candidate.get("supports_beat_ids", []):
            if beat_id in body_beat_ids:
                result[source_id].add(beat_id)
    return {k: set(v) for k, v in result.items()}


def _concentrated_source_ids(
    footprint: dict[str, set[str]],
    *,
    beat_threshold: int = 2,
) -> set[str]:
    """Sources currently hosting ``beat_threshold`` or more body beats."""
    return {
        source_id
        for source_id, beats in footprint.items()
        if len(beats) >= beat_threshold
    }


def _detect_trigger(
    preflight_entry: dict[str, Any],
) -> tuple[str | None, float, dict[str, Any]]:
    """Return ``(trigger, deficit_seconds, diagnostic)``.

    ``trigger`` is one of ``insufficient_surplus`` /
    ``no_partition_meets_minimum`` / ``no_legal_body_partition``, or ``None``
    when the Story is not eligible for automatic scope expansion.
    """
    failure_codes = set(preflight_entry.get("failure_codes", []))
    surplus = preflight_entry.get("editorial_surplus_diagnostics", {})
    partition_diag = preflight_entry.get("body_partition_diagnostics", {})
    minimum_total = float(
        surplus.get("minimum_total_duration_seconds", 0.0) or 0.0
    )
    available = float(
        surplus.get("available_candidate_unique_duration_seconds", 0.0)
        or 0.0
    )
    max_partition_dur = float(
        partition_diag.get("maximum_partition_total_duration_seconds", 0.0)
        or 0.0
    )
    minimum_target = minimum_total * 1.1
    auto_merge_hint = preflight_entry.get("auto_merge_hint")
    hinted_deficit = (
        float(auto_merge_hint.get("deficit_seconds", 0.0) or 0.0)
        if isinstance(auto_merge_hint, dict)
        else 0.0
    )
    # Compatibility utility for explicit structural expansion requests. The
    # active Story Plan variant never invokes it for duration-only deficits.
    scope_expansion_target = max(
        minimum_target,
        available + max(0.0, hinted_deficit),
    )
    diagnostic = {
        "minimum_total_duration_seconds": minimum_total,
        "minimum_target_with_surplus_seconds": round(minimum_target, 3),
        "scope_expansion_target_seconds": round(
            scope_expansion_target, 3
        ),
        "auto_merge_hint_deficit_seconds": round(
            max(0.0, hinted_deficit), 3
        ),
        "available_candidate_unique_duration_seconds": available,
        "maximum_partition_total_duration_seconds": max_partition_dur,
    }
    if "insufficient_editorial_surplus" in failure_codes:
        return (
            "insufficient_surplus",
            max(0.0, scope_expansion_target - available),
            diagnostic,
        )
    if "no_partition_meets_minimum_duration" in failure_codes:
        # Even with available > minimum, span-disjoint filter caps partition
        # duration below the minimum. We need enough new material that adding
        # a Thread Beat lets a longer partition through.
        return (
            "no_partition_meets_minimum",
            max(0.0, minimum_target - max_partition_dur),
            diagnostic,
        )
    if "no_legal_body_partition" in failure_codes:
        # Body beats concentrate on 1-2 sources so no legal partition exists.
        # Aim to add roughly one Story minimum worth of material from a
        # different source.
        return (
            "no_legal_body_partition",
            minimum_target if minimum_target > 0 else 0.0,
            diagnostic,
        )
    return None, 0.0, diagnostic


def _propose_expansion(
    story_id: str,
    preflight_entry: dict[str, Any],
    script: dict[str, Any],
    bible: dict[str, Any],
    event_cards: list[dict[str, Any]],
    span_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trigger, deficit, deficit_diag = _detect_trigger(preflight_entry)
    if trigger is None:
        return {
            "story_id": story_id,
            "status": "not_triggered",
            "reason": (
                "preflight failure codes do not include an expandable trigger"
            ),
        }

    selected = list(script.get("selected_thread_beat_ids", []))
    required = set(script.get("required_thread_beat_ids", []))
    thread_beats = bible.get("thread_beats", [])
    threads = _story_threads_of(selected, thread_beats)
    if not threads:
        return {
            "story_id": story_id,
            "status": "no_thread_context",
            "reason": (
                "selected_thread_beat_ids does not resolve to any Story "
                "Thread; cannot auto-expand"
            ),
            "current_selected_thread_beat_ids": selected,
        }

    body_beat_ids = {
        beat.get("id")
        for beat in script.get("beats", [])
        if isinstance(beat, dict)
        and beat.get("must_have")
        and beat.get("role") != "teaser_intent"
        and isinstance(beat.get("id"), str)
    }
    footprint = _current_source_footprint(
        span_bundle or {}, body_beat_ids
    )
    concentrated_sources = _concentrated_source_ids(footprint)
    current_sources = set(footprint.keys())

    order = _thread_beat_order(bible)
    events_by_thread_beat = _events_by_thread_beat(
        thread_beats, event_cards
    )
    thread_beats_by_id = {
        item.get("id"): item for item in thread_beats
    }
    selected_indices_per_thread: dict[str, list[int]] = defaultdict(list)
    for beat_id in selected:
        beat = thread_beats_by_id.get(beat_id, {})
        thread_id = beat.get("thread_id")
        idx = order.get(beat_id)
        if isinstance(thread_id, str) and idx is not None:
            selected_indices_per_thread[thread_id].append(idx)
    for thread_id in list(selected_indices_per_thread):
        selected_indices_per_thread[thread_id].sort()

    def episodes_for_thread_beat(beat: dict[str, Any]) -> list[Any]:
        events = events_by_thread_beat.get(beat.get("id", ""), [])
        # Prefer explicit source_id / episode from Event's source_ranges.
        sources: dict[Any, None] = {}
        for event in events:
            for range_ref in event.get("source_ranges", []):
                if not isinstance(range_ref, dict):
                    continue
                source_id = range_ref.get("source_id")
                if source_id is None:
                    continue
                sources[source_id] = None
        if sources:
            return list(sources)
        # Fallback: Series Bible's beat has "episode" field.
        episode_hint = beat.get("episode")
        return [episode_hint] if episode_hint is not None else []

    candidates: list[dict[str, Any]] = []
    for thread_id in threads:
        pool = [
            beat
            for beat in thread_beats
            if beat.get("thread_id") == thread_id
            and beat.get("id") not in set(selected)
        ]
        selected_indices = selected_indices_per_thread.get(thread_id, [])
        anchor_low = selected_indices[0] if selected_indices else 0
        anchor_high = selected_indices[-1] if selected_indices else 0
        for beat in pool:
            beat_id = beat.get("id")
            if not isinstance(beat_id, str):
                continue
            events = events_by_thread_beat.get(beat_id, [])
            estimated = _thread_beat_duration_estimate(events)
            episodes = episodes_for_thread_beat(beat)
            # A Thread Beat "adds a new source" if any of its episodes is not
            # already among the current bundle's sources.
            adds_new_source = bool(
                episodes
                and not all(
                    _source_matches_existing(episode, current_sources)
                    for episode in episodes
                )
            )
            # It "breaks concentration" if any episode is NOT one of the
            # over-concentrated sources.
            breaks_concentration = bool(
                episodes
                and any(
                    not _source_matches_existing(
                        episode, concentrated_sources
                    )
                    for episode in episodes
                )
            )
            idx = order.get(beat_id, 10**9)
            distance = min(
                abs(idx - anchor_low), abs(idx - anchor_high)
            )
            candidates.append(
                {
                    "thread_beat_id": beat_id,
                    "thread_id": thread_id,
                    "thread_beat_summary": beat.get("summary", ""),
                    "estimated_extra_source_seconds": estimated,
                    "event_count": len(events),
                    "series_order_index": idx,
                    "distance_to_selected": distance,
                    "episode_hint": beat.get("episode"),
                    "episodes_touched": episodes,
                    "adds_new_source": adds_new_source,
                    "breaks_concentration": breaks_concentration,
                }
            )

    # Sort priority depends on trigger:
    # - insufficient_surplus / no_partition_meets_minimum: raw duration first
    # - no_legal_body_partition: source diversity first (must add a new
    #   source to break the concentration), then duration
    if trigger == "no_legal_body_partition":
        sort_key = lambda item: (
            not item["adds_new_source"],
            not item["breaks_concentration"],
            item["distance_to_selected"],
            -item["estimated_extra_source_seconds"],
            item["series_order_index"],
        )
    else:
        sort_key = lambda item: (
            item["distance_to_selected"],
            not item["adds_new_source"],
            -item["estimated_extra_source_seconds"],
            item["series_order_index"],
        )
    candidates.sort(key=sort_key)

    accepted: list[dict[str, Any]] = []
    cumulative = 0.0
    diversity_achieved = False
    for candidate in candidates:
        accepted.append(candidate)
        cumulative += candidate["estimated_extra_source_seconds"]
        if candidate["adds_new_source"]:
            diversity_achieved = True
        if trigger == "no_legal_body_partition":
            # For the concentration case, stop once we have at least one
            # new-source Beat AND enough estimated material to plausibly
            # form a longer partition (>= half the minimum works as a soft
            # target; the downstream Span Compiler decides the true ceiling).
            if diversity_achieved and cumulative >= max(
                deficit * 0.5, 60.0
            ):
                break
        else:
            if cumulative >= deficit:
                break

    projected_available = round(
        deficit_diag["available_candidate_unique_duration_seconds"]
        + cumulative,
        3,
    )
    minimum_total = deficit_diag["minimum_total_duration_seconds"]
    projected_surplus_ratio = (
        round((projected_available - minimum_total) / minimum_total, 3)
        if minimum_total > 0
        else 0.0
    )

    if trigger == "no_legal_body_partition":
        status = (
            "recommended"
            if accepted and diversity_achieved
            else "insufficient_diversity"
            if accepted
            else "no_adjacent_thread_beats"
        )
    else:
        status = (
            "recommended"
            if cumulative >= deficit and accepted
            else "insufficient_neighbouring_material"
            if accepted
            else "no_adjacent_thread_beats"
        )

    alternatives = [
        item["thread_beat_id"]
        for item in candidates
        if item["thread_beat_id"]
        not in {row["thread_beat_id"] for row in accepted}
    ]
    result = {
        "story_id": story_id,
        "trigger": trigger,
        "status": status,
        "story_thread_ids": threads,
        "current_selected_thread_beat_ids": selected,
        "required_thread_beat_ids": sorted(required),
        "current_source_ids": sorted(current_sources),
        "concentrated_source_ids": sorted(concentrated_sources),
        "minimum_total_duration_seconds": minimum_total,
        "available_candidate_unique_duration_seconds": deficit_diag[
            "available_candidate_unique_duration_seconds"
        ],
        "maximum_partition_total_duration_seconds": deficit_diag[
            "maximum_partition_total_duration_seconds"
        ],
        "deficit_seconds": round(deficit, 3),
        "add_thread_beat_ids": [
            item["thread_beat_id"] for item in accepted
        ],
        "projected_available_candidate_seconds": projected_available,
        "projected_editorial_surplus_ratio": projected_surplus_ratio,
        "diversity_achieved": diversity_achieved,
        "proposals": accepted,
        "alternative_thread_beat_ids": alternatives,
    }
    # Keep the singular field for backwards compat when only one
    # Story Thread is touched.
    if len(threads) == 1:
        result["story_thread_id"] = threads[0]
    return result


def _source_matches_existing(episode: Any, existing: set[str]) -> bool:
    """Fuzzy match a Thread Beat's episode to existing bundle source_ids.

    The Thread Beat's ``episode`` field is an integer, while span-candidate
    bundles use ``source_id`` strings like ``"ep039"``. We accept either an
    exact match or an integer that ends up as ``ep{N}`` / ``ep{N:03d}``.
    """
    if not existing:
        return False
    if isinstance(episode, str):
        return episode in existing
    try:
        number = int(episode)
    except (TypeError, ValueError):
        return False
    formatted = {
        f"ep{number}",
        f"ep{number:02d}",
        f"ep{number:03d}",
    }
    return bool(formatted & existing)


def _choose_scope_expansion_target(
    body_beats: list[dict[str, Any]],
    expansion_thread_beat_ids: set[str],
) -> dict[str, Any] | None:
    """Choose a narratively compatible body Beat for scope material.

    Scope expansion supplies setup/escalation/reveal context.  It must never
    be silently attached to a payoff or end hook merely because that Beat is
    last in the Script.
    """
    compatible = [
        beat
        for beat in body_beats
        if beat.get("role") in SCOPE_EXPANSION_TARGET_ROLES
    ]
    if not compatible:
        return None
    for beat in compatible:
        existing = set(
            beat.get("retrieval_requirements", {}).get(
                "thread_beat_ids", []
            )
        )
        if expansion_thread_beat_ids & existing:
            return beat
    for role in SCOPE_EXPANSION_TARGET_ROLES:
        for beat in compatible:
            if beat.get("role") == role:
                return beat
    return None


def _apply_expansion_to_script(
    script_path: Path,
    add_thread_beat_ids: list[str],
    *,
    trigger_source: str | None = None,
    allow_whole_series: bool = False,
) -> dict[str, Any]:
    script = load_json(script_path)
    selected = list(script.get("selected_thread_beat_ids", []))
    added = [beat for beat in add_thread_beat_ids if beat not in selected]
    beats = script.get("beats", [])
    body_beats = [
        beat for beat in beats if beat.get("role") != "teaser_intent"
    ]
    if not body_beats:
        return {
            "applied": False,
            "status": "requires_story_script_revision",
            "reason": "Story Script has no body Beat for scope expansion",
        }
    # Gather every Thread Beat that has already been auto-attached in a
    # prior F4 run so we can locate the target beat (and fix its lookback)
    # even when there is nothing new to add.
    historical_attached: set[str] = set()
    for entry in script.get("auto_scope_expansion", []):
        if not isinstance(entry, dict):
            continue
        for key in ("added_thread_beat_ids", "attached_thread_beat_ids"):
            historical_attached.update(entry.get(key, []) or [])
    combined_expansion_beats: set[str] = set(
        add_thread_beat_ids or []
    ) | historical_attached

    target_beat = _choose_scope_expansion_target(
        body_beats, combined_expansion_beats
    )
    if target_beat is None:
        return {
            "applied": False,
            "status": "requires_story_script_revision",
            "reason": (
                "No compatible setup/escalation/turn_or_reveal Beat exists; "
                "automatic expansion will not attach material to payoff/end_hook"
            ),
        }

    retrieval = target_beat.setdefault("retrieval_requirements", {})
    target_beat_id = target_beat.get("id")
    historical_lookbacks: dict[str, tuple[str, str]] = {}
    for entry in script.get("auto_scope_expansion", []):
        if not isinstance(entry, dict):
            continue
        historical_target = entry.get("target_beat_id")
        widened_from = entry.get("target_lookback_widened_from")
        widened_to = entry.get("target_lookback_widened_to")
        if (
            isinstance(historical_target, str)
            and isinstance(widened_from, str)
            and isinstance(widened_to, str)
            and historical_target not in historical_lookbacks
        ):
            historical_lookbacks[historical_target] = (
                widened_from,
                widened_to,
            )
    migrated_from_beat_ids: list[str] = []
    # Migrate placements away from payoff/end_hook.  Only
    # auto-attached Thread Beats are removed; native retrieval requirements
    # remain untouched.
    for beat in body_beats:
        if beat is target_beat or not isinstance(beat, dict):
            continue
        beat_retrieval = beat.get("retrieval_requirements", {})
        existing_ids = list(beat_retrieval.get("thread_beat_ids", []))
        if not (set(existing_ids) & combined_expansion_beats):
            continue
        beat_retrieval["thread_beat_ids"] = [
            item
            for item in existing_ids
            if item not in combined_expansion_beats
        ]
        beat_id = beat.get("id")
        if isinstance(beat_id, str):
            migrated_from_beat_ids.append(beat_id)
            historical_lookback = historical_lookbacks.get(beat_id)
            if (
                historical_lookback is not None
                and beat_retrieval.get("lookback")
                == historical_lookback[1]
            ):
                beat_retrieval["lookback"] = historical_lookback[0]
    existing = list(retrieval.get("thread_beat_ids", []))
    newly_attached: list[str] = []
    attachment_ids = list(
        dict.fromkeys(
            [
                *add_thread_beat_ids,
                *sorted(historical_attached),
            ]
        )
    )
    for beat_id in attachment_ids:
        if beat_id not in existing:
            existing.append(beat_id)
            newly_attached.append(beat_id)
    retrieval["thread_beat_ids"] = existing
    if added:
        script["selected_thread_beat_ids"] = selected + added
        omitted = [
            item
            for item in script.get("omitted_thread_beats", [])
            if item.get("thread_beat_id") not in set(added)
        ]
        script["omitted_thread_beats"] = omitted
    # Widen the target Beat's lookback so Evidence actually pulls the
    # attached Thread Beats' events. Without this,
    # ``filter_expanded_events`` in ``build_story_evidence_packet``
    # silently drops them under ``same_episode``.
    previous_lookback = retrieval.get("lookback", "same_episode")
    lookback_widened_from = None
    desired_lookback = previous_lookback
    target_historical_lookback = historical_lookbacks.get(
        str(target_beat_id)
    )
    if newly_attached or combined_expansion_beats:
        if allow_whole_series:
            desired_lookback = "whole_series"
        elif previous_lookback == "same_episode":
            desired_lookback = "earlier_episodes"
        elif (
            previous_lookback == "whole_series"
            and target_historical_lookback is not None
            and target_historical_lookback[1] == "whole_series"
        ):
            # Downgrade whole_series that was introduced automatically by
            # the policy.  User-authored whole_series remains intact.
            desired_lookback = "earlier_episodes"
    if desired_lookback != previous_lookback:
        retrieval["lookback"] = desired_lookback
        lookback_widened_from = previous_lookback
    # Determine whether beats carry stale post-preflight enrichments that
    # should be stripped before this script re-enters preflight.
    beat_enrichment_keys = (
        "estimated_source_duration_seconds",
        "evidence_status",
        "material_risks",
    )
    beats_need_strip = any(
        isinstance(beat, dict)
        and any(key in beat for key in beat_enrichment_keys)
        for beat in script.get("beats", [])
    )
    # Nothing changed — leave the script untouched (idempotent no-op).
    if (
        not added
        and not newly_attached
        and not migrated_from_beat_ids
        and lookback_widened_from is None
        and not beats_need_strip
    ):
        return {"applied": False, "status": "no_change"}
    # Reset feasibility & status so a fresh preflight regenerates them.
    script["status"] = "draft"
    script.pop("feasibility", None)
    # Strip beat-level enrichments that preflight_story_scripts adds when
    # promoting draft -> awaiting_approval; they will be recomputed on the
    # next preflight and their presence would trip the draft schema.
    for beat in script.get("beats", []):
        if isinstance(beat, dict):
            for key in beat_enrichment_keys:
                beat.pop(key, None)
    entry: dict[str, Any] = {
        "added_thread_beat_ids": added,
        "attached_thread_beat_ids": newly_attached,
        "target_beat_id": target_beat_id,
    }
    if migrated_from_beat_ids:
        entry["migrated_from_beat_ids"] = sorted(
            set(migrated_from_beat_ids)
        )
    if lookback_widened_from is not None:
        entry["target_lookback_widened_from"] = previous_lookback
        entry["target_lookback_widened_to"] = desired_lookback
    if trigger_source is not None:
        entry["trigger_source"] = trigger_source
    script.setdefault("auto_scope_expansion", []).append(entry)
    atomic_write_json(script_path, script)
    return {
        "applied": True,
        "status": "applied",
        "target_beat_id": target_beat_id,
        "lookback": retrieval.get("lookback"),
        "migrated_from_beat_ids": sorted(set(migrated_from_beat_ids)),
    }


def _reset_approval_entry(
    approval_path: Path,
    story_id: str,
    reason: str,
    add_thread_beat_ids: list[str],
) -> None:
    approval = load_json(approval_path)
    stories = approval.get("stories", [])
    for entry in stories:
        if entry.get("story_id") != story_id:
            continue
        entry["decision"] = AUTO_SCOPE_EXPANSION_DECISION
        entry["approved_script_sha256"] = ""
        entry["reason"] = reason
        entry.setdefault("auto_scope_expansion", []).append(
            {
                "added_thread_beat_ids": add_thread_beat_ids,
            }
        )
        break
    approval["fulfillment_status"] = "requires_rework"
    approval["status"] = "story_scope_expansion_pending"
    if story_id in approval.get("selected_story_ids", []):
        approval["selected_story_ids"] = [
            item
            for item in approval["selected_story_ids"]
            if item != story_id
        ]
    atomic_write_json(approval_path, approval)


def _revert_expansion_from_script(script_path: Path) -> dict[str, Any]:
    """Undo every ``auto_scope_expansion`` entry recorded in the script.

    Returns a summary of what was reverted, or ``{"reverted": False}`` when
    the script has no expansion history.
    """
    script = load_json(script_path)
    history = script.get("auto_scope_expansion")
    if not history:
        return {"reverted": False}

    # Aggregate every Thread Beat that any prior F4 pass added or attached.
    added_ids: list[str] = []
    attached_ids: list[str] = []
    target_beat_ids: list[str] = []
    lookback_changes: dict[str, tuple[str, str]] = {}
    for entry in history:
        if not isinstance(entry, dict):
            continue
        for key, bucket in (
            ("added_thread_beat_ids", added_ids),
            ("attached_thread_beat_ids", attached_ids),
        ):
            for beat_id in entry.get(key, []) or []:
                if beat_id not in bucket:
                    bucket.append(beat_id)
        tb = entry.get("target_beat_id")
        if isinstance(tb, str) and tb not in target_beat_ids:
            target_beat_ids.append(tb)
        widened_from = entry.get("target_lookback_widened_from")
        if (
            isinstance(tb, str)
            and isinstance(widened_from, str)
            and isinstance(
                entry.get("target_lookback_widened_to"), str
            )
            and tb not in lookback_changes
        ):
            lookback_changes[tb] = (
                widened_from,
                entry["target_lookback_widened_to"],
            )
    all_added = set(added_ids)
    all_attached = set(attached_ids)
    # First-generation F4 entries only recorded ``added_thread_beat_ids`` --
    # those Beats were still attached to the target Beat's retrieval
    # requirements. Treat both fields as "should be removed from
    # retrieval_requirements.thread_beat_ids" when reverting.
    to_strip_from_retrieval = all_added | all_attached

    # 1) selected_thread_beat_ids: drop the added Beats.
    selected = list(script.get("selected_thread_beat_ids", []))
    reverted_selected = [
        item for item in selected if item not in all_added
    ]
    script["selected_thread_beat_ids"] = reverted_selected

    # 2) omitted_thread_beats: put the removed Beats back so preflight's
    # "selected U omitted = source_thread_beat_ids" check still holds.
    omitted = list(script.get("omitted_thread_beats", []))
    already_omitted = {
        item.get("thread_beat_id")
        for item in omitted
        if isinstance(item, dict)
    }
    for beat_id in added_ids:
        if beat_id in already_omitted:
            continue
        omitted.append(
            {
                "thread_beat_id": beat_id,
                "reason": "duration_limit",
                "explanation": (
                    "由 expand_story_scope --revert 从 auto 扩展中撤回；"
                    "如需重新纳入请重新走 Story Catalog/Portfolio 流程。"
                ),
            }
        )
    script["omitted_thread_beats"] = omitted

    # 3) Per-beat retrieval_requirements: strip attached Thread Beats
    # (including from targets and any other beats that happen to still
    # carry them), and restore the lookback on the recorded target beat(s).
    for beat in script.get("beats", []):
        if not isinstance(beat, dict):
            continue
        retrieval = beat.get("retrieval_requirements")
        if not isinstance(retrieval, dict):
            continue
        thread_beat_ids = retrieval.get("thread_beat_ids", [])
        cleaned = [
            item
            for item in thread_beat_ids
            if item not in to_strip_from_retrieval
        ]
        retrieval["thread_beat_ids"] = cleaned
        beat_id = beat.get("id")
        lookback_change = lookback_changes.get(beat_id)
        if (
            lookback_change is not None
            and retrieval.get("lookback") == lookback_change[1]
        ):
            retrieval["lookback"] = lookback_change[0]

    # 4) Strip stale post-preflight beat enrichments so this script parses
    # against the draft schema after we reset its status.
    for beat in script.get("beats", []):
        if isinstance(beat, dict):
            for key in (
                "estimated_source_duration_seconds",
                "evidence_status",
                "material_risks",
            ):
                beat.pop(key, None)

    # 5) Delete the audit trail and reset status; preflight will regenerate
    # feasibility fresh.
    script.pop("auto_scope_expansion", None)
    script["status"] = "draft"
    script.pop("feasibility", None)

    atomic_write_json(script_path, script)
    return {
        "reverted": True,
        "removed_from_selected": added_ids,
        "removed_attached_thread_beat_ids": attached_ids,
        "target_beat_ids": target_beat_ids,
        "restored_lookbacks": {
            beat_id: change[0]
            for beat_id, change in sorted(lookback_changes.items())
        },
    }


def _reset_approval_entry_for_revert(
    approval_path: Path, story_id: str, reason: str
) -> None:
    approval = load_json(approval_path)
    for entry in approval.get("stories", []):
        if entry.get("story_id") != story_id:
            continue
        entry["decision"] = AUTO_SCOPE_REVERT_DECISION
        entry["approved_script_sha256"] = ""
        entry["reason"] = reason
        entry.pop("auto_scope_expansion", None)
        break
    approval["fulfillment_status"] = "requires_rework"
    approval["status"] = "story_scope_revert_pending"
    if story_id in approval.get("selected_story_ids", []):
        approval["selected_story_ids"] = [
            item
            for item in approval["selected_story_ids"]
            if item != story_id
        ]
    atomic_write_json(approval_path, approval)


def revert(job_root: Path) -> Path:
    """Undo every ``auto_scope_expansion`` change recorded in
    ``story-scripts/<story-id>.json`` and reset each affected Story's
    ``story-approval.json`` entry.

    Emits ``story-scope-revert.json`` summarising what was undone. Users
    must re-run ``preflight_story_scripts`` + ``story_approval`` +
    downstream stages the same way they did after ``--apply``.
    """
    approval_path = job_root / "story-approval.json"
    output_path = job_root / "story-scope-revert.json"
    reverted_story_ids: list[str] = []
    reports: list[dict[str, Any]] = []
    for script_path in sorted(
        (job_root / "story-scripts").glob("*.json")
    ):
        if script_path.name == "index.json":
            continue
        script = load_json(script_path)
        story_id = script.get("story_id")
        if not isinstance(story_id, str):
            continue
        result = _revert_expansion_from_script(script_path)
        if not result.get("reverted"):
            continue
        _reset_approval_entry_for_revert(
            approval_path,
            story_id,
            reason=(
                "expand_story_scope --revert：撤销自动 story_scope 扩展；"
                "请重新预检并人工重新审批。"
            ),
        )
        reverted_story_ids.append(story_id)
        reports.append({"story_id": story_id, **result})
    payload = {
        "schema_version": "1.0",
        "reverted_story_ids": reverted_story_ids,
        "next_steps": [
            "python3 preflight_story_scripts.py …",
            "python3 story_approval.py decide …    (per reverted Story)",
            "python3 build_story_evidence_packet.py …",
            "python3 compile_span_candidates.py …",
            "python3 prepare_story_stages.py plans --allow-partial",
        ],
        "reports": reports,
    }
    atomic_write_json(output_path, payload)
    if reverted_story_ids:
        update_project_stage(
            job_root / "project.json",
            "story_scope_expansion",
            "reverted",
            inputs={"story_approval": str(approval_path)},
            outputs={"story_scope_revert": str(output_path)},
            note=(
                "Auto scope expansion reverted on "
                + ", ".join(reverted_story_ids)
                + "; downstream stages must be re-run and approvals "
                "re-collected."
            ),
        )
    return output_path


def _load_preflight(job_root: Path) -> dict[str, Any]:
    """Loader: reads Plan preflight only (kept for backward compat)."""
    path = job_root / "story-plan-preflight.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    return load_json(path)


def _plan_preflight_path(job_root: Path) -> Path:
    return job_root / "story-plan-preflight.json"


def _script_preflight_path(job_root: Path) -> Path:
    return job_root / "story-script-preflight.json"


def _resolve_source(job_root: Path, source: str) -> tuple[str, Path]:
    """Return (effective_source, preflight_path) based on --source.

    ``auto`` prefers Plan preflight when it exists because Script preflight is
    normally retained for the whole job and would otherwise mask later,
    option-level deficits. Script-stage callers use an explicit source.
    """
    if source not in VALID_TRIGGER_SOURCES:
        raise ValueError(
            f"unknown --source {source!r}; must be one of "
            + ", ".join(sorted(VALID_TRIGGER_SOURCES))
        )
    if source == TRIGGER_SOURCE_SCRIPT_PREFLIGHT:
        return TRIGGER_SOURCE_SCRIPT_PREFLIGHT, _script_preflight_path(job_root)
    if source == TRIGGER_SOURCE_PLAN_PREFLIGHT:
        return TRIGGER_SOURCE_PLAN_PREFLIGHT, _plan_preflight_path(job_root)
    plan_path = _plan_preflight_path(job_root)
    if plan_path.is_file():
        return TRIGGER_SOURCE_PLAN_PREFLIGHT, plan_path
    return TRIGGER_SOURCE_SCRIPT_PREFLIGHT, _script_preflight_path(job_root)


def _load_preflight_from(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    return load_json(path)


def _story_script_path(job_root: Path, story_id: str) -> Path:
    return job_root / "story-scripts" / f"{story_id}.json"


def _load_event_cards(job_root: Path) -> list[dict[str, Any]]:
    path = job_root / "event-cards.jsonl"
    if not path.is_file():
        return []
    return load_jsonl(path)


def _script_needs_lookback_fixup(script_path: Path) -> bool:
    """Return True when a prior F4 apply left an expansion Thread Beat on a
    beat whose placement/lookback violates the current scope-expansion
    policy, OR when the script still carries stale beat-level enrichments
    from a previous ``preflight_story_scripts`` run."""
    if not script_path.is_file():
        return False
    script = load_json(script_path)
    for beat in script.get("beats", []):
        if isinstance(beat, dict) and any(
            key in beat
            for key in (
                "estimated_source_duration_seconds",
                "evidence_status",
                "material_risks",
            )
        ):
            return True
    historical: set[str] = set()
    for entry in script.get("auto_scope_expansion", []):
        if not isinstance(entry, dict):
            continue
        for key in ("added_thread_beat_ids", "attached_thread_beat_ids"):
            historical.update(entry.get(key, []) or [])
    if not historical:
        return False
    for beat in script.get("beats", []):
        if not isinstance(beat, dict):
            continue
        retrieval = beat.get("retrieval_requirements", {})
        thread_beat_ids = set(retrieval.get("thread_beat_ids", []))
        if not (historical & thread_beat_ids):
            continue
        if beat.get("role") not in SCOPE_EXPANSION_TARGET_ROLES:
            return True
        if retrieval.get("lookback") in {"same_episode", "whole_series"}:
            return True
    return False


def expand(
    job_root: Path,
    *,
    apply: bool = False,
    source: str = TRIGGER_SOURCE_AUTO,
    allow_whole_series: bool = False,
) -> Path:
    effective_source, preflight_path = _resolve_source(job_root, source)
    preflight = _load_preflight_from(preflight_path)
    bible = load_json(job_root / "series-bible.json")
    event_cards = _load_event_cards(job_root)
    approval_path = job_root / "story-approval.json"
    output_path = job_root / "story-scope-expansion.json"
    proposals: list[dict[str, Any]] = []
    applied_story_ids: list[str] = []
    for entry in preflight.get("stories", []):
        failure_codes = set(entry.get("failure_codes", []))
        if not (failure_codes & EXPANDABLE_FAILURE_CODES):
            continue
        story_id = entry.get("story_id")
        if not isinstance(story_id, str):
            continue
        script_path = _story_script_path(job_root, story_id)
        if not script_path.is_file():
            proposals.append(
                {
                    "story_id": story_id,
                    "status": "missing_script",
                    "script_path": str(script_path),
                }
            )
            continue
        script = load_json(script_path)
        span_bundle = _load_span_bundle(job_root, story_id)
        proposal = _propose_expansion(
            story_id, entry, script, bible, event_cards, span_bundle
        )
        proposals.append(proposal)
        if not apply:
            continue
        if proposal["status"] not in {
            "recommended",
            "insufficient_neighbouring_material",
            "insufficient_diversity",
            "no_adjacent_thread_beats",
        }:
            continue
        # ``no_adjacent_thread_beats`` still needs a chance to widen the
        # target beat's lookback when a prior F4 run left it as
        # ``same_episode``. ``_apply_expansion_to_script`` treats an empty
        # ``add_thread_beat_ids`` list plus an existing history as a
        # lookback-only fix-up.
        if (
            proposal["status"] == "no_adjacent_thread_beats"
            and not _script_needs_lookback_fixup(script_path)
        ):
            continue
        apply_result = _apply_expansion_to_script(
            script_path,
            proposal["add_thread_beat_ids"],
            trigger_source=effective_source,
            allow_whole_series=allow_whole_series,
        )
        proposal["apply_result"] = apply_result
        if not apply_result.get("applied"):
            if apply_result.get("status") == (
                "requires_story_script_revision"
            ):
                proposal["status"] = "requires_story_script_revision"
                proposal["recommended_action"] = (
                    "Add or revise a setup/escalation/turn_or_reveal Beat "
                    "before retrying automatic scope expansion."
                )
            continue
        _reset_approval_entry(
            approval_path,
            story_id,
            reason=(
                f"自动 story_scope 扩展（触发源 {effective_source}）："
                "新增相邻 Thread Beat 后需要重新预检、人工重新审批并重跑 "
                "Evidence/Span/Plan preflight。"
            ),
            add_thread_beat_ids=proposal["add_thread_beat_ids"],
        )
        applied_story_ids.append(story_id)
    payload = {
        "schema_version": "1.0",
        "trigger_source": effective_source,
        "preflight_source_path": str(preflight_path),
        "story_count": len(proposals),
        "applied": apply,
        "whole_series_expansion_authorized": allow_whole_series,
        "applied_story_ids": applied_story_ids,
        "next_steps_when_applied": [
            "python3 preflight_story_scripts.py …  (regenerate feasibility)",
            "python3 story_approval.py review …    (human re-approves the "
            "expanded scope)",
            "python3 build_story_evidence_packet.py …",
            "python3 compile_span_candidates.py …",
            "python3 prepare_story_stages.py plans …",
        ],
        "proposals": proposals,
    }
    atomic_write_json(output_path, payload)
    if apply and applied_story_ids:
        update_project_stage(
            job_root / "project.json",
            "story_scope_expansion",
            "applied",
            inputs={
                "trigger_source": effective_source,
                "preflight_source_path": str(preflight_path),
            },
            outputs={"story_scope_expansion": str(output_path)},
            note=(
                f"Auto scope expansion applied via {effective_source} to "
                + ", ".join(applied_story_ids)
                + "; downstream stages must be re-run and Story approvals "
                "must be re-collected."
            ),
        )
    return output_path