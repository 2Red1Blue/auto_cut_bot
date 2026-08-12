# Auto Cut Bot — Agent-Native VLM-First Architecture

> 更新: 2026-08-12 | 版本: 2.1

## 核心原则

```
Agent 做决策 → Stage 做执行 → StateGraph 做基础设施
```

- **没有 pipeline 编排器**：Agent 直接调用 31 个 stage tools，自主编排流程
- **VLM 是主要信息源**：直接从视频提取对白、角色、场景、节拍、高光
- **API/剧本是辅助**：只注入 VLM 单窗口看不到的全局上下文
- **按需补充**：confidence_check 触发时才启用 ASR/角色参考

## 完整 Pipeline (25 stages, 3 phases)

```
Phase 1: Source Preparation (source_agent: 10 stages)
  1. source_windows      — 视频切片 + 480p CRF32 压缩 + PySceneDetect 全片检测
  2. global_context      — API(优先) → 剧本(降级) → 空(兜底)
  3. vlm_analysis        — VLM 逐窗语义分析
     ├─ 注入 global_context (synopsis/themes/relationships)
     ├─ PySceneDetect 边界修正
     └─ 高光精确标注 → shots 表
  4. confidence_check    — 质量门控，6 个 trigger 按需触发补充
  5. event_cards         — 从 VLM visual_events 跨窗口聚合
  6. episode_digests     — 单集摘要
  7. chapter_digests     — 章节摘要
  8. series_registry     — 全剧注册表 + VLM vs API 高光 IoU 对比
  9. series_assignment   — 章节分配

Phase 2: Story Generation (story_agent: 13 stages)
  10. series_bible       — 全剧圣经
  11. story_catalog      — 故事目录
  12. story_portfolio    — 故事组合
  13. story_treatments   — 故事大纲
  14. story_scripts      — 故事脚本
  15. story_preflight    — 素材可行性预检
  16. story_approval     — 人工审批 [HITL]
  17. story_evidence     — 证据收集
  18. span_candidates    — 候选片段
  19. story_plans_preflight — 计划预检 [HITL]
  20. story_plans        — 剪辑计划
  21. story_plans_materialize — 计划物化
  22. story_plans_qc_admission — QC 准入 [HITL]

Phase 3: Production (production_agent: 3 stages)
  23. story_qc           — 质量检测
  24. story_qc_review    — QC 审核 [HITL]
  25. story_render       — 渲染输出
```

## 数据流

```
API 响应
  ├─ synopsis/themes/relationships → global_context 表 → VLM 注入
  ├─ shots (is_highlight) → shots 表 (source=api)
  └─ characters → subjects 表 (cold start)

VLM 分析
  ├─ visual_events → shots 表 (source=vlm)
  ├─ dialogue_and_text → subtitles 表 (source=vlm)
  ├─ candidates (highlight) → shots 表 (source=vlm, 经 PySceneDetect 标注)
  └─ character_appearances → subjects 表 (source=vlm)

PySceneDetect
  └─ scene_boundaries.json → scene_boundary_fusion → 修正所有时间范围

系列注册
  └─ merge_vlm_api_highlights() → IoU 匹配
     ├─ matched → shots.source = 'vlm+api'
     └─ api_only → highlight_skill_evolution 表

Agent 全局排序
  ├─ 读取 shots 表全部高光
  ├─ 按 visual_impact(35%) + emotional(30%) + narrative(20%) + dialogue(15%)
  └─ 写回 global_rank, rank_score, rank_criteria
```

## 注入策略

| 策略 | 内容 | 来源 |
|------|------|------|
| 始终注入 | synopsis, themes, character_relationships | API → 剧本 → 空 |
| 绝不注入 | traits, dialogue, scene descriptions | design doc §4.2 |
| 按需注入 | ASR, character reference | confidence_check 触发 |
| Skill 注入 | highlight-recognition skill | vlm_analysis._load_highlight_skill() |

## 高光进化系统

```
series_registry → IoU 对比 → api_only → 积累证据
  ↓
analyze_missed_highlight(llm_backend, current_skill)
  ├─ LLM 路径: llm_analyze_missed_highlight()
  └─ 回退: 4 个硬编码 checker
  ↓
accumulated >= 5 → evolve_highlight_skill()
  ├─ contract eval → 写 SKILL.md + DB 版本
  └─ vlm_analysis._load_highlight_skill() → 下次 VLM 用新标准
```

## Agent 架构

```
主 Agent (可莉/Editor)
  ├─ 31 tools: 25 stage + 2 db + 3 domain + 1 ASR
  ├─ subagent: 琴/Reviewer (db_query read-only)
  ├─ SOUL.md: 身份 + 核心价值观
  ├─ AGENTS.md: 执行规则 + 高光排序
  └─ spec.py: 声明式工具注册

审核 Agent (琴/Reviewer)
  ├─ db_query (read-only)
  └─ 独立 session，不能修改数据
```

## 项目结构

```
auto_cut_bot/          ← Agent 框架 + 工具包装
  agent/tools/pipeline/ ← 31 stage tools (Agent 直接调用)
  agents/               ← AgentSpec, SOUL.md, AGENTS.md
  pipeline/plugins/     ← Stage 实现 (从 ac_auto_cut 同步)
  state_graph/          ← StateGraph 引擎 (HITL/resume 基础设施)

ac_auto_cut/           ← Pipeline 运行时
  autocut_core/         ← 核心库 (schema, db, semantic, libs)
  plugins/              ← Stage 实现 (原始)
  skills/               ← Agent 技能文件
  docs/design/          ← 设计文档
```

## 关键设计决策

| 决策 | 理由 |
|------|------|
| 删除 pipeline_orchestrator | broken + agent-native 不需要一键编排 |
| autocut_core → state_graph 重命名 | 解除影子包，恢复 126 个文件 import |
| 复用 shots 表存高光 | 不新建表，加 5 列即可 |
| API 高光不入库判断 | VLM 独立判断，API 只用于 skill 进化证据 |
| 剧本解析交给 Agent | Agent 用自己的 LLM，不调外部 API |
| script_parsed 优先 | 已有解析数据直接读，无数据才读文件 |