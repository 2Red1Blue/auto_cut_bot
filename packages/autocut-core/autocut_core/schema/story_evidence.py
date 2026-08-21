"""Closed Story Evidence v4 schema composition.

This module is deliberately a dependency leaf.  It knows JSON-schema-shaped
data only; compatibility glue supplies the already-built component schemas.
That keeps Story Evidence from importing the compatibility registry or any
producer, validator, or Stage implementation.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

Schema = dict[str, object]


@dataclass(frozen=True)
class StoryEvidenceSchemaComponents:
    """Schemas owned by adjacent contracts and supplied by compatibility glue."""

    teaser_contract: Schema
    window_analysis: Schema
    event_card: Schema
    highlight_hook_candidate: Schema
    character: Schema
    relationship: Schema
    fact: Schema
    story_thread: Schema
    thread_beat: Schema
    open_question: Schema
    causal_dependency: Schema


@dataclass(frozen=True)
class StoryEvidenceSchemas:
    """The two public, closed v4 response schemas."""

    packet: Schema
    index: Schema


def _obj(properties: dict[str, Schema], *, required: list[str] | None = None) -> Schema:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


def _arr(items: Schema, *, minimum: int | None = None) -> Schema:
    schema: Schema = {"type": "array", "items": items}
    if minimum is not None:
        schema["minItems"] = minimum
    return schema


def _string(*, nonempty: bool = False) -> Schema:
    schema: Schema = {"type": "string"}
    if nonempty:
        schema["minLength"] = 1
    return schema


def _sha256() -> Schema:
    return {"type": "string", "minLength": 64, "maxLength": 64}


def build_story_evidence_schemas(
    components: StoryEvidenceSchemaComponents,
) -> StoryEvidenceSchemas:
    """Compose strict packet and index schemas from typed component inputs."""

    string = _string()
    nonempty = _string(nonempty=True)
    strings = _arr(string)
    boolean: Schema = {"type": "boolean"}
    positive_episode: Schema = {"type": "integer", "minimum": 1}
    nonnegative_number: Schema = {"type": "number", "minimum": 0}

    range_ref = _obj(
        {
            "source_id": nonempty,
            "episode": positive_episode,
            "start": nonnegative_number,
            "end": nonnegative_number,
            "origin": {"type": "string", "enum": ["event", "candidate"]},
            "origin_id": nonempty,
            "evidence_window_ids": strings,
        }
    )
    requested_ids = _obj(
        {
            "character_ids": strings,
            "relationship_ids": strings,
            "story_thread_ids": strings,
            "thread_beat_ids": strings,
            "fact_ids": strings,
            "event_ids": strings,
            "candidate_ids": strings,
        }
    )
    must_show = _obj(
        {
            "must_show_id": nonempty,
            "description": nonempty,
            "observable_via": {
                "type": "string",
                "enum": ["visual", "dialogue", "action", "screen_text", "reaction", "mixed"],
            },
            "requested_event_ids": strings,
            "requested_fact_ids": strings,
            "direct_event_ids": strings,
            "fact_context_event_ids": strings,
            "resolved_event_ids": strings,
            "status": {"type": "string", "enum": ["covered", "missing"]},
        }
    )
    ancestor_range = _obj(
        {
            "min_episode": positive_episode,
            "max_episode": positive_episode,
            "reason": string,
        }
    )
    nullable_ancestor_range: Schema = {
        "anyOf": [ancestor_range, {"type": "null"}],
    }
    cross_unit_context = _obj(
        {
            "beat_id": nonempty,
            "opening_candidate_id": nonempty,
            "required_context_ids": strings,
            "required_event_ids": strings,
            "ancestor_episode_range": nullable_ancestor_range,
            "source_episode_ids": _arr(positive_episode),
            "source_unit_ids": strings,
            "covered_event_ids": strings,
            "missing_event_ids": strings,
            "retrieval_status": {
                "type": "string",
                "enum": ["covered", "partial", "missing", "not_required"],
            },
            "cross_unit_required": boolean,
            "reason": nonempty,
        }
    )
    cross_unit_report = _obj(
        {
            "schema_version": {"type": "string", "const": "1.0"},
            "method": {"type": "string", "const": "series-global-causal-routing-v1"},
            "story_id": nonempty,
            "opening_candidate_id": nonempty,
            "status": {
                "type": "string",
                "enum": ["covered", "partial", "missing", "not_required"],
            },
            "may_continue_to_story_plan": boolean,
            "analysis_unit_policy": {"type": "string", "const": "processing_only"},
            "story_scope_policy": {"type": "string", "const": "series_global"},
            "ancestor_episode_range": nullable_ancestor_range,
            "source_unit_ids": strings,
            "required_context_ids": strings,
            "covered_context_ids": strings,
            "missing_context_ids": strings,
            "beats": _arr(cross_unit_context),
        }
    )
    beat_evidence = _obj(
        {
            "beat_id": nonempty,
            "role": nonempty,
            "must_have": boolean,
            "temporal_position": nonempty,
            "search_intent": nonempty,
            "continuity": {
                "type": "string",
                "enum": ["continuous_scene", "causal_chain", "montage_allowed"],
            },
            "lookback": {
                "type": "string",
                "enum": ["same_episode", "earlier_episodes", "whole_series"],
            },
            "requested_ids": requested_ids,
            "resolved_thread_beat_ids": strings,
            "must_show_evidence": _arr(must_show, minimum=1),
            "direct_event_ids": strings,
            "fact_context_event_ids": strings,
            "expanded_event_ids": strings,
            "candidate_ids": strings,
            "evidence_window_ids": strings,
            "context_window_ids": strings,
            "source_ids": strings,
            "direct_range_refs": _arr(range_ref),
            "candidate_range_refs": _arr(range_ref),
            "context_range_refs": _arr(range_ref),
            "range_refs": _arr(range_ref),
            "script_evidence_status": {
                "type": "string",
                "enum": ["covered", "partial", "missing", "conflicting", "needs_video_review"],
            },
            "retrieval_status": {
                "type": "string",
                "enum": ["covered", "partial", "missing", "needs_video_review"],
            },
            "missing_requirements": strings,
            "material_risks": strings,
            "causal_dependency": deepcopy(components.causal_dependency),
            "cross_unit_context": cross_unit_context,
        },
        required=[
            "beat_id",
            "role",
            "must_have",
            "temporal_position",
            "search_intent",
            "continuity",
            "lookback",
            "requested_ids",
            "resolved_thread_beat_ids",
            "must_show_evidence",
            "direct_event_ids",
            "fact_context_event_ids",
            "expanded_event_ids",
            "candidate_ids",
            "evidence_window_ids",
            "context_window_ids",
            "source_ids",
            "direct_range_refs",
            "candidate_range_refs",
            "context_range_refs",
            "range_refs",
            "script_evidence_status",
            "retrieval_status",
            "missing_requirements",
            "material_risks",
        ],
    )
    evidence_source = _obj(
        {
            "id": nonempty,
            "episode": positive_episode,
            "duration_seconds": nonnegative_number,
            "locator_type": {
                "type": "string",
                "enum": ["local_path", "remote_url", "unavailable"],
            },
            "locator": string,
        }
    )
    packet = _obj(
        {
            "schema_version": {"type": "string", "const": "1.2"},
            "method": {"type": "string", "const": "structured-thread-beat-recall-v4"},
            "story_id": nonempty,
            "title": nonempty,
            "production_slot": positive_episode,
            "teaser_contract": deepcopy(components.teaser_contract),
            "status": {
                "type": "string",
                "enum": ["ready", "needs_video_review", "incomplete"],
            },
            "approval_binding": _obj(
                {
                    "story_script_sha256": _sha256(),
                    "portfolio_sha256": _sha256(),
                    "decided_at": nonempty,
                    "accepted_material_risks": boolean,
                    "reviewer_notes": string,
                }
            ),
            "input_fingerprints": _obj(
                {
                    "story_approval_sha256": _sha256(),
                    "series_bible_sha256": _sha256(),
                    "event_cards_sha256": _sha256(),
                    "candidate_catalog_sha256": _sha256(),
                    "source_manifest_sha256": _sha256(),
                    "window_manifest_sha256": _sha256(),
                    "window_summaries_sha256": _sha256(),
                }
            ),
            "retrieval_policy": _obj(
                {
                    "adjacent_window_hops": {"type": "integer", "minimum": 0, "maximum": 3},
                    "semantic_search_used": {"type": "boolean", "const": False},
                    "vector_search_used": {"type": "boolean", "const": False},
                    "analysis_unit_policy": {"type": "string", "const": "processing_only"},
                    "story_scope_policy": {"type": "string", "const": "series_global"},
                    "cross_unit_retrieval_enabled": {"type": "boolean", "const": True},
                }
            ),
            "cross_unit_context_report": cross_unit_report,
            "coverage_summary": _obj(
                {
                    "beat_count": positive_episode,
                    "covered_beat_ids": strings,
                    "partial_beat_ids": strings,
                    "missing_beat_ids": strings,
                    "needs_video_review_beat_ids": strings,
                    "must_have_missing_beat_ids": strings,
                    "required_thread_beat_ids": strings,
                    "covered_thread_beat_ids": strings,
                    "missing_required_thread_beat_ids": strings,
                    "source_count": {"type": "integer", "minimum": 0},
                    "range_count": {"type": "integer", "minimum": 0},
                    "unique_evidence_duration_seconds": nonnegative_number,
                }
            ),
            "beat_evidence": _arr(beat_evidence, minimum=1),
            "evidence_catalog": _obj(
                {
                    "sources": _arr(evidence_source),
                    "windows": _arr(deepcopy(components.window_analysis)),
                    "events": _arr(deepcopy(components.event_card)),
                    "candidates": _arr(deepcopy(components.highlight_hook_candidate)),
                    "characters": _arr(deepcopy(components.character)),
                    "relationships": _arr(deepcopy(components.relationship)),
                    "facts": _arr(deepcopy(components.fact)),
                    "story_threads": _arr(deepcopy(components.story_thread)),
                    "thread_beats": _arr(deepcopy(components.thread_beat)),
                    "open_questions": _arr(deepcopy(components.open_question)),
                }
            ),
        }
    )
    index = _obj(
        {
            "schema_version": {"type": "string", "const": "1.1"},
            "method": {"type": "string", "const": "structured-thread-beat-recall-v4"},
            "status": {
                "type": "string",
                "enum": ["ready", "needs_video_review", "partially_ready", "incomplete"],
            },
            "story_approval_sha256": _sha256(),
            "portfolio_sha256": _sha256(),
            "selected_story_count": positive_episode,
            "packets": _arr(
                _obj(
                    {
                        "story_id": nonempty,
                        "title": nonempty,
                        "production_slot": positive_episode,
                        "status": {
                            "type": "string",
                            "enum": ["ready", "needs_video_review", "incomplete"],
                        },
                        "path": nonempty,
                        "packet_sha256": _sha256(),
                        "story_script_sha256": _sha256(),
                    }
                ),
                minimum=1,
            ),
        }
    )
    return StoryEvidenceSchemas(packet=packet, index=index)
