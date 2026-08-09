"""autocut_core.semantic.prep.scripts — Story Script 准备阶段。

从 prepare_story_stages.py 提取的 Script 相关函数。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


from autocut_core.semantic.prep.utils import (
    _by_ids,
    batch_payload,
    parse_story_treatment_locks,
    write_context,
)
# Strategy constants (mirror compile_story_treatments.py — avoid module-level
# import to prevent cascading ImportError from missing functions).
STRATEGY_CHRONOLOGICAL = "chronological_compression"
STRATEGY_NO_REPRISE = "cold_open_no_reprise"
STRATEGY_DELAYED_REPRISE = "cold_open_delayed_reprise"
from autocut_core.io import (
    atomic_write_json,
    load_json,
    load_jsonl,
    sha256_file,
    update_project_stage,
)

from autocut_core.semantic.granularity import (
    BROAD,
    LEGACY_REMOVAL_MESSAGE,
    require_broad_story_granularity,
)

from autocut_core.semantic.portfolio_replenishment import (
    effective_slot_by_story,
    effective_story_ids,
    load_validated_replenishment,
    portfolio_binding_for_story,
    promotion_for_story,
)
from autocut_core.schema.compat import (
    STR,
    STORY_SCRIPT_DRAFT_SCHEMA,
    validate_task_response,
)
from autocut_core.contracts.teaser_contract import (
    TEASER_MAXIMUM_SECONDS,
    TEASER_PREFERRED_MINIMUM_SECONDS,
    is_teaser_eligible_highlight,
)


def build_per_story_script_schema(
    *,
    story_id: str,
    production_slot: int,
    source_thread_beat_ids: list[str] | tuple[str, ...],
    candidate_ids: set[str] | list[str] | tuple[str, ...],
    candidates: list[dict[str, Any]],
    treatment_options: list[dict[str, Any]] | None = None,
    primary_story_thread_id: str | None = None,
    treatment_options_sha256: str | None = None,
    event_ids: set[str] | list[str] | tuple[str, ...] = (),
    fact_ids: set[str] | list[str] | tuple[str, ...] = (),
    character_ids: set[str] | list[str] | tuple[str, ...] = (),
    relationship_ids: set[str] | list[str] | tuple[str, ...] = (),
    story_thread_ids: set[str] | list[str] | tuple[str, ...] = (),
    open_question_ids: set[str] | list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a per-story override of ``STORY_SCRIPT_DRAFT_SCHEMA``.

    Applies (M3 A1) teaser_contract.mode const and (P1-1) per-story
    enum for candidate / thread_beat references. End-hook references are
    additionally narrowed to the current Story's hook candidates before the
    model request is sent. Both story_id and production_slot become ``const``.
    When the Story has no hook candidates, the schema forbids ``end_hook``
    beats, forces ``ending_hook_intent.may_be_empty=true``, and requires its
    candidate list to stay empty.

    原位置: prepare_story_stages.build_per_story_script_schema (L1156, 455L)
    """
    schema = json.loads(json.dumps(STORY_SCRIPT_DRAFT_SCHEMA))
    # These fields are local audit output written after model admission.  The
    # persisted Story Script schemas keep accepting them, but exposing them in
    # strict model output lets the model impersonate a deterministic repair
    # and spuriously trip needs_regeneration.
    schema["properties"].pop("auto_repairs", None)
    schema["properties"].pop("auto_detected_semantic_gaps", None)
    schema["required"] = [
        field
        for field in schema.get("required", [])
        if field
        not in {"auto_repairs", "auto_detected_semantic_gaps"}
    ]
    schema["properties"]["beats"]["maxItems"] = 14
    if primary_story_thread_id:
        schema["properties"]["primary_story_thread_id"] = {
            "type": "string",
            "const": primary_story_thread_id,
        }
    if treatment_options_sha256:
        schema["properties"]["treatment_options_sha256"] = {
            "type": "string",
            "const": treatment_options_sha256,
        }
    thread_beat_enum_ids = sorted(set(source_thread_beat_ids))
    if not thread_beat_enum_ids:
        raise ValueError(
            "Broad Script schema generation requires at least one Thread Beat"
        )
    candidate_id_set = set(candidate_ids)
    candidate_enum_ids = sorted(candidate_id_set)

    def candidate_roles(item: dict[str, Any]) -> set[str]:
        roles = {
            role
            for role in item.get("allowed_roles", [])
            if isinstance(role, str) and role
        }
        primary_role = item.get("kind") or item.get("type")
        if isinstance(primary_role, str) and primary_role:
            roles.add(primary_role)
        return roles

    highlight_enum_ids = sorted(
        item["id"]
        for item in candidates
        if isinstance(item, dict)
        and item.get("id") in candidate_id_set
        and is_teaser_eligible_highlight(item)
    )
    hook_enum_ids = sorted(
        item["id"]
        for item in candidates
        if isinstance(item, dict)
        and item.get("id") in candidate_id_set
        and "hook" in candidate_roles(item)
    )
    tb_item_schema = {"type": "string", "enum": thread_beat_enum_ids}
    cand_item_schema = (
        {"type": "string", "enum": candidate_enum_ids}
        if candidate_enum_ids
        else dict(STR)
    )

    def enum_array(
        values: set[str] | list[str] | tuple[str, ...],
        *,
        min_items: int | None = None,
    ) -> dict[str, Any]:
        enum_ids = sorted(set(values))
        result: dict[str, Any] = {
            "type": "array",
            "items": (
                {"type": "string", "enum": enum_ids}
                if enum_ids
                else {"type": "string"}
            ),
        }
        if min_items is not None:
            result["minItems"] = min_items
        if not enum_ids:
            result["maxItems"] = 0
        return result
    schema["properties"]["story_id"] = {
        "type": "string",
        "const": story_id,
    }
    schema["properties"]["portfolio"]["properties"]["production_slot"] = {
        "type": "integer",
        "const": production_slot,
    }
    for field in ("selected_thread_beat_ids", "required_thread_beat_ids"):
        schema["properties"][field]["items"] = dict(tb_item_schema)
    source_count = len(thread_beat_enum_ids)
    minimum_selected = max(1, math.ceil(source_count * 0.8))
    schema["properties"]["selected_thread_beat_ids"].update(
        {
            "minItems": minimum_selected,
            "maxItems": source_count,
        }
    )
    schema["properties"]["omitted_thread_beats"]["maxItems"] = (
        source_count - minimum_selected
    )
    schema["properties"]["omitted_thread_beats"]["items"]["properties"][
        "thread_beat_id"
    ] = dict(tb_item_schema)
    beat_schema = schema["properties"]["beats"]["items"]
    beat_props = beat_schema["properties"]
    beat_props["candidate_suggestions"] = {
        "type": "array",
        "items": dict(cand_item_schema),
    }
    rr_props = beat_props["retrieval_requirements"]["properties"]
    rr_props["thread_beat_ids"] = {
        "type": "array",
        "items": dict(tb_item_schema),
        "minItems": 1,
    }
    rr_props["candidate_ids"] = {
        "type": "array",
        "items": dict(cand_item_schema),
    }
    schema["properties"]["character_ids"] = enum_array(
        character_ids, min_items=1
    )
    schema["properties"]["relationship_ids"] = enum_array(
        relationship_ids
    )
    schema["properties"]["story_thread_ids"] = {
        "type": "array",
        "items": {"type": "string"},
        "const": sorted(set(story_thread_ids)),
    }
    for field in (
        "required_fact_ids",
        "intentional_mystery_fact_ids",
    ):
        schema["properties"][field] = enum_array(fact_ids)
    schema["properties"]["evidence_event_ids"] = enum_array(
        event_ids, min_items=1
    )
    event_array = enum_array(event_ids)
    fact_array = enum_array(fact_ids)
    character_array = enum_array(character_ids)
    relationship_array = enum_array(relationship_ids)
    thread_array = enum_array(story_thread_ids)
    question_array = enum_array(open_question_ids)
    beat_props["event_ids"] = json.loads(json.dumps(event_array))
    for field in (
        "must_not_reveal_fact_ids",
        "required_before_fact_ids",
        "introduced_fact_ids",
    ):
        beat_props[field] = json.loads(json.dumps(fact_array))
    beat_props["resolved_question_ids"] = json.loads(
        json.dumps(question_array)
    )
    must_show_props = beat_props["must_show"]["items"]["properties"]
    must_show_props["evidence_event_ids"] = json.loads(
        json.dumps(event_array)
    )
    must_show_props["evidence_fact_ids"] = json.loads(
        json.dumps(fact_array)
    )
    rr_props["character_ids"] = json.loads(
        json.dumps(character_array)
    )
    rr_props["relationship_ids"] = json.loads(
        json.dumps(relationship_array)
    )
    rr_props["story_thread_ids"] = json.loads(
        json.dumps(thread_array)
    )
    rr_props["fact_ids"] = json.loads(json.dumps(fact_array))
    rr_props["event_ids"] = json.loads(json.dumps(event_array))
    ending_hook_props = schema["properties"]["ending_hook_intent"][
        "properties"
    ]
    ending_hook_props["story_thread_ids"] = json.loads(
        json.dumps(thread_array)
    )
    ending_hook_props["event_ids"] = json.loads(
        json.dumps(event_array)
    )
    base_teaser_schema = schema["properties"]["teaser_contract"]
    effective_treatments = list(treatment_options or [])
    if not effective_treatments:
        raise ValueError(
            "compiled Story Treatment Options are required before Script "
            "schema generation"
        )

    treatment_branches: list[dict[str, Any]] = []
    for treatment in effective_treatments:
        branch = json.loads(json.dumps(base_teaser_schema))
        props = branch["properties"]
        mode = treatment["teaser_mode"]
        strategy = treatment["strategy"]
        reprise_policy = treatment["reprise_policy"]
        props["mode"] = {"type": "string", "const": mode}
        props["treatment_option_id"] = {
            "type": "string",
            "const": treatment["treatment_option_id"],
        }
        props["strategy"] = {"type": "string", "const": strategy}
        props["reprise_policy"] = {
            "type": "string",
            "const": reprise_policy,
        }
        eligible_for_option = sorted(
            set(treatment.get("eligible_highlight_candidate_ids", []))
            & set(highlight_enum_ids)
        )
        if mode == "none":
            props["primary_highlight_candidate_id"] = {
                "type": "string",
                "const": "",
            }
        elif eligible_for_option:
            props["primary_highlight_candidate_id"] = {
                "type": "string",
                "enum": eligible_for_option,
            }
        else:
            raise ValueError(
                f"Treatment {treatment['treatment_option_id']} requires a "
                "teaser but has no eligible Highlight Candidate"
            )

        empty_array = {"type": "array", "items": {"type": "string"}, "maxItems": 0}
        if strategy == STRATEGY_CHRONOLOGICAL:
            props["explanation_beat_ids"] = json.loads(
                json.dumps(empty_array)
            )
            props["reprise_beat_ids"] = json.loads(
                json.dumps(empty_array)
            )
            props["reprise_delay_minimum_progression_beats"] = {
                "type": "integer",
                "const": 0,
            }
            props["reprise_function"] = {
                "type": "string",
                "const": "not_applicable",
            }
        elif strategy == STRATEGY_NO_REPRISE:
            props["explanation_beat_ids"] = {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            }
            props["reprise_beat_ids"] = json.loads(
                json.dumps(empty_array)
            )
            props["reprise_delay_minimum_progression_beats"] = {
                "type": "integer",
                "const": 0,
            }
            props["reprise_function"] = {
                "type": "string",
                "const": "not_applicable",
            }
        elif strategy == STRATEGY_DELAYED_REPRISE:
            props["explanation_beat_ids"] = {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            }
            props["reprise_beat_ids"] = {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            }
            props["reprise_delay_minimum_progression_beats"] = {
                "type": "integer",
                "const": int(
                    treatment.get("constraints", {}).get(
                        "minimum_progression_beats_before_reprise", 1
                    )
                ),
            }
            props["reprise_function"] = {
                "type": "string",
                "enum": [
                    "new_causal_context",
                    "relationship_reinterpretation",
                    "consequence_recontextualization",
                    "suspense_recovery",
                ],
            }
        else:
            raise ValueError(f"unknown Treatment strategy {strategy}")
        treatment_branches.append(branch)
    schema["properties"]["teaser_contract"] = (
        treatment_branches[0]
        if len(treatment_branches) == 1
        else {"anyOf": treatment_branches}
    )
    body_beat_schema = json.loads(json.dumps(beat_schema))
    body_beat_props = body_beat_schema["properties"]
    body_beat_props["role"] = {
        "type": "string",
        "enum": [
            role
            for role in beat_props["role"]["enum"]
            if role != "end_hook"
        ],
    }

    def broad_thread_role_branches(
        source_schema: dict[str, Any],
        *,
        end_hook_only: bool = False,
    ) -> dict[str, Any]:
        if (
            not isinstance(primary_story_thread_id, str)
            or not primary_story_thread_id
            or primary_story_thread_id not in set(story_thread_ids)
        ):
            return source_schema
        primary_branch = json.loads(json.dumps(source_schema))
        primary_props = primary_branch["properties"]
        primary_props["thread_role"] = {
            "type": "string",
            "const": "primary",
        }
        primary_props["retrieval_requirements"]["properties"][
            "story_thread_ids"
        ] = {
            "type": "array",
            "items": {
                "type": "string",
                "const": primary_story_thread_id,
            },
            "const": [primary_story_thread_id],
            "minItems": 1,
            "maxItems": 1,
        }
        if end_hook_only:
            return primary_branch

        integrated_branch = json.loads(json.dumps(source_schema))
        integrated_props = integrated_branch["properties"]
        integrated_props["thread_role"] = {
            "type": "string",
            "const": "integrated_support",
        }
        integrated_props["role"] = {
            "type": "string",
            "enum": [
                role
                for role in integrated_props["role"]["enum"]
                if role not in {"payoff", "end_hook"}
            ],
        }

        independent_branch = json.loads(json.dumps(source_schema))
        independent_props = independent_branch["properties"]
        independent_props["thread_role"] = {
            "type": "string",
            "const": "independent_secondary",
        }
        independent_props["role"] = {
            "type": "string",
            "enum": [
                role
                for role in independent_props["role"]["enum"]
                if role in {"orientation", "setup"}
            ],
        }
        return {
            "anyOf": [
                primary_branch,
                integrated_branch,
                independent_branch,
            ]
        }

    hook_candidate_array_schema: dict[str, Any]
    if hook_enum_ids:
        hook_candidate_array_schema = {
            "type": "array",
            "items": {"type": "string", "enum": hook_enum_ids},
        }
        end_hook_schema = json.loads(json.dumps(beat_schema))
        end_hook_props = end_hook_schema["properties"]
        end_hook_props["role"] = {"type": "string", "const": "end_hook"}
        end_hook_props["candidate_suggestions"] = json.loads(
            json.dumps(hook_candidate_array_schema)
        )
        end_hook_props["retrieval_requirements"]["properties"][
            "candidate_ids"
        ] = json.loads(json.dumps(hook_candidate_array_schema))
        schema["properties"]["beats"]["items"] = {
            "anyOf": [
                broad_thread_role_branches(body_beat_schema),
                broad_thread_role_branches(
                    end_hook_schema,
                    end_hook_only=True,
                ),
            ]
        }
    else:
        # An empty enum is not accepted by strict structured-output APIs.
        # maxItems=0 expresses the actual contract without reopening the
        # field to arbitrary strings.
        hook_candidate_array_schema = {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 0,
        }
        schema["properties"]["beats"]["items"] = (
            broad_thread_role_branches(body_beat_schema)
        )

    ending_hook_props = schema["properties"]["ending_hook_intent"][
        "properties"
    ]
    ending_hook_props["candidate_ids"] = json.loads(
        json.dumps(hook_candidate_array_schema)
    )
    if not hook_enum_ids:
        ending_hook_props["may_be_empty"] = {
            "type": "boolean",
            "const": True,
        }
    return schema


def story_script_request_contract(story_granularity: str) -> tuple[str, str]:
    """返回当前 Story Script 的 stage_version 和 schema_name。

    原位置: prepare_story_stages.story_script_request_contract (L1613, 7L)
    """
    if story_granularity != BROAD:
        raise ValueError(LEGACY_REMOVAL_MESSAGE)
    return (
        "story-first-story-script-v18-broad-contract-guided-authoring",
        "story_script_v18_broad_contract_guided_authoring_schema",
    )


def _overlapping_timeline_segment_refs(
    *,
    source_id: str,
    ranges: list[dict[str, Any]],
    timeline_segments: list[dict[str, Any]],
) -> list[str]:
    """返回与给定 source 区间重叠的 timeline segment ref 列表。

    原位置: prepare_story_stages._overlapping_timeline_segment_refs (L1622, 34L)
    """
    refs: set[str] = set()
    for source_range in ranges:
        start = source_range.get("start")
        end = source_range.get("end")
        if (
            not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or float(end) <= float(start)
        ):
            continue
        for segment in timeline_segments:
            if segment.get("source_id") != source_id:
                continue
            segment_start = segment.get("start")
            segment_end = segment.get("end")
            if (
                isinstance(segment_start, (int, float))
                and not isinstance(segment_start, bool)
                and isinstance(segment_end, (int, float))
                and not isinstance(segment_end, bool)
                and min(float(end), float(segment_end))
                - max(float(start), float(segment_start))
                > 0.001
            ):
                refs.add(str(segment["segment_ref"]))
    return sorted(refs)


def build_story_script_direct_evidence_contract(
    *,
    thread_beats: list[dict[str, Any]],
    events: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    timeline_segments: list[dict[str, Any]],
    teaser_eligible_candidate_ids: list[str] | None = None,
    facts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compile the legal Thread Beat -> Event -> physical-unit mapping.

    This contract is deliberately descriptive rather than generative.  It
    tells the model which direct Events are legal for each Thread Beat and
    where those Events physically live, but it never manufactures Editorial
    Beats or decides how the Story should be written.

    原位置: prepare_story_stages.build_story_script_direct_evidence_contract (L1658, 339L)
    """
    from preflight_story_scripts import beat_physical_evidence_diagnostics

    normalized_segments = [
        {
            "source_id": str(item["source_id"]),
            "start": round(float(item["start"]), 3),
            "end": round(float(item["end"]), 3),
            "mode": str(item.get("mode") or "unknown"),
        }
        for item in timeline_segments
        if isinstance(item, dict)
        and isinstance(item.get("source_id"), str)
        and isinstance(item.get("start"), (int, float))
        and not isinstance(item.get("start"), bool)
        and isinstance(item.get("end"), (int, float))
        and not isinstance(item.get("end"), bool)
        and float(item["end"]) > float(item["start"])
    ]
    normalized_segments.sort(
        key=lambda item: (
            item["source_id"],
            item["start"],
            item["end"],
            item["mode"],
        )
    )
    referenced_segments = [
        {**item, "segment_ref": f"timeline-segment-{index:04d}"}
        for index, item in enumerate(normalized_segments, start=1)
    ]
    event_by_id = {
        item["id"]: item
        for item in events
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    fact_ids_by_event: dict[str, set[str]] = {}
    for fact in facts or []:
        if not isinstance(fact, dict) or not isinstance(fact.get("id"), str):
            continue
        for event_id in fact.get("event_ids", []) or []:
            if isinstance(event_id, str):
                fact_ids_by_event.setdefault(event_id, set()).add(fact["id"])
    allowed_thread_beat_ids_by_event: dict[str, list[str]] = {}
    thread_beat_event_ids: list[tuple[str, list[str]]] = []
    for thread_beat in thread_beats:
        if not isinstance(thread_beat, dict) or not isinstance(
            thread_beat.get("id"), str
        ):
            continue
        allowed_event_ids = list(
            dict.fromkeys(
                event_id
                for event_id in thread_beat.get("event_ids", []) or []
                if isinstance(event_id, str) and event_id in event_by_id
            )
        )
        thread_beat_id = thread_beat["id"]
        thread_beat_event_ids.append((thread_beat_id, allowed_event_ids))
        for event_id in allowed_event_ids:
            allowed_thread_beat_ids_by_event.setdefault(event_id, []).append(
                thread_beat_id
            )

    event_contracts: list[dict[str, Any]] = []
    for event_id in sorted(event_by_id):
        event = event_by_id[event_id]
        source_id = event.get("source_id")
        if not isinstance(source_id, str):
            continue
        ranges = [
            {
                "start": round(float(item["start"]), 3),
                "end": round(float(item["end"]), 3),
            }
            for item in event.get("source_ranges", []) or []
            if isinstance(item, dict)
            and isinstance(item.get("start"), (int, float))
            and not isinstance(item.get("start"), bool)
            and isinstance(item.get("end"), (int, float))
            and not isinstance(item.get("end"), bool)
            and float(item["end"]) > float(item["start"])
        ]
        ranges.sort(key=lambda item: (item["start"], item["end"]))
        event_contracts.append(
            {
                "event_id": event_id,
                "source_id": source_id,
                "source_ranges": ranges,
                "timeline_segment_refs": _overlapping_timeline_segment_refs(
                    source_id=source_id,
                    ranges=ranges,
                    timeline_segments=referenced_segments,
                ),
                "allowed_thread_beat_ids": sorted(
                    allowed_thread_beat_ids_by_event.get(event_id, [])
                ),
            }
        )

    located_events = [
        item
        for item in event_contracts
        if item["source_ranges"]
    ]
    located_events.sort(
        key=lambda item: (
            item["source_id"],
            min(source_range["start"] for source_range in item["source_ranges"]),
            max(source_range["end"] for source_range in item["source_ranges"]),
            item["event_id"],
        )
    )
    physical_units_internal: list[dict[str, Any]] = []
    for event_contract in located_events:
        source_id = event_contract["source_id"]
        event_ranges = [
            (source_id, item["start"], item["end"])
            for item in event_contract["source_ranges"]
        ]
        current = (
            physical_units_internal[-1]
            if physical_units_internal
            and physical_units_internal[-1]["source_id"] == source_id
            else None
        )
        if current is not None:
            combined_ranges = [*current["ranges"], *event_ranges]
            combined_diagnostics = beat_physical_evidence_diagnostics(
                combined_ranges,
                timeline_segments=referenced_segments,
                atomic_event_count=len(current["event_ids"]) + 1,
                continuity_mode="independent",
            )
        else:
            combined_diagnostics = None
        if (
            current is not None
            and combined_diagnostics is not None
            and combined_diagnostics["compaction_status"] == "atomic"
        ):
            current["event_ids"].append(event_contract["event_id"])
            current["ranges"] = combined_ranges
            current["timeline_segment_refs"].update(
                event_contract["timeline_segment_refs"]
            )
            current["diagnostics"] = combined_diagnostics
            continue
        diagnostics = beat_physical_evidence_diagnostics(
            event_ranges,
            timeline_segments=referenced_segments,
            atomic_event_count=1,
            continuity_mode="independent",
        )
        physical_units_internal.append(
            {
                "source_id": source_id,
                "event_ids": [event_contract["event_id"]],
                "ranges": event_ranges,
                "timeline_segment_refs": set(
                    event_contract["timeline_segment_refs"]
                ),
                "diagnostics": diagnostics,
            }
        )

    physical_units: list[dict[str, Any]] = []
    physical_unit_id_by_event: dict[str, str] = {}
    for index, item in enumerate(physical_units_internal, start=1):
        unit_id = f"physical-unit-{index:04d}"
        for event_id in item["event_ids"]:
            physical_unit_id_by_event[event_id] = unit_id
        diagnostics = item["diagnostics"]
        physical_units.append(
            {
                "physical_unit_id": unit_id,
                "source_id": item["source_id"],
                "event_ids": item["event_ids"],
                "source_envelope": {
                    "start": min(start for _, start, _ in item["ranges"]),
                    "end": max(end for _, _, end in item["ranges"]),
                },
                "timeline_segment_refs": sorted(
                    item["timeline_segment_refs"]
                ),
                "combined_compaction_status": diagnostics[
                    "compaction_status"
                ],
            }
        )

    event_contracts = [
        {
            **item,
            "physical_unit_id": physical_unit_id_by_event.get(
                item["event_id"], ""
            ),
        }
        for item in event_contracts
    ]
    events_by_physical_unit = {
        item["physical_unit_id"]: item["event_ids"]
        for item in physical_units
    }
    thread_beat_contracts = []
    for thread_beat_id, allowed_event_ids in thread_beat_event_ids:
        physical_event_options = [
            {
                "physical_unit_id": unit_id,
                "event_ids": [
                    event_id
                    for event_id in allowed_event_ids
                    if event_id in unit_event_ids
                ],
            }
            for unit_id, unit_event_ids in events_by_physical_unit.items()
            if any(event_id in unit_event_ids for event_id in allowed_event_ids)
        ]
        thread_beat_contracts.append(
            {
                "thread_beat_id": thread_beat_id,
                "allowed_direct_event_ids": allowed_event_ids,
                "allowed_direct_event_ids_are_optional_menu": True,
                "copying_complete_event_menu_forbidden": True,
                "physical_event_options": physical_event_options,
            }
        )

    eligible_teaser_ids = set(teaser_eligible_candidate_ids or [])
    candidate_contracts: list[dict[str, Any]] = []
    teaser_candidate_contracts: list[dict[str, Any]] = []
    for candidate in sorted(
        (item for item in candidates if isinstance(item, dict)),
        key=lambda item: str(item.get("id") or ""),
    ):
        candidate_id = candidate.get("id")
        source_id = candidate.get("source_id")
        start = candidate.get("start")
        end = candidate.get("end")
        if (
            not isinstance(candidate_id, str)
            or not isinstance(source_id, str)
            or not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or float(end) <= float(start)
        ):
            continue
        ranges = [{"start": round(float(start), 3), "end": round(float(end), 3)}]
        candidate_event_ids = list(
            dict.fromkeys(
                event_id
                for event_id in candidate.get("event_ids", []) or []
                if isinstance(event_id, str)
            )
        )
        contract = {
            "candidate_id": candidate_id,
            "source_id": source_id,
            "source_range": ranges[0],
            "event_ids": candidate_event_ids,
            "physical_unit_ids": sorted(
                {
                    physical_unit_id_by_event[event_id]
                    for event_id in candidate_event_ids
                    if event_id in physical_unit_id_by_event
                }
            ),
            "directly_revealed_fact_ids": sorted(
                {
                    fact_id
                    for event_id in candidate_event_ids
                    for fact_id in fact_ids_by_event.get(event_id, set())
                }
            ),
            "timeline_segment_refs": _overlapping_timeline_segment_refs(
                source_id=source_id,
                ranges=ranges,
                timeline_segments=referenced_segments,
            ),
        }
        candidate_contracts.append(contract)
        if candidate_id in eligible_teaser_ids:
            teaser_candidate_contracts.append(
                {
                    "candidate_id": candidate_id,
                    "source_id": source_id,
                    "primary_highlight_range": ranges[0],
                    "safest_direct_event_ids": candidate_event_ids,
                    "physical_unit_ids": contract["physical_unit_ids"],
                    "directly_revealed_fact_ids": contract[
                        "directly_revealed_fact_ids"
                    ],
                    "candidate_only_direct_evidence_preferred": True,
                    "background_explanation_events_forbidden": True,
                }
            )

    return {
        "schema_version": "1.2",
        "purpose": "legal_direct_evidence_and_physical_units_only",
        "editorial_beat_generation_forbidden": True,
        "authoring_guidance": {
            "allowed_direct_event_ids_semantics": "optional_menu_not_checklist",
            "selection_rule": (
                "choose_the_smallest_semantically_sufficient_direct_event_subset"
            ),
            "copy_complete_thread_beat_event_menu_forbidden": True,
            "same_editorial_beat_direct_events_must_share_physical_unit_id": True,
            "multi_thread_beat_mapping_does_not_authorize_physical_merging": True,
        },
        "thread_beats": thread_beat_contracts,
        "allowed_thread_beat_ids_by_event": {
            event_id: sorted(thread_beat_ids)
            for event_id, thread_beat_ids in sorted(
                allowed_thread_beat_ids_by_event.items()
            )
        },
        "events": event_contracts,
        "physical_event_units": physical_units,
        "candidates": candidate_contracts,
        "teaser_candidates": teaser_candidate_contracts,
        "timeline_segments": referenced_segments,
    }


def prepare_scripts(args: argparse.Namespace) -> Path:
    """准备 Story Script 批处理 manifest。

    原位置: prepare_story_stages.prepare_scripts (L1999, 566L)
    """
    job_root = args.job_root.resolve()
    catalog_path = args.story_catalog.expanduser().resolve()
    series_bible_path = args.series_bible.expanduser().resolve()
    candidate_catalog_path = args.candidate_catalog.expanduser().resolve()
    catalog = load_json(catalog_path)
    portfolio_path = args.story_portfolio.expanduser().resolve()
    portfolio = load_json(portfolio_path)
    story_granularity = require_broad_story_granularity(catalog, portfolio)
    portfolio_errors = validate_task_response("story_portfolio", portfolio)
    if portfolio_errors:
        raise ValueError(
            "invalid Story Portfolio: " + "; ".join(portfolio_errors[:30])
        )
    portfolio_sha256 = sha256_file(portfolio_path)
    replenishment_path_value = getattr(
        args, "story_portfolio_replenishment", None
    )
    replenishment_path = (
        replenishment_path_value.expanduser().resolve()
        if isinstance(replenishment_path_value, Path)
        else job_root / "story-portfolio-replenishment.json"
    )
    replenishment = load_validated_replenishment(
        replenishment_path,
        story_catalog_path=catalog_path,
        story_portfolio_path=portfolio_path,
        series_bible_path=series_bible_path,
    )
    slots = effective_slot_by_story(portfolio, replenishment)
    primary_story_ids = set(portfolio.get("primary_story_ids", []))
    eligible_story_ids = effective_story_ids(portfolio, replenishment)
    target_story_ids = {
        str(item).strip()
        for item in getattr(args, "target_story_id", []) or []
        if str(item).strip()
    }
    treatment_locks = parse_story_treatment_locks(
        getattr(args, "lock_treatment_option", [])
    )
    catalog_story_ids = {
        item.get("story_id")
        for item in catalog.get("stories", [])
        if isinstance(item, dict)
    }
    unknown_primary = sorted(primary_story_ids - catalog_story_ids)
    if unknown_primary:
        raise ValueError(
            f"Story Portfolio contains unknown Primary IDs: {unknown_primary}"
        )
    unknown_target_story_ids = sorted(
        target_story_ids - eligible_story_ids
    )
    if unknown_target_story_ids:
        raise ValueError(
            "Script generation targets contain Stories that do not own a "
            "current production slot: "
            f"{unknown_target_story_ids}"
        )
    if target_story_ids:
        locks_outside_targets = sorted(
            set(treatment_locks) - target_story_ids
        )
        if locks_outside_targets:
            raise ValueError(
                "Treatment generation locks must be contained in the "
                "explicit target Story set: "
                f"{locks_outside_targets}"
            )
    unknown_lock_story_ids = sorted(set(treatment_locks) - eligible_story_ids)
    if unknown_lock_story_ids:
        raise ValueError(
            "Treatment generation locks contain Stories that do not own a "
            "current production slot: "
            f"{unknown_lock_story_ids}"
        )
    treatment_path_value = getattr(args, "story_treatment_options", None)
    treatment_path = (
        treatment_path_value.expanduser().resolve()
        if isinstance(treatment_path_value, Path)
        else job_root / "story-treatment-options.json"
    )
    if not treatment_path.is_file():
        from compile_story_treatments import (
            compile_from_paths as compile_treatments_from_paths,
        )
        treatment_payload = compile_treatments_from_paths(
            story_catalog_path=catalog_path,
            story_portfolio_path=portfolio_path,
            series_bible_path=series_bible_path,
            candidate_catalog_path=candidate_catalog_path,
        )
        atomic_write_json(treatment_path, treatment_payload)
    else:
        treatment_payload = load_json(treatment_path)
    require_broad_story_granularity(treatment_payload)
    from compile_story_treatments import (
        validate_current_inputs as validate_treatment_inputs,
    )
    treatment_errors = validate_treatment_inputs(
        treatment_payload,
        story_catalog_path=catalog_path,
        story_portfolio_path=portfolio_path,
        series_bible_path=series_bible_path,
        candidate_catalog_path=candidate_catalog_path,
    )
    if treatment_errors:
        raise ValueError(
            "invalid or stale Story Treatment Options: "
            + "; ".join(treatment_errors[:30])
        )
    treatment_sha256 = sha256_file(treatment_path)
    treatment_by_story = {
        item["story_id"]: item
        for item in treatment_payload.get("stories", [])
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }

    bible = load_json(series_bible_path)
    thread_beat_by_id = {
        item["id"]: item
        for item in bible.get("thread_beats", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    events = load_jsonl(args.event_cards)
    window_summaries_path = job_root / "window-summaries.jsonl"
    window_records = (
        load_jsonl(window_summaries_path)
        if window_summaries_path.is_file()
        else []
    )
    from preflight_story_scripts import timeline_segments_from_windows
    available_timeline_segments = timeline_segments_from_windows(
        window_records
    )
    candidates_payload = load_json(candidate_catalog_path)
    candidates = candidates_payload.get("candidates", [])
    jobs = []
    context_dir = job_root / "intermediate" / "story-script-contexts"
    output_dir = job_root / "story-scripts"
    generation_story_ids = target_story_ids or eligible_story_ids
    for story in catalog.get("stories", []):
        if story.get("story_id") not in generation_story_ids:
            continue
        story_treatment = treatment_by_story.get(story["story_id"])
        if not isinstance(story_treatment, dict):
            raise ValueError(
                f"{story['story_id']} has no compiled Story Treatment record"
            )
        source_recommended_treatment_option_id = str(
            story_treatment.get("recommended_treatment_option_id") or ""
        )
        locked_treatment_option_id = treatment_locks.get(story["story_id"])
        if locked_treatment_option_id:
            locked_options = [
                item
                for item in story_treatment.get("options", [])
                if isinstance(item, dict)
                and item.get("treatment_option_id")
                == locked_treatment_option_id
            ]
            if len(locked_options) != 1:
                raise ValueError(
                    f"{story['story_id']} Treatment generation lock references "
                    f"unknown or duplicate Option {locked_treatment_option_id!r}"
                )
            # Do not mutate the deterministic Treatment artifact.  This is a
            # new Script request generation whose context and strict Schema
            # expose exactly one already-compiled Option.
            story_treatment = json.loads(json.dumps(story_treatment))
            story_treatment["options"] = locked_options
            story_treatment[
                "recommended_treatment_option_id"
            ] = locked_treatment_option_id
        event_ids = set(story.get("evidence_event_ids", []))
        thread_ids = set(story.get("story_thread_ids", []))
        character_ids = set(story.get("character_ids", []))
        relationship_ids = set(story.get("relationship_ids", []))
        fact_ids = set(story.get("required_fact_ids", []))
        source_thread_beat_ids = list(story.get("source_thread_beat_ids", []))
        unknown_thread_beats = sorted(
            set(source_thread_beat_ids) - set(thread_beat_by_id)
        )
        if unknown_thread_beats:
            raise ValueError(
                f"{story['story_id']} contains unknown Thread Beats: "
                f"{unknown_thread_beats}"
            )
        required_thread_beat_ids = list(
            dict.fromkeys(
                [
                    story.get("subarc_start_beat_id"),
                    *story.get("required_bridge_beat_ids", []),
                    *[
                        beat_id
                        for beat_id in source_thread_beat_ids
                        if thread_beat_by_id[beat_id].get("importance") == "required"
                    ],
                    story.get("subarc_end_beat_id"),
                ]
            )
        )
        required_thread_beat_ids = [
            item for item in required_thread_beat_ids if isinstance(item, str) and item
        ]
        if not set(required_thread_beat_ids) <= set(source_thread_beat_ids):
            raise ValueError(
                f"{story['story_id']} has required Thread Beats outside its source subarc"
            )
        for beat_id in source_thread_beat_ids:
            event_ids.update(thread_beat_by_id[beat_id].get("event_ids", []))
        threads = _by_ids(bible.get("story_threads", []), thread_ids)
        for thread in threads:
            character_ids.update(thread.get("character_ids", []))
        related_events = _by_ids(events, event_ids)
        related_facts = [
            item
            for item in bible.get("facts", [])
            if item.get("id") in fact_ids
            or bool(set(item.get("event_ids", [])) & event_ids)
        ]
        fact_ids.update(
            item["id"]
            for item in related_facts
            if isinstance(item.get("id"), str)
        )
        related_relationships = [
            item
            for item in bible.get("relationships", [])
            if item.get("id") in relationship_ids
            or bool(set(item.get("character_ids", [])) & character_ids)
        ]
        relationship_ids.update(
            item["id"]
            for item in related_relationships
            if isinstance(item.get("id"), str)
        )
        related_characters = _by_ids(
            bible.get("characters", []),
            character_ids,
        )
        candidate_ids = set(story.get("suggested_highlight_candidate_ids", []))
        candidate_ids.update(story.get("suggested_hook_candidate_ids", []))
        candidate_ids.update(
            candidate_id
            for event in related_events
            for candidate_id in event.get("candidate_ids", [])
        )
        candidate_ids.update(
            candidate_id
            for option in story_treatment.get("options", [])
            if isinstance(option, dict)
            for candidate_id in option.get(
                "eligible_highlight_candidate_ids", []
            )
            if isinstance(candidate_id, str)
        )
        candidate_by_id = {
            item["id"]: item
            for item in candidates
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        eligible_highlight_candidate_ids = sorted(
            candidate_id
            for candidate_id in candidate_ids
            if candidate_id in candidate_by_id
            and is_teaser_eligible_highlight(candidate_by_id[candidate_id])
        )
        related_open_questions = [
            item
            for item in bible.get("open_questions", [])
            if set(item.get("event_ids", [])) & event_ids
        ]
        # The local pre-cache Story Script preflight resolves entity recall to
        # Event IDs exactly like the later full preflight. Include that entity
        # evidence closure in the model context so admission never rejects a
        # valid draft merely because an included Character/Fact/Thread points
        # to an otherwise context-only Event.
        admission_event_ids = set(event_ids)
        admission_event_ids.update(
            event_id
            for item in related_characters
            for event_id in item.get("evidence_event_ids", [])
            if isinstance(event_id, str)
        )
        admission_event_ids.update(
            event_id
            for item in related_facts
            for event_id in item.get("event_ids", [])
            if isinstance(event_id, str)
        )
        admission_event_ids.update(
            change.get("event_id")
            for item in related_relationships
            for change in item.get("state_changes", [])
            if isinstance(change, dict)
            and isinstance(change.get("event_id"), str)
        )
        admission_event_ids.update(
            event_id
            for item in threads
            for event_id in item.get("event_ids", [])
            if isinstance(event_id, str)
        )
        admission_event_ids.update(
            event_id
            for candidate_id in candidate_ids
            if candidate_id in candidate_by_id
            for event_id in candidate_by_id[candidate_id].get(
                "event_ids",
                [],
            )
            if isinstance(event_id, str)
        )
        admission_events = _by_ids(events, admission_event_ids)
        admission_source_ids = {
            item.get("source_id")
            for item in admission_events
            if isinstance(item.get("source_id"), str)
        }
        admission_source_ids.update(
            candidate_by_id[candidate_id].get("source_id")
            for candidate_id in candidate_ids
            if candidate_id in candidate_by_id
            and isinstance(
                candidate_by_id[candidate_id].get("source_id"), str
            )
        )
        related_candidates = _by_ids(candidates, candidate_ids)
        related_timeline_segments = [
            item
            for item in available_timeline_segments
            if item["source_id"] in admission_source_ids
        ]
        direct_evidence_contract = (
            build_story_script_direct_evidence_contract(
                thread_beats=[
                    thread_beat_by_id[item]
                    for item in source_thread_beat_ids
                ],
                events=admission_events,
                candidates=related_candidates,
                timeline_segments=related_timeline_segments,
                teaser_eligible_candidate_ids=(
                    eligible_highlight_candidate_ids
                ),
                facts=related_facts,
            )
        )
        portfolio_binding = portfolio_binding_for_story(
            portfolio=portfolio,
            portfolio_sha256=portfolio_sha256,
            replenishment=replenishment,
            story_id=story["story_id"],
        )
        promotion = promotion_for_story(replenishment, story["story_id"])
        context = {
            "schema_version": "1.6",
            "story": story,
            "thread_beats": [
                thread_beat_by_id[item] for item in source_thread_beat_ids
            ],
            "thread_beat_contract": {
                "source_thread_beat_ids": source_thread_beat_ids,
                "required_thread_beat_ids": required_thread_beat_ids,
                "selected_or_omitted_accounting_required": True,
                "required_beats_cannot_be_omitted": True,
            },
            "portfolio_binding": portfolio_binding,
            "characters": related_characters,
            "relationships": related_relationships,
            "facts": related_facts,
            "story_threads": threads,
            "open_questions": related_open_questions,
            "events": admission_events,
            "candidates": related_candidates,
            "timeline_segments": related_timeline_segments,
            "direct_evidence_contract": direct_evidence_contract,
            "teaser_eligible_highlight_candidate_ids": (
                eligible_highlight_candidate_ids
            ),
            "story_treatment": story_treatment,
            "treatment_options_sha256": treatment_sha256,
            "treatment_generation": {
                "mode": (
                    "plan_fallback_locked"
                    if locked_treatment_option_id
                    else "model_selects_compiled_option"
                ),
                "locked_treatment_option_id": (
                    locked_treatment_option_id or ""
                ),
                "source_recommended_treatment_option_id": (
                    source_recommended_treatment_option_id
                ),
            },
            "script_contract": {
                "uses_original_footage_only": True,
                "invented_dialogue_forbidden": True,
                "final_timecodes_forbidden": True,
                "must_include_local_payoff": True,
                "human_approval_required": True,
                "cross_story_source_reuse_allowed": True,
                "story_script_is_editorial_blueprint": True,
                "abstract_logline_is_not_a_script": True,
                "each_must_show_requires_event_or_fact_evidence": True,
                "must_show_direct_event_ids_are_conjunctive": True,
                "each_direct_event_should_remain_atomically_editable": True,
                "fact_and_entity_expansion_is_context_only": True,
                "wide_multi_segment_beats_require_bounded_regeneration": True,
                "direct_evidence_contract_is_authoritative": True,
                "local_code_must_not_generate_final_editorial_beats": True,
                "each_beat_requires_observable_content": True,
                "required_thread_beats_are_blocking": True,
                "feasibility_is_computed_locally_after_draft": True,
                "output_status": "draft",
                "teaser_contract": {
                    "treatment_option_ids": [
                        item["treatment_option_id"]
                        for item in story_treatment["options"]
                    ],
                    "recommended_treatment_option_id": story_treatment[
                        "recommended_treatment_option_id"
                    ],
                    "primary_story_thread_id": story_treatment[
                        "primary_story_thread_id"
                    ],
                    "mode_selection_note": (
                        "从 story_treatment.options 选择且只选择一个 "
                        "treatment_option_id。chronological_compression 无 "
                        "Teaser；cold_open_no_reprise 禁止正文重放开场原片；"
                        "cold_open_delayed_reprise 必须先完成 explanation beats "
                        "并推进主线，再以新增叙事功能重放高光。"
                    ),
                    "maximum_span_count": 1,
                    "preferred_minimum_seconds": (
                        TEASER_PREFERRED_MINIMUM_SECONDS
                    ),
                    "preferred_maximum_seconds": TEASER_MAXIMUM_SECONDS,
                    "maximum_seconds": TEASER_MAXIMUM_SECONDS,
                    "maximum_reaction_tail_seconds": 2,
                    "primary_highlight_candidate_id_must_be_selected": True,
                    "primary_highlight_candidate_id_may_be_empty_when_mode_none": True,
                    "candidate_suggestions_must_equal_primary_only": True,
                    "montage_allowed": False,
                },
                "required_causal_chain": [
                    "cause",
                    "escalation",
                    "reveal_or_turn",
                    "payoff",
                ],
            },
            "downstream_contract": {
                "retrieval_requirements_compile_to_evidence_queries": True,
                "thread_beat_ids_compile_to_required_evidence": True,
                "must_show_items_compile_to_coverage_checks": True,
                "viewer_state_compiles_to_spoiler_checks": True,
                "no_source_ranges_until_story_plan": True,
                "treatment_viability_must_pass_before_approval": True,
                "treatment_fallback_requires_fresh_script_generation": True,
            },
        }
        if promotion is not None:
            context["portfolio_replenishment"] = promotion
        context["story_granularity"] = BROAD
        context["broad_script_contract"] = {
            "selected_thread_beat_coverage_ratio_minimum": 0.8,
            "selected_thread_beat_coverage_ratio_maximum": 1.0,
            "required_thread_beats_must_all_be_selected": True,
            "one_editorial_beat_may_cover_multiple_thread_beats": True,
            "multi_thread_beat_mapping_requires_one_physical_event_unit": True,
            "allowed_direct_event_ids_are_optional_not_mandatory": True,
            "copying_complete_thread_beat_event_menu_forbidden": True,
            "editorial_beat_count_range": [4, 14],
            "required_before_facts_must_be_introduced_in_earlier_beat": True,
            "required_and_introduced_fact_same_beat_forbidden": True,
            "last_body_payoff_or_hook_must_retrieve_primary_story_thread": True,
        }
        story_id = story["story_id"]
        context_path = context_dir / f"{story_id}.json"
        output_path = output_dir / f"{story_id}.json"
        write_context(context_path, context, args.max_context_chars)
        # Compiled Treatment options are authoritative.  The dynamic schema
        # binds option ID, strategy, mode and reprise policy inside each branch
        # while candidate and Thread Beat references, story_id and production
        # slot are narrowed to this Story's actual inputs.
        script_schema = build_per_story_script_schema(
            story_id=story["story_id"],
            production_slot=slots[story["story_id"]],
            treatment_options=story_treatment["options"],
            primary_story_thread_id=story_treatment[
                "primary_story_thread_id"
            ],
            treatment_options_sha256=treatment_sha256,
            source_thread_beat_ids=source_thread_beat_ids,
            candidate_ids=candidate_ids,
            candidates=candidates,
            event_ids=event_ids,
            fact_ids=fact_ids,
            character_ids=character_ids,
            relationship_ids=relationship_ids,
            story_thread_ids=thread_ids,
            open_question_ids={
                item["id"]
                for item in related_open_questions
                if isinstance(item.get("id"), str)
            },
        )
        if promotion is not None:
            promotion_properties = script_schema["properties"]["portfolio"][
                "properties"
            ]
            for field in (
                "promotion_id",
                "promotion_fingerprint",
                "replaces_story_id",
                "root_primary_story_id",
            ):
                promotion_properties[field] = {
                    "type": "string",
                    "const": portfolio_binding[field],
                }
            required_portfolio_fields = script_schema["properties"][
                "portfolio"
            ]["required"]
            for field in (
                "promotion_id",
                "promotion_fingerprint",
                "replaces_story_id",
                "root_primary_story_id",
            ):
                if field not in required_portfolio_fields:
                    required_portfolio_fields.append(field)
        script_stage_version, script_schema_name = story_script_request_contract(
            story_granularity
        )
        jobs.append(
            {
                "id": f"story-script-{story_id}",
                "task": "story_script_draft",
                "stage_version": script_stage_version,
                "context_file": str(context_path.resolve()),
                "output": str(output_path.resolve()),
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": script_schema_name,
                        "strict": True,
                        "schema": script_schema,
                    },
                },
                "max_tokens": 32768,
                **(
                    {"portfolio_replenishment": promotion}
                    if promotion is not None
                    else {}
                ),
            }
        )
    if not jobs:
        raise ValueError(
            "Story Portfolio/replenishment contains no targeted Story to script"
        )
    manifest_path = job_root / "story-script-batch.json"
    atomic_write_json(manifest_path, batch_payload(job_root, args.backend, jobs))
    update_project_stage(
        job_root / "project.json",
        "story_script_jobs",
        "prepared",
        outputs={"batch_manifest": str(manifest_path)},
    )
    return manifest_path