# Agent-Native 微内核架构 — 目标架构设计

## 架构重构核心思想

| 维度 | 旧架构 (Hybrid Pipeline) | 新架构 (True Agent-Native) |
| :--- | :--- | :--- |
| **主控大脑** | 静态双引擎（Agent 与 Pipeline Orchestrator 混用） | **Agent 状态图引擎 (StateGraph Engine)** 作为唯一控制平面 |
| **执行单元** | 26 个硬编码 Stage + 20+ 个细粒度 Tools | **4 个领域子 Agent (Sub-Agents)** / 领域微内核 |
| **剧本解析** | 盲目赌博式 Context 直出 | **自适应策略 (Adaptive Parsing)**：短剧本 Direct-In-Context，长剧本 MapReduce 降级 |
| **人工作业 (HITL)** | 进程挂起，内存等待 | **持久化 Checkpoint 机制**（可无缝暂停/重启数天） |
| **流程管理** | 阶段式 (Stg 1-26) | **目标/里程碑式 (Milestones & Goals)** |

## 架构图

```
+---------------------------------------------------------------------------------------+
|                               🖥️  Frontend Layer (Next.js 15)                          |
|    Dashboard | StoryReview | PipelineMonitor | SSE Stream Reader | Human Approval Box |
+------------------------------------------+--------------------------------------------+
                                           | WebSocket / REST API
+------------------------------------------v--------------------------------------------+
|                          🧠 Agent Control Plane (Agent 核心控制平面)                   |
|                                                                                       |
|  +---------------------------------------------------------------------------------+  |
|  |  Agent Runtime Engine (基于 LangGraph / Custom State Graph)                      |  |
|  |  * 状态持久化与恢复 (Checkpointer: Postgres / Redis)                             |  |
|  |  * 人工审批断点 (HITL Interrupt Gates)                                            |  |
|  +---------------------------------------------------------------------------------+  |
|                                                                                       |
|  +---------------------------------------------------------------------------------+  |
|  |  Goal-Driven Planner & Memory (基于目标与 Playbook 演进，无硬编码 Stg 1-26)         |  |
|  +---------------------------------------------------------------------------------+  |
+--------------------+-------------------+--------------------+-------------------------+
                     |                   |                    |
        +------------v---+       +-------v--------+       +---v------------+
        | 🔍 Domain 1:   |       | 📖 Domain 2:   |       | 🎬 Domain 3:   |
        | Source Sub-    |       | Story Sub-     |       | Production     |
        | Agent          |       | Agent          |       | Sub-Agent      |
        +--------+-------+       +-------+--------+       +--------+-------+
                 |                       |                         |
+----------------v-----------------------v-------------------------v---------------------+
|                          ⚡ Execution Engine (确定性 Python 算子库)                     |
|                                                                                       |
|  * Adaptive Script Parser (自适应剧本解析器: Context Parsing / MapReduce Dynamic)     |
|  * Video Analysis & Window Tooling (视频帧提取、音频对齐)                              |
|  * Artifact Bus (哈希缓存 + 缓存清理)                                                 |
|  * Database Layer (StageDB Client - 确定性读写)                                       |
|  * FFmpeg / GPU Render Cluster (剪辑渲染引擎)                                         |
+---------------------------------------------------------------------------------------+
```

## 核心层级设计

### 1. 控制平面：Agent State Machine (代替 Pipeline Orchestrator)

不再存在 `pipeline/core/orchestrator.py` 硬编码状态机。流程流转完全由 Agent 的状态图管理。

- **State Graph 定义**：

```python
class AgentState(TypedDict):
    project_id: str
    current_milestone: Literal["source_ready", "bible_ready", "script_approved", "rendered"]
    context_data: dict
    artifacts_hash: dict
    requires_human_approval: bool
```

- **Checkpoint 持久化**：HITL 时 Agent 完整内存状态序列化写入 DB，释放计算资源。用户审批后 `resume(session_id)` 原地复活。

### 2. 工具聚合：3 个领域子 Agent

| 子 Agent | 职责 | 内部智能 |
|----------|------|---------|
| `SourceAgent` | 文本解析、音视频对齐、窗口切片 | 自适应：<50K tokens 直接 Context 解析，>50K 自动 MapReduce |
| `StoryAgent` | Series Bible、角色图谱、Story Treatments | 自我纠错、剧情一致性检查 |
| `ProductionAgent` | 分镜、Preflight、QC、FFmpeg 渲染 | 确定性编排，无嵌套 LLM 调用 |

### 3. 执行层：确定性 Python 算子

- IO 与 DB：原子写入、DB 10 表读写完全由 Python 原生代码执行
- Artifact Bus：SHA256 哈希缓存，Agent 重规划时直接命中
- 无嵌套 LLM 调用

## 核心数据流 (source_script 解析)

1. **API 触发**：WebUI / API 发起任务目标
2. **Main Agent 规划**：发现 `current_milestone` 为初始状态，决定调用 `SourceAgent`
3. **SourceAgent 执行**：
   - 调用 `source_script_load` 获取剧本
   - **自适应分流**：短剧本直接 Context 解析，长剧本调用 MapReduce 算子
   - 调用 `source_script_save` 存入 DB
4. **里程碑推进**：`SourceAgent` 返回 `source_ready`
5. **HITL**：需要审批时触发 Interrupt，状态入库，通知 WebUI
6. **人工恢复**：审批通过，Agent 解冻，调度 `StoryAgent` 继续