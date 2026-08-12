---
name: ac_series_knowledge
description: 剧集知识构建 — Event Cards 编译、Episode/Chapter Digests 生成、Series Registry（含高光 IoU 对比）、Series Assignment。覆盖 Stage 5-9，从 VLM 窗口分析结果中提取全剧事件、人物、关系、故事线，对比 VLM 高光与 API 高光，供下游故事生成和全局排序使用。
version: 0.1.0
stages: [5, 6, 7, 8, 9]
status: active
triggers:
  - "剧集理解"
  - "Event Cards"
  - "event_cards"
  - "Episode Digests"
  - "episode_digests"
  - "Chapter Digests"
  - "chapter_digests"
  - "Series Registry"
  - "series_registry"
  - "Series Assignment"
  - "series_assignment"
  - "人物关系"
  - "故事线归账"
anti_triggers:
  - "准备素材" → 使用 ac_source_prep
  - "生成故事" → 使用 ac_story_generation
  - "渲染视频" → 使用 ac_render
---

# 剧集知识 (ac_series_knowledge) — Stage 5-9

从 VLM 窗口分析结果中提取全剧事件、人物、关系、故事线，汇编为结构化知识，
供下游故事生成和计划编排使用。

## Stages

| Stage | Name | Description | Status |
|-------|------|-------------|--------|
| 5 | `event_cards` | 从 VLM visual_events 跨窗口聚合事件卡 | active |
| 6 | `episode_digests` | 单集摘要 | active |
| 7 | `chapter_digests` | 章节摘要 | active |
| 8 | `series_registry` | 全剧注册表（角色统一、关系网、故事线）+ VLM vs API 高光 IoU 对比 | active |
| 9 | `series_assignment` | 章节分配 | active |

## Input Artifacts

| Artifact | Source | Required |
|----------|--------|----------|
| Window Summaries | ac_source_prep (vlm_analysis) | yes |
| Window Manifest | ac_source_prep | yes |
| Source Manifest | ac_source_prep | yes |
| Global Context | ac_source_prep (global_context) | no |

## Output Artifacts

| Artifact | Path Pattern | Consumer |
|----------|-------------|----------|
| Event Cards | `event_cards.json` | ac_story_generation |
| Episode Digests | `episode_digests.json` | ac_story_generation |
| Chapter Digests | `chapter_digests.json` | ac_story_generation |
| Series Registry | `series_registry.json` | ac_story_generation |
| Series Assignment | `series_assignment.json` | ac_story_generation |

## VLM 数据来源

本阶段所有输入来自 VLM 分析结果 (`WindowAnalysisResult`)，定义在 `autocut_core/schema/window.py`：

| 本阶段使用 | VLM 输出字段 |
|-----------|-------------|
| 事件卡 | `visual_events[]` — 视觉事件 + emotion/conflict |
| 角色识别 | `visual_events[].characters[]` + `dialogue_and_text[].speaker_or_source` |
| 叙事节拍 | `story_beats[]` — function/cause/effect/open_question |
| 时空结构 | `timeline_segments[]` — present/flashback/dream |
| 高光候选 | `candidates[]` — highlight/hook + strength |

## 高光对比 (series_registry post-processing)

`series_registry` 完成后自动执行高光对比：

1. 从 `shots` 表读取 VLM 高光 (source=vlm) 和 API 高光 (source=api)
2. 调用 `merge_vlm_api_highlights()` 做 IoU 匹配
3. 匹配成功 → source 更新为 `vlm+api`
4. API 未匹配 (VLM 漏识别) → 记录到 `highlight_skill_evolution` 表
5. 详见 `highlight-recognition.md` skill

## References

| Document | Description |
|----------|-------------|
| [../../autocut_core/schema/window.py](../../autocut_core/schema/window.py) | VLM 输出 Pydantic Schema |
| [../../docs/design/vlm-first-architecture.md](../../docs/design/vlm-first-architecture.md) | VLM-First 架构设计文档 |

## Version History

- **v2.0.0** (2026-08-12): VLM-First 架构 — 阶段从 6-11 调整为 5-9。移除 Series Bible（移入 story_agent）。输入统一为 VLM Pydantic Schema。
- **v1.0.0**: 原始版本