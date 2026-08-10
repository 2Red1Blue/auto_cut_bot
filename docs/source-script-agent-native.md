# Agent-Native source_script 剧本解析 — 设计方案

## 问题

当剧本 `when-lucifer-kneels` 有 45 集时，LLM 解析产物只包含前 2 集 12 个场景，被缓存为"成功"并写入 DB。

### 根因链

4 个问题叠加：

| # | 问题 | 原因 |
|---|------|------|
| 1 | 分片逻辑没触发 | `CHUNK_SIZE=120000`，剧本 `85059` 字符 < 阈值，走单次解析 |
| 2 | LLM 输出 token 上限不够 | 45 集完整 JSON 输出远超 `DEFAULT_MAX_TOKENS=256000`，LLM 输出被截断 |
| 3 | 验证没拦住 | `expected_episode_count` 未配置，集数校验直接跳过 |
| 4 | 缓存锁死错误结果 | 截断结果被缓存，后续重跑直接命中缓存跳过执行 |

## 设计原则

**Agent 决定策略，Stage 提供工具。**

```
┌──────────────────────────────────────────────────────────────────┐
│  Agent（策略层）                                                  │
│  - 读剧本，理解结构（LLM 判断，不用正则）                          │
│  - 决定分几轮解析，每轮分多少集                                   │
│  - 验证每轮结果，不通过就换策略重来                                │
│  - 合并所有轮次的结果                                             │
│  - 缓存感知：检测到上次结果异常时自动清除缓存                      │
├──────────────────────────────────────────────────────────────────┤
│  Stage（工具层）                                                  │
│  - 加载剧本文件（.txt / .docx）                                   │
│  - 调用 LLM API 解析指定范围（doubao 1M context）                 │
│  - 字幕时间对齐                                                  │
│  - 写 DB（scenes / subjects / shots / subtitles）                │
│  - 发布产物（ArtifactBus + project.json）                         │
└──────────────────────────────────────────────────────────────────┘
```

## 架构

```
Agent 调用 source_script_load:
  → 拿到剧本全文

Agent 在自己的上下文中解析:
  → "英文剧本，SCENE 格式，约 45 集"
  → 策略: "分 3 段输出，每段 15 集"
  → Round 1: 输出 Episodes 1-15 的结构化 JSON
  → 验证: 15 集? 场景数合理? JSON 完整?
  → Round 2: 输出 Episodes 16-30
  → 验证: 连续性? 无重复?
  → Round 3: 输出 Episodes 31-45
  → 验证: 总计 45 集?

Agent 调用 source_script_save:
  → Stage 做: 字幕对齐 + DB 写入 + 产物发布 + 缓存
```

## 新增文件

### 1. `agent/tools/pipeline/source_script_load.py` — 加载 Tool

Agent 调用此 tool 加载剧本文件，拿到全文后在自己的上下文中解析。

```python
class SourceScriptLoadTool(Tool):
    name = "source_script_load"
    _scopes = {"pipeline"}

    # 参数:
    #   job_root: str (required)
    #   force_reparse: bool — 跳过缓存

    async def execute(self, **kwargs):
        # 加载剧本文件
        script_text = _find_and_read_script(job_root, cfg)
        return ToolResult({
            "script_text": script_text,
            "total_chars": len(script_text),
            "format_hint": _detect_format(script_text),
            "next_action": "parse_in_context",
        })
```

### 2. `agent/tools/pipeline/source_script_save.py` — 保存 Tool

Agent 解析完成后调用此 tool。Stage 做字幕对齐、DB 写入、产物发布、缓存保存。

```python
class SourceScriptSaveTool(Tool):
    name = "source_script_save"
    _scopes = {"pipeline"}

    # 参数:
    #   job_root: str (required)
    #   episodes: list — agent 解析的完整剧集列表
    #   parse_meta: dict — 解析元数据 (rounds, strategy, etc.)

    async def execute(self, **kwargs):
        # 1. 验证集数、连续性、场景分布
        # 2. 字幕时间对齐
        # 3. 写 DB (scenes, subjects, shots, subtitles)
        # 4. 发布产物 (source_script.json)
        # 5. 更新 project.json
        # 6. 保存缓存
        return ToolResult({"status": "saved", "episodes": 45, "scenes": 320, ...})
```

## 修改文件

### 4. `plugins/.../source_script/stage.py` — 提取工具函数

将现有 stage.py 中的函数提取为可被 Tool 和 Stage 共用的独立函数：

```python
def load_script(job_root, cfg) -> tuple[str, dict]:
    """加载剧本文件，返回文本 + 元数据。"""

def save_result(job_root, parsed_data, book_id, db_url, cfg) -> dict:
    """字幕对齐 + DB 写入 + 产物发布 + 缓存。"""
```

Stage 仍可独立运行（非 agent 模式）：`SourceScriptStage.execute()` → 调 `_parse_script()` → 旧逻辑。

Agent 模式下，Stage 只提供 `load_script` 和 `save_result` 两个函数。解析由 Agent 在自己的对话上下文中完成，不分片、不调中间 tool。

### 5. `pipeline/prompt_context.py` — 缓存感知

```python
def is_cache_valid(job_root, script_sha, expected_count) -> bool:
    """检查缓存是否有效。
    无效条件:
    - episodes < expected_count * 0.5 (明显截断)
    - parse_meta.status == "parse_error"
    - force_reparse flag 已设置
    """
```

## Agent 工作流

```
[Agent 收到: "Parse the script at /jobs/when-lucifer-kneels"]

Step 1: Agent 调用 source_script_load(job_root="/jobs/...")
  → 返回: {script_text: "...", total_chars: 85059, format_hint: "english_scene"}

Step 2: Agent 在自己的上下文中分析:
  "英文剧本，SCENE 格式。85,000 字符。我分段输出结构化数据。"

Step 3: Agent 输出 Round 1 (Episodes 1-15):
  → Agent 在自己的响应中输出 15 集的完整 JSON
  → Agent 自己验证: 15 集? 场景数合理? JSON 完整?

Step 4: Agent 输出 Round 2 (Episodes 16-30):
  → Agent 验证: 连续性? 无重叠?

Step 5: Agent 输出 Round 3 (Episodes 31-45):
  → Agent 验证: 总计 45 集? 集号 1-45 连续?

Step 6: Agent 调用 source_script_save(
    job_root="/jobs/...",
    episodes=[...全部 45 集...],
    parse_meta={"rounds": 3, "total_episodes": 45, "strategy": "agent-native"},
)
  → Stage 做: 字幕对齐 + DB 写入 + 产物发布 + 缓存保存
  → 返回: {status: "saved", episodes: 45, scenes: 320}
```

## 缓存策略

```
旧方案（有问题）:
  cache_key = sha256(script_text)[:16]
  → 任何重跑都命中缓存，错误结果被锁死

新方案:
  cache_key = sha256(script_text + strategy_params)[:16]
  strategy_params = f"{rounds}:{expected_count}:{max_tokens}"
  → 不同策略产不同缓存，agent 可换策略重跑

  额外检查: is_cache_valid()
  → 缓存中 episodes < expected_count * 0.5 时自动失效
  → force_reparse=True 时跳过缓存
```

## 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `agent/tools/pipeline/source_script_load.py` | **新建** | 加载剧本，返回文本给 agent |
| `agent/tools/pipeline/source_script_save.py` | **新建** | 接收 agent 解析的结构化数据，字幕对齐 + DB 写入 + 产物发布 |
| `agent/tools/pipeline/__init__.py` | 修改 | 注册 2 个新 tool |
| `pipeline/plugins/.../source_script/stage.py` | 修改 | 提取 `load_script()`, `save_result()` 为独立函数供 Tool 调用 |
| `pipeline/prompt_context.py` | 修改 | 添加 `is_cache_valid()` 缓存感知检查 |

## 向下兼容

```
Stage 仍可独立运行（非 agent 模式）:
  SourceScriptStage.execute() → 调用 _parse_script() → 旧逻辑

Agent 模式:
  SourceScriptTool.execute() → 返回 script 给 agent → agent 调用 parse_chunk + save

判断逻辑:
  if cfg.extra.get("mode") == "agent_native":  # agent 模式
      return script_text to agent  # agent 接管
  else:
      return _parse_script(script_text, cfg)  # 旧逻辑
```

## 验证方式

1. **45 集剧本** `when-lucifer-kneels`：
   - Agent 预分析 → 决定 3 轮
   - 每轮 15 集 → 全部通过
   - 合并 → 45 集完整
2. **2 集短剧本**：
   - Agent 预分析 → 决定 1 轮
   - 单轮完成 → 2 集完整
3. **缓存测试**：
   - 第一次跑完 → 缓存写入
   - 第二次跑（相同参数）→ 缓存命中
   - 第三次跑（不同参数）→ 缓存未命中，重新解析
   - 旧缓存（episodes=2, expected=45）→ `is_cache_valid()` 返回 False → 自动清除