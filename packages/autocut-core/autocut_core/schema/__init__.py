"""Schema 模块 — Pydantic v2 业务数据模型。

每个子模块对应一个领域的数据 schema:
  ids.py          — ID 正则模式 (EventId, CharId, ThreadId)
  window.py       — 窗口分析结果 (WindowAnalysisResult)
  episode.py      — 集/章消化结果
  registry.py     — 角色注册表 + Assignment + Bible
  story.py        — 故事 Catalog/Portfolio/Treatment/Script
  db_entities.py  — 数据库实体 (与 DB_SCHEMA.md 10 张表一一对应)
  compat.py       — dict schema 兼容桥
"""

from autocut_core.schema.db_entities import (
    Book,
    Boundary,
    Entity,
    Episode,
    Relationship,
    Scene,
    Shot,
    SpeakerMapping,
    Subject,
    SubjectEpisode,
    Subtitle,
    TABLE_NAMES,
)
from autocut_core.schema.window import WindowAnalysisResult, WindowJobResult

__all__ = [
    # window
    "WindowAnalysisResult",
    "WindowJobResult",
    # db_entities
    "Book",
    "Subject",
    "Relationship",
    "Episode",
    "Subtitle",
    "SpeakerMapping",
    "SubjectEpisode",
    "Shot",
    "Scene",
    "Boundary",
    "Entity",
    "TABLE_NAMES",
]