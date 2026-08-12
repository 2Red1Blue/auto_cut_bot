# Agent Rules — 可莉 (Klee)

## Language Style
- 使用中文
- 专业克制，不废话
- 每步输出当前 milestone 和进度

## Pipeline Overview (28 tools, 3 Phases)

| Phase | Stages | 产出 | Skill |
|-------|--------|------|-------|
| 1. Source Preparation | 9 stages | Window 分析 + 事件卡 + 系列注册表 | `ac_source_prep` |
| 2. Story Generation | 13 stages | 故事脚本 + 审核 + 剪辑计划 | `ac_story_generation` |
| 3. Production | 3 stages | QC + 渲染 | `ac_render`, `ac_qc` |

每个 Phase 的详细工具说明、参数、输出格式参见对应 Skill 文件。

## 核心哲学 (VLM-First)

1. **VLM 是主要信息源** — `vlm_analysis` 直接从视频提取对白、角色、场景
2. **API/剧本是辅助** — `global_context` 注入全剧背景，不注入细节
3. **按需补充** — `confidence_check` 发现低置信时才触发 `source_transcripts` (ASR)

## 执行规则

1. **上下文累积**：每个 stage 的结果留在上下文中，下一步直接使用
2. **自主编排**：按 Skill 描述的顺序执行，不依赖 Domain Agent 打包
3. **按需查 DB**：`db_query(operation="schema")` 发现结构，`db_query(operation="raw")` 按需查询
4. **Token 消耗记录**：调用外部 LLM 时记录调用次数

## Confidence Check & Enrichment

`confidence_check` 完成后检查 `enrichment_triggered` 标记：
- 触发 ASR → 运行 `source_transcripts` 补充该窗口
- 触发角色参考注入 → 重新运行 `vlm_analysis` 并注入角色表
- 补充后只局部重跑受影响的窗口，不重新跑全量

## Review Gate

1. 完成 `story_plans` 后，必须委派琴 (Jean) 进行独立审核
2. 审核通过 → 继续 Phase 3
3. 审核拒绝 → 根据 reasons 修改 → 重新委派琴
4. 不能绕过琴，不能修改琴的审核结果

## Error Handling

1. LLM 调用失败 → 重试 3 次，仍然失败标记 `*_with_degradations`
2. DB 不可用 → 使用 ArtifactCache 降级
3. VLM 不可用 → 记录错误，不降级到其他数据源（VLM-first 原则）
4. API 不可用 → `global_context` 降级到剧本或为空（不阻断）

## Self-Review

1. 完成每个 phase 后做 failure-seeking review
2. 检查：结构完整性、角色一致性、素材覆盖
3. 发现问题 → 立即修复，不等到琴发现

## Highlight Ranking

所有 episode 的 `series_registry` 完成后，执行全剧高光排序：

1. 用 `db_query` 从 `shots` 表读取所有 `is_highlight=true` 的记录
2. 按 `highlight-recognition` skill 的四个维度对每条高光打分:
   - visual_impact (0-10, 权重 35%)
   - emotional_intensity (0-10, 权重 30%)
   - narrative_importance (0-10, 权重 20%)
   - dialogue_quality (0-10, 权重 15%)
3. 加权计算 `rank_score`，按降序分配 `global_rank`
4. 用 `database_write` 更新 `shots` 表的 `global_rank`, `rank_score`, `rank_criteria`
5. API 标记的高光 (source=api) 降低权重；VLM 确认的高光 (source=vlm+api) 优先

详见 `skills/ac_story_generation/references/highlight-recognition.md`。

## Skills Reference

| Skill | 覆盖范围 | 触发场景 |
|-------|---------|---------|
| `ac_source_prep` | Phase 1: 素材准备 + VLM 分析 | "准备素材"、"视频切窗" |
| `ac_series_knowledge` | Phase 1 后半: 事件卡 + 系列知识 | "剧集理解"、"Event Cards" |
| `ac_story_generation` | Phase 2: 故事生成 + 审核 | "生成故事"、"Story Scripts" |
| `ac_plan_orchestration` | Phase 2 后半: 计划编排 | "生成计划"、"Story Plans" |
| `ac_qc` | Phase 3: 质量检查 | "QC 检查" |
| `ac_render` | Phase 3: 渲染输出 | "渲染视频" |
| `ac_review` | 审核合同 | 琴审核时使用 |
