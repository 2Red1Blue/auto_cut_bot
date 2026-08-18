"""窗口分析 Schema — Pydantic v2 实现。

原位置: story_schemas.py:92-190 (WINDOW_ANALYSIS_SCHEMA + validate_task_response)
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ── 多源仲裁类型 ─────────────────────────────────────────────────────

# 旧版兼容映射 — 加载旧 JSON 时自动转换为新类型
_AGREEMENT_COMPAT_MAP: dict[str, str] = {
    "both_match": "subtitle_match",
    "minor_divergence": "subtitle_divergence",
    "major_divergence": "subtitle_divergence",
    "asr_only": "no_subtitle",
    "api_only": "subtitle_match",
    "script_only": "no_subtitle",
    "all_diverge": "subtitle_divergence",
    "vlm_override": "subtitle_divergence",
}

_CHOSEN_SOURCE_COMPAT_MAP: dict[str, str] = {
    "asr": "audio",
    "api": "subtitle",
    "script": "subtitle",
    "both": "subtitle",
    "vlm": "subtitle",
}

AGREEMENT_TYPES = Literal[
    "subtitle_match",
    "subtitle_divergence",
    "no_subtitle",
    "screen_text_only",
]

CHOSEN_SOURCE = Literal["subtitle", "audio", "screen_text"]


class SourceAccuracy(BaseModel):
    """VLM 字幕来源仲裁结果 — 判断字幕来源并给出置信度。

    VLM 直接从视频画面提取字幕信息，对比硬字幕与语音是否一致，
    确定最终对白文本的可靠来源。
    """

    agreement: AGREEMENT_TYPES = "subtitle_match"
    chosen_source: CHOSEN_SOURCE = "subtitle"
    vlm_override_text: str | None = None
    reason: str = ""

    @field_validator("agreement", mode="before")
    @classmethod
    def _normalize_agreement(cls, v: Any) -> str:
        """向后兼容: 旧版 agreement 类型自动映射为新类型。"""
        if isinstance(v, str) and v in _AGREEMENT_COMPAT_MAP:
            return _AGREEMENT_COMPAT_MAP[v]
        return v

    @field_validator("chosen_source", mode="before")
    @classmethod
    def _normalize_chosen_source(cls, v: Any) -> str:
        """向后兼容: 旧版 chosen_source 类型自动映射为新类型。"""
        if isinstance(v, str) and v in _CHOSEN_SOURCE_COMPAT_MAP:
            return _CHOSEN_SOURCE_COMPAT_MAP[v]
        return v


# ── 枚举 ────────────────────────────────────────────────────────────

class TimelineMode(str, Enum):
    present = "present"
    flashback = "flashback"
    flashforward = "flashforward"
    dream = "dream"
    unknown = "unknown"


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class DialogueKind(str, Enum):
    dialogue = "dialogue"
    screen_text = "screen_text"


class CandidateType(str, Enum):
    highlight = "highlight"
    hook = "hook"


# ── 子模型 ─────────────────────────────────────────────────────────

class Window(BaseModel):
    start: float
    end: float


class TimelineSegment(BaseModel):
    start: float
    end: float
    mode: TimelineMode
    entry_signal: str = ""
    exit_signal: str = ""
    summary: str = Field(..., min_length=1)


class BoundaryContext(BaseModel):
    starts_mid_scene: bool
    ends_mid_scene: bool
    continues_from_previous_window: bool
    continues_into_next_window: bool
    start_state: str = ""
    end_state: str = ""


class StoryBeat(BaseModel):
    start: float
    end: float
    function: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    characters: list[str] = []
    cause: str = ""
    effect: str = ""
    open_question: str = ""


class DialogueEvent(BaseModel):
    start: float
    end: float
    speaker_or_source: str = ""
    kind: DialogueKind
    text: str = Field(..., min_length=1)
    confidence: Confidence
    source_accuracy: SourceAccuracy | None = None


class VisualEvent(BaseModel):
    start: float
    end: float
    description: str = Field(..., min_length=1)
    characters: list[str] = []
    emotion: str = ""
    action: str = ""
    conflict: str = ""
    visual_impact: str = ""


class HighlightCandidate(BaseModel):
    """高光/钩子候选。

    VLM 输出的结构化高光信息。anchor/lead_in 保留原始文本字段向后兼容，
    新增的 _ts/_seconds/tags/emotion/shot_size/camera_move/characters 字段
    供后续打分和切点优化使用，当前阶段仅存储不消费。
    """
    id: str = Field(..., min_length=1)
    start: float
    end: float
    type: CandidateType
    strength: int = Field(..., ge=1, le=10)
    reason: str = Field(..., min_length=1)
    anchor: str = Field(..., min_length=1)
    lead_in: str = ""
    payoff_or_open_question: str = ""
    dialogue_excerpt: str = ""
    # ── 新增多模态字段（VLM 输出，下游暂不消费） ──
    anchor_ts: float | None = Field(default=None, description="爆点精确时间戳(秒)，从anchor文本中提取或VLM直出")
    tags: list[str] = Field(default_factory=list, description="结构化高光标签，如['名场面','爽点','黑翼爆发']")
    emotion: str = Field(default="", description="核心情绪: badass/sad/shock/sweet/angry/tense")
    shot_size: str = Field(default="", description="景别: closeup/medium/wide")
    camera_move: list[str] = Field(default_factory=list, description="镜头运动: static/zoom_in/zoom_out/pan/follow/slowmo")
    lead_in_seconds: float | None = Field(default=None, description="建议前摇时长(秒)，动作0.5-1s/情绪2-3s/反转3-5s")
    characters: list[str] = Field(default_factory=list, description="核心出场主角列表")


class CharacterAppearance(BaseModel):
    """角色出场记录 — 谁在画面中，什么样子，何时出现/消失。

    VLM 从视频画面直接识别角色，不依赖 API 预置的角色信息。
    """
    name: str = Field(..., min_length=1, description="角色名")
    description: str = Field(default="", description="外观描述（衣着、年龄、特征）")
    role: str = Field(default="", description="角色定位（主角/配角/反派/路人）")
    first_seen: float = Field(..., description="首次出现时间（秒）")
    last_seen: float = Field(default=0.0, description="最后出现时间（秒）")
    source: str = Field(default="visual", description="识别来源: visual/title_card/dialogue/voice")


class SceneLocation(BaseModel):
    """场景位置 — 故事发生在哪里，什么时间，有哪些角色在场。

    VLM 从视觉事件和窗口摘要中提取结构化场景信息，
    供下游 episode_digests 和 story_scripts 使用。
    """
    name: str = Field(..., min_length=1, description="场景名称（如 '破旧木屋'、'教堂'）")
    description: str = Field(default="", description="场景描述（环境、氛围、视觉特征）")
    start: float = Field(..., description="场景开始时间（秒）")
    end: float = Field(..., description="场景结束时间（秒）")
    time_of_day: str = Field(default="", description="时间（白天/夜晚/黄昏/黎明）")
    characters_present: list[str] = Field(default_factory=list, description="在场角色")


class WindowAnalysisResult(BaseModel):
    source_id: str = Field(..., min_length=1)
    episode: int = Field(..., ge=1)
    window_id: str = Field(..., min_length=1)
    window: Window
    window_summary: str = Field(..., min_length=1)
    timeline_segments: list[TimelineSegment] = []
    boundary_context: BoundaryContext
    character_appearances: list[CharacterAppearance] = []
    scene_locations: list[SceneLocation] = []
    story_beats: list[StoryBeat] = []
    dialogue_and_text: list[DialogueEvent] = []
    visual_events: list[VisualEvent] = []
    candidates: list[HighlightCandidate] = []

    model_config = {"extra": "ignore"}  # 允许旧版输出不带新字段


class WindowJobResult(BaseModel):
    """单个 window job 的完整输出 — 用于 validate_task_response 返回。"""
    windows: list[WindowAnalysisResult] = Field(default_factory=list)
    model_config = {"extra": "forbid"}


# ── 导出: 旧 story_schemas 兼容的 dict-based 表示 ───────────────────

def as_dict_schema() -> dict[str, Any]:
    """生成与旧 WINDOW_ANALYSIS_SCHEMA 字节级兼容的 dict schema。"""
    return {
        "type": "object",
        "properties": {
            "source_id": {"type": "string", "minLength": 1},
            "episode": {"type": "integer", "minimum": 1},
            "window_id": {"type": "string", "minLength": 1},
            "window": {
                "type": "object",
                "properties": {"start": {"type": "number"}, "end": {"type": "number"}},
                "required": ["start", "end"],
                "additionalProperties": False,
            },
            "window_summary": {"type": "string", "minLength": 1},
            "timeline_segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "number"},
                        "end": {"type": "number"},
                        "mode": {
                            "type": "string",
                            "enum": ["present", "flashback", "flashforward", "dream", "unknown"],
                        },
                        "entry_signal": {"type": "string"},
                        "exit_signal": {"type": "string"},
                        "summary": {"type": "string", "minLength": 1},
                    },
                    "required": ["start", "end", "mode", "entry_signal", "exit_signal", "summary"],
                    "additionalProperties": False,
                },
            },
            "boundary_context": {
                "type": "object",
                "properties": {
                    "starts_mid_scene": {"type": "boolean"},
                    "ends_mid_scene": {"type": "boolean"},
                    "continues_from_previous_window": {"type": "boolean"},
                    "continues_into_next_window": {"type": "boolean"},
                    "start_state": {"type": "string"},
                    "end_state": {"type": "string"},
                },
                "required": [
                    "starts_mid_scene", "ends_mid_scene",
                    "continues_from_previous_window", "continues_into_next_window",
                    "start_state", "end_state",
                ],
                "additionalProperties": False,
            },
            "character_appearances": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "description": {"type": "string"},
                        "role": {"type": "string"},
                        "first_seen": {"type": "number"},
                        "last_seen": {"type": "number"},
                        "source": {"type": "string"},
                    },
                    "required": ["name", "first_seen"],
                    "additionalProperties": False,
                },
            },
            "scene_locations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "description": {"type": "string"},
                        "start": {"type": "number"},
                        "end": {"type": "number"},
                        "time_of_day": {"type": "string"},
                        "characters_present": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "start", "end"],
                    "additionalProperties": False,
                },
            },
            "story_beats": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "number"},
                        "end": {"type": "number"},
                        "function": {"type": "string", "minLength": 1},
                        "summary": {"type": "string", "minLength": 1},
                        "characters": {"type": "array", "items": {"type": "string"}},
                        "cause": {"type": "string"},
                        "effect": {"type": "string"},
                        "open_question": {"type": "string"},
                    },
                    "required": ["start", "end", "function", "summary", "characters", "cause", "effect", "open_question"],
                    "additionalProperties": False,
                },
            },
            "dialogue_and_text": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "number"},
                        "end": {"type": "number"},
                        "speaker_or_source": {"type": "string"},
                        "kind": {"type": "string", "enum": ["dialogue", "screen_text"]},
                        "text": {"type": "string", "minLength": 1},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "source_accuracy": {
                            "type": "object",
                            "properties": {
                                "agreement": {
                                    "type": "string",
                                    "enum": [
                                        "subtitle_match",
                                        "subtitle_divergence",
                                        "no_subtitle",
                                        "screen_text_only",
                                    ],
                                },
                                "chosen_source": {
                                    "type": "string",
                                    "enum": ["subtitle", "audio", "screen_text"],
                                },
                                "vlm_override_text": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["agreement", "chosen_source", "reason"],
                            "additionalProperties": False,
                        },
                    },
                    "required": ["start", "end", "speaker_or_source", "kind", "text", "confidence"],
                    "additionalProperties": False,
                },
            },
            "visual_events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "number"},
                        "end": {"type": "number"},
                        "description": {"type": "string", "minLength": 1},
                        "characters": {"type": "array", "items": {"type": "string"}},
                        "emotion": {"type": "string"},
                        "action": {"type": "string"},
                        "conflict": {"type": "string"},
                        "visual_impact": {"type": "string"},
                    },
                    "required": ["start", "end", "description", "characters", "emotion", "action", "conflict", "visual_impact"],
                    "additionalProperties": False,
                },
            },
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "start": {"type": "number"},
                        "end": {"type": "number"},
                        "type": {"type": "string", "enum": ["highlight", "hook"]},
                        "strength": {"type": "integer", "minimum": 1, "maximum": 10},
                        "reason": {"type": "string", "minLength": 1},
                        "anchor": {"type": "string", "minLength": 1},
                        "lead_in": {"type": "string"},
                        "payoff_or_open_question": {"type": "string"},
                        "dialogue_excerpt": {"type": "string"},
                        # 新增多模态字段（可选，VLM输出，下游暂不消费）
                        "anchor_ts": {"type": "number"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "emotion": {"type": "string"},
                        "shot_size": {"type": "string"},
                        "camera_move": {"type": "array", "items": {"type": "string"}},
                        "lead_in_seconds": {"type": "number"},
                        "characters": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "start", "end", "type", "strength", "reason", "anchor", "lead_in", "payoff_or_open_question", "dialogue_excerpt"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "source_id", "episode", "window_id", "window", "window_summary",
            "timeline_segments", "boundary_context",
            "character_appearances", "scene_locations",
            "story_beats", "dialogue_and_text", "visual_events", "candidates",
        ],
        "additionalProperties": False,
    }


WINDOW_ANALYSIS_SCHEMA = as_dict_schema()
