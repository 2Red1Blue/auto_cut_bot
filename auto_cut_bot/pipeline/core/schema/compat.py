"""dict schema 兼容桥 — 承载所有 story_schemas 定义。

Pydantic 模型在 window.py / episode.py / registry.py / story.py 中。
兼容桥保持 dict 格式确保 validate_task_response 等函数零改动。
"""
from __future__ import annotations
from typing import Any
from copy import deepcopy

from autocut_core.contracts.teaser_contract import (
    TEASER_MAXIMUM_SECONDS,
    TEASER_PREFERRED_MINIMUM_SECONDS,
)
from autocut_core.schema.window import WINDOW_ANALYSIS_SCHEMA

# 共享的 helper 函数 (与旧 story_schemas.py 完全一致)
def obj(properties, *, required=None, additional=False):
    return {"type": "object", "properties": properties,
            "required": required if required is not None else list(properties),
            "additionalProperties": additional}
def arr(items, *, min_items=None, max_items=None):
    v = {"type": "array", "items": items}
    if min_items is not None: v["minItems"] = min_items
    if max_items is not None: v["maxItems"] = max_items
    return v

STR = {"type": "string"}
NONEMPTY = {"type": "string", "minLength": 1}
NUM = {"type": "number"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}
STRINGS = arr(STR)
NONEMPTY_STRINGS = arr(NONEMPTY, min_items=1)
EVENT_ID = {"type": "string", "pattern": r"^event-[0-9a-f]{12}$"}
EVENT_IDS = arr(EVENT_ID)
NONEMPTY_EVENT_IDS = arr(EVENT_ID, min_items=1)
CHAR_ID = {"type": "string", "pattern": r"^char-[a-z0-9-]{2,40}$"}
REL_ID = {"type": "string", "pattern": r"^rel-[a-z0-9-]{2,40}$"}
THREAD_ID = {"type": "string", "pattern": r"^thread-[a-z0-9-]{2,40}$"}
OQ_ID = {"type": "string", "pattern": r"^q-[a-z0-9-]{2,40}$"}
FACT_ID = {"type": "string", "pattern": r"^fact-[a-z0-9-]{2,40}$"}
CHAR_IDS = arr(CHAR_ID)
NONEMPTY_CHAR_IDS = arr(CHAR_ID, min_items=1)
NONEMPTY_THREAD_IDS = arr(THREAD_ID, min_items=1)
ENTITY_TYPE = {"type": "string", "enum": ["individual", "group", "creature", "unknown"]}
ALIAS = {"type": "string", "minLength": 2}
ALIASES = arr(ALIAS)
LANGUAGE = {"type": "string", "enum": ["zh", "en"]}
THREAD_KIND = {"type": "string", "enum": ["arc", "coda"]}
# ── Schema 定义 (从 story_schemas.py) ──

# Registry and episode schemas are sourced directly from Pydantic v2 modules
# inside SCHEMAS and build_series_assignment_schema() — no module-level re-exports.


def build_series_assignment_schema(
    *,
    chapter_id: str,
    episodes: list[int],
    thread_ids: list[str],
    event_ids: list[str],
    quarantined_event_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Return a per-chapter strict schema with all known identities narrowed."""

    schema = deepcopy(_reg["SERIES_ASSIGNMENT_SCHEMA"])
    properties = schema["properties"]
    ordered_episodes = [int(item) for item in episodes]
    properties["chapter_id"] = {"type": "string", "const": chapter_id}
    properties["episodes"] = {
        "type": "array",
        "items": {"type": "integer", "enum": sorted(set(ordered_episodes))},
        "minItems": len(ordered_episodes),
        "maxItems": len(ordered_episodes),
        "const": ordered_episodes,
    }

    beat_properties = properties["thread_beats"]["items"]["properties"]
    beat_properties["episode"] = {
        "type": "integer",
        "enum": sorted(set(ordered_episodes)),
    }
    if thread_ids:
        beat_properties["thread_id"] = {
            "type": "string",
            "enum": sorted(set(thread_ids)),
        }

    exclusion_properties = properties["excluded_episodes"]["items"]["properties"]
    exclusion_properties["episode"] = {
        "type": "integer",
        "enum": sorted(set(ordered_episodes)),
    }

    quarantined = set(quarantined_event_ids or [])
    known_event_ids = sorted(set(event_ids) - quarantined)
    if known_event_ids:
        event_item_schema = {"type": "string", "enum": known_event_ids}
        # Replace the complete array schemas instead of mutating ``items``:
        # base schema constants intentionally share STRINGS objects, and an
        # in-place edit would accidentally narrow requires_beat_ids as well.
        beat_properties["event_ids"] = {
            "type": "array",
            "items": deepcopy(event_item_schema),
            "minItems": 1,
        }
        exclusion_properties["event_ids"] = {
            "type": "array",
            "items": deepcopy(event_item_schema),
        }
    return schema



# Registry schemas sourced from Pydantic v2 module — used by SERIES_BIBLE_SCHEMA below.
from autocut_core.schema.registry import registry_dict_schemas as _reg_schemas
_reg = _reg_schemas()
BIBLE_METADATA_SCHEMA = _reg["BIBLE_METADATA_SCHEMA"]
SERIES_REGISTRY_SCHEMA = _reg["SERIES_REGISTRY_SCHEMA"]
BIBLE_ENTITY_IMPORTANCE_SCHEMA = _reg["BIBLE_ENTITY_IMPORTANCE_SCHEMA"]
SERIES_BIBLE_THREAD_SCHEMA = _reg["SERIES_BIBLE_THREAD_SCHEMA"]
THREAD_BEAT_SCHEMA = _reg["THREAD_BEAT_SCHEMA"]
EXCLUDED_EPISODE_SCHEMA = _reg["EXCLUDED_EPISODE_SCHEMA"]


SERIES_BIBLE_SCHEMA = obj(
    {
        # v1.4: propagate Registry thread_kind into the assembled Bible so
        # Broad compilation can enforce explicit coda semantics.
        # v1.3 (2026-07-29 stability audit):
        #   - `metadata` — audit trail written by the local assembler
        #   - `main_characters` — derived slice, ranked by objective score
        #     over `entity_type=individual`; never generated by the model
        #   - `entity_importance` — per-char score breakdown for diffing
        #   - id regex + entity_type + identity_evidence inherited from
        #     Registry v1.2
        "schema_version": {"type": "string", "const": "1.4"},
        "metadata": BIBLE_METADATA_SCHEMA,
        "series_summary": NONEMPTY,
        "characters": SERIES_REGISTRY_SCHEMA["properties"]["characters"],
        "main_characters": arr(CHAR_ID, max_items=12),
        "entity_importance": {
            "type": "object",
            "additionalProperties": BIBLE_ENTITY_IMPORTANCE_SCHEMA,
        },
        "relationships": SERIES_REGISTRY_SCHEMA["properties"]["relationships"],
        "facts": SERIES_REGISTRY_SCHEMA["properties"]["facts"],
        "story_threads": arr(SERIES_BIBLE_THREAD_SCHEMA, min_items=1),
        "thread_beats": arr(THREAD_BEAT_SCHEMA, min_items=1),
        "open_questions": SERIES_REGISTRY_SCHEMA["properties"]["open_questions"],
        "unresolved_identity_conflicts": SERIES_REGISTRY_SCHEMA["properties"][
            "unresolved_identity_conflicts"
        ],
        "coverage": obj(
            {
                "ingestion_coverage": obj(
                    {
                        "source_count": {"type": "integer", "minimum": 1},
                        "episode_count": {"type": "integer", "minimum": 1},
                        "window_count": {"type": "integer", "minimum": 1},
                        "episode_digest_count": {"type": "integer", "minimum": 1},
                        "missing_episode_ids": arr(INT),
                    }
                ),
                "narrative_coverage": obj(
                    {
                        "covered_episode_ids": arr(INT),
                        "unassigned_episode_ids": arr(INT),
                        "excluded_episodes": arr(EXCLUDED_EPISODE_SCHEMA),
                    }
                ),
            }
        ),
    }
)


STORY_SCORE_SCHEMA = obj(
    {
        "story_completeness": {"type": "integer", "minimum": 1, "maximum": 10},
        "independent_clarity": {"type": "integer", "minimum": 1, "maximum": 10},
        "highlight_relevance": {"type": "integer", "minimum": 1, "maximum": 10},
        "source_sufficiency": {"type": "integer", "minimum": 1, "maximum": 10},
        "causal_clarity": {"type": "integer", "minimum": 1, "maximum": 10},
        "hook_alignment": {"type": "integer", "minimum": 1, "maximum": 10},
        "background_cost": {"type": "integer", "minimum": 1, "maximum": 10},
    }
)


STORY_CATALOG_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.2"},
        "stories": arr(
            obj(
                {
                    "story_id": NONEMPTY,
                    "title": NONEMPTY,
                    "logline": NONEMPTY,
                    "central_question": NONEMPTY,
                    "character_ids": NONEMPTY_STRINGS,
                    "relationship_ids": STRINGS,
                    "story_thread_ids": NONEMPTY_STRINGS,
                    "source_thread_beat_ids": NONEMPTY_STRINGS,
                    "subarc_start_beat_id": NONEMPTY,
                    "subarc_end_beat_id": NONEMPTY,
                    "required_bridge_beat_ids": STRINGS,
                    "start_state": NONEMPTY,
                    "end_state": NONEMPTY,
                    "payoff_summary": NONEMPTY,
                    "open_hook_summary": STR,
                    "required_fact_ids": STRINGS,
                    "evidence_event_ids": NONEMPTY_STRINGS,
                    "suggested_highlight_candidate_ids": STRINGS,
                    "suggested_hook_candidate_ids": STRINGS,
                    "estimated_source_seconds": {"type": "number", "minimum": 0},
                    "duration_feasibility": {
                        "type": "string",
                        "enum": ["strong", "viable", "short", "insufficient"],
                    },
                    "overlap_notes": STR,
                    "scores": STORY_SCORE_SCHEMA,
                    "recommendation_reason": NONEMPTY,
                }
            ),
            min_items=1,
        ),
    }
)

STORY_PORTFOLIO_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.0"},
        "story_granularity": {"type": "string", "const": "broad"},
        # Coverage-first failure is a first-class outcome. Portfolio persists
        # a blocked payload instead of inviting downstream mutation of the
        # Catalog to bypass the Broad coverage contract.
        "status": {
            "type": "string",
            "enum": ["ready_for_scripts", "blocked"],
        },
        "primary_story_ids": STRINGS,
        "reserve_story_ids": STRINGS,
        "production_slots": arr(
            obj(
                {
                    "slot": {"type": "integer", "minimum": 1},
                    "story_id": NONEMPTY,
                }
            )
        ),
        "coverage_summary": obj(
            {
                "covered_story_thread_ids": STRINGS,
                "covered_character_ids": STRINGS,
                "covered_payoff_event_ids": STRINGS,
                "uncovered_major_thread_ids": STRINGS,
            }
        ),
        "pairwise_similarity_checks": arr(
            obj(
                {
                    "left_story_id": NONEMPTY,
                    "right_story_id": NONEMPTY,
                    "similarity_score": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "near_duplicate": BOOL,
                    "reasons": STRINGS,
                }
            )
        ),
        "insufficiency_reasons": STRINGS,
        # Optional (only populated when status=blocked)
        "blocked_reason": STR,
        # Coverage-blocked output names the major threads that remain
        # uncovered. Ready and blocked outputs both retain the utilization
        # report for review tooling.
        "problem_thread_ids": STRINGS,
        "thread_utilization": obj(
            {
                "diversity_ratio": NUM,
                "stories_per_thread": {
                    "type": "object",
                    "additionalProperties": INT,
                },
                "underutilized_thread_ids": STRINGS,
                "unused_thread_ids": STRINGS,
                "single_beat_terminal_thread_ids": STRINGS,
            },
            required=[
                "diversity_ratio",
                "stories_per_thread",
                "underutilized_thread_ids",
                "unused_thread_ids",
                "single_beat_terminal_thread_ids",
            ],
            additional=False,
        ),
        # Optional audit fingerprint — build_story_portfolio records the
        # sha256 of the story_catalog.json it consumed, so any downstream
        # inspection can detect catalog mutation between Portfolio and later
        # stages. See build_story_portfolio.main().
        "input_fingerprints": obj(
            {"story_catalog_sha256": STR},
            required=[],
            additional=True,
        ),
    },
    required=[
        "schema_version",
        "story_granularity",
        "status",
        "primary_story_ids",
        "reserve_story_ids",
        "production_slots",
        "coverage_summary",
        "pairwise_similarity_checks",
        "insufficiency_reasons",
    ],
)

STORY_PORTFOLIO_SCHEMA["properties"]["coverage_summary"]["properties"][
    "thread_beat_coverage"
] = obj(
    {
        "required_total": {"type": "integer", "minimum": 0},
        "required_covered": {"type": "integer", "minimum": 0},
        "required_coverage_ratio": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "non_coda_total": {"type": "integer", "minimum": 0},
        "non_coda_covered": {"type": "integer", "minimum": 0},
        "non_coda_coverage_ratio": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "all_total": {"type": "integer", "minimum": 0},
        "all_covered": {"type": "integer", "minimum": 0},
        "all_coverage_ratio": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "covered_thread_beat_ids": STRINGS,
        "uncovered_required_thread_beat_ids": STRINGS,
    }
)
STORY_PORTFOLIO_SCHEMA["properties"]["input_fingerprints"]["properties"][
    "subarc_option_catalog_sha256"
] = STR


STORY_TREATMENT_OPTION_SCHEMA = obj(
    {
        "treatment_option_id": NONEMPTY,
        "strategy": {
            "type": "string",
            "enum": [
                "chronological_compression",
                "cold_open_no_reprise",
                "cold_open_delayed_reprise",
            ],
        },
        "teaser_mode": {
            "type": "string",
            "enum": ["single_highlight", "none"],
        },
        "reprise_policy": {
            "type": "string",
            "enum": ["not_applicable", "forbidden", "delayed"],
        },
        "eligible_highlight_candidate_ids": STRINGS,
        "selection_basis": NONEMPTY,
        "constraints": obj(
            {
                "maximum_teaser_span_count": {
                    "type": "integer",
                    "const": 1,
                },
                "maximum_teaser_seconds": {
                    "type": "number",
                    "const": TEASER_MAXIMUM_SECONDS,
                },
                "explanation_beats_required": BOOL,
                "minimum_progression_beats_before_reprise": {
                    "type": "integer",
                    "minimum": 0,
                },
            }
        ),
    }
)


STORY_TREATMENT_RECORD_SCHEMA = obj(
    {
        "story_id": NONEMPTY,
        "portfolio_role": {
            "type": "string",
            "enum": ["primary", "reserve"],
        },
        "primary_story_thread_id": NONEMPTY,
        "thread_roles": arr(
            obj(
                {
                    "thread_id": NONEMPTY,
                    "role": {
                        "type": "string",
                        "enum": [
                            "primary",
                            "integrated_support",
                            "independent_secondary",
                        ],
                    },
                    "reason": NONEMPTY,
                }
            ),
            min_items=1,
        ),
        "recommended_treatment_option_id": NONEMPTY,
        "recommendation": obj(
            {
                "policy_version": {
                    "type": "string",
                    "const": "story-treatment-recommendation-v2",
                },
                "selected_strategy": {
                    "type": "string",
                    "enum": [
                        "chronological_compression",
                        "cold_open_no_reprise",
                        "cold_open_delayed_reprise",
                    ],
                },
                "reason_codes": arr(NONEMPTY, min_items=1),
                "physical_plan_feasibility": {
                    "type": "string",
                    "const": "pending_span_compilation",
                },
                "story_features": obj(
                    {
                        "is_linear_single_thread": BOOL,
                        "primary_thread_beat_count": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "eligible_highlight_candidate_ids": STRINGS,
                        "required_fact_reveal_candidate_ids": STRINGS,
                        "mandatory_body_replay_candidate_ids": STRINGS,
                        "no_reprise_safe_candidate_ids": STRINGS,
                        "delayed_reprise_ready_candidate_ids": STRINGS,
                        "delayed_reprise_payoff_candidate_ids": STRINGS,
                    }
                ),
            }
        ),
        "options": arr(STORY_TREATMENT_OPTION_SCHEMA, min_items=1),
    }
)


STORY_TREATMENT_OPTIONS_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.2"},
        "compiler_version": {
            "type": "string",
            "const": "story-treatment-compiler-v4-reserve-precompile",
        },
        "status": {"type": "string", "const": "ready_for_scripts"},
        "story_granularity": {
            "type": "string",
            "const": "broad",
        },
        "input_fingerprints": obj(
            {
                "story_catalog_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "story_portfolio_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "series_bible_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "candidate_catalog_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
            }
        ),
        "compiled_primary_story_ids": STRINGS,
        "compiled_reserve_story_ids": STRINGS,
        "stories": arr(STORY_TREATMENT_RECORD_SCHEMA, min_items=1),
    }
)


STORY_MUST_SHOW_SCHEMA = obj(
    {
        "id": NONEMPTY,
        "description": NONEMPTY,
        "observable_via": {
            "type": "string",
            "enum": [
                "visual",
                "dialogue",
                "action",
                "screen_text",
                "reaction",
                "mixed",
            ],
        },
        "evidence_event_ids": STRINGS,
        "evidence_fact_ids": STRINGS,
    }
)


STORY_RETRIEVAL_REQUIREMENTS_SCHEMA = obj(
    {
        "search_intent": NONEMPTY,
        "character_ids": STRINGS,
        "relationship_ids": STRINGS,
        "story_thread_ids": STRINGS,
        "thread_beat_ids": STRINGS,
        "fact_ids": STRINGS,
        "event_ids": STRINGS,
        "candidate_ids": STRINGS,
        "continuity": {
            "type": "string",
            "enum": ["continuous_scene", "causal_chain", "montage_allowed"],
        },
        "lookback": {
            "type": "string",
            "enum": ["same_episode", "earlier_episodes", "whole_series"],
        },
    }
)


STORY_SCRIPT_DRAFT_BEAT_SCHEMA = obj(
    {
        "id": NONEMPTY,
        "role": {
            "type": "string",
            "enum": [
                "teaser_intent",
                "orientation",
                "setup",
                "escalation",
                "turn_or_reveal",
                "payoff",
                "end_hook",
            ],
        },
        "dramatic_purpose": NONEMPTY,
        "narrative_description": NONEMPTY,
        "concrete_story_content": NONEMPTY,
        "must_show": arr(STORY_MUST_SHOW_SCHEMA, min_items=1),
        "must_not_reveal_fact_ids": STRINGS,
        "required_before_fact_ids": STRINGS,
        "introduced_fact_ids": STRINGS,
        "resolved_question_ids": STRINGS,
        "viewer_state_before": NONEMPTY_STRINGS,
        "viewer_state_after": NONEMPTY_STRINGS,
        "emotional_change": obj({"from": STR, "to": STR}),
        "causal_role": {
            "type": "string",
            "enum": [
                "context",
                "cause",
                "escalation",
                "reveal",
                "payoff",
                "consequence",
                "hook",
            ],
        },
        "event_ids": STRINGS,
        "candidate_suggestions": STRINGS,
        "retrieval_requirements": STORY_RETRIEVAL_REQUIREMENTS_SCHEMA,
        "temporal_position": {
            "type": "string",
            "enum": ["mainline", "earlier_context", "future_preview", "parallel"],
        },
        "thread_role": {
            "type": "string",
            "enum": [
                "primary",
                "integrated_support",
                "independent_secondary",
            ],
        },
        "must_have": BOOL,
    }
)


STORY_SCRIPT_BEAT_SCHEMA = obj(
    {
        **STORY_SCRIPT_DRAFT_BEAT_SCHEMA["properties"],
        "estimated_source_duration_seconds": obj(
            {
                "minimum": {"type": "number", "minimum": 0},
                "maximum": {"type": "number", "minimum": 0},
            }
        ),
        "evidence_status": {
            "type": "string",
            "enum": [
                "covered",
                "partial",
                "missing",
                "conflicting",
                "needs_video_review",
            ],
        },
        "material_risks": STRINGS,
        "physical_evidence": obj(
            {
                "physical_ranges": arr(
                    obj(
                        {
                            "source_id": NONEMPTY,
                            "start": {"type": "number", "minimum": 0},
                            "end": {"type": "number", "minimum": 0},
                        }
                    )
                ),
                "source_count": {"type": "integer", "minimum": 0},
                "atomic_event_count": {"type": "integer", "minimum": 0},
                "physical_union_duration_seconds": {
                    "type": "number",
                    "minimum": 0,
                },
                "physical_envelope_duration_seconds": {
                    "type": "number",
                    "minimum": 0,
                },
                "internal_gap_seconds": {"type": "number", "minimum": 0},
                "timeline_segment_count": {"type": "integer", "minimum": 0},
                "compaction_status": {
                    "type": "string",
                    "enum": [
                        "atomic",
                        "split_regeneration_required",
                        "continuity_required",
                        "continuity_fallback",
                    ],
                },
            }
        ),
        "continuity_required": BOOL,
    }
)

TEASER_CONTRACT_SCHEMA = obj(
    {
        # teaser 变可选。mode=none 时其余字段仍在 schema 上
        # 但保留占位值（primary_highlight_candidate_id 可为空字符串、时长与
        # 计数字段仍是 const；本地代码在 mode=none 分支跳过 teaser 相关检查
        # 与 legal_teaser_options 编译）。
        "mode": {"type": "string", "enum": ["single_highlight", "none"]},
        "primary_highlight_candidate_id": STR,
        "maximum_span_count": {"type": "integer", "const": 1},
        # teaser 时长收紧到"抖音爆款尺寸"。
        # preferred 上限 20 → 15，硬顶 30 → 15。
        "preferred_minimum_seconds": {
            "type": "number",
            "const": TEASER_PREFERRED_MINIMUM_SECONDS,
        },
        "preferred_maximum_seconds": {
            "type": "number",
            "const": TEASER_MAXIMUM_SECONDS,
        },
        "maximum_seconds": {
            "type": "number",
            "const": TEASER_MAXIMUM_SECONDS,
        },
        "maximum_reaction_tail_seconds": {"type": "number", "const": 2},
        "treatment_option_id": NONEMPTY,
        "strategy": {
            "type": "string",
            "enum": [
                "chronological_compression",
                "cold_open_no_reprise",
                "cold_open_delayed_reprise",
            ],
        },
        "reprise_policy": {
            "type": "string",
            "enum": ["not_applicable", "forbidden", "delayed"],
        },
        "selection_reason": NONEMPTY,
        "explanation_beat_ids": STRINGS,
        "reprise_beat_ids": STRINGS,
        "reprise_delay_minimum_progression_beats": {
            "type": "integer",
            "minimum": 0,
        },
        "reprise_function": {
            "type": "string",
            "enum": [
                "not_applicable",
                "new_causal_context",
                "relationship_reinterpretation",
                "consequence_recontextualization",
                "suspense_recovery",
            ],
        },
    }
)


STORY_SCRIPT_DRAFT_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.6"},
        "story_id": NONEMPTY,
        "title": NONEMPTY,
        "logline": NONEMPTY,
        "story_promise": NONEMPTY,
        "central_question": NONEMPTY,
        "character_ids": NONEMPTY_STRINGS,
        "relationship_ids": STRINGS,
        "story_thread_ids": NONEMPTY_STRINGS,
        "primary_story_thread_id": NONEMPTY,
        "treatment_options_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "selected_thread_beat_ids": NONEMPTY_STRINGS,
        "required_thread_beat_ids": NONEMPTY_STRINGS,
        "omitted_thread_beats": arr(
            obj(
                {
                    "thread_beat_id": NONEMPTY,
                    "reason": {
                        "type": "string",
                        "enum": [
                            "out_of_scope",
                            "redundant_support",
                            "duration_limit",
                            "evidence_insufficient",
                        ],
                    },
                    "explanation": NONEMPTY,
                }
            )
        ),
        "portfolio": obj(
            {
                "portfolio_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "role": {"type": "string", "const": "primary"},
                "production_slot": {"type": "integer", "minimum": 1},
            }
        ),
        "start_state": NONEMPTY,
        "end_state": NONEMPTY,
        "local_payoff": NONEMPTY,
        "teaser_contract": TEASER_CONTRACT_SCHEMA,
        "target_duration": obj(
            {
                "minimum_seconds": {"type": "number", "const": 0},
                "preferred_minimum_seconds": {"type": "number", "const": 0},
                "preferred_target_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1200,
                },
                "maximum_seconds": {"type": "number", "const": 1200},
                "soft_target_seconds": {"type": "number", "const": 0},
            },
            required=[
                "minimum_seconds",
                "preferred_minimum_seconds",
                "preferred_target_seconds",
                "maximum_seconds",
            ],
        ),
        "required_fact_ids": STRINGS,
        "intentional_mystery_fact_ids": STRINGS,
        # Broad Script permits 4-14 Editorial Beats.
        "beats": arr(STORY_SCRIPT_DRAFT_BEAT_SCHEMA, min_items=4, max_items=14),
        "ending_hook_intent": obj(
            {
                "question": STR,
                "story_thread_ids": STRINGS,
                "event_ids": STRINGS,
                "candidate_ids": STRINGS,
                "may_be_empty": BOOL,
            }
        ),
        "evidence_event_ids": NONEMPTY_STRINGS,
        "status": {"type": "string", "const": "draft"},
    }
)


STORY_FEASIBILITY_SCHEMA = obj(
    {
        "status": {
            "type": "string",
            "enum": [
                "feasible",
                "partial",
                "not_feasible",
            ],
        },
        "method": {
            "type": "string",
            "const": "functional-evidence-duration-v4-direct-atomic-compaction",
        },
        "assumptions": obj(
            {
                "context_padding_seconds": {"type": "number", "minimum": 0},
                "usable_ratio": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "context_entity_expansion_counts_toward_duration": {
                    "type": "boolean",
                    "const": False,
                },
            }
        ),
        "estimated_source_duration_min_seconds": {"type": "number", "minimum": 0},
        "estimated_source_duration_max_seconds": {"type": "number", "minimum": 0},
        "covered_beat_ids": STRINGS,
        "partial_beat_ids": STRINGS,
        "missing_beat_ids": STRINGS,
        "conflicting_beat_ids": STRINGS,
        "needs_video_review_beat_ids": STRINGS,
        "review_event_ids": STRINGS,
        "highlight_candidate_ids": STRINGS,
        "hook_candidate_ids": STRINGS,
        "material_risks": STRINGS,
        # 4.18.1 task-local hotfix: preflight_script writes regeneration
        # diagnostics here, so the finalized Story Script schema must accept
        # the field. It remains optional for scripts without semantic gaps.
        "failure_codes": STRINGS,
        "split_regeneration_beat_ids": STRINGS,
        "continuity_required_beat_ids": STRINGS,
        "continuity_fallback_beat_ids": STRINGS,
        "treatment_viability": obj(
            {
                "selected_treatment_option_id": NONEMPTY,
                "status": {
                    "type": "string",
                    "enum": ["feasible", "infeasible"],
                },
                "failure_codes": STRINGS,
                "alternate_treatment_option_ids": STRINGS,
                "alternate_treatments": arr(
                    obj(
                        {
                            "treatment_option_id": NONEMPTY,
                            "strategy": {
                                "type": "string",
                                "enum": [
                                    "chronological_compression",
                                    "cold_open_no_reprise",
                                    "cold_open_delayed_reprise",
                                ],
                            },
                        }
                    )
                ),
                "recommended_alternate_treatment_option_id": STR,
                "chronological_fallback_treatment_option_id": STR,
                "repair_route": {
                    "type": "string",
                    "const": "story_script",
                },
            }
        ),
        "teaser_diagnostics": obj(
            {
                # mode=none 时 teaser_diagnostics 里主要字段
                # 保持 shape，具体值为占位（primary_highlight_candidate_id
                # 空串，duration 0）。
                "mode": {"type": "string", "enum": ["single_highlight", "none"]},
                "treatment_option_id": STR,
                "strategy": {
                    "type": "string",
                    "enum": [
                        "chronological_compression",
                        "cold_open_no_reprise",
                        "cold_open_delayed_reprise",
                    ],
                },
                "reprise_policy": {
                    "type": "string",
                    "enum": ["not_applicable", "forbidden", "delayed"],
                },
                "primary_highlight_candidate_id": STR,
                "source_id": STR,
                "candidate_duration_seconds": {"type": "number", "minimum": 0},
                "physical_obligation_duration_seconds": {
                    "type": "number",
                    "minimum": 0,
                },
                "mandatory_reprise_event_ids": STRINGS,
                "maximum_repeat_seconds": {
                    "type": "number",
                    # Plan 3: 20s 绝对上限 → 60s DFS 剪枝上界（最终由
                    # ratio 10% 硬合同裁决，见 materialize_story_plans）
                    "const": 60,
                },
                "repeat_contract_status": {
                    "type": "string",
                    "enum": ["feasible", "revision_required"],
                },
                "must_show_ids": STRINGS,
                "outside_candidate_must_show_ids": STRINGS,
                "status": {
                    "type": "string",
                    "enum": ["feasible", "revision_required"],
                },
                "failure_codes": STRINGS,
                "repair_route": {
                    "type": "string",
                    "enum": ["story_script", "span_compiler"],
                },
            }
        ),
    },
    required=[
        "status",
        "method",
        "assumptions",
        "estimated_source_duration_min_seconds",
        "estimated_source_duration_max_seconds",
        "covered_beat_ids",
        "partial_beat_ids",
        "missing_beat_ids",
        "conflicting_beat_ids",
        "needs_video_review_beat_ids",
        "review_event_ids",
        "highlight_candidate_ids",
        "hook_candidate_ids",
        "material_risks",
        "treatment_viability",
        "teaser_diagnostics",
    ],
)


STORY_SCRIPT_SCHEMA = obj(
    {
        **{
            key: value
            for key, value in STORY_SCRIPT_DRAFT_SCHEMA["properties"].items()
            if key not in {"beats", "status"}
        },
        "beats": arr(STORY_SCRIPT_BEAT_SCHEMA, min_items=4, max_items=14),
        "feasibility": STORY_FEASIBILITY_SCHEMA,
        "status": {"type": "string", "const": "awaiting_approval"},
    }
)


# Reserve replenishment is optional for original Primary Scripts and required
# only by the per-Story dynamic schema of a promoted Reserve.  Keep these
# properties outside the shared required list so old Primary records remain
# structurally valid while promoted records can carry an immutable audit
# fingerprint.
STORY_PROMOTION_PORTFOLIO_PROPERTIES = {
    "promotion_id": NONEMPTY,
    "promotion_fingerprint": {
        "type": "string",
        "minLength": 64,
        "maxLength": 64,
    },
    "replaces_story_id": NONEMPTY,
    "root_primary_story_id": NONEMPTY,
}
STORY_SCRIPT_DRAFT_SCHEMA["properties"]["portfolio"]["properties"].update(
    STORY_PROMOTION_PORTFOLIO_PROPERTIES
)
STORY_SCRIPT_SCHEMA["properties"]["portfolio"]["properties"].update(
    STORY_PROMOTION_PORTFOLIO_PROPERTIES
)


# Optional audit trail added by expand_story_scope.py --apply. Not part of
# the model-generated draft; downstream schema validation must accept it
# without adding it to the required-field list.
AUTO_SCOPE_EXPANSION_ENTRY_SCHEMA = obj(
    {
        "added_thread_beat_ids": STRINGS,
        "attached_thread_beat_ids": STRINGS,
        "migrated_from_beat_ids": STRINGS,
        "target_beat_id": STR,
        "target_lookback_widened_from": {
            "type": "string",
            "enum": [
                "same_episode",
                "earlier_episodes",
                "whole_series",
            ],
        },
        "target_lookback_widened_to": {
            "type": "string",
            "enum": ["whole_series", "earlier_episodes"],
        },
        "trigger_source": {
            "type": "string",
            "enum": ["plan_preflight", "script_preflight"],
        },
    },
    required=["added_thread_beat_ids"],
)
_AUTO_SCOPE_EXPANSION_PROP = arr(AUTO_SCOPE_EXPANSION_ENTRY_SCHEMA)
STORY_SCRIPT_DRAFT_SCHEMA["properties"][
    "auto_scope_expansion"
] = _AUTO_SCOPE_EXPANSION_PROP
STORY_SCRIPT_SCHEMA["properties"][
    "auto_scope_expansion"
] = _AUTO_SCOPE_EXPANSION_PROP
# P2: optional audit trail written by ``repair_story_script_draft``. Not part
# of the model-generated draft; downstream schema validation must accept it
# without adding it to the required-field list.
_AUTO_REPAIRS_PROP = arr(NONEMPTY)
STORY_SCRIPT_DRAFT_SCHEMA["properties"]["auto_repairs"] = _AUTO_REPAIRS_PROP
STORY_SCRIPT_SCHEMA["properties"]["auto_repairs"] = _AUTO_REPAIRS_PROP

# P0-1 followup fix: `repair_story_script_draft` writes structured
# findings for class-B semantic gaps to `auto_detected_semantic_gaps`. Same
# treatment as `auto_repairs` — declare the field on both schemas so
# `additionalProperties=False` (default of `obj()`) does not reject the
# draft the moment the P2 pass records a finding. The initial P0-1 landing
# missed this and any post-repair `validate_task_response("story_script_draft",
# ...)` failed with an unknown-property error, breaking the assembler.
_AUTO_DETECTED_SEMANTIC_GAPS_PROP = arr(
    obj(
        {"code": NONEMPTY, "detail": NONEMPTY},
        required=["code", "detail"],
        additional=True,
    ),
)
STORY_SCRIPT_DRAFT_SCHEMA["properties"][
    "auto_detected_semantic_gaps"
] = _AUTO_DETECTED_SEMANTIC_GAPS_PROP
STORY_SCRIPT_SCHEMA["properties"][
    "auto_detected_semantic_gaps"
] = _AUTO_DETECTED_SEMANTIC_GAPS_PROP


EVENT_CARD_SCHEMA = obj(
    {
        "id": NONEMPTY,
        "episode": {"type": "integer", "minimum": 1},
        "source_id": NONEMPTY,
        "source_ranges": arr(
            obj(
                {
                    "start": {"type": "number", "minimum": 0},
                    "end": {"type": "number", "minimum": 0},
                    "evidence_window_ids": NONEMPTY_STRINGS,
                }
            ),
            min_items=1,
        ),
        "summary": NONEMPTY,
        "function": NONEMPTY,
        "character_names": STRINGS,
        "cause": STR,
        "effect": STR,
        "open_question": STR,
        "temporal_mode": NONEMPTY,
        "candidate_ids": STRINGS,
        "boundary_resolution": obj(
            {
                "status": {
                    "type": "string",
                    "enum": ["consensus", "single_observation"],
                },
                "member_ranges": arr(
                    obj(
                        {
                            "start": {"type": "number", "minimum": 0},
                            "end": {"type": "number", "minimum": 0},
                        }
                    ),
                    min_items=1,
                ),
            }
        ),
    }
)


HIGHLIGHT_HOOK_CANDIDATE_SCHEMA = obj(
    {
        "id": NONEMPTY,
        "original_id": STR,
        "source_id": NONEMPTY,
        "episode": {"type": "integer", "minimum": 1},
        "start": {"type": "number", "minimum": 0},
        "end": {"type": "number", "minimum": 0},
        "type": {"type": "string", "enum": ["highlight", "hook"]},
        "strength": {"type": "integer", "minimum": 1, "maximum": 10},
        "reason": STR,
        "anchor": STR,
        "lead_in": STR,
        "payoff_or_open_question": STR,
        "dialogue_excerpt": STR,
        "event_ids": STRINGS,
        "evidence_window_ids": NONEMPTY_STRINGS,
    }
)


STORY_EVIDENCE_RANGE_REF_SCHEMA = obj(
    {
        "source_id": NONEMPTY,
        "episode": {"type": "integer", "minimum": 1},
        "start": {"type": "number", "minimum": 0},
        "end": {"type": "number", "minimum": 0},
        "origin": {"type": "string", "enum": ["event", "candidate"]},
        "origin_id": NONEMPTY,
        "evidence_window_ids": STRINGS,
    }
)


STORY_EVIDENCE_REQUESTED_IDS_SCHEMA = obj(
    {
        "character_ids": STRINGS,
        "relationship_ids": STRINGS,
        "story_thread_ids": STRINGS,
        "thread_beat_ids": STRINGS,
        "fact_ids": STRINGS,
        "event_ids": STRINGS,
        "candidate_ids": STRINGS,
    }
)


STORY_EVIDENCE_MUST_SHOW_SCHEMA = obj(
    {
        "must_show_id": NONEMPTY,
        "description": NONEMPTY,
        "observable_via": {
            "type": "string",
            "enum": [
                "visual",
                "dialogue",
                "action",
                "screen_text",
                "reaction",
                "mixed",
            ],
        },
        "requested_event_ids": STRINGS,
        "requested_fact_ids": STRINGS,
        "direct_event_ids": STRINGS,
        "fact_context_event_ids": STRINGS,
        "resolved_event_ids": STRINGS,
        "status": {"type": "string", "enum": ["covered", "missing"]},
    }
)


STORY_BEAT_EVIDENCE_SCHEMA = obj(
    {
        "beat_id": NONEMPTY,
        "role": NONEMPTY,
        "must_have": BOOL,
        "temporal_position": NONEMPTY,
        "search_intent": NONEMPTY,
        "continuity": {
            "type": "string",
            "enum": ["continuous_scene", "causal_chain", "montage_allowed"],
        },
        "lookback": {
            "type": "string",
            "enum": ["same_episode", "earlier_episodes", "whole_series"],
        },
        "requested_ids": STORY_EVIDENCE_REQUESTED_IDS_SCHEMA,
        "resolved_thread_beat_ids": STRINGS,
        "must_show_evidence": arr(STORY_EVIDENCE_MUST_SHOW_SCHEMA, min_items=1),
        "direct_event_ids": STRINGS,
        "fact_context_event_ids": STRINGS,
        "expanded_event_ids": STRINGS,
        "candidate_ids": STRINGS,
        "evidence_window_ids": STRINGS,
        "context_window_ids": STRINGS,
        "source_ids": STRINGS,
        "direct_range_refs": arr(STORY_EVIDENCE_RANGE_REF_SCHEMA),
        "candidate_range_refs": arr(STORY_EVIDENCE_RANGE_REF_SCHEMA),
        "context_range_refs": arr(STORY_EVIDENCE_RANGE_REF_SCHEMA),
        "range_refs": arr(STORY_EVIDENCE_RANGE_REF_SCHEMA),
        "script_evidence_status": {
            "type": "string",
            "enum": [
                "covered",
                "partial",
                "missing",
                "conflicting",
                "needs_video_review",
            ],
        },
        "retrieval_status": {
            "type": "string",
            "enum": ["covered", "partial", "missing", "needs_video_review"],
        },
        "missing_requirements": STRINGS,
        "material_risks": STRINGS,
    }
)


STORY_EVIDENCE_SOURCE_SCHEMA = obj(
    {
        "id": NONEMPTY,
        "episode": {"type": "integer", "minimum": 1},
        "duration_seconds": {"type": "number", "minimum": 0},
        "locator_type": {
            "type": "string",
            "enum": ["local_path", "remote_url", "unavailable"],
        },
        "locator": STR,
    }
)


STORY_EVIDENCE_PACKET_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.2"},
        "method": {"type": "string", "const": "structured-thread-beat-recall-v4"},
        "story_id": NONEMPTY,
        "title": NONEMPTY,
        "production_slot": {"type": "integer", "minimum": 1},
        "teaser_contract": TEASER_CONTRACT_SCHEMA,
        "status": {
            "type": "string",
            "enum": ["ready", "needs_video_review", "incomplete"],
        },
        "approval_binding": obj(
            {
                "story_script_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "portfolio_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "decided_at": NONEMPTY,
                "accepted_material_risks": BOOL,
                "reviewer_notes": STR,
            }
        ),
        "input_fingerprints": obj(
            {
                "story_approval_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "series_bible_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "event_cards_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "candidate_catalog_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "source_manifest_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "window_manifest_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "window_summaries_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
            }
        ),
        "retrieval_policy": obj(
            {
                "adjacent_window_hops": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 3,
                },
                "semantic_search_used": {"type": "boolean", "const": False},
                "vector_search_used": {"type": "boolean", "const": False},
            }
        ),
        "coverage_summary": obj(
            {
                "beat_count": {"type": "integer", "minimum": 1},
                "covered_beat_ids": STRINGS,
                "partial_beat_ids": STRINGS,
                "missing_beat_ids": STRINGS,
                "needs_video_review_beat_ids": STRINGS,
                "must_have_missing_beat_ids": STRINGS,
                "required_thread_beat_ids": STRINGS,
                "covered_thread_beat_ids": STRINGS,
                "missing_required_thread_beat_ids": STRINGS,
                "source_count": {"type": "integer", "minimum": 0},
                "range_count": {"type": "integer", "minimum": 0},
                "unique_evidence_duration_seconds": {
                    "type": "number",
                    "minimum": 0,
                },
            }
        ),
        "beat_evidence": arr(STORY_BEAT_EVIDENCE_SCHEMA, min_items=1),
        "evidence_catalog": obj(
            {
                "sources": arr(STORY_EVIDENCE_SOURCE_SCHEMA),
                "windows": arr(WINDOW_ANALYSIS_SCHEMA),
                "events": arr(EVENT_CARD_SCHEMA),
                "candidates": arr(HIGHLIGHT_HOOK_CANDIDATE_SCHEMA),
                "characters": arr(
                    SERIES_BIBLE_SCHEMA["properties"]["characters"]["items"]
                ),
                "relationships": arr(
                    SERIES_BIBLE_SCHEMA["properties"]["relationships"]["items"]
                ),
                "facts": arr(SERIES_BIBLE_SCHEMA["properties"]["facts"]["items"]),
                "story_threads": arr(
                    SERIES_BIBLE_SCHEMA["properties"]["story_threads"]["items"]
                ),
                "thread_beats": arr(THREAD_BEAT_SCHEMA),
                "open_questions": arr(
                    SERIES_BIBLE_SCHEMA["properties"]["open_questions"]["items"]
                ),
            }
        ),
    }
)


STORY_EVIDENCE_INDEX_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.1"},
        "method": {"type": "string", "const": "structured-thread-beat-recall-v4"},
        "status": {
            "type": "string",
            "enum": [
                "ready",
                "needs_video_review",
                "partially_ready",
                "incomplete",
            ],
        },
        "story_approval_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "portfolio_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "selected_story_count": {"type": "integer", "minimum": 1},
        "packets": arr(
            obj(
                {
                    "story_id": NONEMPTY,
                    "title": NONEMPTY,
                    "production_slot": {"type": "integer", "minimum": 1},
                    "status": {
                        "type": "string",
                        "enum": ["ready", "needs_video_review", "incomplete"],
                    },
                    "path": NONEMPTY,
                    "packet_sha256": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                    },
                    "story_script_sha256": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                    },
                }
            ),
            min_items=1,
        ),
    }
)


SPAN_ANCHOR_REF_SCHEMA = obj(
    {
        "origin": {"type": "string", "enum": ["event", "candidate"]},
        "origin_id": NONEMPTY,
        "start": {"type": "number", "minimum": 0},
        "end": {"type": "number", "minimum": 0},
        "evidence_window_ids": STRINGS,
    }
)


SPAN_SEMANTIC_SEGMENT_REF_SCHEMA = obj(
    {
        "segment_id": NONEMPTY,
        "source_id": NONEMPTY,
        "window_ids": NONEMPTY_STRINGS,
        "kind": {
            "type": "string",
            "enum": [
                "story_beat",
                "dialogue",
                "screen_text",
                "visual_event",
            ],
        },
        "start": {"type": "number", "minimum": 0},
        "end": {"type": "number", "minimum": 0},
        "content_summary": NONEMPTY,
    }
)


SPAN_BOUNDARY_EVIDENCE_SCHEMA = obj(
    {
        "start_basis": {
            "type": "string",
            "enum": [
                "source_boundary",
                "dialogue_boundary",
                "screen_text_boundary",
                "story_beat_boundary",
                "visual_event_boundary",
                "anchor_padding",
                "window_limit",
            ],
        },
        "end_basis": {
            "type": "string",
            "enum": [
                "source_boundary",
                "dialogue_boundary",
                "screen_text_boundary",
                "story_beat_boundary",
                "visual_event_boundary",
                "anchor_padding",
                "window_limit",
            ],
        },
        "starts_mid_sentence_risk": BOOL,
        "ends_mid_sentence_risk": BOOL,
        "starts_mid_scene_risk": BOOL,
        "ends_mid_scene_risk": BOOL,
        "review_reasons": STRINGS,
    }
)


SPAN_CANDIDATE_SCHEMA = obj(
    {
        "span_candidate_id": NONEMPTY,
        "source_id": NONEMPTY,
        "episode": {"type": "integer", "minimum": 1},
        "start": {"type": "number", "minimum": 0},
        "end": {"type": "number", "minimum": 0},
        "duration_seconds": {"type": "number", "minimum": 0},
        "source_duration_seconds": {"type": "number", "minimum": 0},
        "source_coverage_ratio": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "covered_timeline_segment_count": {
            "type": "integer",
            "minimum": 0,
        },
        "full_source_like": BOOL,
        "continuity_closure": BOOL,
        "continuity_closure_member_span_ids": STRINGS,
        "teaser_atomic": BOOL,
        "teaser_atomic_owner_candidate_id": STR,
        "teaser_atomic_stitched": BOOL,
        "teaser_atomic_stitched_from": STRINGS,
        "teaser_atomic_stitched_gap_seconds": {
            "type": "number",
            "minimum": 0,
        },
        "variant_types": arr(
            {
                "type": "string",
                "enum": ["tight", "scene", "context"],
            },
            min_items=1,
        ),
        "provenance_tiers": arr(
            {
                "type": "string",
                "enum": ["direct", "candidate", "context"],
            },
            min_items=1,
        ),
        # Context-only candidates are valid editing handles, but they are not
        # functional evidence and therefore intentionally carry no Beat
        # support.  Legal Option compilation only consumes non-empty support.
        "supports_beat_ids": STRINGS,
        "supports_thread_beat_ids": STRINGS,
        "supports_must_show_ids": STRINGS,
        "content_roles": NONEMPTY_STRINGS,
        "temporal_positions": NONEMPTY_STRINGS,
        "continuity_modes": NONEMPTY_STRINGS,
        "event_ids": STRINGS,
        "candidate_ids": STRINGS,
        "anchor_refs": arr(SPAN_ANCHOR_REF_SCHEMA, min_items=1),
        "semantic_segment_refs": arr(SPAN_SEMANTIC_SEGMENT_REF_SCHEMA),
        "boundary_status": {
            "type": "string",
            "enum": ["proposed", "needs_video_review", "verified"],
        },
        "boundary_evidence": SPAN_BOUNDARY_EVIDENCE_SCHEMA,
        "material_risks": STRINGS,
    }
)


SPAN_BEAT_COVERAGE_SCHEMA = obj(
    {
        "beat_id": NONEMPTY,
        "must_have": BOOL,
        "candidate_ids": STRINGS,
        "status": {
            "type": "string",
            "enum": ["covered", "needs_video_review", "missing"],
        },
    }
)


SPAN_CANDIDATE_BUNDLE_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.4"},
        "method": {
            "type": "string",
            "const": "semantic-window-boundary-v7-dialogue-boundary",
        },
        "story_id": NONEMPTY,
        "title": NONEMPTY,
        "production_slot": {"type": "integer", "minimum": 1},
        "status": {
            "type": "string",
            "enum": ["ready", "needs_video_review", "incomplete"],
        },
        "input_fingerprints": obj(
            {
                "story_evidence_index_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "story_evidence_packet_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "story_script_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
            }
        ),
        "compiler_policy": obj(
            {
                "anchor_merge_gap_seconds": {
                    "type": "number",
                    "minimum": 0,
                },
                "tight_padding_seconds": {
                    "type": "number",
                    "minimum": 0,
                },
                "scene_padding_seconds": {
                    "type": "number",
                    "minimum": 0,
                },
                "reaction_tail_seconds": {
                    "type": "number",
                    "minimum": 0,
                },
                "maximum_context_extension_seconds": {
                    "type": "number",
                    "minimum": 0,
                },
                "maximum_span_seconds": {
                    "type": "number",
                    "minimum": 1,
                },
                "full_source_like_threshold": {
                    "type": "number",
                    "const": 0.85,
                },
                "short_source_duration_seconds": {
                    "type": "number",
                    "const": 180.0,
                },
                "dense_short_source_min_semantic_ratio": {
                    "type": "number",
                    "const": 0.75,
                },
                "highlight_atomic_tight_enabled": {
                    "type": "boolean",
                    "const": True,
                },
                "direct_event_atomic_tight_enabled": {
                    "type": "boolean",
                    "const": True,
                },
                "short_source_exemption_requires_single_timeline_segment": {
                    "type": "boolean",
                    "const": True,
                },
                "semantic_boundary_completion_enabled": {
                    "type": "boolean",
                    "const": True,
                },
                "continuity_closure_enabled": {
                    "type": "boolean",
                    "const": True,
                },
                "maximum_safe_same_source_gap_seconds": {
                    "type": "number",
                    "const": 12.0,
                },
                "emits_verified_boundaries": {"type": "boolean", "const": False},
            }
        ),
        "beat_coverage": arr(SPAN_BEAT_COVERAGE_SCHEMA, min_items=1),
        "candidates": arr(SPAN_CANDIDATE_SCHEMA),
    }
)


SPAN_CANDIDATE_INDEX_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.4"},
        "method": {
            "type": "string",
            "const": "semantic-window-boundary-v7-dialogue-boundary",
        },
        "status": {
            "type": "string",
            "enum": [
                "ready",
                "needs_video_review",
                "partially_ready",
                "incomplete",
            ],
        },
        "story_evidence_index_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "story_count": {"type": "integer", "minimum": 1},
        "candidate_reference_count": {"type": "integer", "minimum": 0},
        "unique_span_candidate_count": {"type": "integer", "minimum": 0},
        "bundles": arr(
            obj(
                {
                    "story_id": NONEMPTY,
                    "title": NONEMPTY,
                    "production_slot": {"type": "integer", "minimum": 1},
                    "status": {
                        "type": "string",
                        "enum": ["ready", "needs_video_review", "incomplete"],
                    },
                    "path": NONEMPTY,
                    "bundle_sha256": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                    },
                    "story_evidence_packet_sha256": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                    },
                    "candidate_count": {"type": "integer", "minimum": 0},
                }
            ),
            min_items=1,
        ),
    }
)


STORY_PLAN_SELECTION_SPAN_SCHEMA = obj(
    {
        "span_candidate_id": NONEMPTY,
        "reuse_mode": {
            "type": "string",
            "enum": ["none", "teaser_reprise"],
        },
        "reprise_adds_information": STR,
    }
)


STORY_PLAN_SELECTION_BLOCK_SCHEMA = obj(
    {
        "play_order": {"type": "integer", "minimum": 1},
        "role": {
            "type": "string",
            "enum": [
                "teaser",
                "orientation",
                "setup",
                "escalation",
                "turn_or_reveal",
                "payoff",
                "end_hook",
            ],
        },
        "beat_ids": NONEMPTY_STRINGS,
        "span_selections": arr(STORY_PLAN_SELECTION_SPAN_SCHEMA, min_items=1),
        "temporal_relation_from_previous": {
            "type": "string",
            "enum": [
                "start",
                "continuation",
                "flashback_context",
                "preview_future",
                "return_to_mainline",
                "parallel",
            ],
        },
        "orientation_required": BOOL,
        "orientation_strategy": {
            "type": "string",
            "enum": [
                "dialogue_anchor",
                "visual_anchor",
                "title_card",
                "none",
            ],
        },
        "selection_reason": NONEMPTY,
    }
)


STORY_PLAN_SELECTION_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "4.0"},
        "story_id": NONEMPTY,
        "production_slot": {"type": "integer", "minimum": 1},
        "finalist": obj(
            {
                "teaser": obj(
                    {
                        # mode=none 时 option_id 是空字符串；动态 Schema
                        # 会把它与所选 Body finalist 一起锁定。
                        "option_id": STR,
                        "selection_reason": NONEMPTY,
                    }
                ),
                "body_partition_id": NONEMPTY,
                "body_block_orientations": arr(
                    obj(
                        {
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
                            "orientation_required": BOOL,
                            "orientation_strategy": {
                                "type": "string",
                                "enum": [
                                    "dialogue_anchor",
                                    "visual_anchor",
                                    "title_card",
                                    "none",
                                ],
                            },
                            "selection_reason": NONEMPTY,
                        }
                    ),
                    min_items=1,
                ),
            }
        ),
        "planning_risks": STRINGS,
    }
)


STORY_PLAN_ORIENTATION_FALLBACK_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.0"},
        "story_id": NONEMPTY,
        "production_slot": {"type": "integer", "minimum": 1},
        # Candidate IDs and exact orientation lengths are locked by the
        # per-Story dynamic schema included in the request signature.
        "candidate_orientations": {"type": "object"},
        "planning_risks": STRINGS,
    }
)


STORY_PLAN_CLIP_SCHEMA = obj(
    {
        "id": NONEMPTY,
        "span_candidate_id": NONEMPTY,
        "source_id": NONEMPTY,
        "episode": {"type": "integer", "minimum": 1},
        "source_start": {"type": "number", "minimum": 0},
        "source_end": {"type": "number", "minimum": 0},
        "duration_seconds": {"type": "number", "minimum": 0},
        "source_duration_seconds": {"type": "number", "minimum": 0},
        "source_coverage_ratio": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "full_source_like": BOOL,
        "event_ids": STRINGS,
        "thread_beat_ids": STRINGS,
        "candidate_ids": STRINGS,
        "reuse_mode": {
            "type": "string",
            "enum": ["none", "teaser_reprise"],
        },
        "reprise_adds_information": STR,
        "boundary_status": {
            "type": "string",
            "enum": ["proposed", "needs_video_review", "verified"],
        },
        "material_risks": STRINGS,
    }
)


STORY_PLAN_VIEWER_KNOWLEDGE_SCHEMA = obj(
    {
        "before_fact_ids": STRINGS,
        "required_before_fact_ids": STRINGS,
        "introduced_fact_ids": STRINGS,
        "intentionally_withheld_fact_ids": STRINGS,
        "after_fact_ids": STRINGS,
    }
)


STORY_PLAN_BLOCK_SCHEMA = obj(
    {
        "id": NONEMPTY,
        "play_order": {"type": "integer", "minimum": 1},
        "role": STORY_PLAN_SELECTION_BLOCK_SCHEMA["properties"]["role"],
        "beat_ids": NONEMPTY_STRINGS,
        "thread_beat_ids": STRINGS,
        "clips": arr(STORY_PLAN_CLIP_SCHEMA, min_items=1),
        "introduced_fact_ids": STRINGS,
        "resolved_question_ids": STRINGS,
        "viewer_knowledge": STORY_PLAN_VIEWER_KNOWLEDGE_SCHEMA,
        "selection_reason": NONEMPTY,
    }
)


STORY_PLAN_SEQUENCE_EDGE_SCHEMA = obj(
    {
        "id": NONEMPTY,
        "from_block_id": NONEMPTY,
        "to_block_id": NONEMPTY,
        "temporal_relation": {
            "type": "string",
            "enum": [
                "continuation",
                "flashback_context",
                "preview_future",
                "return_to_mainline",
                "parallel",
            ],
        },
        "orientation_required": BOOL,
        "orientation_strategy": {
            "type": "string",
            "enum": [
                "dialogue_anchor",
                "visual_anchor",
                "title_card",
                "none",
            ],
        },
    }
)


# Plan 连续性 finding 是本地确定性产物，不是模型判断。它同时挂在
# Plan 顶层汇总和对应 junction 上，使 Materializer / Validator / QC /
# Admission 共用同一份类型化事实。
STORY_PLAN_CONTINUITY_FINDING_SCHEMA = obj(
    {
        "code": {
            "type": "string",
            "enum": ["dialogue_incomplete", "same_source_causal_gap"],
        },
        "severity": {"type": "string", "const": "block"},
        "span_candidate_ids": NONEMPTY_STRINGS,
        "clip_ids": NONEMPTY_STRINGS,
        "source_id": NONEMPTY,
        "gap_seconds": {"type": "number"},
        "shared_causal_ids": STRINGS,
        "reason": NONEMPTY,
    }
)

STORY_PLAN_CONTINUITY_SCHEMA = obj(
    {
        "policy_version": {
            "type": "string",
            "const": "story-continuity-v1",
        },
        "status": {"type": "string", "enum": ["safe", "blocked"]},
        "maximum_safe_same_source_gap_seconds": {
            "type": "number",
            "const": 12.0,
        },
        "findings": arr(STORY_PLAN_CONTINUITY_FINDING_SCHEMA),
    }
)


# M2 change 2: junction_type 描述**每一对相邻 clip** 的时间/线程关系。
# QC flow instruction 根据它给 Qwen 差异化提示：cross_thread_flashback /
# episode_skip / backward_flashback 是**有意设计**的跳转，只要求叙事逻辑连贯，
# 不再判定"画面/场景突变"为 Flow blocked。intra_episode / adjacent_episode
# 走严格视觉连续性检查。teaser_to_body / filler_tail 各按 rule 32/36 处理。
STORY_PLAN_JUNCTION_SCHEMA = obj(
    {
        "id": NONEMPTY,
        "from_clip_id": NONEMPTY,
        "to_clip_id": NONEMPTY,
        "from_block_id": NONEMPTY,
        "to_block_id": NONEMPTY,
        "junction_type": {
            "type": "string",
            "enum": [
                "teaser_to_body",
                "intra_episode",
                "adjacent_episode",
                "episode_skip",
                "backward_flashback",
                "filler_tail",
            ],
        },
        "episode_from": {"type": "integer", "minimum": 1},
        "episode_to": {"type": "integer", "minimum": 1},
        "is_block_boundary": BOOL,
    }
)
# Optional at the generic schema layer so archived Plan 1.0 files remain
# inspectable. Current materialization always writes these fields and the
# active Plan Validator rematerializes/diffs them, so old or hand-edited
# active generations cannot bypass the continuity contract.
STORY_PLAN_JUNCTION_SCHEMA["properties"].update(
    {
        "same_source_gap_seconds": {"type": "number"},
        "continuity_status": {
            "type": "string",
            "enum": ["not_applicable", "safe", "blocked"],
        },
        "continuity_findings": arr(
            STORY_PLAN_CONTINUITY_FINDING_SCHEMA
        ),
    }
)


STORY_PLAN_SOURCE_USAGE_SCHEMA = obj(
    {
        "source_id": NONEMPTY,
        "episode": {"type": "integer", "minimum": 1},
        "selected_clip_count": {"type": "integer", "minimum": 1},
        "playback_duration_seconds": {"type": "number", "minimum": 0},
        "unique_source_duration_seconds": {"type": "number", "minimum": 0},
    }
)


STORY_PLAN_EDITORIAL_METRICS_SCHEMA = obj(
    {
        "playback_duration_seconds": {"type": "number", "minimum": 0},
        "unique_source_duration_seconds": {"type": "number", "minimum": 0},
        "repeated_source_duration_seconds": {"type": "number", "minimum": 0},
        "repeat_ratio": {"type": "number", "minimum": 0, "maximum": 1},
        "available_unique_candidate_duration_seconds": {
            "type": "number",
            "minimum": 0,
        },
        "editorial_surplus_seconds": {"type": "number"},
        "editorial_surplus_ratio": {"type": "number"},
        "insufficient_editorial_surplus": BOOL,
        "full_source_like_clip_count": {"type": "integer", "minimum": 0},
        "full_source_like_playback_duration_seconds": {
            "type": "number",
            "minimum": 0,
        },
        "full_source_like_playback_ratio": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "teaser_duration_seconds": {"type": "number", "minimum": 0},
        "clip_count": {"type": "integer", "minimum": 0},
        "median_clip_duration_seconds": {"type": "number", "minimum": 0},
        "preferred_minimum_clip_count": {
            "type": "integer",
            "minimum": 1,
        },
        "preferred_median_clip_seconds_range": {
            "type": "array",
            "items": {"type": "number", "minimum": 0},
            "minItems": 2,
            "maxItems": 2,
        },
        "editorial_density_status": {
            "type": "string",
            "enum": ["passed", "below_target"],
        },
        "editorial_density_reasons": {
            "type": "array",
            "items": {"type": "string"},
        },
        "highlight_first_status": {
            "type": "string",
            "enum": ["passed", "failed"],
        },
    }
)


STORY_PLAN_REPAIR_ROUTE_SCHEMA = obj(
    {
        "code": NONEMPTY,
        "return_to_stage": {
            "type": "string",
            "enum": [
                "story_script",
                "story_evidence",
                "span_compiler",
                "story_plan",
                "story_scope",
            ],
        },
        "reason": NONEMPTY,
    }
)


STORY_PLAN_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.0"},
        "method": {
            "type": "string",
            "const": "legal-option-selection-local-materialization-v2",
        },
        "story_id": NONEMPTY,
        "title": NONEMPTY,
        "production_slot": {"type": "integer", "minimum": 1},
        "status": {
            "type": "string",
            "enum": ["ready_for_video_qc", "blocked"],
        },
        "input_fingerprints": obj(
            {
                "story_approval_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "portfolio_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "story_script_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "story_evidence_index_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "story_evidence_packet_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "span_candidate_index_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "span_candidate_bundle_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "selection_result_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "plan_generation_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
            }
        ),
        "duration_contract": obj(
            {
                "minimum_seconds": {"type": "number", "const": 0},
                "preferred_minimum_seconds": {"type": "number", "const": 0},
                "preferred_target_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1200,
                },
                "maximum_seconds": {"type": "number", "const": 1200},
                "soft_target_seconds": {"type": "number", "const": 0},
            },
            required=[
                "minimum_seconds",
                "preferred_minimum_seconds",
                "preferred_target_seconds",
                "maximum_seconds",
            ],
        ),
        "estimated_duration_seconds": {"type": "number", "minimum": 0},
        "editorial_metrics": STORY_PLAN_EDITORIAL_METRICS_SCHEMA,
        "blocks": arr(STORY_PLAN_BLOCK_SCHEMA, min_items=1),
        "sequence_edges": arr(STORY_PLAN_SEQUENCE_EDGE_SCHEMA),
        # M2 change 2: per-clip-pair junction 分类。materialize 阶段
        # 从 clip source_id / episode 计算得到；QC 阶段据此差异化 flow_instruction，
        # cross_thread_flashback / episode_skip / backward_flashback 不再被 Qwen
        # 判定为画面突变 Flow error。
        "junctions": arr(STORY_PLAN_JUNCTION_SCHEMA),
        "source_usage": arr(STORY_PLAN_SOURCE_USAGE_SCHEMA, min_items=1),
        "coverage": obj(
            {
                "required_beat_ids": STRINGS,
                "covered_beat_ids": STRINGS,
                "uncovered_required_beat_ids": STRINGS,
                "required_must_show_ids": STRINGS,
                "covered_must_show_ids": STRINGS,
                "uncovered_required_must_show_ids": STRINGS,
                "required_thread_beat_ids": STRINGS,
                "covered_thread_beat_ids": STRINGS,
                "uncovered_required_thread_beat_ids": STRINGS,
            }
        ),
        "selected_span_candidate_ids": NONEMPTY_STRINGS,
        "video_review_span_candidate_ids": STRINGS,
        "planning_risks": STRINGS,
        "blocked_reasons": STRINGS,
        "repair_routes": arr(STORY_PLAN_REPAIR_ROUTE_SCHEMA),
    }
)
# The summary stays optional for archive readability. Newly materialized
# active Plans always contain it and validate_story_plans requires an exact
# deterministic rematerialization match.
STORY_PLAN_SCHEMA["properties"]["continuity"] = (
    STORY_PLAN_CONTINUITY_SCHEMA
)


STORY_PLAN_INDEX_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.0"},
        "method": {
            "type": "string",
            "const": "legal-option-selection-local-materialization-v2",
        },
        "status": {
            "type": "string",
            "enum": [
                "ready_for_video_qc",
                "partially_ready",
                "blocked",
                "stale",
            ],
        },
        "story_approval_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "span_candidate_index_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "plan_generation_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "plan_count": {"type": "integer", "minimum": 0},
        "ready_plan_count": {"type": "integer", "minimum": 0},
        "blocked_plan_count": {"type": "integer", "minimum": 0},
        "plans": arr(
            obj(
                {
                    "story_id": NONEMPTY,
                    "title": NONEMPTY,
                    "production_slot": {"type": "integer", "minimum": 1},
                    "status": {
                        "type": "string",
                        "enum": ["ready_for_video_qc", "blocked"],
                    },
                    "path": NONEMPTY,
                    "plan_sha256": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                    },
                    "story_script_sha256": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                    },
                    "span_candidate_bundle_sha256": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                    },
                    "selection_result_sha256": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                    },
                    "estimated_duration_seconds": {
                        "type": "number",
                        "minimum": 0,
                    },
                    "block_count": {"type": "integer", "minimum": 1},
                    "clip_count": {"type": "integer", "minimum": 1},
                }
            ),
        ),
    }
)


# P0-2: closed enum for Story QC finding codes. Free-string `code` forced
# downstream (assemble_story_qc, boundary_repair) to do fuzzy clustering by
# substring; a closed enum lets patch routing key off exact code.
# `other` is the escape hatch — when it is used, description.minLength is
# raised at assemble time (not in schema, to keep schema simple for strict
# mode compatibility across DashScope revisions).
STORY_VIDEO_QC_FINDING_CODES = (
    # coverage — model-emitted
    "coverage_missing",
    "must_show_absent",
    "payoff_absent",
    "teaser_reprise_missing",
    "thread_broken",
    # flow — model-emitted
    "orientation_missing",
    "character_identity_confused",
    "environment_jump",
    "spoiler_leak",
    # cut_safety — model-emitted
    "action_cut_visible",
    "dialogue_interrupted",
    "filler_visual_glitch",
    # local deterministic Plan continuity — synthesized by Story QC
    "same_source_causal_gap",
    "dialogue_incomplete",
    # local audio boundary — synthesized by assemble_story_qc from
    # audio_boundary_guard reports. Downstream boundary_repair may key
    # patch routing off these codes, so string form is stable.
    "local-audio-fade-fallback-source_start",
    "local-audio-fade-fallback-source_end",
    "local-audio-source-edge-speech-active-source_start",
    "local-audio-source-edge-speech-active-source_end",
    "local-audio-adjustment_required-source_start",
    "local-audio-adjustment_required-source_end",
    "local-audio-blocked_replan-source_start",
    "local-audio-blocked_replan-source_end",
    "local-audio-analysis_error-source_start",
    "local-audio-analysis_error-source_end",
    # escape hatch — assemble_story_qc enforces description.minLength=40
    "other",
)
STORY_VIDEO_QC_FINDING_CODE_OTHER = "other"
STORY_VIDEO_QC_FINDING_OTHER_MIN_DESCRIPTION_LENGTH = 40

# A Story QC response may only emit findings that belong to the review's
# declared scope.  The complete enum above also contains locally synthesized
# audio/continuity codes because those share the final report schema; they are
# never legal model output.  In particular, a Junction preview contains only
# short handles around one cut and has coverage=not_assessed, so it cannot
# decide whether an entire must-show, Payoff or Teaser reprise is absent.
STORY_VIDEO_QC_COVERAGE_FINDING_CODES = (
    "coverage_missing",
    "must_show_absent",
    "payoff_absent",
    "teaser_reprise_missing",
)
STORY_VIDEO_QC_FLOW_FINDING_CODES = (
    "thread_broken",
    "orientation_missing",
    "character_identity_confused",
    "environment_jump",
    "spoiler_leak",
)
STORY_VIDEO_QC_CUT_SAFETY_FINDING_CODES = (
    "action_cut_visible",
    "dialogue_interrupted",
    "filler_visual_glitch",
)
STORY_VIDEO_QC_MODEL_FINDING_CODES = (
    *STORY_VIDEO_QC_COVERAGE_FINDING_CODES,
    *STORY_VIDEO_QC_FLOW_FINDING_CODES,
    *STORY_VIDEO_QC_CUT_SAFETY_FINDING_CODES,
    STORY_VIDEO_QC_FINDING_CODE_OTHER,
)


def story_video_qc_allowed_finding_codes(review_kind: str) -> tuple[str, ...]:
    """Return the closed model-finding enum for one QC review scope."""

    if review_kind == "story_flow":
        return STORY_VIDEO_QC_MODEL_FINDING_CODES
    if review_kind == "junction":
        return (
            *STORY_VIDEO_QC_FLOW_FINDING_CODES,
            *STORY_VIDEO_QC_CUT_SAFETY_FINDING_CODES,
            STORY_VIDEO_QC_FINDING_CODE_OTHER,
        )
    if review_kind in {"boundary_start", "boundary_end"}:
        return (
            *STORY_VIDEO_QC_CUT_SAFETY_FINDING_CODES,
            STORY_VIDEO_QC_FINDING_CODE_OTHER,
        )
    raise ValueError(f"unsupported Story QC review kind {review_kind!r}")


def story_video_qc_allowed_finding_categories(
    review_kind: str,
) -> tuple[str, ...]:
    """Return categories a model may assess for one QC review scope."""

    if review_kind == "story_flow":
        return ("coverage", "flow", "cut_safety")
    if review_kind == "junction":
        return ("flow", "cut_safety")
    if review_kind in {"boundary_start", "boundary_end"}:
        return ("cut_safety",)
    raise ValueError(f"unsupported Story QC review kind {review_kind!r}")


STORY_VIDEO_QC_FINDING_SCHEMA = obj(
    {
        "code": {
            "type": "string",
            "enum": list(STORY_VIDEO_QC_FINDING_CODES),
        },
        "category": {
            "type": "string",
            "enum": ["coverage", "flow", "cut_safety"],
        },
        "severity": {
            "type": "string",
            "enum": ["info", "review", "block"],
        },
        "description": NONEMPTY,
        "proxy_start_seconds": {"type": "number", "minimum": 0},
        "proxy_end_seconds": {"type": "number", "minimum": 0},
        "block_ids": STRINGS,
        "clip_ids": STRINGS,
        "suggested_action": {
            "type": "string",
            "enum": [
                "none",
                "adjust_start",
                "adjust_end",
                "replace_span",
                "replan",
                "human_review",
                "apply_fade_fallback",
            ],
        },
    }
)


STORY_VIDEO_QC_RESULT_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.0"},
        "story_id": NONEMPTY,
        "review_id": NONEMPTY,
        "review_kind": {
            "type": "string",
            "enum": [
                "story_flow",
                "boundary_start",
                "boundary_end",
                "junction",
            ],
        },
        "overall_status": {
            "type": "string",
            "enum": ["pass", "review", "block"],
        },
        "checks": obj(
            {
                "coverage": {
                    "type": "string",
                    "enum": ["not_assessed", "pass", "review", "block"],
                },
                "flow": {
                    "type": "string",
                    "enum": ["not_assessed", "pass", "review", "block"],
                },
                "cut_safety": {
                    "type": "string",
                    "enum": ["not_assessed", "pass", "review", "block"],
                },
            }
        ),
        "findings": arr(STORY_VIDEO_QC_FINDING_SCHEMA),
        "verified_boundary": {
            "type": "string",
            "enum": ["not_applicable", "yes", "no"],
        },
        "summary": NONEMPTY,
    }
)


def story_video_qc_response_format(
    *,
    story_id: str,
    review_id: str,
    review_kind: str,
    block_ids: list[str] | None = None,
    clip_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build the strict response contract for one concrete Story QC review.

    When ``block_ids`` / ``clip_ids`` are provided, the returned schema
    additionally locks ``findings[].block_ids.items`` / ``clip_ids.items``
    to an enum of those IDs, so the model cannot invent references outside
    the current review's scope. Passing ``None`` keeps the free-string
    behavior (used by callers and tests).
    """
    if review_kind not in {
        "story_flow",
        "boundary_start",
        "boundary_end",
        "junction",
    }:
        raise ValueError(f"unsupported Story QC review kind {review_kind!r}")
    schema = deepcopy(STORY_VIDEO_QC_RESULT_SCHEMA)
    properties = schema["properties"]
    properties["story_id"] = {"type": "string", "const": story_id}
    properties["review_id"] = {"type": "string", "const": review_id}
    properties["review_kind"] = {"type": "string", "const": review_kind}
    checks = properties["checks"]["properties"]
    assessed = {"type": "string", "enum": ["pass", "review", "block"]}
    not_assessed = {"type": "string", "const": "not_assessed"}
    if review_kind == "story_flow":
        checks["coverage"] = deepcopy(assessed)
        checks["flow"] = deepcopy(assessed)
        checks["cut_safety"] = deepcopy(assessed)
        properties["verified_boundary"] = {
            "type": "string",
            "const": "not_applicable",
        }
    elif review_kind == "junction":
        checks["coverage"] = not_assessed
        checks["flow"] = deepcopy(assessed)
        checks["cut_safety"] = deepcopy(assessed)
        properties["verified_boundary"] = {
            "type": "string",
            "const": "not_applicable",
        }
    else:
        checks["coverage"] = deepcopy(not_assessed)
        checks["flow"] = deepcopy(not_assessed)
        checks["cut_safety"] = deepcopy(assessed)
        properties["verified_boundary"] = {
            "type": "string",
            "enum": ["yes", "no"],
        }
    finding_props = properties["findings"]["items"]["properties"]
    finding_props["code"] = {
        "type": "string",
        "enum": list(story_video_qc_allowed_finding_codes(review_kind)),
    }
    finding_props["category"] = {
        "type": "string",
        "enum": list(
            story_video_qc_allowed_finding_categories(review_kind)
        ),
    }
    if block_ids is not None or clip_ids is not None:
        if block_ids is not None:
            finding_props["block_ids"] = {
                "type": "array",
                "items": {"type": "string", "enum": sorted(set(block_ids))},
            }
        if clip_ids is not None:
            finding_props["clip_ids"] = {
                "type": "array",
                "items": {"type": "string", "enum": sorted(set(clip_ids))},
            }
    return response_format(
        "story_video_qc",
        schema_override=schema,
        revision_override="v2_dynamic",
    )


STORY_QC_PROXY_CLIP_SCHEMA = obj(
    {
        "clip_id": NONEMPTY,
        "block_id": NONEMPTY,
        "block_play_order": {"type": "integer", "minimum": 1},
        "clip_order_in_block": {"type": "integer", "minimum": 1},
        "source_id": NONEMPTY,
        "episode": {"type": "integer", "minimum": 1},
        "source_start": {"type": "number", "minimum": 0},
        "source_end": {"type": "number", "minimum": 0},
        "proxy_start": {"type": "number", "minimum": 0},
        "proxy_end": {"type": "number", "minimum": 0},
        "boundary_status": {
            "type": "string",
            "enum": ["proposed", "needs_video_review", "verified"],
        },
    }
)
STORY_QC_PROXY_CLIP_SCHEMA["properties"]["junction_edit_id"] = NONEMPTY
STORY_QC_PROXY_CLIP_SCHEMA["properties"]["incoming_junction_edit_id"] = NONEMPTY
STORY_QC_PROXY_CLIP_SCHEMA["properties"]["video_source_end"] = {
    "type": "number",
    "minimum": 0,
}


STORY_QC_REVIEW_ASSET_SCHEMA = obj(
    {
        "review_id": NONEMPTY,
        "review_kind": STORY_VIDEO_QC_RESULT_SCHEMA["properties"][
            "review_kind"
        ],
        "path": NONEMPTY,
        "sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "duration_seconds": {"type": "number", "minimum": 0},
        "cut_at_seconds": {"type": "number", "minimum": 0},
        "context_path": NONEMPTY,
        "context_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "block_ids": STRINGS,
        "clip_ids": STRINGS,
        "edge_ids": STRINGS,
    }
)


STORY_PLAN_QC_ADMISSION_SCHEMA = obj(
    {
        "entry_mode": {
            "type": "string",
            "enum": [
                "machine_validated_plan",
                "human_accepted_blocked_plan",
            ],
        },
        "original_plan_status": {
            "type": "string",
            "enum": ["ready_for_video_qc", "blocked"],
        },
        "admission_path": {
            "anyOf": [NONEMPTY, {"type": "null"}],
        },
        "admission_sha256": {
            "anyOf": [
                {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                {"type": "null"},
            ]
        },
        "accepted_blocked_reasons": STRINGS,
        "human_note": STR,
        "decided_at": {
            "anyOf": [NONEMPTY, {"type": "null"}],
        },
    }
)


STORY_QC_PROXY_MANIFEST_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.0"},
        "method": {
            "type": "string",
            "enum": [
                "hard-cut-qc-proxy-v1",
                "effect-accurate-junction-edit-qc-proxy-v2",
                "effect-accurate-junction-edit-qc-proxy-v3-pair-timeline",
            ],
        },
        "story_id": NONEMPTY,
        "title": NONEMPTY,
        "production_slot": {"type": "integer", "minimum": 1},
        "input_fingerprints": obj(
            {
                "story_plan_index_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "story_plan_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "story_script_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "source_manifest_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
            }
        ),
        "profile": obj(
            {
                "width": {"type": "integer", "minimum": 64},
                "height": {"type": "integer", "minimum": 64},
                "fps": {"type": "number", "minimum": 1},
                "video_bitrate_kbps": {"type": "integer", "minimum": 32},
                "audio_bitrate_kbps": {"type": "integer", "minimum": 16},
                "junction_handle_seconds": {
                    "type": "number",
                    "minimum": 0.25,
                },
            }
        ),
        "story_proxy": obj(
            {
                "path": NONEMPTY,
                "sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "duration_seconds": {"type": "number", "minimum": 0},
                "size_bytes": {"type": "integer", "minimum": 1},
                "has_video": {"type": "boolean", "const": True},
                "has_audio": {"type": "boolean", "const": True},
            }
        ),
        "clips": arr(STORY_QC_PROXY_CLIP_SCHEMA, min_items=1),
        "review_assets": arr(STORY_QC_REVIEW_ASSET_SCHEMA, min_items=1),
    }
)
STORY_QC_PROXY_MANIFEST_SCHEMA["properties"][
    "story_plan_qc_admission"
] = STORY_PLAN_QC_ADMISSION_SCHEMA
STORY_QC_PROXY_MANIFEST_SCHEMA["properties"][
    "junction_edit_plan_path"
] = NONEMPTY
STORY_QC_PROXY_MANIFEST_SCHEMA["properties"][
    "junction_edit_plan_sha256"
] = {
    "type": "string",
    "minLength": 64,
    "maxLength": 64,
}
STORY_QC_PROXY_MANIFEST_SCHEMA["properties"]["input_fingerprints"][
    "properties"
]["junction_edit_plan_sha256"] = {
    "type": "string",
    "minLength": 64,
    "maxLength": 64,
}


STORY_QC_STATIC_CHECK_SCHEMA = obj(
    {
        "id": NONEMPTY,
        "status": {
            "type": "string",
            "enum": ["pass", "review", "block"],
        },
        "description": NONEMPTY,
        "related_ids": STRINGS,
    }
)


STORY_QC_GROUP_SCHEMA = obj(
    {
        "status": {
            "type": "string",
            "enum": ["approved", "review", "blocked"],
        },
        "static_checks": arr(STORY_QC_STATIC_CHECK_SCHEMA, min_items=0),
        "video_review_ids": STRINGS,
        "findings": arr(STORY_VIDEO_QC_FINDING_SCHEMA),
    }
)


STORY_QC_PATCH_RECOMMENDATION_SCHEMA = obj(
    {
        "action": {
            "type": "string",
            "enum": [
                "adjust_start",
                "adjust_end",
                "replace_span",
                "replan",
                "human_review",
                "apply_fade_fallback",
            ],
        },
        "target_ids": NONEMPTY_STRINGS,
        "reason": NONEMPTY,
    }
)


STORY_QC_LOCAL_AUDIO_BOUNDARY_SCHEMA = obj(
    {
        "clip_id": NONEMPTY,
        "block_id": NONEMPTY,
        "source_id": NONEMPTY,
        "boundary": {
            "type": "string",
            "enum": ["source_start", "source_end"],
        },
        "status": {
            "type": "string",
            "enum": [
                "safe",
                "safe_source_edge",
                "not_applicable_no_audio",
                "adjustment_required",
                "blocked_replan",
                "analysis_error",
            ],
        },
        "planned_source_seconds": {"type": "number", "minimum": 0},
        "speech_active_at_cut": BOOL,
        "speech_interval": {
            "anyOf": [
                obj(
                    {
                        "start": {"type": "number", "minimum": 0},
                        "end": {"type": "number", "minimum": 0},
                    }
                ),
                {"type": "null"},
            ]
        },
        "recommended_source_seconds": {"type": "number", "minimum": 0},
        "adjustment_seconds": NUM,
        "reason": NONEMPTY,
    }
)


STORY_QC_LOCAL_AUDIO_SCHEMA = obj(
    {
        "status": {
            "type": "string",
            "enum": ["approved", "review", "blocked"],
        },
        "method": {
            "type": "string",
            "const": "demucs-silero-dual-vad-v1.2",
        },
        "safe_boundary_count": {"type": "integer", "minimum": 0},
        "review_boundary_count": {"type": "integer", "minimum": 0},
        "blocking_boundary_count": {"type": "integer", "minimum": 0},
        "boundaries": arr(STORY_QC_LOCAL_AUDIO_BOUNDARY_SCHEMA, min_items=2),
        "report_path": NONEMPTY,
        "report_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "engines": obj(
            {
                "demucs": {"type": "string", "const": "4.1.0"},
                "silero-vad": {"type": "string", "const": "6.2.1"},
                "onnxruntime": {"type": "string", "const": "1.24.3"},
            }
        ),
        "policy": {"type": "object"},
        "remote_audio_upload": {"type": "boolean", "const": False},
    }
)


STORY_QC_BOUNDARY_REPAIR_SCHEMA = obj(
    {
        "status": {
            "type": "string",
            "enum": [
                "not_needed",
                "verified_after_repair",
                "review",
                "blocked_replan",
            ],
        },
        "metadata_path": NONEMPTY,
        "metadata_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "base_story_plan_index_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "effective_story_plan_index_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "repair_round": {"type": "integer", "minimum": 0, "maximum": 2},
        "applied_change_count": {"type": "integer", "minimum": 0},
        "patch_history": arr(
            obj(
                {
                    "path": NONEMPTY,
                    "sha256": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                    },
                }
            )
        ),
        "unresolved_boundary_ids": STRINGS,
    }
)


STORY_QC_REPORT_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.0"},
        "method": {
            "type": "string",
            "const": "static-proxy-local-audio-boundary-repair-v3",
        },
        "story_id": NONEMPTY,
        "title": NONEMPTY,
        "production_slot": {"type": "integer", "minimum": 1},
        "status": {
            "type": "string",
            "enum": ["approved", "review", "blocked"],
        },
        "input_fingerprints": obj(
            {
                "story_plan_index_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "story_plan_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "story_script_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "source_manifest_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "proxy_manifest_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "story_qc_batch_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "story_audio_boundary_plan_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "story_audio_boundary_report_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "story_boundary_repair_metadata_sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "video_results": arr(
                    obj(
                        {
                            "review_id": NONEMPTY,
                            "sha256": {
                                "type": "string",
                                "minLength": 64,
                                "maxLength": 64,
                            },
                        }
                    ),
                    min_items=1,
                ),
            }
        ),
        "proxy_manifest_path": NONEMPTY,
        "story_proxy_path": NONEMPTY,
        "story_proxy_duration_seconds": {
            "type": "number",
            "minimum": 0,
        },
        "coverage_qc": STORY_QC_GROUP_SCHEMA,
        "flow_qc": STORY_QC_GROUP_SCHEMA,
        "cut_safety_qc": STORY_QC_GROUP_SCHEMA,
        "local_audio_boundary": STORY_QC_LOCAL_AUDIO_SCHEMA,
        "boundary_repair": STORY_QC_BOUNDARY_REPAIR_SCHEMA,
        "verified_clip_ids": STRINGS,
        "review_clip_ids": STRINGS,
        "blocked_clip_ids": STRINGS,
        "findings": arr(STORY_VIDEO_QC_FINDING_SCHEMA),
        "patch_recommendations": arr(
            STORY_QC_PATCH_RECOMMENDATION_SCHEMA
        ),
    }
)
STORY_QC_REPORT_SCHEMA["properties"][
    "story_plan_qc_admission"
] = STORY_PLAN_QC_ADMISSION_SCHEMA
STORY_QC_REPORT_SCHEMA["properties"]["input_fingerprints"]["properties"][
    "junction_edit_plan_sha256"
] = {
    "type": "string",
    "minLength": 64,
    "maxLength": 64,
}


STORY_QC_INDEX_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.0"},
        "method": {
            "type": "string",
            "const": "static-proxy-local-audio-boundary-repair-v3",
        },
        "status": {
            "type": "string",
            "enum": ["approved", "review", "blocked"],
        },
        "story_plan_index_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "story_qc_batch_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "report_count": {"type": "integer", "minimum": 1},
        "approved_count": {"type": "integer", "minimum": 0},
        "review_count": {"type": "integer", "minimum": 0},
        "blocked_count": {"type": "integer", "minimum": 0},
        "reports": arr(
            obj(
                {
                    "story_id": NONEMPTY,
                    "title": NONEMPTY,
                    "production_slot": {"type": "integer", "minimum": 1},
                    "status": {
                        "type": "string",
                        "enum": ["approved", "review", "blocked"],
                    },
                    "path": NONEMPTY,
                    "report_sha256": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                    },
                    "story_plan_sha256": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                    },
                    "proxy_manifest_sha256": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                    },
                }
            ),
            min_items=1,
        ),
    }
)


STORY_RENDER_INPUT_FINGERPRINTS_SCHEMA = obj(
    {
        "story_qc_index_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "story_qc_report_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "source_manifest_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "local_source_manifest_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "effective_story_plan_index_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "effective_story_plan_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "boundary_repair_metadata_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
    }
)


STORY_RENDER_PROFILE_SCHEMA = obj(
    {
        "name": {"type": "string", "const": "delivery"},
        "width": {"type": "integer", "const": 1080},
        "height": {"type": "integer", "const": 1920},
        "fps": {"type": "integer", "const": 25},
        "fit": {"type": "string", "const": "contain"},
        "video_codec": {"type": "string", "const": "libx264"},
        "video_crf": {"type": "integer", "const": 18},
        "video_preset": {"type": "string", "const": "medium"},
        "pixel_format": {"type": "string", "const": "yuv420p"},
        "audio_codec": {"type": "string", "const": "aac"},
        "audio_bitrate_kbps": {"type": "integer", "const": 192},
        "audio_sample_rate": {"type": "integer", "const": 48000},
        "audio_channels": {"type": "integer", "const": 2},
        "faststart": {"type": "boolean", "const": True},
    }
)


STORY_RENDER_TRANSITION_POLICY_SCHEMA = obj(
    {
        "teaser_to_body": obj(
            {
                "type": {"type": "string", "const": "black_separator"},
                "duration_seconds": {"type": "number", "const": 0.35},
                "audio_policy": {"type": "string", "const": "silence"},
                "fade_out_seconds": {"type": "number", "const": 0.18},
                "fade_in_seconds": {"type": "number", "const": 0.18},
                "fade_curve": {"type": "string", "const": "tri"},
            }
        ),
        "other_junctions": {"type": "string", "const": "hard_cut"},
    }
)


STORY_RENDER_SOURCE_SCHEMA = obj(
    {
        "source_id": NONEMPTY,
        "episode": {"type": "integer", "minimum": 1},
        "path": NONEMPTY,
        "sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "duration_seconds": {"type": "number", "minimum": 0},
    }
)


STORY_RENDER_CLIP_SCHEMA = obj(
    {
        "id": NONEMPTY,
        "block_id": NONEMPTY,
        "block_play_order": {"type": "integer", "minimum": 1},
        "block_role": STORY_PLAN_SELECTION_BLOCK_SCHEMA["properties"]["role"],
        "clip_order_in_block": {"type": "integer", "minimum": 1},
        "source_id": NONEMPTY,
        "episode": {"type": "integer", "minimum": 1},
        "source_start": {"type": "number", "minimum": 0},
        "source_end": {"type": "number", "minimum": 0},
        "duration_seconds": {"type": "number", "minimum": 0},
        "filler_tail_seconds": {"type": "number", "minimum": 0},
    }
)
STORY_RENDER_CLIP_SCHEMA["properties"]["plan_duration_seconds"] = {
    "type": "number",
    "minimum": 0,
}
STORY_RENDER_CLIP_SCHEMA["properties"]["video_source_end"] = {
    "type": "number",
    "minimum": 0,
}
STORY_RENDER_CLIP_SCHEMA["properties"]["junction_edit_id"] = NONEMPTY
STORY_RENDER_CLIP_SCHEMA["properties"]["incoming_junction_edit_id"] = NONEMPTY


STORY_RENDER_TRANSITION_SCHEMA = obj(
    {
        "id": NONEMPTY,
        "from_clip_id": NONEMPTY,
        "to_clip_id": NONEMPTY,
        "from_block_id": NONEMPTY,
        "to_block_id": NONEMPTY,
        "type": {"type": "string", "const": "black_separator"},
        "duration_seconds": {"type": "number", "const": 0.35},
        "audio_policy": {"type": "string", "const": "silence"},
        "fade_out_seconds": {"type": "number", "const": 0.18},
        "fade_in_seconds": {"type": "number", "const": 0.18},
        "fade_curve": {"type": "string", "const": "tri"},
    }
)


STORY_RENDER_TIMELINE_ITEM_SCHEMA = obj(
    {
        "order": {"type": "integer", "minimum": 1},
        "kind": {"type": "string", "enum": ["clip", "transition"]},
        "ref_id": NONEMPTY,
        "start_seconds": {"type": "number", "minimum": 0},
        "end_seconds": {"type": "number", "minimum": 0},
        "duration_seconds": {"type": "number", "minimum": 0},
    }
)


STORY_RENDER_FADE_FALLBACK_JUNCTION_SCHEMA = obj(
    {
        "id": NONEMPTY,
        "from_clip_id": NONEMPTY,
        "to_clip_id": NONEMPTY,
        "from_block_id": NONEMPTY,
        "to_block_id": NONEMPTY,
        "type": {"type": "string", "const": "fade_only"},
        "audio_crossfade_seconds": {"type": "number", "minimum": 0},
        "video_fade_out_seconds": {"type": "number", "minimum": 0},
        "video_fade_in_seconds": {"type": "number", "minimum": 0},
        "fade_curve": {"type": "string", "const": "tri"},
        "trigger_boundaries": arr(NONEMPTY, min_items=1),
    }
)


STORY_RENDER_RECIPE_V1_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.1"},
        "method": {
            "type": "string",
            "const": "approved-qc-local-render-recipe-v1",
        },
        "story_id": NONEMPTY,
        "title": NONEMPTY,
        "production_slot": {"type": "integer", "minimum": 1},
        "status": {"type": "string", "const": "ready_for_render"},
        "input_fingerprints": STORY_RENDER_INPUT_FINGERPRINTS_SCHEMA,
        "render_profile": STORY_RENDER_PROFILE_SCHEMA,
        "transition_policy": STORY_RENDER_TRANSITION_POLICY_SCHEMA,
        "sources": arr(STORY_RENDER_SOURCE_SCHEMA, min_items=1),
        "clips": arr(STORY_RENDER_CLIP_SCHEMA, min_items=1),
        # M3 A1: mode=none 时 transitions 可为空数组（无 teaser→body
        # 黑场）；mode=single_highlight 时恰好一条 black_separator。
        "transitions": arr(STORY_RENDER_TRANSITION_SCHEMA, min_items=0),
        # A4: fade_fallback junctions——由 boundary_repair 的
        # fade_fallback route 生成。在对应相邻 clip junction 上做 audio
        # crossfade + video fade，不占用 timeline 时长（在相邻 clip 音视频
        # 内部合成）。可以为空数组。
        "fade_fallback_junctions": arr(
            STORY_RENDER_FADE_FALLBACK_JUNCTION_SCHEMA, min_items=0
        ),
        "timeline": arr(STORY_RENDER_TIMELINE_ITEM_SCHEMA, min_items=3),
        "clip_count": {"type": "integer", "minimum": 2},
        # M3 A1: mode=none 时 transition_count=0；single_highlight 时=1。
        "transition_count": {"type": "integer", "minimum": 0, "maximum": 1},
        "fade_fallback_junction_count": {"type": "integer", "minimum": 0},
        "source_duration_seconds": {"type": "number", "minimum": 0},
        # M3 A1: transition_duration_seconds 0（mode=none）或 0.35（single_highlight）。
        "transition_duration_seconds": {"type": "number", "minimum": 0, "maximum": 0.35},
        "expected_duration_seconds": {"type": "number", "minimum": 0},
        "output_filename": NONEMPTY,
        "filler_tail_seconds": {"type": "number", "minimum": 0},
        "filler_tail_target_seconds": {"type": "number", "minimum": 0},
    }
)


STORY_RENDER_JUNCTION_EDIT_SCHEMA = obj(
    {
        "id": NONEMPTY,
        "constraint_id": NONEMPTY,
        "type": {"type": "string", "const": "audio_tail_over_bridge"},
        "from_clip_id": NONEMPTY,
        "to_clip_id": NONEMPTY,
        "from_source_id": NONEMPTY,
        "to_source_id": NONEMPTY,
        "left_video_end_seconds": {"type": "number", "minimum": 0},
        "left_audio_end_seconds": {"type": "number", "minimum": 0},
        "audio_tail_duration_seconds": {"type": "number", "minimum": 0},
        "bridge": obj(
            {
                "source_id": NONEMPTY,
                "source_start": {"type": "number", "minimum": 0},
                "source_end": {"type": "number", "minimum": 0},
                "duration_seconds": {"type": "number", "minimum": 0},
                "frame_count": {"type": "integer", "minimum": 1},
                "audio_policy": {"type": "string", "const": "mute"},
            }
        ),
        "audio_padding": obj(
            {
                "type": {"type": "string", "const": "silence"},
                "duration_seconds": {"type": "number", "minimum": 0},
            }
        ),
        "right_video_start_seconds": {"type": "number", "minimum": 0},
        "right_audio_start_seconds": {"type": "number", "minimum": 0},
        "preserve_left_audio": {"type": "boolean", "const": True},
        "preserve_right_audio": {"type": "boolean", "const": True},
        "forbidden_visual_ranges": arr(
            obj(
                {
                    "source_id": NONEMPTY,
                    "start_seconds": {"type": "number", "minimum": 0},
                    "end_seconds": {"type": "number", "minimum": 0},
                    "reason": NONEMPTY,
                }
            ),
            min_items=1,
        ),
        "reason": NONEMPTY,
    }
)


STORY_RENDER_REVIEWED_BRIDGE_EDIT_SCHEMA = deepcopy(
    STORY_RENDER_JUNCTION_EDIT_SCHEMA
)
STORY_RENDER_REVIEWED_BRIDGE_EDIT_SCHEMA["properties"].update(
    {
        "effect": {"type": "string", "const": "audio_tail_visual_repair"},
        "strategy": {"type": "string", "const": "reviewed_bridge"},
        "duration_delta_seconds": {"type": "number"},
    }
)
STORY_RENDER_REVIEWED_BRIDGE_EDIT_SCHEMA["required"].extend(
    ["effect", "strategy", "duration_delta_seconds"]
)


STORY_RENDER_RIGHT_AV_OVERLAP_EDIT_SCHEMA = obj(
    {
        "id": NONEMPTY,
        "constraint_id": NONEMPTY,
        "type": {"type": "string", "const": "audio_tail_visual_repair"},
        "effect": {"type": "string", "const": "audio_tail_visual_repair"},
        "strategy": {"type": "string", "const": "right_av_overlap"},
        "from_clip_id": NONEMPTY,
        "to_clip_id": NONEMPTY,
        "from_source_id": NONEMPTY,
        "to_source_id": NONEMPTY,
        "left_video_end_seconds": {"type": "number", "minimum": 0},
        "left_audio_end_seconds": {"type": "number", "minimum": 0},
        "audio_tail_duration_seconds": {"type": "number", "minimum": 0},
        "overlap": obj(
            {
                "duration_seconds": {"type": "number", "minimum": 0},
                "left_audio_fade_out_seconds": {"type": "number", "minimum": 0},
                "right_audio_fade_in_seconds": {"type": "number", "minimum": 0},
                "simultaneous_speech_seconds": {"type": "number", "minimum": 0},
                "max_simultaneous_speech_seconds": {"type": "number", "minimum": 0},
                "right_entry_visual_review": {"type": "string", "const": "safe"},
                "right_av_sync_offset_seconds": {"type": "number", "const": 0},
            }
        ),
        "duration_delta_seconds": {"type": "number", "maximum": 0},
        "right_video_start_seconds": {"type": "number", "minimum": 0},
        "right_audio_start_seconds": {"type": "number", "minimum": 0},
        "preserve_left_audio": {"type": "boolean", "const": True},
        "preserve_right_audio": {"type": "boolean", "const": True},
        "forbidden_visual_ranges": arr(
            obj(
                {
                    "source_id": NONEMPTY,
                    "start_seconds": {"type": "number", "minimum": 0},
                    "end_seconds": {"type": "number", "minimum": 0},
                    "reason": NONEMPTY,
                }
            ),
            min_items=1,
        ),
        "reason": NONEMPTY,
    }
)


STORY_RENDER_PAIR_JUNCTION_EDIT_SCHEMA = {
    "anyOf": [
        STORY_RENDER_JUNCTION_EDIT_SCHEMA,
        STORY_RENDER_REVIEWED_BRIDGE_EDIT_SCHEMA,
        STORY_RENDER_RIGHT_AV_OVERLAP_EDIT_SCHEMA,
    ]
}


STORY_RENDER_INPUT_FINGERPRINTS_V2_SCHEMA = deepcopy(
    STORY_RENDER_INPUT_FINGERPRINTS_SCHEMA
)
STORY_RENDER_INPUT_FINGERPRINTS_V2_SCHEMA["properties"][
    "junction_edit_plan_sha256"
] = {
    "type": "string",
    "minLength": 64,
    "maxLength": 64,
}
STORY_RENDER_INPUT_FINGERPRINTS_V2_SCHEMA["required"].append(
    "junction_edit_plan_sha256"
)


STORY_RENDER_RECIPE_V2_SCHEMA = deepcopy(STORY_RENDER_RECIPE_V1_SCHEMA)
STORY_RENDER_RECIPE_V2_SCHEMA["properties"]["schema_version"] = {
    "type": "string",
    "const": "2.0",
}
STORY_RENDER_RECIPE_V2_SCHEMA["properties"]["method"] = {
    "type": "string",
    "const": "approved-qc-local-render-recipe-v2-junction-edit",
}
STORY_RENDER_RECIPE_V2_SCHEMA["properties"][
    "input_fingerprints"
] = STORY_RENDER_INPUT_FINGERPRINTS_V2_SCHEMA
STORY_RENDER_RECIPE_V2_SCHEMA["properties"]["transition_policy"][
    "properties"
]["other_junctions"] = {
    "type": "string",
    "const": "hard_cut_unless_compiled_junction_edit",
}
STORY_RENDER_RECIPE_V2_SCHEMA["properties"]["junction_edits"] = arr(
    STORY_RENDER_JUNCTION_EDIT_SCHEMA,
    min_items=1,
)
STORY_RENDER_RECIPE_V2_SCHEMA["properties"]["junction_edit_count"] = {
    "type": "integer",
    "minimum": 1,
}
STORY_RENDER_RECIPE_V2_SCHEMA["required"].extend(
    ["junction_edits", "junction_edit_count"]
)


STORY_RENDER_RECIPE_V3_SCHEMA = deepcopy(STORY_RENDER_RECIPE_V2_SCHEMA)
STORY_RENDER_RECIPE_V3_SCHEMA["properties"]["schema_version"] = {
    "type": "string",
    "const": "2.1",
}
STORY_RENDER_RECIPE_V3_SCHEMA["properties"]["method"] = {
    "type": "string",
    "const": "approved-qc-local-render-recipe-v3-pair-timeline",
}
STORY_RENDER_RECIPE_V3_SCHEMA["properties"]["junction_edits"] = arr(
    STORY_RENDER_PAIR_JUNCTION_EDIT_SCHEMA,
    min_items=1,
)


STORY_RENDER_RECIPE_SCHEMA = {
    "anyOf": [
        STORY_RENDER_RECIPE_V1_SCHEMA,
        STORY_RENDER_RECIPE_V2_SCHEMA,
        STORY_RENDER_RECIPE_V3_SCHEMA,
    ]
}


STORY_RENDER_RECIPE_INDEX_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.0"},
        "method": {
            "type": "string",
            "const": "approved-qc-local-render-recipe-index-v1",
        },
        "status": {
            "type": "string",
            "enum": ["complete", "partial", "blocked"],
        },
        "story_qc_index_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "effective_story_plan_index_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "source_manifest_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "local_source_manifest_path": NONEMPTY,
        "local_source_manifest_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "qc_report_count": {"type": "integer", "minimum": 1},
        "approved_story_count": {"type": "integer", "minimum": 0},
        "recipe_count": {"type": "integer", "minimum": 0},
        "skipped_story_count": {"type": "integer", "minimum": 0},
        "recipes": arr(
            obj(
                {
                    "story_id": NONEMPTY,
                    "title": NONEMPTY,
                    "production_slot": {"type": "integer", "minimum": 1},
                    "path": NONEMPTY,
                    "recipe_sha256": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                    },
                    "story_qc_report_sha256": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                    },
                    "effective_story_plan_sha256": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                    },
                    "expected_duration_seconds": {
                        "type": "number",
                        "minimum": 0,
                    },
                    "output_filename": NONEMPTY,
                }
            )
        ),
        "skipped": arr(
            obj(
                {
                    "story_id": NONEMPTY,
                    "title": NONEMPTY,
                    "production_slot": {"type": "integer", "minimum": 1},
                    "qc_status": {
                        "type": "string",
                        "enum": ["review", "blocked"],
                    },
                    "reason": NONEMPTY,
                }
            )
        ),
        "include_review": BOOL,
    }
)
STORY_RENDER_RECIPE_INDEX_SCHEMA["properties"][
    "junction_edit_index_sha256"
] = {
    "type": "string",
    "minLength": 64,
    "maxLength": 64,
}
STORY_RENDER_RECIPE_INDEX_SCHEMA["properties"][
    "include_auto_safe_review"
] = BOOL
STORY_RENDER_RECIPE_INDEX_SCHEMA["properties"]["recipes"]["items"][
    "properties"
]["junction_edit_plan_sha256"] = {
    "type": "string",
    "minLength": 64,
    "maxLength": 64,
}


STORY_RENDER_OUTPUT_SCHEMA = obj(
    {
        "story_id": NONEMPTY,
        "title": NONEMPTY,
        "production_slot": {"type": "integer", "minimum": 1},
        "recipe_path": NONEMPTY,
        "recipe_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "path": NONEMPTY,
        "sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "duration_seconds": {"type": "number", "minimum": 0},
        "size_bytes": {"type": "integer", "minimum": 1},
        "width": {"type": "integer", "const": 1080},
        "height": {"type": "integer", "const": 1920},
        "fps": {"type": "number", "minimum": 1},
        "video_codec": NONEMPTY,
        "audio_codec": NONEMPTY,
        "audio_sample_rate": {"type": "integer", "const": 48000},
        "audio_channels": {"type": "integer", "const": 2},
    }
)


STORY_RENDER_INDEX_SCHEMA = obj(
    {
        "schema_version": {"type": "string", "const": "1.0"},
        "method": {
            "type": "string",
            "const": "local-ffmpeg-story-render-v1",
        },
        "status": {
            "type": "string",
            "enum": ["complete", "partial", "failed"],
        },
        "recipe_index_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "recipe_count": {"type": "integer", "minimum": 0},
        "rendered_count": {"type": "integer", "minimum": 0},
        "failed_count": {"type": "integer", "minimum": 0},
        "skipped_story_count": {"type": "integer", "minimum": 0},
        "outputs": arr(STORY_RENDER_OUTPUT_SCHEMA),
        "failures": arr(
            obj(
                {
                    "story_id": NONEMPTY,
                    "reason": NONEMPTY,
                }
            )
        ),
    }
)


# Build SCHEMAS dict — registry and episode entries sourced from Pydantic v2 modules.
from autocut_core.schema.episode import episode_dict_schema as _episode_dict_schema
from autocut_core.schema.episode import chapter_dict_schema as _chapter_dict_schema

SCHEMAS = {
    "window_analysis": WINDOW_ANALYSIS_SCHEMA,
    "episode_digest": _episode_dict_schema(),
    "chapter_digest": _chapter_dict_schema(),
    "series_registry": _reg["SERIES_REGISTRY_SCHEMA"],
    "series_registry_relationship_repair": (
        _reg["SERIES_REGISTRY_RELATIONSHIP_REPAIR_SCHEMA"]
    ),
    "series_registry_identity_audit": _reg["SERIES_REGISTRY_IDENTITY_AUDIT_SCHEMA"],
    "series_assignment": _reg["SERIES_ASSIGNMENT_SCHEMA"],
    "series_bible": SERIES_BIBLE_SCHEMA,
    "story_catalog": STORY_CATALOG_SCHEMA,
    "story_portfolio": STORY_PORTFOLIO_SCHEMA,
    "story_treatment_options": STORY_TREATMENT_OPTIONS_SCHEMA,
    "story_script_draft": STORY_SCRIPT_DRAFT_SCHEMA,
    "story_script": STORY_SCRIPT_SCHEMA,
    "story_evidence_packet": STORY_EVIDENCE_PACKET_SCHEMA,
    "story_evidence_index": STORY_EVIDENCE_INDEX_SCHEMA,
    "span_candidate_bundle": SPAN_CANDIDATE_BUNDLE_SCHEMA,
    "span_candidate_index": SPAN_CANDIDATE_INDEX_SCHEMA,
    "story_plan_selection": STORY_PLAN_SELECTION_SCHEMA,
    "story_plan_orientation_fallback": (
        STORY_PLAN_ORIENTATION_FALLBACK_SCHEMA
    ),
    "story_plan": STORY_PLAN_SCHEMA,
    "story_plan_index": STORY_PLAN_INDEX_SCHEMA,
    "story_video_qc": STORY_VIDEO_QC_RESULT_SCHEMA,
    "story_qc_proxy_manifest": STORY_QC_PROXY_MANIFEST_SCHEMA,
    "story_qc_report": STORY_QC_REPORT_SCHEMA,
    "story_qc_index": STORY_QC_INDEX_SCHEMA,
    "story_render_recipe": STORY_RENDER_RECIPE_SCHEMA,
    "story_render_recipe_index": STORY_RENDER_RECIPE_INDEX_SCHEMA,
    "story_render_output": STORY_RENDER_OUTPUT_SCHEMA,
    "story_render_index": STORY_RENDER_INDEX_SCHEMA,
}


WINDOW_ANALYSIS_PROMPT_VERSION = "window-highlight-semantics-v1"
STORY_SCRIPT_HIGHLIGHT_SELECTION_PROMPT_VERSION = (
    "story-script-highlight-selection-v1"
)


WINDOW_HIGHLIGHT_DEFINITION_PROMPT = (
    "\n\n【Highlight（高光片段）识别合同】\n"
    "核心定义：Highlight 是按全剧、跨剧集一致的绝对尺度成立的情绪、剧情或视觉峰值，"
    "应具备明确释放或兑现、强吸引力和可记忆的传播点；不得因为某段只是当前窗口或本集"
    "最强，就自动判为 Highlight 或给高分。入选必须符合核心定义，并至少满足下列一类：\n"
    "1. 剧情节点：核心剧情发生颠覆性反转，核心秘密或事件真相完整揭晓，重点矛盾正式"
    "引爆，先果后因的打脸名场面完整成立，或严重后果明确落地。\n"
    "2. 情绪冲突：角色出现有台词、动作或反应支撑的极致情绪爆发，核心矛盾正面对决或"
    "显著升级，或出现足以引发气愤、吐槽、共情、争议等强反馈的话题性表达。\n"
    "3. 视觉感官：高强度肢体冲突、高难度动作、震撼场面、显著特效或其他明确的强视觉/"
    "感官冲击。\n"
    "绝对排除：普通闲聊、无冲突日常叙事、轻微情绪波动、空镜/转场/背景交代等纯铺垫；"
    "没有情绪爆发、剧情反转或冲突升级相伴的纯道具/屏幕证据静态特写；以及只抛悬念而"
    "没有实质情绪或剧情释放的内容。最后一类可以判为 Hook，但不得判为 Highlight。"
    "不要为了凑数量把普通内容伪装成边缘候选；当前窗口没有合格高光时 candidates 中"
    "可以没有 highlight。\n"
    "边界合同：Candidate 必须是单一连续区间。anchor 对准情绪峰值、反转发生或信息揭露的"
    "精准瞬间；start 回溯到承载该峰值的台词、动作或反应的自然起始停顿/语义起点，不得从"
    "台词或动作中段切入，也不得带入无关长铺垫；end 保留核心表达、可见兑现和必要即时反应/"
    "情绪回落的完整结束，不得在高潮尚未完成时截断，也不得拖入下一语义单元或无关长尾。"
    "若窗口边界内看不到完整起点或终点，不得把窗口边界冒充自然边界；保留相应 Story Beat/"
    "Visual Event 证据，但本窗口不输出这条 Highlight。"
    f"在自然完整边界内，优选 {TEASER_PREFERRED_MINIMUM_SECONDS:g}–"
    f"{TEASER_MAXIMUM_SECONDS:g} 秒作为可直接用于冷开场的 Highlight。"
    f"如果完整成立必须超过 {TEASER_MAXIMUM_SECONDS:g} 秒，保留自然完整范围，不得为了 "
    "Teaser 时长硬截；该 Candidate 仍是剧情/正文证据，但后续不会获得直接 Teaser 资格。\n"
    "strength 使用跨剧集一致的绝对标准。先在内部独立评估并求和，只输出最终 1–10 整数，"
    "不得新增评分子项字段：剧情重要性 0–3、情绪/视觉冲击 0–3、脱离上下文后的独立传播能力"
    " 0–2、相对常见短剧桥段的稀缺性 0–2。总分为 0 的普通内容不入选。"
    "10=极少数四维满档的标杆级高光；9=全剧核心级反转、真相揭晓、极致冲突或强视觉名场面，"
    "几乎无需上下文即可开场；8=非常强且独立传播能力强，但仍有一维未到标杆；"
    "7=核心矛盾显著升级或关键结果落地，具备较强开场吸引力；6=中等偏强，有清晰爆点，"
    "但依赖部分上下文或形式较常见；5=合格但普通；4=偏弱且需要较多铺垫；"
    "3=明显依赖上下文；2=仅有轻微冲突、反转或视觉刺激的边缘候选；"
    "1=仅略强于普通剧情的最低入选线，低于此线不得标记为 Highlight。"
    "只按当前窗口可观察证据评分；无法确认更高层剧情重要性时不得猜测其全剧地位。"
    "没有逐项证据时不得把分数集中在 8–10；同一集多个高光按绝对标准拉开差异，"
    "不按数量平均，也不为制造分布故意压低真实强片段。"
)


TASK_PROMPTS = {
    "window_analysis": (
        "直接理解提供的连续视频窗。完整提取剧情节拍、对白与屏幕文字、视觉事件、"
        "人物、因果、时间模式、窗口首尾状态以及 Highlight/Hook 候选。"
        "Hook 必须完整抛出问题但不能给出答案。"
        "story_beats、dialogue_and_text 和 visual_events 应保留完整句子、动作与反应；"
        "Highlight Candidate 是可编辑的连续语义句柄，不是完整 Event；前因和不属于"
        "必要即时兑现的其余内容继续保存在 story_beats / dialogue_and_text / "
        "visual_events 中。"
        + WINDOW_HIGHLIGHT_DEFINITION_PROMPT
        + "\n"
        "所有时间码必须使用输入声明的原视频绝对秒数。"
    ),
    "episode_digest": (
        "只根据输入的本集窗口证据、Event Card 和候选目录形成逐集摘要。"
        "逐字复制 episode、source_ids、window_ids 和已有 Event/Candidate ID。"
        "人物 key 和故事线 key 在本集内保持稳定；不得编造输入中不存在的事实。"
    ),
    "chapter_digest": (
        "把提供的连续若干集 Episode Digest 合并为分章摘要。"
        "保留人物状态、关系变化、故事线推进、事实和未解问题的证据 ID。"
        "当 Episode Digest 是本地合成且语义 rollup 为空时，必须直接使用输入 "
        "event_index 中的 Event Card 恢复人物、关系、故事线、事实和未解问题；"
        "所有证据 ID 必须来自 event_index。不得新增 Episode Digest 或 Event "
        "Card 中没有依据的剧情。"
    ),
    "series_registry": (
        "根据紧凑的全剧 Chapter Digest 与 Event 锚点建立全局 Registry。"
        "统一人物、别名、关系、事实、未解问题和 Story Thread 的稳定全局 ID，"
        "并把章节内不同 local thread_key 归并到真正同一条全局线。"
        "每个实体只引用输入中存在的真实 Event ID；无法消歧的人物身份进入 "
        "unresolved_identity_conflicts。不要生成逐集覆盖率或 Thread Beat。"
        "所有 character_id、Event ID、open_question_id 必须引用本次输出或输入"
        "中真实存在的对象；输出前逐项检查 story_threads.open_question_ids，"
        "不得引用未生成的 Question。"
        "\n\n"
        "【人物词表闭合】服装、发色、站位、镜头内临时外观或带“部分”限定的描述"
        "（例如“白衣女子”“红发男子”“紫裙女子(部分)”）不是稳定 alias；除非"
        "证据能唯一绑定同一人物，否则不得放入任何角色 aliases，应写入 "
        "unresolved_identity_conflicts。canonical_name 与 aliases 经标准化后"
        "必须全剧唯一，同一 alias 不能属于两个角色。"
        "\n"
        "【关系闭合】只有进入正式 story_threads 且具备持续证据的 "
        "entity_type=individual 人物必须至少出现在一条 relationships[].character_ids "
        "中；同一场戏被拆成多个 Event 不能单独把人物升级为全剧关系义务。"
        "关系必须由真实 Event "
        "支撑；无法确认时降低无依据的人物活动声明或保留身份冲突，不得编造关系。"
        "若某个无关系人物与既有人物共享唯一能力、固定伴侣、连续道具或明确身份"
        "揭示，优先将其视为待归并身份，不得创建重复人物后再虚构一条关系补洞。"
        "\n\n"
        "【P4-粒度硬要求 v2】story_threads 是**全剧贯穿的因果/关系/冲突主线**，"
        "不是逐段的子故事。目标数量：**每部剧 3-6 条主线**，不随集数线性放大——"
        "40 集短剧常见 3-5 条；60 集 4-6 条；80 集 4-8 条。"
        "\n"
        "拆分/合并的**第一判据是「能否收敛到单一 central 人物/关系/冲突」**："
        "如果两条候选 thread 共享同一中心人物且同一 central_question，合并；"
        "如果一条候选 thread 里有两个互不相干的 central_question，才拆。"
        "集号跨度只作次级 tiebreaker，不再是硬红线——"
        "贯穿全剧的男女主线、复仇线允许跨 30-40 集，不必强拆成前/中/后。"
        "\n"
        "每条 thread 应能在下游 catalog 派生 **≥2 个 story**（收尾/前置的 coda 类除外）。"
        "创建的每条 thread 必须能在后续 chapter assignment 中获得 **至少 5 个 Thread Beat**；"
        "beat 数 ≤2 的收尾类 thread 允许存在，但必须显式写 "
        "`thread_kind=coda`；普通主线固定写 `thread_kind=arc`。"
        "coda 只用于全剧末端的尾声、框架揭幕、杀青/全剧终或最终后果，不得把"
        "普通短支线、单次 reveal 或素材不足的 arc 标成 coda。"
        "`thread_kind=arc` 且 `status=resolved` 时，下游必须恢复 setup 与 payoff；"
        "`thread_kind=coda` 允许 1-2 个终局 Beat，但必须至少一个 Beat 的主导 phase"
        "为 `coda`。Event 细节可以包含 reveal，不能因此把整条尾声的结构 phase"
        "错误标成 reveal。"
        "\n"
        "**禁止把 thread 切得和下游 story 一样细**——如果全局 thread 数与预期 story 数接近"
        "（story 密度约每 4-7 集 1 个），说明 thread 切碎了，请合并同一 central 人物/关系的相邻 thread。"
        "portfolio 阶段会以 `catalog_thread_1to1_mapping` 硬阻断全局 1:1 映射。"
    ),
    "series_registry_relationship_repair": (
        "只修复输入指定的一个高活动人物的关系闭合缺口。Registry 已冻结，"
        "不得修改人物、别名、事实、问题或故事线。只能从 partner_candidates "
        "选择一个与 subject_character 共享真实 Event 的已注册人物，并根据 "
        "shared_events、每个 Event 的 evidence_sources 与 supporting_facts 判断 "
        "initial_state 和关系变化。state_changes 的 event_id 必须是该 partner "
        "的 shared_event_ids。若 repair_contract.requires_evidence_review=true，"
        "表示上一次响应无效，或在存在多条共现/Fact 强证据时过早判断无关系；"
        "必须逐项复核所有 shared_events 和 supporting_facts 后再决定。若复核后"
        "证据仍不足，输出 "
        "no_supported_relationship，且 partner_character_id、initial_state 和 "
        "state_changes 必须分别为空字符串、空字符串和空数组；不得猜测或编造关系。"
    ),
    "series_registry_identity_audit": (
        "只审核一个已经完成关系证据复核、但仍没有合法关系的高活动人物。"
        "先判断 subject_character 是否与 candidate_characters 中某个既有人物实为"
        "同一人；唯一能力、固定伴侣、连续道具、明确身份揭示和连续事件可以作为"
        "同一人证据，服装、发色、站位或泛化称谓不能单独作为合并依据。若两个"
        "canonical_name 在同一 Event 中明确同时出现，禁止合并。只有证据明确时"
        "输出 merge_with_existing_character，并选择一个候选 target；若 subject"
        "只是无法确认的泛化角色标签且所有证据均已由保留人物覆盖，可输出"
        "quarantine_unresolved_identity；其余情况必须输出 keep_blocking。不得"
        "发明人物、关系、事件或修改任何输入 ID。"
    ),
    "series_assignment": (
        "只处理输入指定的一个章节。使用全局 Registry 中已有的 thread_id，"
        "把该章每一集的关键剧情推进拆成 Thread Beat，并绑定本集真实 Event ID。"
        "每集必须至少有一个 Thread Beat；只有确属非叙事、纯回顾、占位损坏或证据不足时，"
        "才可进入 excluded_episodes 并给出类型化原因。不得新建全局人物、故事线或覆盖统计。"
        "required 表示删除后会破坏子故事因果、揭示或兑现；requires_beat_ids 只写真实前置 Beat。"
        "严格服从 Registry 的 `thread_kind`：arc 按普通 setup→payoff 因果线恢复；"
        "coda 只能生成 1-2 个 payoff/consequence/coda 终局 Beat，且全局至少一个"
        "必须使用 `phase=coda`。如果一个尾声 Beat 同时包含摄影棚揭幕、演员冲突"
        "和杀青，其 Event 摘要保留 reveal 细节，但主导结构 phase 必须是 coda。"
        "\n\n"
        "【P1-硬合同】beat.episode 一致性：\n"
        "每个 thread_beat 的 `episode` 字段必须等于其 `event_ids` 中所有 Event 的 episode。"
        "输入 event_index 的每个 event 已经明确写出 `episode` 字段——请逐个核对。"
        "若某个 beat 的叙事跨越多集（例如冲突从 ep3 铺垫到 ep5 兑现），"
        "**必须拆成多个 beat**（每集一个 beat，各自绑定该集的 event），"
        "不要用一个 beat.episode=3 却绑定 ep5 的 event。"
        "示例：\n"
        "  # 错误：beat.episode=3 但 event 来自 ep5\n"
        "  {\"id\":\"beat-ep3-property-destruction\", \"episode\":3, "
        "\"event_ids\":[\"event-e9aae5b9055d\" (from ep5)]}\n"
        "  # 正确（选项 A：按 event 的实际集号）：\n"
        "  {\"id\":\"beat-ep5-property-destruction\", \"episode\":5, "
        "\"event_ids\":[\"event-e9aae5b9055d\"]}\n"
        "  # 正确（选项 B：按集分拆成 2 个 beat）：\n"
        "  {\"id\":\"beat-ep3-setup\", \"episode\":3, "
        "\"event_ids\":[\"<ep3 event>\"]},\n"
        "  {\"id\":\"beat-ep5-climax\", \"episode\":5, "
        "\"event_ids\":[\"<ep5 event>\"], "
        "\"requires_beat_ids\":[\"beat-ep3-setup\"]}\n"
        "输出前自检：对每个 thread_beat，验证 event_ids 中所有 event 的 episode "
        "都等于 beat.episode。若不一致，改 beat.episode 或拆分 beat，不要改 event_ids。"
        "\n\n"
        "【P1-硬合同】requires_beat_ids 不得跨 thread：\n"
        "`requires_beat_ids` 只能引用**同一 thread_id 内**的前置 beat。"
        "跨 thread 的因果关系不是 beat 依赖，是 open_questions 层面的关联，"
        "不要在这里写。若你觉得某条依赖必须跨 thread 表达，说明这两条 thread "
        "应该合并成一条，请修改 registry；这里就删除该 requires 条目。"
        "\n\n"
        "【P1-硬合同】episode 覆盖完整性：\n"
        "章节内每一集**必须**要么在 thread_beats 中有 beat 引用它，"
        "要么在 excluded_episodes 中显式声明并选择 reason_type "
        "（non_narrative / recap_only / credits_or_placeholder / "
        "corrupted_or_unavailable / insufficient_evidence）。"
        "**禁止悄悄漏掉某一集**——本地校验器会拒绝任何未归账的 episode。"
        "输入 series_registry_admission 中列出的 quarantined_event_ids 绝不能"
        "进入 thread_beats 或普通 excluded_episodes。若某集全部已知 Event 都被"
        "隔离，请让本地编译器依据 quarantine_sha256 确定性补入 "
        "registry_quarantined_dependency；模型不得复制隔离 Event ID 或自行伪造"
        "该排除依据。"
        "\n\n"
        "【P0-整集归账互斥合同】：\n"
        "`excluded_episodes` 是**整集级**归账，不是未使用 Event 的收纳区。"
        "某集只要存在至少一个 Thread Beat，该集就绝不能再出现在 "
        "`excluded_episodes`，即使该集仍有未被 Beat 引用的次要 Event。"
        "未被 Beat 引用的 Event 无需归账，也不得为了容纳这些 Event 排除整集。"
        "\n"
        "错误示例：thread_beats 已含 episode=7 的主线 Beat，同时又因一个等待、"
        "过场或反应 Event 不值得单独建 Beat而写入 excluded_episodes[episode=7]。"
        "正确做法是保留 episode=7 的 Beat，并完全省略该 exclusion。"
        "\n"
        "输出前必须在内部自检：\n"
        "  assigned = {beat.episode for beat in thread_beats}\n"
        "  excluded = {item.episode for item in excluded_episodes}\n"
        "  assert assigned & excluded == set()\n"
        "  assert assigned | excluded == set(episodes)\n"
        "不要把 assigned/excluded 计算结果作为额外字段输出。"
    ),
    "series_bible": (
        "Series Bible v2 由本地确定性装配器生成，语义模型不得直接生成。"
    ),
    "story_catalog": (
        "从全剧 Bible 中发现能够承担相对独立观看单元的故事。"
        "发现所有有真实证据、局部 Payoff 和独立观看价值的候选，不设置数量配额，"
        "不得用近重复故事凑数，也不得为了固定数量删除真实完整故事。"
        "每个候选必须用 source_thread_beat_ids 定义连续子弧，并明确起点、终点和"
        "中间不可跳过的 required_bridge_beat_ids。"
        "故事必须有中心人物、中心冲突、必要背景、展开、局部 Payoff 和同线 Hook 可能性。"
        "高光只是故事入口，不能仅因刺激而定义故事。允许不同故事引用重叠 Event 或原片。"
        "\n\n"
        "【P4-粒度硬要求 v2】stories 目标数量：**每 4-7 集 1 个 story**。"
        "40 集短剧 → 6-10 个 story；54 集 → 8-14 个；80 集 → 12-20 个。"
        "每个 story：\n"
        "  - source_thread_beat_ids 长度 **5-12**（**不得超过 12**）\n"
        "  - estimated_source_seconds **300-700 秒**（**不得超过 900**）\n"
        "  - **1 条主线 thread 通常派生 2-3 个 story**。"
        "若某 thread 的 required beat ≥ 8，你**必须**在这条 thread 上拆出 ≥2 个独立 story，"
        "各覆盖 4-8 个连续 beat（例如按 setup / escalation / payoff 切）。\n"
        "  - 若某 thread 只产出 1 个 story，该 thread 的 required beat 应 ≤5 且形成单一紧凑弧；"
        "否则 portfolio 阶段会以 `wide_thread_underexploited` 警告。\n"
        "  - 不同 story 允许共享 3-5 个背景 beat 用于铺垫，但主体不能重叠。\n"
        "宁可产出更多短故事让运营挑选，也不要合成 1000+ 秒的巨型故事。"
        "**禁止直接把一条 thread 一比一映射成一个 story**——"
        "若整份 catalog 呈现 thread ≈ story 的全局 1:1 映射（diversity_ratio < 1.3 且 story ≥ 5），"
        "portfolio 阶段会以 `catalog_thread_1to1_mapping` 硬阻断。"
        "\n\n"
        "素材不足时如实标注 duration_feasibility=insufficient；"
        "不得用无功能内容填充。"
    ),
    "story_script_draft": (
        "针对一个 Story Catalog 候选编写证据化 Editorial Blueprint 草稿，不能只复述 Logline。"
        "每个 Beat 都要写可由画面、对白、动作、反应或屏幕文字观察到的 concrete_story_content，"
        "并把每一项不可缺失内容写入结构化 must_show，逐项绑定真实 Event 或 Fact。"
        "must_show 必须保持原子：一个 must_show 只表达一个可观察动作、对白、反应或屏幕文字义务；"
        "如果同一义务确实需要多个直接 Event 才成立，evidence_event_ids 必须完整列出全部 Event，"
        "下游按 AND 覆盖，不得把任一 Event 命中当成整项成立。"
        "优先让每个 Editorial Beat 由可独立剪辑的 direct Event 组成；若两个直接 Event 的物理范围"
        "相隔很远或跨多个 Timeline Segment，应拆成有因果顺序的多个 Beat，不得用一个巨大 Beat 包络。"
        "只有原片确属同一 Timeline Segment 的连续表演时才使用 continuity=continuous_scene，"
        "让本地 Preflight 标记 continuity_required；不得用 continuous_scene 掩盖可拆的跨场内容。"
        "Fact、Character、Relationship 和 whole Thread 扩展只用于 context recall，"
        "不能代替 direct Event 或进入 must-show 功能覆盖。"
        "retrieval_requirements.thread_beat_ids 继续承担功能覆盖与整体可用时长合同，"
        "但不会把该 Thread Beat 的全部 Event 注入单个 Editorial Beat 的原子物理包络；"
        "单 Beat 的物理压缩诊断只读取其 event_ids、must_show.evidence_event_ids、"
        "retrieval_requirements.event_ids 与显式 Candidate 的 Event/range。"
        "输入 direct_evidence_contract 已确定性列出 Thread Beat→合法 direct Event→"
        "原片 range→Timeline Segment 的映射；Editorial Beat 同时引用 Thread Beat 与"
        "direct Event 时，只能使用该 Thread Beat 的 allowed_direct_event_ids。"
        "direct_evidence_contract 只提供身份和物理原子单位，绝不代表本地代码已经生成"
        "最终 Editorial Beat；如何按因果顺序拆分仍必须由你完成。"
        "同时明确观众进入/离开 Beat 时的知识状态、因果角色、情绪变化、剧透边界和下游检索条件。"
        "逐字遵守输入 thread_beat_contract：required Thread Beat 不得省略，"
        "每个 selected Thread Beat 必须被至少一个 Editorial Beat 的 retrieval_requirements 引用；"
        "未选择的可选 Thread Beat 必须逐项写入 omitted_thread_beats 并说明原因。"
        "使用 teaser_intent、orientation、setup、escalation、turn_or_reveal、payoff、end_hook 等 role；"
        "允许 beats 数量为 4–11（1 个 teaser_intent + 3–10 个 body beat）；"
        "escalation 可以出现多次（例如 escalation×2 或 escalation + turn_or_reveal + reveal）。"
        "Plan 阶段不再要求最短时长；请优先讲清一个完整故事，不要为了拼时长增加无功能 Beat。"
        "故事必须形成原因、升级、关键变化和局部 Payoff；Hook 非必需，确无合法 Hook 时"
        "把 ending_hook_intent.may_be_empty 置 true 并省略 end_hook Beat。"
        "先读取 story_treatment：逐字复制 primary_story_thread_id 与 "
        "treatment_options_sha256，并从 options 中选择且只选择一个 "
        "treatment_option_id；strategy、mode 与 reprise_policy 必须与该 Option 完全一致。"
        "每个 Beat 必须声明 thread_role：主线 Beat 用 primary；只为主线补充前因、关系、"
        "赌注或情绪兑现的次线用 integrated_support；不共享主线人物且可独立成立的内容用 "
        "independent_secondary。独立次线不得承担 escalation、turn_or_reveal、payoff 或 end_hook，"
        "Payoff/Hook 必须回到 primary_story_thread_id。"
        "chronological_compression 从 mainline 正文开始，不生成 teaser_intent；"
        "cold_open_no_reprise 与 cold_open_delayed_reprise 必须以 future_preview "
        "teaser_intent 起手，并指定唯一 primary_highlight_candidate_id。"
        "选择 primary Highlight 时，必须逐一比较所选 Treatment Option 的 "
        "eligible_highlight_candidate_ids 中全部 Candidate；不得按 Candidate ID、"
        "输入顺序或动态 Schema enum 顺序默认选择。strength 是强信号而非硬门槛；"
        "综合优先选择更贴合 primary_story_thread_id 与本 Story 核心承诺、开场即时"
        "冲击更强、脱离前文仍可理解，且冲突、反转、揭示或后果兑现更完整的 Candidate。"
        "还必须评估 reprise_policy：若 no_reprise 会使该 Candidate 原片不再出现在正文，"
        "不得为了开场刺激拿走正文不可替代的主线 Payoff。selection_reason 必须说明"
        "本次 Candidate 间的具体比较，不得照抄或泛化复述 Treatment.selection_basis。"
        "若所选 Candidate 的 strength 低于 eligible 集合中的最高 strength，"
        "selection_reason 必须明确写出所选 Candidate ID、最高 strength 备选 ID、"
        "两者的 strength，以及决定胜负的 Story/Treatment 具体取舍。"
        "teaser_contract.explanation_beat_ids 与 reprise_beat_ids 只能填写本次输出"
        "顶层 beats[].id 中的 Editorial Beat ID；不能填写 Series Bible/Context "
        "中的 Thread Beat ID。Thread Beat ID 只允许出现在 "
        "selected_thread_beat_ids、required_thread_beat_ids、omitted_thread_beats "
        "和 retrieval_requirements.thread_beat_ids。"
        "Teaser 的 candidate_suggestions 必须严格等于只含该 Candidate 的单元素数组，"
        "must_show 允许落在该 Candidate 时间窗 ±5 秒的 stitch 窗内（Span Candidate Compiler 会自动"
        "用同源相邻候选合成 atomic Span），但禁止跨集、跨源。"
        "no_reprise 必须声明 explanation_beat_ids、保持 reprise_beat_ids 为空，正文不得物理重放"
        "开场原片；delayed_reprise 必须声明 explanation_beat_ids 与 reprise_beat_ids，且在全部"
        "前因解释完成并至少推进一次主线后才可重放，高光重放还必须通过 reprise_function "
        "说明新增叙事功能。Teaser 最终只能编译为一个连续 Span/Clip，"
        f"优先 {TEASER_PREFERRED_MINIMUM_SECONDS:g}–"
        f"{TEASER_MAXIMUM_SECONDS:g} 秒，硬上限 "
        f"{TEASER_MAXIMUM_SECONDS:g} 秒。"
        "没有合法未来高光时选择 compiled chronological_compression Option，不得伪造 Candidate。"
        "只引用输入中存在的 ID，不写自由时间码，不编造对白、旁白、动作或事实。"
        "所有 evidence_event_ids 必须覆盖 Beat 与 must_show 的全部直接证据 Event。"
        "逐字复制上下文中的 portfolio_binding，不能自行更换生产槽位。"
        "status 固定为 draft；素材时长和 evidence_status 由后续本地预检计算，模型不得臆测。"
        "\n\n"
        "【P2-硬合同】以下 6 条约束是硬性的，输出前必须逐条自检；违反任一条都会被本地 "
        "validator 拒绝，且下游 Preflight/Evidence/Plan 全部失效：\n"
        "1. `retrieval_requirements.thread_beat_ids` 必须非空：每一个 Editorial Beat 都"
        "必须在 retrieval_requirements.thread_beat_ids 里列出该 beat 承担的所有 "
        "Thread Beat ID。空数组 [] 是非法的。全部 beats 的 thread_beat_ids 并集必须"
        "**恰好等于** selected_thread_beat_ids 集合。\n"
        "2. required Thread Beat 不得进 omitted：任何 importance='required' 的 Thread "
        "Beat 都**必须**出现在 selected_thread_beat_ids 中，**不能**进 omitted_thread_beats。"
        "若你觉得某条 required beat 是 'redundant' 或 'out_of_scope'，说明 Story "
        "Catalog 选错了子弧——请仍然把它 select 并挂到某个 script beat 上，或者"
        "在 required_bridge_beat_ids 说明层面把它切掉；这里不能 omit。\n"
        "3. candidate_suggestions 与 retrieval_requirements.candidate_ids 只能引用"
        "上下文 candidate_catalog 中真实存在的 ID。**禁止发明新 candidate ID**"
        "（例如 `candidate-hook-confrontation_cliffhanger` 这种自造名称）。找不到匹配"
        "候选就留空数组。role=`end_hook` 的 candidate_suggestions 与 "
        "retrieval_requirements.candidate_ids，以及顶层 ending_hook_intent."
        "candidate_ids，进一步只能引用 type/kind=`hook` 或 allowed_roles 含 hook "
        "的 Candidate；不得把 highlight 当作 hook。当前 Story 没有合法 Hook Candidate "
        "时，必须设置 ending_hook_intent.may_be_empty=true，并省略 end_hook Beat，"
        "让最后一个 Beat 保持 payoff。\n"
        "4. beats[-1].role 必须是 `end_hook`，除非 story.ending_hook_intent."
        "may_be_empty=true（此时最后一 beat 可以是 payoff，且不含 end_hook）。"
        "**禁止**用 payoff 作最后一 beat 而 may_be_empty 又是 false。\n"
        "5. required_fact_ids 里的每个 fact 都**必须**出现在**恰好一个** beat 的 "
        "introduced_fact_ids 中。若你无法承诺哪个 beat 引入这条 fact，请把它"
        "从 required_fact_ids 里删掉，或搬进 intentional_mystery_fact_ids。\n"
        "6. Treatment 重放：cold_open_no_reprise 的 reprise_beat_ids 必须为空；"
        "cold_open_delayed_reprise 的 reprise_beat_ids 必须至少包含一个后置 Beat，"
        "这里的 Beat ID 必须逐字等于同一响应中某个 beats[].id，绝不是 Thread Beat ID；"
        "且全部 explanation_beat_ids 位于其前，二者之间至少包含 "
        "reprise_delay_minimum_progression_beats 个主线推进 Beat；"
        "chronological_compression 不得生成 teaser_intent。\n"
        "输出前的自检伪代码：\n"
        "  assert set().union(*[b.retrieval_requirements.thread_beat_ids for b in beats]) "
        "== set(selected_thread_beat_ids)\n"
        "  assert set(required_thread_beat_ids) <= set(selected_thread_beat_ids)\n"
        "  for beat in beats: assert all(c in candidate_catalog_ids for c in "
        "beat.candidate_suggestions)\n"
        "  assert beats[-1].role == 'end_hook' or "
        "story.ending_hook_intent.may_be_empty is True\n"
        "  introduced = {f for b in beats for f in b.introduced_fact_ids}\n"
        "  assert set(required_fact_ids) <= introduced\n"
        "  assert teaser_contract.treatment_option_id in "
        "[o.treatment_option_id for o in story_treatment.options]\n"
    ),
    "story_plan_selection": (
        "把已批准 Story Script 映射为 Block 顺序。禁止自由组合 Beat/Span、"
        "禁止输出 source_id/episode/时间码/时长/Event/Candidate/边界状态——这些字段全部"
        "由后续本地代码按 Option ID 展开。首 Block 的 role、start 时间关系、"
        "reuse_mode=none、无定向由编译器固定，不需要模型填写。"
        "\n\n"
        "按以下顺序决策：\n"
        "步骤 1｜body finalist：编译器已按对白/动作边界、直接 must-show "
        "Event 功能边界紧致度、同源因果 gap、重复预算、整集率和"
        "编辑密度保留最多 3 个 legal_body_partitions。功能证据覆盖和"
        "非功能 head/tail slack 优先于 20–40 秒中位数与目标时长。"
        "存在 finalist_proxy_comparison 时，按其中每个 partition_id 对应的"
        " proxy_start/proxy_end 观看局部代理，选择因果完整、反应有触发、删减"
        "自然且不拖沓的一项；只能逐字复制 finalist partition_id，不得自由组合。\n"
        "步骤 2｜复用与重叠：允许同一 `span_candidate_id` 出现在多个 Block 中"
        "（Teaser↔正文 或 正文↔正文），同 Block 内部不得复用；本地物化会为"
        "有源片重叠的 Clip 打 reuse_mode=teaser_reprise，并把该复用时长计入"
        "重复预算。最终只执行播放时长 10% 硬合同；60 秒仅作 DFS 防爆剪枝。\n"
        "步骤 3｜时长：不再校验最短时长，只强制 ≤1200 秒硬上限。"
        "优先讲清故事，不为凑时长增加无功能 Span。\n"
        "步骤 4｜整集比：全部 body 序列的 full_source_like_clip_count 相加 ≤ 1，"
        "且整集播放占比 ≤ 50%。当存在同覆盖的非整集替代时不得选整集。\n"
        "步骤 5｜功能边界：优先保留直接 must-show Event 完整证据，"
        "再减少 functional_boundary_metrics.nonfunctional_slack_seconds；"
        "不得为了时长或中位数把前后无关 Event 包进 Span。"
        "同一 Thread Beat 不等于同一原子因果单元；直接 Event/must-show "
        "不同时，不得为了让原片时码连续而强行保留中间剧情。\n"
        "步骤 6｜编辑密度：在功能边界等价时，优先让 clip 数 ≥ "
        "planning_contract.editorial_density.preferred_minimum_clip_count、"
        "clip 中位时长落在 20–40 秒。达不到时在 planning_risks 里写 "
        "editorial_density_below_target。\n"
        "步骤 7｜偏好目标：先保证边界与因果，再比较冗余、节奏和目标时长；"
        "不得因为更短而选择有半句或无触发反应的版本。\n"
        "步骤 8｜Composite finalist：只能从动态 Schema 的 finalist.anyOf"
        " 分支中完整选择一支；body_partition_id、Teaser option_id 和"
        " body_block_orientations 数量属于同一个原子组合，禁止跨分支混配。\n"
        "步骤 9｜时间关系与观众定向：body_blocks 数组顺序即播放顺序。"
        "非线性跳转必须声明 temporal_relation_from_previous 与 orientation_required=true"
        "+ 对白/画面/标题卡锚点；顺叙沿用 continuation + orientation_required=false + "
        "orientation_strategy=none。局部 Teaser 与正文重叠时，本地会自动声明 teaser_reprise。\n"
        "步骤 10｜selection_reason：每个 option 的 selection_reason 请写清"
        "命中了 1–6 的哪几条硬门槛（比如 duration_meets_minimum、no_full_source_like、"
        "no_repeat）与该 option 的编辑功能，避免套话。\n"
        "\n"
        "不再计算 insufficient_editorial_surplus / 编辑余量比例。"
        "禁止靠重复或整集拼接填长；宁可故事总时长偏短，也不要塞入无功能片段。"
    ),
    "story_plan_orientation_fallback": (
        "本次每个 Plan Candidate 的 Teaser 和正文 Partition 已由本地编译器"
        "锁定，禁止选择、淘汰、改写或比较 Candidate。只为"
        " orientation_fallback_contract.candidates 中列出的歧义 Candidate"
        "逐项填写正文 Block 的时间关系与观众定向。Candidate ID 必须逐字复制；"
        "数组长度必须与各自锁定 Partition 的 segment_count 完全一致。"
        "正向连续叙事使用 continuation/false/none；明显倒序进入上下文使用"
        " flashback_context；返回原主线使用 return_to_mainline；未来预演和并行"
        "线分别使用 preview_future、parallel。所有非 continuation 关系必须"
        " orientation_required=true 且选择输入证据实际支持的 dialogue_anchor、"
        "visual_anchor 或 title_card，不得虚构不存在的提示。"
    ),
    "story_video_qc": (
        "直接观看提供的 Story QC 代理视频，并只根据画面、对白、动作、反应、"
        "屏幕文字和输入的 Story Plan/Script 上下文做判断。review_kind=story_flow "
        "时检查 Story Coverage、非线性叙事 Flow 和成片中画面可见的动作/反应切点问题；"
        "review_kind=boundary_start 或 boundary_end 时，只检查声明 cut_at_seconds "
        "处的词音节、对白、动作、反应和音频边界，coverage/flow 必须写 "
        "not_assessed；review_kind=junction 时只检查短 handle 中眼前切点的叙事连接、"
        "人物/时空定向、画面动作和反应连续性，coverage 必须写 not_assessed。"
        "Junction 不含完整 Clip/Beat，不得判断 must-show、Payoff 或 Teaser reprise"
        " 是否完整出现，也不得输出 coverage_missing、must_show_absent、"
        "payoff_absent、teaser_reprise_missing，或把局部未展示改写为 thread_broken；"
        "完整 Coverage 只由 story_flow 负责。若上下文写明 "
        "local_audio_boundary_authoritative=true，不得判断吞字、词音节或说话切断，"
        "这些项目由本地 Demucs + 原始混音/人声双路 Silero VAD 门禁判定。"
        "不得根据未展示的原片猜测剧情，"
        "不得建议编造对白或旁白。可安全接受写 pass；需要人工判断写 review；"
        "story_flow 中明确缺失 must-have、Payoff、发生剧透或切断关键动作写 block；"
        "Junction 只有切点本身造成无法消解的局部矛盾才可 block。"
        "所有 finding 时间均使用当前代理视频从 0 开始的秒数。"
    ),
}


def response_format(
    task: str,
    *,
    schema_override: dict[str, Any] | None = None,
    revision_override: str | None = None,
) -> dict[str, Any]:
    try:
        schema = schema_override if schema_override is not None else SCHEMAS[task]
    except KeyError as exc:
        raise ValueError(f"no response schema for task {task!r}") from exc
    revision = revision_override or {
        "series_registry": "v4_typed_coda",
        "series_assignment": "v5_typed_coda",
        "series_bible": "v3_typed_coda",
        "story_catalog": "v3",
        "story_script_draft": "v3",
        "story_plan_selection": "v7",
        "story_plan_orientation_fallback": "v1",
    }.get(task, "v1")
    return {
        "type": "json_schema",
        "json_schema": {
            "name": f"story_first_{task}_{revision}",
            "strict": True,
            "schema": schema,
        },
    }


def task_prompt(task: str) -> str:
    try:
        return TASK_PROMPTS[task]
    except KeyError as exc:
        raise ValueError(f"no prompt for task {task!r}") from exc


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def validate_schema(
    value: Any, schema: dict[str, Any], *, where: str = "$"
) -> list[str]:
    """Validate the strict schema subset emitted by this module."""
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list) and alternatives:
        branch_errors = [
            validate_schema(value, branch, where=where)
            for branch in alternatives
            if isinstance(branch, dict)
        ]
        if any(not errors for errors in branch_errors):
            return []
        if branch_errors:
            best_errors = min(branch_errors, key=len)
            return [f"{where}: did not match anyOf"] + best_errors
        return [f"{where}: anyOf contains no schema branches"]

    errors: list[str] = []
    expected = schema.get("type")
    if isinstance(expected, str) and not _matches_type(value, expected):
        return [f"{where}: expected {expected}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: expected constant {schema['const']!r}")
    allowed = schema.get("enum")
    if isinstance(allowed, list) and value not in allowed:
        errors.append(f"{where}: expected one of {allowed!r}")
    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            errors.append(f"{where}: string shorter than {minimum_length}")
        maximum_length = schema.get("maxLength")
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            errors.append(f"{where}: string longer than {maximum_length}")
        # P5: regex pattern check. Qwen API strict mode already enforces this,
        # but local validators (assemble_story_artifacts, run_semantic_batch
        # cache_hit revalidation, tests) must reject the same input.
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            import re as _re

            if not _re.match(pattern, value):
                errors.append(
                    f"{where}: string does not match pattern {pattern!r}"
                )
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{where}: value below minimum {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{where}: value above maximum {maximum}")
    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            errors.append(f"{where}: expected at least {minimum_items} items")
        maximum_items = schema.get("maxItems")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            errors.append(f"{where}: expected at most {maximum_items} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(
                    validate_schema(item, item_schema, where=f"{where}[{index}]")
                )
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    errors.append(f"{where}: missing required property {key!r}")
        if schema.get("additionalProperties") is False and isinstance(properties, dict):
            unknown = sorted(set(value) - set(properties))
            if unknown:
                errors.append(f"{where}: unknown properties {unknown}")
        if isinstance(properties, dict):
            for key, item in value.items():
                child = properties.get(key)
                if isinstance(child, dict):
                    errors.extend(
                        validate_schema(item, child, where=f"{where}.{key}")
                    )
    return errors


def validate_task_response(task: str, value: Any) -> list[str]:
    try:
        schema = SCHEMAS[task]
    except KeyError as exc:
        raise ValueError(f"no response schema for task {task!r}") from exc
    return validate_schema(value, schema)


