#!/usr/bin/env python3
"""Deterministic Broad Story subarc and artifact-identity contracts."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from copy import deepcopy
from typing import Any, Iterable

from autocut_core.semantic.assignment_contract import (
    CODA_TERMINAL_PHASES,
    THREAD_KIND_CODA,
    THREAD_KINDS,
)

from autocut_core.schema.compat import STORY_CATALOG_SCHEMA


BROAD = "broad"
LEGACY_REMOVAL_MESSAGE = (
    "Legacy Story granularity is no longer supported; rerun from the Broad "
    "Story Catalog stage"
)

BROAD_MIN_BEATS = 4
BROAD_PREFERRED_MAX_BEATS = 8
BROAD_MAX_BEATS = 12
BROAD_REQUIRED_COVERAGE_RATIO = 1.0
BROAD_NON_CODA_COVERAGE_RATIO = 0.85

_CLOSURE_PHASES = {"turn", "reveal", "payoff", "consequence", "coda"}
_TERMINAL_PHASES = CODA_TERMINAL_PHASES


def require_broad_story_granularity(
    *payloads: dict[str, Any] | None,
) -> str:
    """Require every consumed Story artifact to carry the Broad identity.

    Broad Catalog generation starts without an upstream Story artifact and
    therefore does not call this helper.  Every later consumer does.  Missing
    identity is rejected because Legacy Catalogs did not carry a
    ``story_granularity`` field and must not be silently reinterpreted as
    Coverage-first Broad artifacts.
    """

    missing_indexes: list[int] = []
    invalid_values: list[str] = []
    for index, payload in enumerate(payloads, start=1):
        if not isinstance(payload, dict):
            continue
        value = payload.get("story_granularity")
        if value is None:
            missing_indexes.append(index)
        elif value != BROAD:
            invalid_values.append(str(value))
    if invalid_values:
        raise ValueError(
            f"{LEGACY_REMOVAL_MESSAGE}; unsupported artifact values: "
            f"{sorted(set(invalid_values))}"
        )
    if missing_indexes:
        raise ValueError(
            f"{LEGACY_REMOVAL_MESSAGE}; Story artifact(s) "
            f"{missing_indexes} have no explicit story_granularity={BROAD!r}"
        )
    return BROAD


def _stable_id(prefix: str, payload: Any) -> str:
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _ordered_thread_beats(
    bible: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
]:
    beats = [
        item
        for item in bible.get("thread_beats", []) or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    beat_by_id = {item["id"]: item for item in beats}
    by_thread: dict[str, list[dict[str, Any]]] = {}
    for thread in bible.get("story_threads", []) or []:
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            continue
        thread_id = thread["id"]
        ordered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for beat_id in thread.get("thread_beat_ids", []) or []:
            beat = beat_by_id.get(beat_id)
            if beat is not None and beat.get("thread_id") == thread_id:
                ordered.append(beat)
                seen.add(beat_id)
        ordered.extend(
            sorted(
                (
                    beat
                    for beat in beats
                    if beat.get("thread_id") == thread_id
                    and beat["id"] not in seen
                ),
                key=lambda item: (
                    int(item.get("episode") or 0),
                    item["id"],
                ),
            )
        )
        if ordered:
            by_thread[thread_id] = ordered
    return beats, beat_by_id, by_thread


def _balanced_sizes(total: int, target: int) -> list[int]:
    if total < BROAD_MIN_BEATS:
        return []
    groups = max(1, round(total / target))
    while math.ceil(total / groups) > BROAD_PREFERRED_MAX_BEATS:
        groups += 1
    while groups > 1 and total // groups < BROAD_MIN_BEATS:
        groups -= 1
    base, remainder = divmod(total, groups)
    sizes = [base + (1 if index < remainder else 0) for index in range(groups)]
    if min(sizes) < BROAD_MIN_BEATS or max(sizes) > BROAD_PREFERRED_MAX_BEATS:
        return []
    return sizes


def _dependency_closed_ids(
    seed_ids: Iterable[str],
    *,
    beat_by_id: dict[str, dict[str, Any]],
) -> set[str]:
    selected = set(seed_ids)
    pending = list(selected)
    while pending:
        beat_id = pending.pop()
        beat = beat_by_id.get(beat_id, {})
        for required_id in beat.get("requires_beat_ids", []) or []:
            required = beat_by_id.get(required_id)
            if (
                required is not None
                and required.get("thread_id") == beat.get("thread_id")
                and required_id not in selected
            ):
                selected.add(required_id)
                pending.append(required_id)
    return selected


def _merged_duration_seconds(
    event_ids: Iterable[str],
    event_by_id: dict[str, dict[str, Any]],
) -> float:
    ranges: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for event_id in event_ids:
        event = event_by_id.get(event_id, {})
        source_id = event.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            continue
        for source_range in event.get("source_ranges", []) or []:
            if not isinstance(source_range, dict):
                continue
            start, end = source_range.get("start"), source_range.get("end")
            if (
                isinstance(start, (int, float))
                and isinstance(end, (int, float))
                and float(end) > float(start)
            ):
                ranges[source_id].append((float(start), float(end)))
    duration = 0.0
    for source_ranges in ranges.values():
        merged: list[list[float]] = []
        for start, end in sorted(source_ranges):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        duration += sum(end - start for start, end in merged)
    return round(duration, 3)


def _candidate_roles(candidate: dict[str, Any]) -> set[str]:
    roles = {
        role
        for role in candidate.get("allowed_roles", []) or []
        if isinstance(role, str) and role
    }
    primary = candidate.get("kind") or candidate.get("type")
    if isinstance(primary, str) and primary:
        roles.add(primary)
    return roles


def _local_duration_feasibility(seconds: float) -> str:
    # has no minimum-duration gate.  ``insufficient`` is therefore
    # reserved for an Option with no measurable Event range at all; short
    # but evidence-backed arcs remain eligible and Render filler tail handles
    # delivery length later.
    if seconds <= 0:
        return "insufficient"
    if seconds < 90:
        return "short"
    if seconds < 300:
        return "viable"
    return "strong"


def _make_option(
    selected_ids: Iterable[str],
    *,
    option_type: str,
    beat_by_id: dict[str, dict[str, Any]],
    thread_order: dict[str, int],
    beat_order: dict[str, int],
    event_by_id: dict[str, dict[str, Any]],
    candidate_by_id: dict[str, dict[str, Any]],
    fact_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    selected = _dependency_closed_ids(selected_ids, beat_by_id=beat_by_id)
    ordered = sorted(
        (beat_by_id[beat_id] for beat_id in selected if beat_id in beat_by_id),
        key=lambda beat: (
            int(beat.get("episode") or 0),
            thread_order.get(str(beat.get("thread_id")), 10**6),
            beat_order.get(beat["id"], 10**6),
            beat["id"],
        ),
    )
    if not ordered:
        return None
    if option_type == "coda":
        if len(ordered) > 2 or any(
            beat.get("phase") not in _TERMINAL_PHASES for beat in ordered
        ) or not any(beat.get("phase") == "coda" for beat in ordered):
            return None
    elif option_type == "compact_resolution":
        if len(ordered) != 3 or not any(
            beat.get("phase") in _CLOSURE_PHASES for beat in ordered
        ):
            return None
    elif not (BROAD_MIN_BEATS <= len(ordered) <= BROAD_MAX_BEATS):
        return None

    story_thread_ids = list(
        dict.fromkeys(str(beat.get("thread_id")) for beat in ordered)
    )
    beat_ids = [beat["id"] for beat in ordered]
    event_ids = list(
        dict.fromkeys(
            event_id
            for beat in ordered
            for event_id in beat.get("event_ids", []) or []
            if isinstance(event_id, str) and event_id in event_by_id
        )
    )
    internal_required = [
        beat["id"]
        for beat in ordered[1:-1]
        if beat.get("importance") == "required"
        or any(
            beat["id"] in (other.get("requires_beat_ids", []) or [])
            for other in ordered
        )
    ]
    related_fact_ids = sorted(
        item["id"]
        for item in fact_records
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and bool(set(item.get("event_ids", []) or []) & set(event_ids))
    )
    related_candidate_ids = sorted(
        candidate_id
        for candidate_id, candidate in candidate_by_id.items()
        if bool(set(candidate.get("event_ids", []) or []) & set(event_ids))
        or candidate_id
        in {
            candidate_id
            for event_id in event_ids
            for candidate_id in event_by_id[event_id].get("candidate_ids", []) or []
            if isinstance(candidate_id, str)
        }
    )
    highlight_ids = [
        candidate_id
        for candidate_id in related_candidate_ids
        if "highlight" in _candidate_roles(candidate_by_id[candidate_id])
    ]
    hook_ids = [
        candidate_id
        for candidate_id in related_candidate_ids
        if "hook" in _candidate_roles(candidate_by_id[candidate_id])
    ]
    phases = [str(beat.get("phase") or "") for beat in ordered]
    closure_ids = [
        beat["id"] for beat in ordered if beat.get("phase") in _CLOSURE_PHASES
    ]
    if not closure_ids:
        closure_ids = [ordered[-1]["id"]]
    episode_ids = sorted(
        {
            int(beat["episode"])
            for beat in ordered
            if isinstance(beat.get("episode"), int)
        }
    )
    identity = {
        "option_type": option_type,
        "story_thread_ids": story_thread_ids,
        "source_thread_beat_ids": beat_ids,
    }
    estimated_source_seconds = _merged_duration_seconds(
        event_ids, event_by_id
    )
    return {
        "subarc_option_id": _stable_id("subarc", identity),
        "option_type": option_type,
        "story_thread_ids": story_thread_ids,
        "source_thread_beat_ids": beat_ids,
        "subarc_start_beat_id": beat_ids[0],
        "subarc_end_beat_id": beat_ids[-1],
        "required_bridge_beat_ids": internal_required,
        "required_thread_beat_ids": [
            beat["id"]
            for beat in ordered
            if beat.get("importance") == "required"
        ],
        "non_coda_thread_beat_ids": [
            beat["id"] for beat in ordered if beat.get("phase") != "coda"
        ],
        "local_payoff_beat_ids": closure_ids,
        "phases": phases,
        "episode_ids": episode_ids,
        "evidence_event_ids": event_ids,
        "required_fact_ids": related_fact_ids,
        "highlight_candidate_ids": highlight_ids,
        "hook_candidate_ids": hook_ids,
        "estimated_source_seconds": estimated_source_seconds,
        "duration_feasibility": _local_duration_feasibility(
            estimated_source_seconds
        ),
    }


def _small_thread_option(
    thread_id: str,
    own_beats: list[dict[str, Any]],
    *,
    threads: dict[str, dict[str, Any]],
    by_thread: dict[str, list[dict[str, Any]]],
    beat_by_id: dict[str, dict[str, Any]],
) -> tuple[list[str], str]:
    phases = {beat.get("phase") for beat in own_beats}
    thread_kind = threads.get(thread_id, {}).get("thread_kind")
    if thread_kind == THREAD_KIND_CODA:
        if (
            len(own_beats) <= 2
            and phases <= _TERMINAL_PHASES
            and "coda" in phases
        ):
            return [beat["id"] for beat in own_beats], "coda"
        raise ValueError(
            f"typed coda thread {thread_id} must contain 1-2 terminal Beats "
            "and at least one phase=coda Beat"
        )

    own_characters = set(threads.get(thread_id, {}).get("character_ids", []) or [])
    own_episodes = [
        int(beat.get("episode") or 0) for beat in own_beats
    ]
    anchor = sum(own_episodes) / len(own_episodes)
    neighbors: list[tuple[int, float, int, str]] = []
    for other_thread_id, other_beats in by_thread.items():
        if other_thread_id == thread_id:
            continue
        shared = own_characters & set(
            threads.get(other_thread_id, {}).get("character_ids", []) or []
        )
        if not shared:
            continue
        for beat in other_beats:
            neighbors.append(
                (
                    -len(shared),
                    abs(float(beat.get("episode") or 0) - anchor),
                    int(beat.get("episode") or 0),
                    beat["id"],
                )
            )
    selected = {beat["id"] for beat in own_beats}
    for _, _, _, beat_id in sorted(neighbors):
        selected.add(beat_id)
        selected = _dependency_closed_ids(selected, beat_by_id=beat_by_id)
        if len(selected) >= BROAD_MIN_BEATS:
            return sorted(selected), "cross_thread"
        if len(selected) >= BROAD_PREFERRED_MAX_BEATS:
            break
    if len(own_beats) == 3 and phases & _CLOSURE_PHASES:
        return [beat["id"] for beat in own_beats], "compact_resolution"
    raise ValueError(
        f"broad mode cannot form a 4+ beat closed subarc for short thread "
        f"{thread_id}; add a related thread/character link or declare an "
        "evidence-valid typed coda in Registry"
    )


def compile_broad_subarc_options(
    bible: dict[str, Any],
    events: list[dict[str, Any]],
    candidate_catalog: dict[str, Any],
) -> dict[str, Any]:
    """Compile bounded, dependency-closed Broad Story subarc options."""

    _, beat_by_id, by_thread = _ordered_thread_beats(bible)
    if not by_thread:
        raise ValueError("Series Bible contains no Thread Beats")
    threads = {
        item["id"]: item
        for item in bible.get("story_threads", []) or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    invalid_thread_kinds = sorted(
        thread_id
        for thread_id in by_thread
        if threads.get(thread_id, {}).get("thread_kind") not in THREAD_KINDS
    )
    if invalid_thread_kinds:
        raise ValueError(
            "Broad Compiler requires every participating Story Thread to "
            "declare thread_kind=arc|coda; missing_or_invalid="
            f"{invalid_thread_kinds}"
        )
    thread_order = {
        thread_id: index for index, thread_id in enumerate(by_thread)
    }
    beat_order = {
        beat["id"]: index
        for thread_id in by_thread
        for index, beat in enumerate(by_thread[thread_id])
    }
    event_by_id = {
        item["id"]: item
        for item in events
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    candidate_by_id = {
        item["id"]: item
        for item in candidate_catalog.get("candidates", []) or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    facts = [
        item for item in bible.get("facts", []) or [] if isinstance(item, dict)
    ]

    options_by_beats: dict[tuple[str, ...], dict[str, Any]] = {}
    recommended_beat_sets: list[tuple[str, ...]] = []
    for thread_id, ordered in by_thread.items():
        if len(ordered) < BROAD_MIN_BEATS:
            ids, option_type = _small_thread_option(
                thread_id,
                ordered,
                threads=threads,
                by_thread=by_thread,
                beat_by_id=beat_by_id,
            )
            option = _make_option(
                ids,
                option_type=option_type,
                beat_by_id=beat_by_id,
                thread_order=thread_order,
                beat_order=beat_order,
                event_by_id=event_by_id,
                candidate_by_id=candidate_by_id,
                fact_records=facts,
            )
            if option is None:
                raise ValueError(f"failed to compile broad option for {thread_id}")
            key = tuple(option["source_thread_beat_ids"])
            options_by_beats[key] = option
            recommended_beat_sets.append(key)
            continue

        partitions: dict[int, list[tuple[str, ...]]] = {}
        for target in (5, 6, 7, 8):
            sizes = _balanced_sizes(len(ordered), target)
            if not sizes:
                continue
            cursor = 0
            partition: list[tuple[str, ...]] = []
            for size in sizes:
                seed = [beat["id"] for beat in ordered[cursor : cursor + size]]
                cursor += size
                option = _make_option(
                    seed,
                    option_type="broad",
                    beat_by_id=beat_by_id,
                    thread_order=thread_order,
                    beat_order=beat_order,
                    event_by_id=event_by_id,
                    candidate_by_id=candidate_by_id,
                    fact_records=facts,
                )
                if option is None:
                    partition = []
                    break
                key = tuple(option["source_thread_beat_ids"])
                options_by_beats[key] = option
                partition.append(key)
            if partition:
                partitions[target] = partition
        if not partitions:
            raise ValueError(
                f"broad mode cannot partition thread {thread_id} into "
                f"{BROAD_MIN_BEATS}-{BROAD_MAX_BEATS} beat closed options"
            )
        preferred_target = min(
            partitions,
            key=lambda target: (abs(target - 6), target),
        )
        recommended_beat_sets.extend(partitions[preferred_target])

    options = sorted(
        options_by_beats.values(),
        key=lambda item: (
            min(item.get("episode_ids") or [10**6]),
            item["story_thread_ids"],
            item["source_thread_beat_ids"],
        ),
    )
    option_id_by_beats = {
        tuple(option["source_thread_beat_ids"]): option["subarc_option_id"]
        for option in options
    }
    recommended_option_ids = list(
        dict.fromkeys(option_id_by_beats[key] for key in recommended_beat_sets)
    )
    all_beats = list(beat_by_id)
    required_beats = sorted(
        beat_id
        for beat_id, beat in beat_by_id.items()
        if beat.get("importance") == "required"
    )
    non_coda_beats = sorted(
        beat_id
        for beat_id, beat in beat_by_id.items()
        if beat.get("phase") != "coda"
    )
    recommended_covered = {
        beat_id
        for option in options
        if option["subarc_option_id"] in set(recommended_option_ids)
        for beat_id in option["source_thread_beat_ids"]
    }
    missing_required = sorted(set(required_beats) - recommended_covered)
    if missing_required:
        raise ValueError(
            "Broad recommended options do not cover required Thread Beats: "
            f"{missing_required}"
        )
    return {
        "schema_version": "1.0",
        "story_granularity": BROAD,
        "compiler_version": "coverage-first-broad-subarc-v2-typed-coda",
        "coverage_contract": {
            "required_thread_beat_coverage_ratio": (
                BROAD_REQUIRED_COVERAGE_RATIO
            ),
            "non_coda_thread_beat_coverage_ratio": (
                BROAD_NON_CODA_COVERAGE_RATIO
            ),
            "ordinary_story_min_beats": BROAD_MIN_BEATS,
            "ordinary_story_preferred_max_beats": (
                BROAD_PREFERRED_MAX_BEATS
            ),
            "ordinary_story_hard_max_beats": BROAD_MAX_BEATS,
            "short_story_types": ["coda", "compact_resolution"],
        },
        "required_thread_beat_ids": required_beats,
        "non_coda_thread_beat_ids": non_coda_beats,
        "all_thread_beat_ids": sorted(all_beats),
        "recommended_option_ids": recommended_option_ids,
        "options": options,
    }


def _array_enum(values: Iterable[str], *, nonempty: bool = False) -> dict[str, Any]:
    enum = sorted(set(values))
    schema: dict[str, Any] = {
        "type": "array",
        "items": (
            {"type": "string", "enum": enum}
            if enum
            else {"type": "string"}
        ),
    }
    if nonempty:
        schema["minItems"] = 1
    if not enum:
        schema["maxItems"] = 0
    return schema


def build_broad_catalog_schema(
    bible: dict[str, Any],
    option_catalog: dict[str, Any],
    *,
    option_catalog_sha256: str,
    story_id_by_option: dict[str, str] | None = None,
    exact_story_count: int | None = None,
) -> dict[str, Any]:
    """Build a strict per-option Story Catalog request schema."""

    schema = deepcopy(STORY_CATALOG_SCHEMA)
    schema["properties"]["story_granularity"] = {
        "type": "string",
        "const": BROAD,
    }
    schema["properties"]["subarc_option_catalog_sha256"] = {
        "type": "string",
        "const": option_catalog_sha256,
    }
    for field in ("story_granularity", "subarc_option_catalog_sha256"):
        if field not in schema["required"]:
            schema["required"].append(field)

    base_story = schema["properties"]["stories"]["items"]
    thread_by_id = {
        item["id"]: item
        for item in bible.get("story_threads", []) or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    relationships = [
        item
        for item in bible.get("relationships", []) or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    branches: list[dict[str, Any]] = []
    options = option_catalog.get("options", []) or []
    for option in options:
        branch = deepcopy(base_story)
        props = branch["properties"]
        props["subarc_option_id"] = {
            "type": "string",
            "const": option["subarc_option_id"],
        }
        if (
            story_id_by_option is not None
            and option["subarc_option_id"] in story_id_by_option
        ):
            props["story_id"] = {
                "type": "string",
                "const": story_id_by_option[option["subarc_option_id"]],
            }
        if "subarc_option_id" not in branch["required"]:
            branch["required"].append("subarc_option_id")
        for field in (
            "story_thread_ids",
            "source_thread_beat_ids",
            "required_bridge_beat_ids",
            "evidence_event_ids",
        ):
            values = list(option[field])
            props[field] = {
                "type": "array",
                "items": {"type": "string"},
                "const": values,
            }
        props["subarc_start_beat_id"] = {
            "type": "string",
            "const": option["subarc_start_beat_id"],
        }
        props["subarc_end_beat_id"] = {
            "type": "string",
            "const": option["subarc_end_beat_id"],
        }
        props["estimated_source_seconds"] = {
            "type": "number",
            "const": option["estimated_source_seconds"],
        }
        props["duration_feasibility"] = {
            "type": "string",
            "const": option["duration_feasibility"],
        }
        option_character_ids = sorted(
            {
                character_id
                for thread_id in option["story_thread_ids"]
                for character_id in thread_by_id.get(thread_id, {}).get(
                    "character_ids", []
                )
                if isinstance(character_id, str)
            }
        )
        option_relationship_ids = sorted(
            item["id"]
            for item in relationships
            if set(item.get("character_ids", []) or [])
            <= set(option_character_ids)
            and len(item.get("character_ids", []) or []) >= 2
        )
        props["character_ids"] = _array_enum(
            option_character_ids, nonempty=True
        )
        props["relationship_ids"] = _array_enum(option_relationship_ids)
        props["required_fact_ids"] = _array_enum(
            option.get("required_fact_ids", [])
        )
        props["suggested_highlight_candidate_ids"] = _array_enum(
            option.get("highlight_candidate_ids", [])
        )
        props["suggested_hook_candidate_ids"] = _array_enum(
            option.get("hook_candidate_ids", [])
        )
        branches.append(branch)
    if not branches:
        raise ValueError("Broad catalog schema requires at least one subarc option")

    required_count = len(
        option_catalog.get("required_thread_beat_ids", []) or []
    )
    minimum_stories = max(1, math.ceil(required_count / BROAD_MAX_BEATS))
    recommended_count = len(
        option_catalog.get("recommended_option_ids", []) or []
    )
    maximum_stories = max(
        minimum_stories,
        recommended_count + 3,
        math.ceil(max(1, required_count) / BROAD_MIN_BEATS) + 2,
    )
    if exact_story_count is not None:
        minimum_stories = exact_story_count
        maximum_stories = exact_story_count
    schema["properties"]["stories"] = {
        "type": "array",
        "items": {"anyOf": branches},
        "minItems": minimum_stories,
        "maxItems": maximum_stories,
    }
    return schema


def validate_broad_catalog(
    catalog: dict[str, Any],
    option_catalog: dict[str, Any],
    *,
    option_catalog_sha256: str | None = None,
) -> list[str]:
    """Validate option identity, dependency closure and union coverage."""

    errors: list[str] = []
    if catalog.get("story_granularity") != BROAD:
        errors.append("story_catalog.story_granularity must be broad")
    if (
        option_catalog_sha256 is not None
        and catalog.get("subarc_option_catalog_sha256")
        != option_catalog_sha256
    ):
        errors.append(
            "story_catalog.subarc_option_catalog_sha256 does not match "
            "the compiled Broad option catalog"
        )
    option_by_id = {
        option["subarc_option_id"]: option
        for option in option_catalog.get("options", []) or []
        if isinstance(option, dict)
        and isinstance(option.get("subarc_option_id"), str)
    }
    seen: set[str] = set()
    covered: set[str] = set()
    for index, story in enumerate(catalog.get("stories", []) or []):
        if not isinstance(story, dict):
            continue
        option_id = story.get("subarc_option_id")
        prefix = f"stories[{index}]"
        if option_id not in option_by_id:
            errors.append(f"{prefix}.subarc_option_id is not a compiled option")
            continue
        if option_id in seen:
            errors.append(f"{prefix}.subarc_option_id is duplicated: {option_id}")
        seen.add(option_id)
        option = option_by_id[option_id]
        for field in (
            "story_thread_ids",
            "source_thread_beat_ids",
            "required_bridge_beat_ids",
            "evidence_event_ids",
        ):
            if story.get(field) != option.get(field):
                errors.append(
                    f"{prefix}.{field} must exactly match {option_id}"
                )
        for field in ("subarc_start_beat_id", "subarc_end_beat_id"):
            if story.get(field) != option.get(field):
                errors.append(
                    f"{prefix}.{field} must exactly match {option_id}"
                )
        actual_seconds = story.get("estimated_source_seconds")
        expected_seconds = option.get("estimated_source_seconds")
        if (
            not isinstance(actual_seconds, (int, float))
            or abs(float(actual_seconds) - float(expected_seconds)) > 0.001
        ):
            errors.append(
                f"{prefix}.estimated_source_seconds must be locally computed "
                f"value {expected_seconds}"
            )
        if story.get("duration_feasibility") != option.get(
            "duration_feasibility"
        ):
            errors.append(
                f"{prefix}.duration_feasibility must be locally derived "
                f"value {option.get('duration_feasibility')!r}"
            )
        if not set(story.get("suggested_highlight_candidate_ids", []) or []) <= set(
            option.get("highlight_candidate_ids", []) or []
        ):
            errors.append(
                f"{prefix}.suggested_highlight_candidate_ids contains a "
                "non-highlight or out-of-subarc Candidate"
            )
        if not set(story.get("suggested_hook_candidate_ids", []) or []) <= set(
            option.get("hook_candidate_ids", []) or []
        ):
            errors.append(
                f"{prefix}.suggested_hook_candidate_ids contains a "
                "non-hook or out-of-subarc Candidate"
            )
        beat_count = len(option.get("source_thread_beat_ids", []) or [])
        option_type = option.get("option_type")
        if option_type == "coda" and beat_count > 2:
            errors.append(f"{prefix}: coda option exceeds 2 Thread Beats")
        elif option_type == "coda" and (
            "coda" not in set(option.get("phases", []) or [])
            or not set(option.get("phases", []) or []) <= _TERMINAL_PHASES
        ):
            errors.append(
                f"{prefix}: coda option must contain only terminal phases "
                "and at least one phase=coda"
            )
        elif option_type == "compact_resolution" and beat_count != 3:
            errors.append(
                f"{prefix}: compact_resolution must contain exactly 3 Thread Beats"
            )
        elif option_type not in {"coda", "compact_resolution"} and not (
            BROAD_MIN_BEATS <= beat_count <= BROAD_MAX_BEATS
        ):
            errors.append(
                f"{prefix}: ordinary Broad Story must contain "
                f"{BROAD_MIN_BEATS}-{BROAD_MAX_BEATS} Thread Beats"
            )
        covered.update(option.get("source_thread_beat_ids", []) or [])

    required = set(
        option_catalog.get("required_thread_beat_ids", []) or []
    )
    non_coda = set(
        option_catalog.get("non_coda_thread_beat_ids", []) or []
    )
    missing_required = sorted(required - covered)
    if missing_required:
        errors.append(
            "Broad Story Catalog must cover every required Thread Beat; "
            f"missing={missing_required}"
        )
    non_coda_ratio = len(covered & non_coda) / len(non_coda) if non_coda else 1.0
    if non_coda_ratio + 1e-9 < BROAD_NON_CODA_COVERAGE_RATIO:
        errors.append(
            "Broad Story Catalog non-coda Thread Beat coverage "
            f"{non_coda_ratio:.3f} is below "
            f"{BROAD_NON_CODA_COVERAGE_RATIO:.2f}"
        )
    return errors


def broad_catalog_prompt(context: dict[str, Any]) -> str:
    contract = context.get("broad_story_contract", {})
    recommended = contract.get("recommended_option_ids", [])
    return (
        "\n\n【Broad Story · Coverage-first 硬合同】\n"
        "本地 Coverage Compiler 已经选定覆盖集；本请求只是其中一个固定 Option "
        "的单 Story shard，不要求也不允许模型另选 Option。本任务不是自由编写 "
        "source_thread_beat_ids；必须输出动态 Schema 固定的 subarc_option_id，"
        "并逐字复制该 Option "
        "锁定的 story_thread_ids、source_thread_beat_ids、起止 Beat、"
        "required_bridge_beat_ids、evidence_event_ids 与本地计算的"
        " estimated_source_seconds。动态 JSON Schema 已按 Option 分支锁定这些"
        "字段；禁止混用两个 Option 的字段。\n"
        "全批次 required/non-coda 覆盖已由本地 Coverage Compiler 负责；本 shard "
        f"只输出预分配 Option：{recommended}，不要复制或扩展 Option。\n"
        "suggested_highlight_candidate_ids 只能从该 Option 的 highlight "
        "Candidate 枚举中选；suggested_hook_candidate_ids 只能从 hook "
        "Candidate 枚举中选。无对应类型时必须输出空数组。"
    )
