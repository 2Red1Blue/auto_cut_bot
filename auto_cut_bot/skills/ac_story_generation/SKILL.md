---
name: ac_story_generation
description: 故事生成 — Story Catalog 子弧发现、Story Portfolio 分槽、Treatment 讲法编译、Story Scripts 生成、素材可行性预检与人工审批。覆盖 Stage 12-17，产出已批准的故事脚本供下游证据检索和计划编排。Pipeline automation skill for auto cut bot.
metadata:
  auto_cut_bot:
    emoji: "✍️"
    always: false
version: 1.0.0
stages: [12, 13, 14, 15, 16, 17]
status: active
tools:
  - episode_digests
  - chapter_digests
  - series_registry
  - series_assignment
  - series_bible
  - story_catalog
  - story_portfolio
  - story_treatments
  - story_scripts
  - story_preflight
  - story_approval
data_layer_tools:
  - db_query.DBQueryTool  # schema discovery + raw SQL for any table
  - search_scenes
  - get_dialogue_samples
  - get_character_coverage
  - get_relation_timeline
  - get_emotion_peaks
  - check_fact
  - context_packer.pack_context
  - grounded_gen.validate_source_refs
  - grounded_gen.validate_temporal_constraints
  - grounded_gen.validate_voice_constraints
triggers:
  - "生成故事"
  - "Story Catalog"
  - "story_catalog"
  - "Story Portfolio"
  - "story_portfolio"
  - "Treatment"
  - "story_treatment"
  - "Story Scripts"
  - "story_scripts"
  - "素材预检"
  - "script_preflight"
  - "故事审批"
  - "story_approval"
  - "高光开场"
  - "cold_open"
anti_triggers:
  - "准备素材" → 使用 ac_source_prep
  - "剧集理解" → 使用 ac_series_knowledge
  - "生成计划" → 使用 ac_plan_orchestration
  - "渲染视频" → 使用 ac_render
---

# 故事生成 (ac_story_generation) — Stage 12-17

从 Series Bible 中发现故事子弧、分配生产槽位、编译讲法、生成故事脚本，经预检和人工审批后交付下游。

## Stages

| Stage | Name | Description | Status |
|-------|------|-------------|--------|
| 12 | `story_catalog` | Story Catalog（Broad Subarc Option Compiler） | active |
| 13 | `story_portfolio` | Story Portfolio（Primary/Reserve 分槽） | active |
| 14 | `story_treatment` | Treatment Options（3 种讲法编译） | active |
| 15 | `story_scripts` | Story Scripts（draft → awaiting_approval） | active |
| 16 | `script_preflight` | 素材可行性预检 | active |
| 17 | `story_approval` | 人工审批 | active |

## Input Artifacts

| Artifact | Source | Required |
|----------|--------|----------|
| Series Bible | ac_series_knowledge | yes |
| Event Cards | ac_series_knowledge | yes |
| Highlight/Hook Catalog | ac_series_knowledge | yes |
| Episode Digests | ac_series_knowledge | yes |
| Source Manifest | ac_source_prep | yes |

## Output Artifacts

| Artifact | Path Pattern | Consumer |
|----------|-------------|----------|
| Story Catalog | `story-catalog.json` | ac_story_generation, ac_plan_orchestration |
| Story Portfolio | `story-portfolio.json` | ac_story_generation, ac_plan_orchestration |
| Treatment Options | `story-treatment-options.json` | ac_story_generation, ac_plan_orchestration |
| Story Scripts | `story-scripts/index.json` | ac_plan_orchestration |
| Story Feasibility | `story-feasibility.json` | 审批 |
| Story Approval | `story-approval.json` | ac_plan_orchestration |

## References

| Document | Description |
|----------|-------------|
| [references/portfolio-design.md](references/portfolio-design.md) | Story Portfolio 分槽策略、时长合同、Reserve 补位 |
| [references/treatment-design.md](references/treatment-design.md) | 三种讲法编译：顺叙/冷开场不重放/冷开场延迟重放 |
| [references/script-schema.md](references/script-schema.md) | Story Catalog 与 Story Script 完整合同 |
| [references/approval.md](references/approval.md) | 人工审批流程与命令 |
| [references/highlight-opening-rubric.md](references/highlight-opening-rubric.md) | 高光开场筛选标准 |
| [references/opening-effective-frame-policy.md](references/opening-effective-frame-policy.md) | 开场有效帧策略 |
| [../../shared_contracts/references/editorial-knowledge/](../../shared_contracts/references/editorial-knowledge/) | 编导知识库（7 种类型模板 + 通用合同 + 开场策略） |

## Contract Rules

| Rule ID | Description | Engine Status |
|---------|-------------|---------------|
| rule_05 | Story Script 只描述要讲什么，不生成最终剪辑时间码；Treatment 先编译讲法 | landed |
| rule_06 | 每个 Story 必须有中心人物、中心冲突、必要背景、展开和局部 Payoff | landed |
| rule_07 | Plan 阶段不设时长下限，只保留硬上限 1200 秒 | landed |
| rule_09 | 未经人工批准的 Story 不得进入任何后续选片或渲染流程 | landed |
| rule_10 | 模型只生成 draft Editorial Blueprint；预检通过后才置为 awaiting_approval | landed |

## Tools

These are the pipeline stage tools managed by this Skill. The Agent should call them in dependency order:

| Tool | Stage | Depends On | Description |
|------|-------|-----------|-------------|
| `episode_digests` | 8 | `event_cards` | Per-episode summaries |
| `chapter_digests` | 9 | `episode_digests` | Chapter-level summaries |
| `series_registry` | 10 | `chapter_digests` | Character naming + relationship inference |
| `series_assignment` | 10 | `series_registry` | Story thread assignment |
| `series_bible` | 11 | `series_assignment` | Global normalized character bible |
| `story_catalog` | 12 | `series_bible` | Broad subarc option compiler |
| `story_portfolio` | 13 | `story_catalog` | Primary/Reserve slot assignment |
| `story_treatments` | 14 | `story_portfolio` | 3 treatment strategies per story |
| `story_scripts` | 15 | `story_treatments` | Generate story scripts |
| `story_preflight` | 16 | `story_scripts` | Material feasibility check |
| `story_approval` | 17 | `story_preflight` | Human approval gate (HITL) |

## Data Layer

When writing story beats, use these deterministic query tools to ground generation in facts:

### Query Tools (zero LLM cost)
- `search_scenes(characters, location, episode_range, min_intensity)` — Find scenes matching criteria. Use BEFORE writing beats to understand what material exists.
- `get_dialogue_samples(character, n=5)` — Get real dialogue samples. Use to maintain character voice consistency.
- `get_character_coverage(character)` — Get character's total screentime, scene count, episode distribution. Use to decide if a character has enough material for a lead role.
- `get_relation_timeline(char_a, char_b)` — Get every scene where both characters appear. Use to verify relationship arcs.
- `get_emotion_peaks(episode_range, top_k)` — Get high-intensity scenes. Use for hook/cold-open selection.
- `check_fact(claim)` — Verify a claim against source data. Use when unsure if a plot point exists in the source material.

### Context Assembly
- `context_packer.pack_context(task)` — Assemble context by priority (bible=0, episode_digests=1, catalog=2). Use before starting story generation.

### Validation Gates (must pass before approval)
- `grounded_gen.validate_source_refs(beat)` — Every beat must have source_refs pointing to real scenes with matching characters.
- `grounded_gen.validate_temporal_constraints(beat)` — Beat's relationship references must be valid at the story's time point (no continuity errors).
- `grounded_gen.validate_voice_constraints(dialogue)` — Rewritten dialogue must be similar to the character's real dialogue samples.

### Writing Discipline
1. **Retrieve first, write second**: Always query the data layer BEFORE writing beats.
2. **Every beat carries source_refs**: A beat without source_refs is a hallucination.
3. **Validate before approval**: Run all three validation gates. Fix violations before calling story_approval.
4. **Reserve activation**: If a primary story is rejected, activate the next reserve from the portfolio and re-run treatments → scripts → preflight → approval.

## Quick Start

```bash
# 生成 Story Catalog
python3 /absolute/skill/scripts/prepare_story_stages.py catalog \
  --job-root /absolute/job \
  --series-bible /absolute/job/series-bible.json \
  --event-cards /absolute/job/event-cards.jsonl \
  --candidate-catalog /absolute/job/highlight-hook-catalog.json

python3 /absolute/skill/scripts/run_semantic_batch.py \
  /absolute/job/story-catalog-batch.json \
  --backend qwen --workers auto --requests-per-minute 0 --fail-fast

python3 /absolute/skill/scripts/assemble_broad_story_catalog.py \
  /absolute/job/story-catalog-batch.json

# 建立 Story Portfolio
python3 /absolute/skill/scripts/build_story_portfolio.py \
  /absolute/job/story-catalog.json \
  --series-bible /absolute/job/series-bible.json \
  --output /absolute/job/story-portfolio.json \
  --project /absolute/job/project.json

# 编译 Treatment Options
python3 /absolute/skill/scripts/compile_story_treatments.py \
  /absolute/job \
  --story-catalog /absolute/job/story-catalog.json \
  --story-portfolio /absolute/job/story-portfolio.json \
  --series-bible /absolute/job/series-bible.json \
  --candidate-catalog /absolute/job/highlight-hook-catalog.json \
  --output /absolute/job/story-treatment-options.json

# 生成 Story Scripts
python3 /absolute/skill/scripts/prepare_story_stages.py scripts \
  --job-root /absolute/job \
  --story-catalog /absolute/job/story-catalog.json \
  --story-portfolio /absolute/job/story-portfolio.json \
  --story-treatment-options /absolute/job/story-treatment-options.json \
  --series-bible /absolute/job/series-bible.json \
  --event-cards /absolute/job/event-cards.jsonl \
  --candidate-catalog /absolute/job/highlight-hook-catalog.json

# 素材可行性预检
python3 /absolute/skill/scripts/preflight_story_scripts.py \
  /absolute/job/story-scripts/index.json \
  --series-bible /absolute/job/series-bible.json \
  --story-portfolio /absolute/job/story-portfolio.json \
  --story-catalog /absolute/job/story-catalog.json \
  --story-treatment-options /absolute/job/story-treatment-options.json \
  --event-cards /absolute/job/event-cards.jsonl \
  --candidate-catalog /absolute/job/highlight-hook-catalog.json \
  --source-manifest /absolute/job/source_manifest.json \
  --review-markdown /absolute/job/story-review.md \
  --project /absolute/job/project.json

# 初始化审批
python3 /absolute/skill/scripts/story_approval.py init \
  /absolute/job/story-scripts/index.json \
  --output /absolute/job/story-approval.json \
  --project /absolute/job/project.json
```

## Recovery

- Treatment 语义重试：每个 Option 只尝试一次，已失败 Option 不重复；全部耗尽后停止。
- Primary 被正式 rejection 后才触发 Reserve 补位，每个 Reserve 最多尝试一次。
- 网络/限流/传输故障不得装配成 Story rejection，必须停止当前 stage 等待恢复。

## Version History

## Agent-Native Execution

使用 db_query 自主查询数据库，不在 Pipeline Stage 硬编码顺序中执行。

1. db_query(operation="schema") → 发现可用数据
2. db_query(operation="raw", sql="...") → 按需查询
3. 在上下文中处理数据（LLM 推理或编译）
4. database_write → 写回 DB
5. 上下文已有数据 → 不重复查询
