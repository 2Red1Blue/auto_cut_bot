# 1若采用子agent

## 一、重新设计后的 cut_bot 如何利用 nanobot 原生上下文

### 当前的架构层次

```
用户消息 → nanobot AgentLoop（完整上下文体系）
              │
              ├── 主 Agent 看到：
              │   ├── System Prompt（SOUL.md + AGENTS.md + MEMORY.md + Skills...）
              │   ├── 完整对话历史
              │   ├── 工具调用/结果
              │   └── Runtime Context
              │
              └── 当主 Agent 调用 source_agent / story_agent / production_agent 工具时：
                    │
                    ▼
              DomainAgent._run_via_subagent()
                    │
                    ├── 1. AgentBuilder.build("editor") → 获取 system_prompt
                    ├── 2. task_prompt = _build_task_prompt()
                    │      └── skill 注入 + goal + job_root + rules
                    ├── 3. full_task = system_prompt + task_prompt
                    └── 4. SubagentManager.run_inline(
                          task=full_task,
                          session_key=f"{base}:{agent_name}",  ← 独立 session
                          ...
                        )
```

### 子代理实际收到的上下文

```
┌─────────────────────────────────────────────────────┐
│  子代理（source_agent）收到的输入                     │
├─────────────────────────────────────────────────────┤
│                                                      │
│  ① System Prompt（来自 AgentBuilder）                │
│     └── "editor" agent 的 instructions              │
│         （SOUL.md + AGENTS.md，不含主 Agent 的记忆）  │
│                                                      │
│  ② 一条 user 消息（task_prompt）                     │
│     ├── Skill 内容（ac_source_prep 的 SKILL.md）     │
│     ├── "Goal: Complete source material preparation" │
│     ├── "Job root: /path/to/job"                     │
│     ├── "IMPORTANT RULES: ..."                       │
│     └── "Backend: qwen / Mode: auto"                 │
│                                                      │
│  ③ 自己的对话历史（独立 session）                     │
│     └── 首轮为空，后续迭代累积工具调用/结果            │
│                                                      │
│  ❌ 没有主 Agent 的对话历史                           │
│  ❌ 没有 MEMORY.md 的长期记忆                        │
│  ❌ 没有 history.jsonl 的 Recent History             │
│  ❌ 没有 DB 状态快照（因为根本没有 DB）                │
│  ❌ 没有前一个子代理的执行结果                         │
│                                                      │
└─────────────────────────────────────────────────────┘
```

### StateGraph 的 state dict 传递了什么

```python
# SubAgentPlugin.execute()
ctx = DomainContext(
    job_root=state.get("job_root", ""),      # 路径
    config=state.get("config"),               # PipelineConfig
    bus=state.get("bus"),                     # ArtifactBus
    backend=state.get("backend", "qwen"),     # 后端名
    mode=state.get("mode", "auto"),           # 模式
)

result = await self._agent.execute(ctx)

# 结果写回 state
output["_last_result"] = result              # DomainResult 对象
output["_agent_name"] = self._agent.contract.agent_name
```

**关键发现：state dict 里传的全是"基础设施"对象（路径、配置、bus），没有任何"语义上下文"。**

子代理不知道：
- 前一个 Agent 做了什么决策、为什么
- 用户说了什么、期望什么
- 项目的整体进度和当前焦点
- 之前犯过什么错

它只知道"我的 job_root 在这里，Skill 文件告诉我要做什么，去做吧"。

---

## 二、从 Z3r0 借鉴：cut_bot 的上下文差距

### 差距 1：没有"黑板"——子代理之间的信息传递是断裂的

**Z3r0 的做法：** WorkProject DB 是共享黑板。CSO 委派 CIE 做情报收集 → CIE 把发现写入 DB → CSO 下一轮自动看到最新的 `graph`、`findings`、`evidence`。

**cut_bot 的现状：**

```python
# source_agent 完成后
output["_last_result"] = DomainResult(
    status=DomainStatus.SUCCESS,
    artifacts=[Artifact(name="event_cards", path="...")],
    milestone_reached="source_ready",
)

# story_agent 开始时
ctx = DomainContext(
    job_root=state.get("job_root", ""),    # 同一个路径
    # ← 它怎么知道 source_agent 产出了什么？
)
```

**子代理之间靠"文件系统"传递**（同一个 job_root 下的文件），而不是靠结构化的状态传递。story_agent 需要自己去扫描 job_root 下有什么文件，而不是被告知"source_agent 产出了这些 artifact"。

**类比：** 就像工厂里，上一个车间把零件放在传送带上，下一个车间自己去传送带上翻——没有交接单，没有质检报告。

### 差距 2：子代理的上下文太"冷"——没有项目语义

**Z3r0 的做法：** 每轮注入 `work_project_context`（结构化 JSON：当前聚焦的 WorkItem、目标、证据、资产图谱...），Agent 始终知道"我在做什么、做到哪了、还有什么要做"。

**cut_bot 的现状：** 子代理收到的 task_prompt 是：

```
[Skill 内容]

Goal: Complete source material preparation
Job root: /path/to/job

IMPORTANT RULES:
- Run stages in the order described in the Skill above.
- If any stage fails, report the error and do not continue.
- When all stages complete, report: milestone=source_ready.
Backend: qwen
Mode: auto
```

**它不知道：**
- 这个 job 处理的是什么内容（综艺？纪录片？短视频？）
- 用户有什么特殊要求
- 之前同类型 job 遇到过什么问题
- 当前 job 的哪些 stage 已经跑过（如果是 resume）

### 差距 3：没有"累积学习"——每次都是从零开始

**Z3r0 的做法：** RAG 检索 + history.jsonl Recent History → Agent 能参考"之前类似任务是怎么做的"。

**cut_bot 的现状：** 子代理是**无状态的一次性执行**。即使同一个用户跑了 100 个 job，第 101 个 job 的 source_agent 对前 100 个 job 的经验一无所知。

### 差距 4：HITL 的上下文断裂

**Z3r0 的做法：** HITL Gate 暂停后，human 的 decision 写入 checkpoint，resume 时 Agent 能看到完整的 decision 上下文（approved/rejected + reason + modifications），而且之前的对话历史都在。

**cut_bot 的现状：**

```python
# HITLGatePlugin
async def execute(self, state: dict[str, Any]) -> NodeResult:
    requires_human = node_config.get("human_review", False) or state.get("_requires_human_review", False)
    if not requires_human:
        return NodeResult(status="completed", output=dict(state))
    return NodeResult(status="waiting_human", output=dict(state))

# resume 时
# engine.resume() 把 decision 写入 checkpoint
# 但 decision 的内容（reason、modifications）并没有注入到下一个节点的上下文里
```

human 说了"这个镜头切分太细了，合并一下"——这个反馈在 resume 后**只存在 checkpoint 里**，下一个子代理看不到。

### 差距 5：nanobot 的 Dream 机制未被利用

nanobot 有完整的 **Dream 两阶段记忆整合**：
1. 对话历史 → history.jsonl
2. Dream 定期用 LLM 分析历史 → 更新 MEMORY.md / SOUL.md / USER.md

这意味着 nanobot **本身就有"跨会话学习"的能力**。但 cut_bot 的子代理完全没有利用这个机制——它们用独立的 session key，产生的历史不进主 session 的 history.jsonl，Dream 也看不到它们。

---

## 三、具体的借鉴方案

### 借鉴 1：引入"Job Context"结构化注入（学 Z3r0 的 work_project_context）

```python
# 设想的 JobContextBuilder
class JobContextBuilder:
    """每次子代理启动时，构建结构化的 job 上下文。"""

    def build(self, job_root: Path, state: dict) -> str:
        parts = []

        # 1. Job 元信息
        meta = self._read_job_meta(job_root)  # 从 job_root/.meta.json 读
        parts.append(f"## Job Info\n- Type: {meta.content_type}\n- Episodes: {meta.episode_count}")

        # 2. 前置节点的产出摘要
        if "_last_result" in state:
            prev = state["_last_result"]
            parts.append(f"## Previous Agent Output\n- Agent: {state.get('_agent_name')}")
            parts.append(f"- Status: {prev.status}")
            for art in prev.artifacts:
                parts.append(f"- Artifact: {art.name} → {art.path}")

        # 3. 用户原始需求（从主 Agent 的 session 提取）
        if "user_request" in state:
            parts.append(f"## User Request\n{state['user_request']}")

        # 4. HITL 反馈（如果有）
        if state.get("_human_decision"):
            d = state["_human_decision"]
            parts.append(f"## Human Feedback\n- Approved: {d['approved']}\n- Reason: {d['reason']}")

        return "\n".join(parts)
```

**不需要 DB。** 用文件系统 + state dict 就能实现类似 Z3r0 的"每轮注入"效果。

### 借鉴 2：子代理之间的"交接单"（学 Z3r0 的黑板模式）

```python
# 在 job_root 下维护一个 handoff.json
# 每个子代理完成后写入自己的产出摘要

class AgentHandoff:
    """子代理之间的结构化交接。"""

    @staticmethod
    def write(job_root: Path, agent_name: str, result: DomainResult):
        handoff_file = job_root / ".handoff.json"
        data = {}
        if handoff_file.exists():
            data = json.loads(handoff_file.read_text())

        data[agent_name] = {
            "status": result.status.value,
            "artifacts": [{"name": a.name, "path": a.path} for a in result.artifacts],
            "errors": result.errors,
            "milestone": result.milestone_reached,
            "duration_ms": result.duration_ms,
        }
        handoff_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    @staticmethod
    def read(job_root: Path) -> dict:
        handoff_file = job_root / ".handoff.json"
        if handoff_file.exists():
            return json.loads(handoff_file.read_text())
        return {}
```

story_agent 启动时就能读到：
```json
{
  "source_agent": {
    "status": "success",
    "artifacts": [
      {"name": "event_cards", "path": "01_source/event_cards.json"},
      {"name": "episode_digests", "path": "01_source/episode_digests.json"}
    ],
    "errors": [],
    "milestone": "source_ready"
  }
}
```

### 借鉴 3：把子代理的关键结果写回主 session 的 history.jsonl

```python
# 子代理完成后，把摘要写入主 session 的 history
# 这样 Dream 能学到，Recent History 也能参考

def _record_to_main_history(self, agent_name: str, result: DomainResult):
    summary = f"[{agent_name}] {result.status.value}: "
    summary += ", ".join(a.name for a in result.artifacts)
    if result.errors:
        summary += f" | errors: {'; '.join(result.errors)}"

    # 写入主 session 的 history.jsonl
    self.memory_store.append_history(summary, session_key=main_session_key)
```

### 借鉴 4：利用 nanobot 的 Runtime Context 注入 job 状态

nanobot 已经有 `RuntimeContextProvider` 机制——每轮可以注入额外上下文。可以注册一个 provider：

```python
async def job_status_provider(request: RequestContext) -> RuntimeContextBlock | None:
    """如果有活跃的 pipeline job，注入当前状态。"""
    job_root = get_active_job_root(request.session_key)
    if not job_root:
        return None

    handoff = AgentHandoff.read(job_root)
    content = f"[Pipeline Status]\nActive job: {job_root}\nCompleted agents: {list(handoff.keys())}"
    return RuntimeContextBlock(source="pipeline_status", content=content)
```

这样主 Agent 每轮都能看到"当前 pipeline 跑到哪了"。

### 借鉴 5：HITL 反馈注入下一节点的 task prompt

```python
# engine.resume() 时，把 decision 写入 state
session.status = SessionStatus.RUNNING
latest.human_decision = {
    "approved": decision.approved,
    "modifications": decision.modifications,
    "reason": decision.reason,        # ← 这个要传给下一个子代理
}

# SubAgentPlugin.execute() 时，检查 state 里有没有 human_decision
human_feedback = state.get("_last_human_decision")
if human_feedback:
    task_prompt += f"\n\nHuman Review Feedback:\n- Approved: {human_feedback['approved']}\n- Reason: {human_feedback['reason']}"
```

---

## 四、总结：利用情况评分

| 维度 | 利用了 nanobot 的什么 | 没利用什么 | 评分 |
|------|---------------------|-----------|:---:|
| **System Prompt** | 子代理用 AgentBuilder 获取 instructions | 用的是 "editor" 的通用身份，不是定制身份 | 50% |
| **对话历史** | 子代理有独立 session，历史在 session 内累积 | 主 Agent 历史不传给子代理；子代理历史不回写主 session | 30% |
| **MEMORY.md** | 主 Agent 能看到 | 子代理完全看不到 MEMORY.md | 20% |
| **Skills** | 通过 `_build_task_prompt` 注入 skill 全文 | ✅ 利用得不错 | 80% |
| **Dream** | 主 Agent 的 Dream 正常工作 | 子代理的历史不进 history.jsonl，Dream 看不到 | 10% |
| **Recent History** | 主 Agent 能看到 | 子代理看不到；子代理的产出不进 Recent History | 20% |
| **Context Governor** | ✅ 子代理也走 AgentRunner，自动享受飞行中压缩 | — | 100% |
| **Consolidation** | ✅ 子代理 session 也走 consolidation | 但子代理 session 通常是短命的一次性的 | 60% |
| **Runtime Context** | 主 Agent 有 | 子代理没有；没有注册 pipeline 相关的 provider | 10% |
| **工具结果管理** | ✅ 子代理通过 nanobot 工具执行 stage | 但 stage 结果是文件，不进 tool result 管理 | 50% |

### 核心问题

**重新设计后的 cut_bot 把 nanobot 当成了一个"执行引擎"——用它跑 LLM 循环、执行工具、管理 session——但没有利用 nanobot 的"记忆体系"。**

子代理是"失忆的临时工"：
- 不知道自己之前做过什么（没有跨 session 记忆）
- 不知道同事做了什么（没有交接单）
- 不知道用户想要什么（没有需求传递）
- 做完就走，不留痕迹（历史不回写主 session）

Z3r0 的 Agent 是"有记忆的团队成员"：
- 每轮看到项目最新状态（WorkProject 注入）
- 能看到同事的公开结论（投影层）
- 能参考知识库（RAG）
- 自己的产出写入共享黑板（DB）

**不需要把 cut_bot 改造成 Z3r0 那样重——但"交接单"、"job 上下文注入"、"子代理结果回写"这三件事是投入产出比最高的改进。**