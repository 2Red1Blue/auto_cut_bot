"""Prompt v7: bounded video observations with an explicit context-pack input.

The wire response remains the registered V4 semantic pack.  This increment
changes only what can assist the model's interpretation, never the evidence
classes or physical-edit authority.
"""

from __future__ import annotations

from autocut_kernel.context_pack import WindowContextPack
from autocut_kernel.vlm import WindowManifest

from .bounded_video_prompt import (
    build_vlm_bounded_video_prompt,
    build_vlm_window_duration_descriptor,
)

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
VLM_CONTEXTUAL_STRICT_WIRE_CORE_VIDEO_PROMPT_VERSION = (
    "vlm-semantic-pack-v16-context-assisted-strict-wire-core"
)
VLM_CONTEXTUAL_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION = (
    "vlm-semantic-pack-v17-context-assisted-fact-anchored-event-core"
)
VLM_CONTEXTUAL_ENUM_DISAMBIGUATED_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION = (
    "vlm-semantic-pack-v18-context-assisted-enum-disambiguated-fact-anchored-event-core"
)
VLM_CONTEXTUAL_MINIMAL_CORE_VIDEO_PROMPT_VERSION = (
    "vlm-semantic-pack-v20-context-assisted-minimal-core-observations"
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
VLM_CONTEXTUAL_STRICT_WIRE_CORE_VIDEO_PROMPT_TEMPLATE = (
    VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_TEMPLATE
    + "本版本只输出观察图，不输出因果边或多段时间模式：每一个 event 必须显式包含"
    "\"cause_event_refs\":[] 与 \"effect_event_refs\":[]；continuity 必须显式包含"
    "\"temporal_segments\":[]。这些字段不能省略、不能写 null、不能写成字符串或加入 ID。"
    "candidate_hypotheses 也必须显式为 []。\n"
    "严格执行本地 ID 协议：先声明实体，实体 ID 依次只能使用 p001、p002、…；再声明事实，"
    "事实 ID 依次只能使用 f001、f002、…；最后声明事件，事件 ID 依次只能使用 e001、e002、…。"
    "所有 ref 都必须逐字复制已声明的本地 ID，且必须是 JSON 字符串。例："
    "\"subject_ref\":\"p001\"，\"object_ref\":\"p008\"，无受词时"
    "\"object_ref\":null。绝不能输出 /p008、裸 p008、\"null\"、`p008`、角色名或 API ID。"
    "每一个 subject_ref 和非 null object_ref 必须在 entities.local_entity_id 中存在；每一个"
    "participant_refs 必须引用已声明 entity；每一个 fact_refs 必须引用已声明 fact。\n"
    "event_kind 只能逐字使用 action、interaction、state_change、reaction、reveal、transition。"
    "例如 confrontation、argument、fight、dialogue、emotion 都不是合法 event_kind：冲突互动用 interaction，"
    "单人动作用 action，状态变化用 state_change，反应只用 reaction。"
    "输出前按以下顺序机械检查：完整 JSON；所有本地 ID/ref 的引号和格式；引用闭合；枚举；"
    "全部必填字段。只返回一个 JSON 对象，不要 Markdown、解释或代码围栏。\n"
)
VLM_CONTEXTUAL_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_TEMPLATE = (
    VLM_CONTEXTUAL_STRICT_WIRE_CORE_VIDEO_PROMPT_TEMPLATE
    + "严格执行事件锚定规则：每一个 event.fact_refs 必须恰好有一个已声明 fact ID。"
    "event.support 必须逐字复制该唯一 fact 的完整 support 对象（support_kind、confidence 与"
    "interval_ms 的 start_ms、end_ms、uncertainty_ms 都相同）。不得把相邻片段当作重叠："
    "例如 fact 为 [198000,213000) 时，event 不能从 213000 或更晚开始。"
    "一个事件需要多个事实时，保留额外事实但为每个时间不连续事实分别输出 event；不要在"
    "同一 event.fact_refs 中加入第二个事实。输出前逐 event 比对其唯一 fact_ref 与 support 完全相同。\n"
)
VLM_CONTEXTUAL_ENUM_DISAMBIGUATED_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_TEMPLATE = (
    VLM_CONTEXTUAL_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_TEMPLATE
    + "事实类型必须逐字从下列九个值中选择，不能拼接、加后缀或创建组合词："
    "visible_presence、visible_state、visible_action、visible_change、visible_relation、"
    "scene_context、character_appearance、screen_text、temporal_mode。尤其禁止 "
    "visible_state_change：画面中已经发生的状态改变写 visible_change；只描述某时刻的"
    "状态、表情或姿态写 visible_state。visible_state_change、state_change_fact、"
    "visible_reaction、emotion、reaction 都不是合法 fact_kind。反应的动作写 visible_action；"
    "只有表情或姿态写 visible_state。输出前逐条检查 facts[*].fact_kind；不能确定时保留事实但"
    "选上述最接近的单一合法值，绝不造新枚举。再做引用与 JSON 机械自检：每个非 null object_ref "
    "必须逐字出现在 entities.local_entity_id；没有受词时只能写 JSON token \"object_ref\":null，"
    "绝不能写字符串 \"null\"。输出必须是一个可由严格 JSON 解析器读取的对象：所有 key/字符串"
    "使用双引号，不能有尾逗号、注释、Markdown、解释文字或半截对象。\n"
)
VLM_CONTEXTUAL_MINIMAL_CORE_VIDEO_PROMPT_TEMPLATE = (
    "你是完整视频窗口的局部语义分析器。本轮只输出可由当前视频支持的核心观察，"
    "不生成剪辑候选、因果图或多段时间叙事。"
    "candidate_hypotheses 必须是 []；每个 event 的 cause_event_refs 与 effect_event_refs 必须是 []；"
    "continuity.temporal_segments 必须是 []。这些字段均须显式出现，不能省略或写 null。\n"
    "只返回一个完整、严格的 JSON 对象：schema_version 必须为 4，根字段按 schema_version、entities、facts、"
    "events、window_summary、continuity、candidate_hypotheses 输出；不要 Markdown、解释、注释或尾逗号。"
    "只保留高信息量观察，不凑数量。\n"
    "先声明实体，再声明事实，最后声明事件。实体本地 ID 只能依次为 p001、p002、…；事实只能依次为"
    "f001、f002、…；事件只能依次为 e001、e002、…。每一个 subject_ref、非 null object_ref 与"
    "participant_refs 必须逐字引用已声明的实体；每一个 event.fact_refs 必须恰好引用一个已声明事实。"
    "没有受词时 object_ref 必须写 JSON null，绝不能省略或写字符串 \"null\"。\n"
    "fact_kind 只能是 visible_presence、visible_state、visible_action、visible_change、visible_relation、"
    "scene_context、character_appearance、screen_text、temporal_mode。表情或姿态写 visible_state，"
    "已发生的状态改变写 visible_change，动作写 visible_action；不得创造 visible_state_change、"
    "visible_reaction、emotion 或其他未注册值。event_kind 只能是 action、interaction、state_change、"
    "reaction、reveal、transition。\n"
    "每个实体、事实、事件都必须有 video_observation support。时间使用从播放窗口开始的整数毫秒半开区间，"
    "满足 0<=start_ms<end_ms<=duration_ms_floor；uncertainty_ms 是 0 到 5000 的整数。"
    "每个 event 的 support 必须逐字复制其唯一 fact_ref 的完整 support。为避免跨窗口推断，"
    "本轮 continuity 必须固定输出 continues_from_previous=false、continues_into_next=false、"
    "starts_mid_event=false、ends_mid_event=false、entry_state_fact_refs=[]、"
    "exit_state_fact_refs=[]、temporal_segments=[]；不要填写任何跨窗口事实引用。"
    "window_summary 必须简短并只引用已经声明的事实或事件。\n"
    "播放窗口："
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
        VLM_CONTEXTUAL_STRICT_WIRE_CORE_VIDEO_PROMPT_VERSION: (
            VLM_CONTEXTUAL_STRICT_WIRE_CORE_VIDEO_PROMPT_TEMPLATE
        ),
        VLM_CONTEXTUAL_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION: (
            VLM_CONTEXTUAL_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_TEMPLATE
        ),
        VLM_CONTEXTUAL_ENUM_DISAMBIGUATED_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION: (
            VLM_CONTEXTUAL_ENUM_DISAMBIGUATED_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_TEMPLATE
        ),
        VLM_CONTEXTUAL_MINIMAL_CORE_VIDEO_PROMPT_VERSION: (
            VLM_CONTEXTUAL_MINIMAL_CORE_VIDEO_PROMPT_TEMPLATE
        ),
    }
    template = templates.get(prompt_version)
    if template is None:
        raise ValueError("prompt version is not a registered contextual video prompt")
    if prompt_version == VLM_CONTEXTUAL_MINIMAL_CORE_VIDEO_PROMPT_VERSION:
        return (
            template
            + build_vlm_window_duration_descriptor(manifest)
            + "\n"
            + VLM_CONTEXTUAL_VIDEO_PROMPT_TEMPLATE
            + context_pack.rendered_context
        )
    return (
        build_vlm_bounded_video_prompt(manifest)
        + "\n"
        + template
        + context_pack.rendered_context
    )
