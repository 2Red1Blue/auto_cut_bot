"""Plan option helpers originally from story_plan_options.py.

These functions are used by the semantic/prep/plans.py module.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from autocut_core.libs.editorial_plan import (
    PREFERRED_MEDIAN_CLIP_SECONDS_RANGE,
)

# ── Constants ────────────────────────────────────────────────────────────────

MAX_PLAN_FINALISTS = 3
SELECTION_RESPONSE_SCHEMA_VERSION = "4.0"
_SYNTHETIC_SELECTION_REASON = (
    "auto-selected: deterministic continuity finalist count is 1"
)
_SYNTHETIC_TEASER_REASON = (
    "auto-selected: deterministic compatible teaser finalist count is 1"
)
_SYNTHETIC_MODE_NONE_TEASER_REASON = "mode=none: linear opening, no teaser"
LOCAL_ORIENTATION_METHOD = "deterministic-beat-episode-orientation-v1"
ORIENTATION_FALLBACK_SCHEMA_VERSION = "1.0"


# ── Partition quality ranking ────────────────────────────────────────────────

def _functional_boundary_rank(
    metrics: dict[str, Any] | None,
) -> tuple[Any, ...]:
    """Return a tightness rank; lower is better and missing data is neutral."""
    value = metrics or {}
    return (
        round(
            1.0
            - float(
                value.get("functional_evidence_coverage_ratio", 1.0)
            ),
            6,
        ),
        round(float(value.get("nonfunctional_slack_seconds", 0.0)), 3),
        round(
            1.0
            - float(
                value.get("functional_selection_precision_ratio", 1.0)
            ),
            6,
        ),
        round(
            float(value.get("excess_head_seconds", 0.0))
            + float(value.get("excess_tail_seconds", 0.0)),
            3,
        ),
    )


def partition_quality_rank(item: dict[str, Any]) -> tuple[Any, ...]:
    """Quality-first deterministic ordering for the Plan finalist arena."""

    constraints = set(item.get("constraints_met", []))
    continuity = item.get("continuity_metrics", {})
    median = float(item.get("median_clip_duration_seconds", 0.0))
    preferred_minimum, preferred_maximum = (
        PREFERRED_MEDIAN_CLIP_SECONDS_RANGE
    )
    if median < preferred_minimum:
        median_penalty = round(preferred_minimum - median, 3)
    elif median > preferred_maximum:
        median_penalty = round(median - preferred_maximum, 3)
    else:
        median_penalty = 0.0
    return (
        int(continuity.get("hard_finding_count", 0)),
        int(continuity.get("dialogue_incomplete_count", 0)),
        int(continuity.get("same_source_causal_gap_count", 0)),
        int(item.get("full_source_like_clip_count", 0)),
        "duration_within_maximum" not in constraints,
        *_functional_boundary_rank(
            item.get("functional_boundary_metrics")
        ),
        int(continuity.get("continuity_closure_span_count", 0) > 0),
        "clip_count_meets_preferred_minimum" not in constraints,
        median_penalty,
        float(item.get("distance_from_preferred_target_seconds", 0.0)),
        round(float(item.get("mean_source_coverage_ratio", 0.0)), 3),
        round(float(item.get("max_source_coverage_ratio", 0.0)), 3),
        -int(item.get("clip_count", 0)),
        item["partition_id"],
    )


# ── Risk separation ──────────────────────────────────────────────────────────

def _partition_risk_signature(
    partition: dict[str, Any],
    *,
    legal_block_options: list[dict[str, Any]] | None,
    span_catalog: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Describe the QC-relevant decisions made by one body partition."""

    options_by_id = {
        item.get("option_id"): item
        for item in legal_block_options or []
        if isinstance(item, dict) and isinstance(item.get("option_id"), str)
    }
    spans_by_id = {
        item.get("span_candidate_id"): item
        for item in span_catalog or []
        if isinstance(item, dict)
        and isinstance(item.get("span_candidate_id"), str)
    }
    beat_assignments: dict[str, set[str]] = defaultdict(set)
    must_show_assignments: dict[str, set[str]] = defaultdict(set)
    for option_id in partition.get("body_option_ids", []):
        option = options_by_id.get(option_id)
        if option is None:
            continue
        span_ids = {
            span_id
            for span_id in option.get("span_candidate_ids", [])
            if isinstance(span_id, str)
        }
        for beat_id in option.get("beat_ids", []):
            if isinstance(beat_id, str):
                beat_assignments[beat_id].update(span_ids)
        for span_id in span_ids:
            span = spans_by_id.get(span_id, {})
            for must_show_id in span.get("supports_must_show_ids", []):
                if isinstance(must_show_id, str):
                    must_show_assignments[must_show_id].add(span_id)
    sequence = tuple(
        item
        for item in partition.get("physical_span_sequence", [])
        if isinstance(item, str)
    )
    return {
        "must_show": {
            key: tuple(sorted(value))
            for key, value in must_show_assignments.items()
        },
        "beats": {
            key: tuple(sorted(value))
            for key, value in beat_assignments.items()
        },
        "junctions": tuple(zip(sequence, sequence[1:])),
        "beat_partition": tuple(
            tuple(item for item in group if isinstance(item, str))
            for group in partition.get("beat_partition", [])
            if isinstance(group, list)
        ),
        "closure": bool(
            int(
                partition.get("continuity_metrics", {}).get(
                    "continuity_closure_span_count", 0
                )
            )
        ),
        "sequence": sequence,
    }


def _mapping_change_count(
    left: dict[str, tuple[str, ...]],
    right: dict[str, tuple[str, ...]],
) -> int:
    keys = set(left) | set(right)
    return sum(left.get(key) != right.get(key) for key in keys)


def _risk_separation_score(
    left: dict[str, Any], right: dict[str, Any]
) -> tuple[int, int, int, int, int, int]:
    """Return a lexicographic QC-failure-isolation distance."""

    must_show_changes = _mapping_change_count(
        left["must_show"], right["must_show"]
    )
    beat_changes = _mapping_change_count(left["beats"], right["beats"])
    junction_changes = len(
        set(left["junctions"]) ^ set(right["junctions"])
    )
    block_grouping_changed = int(
        left["beat_partition"] != right["beat_partition"]
    )
    closure_changed = int(left["closure"] != right["closure"])
    sequence_changes = len(set(left["sequence"]) ^ set(right["sequence"]))
    return (
        must_show_changes,
        junction_changes,
        beat_changes,
        block_grouping_changed,
        closure_changed,
        sequence_changes,
    )


# ── Finalist ranking ─────────────────────────────────────────────────────────

def rank_legal_body_finalists(
    partitions: list[dict[str, Any]],
    *,
    maximum_finalists: int = MAX_PLAN_FINALISTS,
    legal_block_options: list[dict[str, Any]] | None = None,
    span_catalog: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Keep legal, risk-separated finalists plus a closure fallback."""

    if maximum_finalists < 1:
        raise ValueError("maximum_finalists must be positive")
    ranked_all = sorted(partitions, key=partition_quality_rank)
    ranked: list[dict[str, Any]] = []
    seen_physical_sequences: set[tuple[str, ...]] = set()
    for item in ranked_all:
        raw_sequence = item.get("physical_span_sequence")
        physical_sequence = (
            tuple(raw_sequence)
            if isinstance(raw_sequence, list) and raw_sequence
            else (f"partition:{item['partition_id']}",)
        )
        if physical_sequence in seen_physical_sequences:
            continue
        seen_physical_sequences.add(physical_sequence)
        ranked.append(item)
    if legal_block_options is None or span_catalog is None:
        finalists = ranked[:maximum_finalists]
    else:
        signatures = {
            item["partition_id"]: _partition_risk_signature(
                item,
                legal_block_options=legal_block_options,
                span_catalog=span_catalog,
            )
            for item in ranked
        }
        finalists = ranked[:1]
        while len(finalists) < min(maximum_finalists, len(ranked)):
            remaining = [item for item in ranked if item not in finalists]

            def diversity_rank(item: dict[str, Any]) -> tuple[Any, ...]:
                distances = [
                    _risk_separation_score(
                        signatures[item["partition_id"]],
                        signatures[selected["partition_id"]],
                    )
                    for selected in finalists
                ]
                minimum_distance = min(distances)
                return (
                    tuple(-value for value in minimum_distance),
                    partition_quality_rank(item),
                )

            finalists.append(min(remaining, key=diversity_rank))
    closure_candidates = [
        item
        for item in ranked
        if int(
            item.get("continuity_metrics", {}).get(
                "continuity_closure_span_count", 0
            )
        )
        > 0
    ]
    if closure_candidates and not any(
        int(
            item.get("continuity_metrics", {}).get(
                "continuity_closure_span_count", 0
            )
        )
        > 0
        for item in finalists
    ):
        if len(finalists) >= maximum_finalists:
            finalists[-1] = closure_candidates[0]
        else:
            finalists.append(closure_candidates[0])
    return sorted(
        {item["partition_id"]: item for item in finalists}.values(),
        key=partition_quality_rank,
    )


# ── Unique option / synthetic selection ──────────────────────────────────────

def is_unique_option_case(legal_options: dict[str, Any]) -> bool:
    partitions = legal_options.get("legal_body_partitions") or []
    teasers = legal_options.get("legal_teaser_options") or []
    return len(partitions) == 1 and len(teasers) <= 1


def build_synthetic_selection(
    legal_options: dict[str, Any],
) -> dict[str, Any]:
    """Return the selection response the model would have produced when the
    legal-option space has been reduced to a single choice."""

    if not is_unique_option_case(legal_options):
        raise ValueError(
            "build_synthetic_selection requires a unique legal option "
            "case; got "
            f"{len(legal_options.get('legal_body_partitions') or [])} "
            "partition(s) and "
            f"{len(legal_options.get('legal_teaser_options') or [])} "
            "teaser option(s)"
        )
    partition = legal_options["legal_body_partitions"][0]
    teasers = legal_options.get("legal_teaser_options") or []
    teaser_mode_none = not teasers
    if teaser_mode_none:
        teaser_block = {
            "option_id": "",
            "selection_reason": _SYNTHETIC_MODE_NONE_TEASER_REASON,
        }
    else:
        teaser_block = {
            "option_id": teasers[0]["option_id"],
            "selection_reason": _SYNTHETIC_TEASER_REASON,
        }
    segment_count = int(partition.get("segment_count", 0))
    if segment_count <= 0:
        raise ValueError(
            "unique body finalist has no segments -- check finalist ranking"
        )
    body_block_orientations = []
    for index in range(segment_count):
        if not teaser_mode_none and index == 0:
            temporal = "return_to_mainline"
            orientation_required = True
            orientation_strategy = "dialogue_anchor"
        else:
            temporal = "continuation"
            orientation_required = False
            orientation_strategy = "none"
        body_block_orientations.append(
            {
                "temporal_relation_from_previous": temporal,
                "orientation_required": orientation_required,
                "orientation_strategy": orientation_strategy,
                "selection_reason": _SYNTHETIC_SELECTION_REASON,
            }
        )
    return {
        "schema_version": SELECTION_RESPONSE_SCHEMA_VERSION,
        "story_id": legal_options["story_id"],
        "production_slot": legal_options["production_slot"],
        "finalist": {
            "teaser": teaser_block,
            "body_partition_id": partition["partition_id"],
            "body_block_orientations": body_block_orientations,
        },
        "planning_risks": [],
    }


# ── Local orientation selection ──────────────────────────────────────────────

def _orientation_strategy_for_beats(
    beats: list[dict[str, Any]],
) -> str | None:
    """Choose an evidence-backed cue type without asking a model to guess."""

    observable = {
        item.get("observable_via")
        for beat in beats
        for item in beat.get("must_show", [])
        if isinstance(item, dict)
        and isinstance(item.get("observable_via"), str)
    }
    if observable.intersection({"dialogue", "mixed"}):
        return "dialogue_anchor"
    if observable.intersection({"visual", "action", "reaction"}):
        return "visual_anchor"
    if "screen_text" in observable:
        return "title_card"
    return None


def _candidate_block_orientation_inputs(
    legal_options: dict[str, Any],
    story_script: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    partition = legal_options["legal_body_partitions"][0]
    options_by_id = {
        item["option_id"]: item
        for item in legal_options.get("legal_block_options", [])
    }
    beats_by_id = {
        item["id"]: item
        for item in story_script.get("beats", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    spans_by_id = {
        item["span_candidate_id"]: item
        for item in legal_options.get("span_catalog", [])
        if isinstance(item, dict)
        and isinstance(item.get("span_candidate_id"), str)
    }
    inputs: list[dict[str, Any]] = []
    ambiguities: list[str] = []
    for index, option_id in enumerate(partition.get("body_option_ids", [])):
        option = options_by_id.get(option_id)
        if option is None:
            ambiguities.append(
                f"segment[{index}] missing Body Option {option_id}"
            )
            continue
        beats = [
            beats_by_id[beat_id]
            for beat_id in option.get("beat_ids", [])
            if beat_id in beats_by_id
        ]
        missing_beats = sorted(
            set(option.get("beat_ids", [])) - set(beats_by_id)
        )
        positions = {
            beat.get("temporal_position")
            for beat in beats
            if isinstance(beat.get("temporal_position"), str)
        }
        if missing_beats:
            ambiguities.append(
                f"segment[{index}] unknown Beat IDs {missing_beats}"
            )
        if len(positions) != 1:
            ambiguities.append(
                f"segment[{index}] temporal positions are ambiguous: "
                f"{sorted(item for item in positions if isinstance(item, str))}"
            )
        episodes = [
            spans_by_id[span_id].get("episode")
            for span_id in option.get("span_candidate_ids", [])
            if span_id in spans_by_id
        ]
        if not episodes or any(
            not isinstance(episode, int) or isinstance(episode, bool)
            for episode in episodes
        ):
            ambiguities.append(
                f"segment[{index}] has no complete integer Episode range"
            )
        inputs.append(
            {
                "index": index,
                "option_id": option_id,
                "beats": beats,
                "temporal_position": (
                    next(iter(positions)) if len(positions) == 1 else None
                ),
                "episode_min": min(episodes) if episodes else None,
                "episode_max": max(episodes) if episodes else None,
            }
        )
    if len(inputs) != int(partition.get("segment_count", 0)):
        ambiguities.append("Body Partition segment count is inconsistent")
    return inputs, list(dict.fromkeys(ambiguities))


def build_local_orientation_selection(
    legal_options: dict[str, Any],
    story_script: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Compile a locked Candidate's temporal orientation locally."""

    if not is_unique_option_case(legal_options):
        return None, ["Candidate is not locked to one Body/Teaser option"]
    selection = build_synthetic_selection(legal_options)
    inputs, ambiguities = _candidate_block_orientation_inputs(
        legal_options, story_script
    )
    if ambiguities:
        return None, ambiguities

    teaser_present = bool(legal_options.get("legal_teaser_options"))
    previous_mode = "future_preview" if teaser_present else None
    previous_min: int | None = None
    previous_max: int | None = None
    episode_flashback_anchor: tuple[int, int] | None = None
    compiled: list[dict[str, Any]] = []
    for item in inputs:
        index = int(item["index"])
        mode = str(item["temporal_position"])
        episode_min = int(item["episode_min"])
        episode_max = int(item["episode_max"])
        relation = "continuation"

        if not teaser_present and index == 0:
            relation = "continuation"
        elif mode == "earlier_context":
            relation = (
                "continuation"
                if previous_mode == "earlier_context"
                else "flashback_context"
            )
        elif mode == "future_preview":
            relation = (
                "continuation"
                if previous_mode == "future_preview"
                else "preview_future"
            )
        elif mode == "parallel":
            relation = (
                "continuation" if previous_mode == "parallel" else "parallel"
            )
        elif mode == "mainline":
            returning_from_authored_nonmainline = previous_mode in {
                "earlier_context",
                "future_preview",
                "parallel",
            }
            returning_from_episode_flashback = (
                episode_flashback_anchor is not None
                and episode_min >= episode_flashback_anchor[0]
            )
            if (
                returning_from_authored_nonmainline
                or returning_from_episode_flashback
            ):
                relation = "return_to_mainline"
                episode_flashback_anchor = None
            elif (
                previous_min is not None
                and previous_max is not None
                and episode_max < previous_min
            ):
                relation = "flashback_context"
                episode_flashback_anchor = (previous_min, previous_max)
            else:
                relation = "continuation"
        else:
            return None, [
                f"segment[{index}] unsupported temporal position {mode!r}"
            ]

        nonlinear = relation != "continuation"
        strategy = (
            _orientation_strategy_for_beats(item["beats"])
            if nonlinear
            else "none"
        )
        if nonlinear and strategy is None:
            return None, [
                f"segment[{index}] {relation} has no observable orientation cue"
            ]
        compiled.append(
            {
                "temporal_relation_from_previous": relation,
                "orientation_required": nonlinear,
                "orientation_strategy": strategy,
                "selection_reason": (
                    f"{LOCAL_ORIENTATION_METHOD}: temporal_position={mode}, "
                    f"episodes={episode_min}-{episode_max}"
                ),
            }
        )
        previous_mode = mode
        previous_min = episode_min
        previous_max = episode_max

    selection["finalist"]["body_block_orientations"] = compiled
    selection["planning_risks"] = []
    return selection, []


# ── Orientation fallback schema ──────────────────────────────────────────────

def orientation_fallback_response_schema(
    *,
    story_id: str,
    production_slot: int,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one strict model response for all ambiguous Candidates in a Story."""

    orientation_item = {
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
                    "none",
                    "dialogue_anchor",
                    "visual_anchor",
                    "title_card",
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
    properties: dict[str, Any] = {}
    for candidate in candidates:
        partition = candidate["legal_body_partitions"][0]
        segment_count = int(partition["segment_count"])
        properties[candidate["plan_candidate_id"]] = {
            "type": "array",
            "items": orientation_item,
            "minItems": segment_count,
            "maxItems": segment_count,
        }
    return {
        "type": "object",
        "properties": {
            "schema_version": {
                "type": "string",
                "const": ORIENTATION_FALLBACK_SCHEMA_VERSION,
            },
            "story_id": {"type": "string", "const": story_id},
            "production_slot": {
                "type": "integer",
                "const": production_slot,
            },
            "candidate_orientations": {
                "type": "object",
                "properties": properties,
                "required": sorted(properties),
                "additionalProperties": False,
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
            "candidate_orientations",
            "planning_risks",
        ],
        "additionalProperties": False,
    }
