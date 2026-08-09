"""数据库实体 Pydantic v2 模型 — 与 DB_SCHEMA.md 10 张表一一对应。

这些模型是流水线产出的**规范数据合同**——每个 Stage 产出的数据
必须能直接映射到对应的数据库表。模型定义包括：

- 字段类型、约束、默认值
- 表间关系（外键引用）
- 来源标记（api/vlm/script/asr）
- 置信度/验证状态

来源: DB_SCHEMA.md (PostgreSQL autocut schema)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── 1. books — 剧 ──────────────────────────────────────────────────────────


class Book(BaseModel):
    """整部剧的元数据，是所有其他实体的根节点。

    对应: autocut.books
    数据来源: API 4 (batch-content-assets) + API 5 (batch-episodes-info)
    """

    book_id: str = Field(..., description="剧的唯一标识，来自 API 的 bookId")
    book_name: str = Field(..., description="剧名")
    total_episodes: int | None = Field(None, ge=1, description="总集数")
    source_type: str = Field(
        default="vlm_only",
        description="数据来源类型: api_script / api_only / vlm_only",
    )
    overall_synopsis: str | None = Field(None, description="350~450 字整体剧情概括")
    genre: str | None = Field(
        None,
        description="类型: romance/revenge/fantasy/mystery/identity_reversal/family_drama",
    )
    sub_genre: str | None = Field(None, description="子类型")
    mood: str | None = Field(None, description="情绪基调: dark/sweet/intense/suspense")
    era: str | None = Field(None, description="时代背景: modern/historical/fantasy_world")
    language: str = Field(default="zh", description="语言: zh/en/ko")
    tags: list[str] = Field(default_factory=list, description="合并所有标签")
    script_parsed: dict[str, Any] | None = Field(None, description="LLM 解析后的剧本结构")
    script_sha: str | None = Field(None, description="原始剧本 SHA-256")
    script_raw_path: str | None = Field(None, description="原始剧本文件路径")
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── 2. subjects — 角色 ─────────────────────────────────────────────────────


class Subject(BaseModel):
    """全剧角色信息。

    对应: autocut.subjects
    数据来源: API 4 CharacterAsset + API 5 CharacterInfo
    """

    id: int | None = Field(None, description="自增主键 (SERIAL)")
    book_id: str = Field(..., description="所属剧 ID")
    name: str = Field(..., description="角色名称，同一剧中唯一")
    aliases: list[str] = Field(default_factory=list, description="别名列表")
    persona: str | None = Field(None, description="角色概述")
    personality: list[str] = Field(default_factory=list, description="性格关键词")
    traits: str | None = Field(None, description="可复用识别特征")
    tone: str | None = Field(None, description="说话语气")
    voice_timbre: str | None = Field(None, description="音色描述 (预留 TTS)")
    visual_features: str | None = Field(None, description="预裁剪的视觉特征")
    relationship: str | None = Field(None, description="单集角色关系描述")
    role: str | None = Field(
        None,
        description="角色定位: male_lead/female_lead/antagonist/supporting",
    )
    first_episode: int | None = Field(None, ge=1, description="首次出现集数")
    last_episode: int | None = Field(None, ge=1, description="最后出现集数")
    source: str = Field(default="vlm", description="数据来源: api/vlm/script")
    vlm_verified: bool = Field(default=False, description="VLM+ASR 验证后设为 true")
    vlm_verified_at: datetime | None = None
    created_at: datetime | None = None


# ── 3. relationships — 角色关系 ────────────────────────────────────────────


class Relationship(BaseModel):
    """角色之间的关系。

    对应: autocut.relationships
    数据来源: API 4 relationships
    """

    id: int | None = Field(None, description="自增主键")
    book_id: str = Field(..., description="所属剧 ID")
    source_subject_id: int = Field(..., description="源角色 ID")
    target_subject_id: int = Field(..., description="目标角色 ID")
    description: str | None = Field(None, description="关系描述")
    source: str = Field(default="api", description="数据来源: api/vlm")
    created_at: datetime | None = None


# ── 4. episodes — 集 ───────────────────────────────────────────────────────


class Episode(BaseModel):
    """每集的基本信息。

    对应: autocut.episodes
    数据来源: API 5 episodes
    """

    episode_id: int = Field(..., ge=1, description="集号")
    book_id: str = Field(..., description="所属剧 ID")
    chapter_id: int | None = Field(None, description="API 返回的章节 ID")
    title: str | None = Field(None, description="从剧本解析出的集名")
    summary: str | None = Field(None, description="本集剧情摘要")
    is_free: bool = Field(
        default=False,
        description="是否免费集数。关键约束: 剪辑只能从免费集中选素材",
    )
    scene_count: int | None = Field(None, ge=0, description="本集场景数")
    duration: float | None = Field(None, ge=0, description="视频时长(秒)")
    source: str = Field(default="vlm", description="数据来源: api/vlm")
    vlm_verified: bool = Field(default=False)


# ── 5. subtitles — 字幕 ────────────────────────────────────────────────────


class Subtitle(BaseModel):
    """带时间戳的对白，VLM 上下文注入的核心数据。

    对应: autocut.subtitles
    数据来源: API 5 subtitles
    """

    id: int | None = Field(None, description="自增主键")
    book_id: str = Field(..., description="所属剧 ID")
    episode_id: int = Field(..., ge=1, description="所属集号")
    start_time: float = Field(..., ge=0, description="起始时间(秒)")
    end_time: float = Field(..., ge=0, description="结束时间(秒)")
    speaker: str | None = Field(None, description="说话人")
    text: str = Field(..., description="字幕文本 (精确对白)")
    tone: str | None = Field(None, description="语气")
    emotion: str | None = Field(None, description="情绪强度")
    group_id: int | None = Field(None, description="分组 ID")
    group_tone: str | None = Field(None, description="TTS 表演指示")
    source: str = Field(default="api", description="数据来源: api/asr")
    confidence: float | None = Field(None, ge=0, le=1, description="ASR 置信度")
    cer_estimate: float | None = Field(None, ge=0, description="估计字符错误率")


# ── 5b. speaker_mappings — ASR 说话人映射 ──────────────────────────────────


class SpeakerMapping(BaseModel):
    """ASR 临时标签到角色名的映射。

    对应: autocut.speaker_mappings
    """

    id: int | None = Field(None, description="自增主键")
    book_id: str = Field(..., description="所属剧 ID")
    episode_id: int = Field(..., ge=1, description="所属集号")
    speaker_label: str = Field(..., description="ASR 临时标签 (speaker_01, speaker_02...)")
    mapped_subject_id: int | None = Field(None, description="映射到的角色 ID")
    confidence: float = Field(default=0.0, ge=0, le=1, description="映射置信度")
    resolved_by: str | None = Field(None, description="解析方式: vlm/api/manual")
    resolved_at: datetime | None = None
    created_at: datetime | None = None


# ── 5c. subject_episodes — 角色按集出场 ────────────────────────────────────


class SubjectEpisode(BaseModel):
    """角色在每集中的出场信息。

    对应: autocut.subject_episodes
    与 subjects 的分工: subjects 存全局信息，此表存按集变化的数据。
    """

    id: int | None = Field(None, description="自增主键")
    subject_id: int = Field(..., description="关联 subjects.id")
    book_id: str = Field(..., description="所属剧 ID")
    episode_id: int = Field(..., ge=1, description="所属集号")
    relationship: str | None = Field(None, description="本集角色关系描述")
    visual_features: str | None = Field(None, description="本集外表特征")
    appears_in_episode: bool = Field(default=True, description="本集是否出场")
    source: str = Field(default="api", description="数据来源: api/vlm")
    created_at: datetime | None = None


# ── 6. shots — 分镜 ────────────────────────────────────────────────────────


class Shot(BaseModel):
    """API 5 返回的分镜信息。

    对应: autocut.shots
    """

    id: int | None = Field(None, description="自增主键")
    book_id: str = Field(..., description="所属剧 ID")
    episode_id: int = Field(..., ge=1, description="所属集号")
    start_time: float | None = Field(None, ge=0, description="起始时间(秒)")
    end_time: float | None = Field(None, ge=0, description="结束时间(秒)")
    scene: str | None = Field(None, description="场景描述")
    subjects: list[str] = Field(default_factory=list, description="主体角色列表")
    actions: str | None = Field(None, description="动作描述")
    is_highlight: bool = Field(default=False, description="是否高亮片段")
    highlight_score: int | None = Field(None, description="高亮评分")
    highlight_reason: str | None = Field(None, description="高亮原因")
    related_srt_range: str | None = Field(None, description="相关字幕范围")
    source: str = Field(default="api", description="数据来源")


# ── 7. scenes — 场景 ───────────────────────────────────────────────────────


class Scene(BaseModel):
    """LLM 解析剧本或 VLM 推断的场景。

    对应: autocut.scenes
    """

    scene_id: str = Field(..., description="场景 ID")
    book_id: str = Field(..., description="所属剧 ID")
    episode_id: int = Field(..., ge=1, description="所属集号")
    scene_order: int | None = Field(None, ge=1, description="本集内场景序号")
    heading: str | None = Field(None, description="场景标题")
    location: str | None = Field(None, description="地点")
    time_of_day: str | None = Field(None, description="时间: 日/夜/黄昏")
    is_flashback: bool = Field(default=False, description="是否闪回")
    flashback_label: str | None = Field(None, description="闪回标签")
    characters_present: list[str] = Field(default_factory=list, description="出场角色")
    dialogues: list[dict[str, Any]] = Field(default_factory=list, description="场景对白")
    raw_description: str | None = Field(None, description="L1 降级: 完整剧本原文")
    distilled_summary: str | None = Field(None, description="L2 降级: 1-2 句摘要")
    meta_tags: dict[str, Any] = Field(default_factory=dict, description="L3 降级: 元标签")
    start_time: float | None = Field(None, ge=0, description="对齐后视频时间戳(秒)")
    end_time: float | None = Field(None, ge=0, description="对齐后视频时间戳(秒)")
    alignment_confidence: str | None = Field(
        None, description="对齐置信度: exact/fuzzy/inferred/none"
    )
    alignment_source: str | None = Field(None, description="对齐来源: api_subtitle/asr/heuristic")
    source: str = Field(default="vlm", description="数据来源: script/vlm/api")
    detected_in_video: bool = Field(default=False, description="VLM 检测到")
    vlm_verified: bool = Field(default=False)
    vlm_verified_at: datetime | None = None


# ── 8. boundaries — 时间边界索引 ───────────────────────────────────────────


class Boundary(BaseModel):
    """剪辑所需的精确 start/end 事件统一索引。

    对应: autocut.boundaries
    来源覆盖 API 和 VLM+ASR。
    span_candidates 查询时只从 is_free=true 的集中选素材。
    """

    boundary_id: str = Field(..., description="边界 ID")
    book_id: str = Field(..., description="所属剧 ID")
    episode_id: int = Field(..., ge=1, description="所属集号")
    event_type: str = Field(
        ...,
        description="事件类型: dialogue/action/sound/scene_change/highlight",
    )
    start_time: float = Field(..., ge=0, description="起始时间(秒)")
    end_time: float = Field(..., ge=0, description="结束时间(秒)")
    description: str | None = Field(None, description="事件描述")
    subjects: list[str] = Field(default_factory=list, description="涉及的角色")
    source_table: str = Field(
        ...,
        description="来源表: subtitles/shots/scenes/window_summaries",
    )
    source_id: str | None = Field(None, description="原始记录 ID")
    confidence: str = Field(
        default="low",
        description="置信度: high/medium/low",
    )
    precision: float = Field(default=2.0, ge=0, description="当前精度 ±秒")
    verified_by: list[str] = Field(default_factory=list, description="验证来源链")
    corrected_at: datetime | None = Field(None, description="最后修正时间")


# ── 类型别名 ────────────────────────────────────────────────────────────────

# 跨表联合类型
Entity = Book | Subject | Relationship | Episode | Subtitle | SpeakerMapping | SubjectEpisode | Shot | Scene | Boundary

# 表名映射
TABLE_NAMES: dict[str, type[BaseModel]] = {
    "books": Book,
    "subjects": Subject,
    "relationships": Relationship,
    "episodes": Episode,
    "subtitles": Subtitle,
    "speaker_mappings": SpeakerMapping,
    "subject_episodes": SubjectEpisode,
    "shots": Shot,
    "scenes": Scene,
    "boundaries": Boundary,
}