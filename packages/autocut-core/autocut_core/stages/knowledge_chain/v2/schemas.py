"""v2 知识链输出Pydantic Schema"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Chapter(BaseModel):
    start_ep: int
    end_ep: int
    title: str
    arc_type: str
    core_conflict: str
    climax_episode: int
    boundary_reason: str
    chapter_id: str = ""


class Theme(BaseModel):
    """核心主题模型"""

    name: str
    weight: float = Field(ge=0, le=1, default=0.5)
    first_ep: int = 1
    related_thread_ids: list[str] = Field(default_factory=list)
    related_char_ids: list[str] = Field(default_factory=list)


class StoryThread(BaseModel):
    id: str
    name: str
    description: str = ""
    summary: str = ""
    chapter_coverage: list[int] = Field(default_factory=list)
    key_episodes: list[int] = Field(default_factory=list)
    importance_tier: Literal["primary", "secondary", "tertiary"] = "secondary"
    importance: float = Field(ge=0, le=1, default=0.5)
    is_primary: bool = False
    beat_ids: list[str] = Field(default_factory=list)
    character_ids: list[str] = Field(default_factory=list)


class Character(BaseModel):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    importance_tier: Literal["protagonist", "supporting", "minor"] = "minor"
    importance: float = Field(ge=0, le=1, default=0.2)
    is_core: bool = False
    first_seen_ep: int = 1
    last_seen_ep: int = 1
    final_state: str = "未知"
    state_milestones: list[dict[str, object]] = Field(default_factory=list)
    evidence_event_ids: list[str] = Field(default_factory=list)
    arc_summary: str = ""


class Beat(BaseModel):
    id: str
    chapter_id: str = ""
    thread_id: str = ""
    episode: int = 1
    phase: Literal["setup", "escalation", "turn", "reveal", "payoff", "consequence", "coda"] = (
        "setup"
    )
    summary: str = ""
    evidence_event_ids: list[str] = Field(default_factory=list)
    requires_beat_ids: list[str] = Field(default_factory=list)
    is_foreshadow_setup: bool = False


class Relationship(BaseModel):
    id: str
    from_char_id: str
    to_char_id: str
    type: str = "other"
    summary: str = ""
    importance: float = 0.5
    evidence_event_ids: list[str] = Field(default_factory=list)


class Fact(BaseModel):
    id: str
    content: str
    episode: int
    evidence_event_ids: list[str] = Field(default_factory=list)


class WorldRule(BaseModel):
    id: str
    content: str
    type: str = "other"
    first_ep: int = 1
    mentioned_eps: list[int] = Field(default_factory=list)
    contradiction_warning: str | None = None


class TurningPoint(BaseModel):
    ep: int
    description: str
    significance: str = "minor"
    importance: float = 0.5
    must_include: bool = False


class TensionPoint(BaseModel):
    ep: int
    tension: int = Field(ge=1, le=10)
    keywords: list[str] = Field(default_factory=list)


class ExcludedEpisode(BaseModel):
    episode: int
    event_ids: list[str] = Field(default_factory=list)
    reason_type: str
    explanation: str


class Question(BaseModel):
    id: str
    content: str
    first_ep: int = 1
    related_char_ids: list[str] = Field(default_factory=list)
    related_event_ids: list[str] = Field(default_factory=list)
    is_resolved: bool = False
    resolved_ep: int | None = None
    resolution: str | None = None


class ForeshadowPair(BaseModel):
    id: str
    description: str
    setup_ep: int
    setup_beat_id: str | None = None
    payoff_ep: int | None = None
    payoff_beat_id: str | None = None
    is_resolved: bool = False
    importance: float = 0.5


class KnowledgeChainV2ExtraConfig(BaseModel):
    """v2扩展字段配置，默认全部开启"""

    enable_themes: bool = True
    enable_world_rules: bool = True
    enable_questions: bool = True
    enable_foreshadows: bool = True


class KnowledgeChainV2Output(BaseModel):
    schema_version: str = "2.0"
    metadata: dict[str, object] = Field(default_factory=dict)
    chapters: list[Chapter] = Field(default_factory=list)
    story_threads: list[StoryThread] = Field(default_factory=list)
    characters: list[Character] = Field(default_factory=list)
    beats: list[Beat] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    turning_points: list[TurningPoint] = Field(default_factory=list)
    tension_curve: list[TensionPoint] = Field(default_factory=list)
    themes: list[Theme] = Field(default_factory=list)
    world_rules: list[WorldRule] = Field(default_factory=list)
    open_questions: list[Question] = Field(default_factory=list)
    resolved_questions: list[Question] = Field(default_factory=list)
    foreshadow_pairs: list[ForeshadowPair] = Field(default_factory=list)
    excluded_episodes: list[ExcludedEpisode] = Field(default_factory=list)
