"""A bounded generation subset of V4, not a replacement for Kernel admission.

The frozen V4 parser still accepts valid frame-anchored observations. This
registered prompt asks only for video observations and compact local references;
neither JSON Schema nor field ordering proves semantic or reference closure.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from typing import cast

from autocut_kernel.vlm import WindowManifest

from .video_prompt import vlm_video_response_schema

VLM_BOUNDED_VIDEO_PROMPT_VERSION = "vlm-semantic-pack-v6-bounded-references"
VLM_VIDEO_FIELD_ORDER = (
    "schema_version", "entities", "facts", "events", "window_summary",
    "continuity", "candidate_hypotheses",
)
VLM_BOUNDED_VIDEO_PROMPT_TEMPLATE = (
    "你是完整视频窗口的局部语义分析器。只依据随附视频实际可见、可听的内容，"
    "输出符合所给Schema的完整单行紧凑JSON，不要解释、Markdown或思考过程。"
    "不得借用外部剧本、独立ASR/OCR或字幕轨道；可记录视频中可见文字，"
    "但文字不能单独证明其声称的身份、动作、对白或关系。\n"
    "依次输出schema_version、entities、facts、events、window_summary、continuity、"
    "candidate_hypotheses。完整记录独立事实和必要实体，不把不同时间的变化合并，"
    "不为压缩引用而省略独立事实。摘要简洁，不重复、不凑数。"
    "所有实体（包括人物、物体、地点、文字来源）统一用p001至p024；"
    "事实f001至f048，事件e001至e024，候选c001至c008。各类ID唯一、按字典序排列。"
    "引用只使用本窗口实际声明的对应类型ID；每处只引用与该处有关的必要项，"
    "不要复制整张事实表、虚构编号或为了填满额度添加条目。\n"
    "时间为随附视频播放起点起算的整数毫秒（1秒=1000毫秒），"
    "半开区间满足0<=start_ms<end_ms<=duration_ms_floor。"
    "uncertainty_ms为模型自报的非负整数粗定位估计，不是经校准误差，不可机械填同一值；"
    "不能用来挽救倒置、越界或不相交的时间段。"
    "support只用video_observation，填support_kind、interval_ms、confidence。"
    "不填帧引用、PTS、源时间、文件路径或视频hash；不要扩大事实区间。"
    "这些仅是模型粗语义观察，不是独立事实核验、安全许可或物理剪辑端点。\n"
    "每个事件至少引用一个事实，事件区间须与每个引用事实区间相交。"
    "因果必须有直接素材依据，不把先后顺序当因果；A的effect_event_refs含B，当且仅当"
    "B的cause_event_refs含A；不能成环，不确定则留空。"
    "window_summary引用必要的已声明事实/事件。"
    "continuity只依据窗口内未闭合状态，不预知未输入的前后集内容；"
    "满足starts_mid_event=continues_from_previous=bool(entry_state_fact_refs)，"
    "ends_mid_event=continues_into_next=bool(exit_state_fact_refs)；"
    "temporal_segments含mode、summary、support，按时间有序且不重叠。\n"
    "候选只是高光/钩子假设，不是剪辑决定；普通铺垫、过场、证据不足时为空。"
    "anchor/supporting/payoff事件是候选直接素材，区间都须与候选相交；"
    "context事件是解释背景，可以在候选区间外，不要为包含背景而扩大候选。"
    "measurement至少引用一个事实或事件，且仅引用本候选所列事件及这些事件的事实。"
    "hook须有具体未解open_question，payoff_event_refs为空；"
    "highlight须有已发生的非空payoff_event_refs。"
    "reason说明选择理由，anchor_summary复用核心事件摘要；"
    "hook的payoff_or_open_question复用open_question，避免重复扩写。"
    "dialogue_excerpt只作简短语义描述，不是逐字ASR或时间证据。\n"
    "枚举值遵循Schema；editing_modes、narrative_functions、tags按Schema给定顺序去重。"
    "所有confidence和measurement.value为0到1的十进制字符串，不是JSON浮点数。"
    "保留全部必填字段和必要引用，不添加未知字段；结构符合Schema仍须通过语义校验。\n"
    "播放窗口："
)

_ID_PATTERNS = {
    "entity": "^p(?:00[1-9]|01[0-9]|02[0-4])$",
    "fact": "^f(?:00[1-9]|0[1-3][0-9]|04[0-8])$",
    "event": "^e(?:00[1-9]|01[0-9]|02[0-4])$",
    "candidate": "^c00[1-8]$",
}
_ID_LIMITS = {"entity": 24, "fact": 48, "event": 24, "candidate": 8}
_ID_FIELDS = {
    "local_entity_id": "entity", "subject_ref": "entity",
    "local_fact_id": "fact", "local_event_id": "event",
    "anchor_event_ref": "event", "local_candidate_id": "candidate",
}
_REF_FIELDS = {
    "participant_refs": "entity", "fact_refs": "fact",
    "entry_state_fact_refs": "fact", "exit_state_fact_refs": "fact",
    "event_refs": "event", "cause_event_refs": "event", "effect_event_refs": "event",
    "context_event_refs": "event", "supporting_event_refs": "event", "payoff_event_refs": "event",
}


def _object(value: object) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise ValueError("registered video schema member must be an object")
    return cast(dict[str, object], value)


def _id_schema(kind: str) -> dict[str, object]:
    return {"type": "string", "pattern": _ID_PATTERNS[kind], "minLength": 4, "maxLength": 4}


def _generation_subset(value: object) -> object:
    """Rebuild nodes independently: the historical schema shares reference dicts."""
    if type(value) is list:
        return [_generation_subset(item) for item in cast(list[object], value)]
    if type(value) is not dict:  # noqa: E721
        return value
    node = {key: _generation_subset(item) for key, item in cast(dict[str, object], value).items()}
    if "properties" not in node:
        return node
    properties = _object(node["properties"])
    for name, member in properties.items():
        field = _object(member)
        if name == "support":
            branches = cast(list[object], field["oneOf"])
            videos = [
                branch for branch in branches
                if _object(_object(_object(branch)["properties"])["support_kind"]).get("const")
                == "video_observation"
            ]
            if len(videos) != 1:
                raise ValueError("registered video schema must have exactly one video support branch")
            properties[name] = videos[0]
        elif name in _ID_FIELDS:
            properties[name] = _id_schema(_ID_FIELDS[name])
        elif name == "object_ref":
            properties[name] = {"anyOf": [_id_schema("entity"), {"type": "null"}]}
        elif name in _REF_FIELDS and field.get("type") == "array":
            # Conditional fragments containing only minItems/maxItems are not
            # array definitions; leave their hook/highlight rules untouched.
            kind = _REF_FIELDS[name]
            field["items"] = _id_schema(kind)
            field["maxItems"] = _ID_LIMITS[kind]
    return node


def vlm_bounded_video_response_schema() -> dict[str, object]:
    """Return a fresh generation schema, retaining every required semantic field."""
    schema = _object(_generation_subset(vlm_video_response_schema()))
    properties = _object(schema["properties"])
    schema["properties"] = {name: properties[name] for name in VLM_VIDEO_FIELD_ORDER}
    schema["required"] = list(VLM_VIDEO_FIELD_ORDER)
    return schema


def vlm_core_video_response_schema() -> dict[str, object]:
    """Return the V8 core-observation schema without proposal generation.

    Candidate hypotheses mix editorial judgement with the factual graph.  They
    are deferred until the graph passes admission; a closed zero-length array
    keeps the v4 wire shape while excluding proposal-only enum fields here.
    """

    schema = vlm_bounded_video_response_schema()
    properties = _object(schema["properties"])
    candidates = _object(properties["candidate_hypotheses"])
    candidates["maxItems"] = 0
    return schema


def vlm_validated_reciprocal_core_video_response_schema() -> dict[str, object]:
    """Return V13's tighter wire schema without altering V4/V12 bytes."""

    schema = vlm_core_video_response_schema()
    properties = _object(schema["properties"])
    for group in ("entities", "facts", "events"):
        member = _object(_object(properties[group])["items"])
        support = _object(_object(member["properties"])["support"])
        interval = _object(_object(support["properties"])["interval_ms"])
        uncertainty = _object(_object(interval["properties"])["uncertainty_ms"])
        uncertainty["maximum"] = 5_000
    continuity = _object(_object(properties["continuity"])["properties"])
    segment = _object(_object(continuity["temporal_segments"])["items"])
    support = _object(_object(segment["properties"])["support"])
    interval = _object(_object(support["properties"])["interval_ms"])
    uncertainty = _object(_object(interval["properties"])["uncertainty_ms"])
    uncertainty["maximum"] = 5_000
    return schema


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def vlm_bounded_video_response_schema_json() -> str:
    """Return canonical schema identity text; API ordering is a separate view."""
    return _canonical_json(vlm_bounded_video_response_schema())


def vlm_core_video_response_schema_json() -> str:
    """Return canonical JSON for the registered V8 core-observation schema."""

    return _canonical_json(vlm_core_video_response_schema())


def vlm_validated_reciprocal_core_video_response_schema_json() -> str:
    """Return canonical V13 schema bytes for request identity binding."""

    return _canonical_json(vlm_validated_reciprocal_core_video_response_schema())


def ordered_bounded_video_schema(schema: object) -> dict[str, object]:
    """Order only the exact registered schema, without promising model key order."""
    expected = vlm_bounded_video_response_schema()
    try:
        identical = type(schema) is dict and _canonical_json(cast(dict[str, object], schema)) == _canonical_json(expected)
    except (TypeError, ValueError) as exc:
        raise ValueError("bounded video schema must exactly match the registered schema") from exc
    if not identical:
        raise ValueError("bounded video schema must exactly match the registered schema")
    return copy.deepcopy(expected)


def build_vlm_bounded_video_prompt(manifest: WindowManifest) -> str:
    """Expose only exact floored playback duration; evidence remains program-owned."""
    if type(manifest) is not WindowManifest:  # noqa: E721
        raise TypeError("bounded video prompt requires an exact WindowManifest")
    timeline = manifest.timeline_map
    time_base = timeline.proxy_time_base
    duration = Fraction(
        (timeline.proxy_range.end_pts - timeline.proxy_range.start_pts)
        * time_base.numerator * 1000, time_base.denominator,
    )
    duration_floor = duration.numerator // duration.denominator
    if duration_floor < 1:
        raise ValueError("V4 millisecond wire requires at least one millisecond")
    return VLM_BOUNDED_VIDEO_PROMPT_TEMPLATE + _canonical_json({"duration_ms_floor": duration_floor})
