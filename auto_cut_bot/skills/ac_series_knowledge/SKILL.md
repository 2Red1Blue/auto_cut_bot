---
name: ac_series_knowledge
description: 剧集知识构建 — 逐窗视频理解、Event Cards 编译、Episode/Chapter Digests 生成、Series Registry 与 Series Bible 装配。覆盖 Stage 6-11，产出全剧人物关系图、故事线归账和正式 Series Bible。Pipeline automation skill for auto cut bot.
metadata:
  auto_cut_bot:
    emoji: "📚"
    always: false
version: 1.0.0
stages: [6, 7, 8, 9, 10, 11]
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
  - "Series Bible"
  - "series_bible"
  - "人物关系"
  - "故事线归账"
anti_triggers:
  - "准备素材" → 使用 ac_source_prep
  - "生成故事" → 使用 ac_story_generation
  - "渲染视频" → 使用 ac_render
---

# 剧集知识 (ac_series_knowledge) — Stage 6-11

从窗口摘要中提取全剧事件、人物、关系、故事线，汇编为正式的 Series Bible，供下游故事生成和计划编排使用。

## Stages

| Stage | Name | Description | Status |
|-------|------|-------------|--------|
| 6 | `window_summaries` | 逐窗视频理解（VLM 多模态） | active |
| 7 | `event_cards` | 编译 Event Cards 与候选目录 | active |
| 8 | `episode_digests` | 生成 Episode Digests | active |
| 9 | `chapter_digests` | 生成 Chapter Digests | active |
| 10 | `series_registry` | Series Registry（Thread/Character/Relationship） | active |
| 11 | `series_bible` | Series Bible 装配 | active |

## Input Artifacts

| Artifact | Source | Required |
|----------|--------|----------|
| Window Summaries | ac_source_prep | yes |
| Window Manifest | ac_source_prep | yes |
| Source Manifest | ac_source_prep | yes |

## Output Artifacts

| Artifact | Path Pattern | Consumer |
|----------|-------------|----------|
| Event Cards | `event-cards.jsonl` | ac_story_generation |
| Highlight/Hook Catalog | `highlight-hook-catalog.json` | ac_story_generation |
| Episode Digests | `episode-digests.jsonl` | ac_story_generation |
| Chapter Digests | `chapter-digests.jsonl` | ac_story_generation |
| Series Registry | `series-registry.json` | ac_story_generation |
| Series Registry Admission | `series-registry-admission.json` | ac_story_generation |
| Series Registry Quarantine | `series-registry-quarantine.json` | 审计 |
| Series Assignment | `series-assignment.json` | ac_story_generation |
| Series Bible | `series-bible.json` | 全链路 |

## References

| Document | Description |
|----------|-------------|
| [references/bible-schema.md](references/bible-schema.md) | Series Bible v1.4 合同：Registry v9 分级别准入门禁、Assignment 归账与依赖门禁 |
| [docs/degradation-strategy.md](../../docs/degradation-strategy.md) | 降级框架 |
| [docs/timeline-anchoring.md](../../docs/timeline-anchoring.md) | 时间锚定系统 |

## Contract Rules

| Rule ID | Description | Engine Status |
|---------|-------------|---------------|
| rule_03 | Story Thread 归账：excluded_episodes 是整集级排除，不是未使用 Event 的收纳区 | landed |
| rule_04 | 所有事实/人物/关系/Story Thread 必须引用真实 Event/Fact ID | landed |
| rule_12 | Story Catalog 必须发现所有真实、有证据且具独立观看价值的子故事 | landed |

## Quick Start

```bash
# 编译 Event Cards
python3 /absolute/skill/scripts/compile_event_cards.py \
  /absolute/job/window-summaries.jsonl \
  --output-dir /absolute/job \
  --project /absolute/job/project.json

# 生成 Episode Digests
python3 /absolute/skill/scripts/prepare_story_stages.py episodes \
  --job-root /absolute/job \
  --source-manifest /absolute/job/source_manifest.json \
  --window-manifest /absolute/job/window_manifest.json \
  --window-summaries /absolute/job/window-summaries.jsonl \
  --event-cards /absolute/job/event-cards.jsonl \
  --candidate-catalog /absolute/job/highlight-hook-catalog.json

python3 /absolute/skill/scripts/run_semantic_batch.py \
  /absolute/job/episode-digest-batch.json \
  --backend qwen --workers auto --requests-per-minute 0

python3 /absolute/skill/scripts/assemble_story_artifacts.py episodes \
  /absolute/job/episode-digest-batch.json \
  --output /absolute/job/episode-digests.jsonl

# 生成 Chapter Digests、Registry 与 Bible
python3 /absolute/skill/scripts/prepare_story_stages.py chapters \
  --job-root /absolute/job \
  --episode-digests /absolute/job/episode-digests.jsonl \
  --episodes-per-chapter 6

python3 /absolute/skill/scripts/prepare_story_stages.py registry \
  --job-root /absolute/job \
  --episode-digests /absolute/job/episode-digests.jsonl \
  --chapter-digests /absolute/job/chapter-digests.jsonl \
  --event-cards /absolute/job/event-cards.jsonl

python3 /absolute/skill/scripts/prepare_story_stages.py assignments \
  --job-root /absolute/job \
  --series-registry /absolute/job/series-registry.json \
  --episode-digests /absolute/job/episode-digests.jsonl \
  --chapter-digests /absolute/job/chapter-digests.jsonl \
  --event-cards /absolute/job/event-cards.jsonl

python3 /absolute/skill/scripts/assemble_series_bible.py \
  /absolute/job/series-assignment-batch.json \
  --series-registry /absolute/job/series-registry.json \
  --source-manifest /absolute/job/source_manifest.json \
  --window-manifest /absolute/job/window_manifest.json \
  --episode-digests /absolute/job/episode-digests.jsonl \
  --event-cards /absolute/job/event-cards.jsonl \
  --output /absolute/job/series-bible.json
```

## Recovery

- Registry 缓存门禁：cache stage 为 `story-first-series-registry-v9-typed-coda-v1`，旧 cache signature 不得跨该门禁复用。
- 上游 Window Summaries 或 Event Cards 变更后，Registry 和 Bible 必须重新生成。
- Partial Admission 将无法闭合的人物/事件写入 quarantine，只发布 admitted 核心图；下游不得引用 quarantined ID。

## Multi-Source Conflict Resolution

Series Registry 在将人物数据写入 DB 后，自动运行多源冲突解决流程：

### 数据源

| Source | ID | Description |
|--------|----|-------------|
| LLM    | `llm` | Series Registry 从剧集摘要中提取的人物属性 |
| API    | `api` | 平台内容 API 提供的目录数据（人物名、角色、简介等） |

### 合并策略 (auto_policy)

| 字段类型 | 字段 | 策略 | 说明 |
|----------|------|------|------|
| 标量字段 | persona, traits, tone, voice_timbre, visual_features, relationship, role | LLM 优先 | LLM 值为空时回退到 API |
| 并集字段 | personality, aliases | 取并集 | 两个来源的值合并去重 |
| 结构字段 | first_episode, last_episode | LLM 优先 | 记录首次/最后出场集数 |

### 数据流

```
series_registry subjects
        │
        ├─── merge_operator(llm_data, api_data)
        │       │
        │       ├─── canonical values ──→ UPDATE subjects
        │       ├─── provenance records ──→ INSERT INTO source_provenance
        │       └─── conflict records ──→ UPSERT source_conflicts
        │
        ▼
  DB contains canonical row + full provenance + pending conflicts
```

### Provenance (source_provenance)

每条记录追踪一个字段的每个来源提供的值：

| Column | Description |
|--------|-------------|
| entity_table | 目标表名（如 `subjects`） |
| entity_id | 实体标识（人物名） |
| field_path | 字段名（如 `persona`） |
| values | JSONB: `{"llm": "...", "api": "..."}` |
| canonical_source | 被选为 canonical 的来源 (`llm` / `api` / `union`) |
| resolved_by | 策略名称（`auto_policy`） |

### Conflicts (source_conflicts)

当两个来源对同一字段提供不同的非空值时，生成冲突记录：

| Column | Description |
|--------|-------------|
| candidates | JSONB: `{"llm": "...", "api": "..."}` |
| severity | `low` / `medium` / `high` |
| status | `pending` / `resolved` |
| resolution | JSONB: 人工或自动裁决结果 |

### 查询待处理冲突

```sql
-- 按 book 查询所有待处理冲突
SELECT sc.*
FROM autocut.source_conflicts sc
JOIN autocut.subjects s ON s.name = sc.entity_id
WHERE s.book_id = '42000023011'
  AND sc.status = 'pending'
ORDER BY
  CASE sc.severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
  sc.created_at;
```

### 解决冲突

```python
db.resolve_conflict(conflict_id=42, resolution={
    "chosen_value": "...",
    "chosen_source": "llm",
    "reason": "LLM extraction is more detailed than API catalogue",
    "resolved_by": "manual_review",
})
```

## Version History

