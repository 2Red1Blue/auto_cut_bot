"""窗口分析 Schema — Pydantic v2 实现。

原位置: story_schemas.py:92-190 (WINDOW_ANALYSIS_SCHEMA + validate_task_response)
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ── 多源仲裁类型 ─────────────────────────────────────────────────────

AGREEMENT_TYPES = Literal[
    "both_match",
    "minor_divergence",
    "major_divergence",
    "asr_only",
    "api_only",
    "script_only",
    "all_diverge",
    "vlm_override",
]

CHOSEN_SOURCE = Literal["asr", "api", "script", "both", "vlm"]


class SourceAccuracy(BaseModel):
    """VLM 多源字幕仲裁结果 — 对比 ASR/API/剧本三源后确定最准确的字幕。"""

    asr_text: str | None = None
    api_text: str | None = None
    script_text: str | None = None
    agreement: AGREEMENT_TYPES = "both_match"
    chosen_source: CHOSEN_SOURCE = "both"
    vlm_override_text: str | None = None
    reason: str = ""


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


class WindowAnalysisResult(BaseModel):
    source_id: str = Field(..., min_length=1)
    episode: int = Field(..., ge=1)
    window_id: str = Field(..., min_length=1)
    window: Window
    window_summary: str = Field(..., min_length=1)
    timeline_segments: list[TimelineSegment] = []
    boundary_context: BoundaryContext
    story_beats: list[StoryBeat] = []
    dialogue_and_text: list[DialogueEvent] = []
    visual_events: list[VisualEvent] = []
    candidates: list[HighlightCandidate] = []

    model_config = {"extra": "forbid"}


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
                                "asr_text": {"type": "string"},
                                "api_text": {"type": "string"},
                                "script_text": {"type": "string"},
                                "agreement": {
                                    "type": "string",
                                    "enum": [
                                        "both_match", "minor_divergence", "major_divergence",
                                        "asr_only", "api_only", "script_only",
                                        "all_diverge", "vlm_override",
                                    ],
                                },
                                "chosen_source": {
                                    "type": "string",
                                    "enum": ["asr", "api", "script", "both", "vlm"],
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
                    },
                    "required": ["id", "start", "end", "type", "strength", "reason", "anchor", "lead_in", "payoff_or_open_question", "dialogue_excerpt"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "source_id", "episode", "window_id", "window", "window_summary",
            "timeline_segments", "boundary_context", "story_beats",
            "dialogue_and_text", "visual_events", "candidates",
        ],
        "additionalProperties": False,
    }


WINDOW_ANALYSIS_SCHEMA = as_dict_schema()
