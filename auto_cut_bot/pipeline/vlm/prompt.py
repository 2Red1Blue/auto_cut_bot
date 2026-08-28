"""Versioned prompt pack for provenance-bound VLM Semantic Pack v3."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import hashlib
import json

from autocut_kernel.vlm import WindowManifest

VLM_PROMPT_VERSION = "vlm-semantic-pack-v3"

VLM_PROMPT_TEMPLATE = (
    "你是 Story-first 视频窗口语义分析器。只根据当前窗口内确实可见、可听且可由给定帧佐证的内容输出局部语义包；"
    "不得借用语音识别、语音活动检测、外部接口、预写文本、文字识别或字幕轨道作为语义权威，也不得臆造身份、关系、对白或因果。\n"
    "先完整记录所有对叙事有意义的可见事实，再组织事件。window_summary 必须引用已声明 fact/event 并给出 dominant_temporal_mode；"
    "continuity 必须明确窗口是否从上一窗口延续、是否延续到下一窗口、是否从事件中段开始或在事件中段结束，"
    "entry_state_fact_refs 与 exit_state_fact_refs 只引用已声明事实。temporal_segments 只描述可见的 present/flashback/flashforward/dream/unknown，证据不足用 unknown。\n"
    "所有 ID 都是本窗口局部 ID，按字典序输出且不可重复。entity_kind 只能取 person,object,location,screen_text_source。"
    "fact_kind 只能取 visible_presence,visible_state,visible_action,visible_change,visible_relation,scene_context,"
    "character_appearance,screen_text,temporal_mode；subject_ref/object_ref 只引用已声明实体。"
    "event_kind 只能取 action,interaction,state_change,reaction,reveal,transition；每个 event 至少引用一条 fact，"
    "participant_refs 只引用实体。cause_event_refs/effect_event_refs 只表达画面明确支持的因果与反应，证据不足时为空。\n"
    "每个 support 必须使用 proxy 时钟整数 PTS 的半开区间 [start_pts,end_pts)，位于给定范围内且 start_pts 小于 end_pts；"
    "uncertainty_pts 是非负整数保守误差。support 至少引用一个 allowlist frame_id，frame_id 必须逐字复制完整的 sha256: 加 64 位小写十六进制字符（共 71 字符）。"
    "所有 confidence 以及 measurement 的 value 必须是 0 到 1 的 canonical 十进制字符串，禁止 JSON 浮点数。\n"
    "candidate_hypotheses 只是叙事候选，不是剪辑决定。普通闲聊、过场、铺垫或证据不足时必须输出空数组。"
    "只有达到绝对标准才可输出：candidate_kind=hook 必须提出具体且尚未回答的 open_question，payoff_event_refs 必须为空；"
    "candidate_kind=highlight 必须引用本窗口已经发生的非空 payoff_event_refs。不得因为它是窗口内相对最强片段就降低标准。"
    "每个候选必须给出 anchor_event_ref、reason、anchor_summary、payoff_or_open_question 及至少一条 measurement。"
    "editing_modes 必须是 canonical 非空 ['dialogue']、['action'] 或按此顺序的 ['dialogue','action']。"
    "narrative_functions 按 hook,setup,escalation,confrontation,reveal,reversal,payoff,aftermath 的顺序去重且非空。"
    "tags 按 dialogue,action,emotion,suspense,conflict,reveal,reversal,visual_spectacle,character_moment,relationship_moment 的顺序去重且非空。"
    "measurement_kind 只能取 hook_strength,reveal_strength,emotional_payoff_strength,dialogue_salience,action_salience,visual_salience；"
    "每条 measurement 必须引用至少一个已有 fact 或 event。dialogue_excerpt 仅作窗口内对话语义的简短描述；"
    "它不是逐字记录，也不能证明任何时间边界；不适用时为 null。\n"
    "所有时间只是粗粒度语义支持，绝不输出源时间、精确媒体端点、提前量或任何物理剪辑位置。"
    "只输出符合所给 JSON Schema 的单个 JSON 对象，不要 Markdown、解释或代码围栏。\n"
    "窗口证据："
)

# V3 is an immutable replay contract and remains the default. Only an explicit
# installed policy may select the compact representation below.
VLM_COMPACT_PROMPT_VERSION = "vlm-semantic-pack-v4-compact"
VLM_COMPACT_PROMPT_TEMPLATE = (
    "你是 Story-first 视频窗口语义分析器。只根据当前窗口内确实可见、可听且可由给定帧佐证的内容输出局部语义包。"
    "输出完整的单行紧凑 JSON：不缩进，不在字符串外添加空格、制表符或换行；不要 Markdown、解释、代码围栏或工作过程。"
    "只压缩表达，不得省略 schema 必填字段、独立事实、事件、必要证据或引用。文本字段用准确简短的短句；"
    "同一事实只记录一次并通过引用复用，不在不同字段反复展开相同描述；不同时间的独立变化不能为去重而合并。\n"
    "禁止以独立的语音识别、语音活动检测、独立 OCR、字幕轨道、外部接口或预写文本作为语义权威。"
    "允许直接观察随附视频画面中可见的文字并记录为 screen_text，文字载体可记为 screen_text_source；"
    "画面文字的存在不证明其所述动作、身份、关系或对白真实发生。不得臆造身份、关系、对白或因果。\n"
    "完整记录所有对叙事有意义的可见事实并组织事件，不为凑数量重复描述。window_summary 必须引用已声明 fact/event 并给出 dominant_temporal_mode；"
    "continuity 必须明确窗口是否从上一窗口延续、是否延续到下一窗口、是否从事件中段开始或在事件中段结束，"
    "entry_state_fact_refs 与 exit_state_fact_refs 只引用已声明事实。temporal_segments 只描述可见的 present/flashback/flashforward/dream/unknown，证据不足用 unknown。\n"
    "所有 ID 都是本窗口局部 ID，按字典序输出且不可重复。entity_kind 只能取 person,object,location,screen_text_source。"
    "fact_kind 只能取 visible_presence,visible_state,visible_action,visible_change,visible_relation,scene_context,"
    "character_appearance,screen_text,temporal_mode；subject_ref/object_ref 只引用已声明实体。"
    "event_kind 只能取 action,interaction,state_change,reaction,reveal,transition；每个 event 至少引用一条 fact，"
    "participant_refs 只引用实体。cause_event_refs/effect_event_refs 只表达画面明确支持的因果与反应，证据不足时为空。\n"
    "每个 support 必须使用 proxy 时钟整数 PTS 的半开区间 [start_pts,end_pts)，位于给定范围内且 start_pts 小于 end_pts。"
    "proxy_time_base 的 numerator/denominator 是每 tick 的秒数；输出仍是整数 PTS，不得把秒或毫秒直接当作 PTS。"
    "uncertainty_pts 是非负整数保守误差。supporting_frame_ids 使用最小充分的必要证据帧集合，至少一帧；"
    "不列无关帧，但不得为缩短输出删除证明变化所必需的多帧证据。frame_id 必须逐字复制 allowlist 中完整的 sha256: 加 64 位小写十六进制字符（共 71 字符）。"
    "所有 confidence 以及 measurement 的 value 必须是 0 到 1 的 canonical 十进制字符串，禁止 JSON 浮点数。\n"
    "candidate_hypotheses 只是叙事候选，不是剪辑决定。普通闲聊、过场、铺垫或证据不足时必须输出空数组。"
    "只有达到绝对标准才可输出：candidate_kind=hook 必须提出具体且尚未回答的 open_question，payoff_event_refs 必须为空；"
    "candidate_kind=highlight 必须引用本窗口已经发生的非空 payoff_event_refs。不得因为它是窗口内相对最强片段就降低标准。"
    "每个候选必须给出 anchor_event_ref、reason、anchor_summary、payoff_or_open_question 及至少一条 measurement。"
    "editing_modes 必须是 canonical 非空 ['dialogue']、['action'] 或按此顺序的 ['dialogue','action']。"
    "narrative_functions 按 hook,setup,escalation,confrontation,reveal,reversal,payoff,aftermath 的顺序去重且非空。"
    "tags 按 dialogue,action,emotion,suspense,conflict,reveal,reversal,visual_spectacle,character_moment,relationship_moment 的顺序去重且非空。"
    "measurement_kind 只能取 hook_strength,reveal_strength,emotional_payoff_strength,dialogue_salience,action_salience,visual_salience；"
    "每条 measurement 必须引用至少一个已有 fact 或 event。dialogue_excerpt 仅作窗口内对话语义的简短描述；"
    "它不是逐字记录，也不能证明任何时间边界；不适用时为 null。\n"
    "所有时间只是粗粒度语义支持，绝不输出源时间、精确媒体端点、提前量或任何物理剪辑位置。"
    "必须完成整个符合所给 JSON Schema 的单个 JSON 对象；不得只输出前几个数组，也不得省略 window_summary、continuity 或 candidate_hypotheses。\n"
    "窗口证据："
)

_SHA256_SCHEMA: dict[str, object] = {
    "type": "string",
    "pattern": "^sha256:[0-9a-f]{64}$",
}
_DECIMAL_0_TO_1_SCHEMA: dict[str, object] = {
    "type": "string",
    "pattern": "^(?:0|0\\.[0-9]+|1|1\\.0+)$",
}
_TEXT_SCHEMA: dict[str, object] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 512,
}
_NULLABLE_TEXT_SCHEMA: dict[str, object] = {
    "anyOf": [_TEXT_SCHEMA, {"type": "null"}],
}
_LOCAL_ID_SCHEMA: dict[str, object] = {
    "type": "string",
    "pattern": "^[a-z][a-z0-9_]{0,63}$",
}
_LOCAL_REF_LIST_SCHEMA: dict[str, object] = {
    "type": "array",
    "uniqueItems": True,
    "items": _LOCAL_ID_SCHEMA,
}
_TEMPORAL_MODE_SCHEMA: dict[str, object] = {
    "type": "string",
    "enum": ["present", "flashback", "flashforward", "dream", "unknown"],
}

_PROXY_INTERVAL_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["start_pts", "end_pts", "uncertainty_pts"],
    "properties": {
        "start_pts": {"type": "integer"},
        "end_pts": {"type": "integer"},
        "uncertainty_pts": {"type": "integer", "minimum": 0},
    },
}
_FRAME_IDS_SCHEMA: dict[str, object] = {
    "type": "array",
    "minItems": 1,
    "uniqueItems": True,
    "items": _SHA256_SCHEMA,
}

_SUPPORT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["confidence", "proxy_interval", "supporting_frame_ids"],
    "properties": {
        "confidence": _DECIMAL_0_TO_1_SCHEMA,
        "proxy_interval": _PROXY_INTERVAL_SCHEMA,
        "supporting_frame_ids": _FRAME_IDS_SCHEMA,
    },
}

_MEASUREMENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["confidence", "event_refs", "fact_refs", "measurement_kind", "value"],
    "properties": {
        "confidence": _DECIMAL_0_TO_1_SCHEMA,
        "event_refs": _LOCAL_REF_LIST_SCHEMA,
        "fact_refs": _LOCAL_REF_LIST_SCHEMA,
        "measurement_kind": {
            "type": "string",
            "enum": [
                "hook_strength",
                "reveal_strength",
                "emotional_payoff_strength",
                "dialogue_salience",
                "action_salience",
                "visual_salience",
            ],
        },
        "value": _DECIMAL_0_TO_1_SCHEMA,
    },
    "anyOf": [
        {"properties": {"fact_refs": {"minItems": 1}}},
        {"properties": {"event_refs": {"minItems": 1}}},
    ],
}

_EDITING_MODES_SCHEMA: dict[str, object] = {
    "oneOf": [
        {"const": ["dialogue"]},
        {"const": ["action"]},
        {"const": ["dialogue", "action"]},
    ]
}

VLM_RESPONSE_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "candidate_hypotheses",
        "continuity",
        "entities",
        "events",
        "facts",
        "schema_version",
        "window_summary",
    ],
    "properties": {
        "schema_version": {"const": 3, "type": "integer"},
        "window_summary": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "confidence",
                "dominant_temporal_mode",
                "event_refs",
                "fact_refs",
                "summary",
            ],
            "properties": {
                "confidence": _DECIMAL_0_TO_1_SCHEMA,
                "dominant_temporal_mode": _TEMPORAL_MODE_SCHEMA,
                "event_refs": _LOCAL_REF_LIST_SCHEMA,
                "fact_refs": _LOCAL_REF_LIST_SCHEMA,
                "summary": _TEXT_SCHEMA,
            },
        },
        "continuity": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "continues_from_previous",
                "continues_into_next",
                "ends_mid_event",
                "entry_state_fact_refs",
                "exit_state_fact_refs",
                "starts_mid_event",
                "temporal_segments",
            ],
            "properties": {
                "continues_from_previous": {"type": "boolean"},
                "continues_into_next": {"type": "boolean"},
                "ends_mid_event": {"type": "boolean"},
                "entry_state_fact_refs": _LOCAL_REF_LIST_SCHEMA,
                "exit_state_fact_refs": _LOCAL_REF_LIST_SCHEMA,
                "starts_mid_event": {"type": "boolean"},
                "temporal_segments": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "confidence",
                            "mode",
                            "proxy_interval",
                            "summary",
                            "supporting_frame_ids",
                        ],
                        "properties": {
                            "confidence": _DECIMAL_0_TO_1_SCHEMA,
                            "mode": _TEMPORAL_MODE_SCHEMA,
                            "proxy_interval": _PROXY_INTERVAL_SCHEMA,
                            "summary": _TEXT_SCHEMA,
                            "supporting_frame_ids": _FRAME_IDS_SCHEMA,
                        },
                    },
                },
            },
        },
        "entities": {
            "type": "array",
            "maxItems": 24,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "display_label",
                    "entity_kind",
                    "local_entity_id",
                    "support",
                    "visual_description",
                ],
                "properties": {
                    "display_label": _TEXT_SCHEMA,
                    "entity_kind": {
                        "type": "string",
                        "enum": ["person", "object", "location", "screen_text_source"],
                    },
                    "local_entity_id": _LOCAL_ID_SCHEMA,
                    "support": _SUPPORT_SCHEMA,
                    "visual_description": _TEXT_SCHEMA,
                },
            },
        },
        "facts": {
            "type": "array",
            "minItems": 1,
            "maxItems": 48,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "fact_kind",
                    "local_fact_id",
                    "object_ref",
                    "subject_ref",
                    "summary",
                    "support",
                ],
                "properties": {
                    "fact_kind": {
                        "type": "string",
                        "enum": [
                            "visible_presence",
                            "visible_state",
                            "visible_action",
                            "visible_change",
                            "visible_relation",
                            "scene_context",
                            "character_appearance",
                            "screen_text",
                            "temporal_mode",
                        ],
                    },
                    "local_fact_id": _LOCAL_ID_SCHEMA,
                    "object_ref": {
                        "anyOf": [_LOCAL_ID_SCHEMA, {"type": "null"}],
                    },
                    "subject_ref": _LOCAL_ID_SCHEMA,
                    "summary": _TEXT_SCHEMA,
                    "support": _SUPPORT_SCHEMA,
                },
            },
        },
        "events": {
            "type": "array",
            "maxItems": 24,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "cause_event_refs",
                    "effect_event_refs",
                    "event_kind",
                    "fact_refs",
                    "local_event_id",
                    "open_question",
                    "participant_refs",
                    "summary",
                    "support",
                    "temporal_mode",
                ],
                "properties": {
                    "cause_event_refs": _LOCAL_REF_LIST_SCHEMA,
                    "effect_event_refs": _LOCAL_REF_LIST_SCHEMA,
                    "event_kind": {
                        "type": "string",
                        "enum": [
                            "action",
                            "interaction",
                            "state_change",
                            "reaction",
                            "reveal",
                            "transition",
                        ],
                    },
                    "fact_refs": {**_LOCAL_REF_LIST_SCHEMA, "minItems": 1},
                    "local_event_id": _LOCAL_ID_SCHEMA,
                    "open_question": _NULLABLE_TEXT_SCHEMA,
                    "participant_refs": _LOCAL_REF_LIST_SCHEMA,
                    "summary": _TEXT_SCHEMA,
                    "support": _SUPPORT_SCHEMA,
                    "temporal_mode": _TEMPORAL_MODE_SCHEMA,
                },
            },
        },
        "candidate_hypotheses": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "anchor_event_ref",
                    "anchor_summary",
                    "candidate_kind",
                    "context_event_refs",
                    "dialogue_excerpt",
                    "editing_modes",
                    "local_candidate_id",
                    "measurements",
                    "narrative_functions",
                    "open_question",
                    "payoff_event_refs",
                    "payoff_or_open_question",
                    "reason",
                    "support",
                    "supporting_event_refs",
                    "tags",
                ],
                "properties": {
                    "anchor_event_ref": _LOCAL_ID_SCHEMA,
                    "anchor_summary": _TEXT_SCHEMA,
                    "candidate_kind": {
                        "type": "string",
                        "enum": ["highlight", "hook"],
                    },
                    "context_event_refs": _LOCAL_REF_LIST_SCHEMA,
                    "dialogue_excerpt": _NULLABLE_TEXT_SCHEMA,
                    "editing_modes": _EDITING_MODES_SCHEMA,
                    "local_candidate_id": _LOCAL_ID_SCHEMA,
                    "measurements": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 6,
                        "items": _MEASUREMENT_SCHEMA,
                    },
                    "narrative_functions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "uniqueItems": True,
                        "items": {
                            "type": "string",
                            "enum": [
                                "hook",
                                "setup",
                                "escalation",
                                "confrontation",
                                "reveal",
                                "reversal",
                                "payoff",
                                "aftermath",
                            ],
                        },
                    },
                    "open_question": _NULLABLE_TEXT_SCHEMA,
                    "payoff_event_refs": _LOCAL_REF_LIST_SCHEMA,
                    "payoff_or_open_question": _TEXT_SCHEMA,
                    "reason": _TEXT_SCHEMA,
                    "support": _SUPPORT_SCHEMA,
                    "supporting_event_refs": _LOCAL_REF_LIST_SCHEMA,
                    "tags": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 10,
                        "uniqueItems": True,
                        "items": {
                            "type": "string",
                            "enum": [
                                "dialogue",
                                "action",
                                "emotion",
                                "suspense",
                                "conflict",
                                "reveal",
                                "reversal",
                                "visual_spectacle",
                                "character_moment",
                                "relationship_moment",
                            ],
                        },
                    },
                },
                "allOf": [
                    {
                        "if": {
                            "properties": {"candidate_kind": {"const": "hook"}},
                            "required": ["candidate_kind"],
                        },
                        "then": {
                            "properties": {
                                "open_question": _TEXT_SCHEMA,
                                "payoff_event_refs": {"maxItems": 0},
                            }
                        },
                    },
                    {
                        "if": {
                            "properties": {"candidate_kind": {"const": "highlight"}},
                            "required": ["candidate_kind"],
                        },
                        "then": {"properties": {"payoff_event_refs": {"minItems": 1}}},
                    },
                ],
            },
        },
    },
}


def vlm_response_schema_json() -> str:
    """Return the exact compact schema bytes represented as UTF-8 JSON text."""

    return json.dumps(
        VLM_RESPONSE_SCHEMA, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def resolve_vlm_prompt_template(prompt_version: str) -> str:
    """Resolve only an explicit registered version, never a current fallback."""
    if type(prompt_version) is not str:  # noqa: E721
        raise ValueError("prompt version must be a registered VLM prompt version")
    if prompt_version == VLM_PROMPT_VERSION:
        return VLM_PROMPT_TEMPLATE
    if prompt_version == VLM_COMPACT_PROMPT_VERSION:
        return VLM_COMPACT_PROMPT_TEMPLATE
    from .bounded_video_prompt import (
        VLM_BOUNDED_VIDEO_PROMPT_TEMPLATE,
        VLM_BOUNDED_VIDEO_PROMPT_VERSION,
    )

    if prompt_version == VLM_BOUNDED_VIDEO_PROMPT_VERSION:
        return VLM_BOUNDED_VIDEO_PROMPT_TEMPLATE
    from .contextual_video_prompt import (
        VLM_CONTEXTUAL_CLOSED_VOCABULARY_CORE_VIDEO_PROMPT_TEMPLATE,
        VLM_CONTEXTUAL_CLOSED_VOCABULARY_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_TEMPLATE,
        VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_CORE_VIDEO_PROMPT_TEMPLATE,
        VLM_CONTEXTUAL_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_TEMPLATE,
        VLM_CONTEXTUAL_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_STABLE_CORE_VIDEO_PROMPT_TEMPLATE,
        VLM_CONTEXTUAL_STABLE_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_TIMELINE_CORE_VIDEO_PROMPT_TEMPLATE,
        VLM_CONTEXTUAL_TIMELINE_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_TEMPLATE,
        VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_VIDEO_PROMPT_TEMPLATE,
        VLM_CONTEXTUAL_VIDEO_PROMPT_VERSION,
    )

    if prompt_version == VLM_CONTEXTUAL_VIDEO_PROMPT_VERSION:
        return VLM_CONTEXTUAL_VIDEO_PROMPT_TEMPLATE
    if prompt_version == VLM_CONTEXTUAL_CORE_VIDEO_PROMPT_VERSION:
        return VLM_CONTEXTUAL_CORE_VIDEO_PROMPT_TEMPLATE
    if prompt_version == VLM_CONTEXTUAL_TIMELINE_CORE_VIDEO_PROMPT_VERSION:
        return VLM_CONTEXTUAL_TIMELINE_CORE_VIDEO_PROMPT_TEMPLATE
    if prompt_version == VLM_CONTEXTUAL_CLOSED_VOCABULARY_CORE_VIDEO_PROMPT_VERSION:
        return VLM_CONTEXTUAL_CLOSED_VOCABULARY_CORE_VIDEO_PROMPT_TEMPLATE
    if prompt_version == VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_VERSION:
        return VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_TEMPLATE
    if prompt_version == VLM_CONTEXTUAL_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION:
        return VLM_CONTEXTUAL_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_TEMPLATE
    if prompt_version == VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION:
        return VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_TEMPLATE
    if prompt_version == VLM_CONTEXTUAL_STABLE_CORE_VIDEO_PROMPT_VERSION:
        return VLM_CONTEXTUAL_STABLE_CORE_VIDEO_PROMPT_TEMPLATE
    from .video_prompt import VLM_VIDEO_PROMPT_TEMPLATE, VLM_VIDEO_PROMPT_VERSION

    if prompt_version == VLM_VIDEO_PROMPT_VERSION:
        return VLM_VIDEO_PROMPT_TEMPLATE
    raise ValueError("prompt version must be a registered VLM prompt version")


def vlm_prompt_template_sha256(prompt_version: str = VLM_PROMPT_VERSION) -> str:
    """Return the exact UTF-8 identity of the static prompt instructions."""
    template = resolve_vlm_prompt_template(prompt_version)
    return "sha256:" + hashlib.sha256(template.encode("utf-8")).hexdigest()


def build_vlm_prompt(
    manifest: WindowManifest, *, prompt_version: str = VLM_PROMPT_VERSION,
) -> str:
    """Build a manifest-bound prompt without granting physical-cut authority."""

    if type(manifest) is not WindowManifest:  # noqa: E721
        raise TypeError("manifest must be an exact WindowManifest")
    from .bounded_video_prompt import (
        VLM_BOUNDED_VIDEO_PROMPT_VERSION,
        build_vlm_bounded_video_prompt,
    )

    if prompt_version == VLM_BOUNDED_VIDEO_PROMPT_VERSION:
        return build_vlm_bounded_video_prompt(manifest)
    from .contextual_video_prompt import (
        VLM_CONTEXTUAL_CLOSED_VOCABULARY_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_TIMELINE_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_VIDEO_PROMPT_VERSION,
    )

    if prompt_version in {
        VLM_CONTEXTUAL_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_CLOSED_VOCABULARY_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_TIMELINE_CORE_VIDEO_PROMPT_VERSION,
    }:
        raise ValueError("contextual video prompt requires an exact WindowContextPack")
    from .video_prompt import VLM_VIDEO_PROMPT_VERSION, build_vlm_video_prompt

    if prompt_version == VLM_VIDEO_PROMPT_VERSION:
        return build_vlm_video_prompt(manifest)
    template = resolve_vlm_prompt_template(prompt_version)
    frame_anchors = [
        {"frame_id": sample.frame_id, "proxy_pts": sample.proxy_pts}
        for sample in manifest.frame_samples
    ]
    context: dict[str, object] = {
        "allowed_frame_anchors": frame_anchors,
        "proxy_range": {
            "end_pts_exclusive": manifest.timeline_map.proxy_range.end_pts,
            "start_pts": manifest.timeline_map.proxy_range.start_pts,
        },
    }
    if prompt_version == VLM_COMPACT_PROMPT_VERSION:
        proxy_time_base = manifest.timeline_map.proxy_time_base
        context["proxy_time_base"] = {
            "numerator": proxy_time_base.numerator,
            "denominator": proxy_time_base.denominator,
        }
    context_json = json.dumps(
        context,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return template + context_json


__all__ = [
    "VLM_PROMPT_VERSION",
    "VLM_PROMPT_TEMPLATE",
    "VLM_COMPACT_PROMPT_VERSION",
    "VLM_COMPACT_PROMPT_TEMPLATE",
    "VLM_RESPONSE_SCHEMA",
    "build_vlm_prompt",
    "resolve_vlm_prompt_template",
    "vlm_prompt_template_sha256",
    "vlm_response_schema_json",
]
