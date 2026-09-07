# R3：Agent 侧 typed inspect / plan / execute

## 目标与完成定义

让 `cut_bot/nanobot` 根据用户目标选择查询、单阶段续跑或本地成片，同时让 HTTP Pipeline 保持固定计划。
Agent 工具调用共享 Pipeline Runtime/Kernel，不再通过旧 `job_root` 文件或 legacy 工具制造另一套业务状态。

完成要求：Agent 可查询确切阶段、生成可检查计划、执行已有 Command、按原 key 恢复；HTTP 不依赖 Agent；
相同输入从两种入口得到同一业务 Artifact hashes 和等价 Receipt/Admission/QC 事实。

## 现状处理

`auto_cut_bot/agent/tools/pipeline/` 已有大量旧工具，其中 `story_render.py` 直接读取文件并 import
`autocut_core`。这些工具不作为新架构调用依据。本阶段不批量删除它们；新工具使用独立命名和注册开关，
并在 R5 切换后将重复旧工具隐藏/标记兼容用途。

新增建议：

```text
auto_cut_bot/agent/tools/media_workflow/
  contracts.py
  capability_catalog.py
  inspect.py
  plan.py
  execute.py
  service_port.py
tests/agent/tools/media_workflow/
```

保持 `agent/loop.py`、`agent/runner.py` 不变，使用现有 `Tool`、`tool_parameters` 和 `ToolRegistry`。

## 能力目录

`CapabilityDescriptor/v1` 由程序注册，字段为 capability_id/version、输入/输出 Artifact types、
依赖能力、调用的 Runtime port、是否调用 provider、可选并行维度和恢复语义。
首批只登记现有可调用能力：source/context/VLM/Stage1/Stage2/Stage3、media preflight、recipe、local render/QC。
尚未在当前 HTTP/Kernel 闭合的能力标 `unavailable` 并给 reason；Agent 不能自行改为可用。

目录 hash 由排序后的描述计算并进入计划。它不把 Python 函数、SQL、任意 shell 暴露给模型。

## 三个工具

### `media_inspect`

输入 job/run ref 及可选 episode/story selector。通过 `PipelineRunService`/Store read port 读取确切 snapshot，
返回 revision、阶段状态、Artifact/Receipt refs、可执行/等待/阻断能力和安全摘要。只读，不返回密钥/Blob 原文。
结果按条数和字节数限制分页；模型需要细节时按 opaque ref 再查询，不能把整批 Artifact payload 塞进聊天上下文。

### `media_plan`

模型只给 `MediaIntentDraft/v1`：goal、episode/story aliases、用户偏好。程序解析为 `ResolvedMediaPlan/v1`：
job/base revision、catalog hash、node DAG、exact input refs/input links、预算和 plan hash。
缺前置时补依赖节点；已有兼容成功节点标 reuse；未知 alias、成环、无授权源直接返回结构化诊断。

| Resolved plan 字段 | 必要性 | 来源/用途 |
|---|---|---|
| `schema_version` | 必需 | 程序常量 |
| `job_ref`, `base_revision` | 必需 | inspect snapshot；执行前做 revision 比较 |
| `capability_catalog_hash` | 必需 | 固定能力和请求版本 |
| `nodes` | 必需、非空 | 程序构建的 DAG |
| `input_refs` | 节点条件必需 | 已提交输入；模型不回显 |
| `input_links` | 节点条件必需 | 上游 node + 注册输出槽位，完成后解析为 refs |
| `budget` | 模型/重媒体节点必需 | provider 调用/token/资源上限 |
| `plan_hash` | 必需 | 规范化业务计划身份；不含日志时间 |

计划通过 `CreateMediaExecutionPlanCommand` 作为普通 Artifact 持久化，使用现有 Store/Receipt，避免新增表。
新 revision 保留旧计划；运行中节点不会被原地重写。

### `media_execute`

输入 plan reference、expected revision 和可选 node subset。先重读计划及输入 refs，再将 ready node 映射到
现有 Runtime/Kernel Command。持久化 node→command key 后调用；pending/unknown 返回恢复信息。
工具输出中的 `succeeded` 只表示本次目标节点完成；本地成片必须有 Render+QC success，发布仍需发布许可。

## 调度、并行与重算

节点状态为 `waiting/ready/running/succeeded/denied/failed/invalidated`。成功节点内容兼容时复用；
计划修订只使依赖改变的节点 invalidated。并行执行 ready 的独立 episode，受 Runtime 已有并发策略限制。
单集失败记录因果，其他已开始集可完成；整批 finalizer 在 census 不闭合时拒绝提交下阶段批次。

Agent 可以发起修复计划；程序决定是否本地 reprocess、重跑当前阶段或等待人工补配置。
同一诊断和输入不产生新 provider attempt。用户改偏好形成新 plan revision，不覆盖旧 Receipt。

## 实现任务与测试

1. 盘点当前 Commands/Runtime ports，生成首版 capability 表和 unavailable 原因。
2. 实现三个 closed DTO、strict codecs、catalog hash、DAG resolver。
3. 实现 CreatePlan Command/reader，复用 Artifact Store；不加新的业务表。
4. 实现三个 Tool 和配置开关；默认只开放 inspect/plan，execute 在集成通过后开启。
5. 单元覆盖 alias、依赖补全、环、stale revision、跨 Job refs、重复 execute、未知结果恢复、预算。
6. 集成覆盖 Agent 单 Stage 2 重跑、本地 render 计划及 HTTP 同输入对照；确认没有 legacy import。

退出条件：一个真实 run 可由 Agent inspect→plan→execute→resume；HTTP 可独立做同一操作；双方业务 hashes
一致。若能力表显示 Stage 4/Render 仍未闭合，只能完成到对应可用节点，不能把计划成功写成成片成功。
