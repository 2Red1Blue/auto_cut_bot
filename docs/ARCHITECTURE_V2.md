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

## Phase 1: Source Preparation (VLM-First Pipeline)

```
┌─────────────────────────────────────────────────────────────────┐
│  Stage 1: source_windows                                        │
│  ffmpeg 切片 + PySceneDetect 全片检测 + 闪白过滤                │
│  输出: window_batch.json (480p CRF32 窗口列表)                  │
│  写入: boundaries 表 (PySceneDetect 精确帧边界)                 │
├─────────────────────────────────────────────────────────────────┤
│  Stage 2: global_context                                        │
│  API(优先) → 剧本(降级) → 空(兜底)                              │
│  提取: synopsis, themes, relationships                           │
│  写入: global_context 表, books 表, episodes 表                 │
│  冷启动: subjects 表 (source='api', 角色名列表)                 │
├─────────────────────────────────────────────────────────────────┤
│  Stage 3: vlm_analysis                                          │
│  VLM 逐窗分析 + 注入 global_context                             │
│  输入: window_batch + global_context                            │
│  注入策略:                                                       │
│    始终注入: synopsis, themes, character_relationships           │
│    绝不注入: traits, dialogue, scene descriptions                │
│    按需注入: confidence_check 触发 → ASR / character_reference  │
│  输出: window_summaries.jsonl                                   │
│  写入:                                                           │
│    shots (source='vlm')                                          │
│    subtitles (source='vlm')                                      │
│    subjects (source='vlm', vlm_verified=True)                    │
│    scenes (source='vlm')                                         │
│    candidates (source='vlm', type='highlight'/'hook')            │
├─────────────────────────────────────────────────────────────────┤
│  Stage 4: confidence_check                                      │
│  VLM 输出质量门控 + 6 个 Agent 动态决策 trigger                 │
│  检查:                                                           │
│    对白置信度统计 (high/medium/low)                              │
│    硬字幕检测 (source_accuracy.agreement)                        │
│    边界连续性检查 (相邻窗口)                                     │
│    角色命名一致性检查                                            │
│  触发条件:                                                       │
│    无硬字幕 → 触发 ASR                                           │
│    低置信对白比例 > 20% → 触发 ASR                              │
│    边界不连续 → 触发重跑                                         │
│    角色命名不一致 → 触发 character_reference                     │
│  输出: confidence_report.json                                   │
│  写入: vlm_confidence_log 表                                    │
├─────────────────────────────────────────────────────────────────┤
│  Stage 5-10: series_knowledge                                   │
│  event_cards → episode_digests → chapter_digests →              │
│  series_registry → series_assignment                            │
│  跨窗口聚合 → 单集摘要 → 章节摘要 → 全剧注册表 → 章节分配     │
│  输出: series_bible.json                                        │
│  写入: subject_episodes 表                                      │
└─────────────────────────────────────────────────────────────────┘
```

## Database (13 tables, VLM-First 数据流)

### 核心原则

**最终数据都是 VLM 经 PySceneDetect 修正后的。API 数据只用于 cold start 和 skill 进化。**

### 表级决策

| 表 | 旧用途 | VLM-First 后 | 谁写 |
|---|--------|-------------|------|
| global_context | — | synopsis, themes, relationships | global_context stage |
| subjects | API 角色 | cold start: API 角色名 (source=api) + VLM 聚合 (source=vlm) | global_context + vlm_analysis |
| books | 书元数据 | 书名/集数/类型 (不变) | global_context |
| episodes | 集列表 | FK 约束 (不变) | global_context |
| shots | API 分镜 | VLM visual_events (source=vlm) | vlm_analysis |
| subtitles | API 字幕 | VLM dialogue_and_text (source=vlm) | vlm_analysis |
| scenes | 剧本场景 | VLM 场景变化 (source=vlm) | vlm_analysis |
| boundaries | 镜头边界 | PySceneDetect 精确帧边界 | source_windows |
| speaker_mappings | ASR→角色 | **废弃** (VLM 直接识别说话人) | 不再写入 |
| highlight_skill_evolution | — | API 高光 VLM 漏识别的 | series_registry (对比后) |

### API 高光：只记录 VLM 漏识别的

```
API shots (is_highlight=True)
  ↓ PySceneDetect 修正时间范围
  ↓
VLM candidates (type="highlight")
  ↓ PySceneDetect 修正时间范围
  ↓
IoU 匹配 (intersection / union > 0.3)
  ├─ 匹配 → VLM 找到了 → 不记录
  └─ 不匹配 → VLM 漏了 → 记录到 highlight_skill_evolution
```

### 时间范围交叉匹配

```python
def compute_iou(a_start, a_end, b_start, b_end):
    """计算两个时间范围的交并比"""
    intersection = max(0, min(a_end, b_end) - max(a_start, b_start))
    union = max(a_end, b_end) - min(a_start, b_start)
    return intersection / union if union > 0 else 0

# 示例
api:    (10.0, 25.0)  →  PySceneDetect snap → (10.2, 24.8)
vlm:    (12.0, 28.0)  →  PySceneDetect snap → (12.2, 27.8)
intersection = min(24.8, 27.8) - max(10.2, 12.2) = 24.8 - 12.2 = 12.6
union        = max(24.8, 27.8) - min(10.2, 12.2) = 27.8 - 10.2 = 17.6
IoU = 12.6 / 17.6 = 0.716 → 匹配！
```

### highlight_skill_evolution 写入时机

```
vlm_analysis 完成 → 每个窗口有 VLM candidates
  ↓
series_registry 阶段:
  for each API highlight:
    for each VLM candidate in same episode:
      iou = compute_iou(...)
      if iou > 0.3: matched, skip
    if no match:
      db.record_highlight_evolution(
        skill_version="v1",
        window_id=window_id,
        api_highlight={start, end, reason, score},
        vlm_miss_reason=None,  # 由 Agent 分析
      )
```

### 不需要存的 API 数据

| API 数据 | 原因 |
|---------|------|
| shots (非高光) | VLM visual_events 替代，VLM 直接看画面更准确 |
| subtitles | VLM 读硬字幕更准确 |
| speaker_mappings | VLM 直接识别说话人 |
| 角色视觉特征 | 设计文档 §4.2 明确禁止注入 |

### API 数据的唯一价值 = VLM 的盲区补充

**1. 全局上下文注入 (global_context)**
- synopsis, themes, relationships
- VLM 单窗口看不到的跨窗口信息
- 始终注入，不依赖 VLM 是否识别

**2. Skill 进化证据 (highlight_skill_evolution)**
- API 高光 VLM 漏了
- 记录漏识别 case
- Agent 分析原因 → 更新 skill

**API 数据不需要存的:**
- VLM 已经识别到的 → 冗余, 不存
- VLM 能自己看的 → 不注入 (设计文档 §4.2)

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
9. **VLM-First 数据流** — 最终数据全部来自 VLM + PySceneDetect 修正，API 数据只用于 cold start 和 skill 进化
10. **API 数据零冗余存储** — VLM 已识别到的不存，VLM 能自己看的不注入（§4.2）
11. **Skill 进化闭环** — API 高光 VLM 漏识别 → highlight_skill_evolution 表 → Agent 分析原因 → 更新 skill
12. **scene_boundary 内联 vlm_analysis** — PySceneDetect 精确帧修正作为 vlm_analysis 的可选后处理步骤（step 2.5），不是独立 stage

## VLM Schema (Pydantic v2)

### 当前 schema (`pipeline/core/schema/window.py`)

```
WindowAnalysisResult
├── source_id, episode, window_id, window(start, end)
├── window_summary: str
├── timeline_segments: list[TimelineSegment]     # present/flashback/dream 模式
│   └── start, end, mode, entry_signal, exit_signal, summary
├── boundary_context: BoundaryContext             # 场景边界上下文
│   └── starts_mid_scene, ends_mid_scene, continues_from/into_next
├── story_beats: list[StoryBeat]                  # 剧情节拍
│   └── start, end, function, summary, characters, cause, effect, open_question
├── dialogue_and_text: list[DialogueEvent]        # 对白 + 屏幕文字
│   └── start, end, speaker_or_source, kind, text, confidence
│       └── source_accuracy: SourceAccuracy        # 多源仲裁 (ASR/API/剧本)
├── visual_events: list[VisualEvent]              # 视觉事件
│   └── start, end, description, characters, emotion, action, conflict, visual_impact
└── candidates: list[HighlightCandidate]          # 高光/钩子候选
    └── id, start, end, type, strength(1-10), reason, anchor, lead_in, payoff
```

### 对比测试文件 MAX_OUTPUT — 缺失的关键字段

**🔴 核心缺失**（短剧剪辑必须有）：

| 字段 | 用途 | 影响 |
|------|------|------|
| `character_appearances` | 每个角色的详细外观描述（谁在画面里） | 角色识别 |
| `scene_locations` | 每个场景的详细位置描述（在哪里） | 场景管理 |
| `on_screen_subtitles` | 画面硬字幕逐条提取（短剧的主要数据源） | 字幕提取 |

**🟡 重要缺失**（应该有）：

| 字段 | 用途 |
|------|------|
| `character_relationships` | 从画面推断角色间的关系 |
| `character_emotion_timeline` | 每个角色在不同时间段的情绪状态 |
| `editing_transitions` | 剪辑转场点 |

**🟢 可选缺失**（锦上添花）：

| 字段 | 用途 |
|------|------|
| `props_objects` | 道具物件 |
| `music_sound` | 背景音乐/音效 |
| `camera/lighting/composition` | 镜头细节 |
| `emotional_arc/tension_level/pacing` | 节拍增强属性 |

### 调用方式

```python
# 方式 1: json_object (宽松，VLM 自由输出)
response_format={"type": "json_object"}

# 方式 2: Pydantic 结构化输出 + 校验重试
response_format=VlmAnalysisResult  # Pydantic 自动转 json_schema → API 受限解码

# 方式 3: dict-based JSON Schema (strict mode, 当前 pipeline 使用)
WINDOW_ANALYSIS_SCHEMA = as_dict_schema()  # Pydantic → dict 兼容
```

## Agent 架构 (Z3r0 模式)

### AgentSpec 声明式定义

```
auto_cut_bot/agents/
├── __init__.py          ← 导出 AgentSpec, AgentBuilder, AgentRegistry
├── spec.py              ← AgentSpec + ToolMount 定义
├── registry.py          ← AgentRegistry (单例) + AgentBuilder (运行时组装)
├── editor/
│   ├── SOUL.md          ← "我是剪辑编排者，负责从素材到成片的全流程"
│   └── AGENTS.md        ← 行为规则：累积上下文、不spawn、调用审核
└── reviewer/
    ├── SOUL.md          ← "我是独立审核员，不信任主 Agent 的决策"
    └── AGENTS.md        ← 行为规则：只读DB、规则检查、approved/rejected
```

### 运行时组装 (AgentBuilder)

```
AgentBuilder.build("editor", has_pipeline_context=True)
  → 1. 读取 SOUL.md + AGENTS.md
  → 2. 按环境过滤工具 (ToolMount.requires_pipeline)
  → 3. 拼接 instructions (soul + rules + pipeline context + delegation)
  → 4. 如果有 subagents → 生成委派工具名称
  → 返回 AgentInstance(spec, instructions, tools, model)
```

### 执行路径

```
Editor (主 Agent)
  → 23 个 stage tools (上下文累积)
  → 完成后调用 Reviewer (spawn subagent, 独立 session)

Reviewer (审核 Agent)
  → AgentBuilder.build("reviewer", has_review_context=True)
  → 独立 LLM client (不复用父级 runtime)
  → 独立 session_key (f"{parent_session}:review")
  → 只读 DB (db_query)
  → 返回: ReviewVerdict(status, score, reasons)
```

### autocut_core 双包问题

```
auto_cut_bot/autocut_core/   → StateGraph 引擎 (agent/entities, engine, ports)  1,477 行
ac_auto_cut/autocut_core/    → Pipeline 运行时 (PipelineConfig, ArtifactBus, Stage)

Python import 总是找到本地影子包 → 所有 `from autocut_core import PipelineConfig` 全部 ImportError
```

**StateGraph 的定位**：
- 当前不需要（流程是线性的 + 一个条件分支）
- 保留 autocut_core/ 作为未来基础设施
- 等系统复杂度增加时再激活（多 Agent 并行、动态流程重组、可视化监控）
- 需要先解决影子包命名冲突

## Highlight Pipeline (高光识别 + 排序 + Skill 进化)

### 数据融合流程

```
VLM candidates (type=highlight, semantic time range)
  +
API highlights (is_highlight=True, reference marker)
  +
PySceneDetect boundaries (precise frame cuts)
  ↓
merge_vlm_api_highlights()  → IoU 匹配
  ├─ matched → source=vlm+api, keep VLM time
  └─ API-only → source=api (VLM missed)
  ↓
annotate_highlights_with_scene_boundaries()  → snap to PySceneDetect
  ↓
highlight_annotations 表 {precise_start, precise_end, source, ...}
```

### highlight_annotations 表

| 字段 | 类型 | 说明 |
|------|------|------|
| annotation_id | str | 唯一标识 |
| book_id | str | 所属剧 ID |
| episode_id | int | 所属集号 |
| window_id | str | 所属窗口 |
| precise_start | float | PySceneDetect 修正后起始时间(秒) |
| precise_end | float | PySceneDetect 修正后结束时间(秒) |
| source | str | vlm / api / vlm+api |
| strength | int | VLM 原始强度 (1-10) |
| reason | str | 高光原因 |
| anchor | str | 情绪峰值描述 |
| vlm_missed | bool | VLM 是否漏识别 (API-only 时为 true) |

### 全局排序流程

```
Agent 读取 highlight_annotations 表 (全部 episode)
  ↓
Agent 应用 highlight-recognition skill 四个维度:
  emotional_intensity × conflict_level × visual_impact × narrative_importance
  ↓
Agent 分配 global_rank (1-N) + rank_score (0-100)
  ↓
Agent 写回 DB: global_rank, rank_score, rank_criteria, rank_version
  ↓
Skill 进化 → 重新排序 → rank_version 递增
```

### IoU 匹配逻辑 (已有基础设施可复用)

```python
# event_cards/stage.py:115-117 已有 IoU 实现
def compute_iou(a_start, a_end, b_start, b_end):
    overlap = min(a_end, b_end) - max(a_start, b_start)
    union = max(a_end, b_end) - min(a_start, b_start)
    return overlap / union if overlap > 0 and union > 0 else 0.0

# IoU > 0.3 → matched (source=vlm+api)
# IoU ≤ 0.3 → API-only (source=api, vlm_missed=true)
```

### Snap to Scene Boundary

```python
def snap_to_scene_boundary(semantic_start, semantic_end, boundaries):
    """将 VLM 语义时间范围对齐到最近的 PySceneDetect 帧边界"""
    # 找最近的 scene_change 边界
    snap_start = min(boundaries, key=lambda b: abs(b.start_time - semantic_start))
    snap_end = min(boundaries, key=lambda b: abs(b.end_time - semantic_end))
    return snap_start.start_time, snap_end.end_time
```

### Skill 进化闭环

```
highlight_annotations (vlm_missed=true 的记录)
  ↓
Agent 分析漏识别原因:
  - VLM 没看到? (画面不明显)
  - VLM 看到了但没标记为 highlight? (理解偏差)
  - 时间范围太窄? (IoU 不够)
  ↓
Agent 更新 highlight-recognition skill:
  - 调整四维度权重
  - 增加新的识别模式
  - 修改阈值
  ↓
重新排序 → rank_version 递增
  ↓
对比新版本 vs 旧版本的排序差异
  ↓
确认改进 → 保留新 skill / 回滚
```

### 当前实现状态

| 组件 | 状态 | 位置 |
|------|------|------|
| HighlightCandidate Pydantic | ✅ 已实现 | `schema/window.py:125-135` |
| Shot.is_highlight 字段 | ✅ 已实现 | `db_entities.py:217-219` |
| Boundary.event_type (含 highlight) | ✅ 已实现 | `db_entities.py:274-275` |
| IoU 计算函数 | ✅ 已实现 | `event_cards/stage.py:115-117`, `_common.py:61` |
| highlight_annotations 表 | ❌ 需新建 | — |
| snap_to_scene_boundary() | ❌ 需新建 | — |
| merge_vlm_api_highlights() | ❌ 需新建 | — |
| highlight-recognition skill | ❌ 需新建 | 仅有 `highlight-opening-rubric.md` 参考 |
| global_rank / rank_score 字段 | ❌ 需新建 | `rank_score` 仅用于 story_portfolio |
| rank_version 机制 | ❌ 需新建 | — |

## Git & Podman

| 组件 | 地址 |
|------|------|
| DB | postgresql://ac_user:ac_pass_2026@localhost:5433/autocut |
| Gateway | http://127.0.0.1:8767/health |
| WebUI | http://127.0.0.1:8768/ |
| Tests | 1578 passed, 2 skipped |