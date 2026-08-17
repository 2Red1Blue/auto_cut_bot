"""集/章消化 Schema — Pydantic v2 实现。

原 story_schemas.py: EPISODE_DIGEST_SCHEMA + CHAPTER_DIGEST_SCHEMA
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ThreadStatus(str, Enum):
    introduced = "introduced"
    advanced = "advanced"
    partially_resolved = "partially_resolved"
    resolved = "resolved"
    open = "open"


class QuestionStatus(str, Enum):
    open = "open"
    resolved = "resolved"


# ── 剧集消化 (Episode Digest) ──────────────────────────────────────

class EpisodeCharacter(BaseModel):
    character_key: str = Field(..., min_length=1)
    canonical_name: str = Field(..., min_length=1)
    aliases: list[str] = []
    identity: str = ""
    goals: list[str] = []
    evidence_event_ids: list[str] = Field(..., min_length=1)


class EpisodeRelationship(BaseModel):
    relationship_key: str = Field(..., min_length=1)
    character_keys: list[str] = Field(..., min_length=1)
    state: str = Field(..., min_length=1)
    change: str = ""
    evidence_event_ids: list[str] = Field(..., min_length=1)


class EpisodeThreadUpdate(BaseModel):
    thread_key: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    status: ThreadStatus
    summary: str = Field(..., min_length=1)
    event_ids: list[str] = Field(..., min_length=1)


class EpisodeFact(BaseModel):
    fact_key: str = Field(..., min_length=1)
    statement: str = Field(..., min_length=1)
    event_ids: list[str] = Field(..., min_length=1)


class EpisodeQuestion(BaseModel):
    question_key: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1)
    status: QuestionStatus
    event_ids: list[str] = Field(..., min_length=1)


class EpisodeDigestResult(BaseModel):
    schema_version: str = "1.0"
    episode: int = Field(..., ge=1)
    source_ids: list[str] = Field(..., min_length=1)
    window_ids: list[str] = Field(..., min_length=1)
    opening_state: str = Field(..., min_length=1)
    ending_state: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    characters: list[EpisodeCharacter] = []
    relationships: list[EpisodeRelationship] = []
    event_ids: list[str] = Field(..., min_length=1)
    story_thread_updates: list[EpisodeThreadUpdate] = []
    facts: list[EpisodeFact] = []
    open_questions: list[EpisodeQuestion] = []
    highlight_candidate_ids: list[str] = []
    hook_candidate_ids: list[str] = []
    # Phase 1 deterministic signal fields (non-required, backward-compatible)
    character_mentions: list[dict] = Field(default_factory=list)
    event_summary_signals: dict = Field(default_factory=dict)
    summary_quality: str = "generated"

    model_config = {"extra": "ignore"}


# ── 章消化 (Chapter Digest) ────────────────────────────────────────

class ChapterCharacterRollup(BaseModel):
    character_key: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    state_at_start: str = ""
    state_at_end: str = ""
    episode_numbers: list[int] = []
    evidence_event_ids: list[str] = Field(..., min_length=1)


class ChapterRelationshipRollup(BaseModel):
    relationship_key: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    evidence_event_ids: list[str] = Field(..., min_length=1)


class ChapterThreadSummary(BaseModel):
    thread_key: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    status: str  # resolved | partially_resolved | open
    event_ids: list[str] = Field(..., min_length=1)


class ChapterDigestResult(BaseModel):
    schema_version: str = "1.0"
    chapter_id: str = Field(..., min_length=1)
    episodes: list[int] = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    character_rollup: list[ChapterCharacterRollup] = []
    relationship_rollup: list[ChapterRelationshipRollup] = []
    story_threads: list[ChapterThreadSummary] = []
    fact_keys: list[str] = []
    event_ids: list[str] = Field(..., min_length=1)
    open_question_keys: list[str] = []

    # Pydantic 本地校验层用 "ignore" 兜底（防止 strict mode 下极少数 API 侧逃逸的
    # 幻觉字段导致抛出 ValidationError）。传给 LLM API 的 dict schema 仍为 strict
    # 模式（additionalProperties: False，所有 object 级都严格禁止额外字段）。
    # 根因防御：通过 _clean_episode_for_chapter() 在 context 准备阶段剥离会误导
    # LLM 的字段（空 rollup 数组、本地信号字段、元数据字段），从源头上减少字段幻觉。
    model_config = {"extra": "ignore"}


# ── 旧兼容 dict schema ────────────────────────────────────────────

def episode_dict_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "const": "1.0"},
            "episode": {"type": "integer", "minimum": 1},
            "source_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "window_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "opening_state": {"type": "string", "minLength": 1},
            "ending_state": {"type": "string", "minLength": 1},
            "summary": {"type": "string", "minLength": 1},
            "characters": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "character_key": {"type": "string", "minLength": 1},
                    "canonical_name": {"type": "string", "minLength": 1},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "identity": {"type": "string"},
                    "goals": {"type": "array", "items": {"type": "string"}},
                    "evidence_event_ids": {"type": "array", "items": _event_id_pat(), "minItems": 1},
                },
                "required": ["character_key", "canonical_name", "aliases", "identity", "goals", "evidence_event_ids"],
                "additionalProperties": False,
            }},
            "relationships": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "relationship_key": {"type": "string", "minLength": 1},
                    "character_keys": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "state": {"type": "string", "minLength": 1},
                    "change": {"type": "string"},
                    "evidence_event_ids": {"type": "array", "items": _event_id_pat(), "minItems": 1},
                },
                "required": ["relationship_key", "character_keys", "state", "change", "evidence_event_ids"],
                "additionalProperties": False,
            }},
            "event_ids": {"type": "array", "items": _event_id_pat(), "minItems": 1},
            "story_thread_updates": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "thread_key": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "status": {"type": "string", "enum": ["introduced", "advanced", "partially_resolved", "resolved", "open"]},
                    "summary": {"type": "string", "minLength": 1},
                    "event_ids": {"type": "array", "items": _event_id_pat(), "minItems": 1},
                },
                "required": ["thread_key", "title", "status", "summary", "event_ids"],
                "additionalProperties": False,
            }},
            "facts": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "fact_key": {"type": "string", "minLength": 1},
                    "statement": {"type": "string", "minLength": 1},
                    "event_ids": {"type": "array", "items": _event_id_pat(), "minItems": 1},
                },
                "required": ["fact_key", "statement", "event_ids"],
                "additionalProperties": False,
            }},
            "open_questions": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "question_key": {"type": "string", "minLength": 1},
                    "question": {"type": "string", "minLength": 1},
                    "status": {"type": "string", "enum": ["open", "resolved"]},
                    "event_ids": {"type": "array", "items": _event_id_pat(), "minItems": 1},
                },
                "required": ["question_key", "question", "status", "event_ids"],
                "additionalProperties": False,
            }},
            "highlight_candidate_ids": {"type": "array", "items": {"type": "string"}},
            "hook_candidate_ids": {"type": "array", "items": {"type": "string"}},
            # Phase 1: deterministic signal fields (non-required, backward-compatible)
            "character_mentions": {"type": "array", "items": {"type": "object"}},
            "event_summary_signals": {"type": "object"},
            "summary_quality": {"type": "string"},
        },
        "required": [
            "schema_version", "episode", "source_ids", "window_ids",
            "opening_state", "ending_state", "summary", "characters",
            "relationships", "event_ids", "story_thread_updates",
            "facts", "open_questions", "highlight_candidate_ids", "hook_candidate_ids",
        ],
        "additionalProperties": False,
    }


def chapter_dict_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "const": "1.0"},
            "chapter_id": {"type": "string", "minLength": 1},
            "episodes": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1},
            "summary": {"type": "string", "minLength": 1},
            "character_rollup": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "character_key": {"type": "string", "minLength": 1},
                    "name": {"type": "string", "minLength": 1},
                    "state_at_start": {"type": "string"},
                    "state_at_end": {"type": "string"},
                    "episode_numbers": {"type": "array", "items": {"type": "integer"}},
                    "evidence_event_ids": {"type": "array", "items": _event_id_pat(), "minItems": 1},
                },
                "required": ["character_key", "name", "state_at_start", "state_at_end", "episode_numbers", "evidence_event_ids"],
                "additionalProperties": False,
            }},
            "relationship_rollup": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "relationship_key": {"type": "string", "minLength": 1},
                    "summary": {"type": "string", "minLength": 1},
                    "evidence_event_ids": {"type": "array", "items": _event_id_pat(), "minItems": 1},
                },
                "required": ["relationship_key", "summary", "evidence_event_ids"],
                "additionalProperties": False,
            }},
            "story_threads": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "thread_key": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "summary": {"type": "string", "minLength": 1},
                    "status": {"type": "string", "enum": ["resolved", "partially_resolved", "open"]},
                    "event_ids": {"type": "array", "items": _event_id_pat(), "minItems": 1},
                },
                "required": ["thread_key", "title", "summary", "status", "event_ids"],
                "additionalProperties": False,
            }},
            "fact_keys": {"type": "array", "items": {"type": "string"}},
            "event_ids": {"type": "array", "items": _event_id_pat(), "minItems": 1},
            "open_question_keys": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "schema_version", "chapter_id", "episodes", "summary",
            "character_rollup", "relationship_rollup", "story_threads",
            "fact_keys", "event_ids", "open_question_keys",
        ],
        # Strict JSON mode 要求所有 object 级 additionalProperties=False。
        # 防止 LLM 从 episode_digest 输入中"复制"字段的正确做法是在 context
        # 准备阶段清洗掉会误导 LLM 的字段（_clean_episode_for_chapter），
        # 而不是放宽 schema——放宽会破坏 strict mode 对输出的强约束。
        "additionalProperties": False,
    }


def _event_id_pat() -> dict[str, Any]:
    return {"type": "string", "pattern": r"^event-[0-9a-f]{12}$"}
