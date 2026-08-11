# Auto Cut Bot Agent 上下文构建流程分析

## 1. System Prompt 构建流程 (agent/loop.py + agent/context.py)

### 构建入口
- **位置**: `agent/loop.py` 第 713 行 `_build_initial_messages()` 方法
- **调用链**: `_build_turn()` → `_build_initial_messages()` → `ContextBuilder.build_messages()`

### ContextBuilder.build_system_prompt() 构建顺序
```python
# agent/context.py 第 82-141 行
def build_system_prompt(...):
    parts = []
    
    # 1. 身份标识 (identity.md 模板)
    parts.append(self._get_identity(...))
    
    # 2. 引导文件 (AGENTS.md, SOUL.md, USER.md)
    parts.append(self._load_bootstrap_files(...))
    
    # 3. 工具使用契约 (tool_contract.md)
    parts.append(render_template("agent/tool_contract.md"))
    
    # 4. 长期记忆 (memory/MEMORY.md)
    if include_memory:
        memory = self.memory.read_memory()
        parts.append(f"# Memory\n\n## Long-term Memory\n{memory}")
    
    # 5. 活跃技能 (always-on skills + 显式调用的技能)
    active_skills = self.skills.get_always_skills()
    active_skills.extend(active_skill_names)
    if active_skills:
        content = self.skills.load_skills_for_context(active_skills)
        parts.append(f"# Active Skills\n\n{content}")
    
    # 6. 技能索引摘要 (排除已加载的技能)
    skills_summary = self.skills.build_skills_summary(exclude=active_skills)
    if skills_summary:
        parts.append(render_template("agent/skills_section.md", ...))
    
    # 7. 近期历史 (memory/history.jsonl)
    if include_memory_recent_history:
        entries = self.memory.read_recent_history_for_prompt(...)
        parts.append("# Recent History\n\n" + history_text)
    
    # 8. 会话摘要 (如果有压缩的上下文)
    if session_summary:
        parts.append(f"[Archived Context Summary]\n\n{session_summary}")
    
    return "\n\n---\n\n".join(parts)
```

### 关键组件
- **Identity 模板** (`templates/agent/identity.md`): 运行时信息、工作区路径、平台策略、格式提示
- **Bootstrap 文件**: 
  - `AGENTS.md` - 项目级指令
  - `SOUL.md` - 人格/风格指导
  - `USER.md` - 用户偏好
- **工具契约** (`templates/agent/tool_contract.md`): 工具使用规范、发现策略、文件操作流程
- **记忆系统**: 长期记忆 + 近期历史 (最多 50 条, 8000 tokens)
- **技能系统**: 始终激活的技能 + 按需加载的技能

### 消息列表构建
```python
# agent/context.py 第 220-279 行
def build_messages(...):
    messages = [
        {"role": "system", "content": self.build_system_prompt(...)},
        *history,  # 历史对话
    ]
    current = self.build_current_message(...)
    
    # 合并连续的相同角色消息
    if messages[-1].get("role") == current_role:
        last["content"] = self._merge_message_content(last["content"], current["content"])
        messages[-1] = last
    else:
        messages.append(current)
    
    return messages
```

---

## 2. AgentRunSpec 结构 (agent/runner.py)

### 数据类定义
```python
# agent/runner.py 第 90-117 行
@dataclass(slots=True)
class AgentRunSpec:
    """Configuration for a single agent execution."""
    
    # 核心参数
    initial_messages: list[dict[str, Any]]  # 初始消息列表 (system + history + current)
    tools: ToolRegistry                      # 工具注册表
    runtime: LLMRuntime                      # LLM 运行时 (provider + model)
    max_iterations: int                      # 最大工具迭代次数
    max_tool_result_chars: int              # 工具结果最大字符数
    
    # 可选配置
    hook: AgentHook | None = None           # 生命周期钩子
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False          # 是否并发执行工具
    fail_on_tool_error: bool = False        # 工具错误时是否终止
    workspace: Path | None = None           # 工作区路径
    session_key: str | None = None          # 会话标识
    context_block_limit: int | None = None  # 上下文块限制
    provider_retry_mode: str = "standard"   # 重试模式
    llm_timeout_s: float | None = None      # LLM 调用超时
    
    # 回调函数
    progress_callback: ProgressCallback | None = None
    stream_progress_deltas: bool = True
    retry_wait_callback: RetryWaitCallback | None = None
    checkpoint_callback: CheckpointCallback | None = None
    injection_callback: InjectionCallback | None = None
    
    # 持续目标支持
    goal_active_predicate: Callable[[], bool] | None = None
    goal_continue_message: GoalContinueMessage | None = None
    finalize_on_max_iterations: bool = True
    
    # 对话状态 (用于 resumable conversations)
    provider_state: ProviderConversationState | None = None
```

### 使用场景

#### 主 Agent (loop.py 第 1069-1103 行)
```python
result = await self.runner.run(AgentRunSpec(
    initial_messages=initial_messages,
    tools=effective_tools,
    runtime=runtime,
    max_iterations=self.max_iterations,
    max_tool_result_chars=self.max_tool_result_chars,
    hook=hook,
    error_message="Sorry, I encountered an error calling the AI model.",
    concurrent_tools=True,
    workspace=effective_scope.project_path,
    session_key=session.key if session else None,
    context_block_limit=self.context_block_limit,
    provider_retry_mode=self.provider_retry_mode,
    progress_callback=on_progress,
    stream_progress_deltas=on_stream is not None,
    retry_wait_callback=on_retry_wait,
    checkpoint_callback=_checkpoint,
    injection_callback=_drain_pending,
    llm_timeout_s=runner_wall_llm_timeout_s(...),
    goal_active_predicate=lambda: sustained_goal_active(session.metadata),
    goal_continue_message=_goal_continue,
    finalize_on_max_iterations=turn_continuation.should_finalize_on_max_iterations(...),
    provider_state=provider_state,
))
```

#### 子 Agent (subagent.py 第 400-415 行)
```python
result = await self.runner.run(AgentRunSpec(
    initial_messages=messages,  # [system, user(task)]
    tools=tools,
    runtime=runtime,
    max_iterations=self.max_iterations,
    max_tool_result_chars=self.max_tool_result_chars,
    hook=_SubagentHook(task_id, status),
    max_iterations_message="Task completed but no final response was generated.",
    finalize_on_max_iterations=False,
    error_message=None,
    fail_on_tool_error=self.fail_on_tool_error,
    checkpoint_callback=_on_checkpoint,
    session_key=sess_key,
    workspace=root,
    llm_timeout_s=llm_timeout,
))
```

---

## 3. ContextBuilder 实现 (agent/context.py)

### 核心类
```python
# agent/context.py 第 66-329 行
class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""
    
    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md"]
    _SKIPPABLE_DEFAULTS = {"AGENTS.md", "USER.md"}
    _MAX_RECENT_HISTORY = 50
    _MAX_HISTORY_TOKENS = 8_000
    
    def __init__(self, workspace: Path, timezone: str | None = None, disabled_skills: list[str] | None = None):
        self.workspace = workspace
        self.timezone = timezone
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace, disabled_skills=set(disabled_skills) if disabled_skills else None)
```

### 关键方法

#### build_system_prompt()
- 构建完整的 system prompt
- 按顺序组装: identity → bootstrap → tool_contract → memory → skills → history → summary
- 支持按 channel 定制格式提示

#### build_messages()
- 构建完整的消息列表
- 自动合并连续相同角色的消息
- 处理 runtime context blocks (运行时上下文注入)

#### build_current_message()
- 构建当前轮次的用户消息
- 支持图片附件 (base64 编码)
- 注入 runtime context blocks

#### _load_bootstrap_files()
- 加载 AGENTS.md, SOUL.md, USER.md
- 跳过未修改的模板内容
- SOUL.md 支持 bundled template fallback

#### _get_identity()
- 渲染 identity.md 模板
- 注入: workspace_path, agent_workspace_path, runtime, platform_policy, channel

---

## 4. SubagentManager.run_inline 实现 (agent/subagent.py)

### 完整实现
```python
# agent/subagent.py 第 289-349 行
async def run_inline(
    self,
    task: str,
    label: str | None = None,
    origin_channel: str = "cli",
    origin_chat_id: str = "direct",
    session_key: str | None = None,
    origin_message_id: str | None = None,
    temperature: float | None = None,
    workspace_scope: WorkspaceScope | None = None,
    *,
    runtime: LLMRuntime | None = None,
) -> str:
    """Run a subagent synchronously and return its result to the caller."""
    
    # 1. 运行时准备
    if runtime is None:
        runtime = self._compat_spawn_runtime()
    if temperature is not None:
        runtime = runtime.with_generation_overrides(temperature=temperature)
    
    # 2. 任务标识
    task_id = str(uuid.uuid4())[:8]
    display_label = label or task[:30] + ("..." if len(task) > 30 else "")
    origin: _SubagentOrigin = {
        "channel": origin_channel,
        "chat_id": origin_chat_id,
        "session_key": session_key,
    }
    
    # 3. 状态跟踪
    status = SubagentStatus(
        task_id=task_id,
        label=display_label,
        task_description=task,
        started_at=time.monotonic(),
    )
    self._task_statuses[task_id] = status
    
    logger.info("Running inline subagent [{}]: {}", task_id, display_label)
    
    # 4. 创建异步任务 (但不后台运行)
    inline_task = asyncio.create_task(
        self._run_subagent(
            task_id,
            task,
            display_label,
            origin,
            status,
            runtime,
            origin_message_id,
            workspace_scope,
            announce=False,  # 关键: 不广播结果
        )
    )
    self._running_tasks[task_id] = inline_task
    if session_key:
        self._session_tasks.setdefault(session_key, set()).add(task_id)
    
    # 5. 等待完成并返回结果
    try:
        result = await inline_task
        if status.phase == "error" or status.stop_reason in {"error", "tool_error"}:
            return ToolResult.error(result)
        return result
    finally:
        # 6. 清理
        self._running_tasks.pop(task_id, None)
        self._task_statuses.pop(task_id, None)
        if session_key and (ids := self._session_tasks.get(session_key)):
            ids.discard(task_id)
            if not ids:
                del self._session_tasks[session_key]
```

### _run_subagent 核心逻辑
```python
# agent/subagent.py 第 351-461 行
async def _run_subagent(self, ...):
    # 1. 构建工具注册表
    tools = self._build_tools(tools_config=cfg)
    
    # 2. 构建子 Agent 专用 system prompt
    system_prompt = self._build_subagent_prompt(workspace=root)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]
    
    # 3. 绑定请求上下文
    request_token = bind_request_context(RequestContext(...))
    token = bind_workspace_scope(workspace_scope) if workspace_scope else None
    
    # 4. 执行 Agent 循环
    try:
        result = await self.runner.run(AgentRunSpec(...))
    finally:
        if token:
            reset_workspace_scope(token)
        reset_request_context(request_token)
    
    # 5. 处理结果
    if result.stop_reason == "tool_error":
        final_result = self._format_partial_progress(result)
    elif result.stop_reason == "error":
        final_result = result.error or "Error: subagent execution failed."
    else:
        final_result = result.final_content or "Task completed but no final response was generated."
    
    # 6. 广播结果 (如果 announce=True)
    if announce:
        await self._announce_result(...)
    
    return final_result
```

### 子 Agent System Prompt
```python
# agent/subagent.py 第 529-545 行
def _build_subagent_prompt(self, workspace: Path | None = None) -> str:
    """Build a focused system prompt for the subagent."""
    from auto_cut_bot.agent.skills import SkillsLoader
    
    agent_workspace = self.workspace.expanduser().resolve()
    project_workspace = workspace.expanduser().resolve() if workspace else agent_workspace
    skills_summary = SkillsLoader(
        self.workspace,
        disabled_skills=self.disabled_skills,
    ).build_skills_summary()
    
    return render_template(
        "agent/subagent_system.md",
        workspace=str(project_workspace),
        agent_workspace=str(agent_workspace),
        history_log=str(agent_workspace / "memory" / "history.jsonl"),
        skills_summary=skills_summary or "",
    )
```

---

## 5. Spawn 工具实现 (agent/tools/spawn.py)

### 完整实现
```python
# agent/tools/spawn.py 第 48-110 行
@tool_parameters(
    tool_parameters_schema(
        task=StringSchema("The task for the subagent to complete"),
        label=StringSchema("Optional short label for the task (for display)"),
        temperature=NumberSchema(
            description="Optional sampling temperature...",
            minimum=0.0,
            maximum=2.0,
        ),
        wait=BooleanSchema(
            description="Wait for the subagent and return its result directly...",
            default=False,
        ),
        required=["task"],
    )
)
class SpawnTool(Tool):
    """Tool to spawn a subagent for background task execution."""
    
    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
    
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        manager = ctx.subagent_manager
        if manager is None:
            raise RuntimeError("SpawnTool requires an initialized subagent manager")
        return cls(manager=manager)
    
    @property
    def name(self) -> str:
        return "spawn"
    
    @property
    def description(self) -> str:
        return (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "Set wait=true for a consultation whose result must inform the current turn. "
            "The subagent will complete the task and report back when done. "
            "For deliverables or existing projects, inspect the workspace first "
            "and use a dedicated subdirectory when helpful."
        )
    
    async def execute(
        self,
        task: str,
        label: str | None = None,
        temperature: float | None = None,
        wait: bool = False,
        **kwargs: Any,
    ) -> str:
        """Spawn a subagent to execute the given task."""
        
        # 1. 并发限制检查
        running = self._manager.get_running_count()
        limit = self._manager.max_concurrent_subagents
        if running >= limit:
            return (
                f"Cannot spawn subagent: concurrency limit reached "
                f"({running}/{limit} running). Wait for a running subagent "
                f"to complete before spawning a new one."
            )
        
        # 2. 运行时检查
        request_ctx = current_request_context()
        if request_ctx is None or request_ctx.runtime is None:
            return ToolResult.error("Error: spawn requires an active model runtime")
        
        # 3. 提取来源信息
        origin_channel = request_ctx.channel
        origin_chat_id = request_ctx.chat_id
        session_key = request_ctx.session_key or f"{origin_channel}:{origin_chat_id}"
        
        # 4. 选择执行模式
        method = self._manager.run_inline if wait else self._manager.spawn
        
        # 5. 执行
        return await method(
            task=task,
            runtime=request_ctx.runtime,
            label=label,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            session_key=session_key,
            origin_message_id=request_ctx.message_id,
            temperature=temperature,
            workspace_scope=current_workspace_scope(),
        )
```

### 两种执行模式

#### spawn() - 后台异步执行
```python
# agent/subagent.py 第 226-287 行
async def spawn(self, ...) -> str:
    # 创建后台任务
    bg_task = asyncio.create_task(self._run_subagent(...))
    self._running_tasks[task_id] = bg_task
    
    # 注册清理回调
    def _cleanup(_: asyncio.Task[str]) -> None:
        self._running_tasks.pop(task_id, None)
        self._task_statuses.pop(task_id, None)
        ...
    bg_task.add_done_callback(_cleanup)
    
    # 立即返回
    return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."
```

#### run_inline() - 同步等待执行
- 创建任务但立即 `await`
- 直接返回结果给调用者
- 不广播结果到消息总线

---

## 6. AgentDefaults 完整定义 (config/schema.py)

```python
# config/schema.py 第 117-195 行
class AgentDefaults(Base):
    """Default agent configuration."""
    
    # 工作区和模型
    workspace: str = "~/.auto_cut_bot/workspace"
    model_preset: str | None = None  # 活跃预设名称 (优先于下面的字段)
    model: str = "anthropic/claude-opus-4-5"
    provider: str = "auto"  # 或 "auto" 自动检测
    
    # 模型参数
    max_tokens: int = 8192
    context_window_tokens: int = 200_000
    context_block_limit: int | None = None
    temperature: float = 0.1
    reasoning_effort: str | None = None  # low/medium/high/xhigh/max/adaptive/none
    
    # 回退模型
    fallback_models: list[FallbackCandidate] = Field(default_factory=list)
    
    # 工具执行
    max_tool_iterations: int = 200
    max_tool_result_chars: int = 16_000
    fail_on_tool_error: bool = True
    
    # 子 Agent
    max_concurrent_subagents: int = Field(default=1, ge=1)
    
    # 重试策略
    provider_retry_mode: Literal["standard", "persistent"] = "standard"
    
    # UI 显示
    tool_hint_max_length: int = Field(default=40, ge=20, le=500)
    bot_name: str = "auto_cut_bot"
    bot_icon: str = "🐈"
    
    # 时区
    timezone: str = "UTC"
    timezone_mode: Literal["auto", "manual"] = "auto"
    
    # 会话管理
    unified_session: bool = False  # 跨渠道共享单一会话
    disabled_skills: list[str] = Field(default_factory=list)
    session_ttl_minutes: int = Field(default=15, ge=0)  # 空闲压缩阈值
    idle_compact_check_interval_seconds: int = Field(default=60, ge=0)
    
    # 记忆压缩
    consolidation_ratio: float = Field(default=0.5, ge=0.1, le=0.95)
    
    # Dream 配置
    dream: DreamConfig = Field(default_factory=DreamConfig)
    
    # 验证器
    @model_validator(mode="before")
    @classmethod
    def resolve_timezone(cls, value: object) -> object:
        """自动检测系统时区"""
        if not isinstance(value, dict):
            return value
        data = dict(cast(dict[str, object], value))
        timezone_mode = data.get("timezoneMode", data.get("timezone_mode"))
        if timezone_mode is None:
            timezone_mode = "manual" if "timezone" in data else "auto"
            data["timezoneMode"] = timezone_mode
        if timezone_mode == "auto":
            data["timezone"] = detect_system_timezone()
        return data
    
    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError(f"unknown timezone {value!r}") from None
        return value
```

### 使用方式
```python
# config/schema.py 第 197-200 行
class AgentsConfig(Base):
    """Agent configuration."""
    defaults: AgentDefaults = Field(default_factory=AgentDefaults)
```

---

## 架构分析总结

### 需要保留的部分

1. **ContextBuilder 的分层构建策略**
   - 清晰的职责分离: identity → bootstrap → tools → memory → skills → history
   - 支持按 channel 定制格式
   - 模板系统灵活 (Jinja2)

2. **AgentRunSpec 的不可变配置**
   - dataclass 保证类型安全
   - 所有参数显式传递,无隐式依赖
   - 支持多种回调钩子

3. **AgentRunner 的纯粹性**
   - 无状态执行循环
   - 不关心消息来源或去向
   - 专注于 LLM 交互和工具执行

4. **SubagentManager 的双模式设计**
   - spawn: 后台异步,结果广播
   - run_inline: 同步等待,直接返回
   - 统一的 `_run_subagent` 核心逻辑

5. **请求上下文绑定**
   - contextvars 实现线程安全
   - 工具可以访问当前请求信息
   - 工作区作用域隔离

### 可能需要重构的部分

1. **ContextBuilder 职责过重**
   - 同时处理: 模板渲染、记忆读取、技能加载、历史截断
   - 建议拆分为: PromptAssembler, MemoryProvider, SkillsProvider

2. **Bootstrap 文件加载逻辑**
   - 硬编码文件名 (AGENTS.md, SOUL.md, USER.md)
   - 模板检测逻辑分散
   - 建议统一为 BootstrapLoader

3. **消息合并逻辑重复**
   - ContextBuilder._merge_message_content
   - AgentRunner._merge_message_content
   - 建议提取到 utils.message_merger

4. **子 Agent 提示词构建**
   - 与主 Agent 提示词构建逻辑不一致
   - 缺少统一的 PromptBuilder 抽象
   - 建议引入 PromptBuilder 接口

5. **配置验证分散**
   - AgentDefaults 有多个 validator
   - 时区检测逻辑复杂
   - 建议集中到 ConfigValidator

6. **缺少 Prompt 缓存机制**
   - 每次调用都重新构建完整 prompt
   - 相同 session 的 bootstrap 文件可以缓存
   - 建议引入 PromptCache

### 架构优势

1. **清晰的层次结构**
   - Loop (编排) → Runner (执行) → Provider (LLM) → Tools
   - 每层职责明确

2. **高度可配置**
   - AgentDefaults 提供细粒度控制
   - 支持预设覆盖
   - 运行时参数灵活

3. **扩展性强**
   - Hook 系统支持生命周期拦截
   - Tool Registry 支持动态注册
   - Skills 系统支持插件化

4. **错误处理完善**
   - 工具错误可配置为终止或继续
   - 重试机制支持多种策略
   - 状态跟踪详细

### 建议的改进方向

1. **引入 PromptBuilder 抽象**
   ```python
   class PromptBuilder(Protocol):
       def build_system_prompt(self, context: PromptContext) -> str: ...
       def build_messages(self, context: PromptContext) -> list[dict]: ...
   ```

2. **统一消息合并工具**
   ```python
   # utils/message_merger.py
   def merge_messages(messages: list[dict]) -> list[dict]: ...
   ```

3. **引入 Prompt 缓存**
   ```python
   class PromptCache:
       def get_or_build(self, key: str, builder: Callable) -> str: ...
   ```

4. **配置验证集中化**
   ```python
   class ConfigValidator:
       def validate(self, config: AgentDefaults) -> list[ValidationError]: ...
   ```

5. **Bootstrap 加载器**
   ```python
   class BootstrapLoader:
       def load(self, workspace: Path) -> dict[str, str]: ...
   ```
