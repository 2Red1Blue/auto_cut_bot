# Auto Cut Bot — Agent-Native V2 Architecture

## Execution Framework: StateGraph

```
┌──────────────────────────────────────────────────────────────────┐
│                    StateGraph (执行框架)                          │
│                                                                  │
│  source_ready → bible_ready → script_approved → rendered         │
│       │              │              │                │           │
│       ▼              ▼              ▼                ▼           │
│  [主 Agent]     [主 Agent]     [审核 gate]      [主 Agent]       │
│  素材准备       故事生成        review_agent     渲染输出         │
│                                                                  │
│  Checkpoint 持久化 — 失败恢复，不丢数据                           │
│  HITL gates — 审核不通过 → 退回主 Agent 修改                      │
│  Resume(session_id) — 断点续跑                                   │
│  可观测性 — 每个 node 的耗时和状态                                 │
└──────────────────────────────────────────────────────────────────┘
```

## Agent 架构: 1 主 + 1 审核 + 人

```
┌──────────────────────────────┐
│       主 Agent (剪辑)         │
│                              │
│ 自己做全部 23 个 stage         │
│ 上下文累积，不 spawn 子 Agent  │
│                              │
│ tools:                       │
│  db_query (自主 SQL)         │
│  database_write              │
│  source_script_load/save     │
│  ffmpeg_video_editor         │
│                              │
│ skills:                      │
│  ac_source_prep              │
│  ac_series_knowledge         │
│  ac_story_generation         │
│  ac_plan_orchestration       │
│  ac_qc                       │
│  ac_render                   │
│  ac_shared_contracts         │
│                              │
│ 调用外部:                     │
│  source_windows (ffmpeg)     │
│  window_analysis (VLM)       │
│  asr_transcript (FunASR)     │
└──────────┬───────────────────┘
           │
           │ story_plans → DB
           ▼
┌──────────────────────────────┐
│     审核 Agent (独立)         │
│                              │
│ 独立上下文，独立视角           │
│ 只读 DB，规则检查             │
│ 不重新运行 VLM/ASR            │
│                              │
│ tools:                       │
│  db_query (只读)             │
│                              │
│ 审核清单:                     │
│  ✅ 结构完整性               │
│  ✅ 角色一致性               │
│  ✅ 素材覆盖                 │
│  ✅ 切点边界                 │
│  ✅ 情绪曲线                 │
│  ✅ 三道约束                 │
│  ✅ Opening rubric           │
│                              │
│ 输出: approved / rejected    │
│ 退回: reasons → 主 Agent 修改 │
└──────────┬───────────────────┘
           │
           │ approved
           ▼
┌──────────────────────────────┐
│        人 (HITL)              │
│                              │
│ 审核 Agent 查不到的:          │
│  🎬 画面质量                 │
│  🔊 音频质量                 │
│  🎨 创意方向                 │
│  🎭 风格偏好                 │
│                              │
│ 决定: 通过 / 修改 / 拒绝      │
└──────────────────────────────┘
```

## Data Bridge (db_query)

```
schema() → 发现 13 张表, 字段, 关系
raw(sql) → 自主 SELECT, 5层安全, 列式压缩
  5层: 黑名单 → LIMIT 2000 → 参数化 → 只读 → 5s超时
  压缩: ≤20行=行式, 20-500=列式(省70%), 500+=分页提示
```

## Database (13 tables, source of truth)

```
books  episodes  scenes  subjects  subtitles  shots
boundaries  relationships  speaker_mappings  subject_episodes
source_conflicts  source_provenance  schema_migrations
```

## Pipeline (3 compute-heavy stages, retained)

| Stage | 做什么 | 为什么保留 |
|-------|--------|-----------|
| source_windows | ffmpeg 切片 + PySceneDetect + 闪白过滤 | I/O 密集，确定性 |
| window_analysis | VLM 100+ 并发多模态分析 | 计算密集，必须并发 |
| asr_transcript | FunASR 音频处理 | 计算密集，音频专用 |

## Skills (8 pipeline skills, all with db_query)

| Skill | Stages | 职责 |
|-------|--------|------|
| ac_source_prep | 1-5 | 素材准备 |
| ac_series_knowledge | 6-11 | 剧集知识 |
| ac_story_generation | 12-17 | 故事生成 |
| ac_plan_orchestration | 18-22 | 计划编排 |
| ac_review | review gate | 独立审核 ← 新增 |
| ac_qc | 23-25 | 质量检查 |
| ac_render | 26 | 渲染输出 |
| ac_shared_contracts | — | 共享合同 |

## Infrastructure

| Component | Purpose |
|-----------|---------|
| ArtifactCache | SHA256 content-addressing + prompt_version + truncation + TTL |
| StateGraphEngine | State flow + checkpoint + HITL + resume |
| db_query | 5-layer safe SQL + schema discovery + columnar output |
| ConflictResolver | Multi-source merge + strategy matrix + HITL gate |
| ASRValidator | Empty/all-zero/single-segment + density + PySceneDetect cross-check |
| FilmabilityGate | Pre-flight coverage + CutBoundaryAnchoring (PySceneDetect first) |
| WritingContract | 3 constraints: source_ref + duration + character_continuity |
| PySceneDetect | Frame-accurate shot boundaries + flash/motion filter |

## Key Decisions

1. **1 主 Agent + 1 审核 Agent + 人** — 不需要子 Agent，上下文累积
2. **StateGraph 是执行框架，Agent 是执行者** — 不冲突，互补
3. **Pipeline 保留 3 个 compute-heavy stage** — ffmpeg/VLM/ASR，Agent 不适合
4. **DB 是唯一数据源** — 删除冗余 JSON 文件，审核 Agent 只读 DB
5. **PySceneDetect 是场景边界主来源** — 确定性，帧级精度，ASR 只做内容理解
6. **审核 Agent 不重新跑 VLM/ASR** — 只查 DB + 规则检查，省成本
7. **planner-memory 不单独实现** — Agent 推理 = Planner, Checkpoint = Memory, Skills = Playbook
8. **db_query(raw) 自主 SQL** — Agent 像 MCP 一样灵活查询，5 层安全保护

## Git & Podman

| 组件 | 地址 |
|------|------|
| DB | postgresql://ac_user:ac_pass_2026@localhost:5433/autocut |
| Gateway | http://127.0.0.1:8767/health |
| WebUI | http://127.0.0.1:8768/ |
| Tests | 1578 passed, 2 skipped |