"""V4 video observations: human playback time, not invented physical evidence.

This module is deliberately not the default prompt. Registration and end-to-end
reader/finalizer support are required before an installed authority selects it.
"""

from __future__ import annotations

import copy
import hashlib
import json
from fractions import Fraction
from typing import cast

from autocut_kernel.vlm import WindowManifest
from autocut_kernel.vlm.semantic_support_v4 import frame_aliases

from .prompt import VLM_RESPONSE_SCHEMA

VLM_VIDEO_PROMPT_VERSION = "vlm-semantic-pack-v5-video-observation"
VLM_VIDEO_PROMPT_TEMPLATE = (
    "你是完整视频窗口的局部语义分析器。只依据随附视频中实际可见、可听的内容，"
    "输出符合schema_version=4的完整单行紧凑JSON，不要解释、Markdown或思考过程。\n"
    "记录必要人物/物体、独立事实、事件、窗口总结、连续性和符合绝对标准的高光/钩子候选。"
    "不为了凑数重复描述，不借用外部剧本、独立ASR/OCR或字幕轨道。允许直接观察视频中可见文字，"
    "但文字内容不能独自证明其所述身份、动作、对白或关系真实发生。\n"
    "时间一律使用当前随附视频从播放起点开始的整数毫秒：1秒=1000毫秒。"
    "合法范围为0到duration_ms_floor；每个区间必须满足0<=start_ms<end_ms<=duration_ms_floor。"
    "不要输出PTS、帧编号、绝对源时间或凭剧情想象的时长，不得超过视频真实播放长度。"
    "uncertainty_ms是非负整数的粗定位不确定度；不能以误差为由填倒置或越界区间。"
    "所有时间仅用于粗语义定位，不是对白切割点或任何物理剪辑端点。\n"
    "support有两种明确类型。video_observation表示在随附完整视频中的模型观察，"
    "只填support_kind、interval_ms和confidence，不要求帧引用。参考帧是稀疏的，"
    "短暂事实可直接用video_observation；不要为了包含参考帧而扩大事实时间段。"
    "frame_anchored_observation另外要求frame_refs非空且唯一，只能逐字使用给定短alias，"
    "并且至少一个引用帧的实际时间必须位于声明半开区间[start_ms,end_ms)内。"
    "参考帧显示毫秒为向下取整，仅供观察定位；它们不是独立事实核验或安全许可。"
    "模型不得填写视频hash、文件路径、已验证/安全/发布标记。\n"
    "所有局部ID唯一且按字典序排列；entity/fact/event/candidate分别使用p001/f001/e001/c001等。"
    "各引用只指向本窗口声明的对应实体、事实或事件，不使用短帧alias作为业务ID。"
    "entity_kind仅person/object/location/screen_text_source；fact_kind仅visible_presence/"
    "visible_state/visible_action/visible_change/visible_relation/scene_context/character_appearance/"
    "screen_text/temporal_mode；event_kind仅action/interaction/state_change/reaction/reveal/transition。"
    "每个事件至少引用一个事实；因果必须有画面支持、双向一致且不能成环，不能确定则留空。\n"
    "window_summary须引用已声明fact/event并给出dominant_temporal_mode。continuity须完整说明"
    "前后延续/中途开始结束及entry_state_fact_refs/exit_state_fact_refs。"
    "temporal_segments每项都是mode、summary和support三个字段；时间段有序且不重叠，"
    "mode只取present/flashback/flashforward/dream/unknown，不确定用unknown。\n"
    "candidate_hypotheses不是剪辑决定：普通铺垫、过场或证据不足时为空。hook必须提出具体未解"
    "open_question且payoff_event_refs为空；highlight必须引用已经发生的非空payoff_event_refs。"
    "候选anchor/context/supporting/payoff事件引用必须闭合，measurement至少引用一个fact/event。"
    "editing_modes按dialogue/action顺序，narrative_functions按hook/setup/escalation/confrontation/"
    "reveal/reversal/payoff/aftermath顺序，tags按dialogue/action/emotion/suspense/conflict/reveal/"
    "reversal/visual_spectacle/character_moment/relationship_moment顺序去重。"
    "measurement_kind只取hook_strength/reveal_strength/emotional_payoff_strength/dialogue_salience/"
    "action_salience/visual_salience。dialogue_excerpt仅作简短语义描述，不是逐字ASR或时间证据。"
    "所有confidence和measurement.value为0到1的十进制字符串，不是JSON浮点数。\n"
    "完成所有schema要求的字段；表达简洁，不省略独立事实/必要引用，不添加未知字段。\n"
    "播放窗口："
)


def _object(value: object) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise ValueError("installed V4 schema member must be an object")
    return cast(dict[str, object], value)


def _support_schema() -> dict[str, object]:
    interval = {
        "type": "object", "additionalProperties": False,
        "required": ["start_ms", "end_ms", "uncertainty_ms"],
        "properties": {
            name: {"type": "integer", "minimum": 0}
            for name in ("start_ms", "end_ms", "uncertainty_ms")
        },
    }
    confidence = {"type": "string", "pattern": "^(?:0|0\\.[0-9]+|1|1\\.0+)$"}
    branches: list[dict[str, object]] = []
    for kind in ("video_observation", "frame_anchored_observation"):
        properties: dict[str, object] = {
            "support_kind": {"type": "string", "const": kind},
            "interval_ms": copy.deepcopy(interval), "confidence": confidence,
        }
        if kind == "frame_anchored_observation":
            properties["frame_refs"] = {
                "type": "array", "minItems": 1, "uniqueItems": True,
                "items": {"type": "string", "pattern": "^f[0-9]{4,}$"},
            }
        branches.append({
            "type": "object", "additionalProperties": False,
            "required": list(properties), "properties": properties,
        })
    return {"oneOf": branches}


def vlm_video_response_schema() -> dict[str, object]:
    """Derive a fresh V4 wire schema without mutating the frozen V3 schema."""
    schema = copy.deepcopy(VLM_RESPONSE_SCHEMA)
    root = _object(schema["properties"])
    root["schema_version"] = {"type": "integer", "const": 4}
    for name in ("entities", "facts", "events", "candidate_hypotheses"):
        member = _object(_object(root[name])["items"])
        _object(member["properties"])["support"] = _support_schema()
    continuity = _object(_object(root["continuity"])["properties"])
    segments = _object(continuity["temporal_segments"])
    old_properties = _object(_object(segments["items"])["properties"])
    segments["items"] = {
        "type": "object", "additionalProperties": False,
        "required": ["mode", "summary", "support"],
        "properties": {
            "mode": old_properties["mode"], "summary": old_properties["summary"],
            "support": _support_schema(),
        },
    }
    return schema


def vlm_video_response_schema_json() -> str:
    return json.dumps(vlm_video_response_schema(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def vlm_video_prompt_template_sha256() -> str:
    return "sha256:" + hashlib.sha256(VLM_VIDEO_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()


def build_vlm_video_prompt(manifest: WindowManifest) -> str:
    """Render playback offsets; full evidence identities remain program-owned."""
    if type(manifest) is not WindowManifest:  # noqa: E721
        raise TypeError("V4 prompt requires an exact WindowManifest")
    timeline = manifest.timeline_map
    time_base = timeline.proxy_time_base
    duration_ms = Fraction(
        (timeline.proxy_range.end_pts - timeline.proxy_range.start_pts)
        * time_base.numerator * 1000, time_base.denominator,
    )
    duration_floor = duration_ms.numerator // duration_ms.denominator
    if duration_floor < 1:
        raise ValueError("V4 millisecond wire requires at least one millisecond")
    aliases = frame_aliases(manifest)
    context = {
        "duration_ms_floor": duration_floor,
        "time_unit": "milliseconds_from_attached_video_playback_start",
        "frame_time_display": "floor_milliseconds_not_exact_frame_time",
        "frame_alias_map_sha256": aliases.canonical_hash,
        "reference_frames": [
            {"frame_ref": item.alias, "time_ms_floor": item.relative_time_ms.numerator // item.relative_time_ms.denominator}
            for item in aliases.entries
        ],
    }
    return VLM_VIDEO_PROMPT_TEMPLATE + json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
