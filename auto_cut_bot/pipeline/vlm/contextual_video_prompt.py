"""Prompt v7: bounded video observations with an explicit context-pack input.

The wire response remains the registered V4 semantic pack.  This increment
changes only what can assist the model's interpretation, never the evidence
classes or physical-edit authority.
"""

from __future__ import annotations

from autocut_kernel.context_pack import WindowContextPack
from autocut_kernel.vlm import WindowManifest

from .bounded_video_prompt import build_vlm_bounded_video_prompt

VLM_CONTEXTUAL_VIDEO_PROMPT_VERSION = "vlm-semantic-pack-v7-context-assisted"
VLM_CONTEXTUAL_CORE_VIDEO_PROMPT_VERSION = (
    "vlm-semantic-pack-v8-context-assisted-core-observations"
)
VLM_CONTEXTUAL_TIMELINE_CORE_VIDEO_PROMPT_VERSION = (
    "vlm-semantic-pack-v9-context-assisted-timeline-core-observations"
)
VLM_CONTEXTUAL_CLOSED_VOCABULARY_CORE_VIDEO_PROMPT_VERSION = (
    "vlm-semantic-pack-v10-context-assisted-timeline-closed-vocabulary"
)
VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_VERSION = (
    "vlm-semantic-pack-v11-context-assisted-compact-canonical-core"
)
VLM_CONTEXTUAL_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION = (
    "vlm-semantic-pack-v12-context-assisted-reciprocal-causal-core"
)
VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION = (
    "vlm-semantic-pack-v13-context-assisted-validated-reciprocal-causal-core"
)
VLM_CONTEXTUAL_STABLE_CORE_VIDEO_PROMPT_VERSION = (
    "vlm-semantic-pack-v14-context-assisted-stable-core-observations"
)
VLM_CONTEXTUAL_REQUIRED_EMPTY_ARRAY_CORE_VIDEO_PROMPT_VERSION = (
    "vlm-semantic-pack-v15-context-assisted-required-empty-array-core"
)
VLM_CONTEXTUAL_VIDEO_PROMPT_TEMPLATE = (
    "你会得到一段外部剧情辅助。它只帮助理解叙事，不是视频证据。"
    "不得把其中的人名、关系、剧情或主题直接写成已观察到的 entity、fact、event、"
    "candidate 或时间范围；这些字段仍必须由随附视频实际可见、可听的内容支持。"
    "画面与剧情辅助冲突、无法确认人物对应关系或无法确认关系时，保留不确定性，"
    "使用中性 display_label，不要猜测。不得输出 context_ref、API ID、外部接口数据、"
    "ASR、VAD、字幕、shot/highlight 或任何物理剪辑端点。\n"
    "剧情辅助：\n"
)
VLM_CONTEXTUAL_CORE_VIDEO_PROMPT_TEMPLATE = (
    VLM_CONTEXTUAL_VIDEO_PROMPT_TEMPLATE
    + "本轮只采集可由视频支持的核心观察图。candidate_hypotheses 必须严格输出空数组 []；"
    "不要尝试评分、命名或提出高光/钩子。后续阶段会仅依据已通过校验的事实和事件生成候选。\n"
)
VLM_CONTEXTUAL_TIMELINE_CORE_VIDEO_PROMPT_TEMPLATE = (
    VLM_CONTEXTUAL_CORE_VIDEO_PROMPT_TEMPLATE
    + "事件与事实必须按时间线闭合：先为每个事件确定其 support 区间，再只选择与该区间"
    "有非空时间交集的事实写入 fact_refs。相同人物在更早或更晚发生的动作、状态、"
    "镜头不得作为该事件的 fact_refs。一个叙事描述跨越不连续动作时，拆成多个事件，"
    "不要扩大事件区间或把远处事实并入。若不能为事件选出至少一个严格时间相交的事实，"
    "保留事实但不要输出该事件。输出前逐个复核 event.fact_refs 的每项均与事件区间相交。\n"
)
VLM_CONTEXTUAL_CLOSED_VOCABULARY_CORE_VIDEO_PROMPT_TEMPLATE = (
    VLM_CONTEXTUAL_TIMELINE_CORE_VIDEO_PROMPT_TEMPLATE
    + "所有枚举必须逐字使用下列封闭值，不得造词或翻译：entity_kind 仅 person、object、"
    "location、screen_text_source；fact_kind 仅 visible_presence、visible_state、"
    "visible_action、visible_change、visible_relation、scene_context、character_appearance、"
    "screen_text、temporal_mode；event_kind 仅 action、interaction、state_change、reaction、"
    "reveal、transition；temporal_mode 仅 present、flashback、flashforward、dream、unknown。"
    "特别地，人物的表情、惊讶、恐惧、愤怒等“反应”不是 fact_kind：有可见动作写"
    "visible_action，只有表情或姿态写 visible_state。输出前逐个检查这些字段；"
    "禁止 visible_reaction、emotion、dialogue、establishing_shot 等未注册值。"
    "所有 summary、reason、visual_description、open_question 等文本字段均使用一到两句短句，"
    "不超过240个字符；window_summary.summary不超过360个字符，不能复述事实列表。\n"
)
VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_TEMPLATE = (
    VLM_CONTEXTUAL_CLOSED_VOCABULARY_CORE_VIDEO_PROMPT_TEMPLATE
    + "为确保可验证性，本窗口最多输出12个实体、18个事实、10个事件、4个 temporal_segments；"
    "只保留高信息量观察，不凑满上限。JSON词法规则：每个 local_*_id、subject_ref、object_ref、"
    "participant_refs、fact_refs、event_refs 都必须是带双引号的 JSON 字符串，例如 \"p001\"，"
    "绝不能写成 p001；所有 Schema 必填字段必须恰好出现一次，未知可空字段写 null，"
    "对象不得添加字段，引用数组按字典序且无重复。输出前按 Schema 逐字段检查 JSON 引号、"
    "逗号和括号；只返回一个 JSON 对象。\n"
)
VLM_CONTEXTUAL_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_TEMPLATE = (
    VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_TEMPLATE
    + "事件因果边必须成对且只表达画面明确支持的直接关系：若事件 B 的 cause_event_refs 包含 "
    "\"e001\"，则事件 e001 的 effect_event_refs 必须包含 B 的 local_event_id；反之亦然。"
    "同一因果边只能各写一次、不得自指。不能同时保证两端引用时，两个数组都保持空数组 []。"
    "输出前逐一核对每条 cause_event_refs 与对应 effect_event_refs 的反向引用完全一致。\n"
)
VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_TEMPLATE = (
    VLM_CONTEXTUAL_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_TEMPLATE
    + "这是严格验证版本。输出前逐项执行如下机械自检：每一个 fact 必须同时拥有 subject_ref 和 "
    "object_ref，缺少受词时 object_ref 写 JSON null，绝不省略字段；event_kind 只能是 "
    "action、interaction、state_change、reaction、reveal、transition；每条 event.fact_refs "
    "的 support 均须和该 event.support 的 interval_ms 有非空交集；uncertainty_ms 必须是 "
    "0 到 5000 的普通整数。每一个 B.cause_event_refs 中的 A，必须有且只有 A.effect_event_refs "
    "包含 B；禁止自指、未知 ID、重复 ID 或只写单侧。无法同时证明因果和互反关系时，两侧均写 []。"
    "最后确认所有对象字段、数组和字符串都闭合为一个完整 JSON 后再输出。\n"
)
VLM_CONTEXTUAL_STABLE_CORE_VIDEO_PROMPT_TEMPLATE = (
    VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_TEMPLATE
    + "本版本将两类非必要的冗余图字段固定为空：每个事件的 cause_event_refs 与 "
    "effect_event_refs 都必须是 []；continuity.temporal_segments 也必须是 []。"
    "不要用这三个数组表达因果、前情、倒叙或镜头时间线。事件本身、fact_refs、support "
    "和 window_summary 仍须完整、准确；后续确定性阶段会从已校验观察推导所需关系。\n"
)
VLM_CONTEXTUAL_REQUIRED_EMPTY_ARRAY_CORE_VIDEO_PROMPT_TEMPLATE = (
    VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_TEMPLATE
    + "本版本不输出因果边或多段时间模式：每一个 event 都必须显式包含"
    "\"cause_event_refs\":[] 与 \"effect_event_refs\":[]，并且"
    "continuity 必须显式包含 \"temporal_segments\":[]。这些字段虽然为空，"
    "但绝不能省略、写成 null、写成字符串或添加 ID。每一个 fact 也必须显式包含"
    "object_ref；无受词时写 JSON null（不带引号），绝不能写字符串 \"null\"。"
    "先检查所有必填字段均出现后再输出。\n"
)


def build_vlm_contextual_video_prompt(
    manifest: WindowManifest,
    context_pack: WindowContextPack,
    *,
    prompt_version: str = VLM_CONTEXTUAL_VIDEO_PROMPT_VERSION,
) -> str:
    if type(manifest) is not WindowManifest:  # noqa: E721
        raise TypeError("contextual prompt requires an exact WindowManifest")
    if type(context_pack) is not WindowContextPack:  # noqa: E721
        raise TypeError("contextual prompt requires an exact WindowContextPack")
    templates = {
        VLM_CONTEXTUAL_VIDEO_PROMPT_VERSION: VLM_CONTEXTUAL_VIDEO_PROMPT_TEMPLATE,
        VLM_CONTEXTUAL_CORE_VIDEO_PROMPT_VERSION: VLM_CONTEXTUAL_CORE_VIDEO_PROMPT_TEMPLATE,
        VLM_CONTEXTUAL_TIMELINE_CORE_VIDEO_PROMPT_VERSION: (
            VLM_CONTEXTUAL_TIMELINE_CORE_VIDEO_PROMPT_TEMPLATE
        ),
        VLM_CONTEXTUAL_CLOSED_VOCABULARY_CORE_VIDEO_PROMPT_VERSION: (
            VLM_CONTEXTUAL_CLOSED_VOCABULARY_CORE_VIDEO_PROMPT_TEMPLATE
        ),
        VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_VERSION: (
            VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_TEMPLATE
        ),
        VLM_CONTEXTUAL_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION: (
            VLM_CONTEXTUAL_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_TEMPLATE
        ),
        VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION: (
            VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_TEMPLATE
        ),
        VLM_CONTEXTUAL_STABLE_CORE_VIDEO_PROMPT_VERSION: (
            VLM_CONTEXTUAL_STABLE_CORE_VIDEO_PROMPT_TEMPLATE
        ),
        VLM_CONTEXTUAL_REQUIRED_EMPTY_ARRAY_CORE_VIDEO_PROMPT_VERSION: (
            VLM_CONTEXTUAL_REQUIRED_EMPTY_ARRAY_CORE_VIDEO_PROMPT_TEMPLATE
        ),
    }
    template = templates.get(prompt_version)
    if template is None:
        raise ValueError("prompt version is not a registered contextual video prompt")
    return (
        build_vlm_bounded_video_prompt(manifest)
        + "\n"
        + template
        + context_pack.rendered_context
    )
