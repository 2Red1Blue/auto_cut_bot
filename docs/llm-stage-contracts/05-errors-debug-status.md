# 05 错误、重试、Debug 与当前状态

## 错误写到哪里

### 1. HTTP Pipeline 控制面

- `runtime.pipeline_runs`：整次 run 的当前状态。
- `runtime.pipeline_commands`：每个阶段的 ordinal、state、lease、version 和完成时间。
- `runtime.pipeline_run_receipts`：Pipeline 阶段终态。普通 Kernel-backed 失败主要看下层
  Receipt；`VLM_BATCH_CHILD_REQUEST_POLICY_MISMATCH` 这类隔离错误会在这里保存
  `failure_code/failure_detail`。
- `runtime.pipeline_run_outbox`：worker 是否仍需调度该 run。

### 2. Kernel 命令与模型调用

- `runtime.command_slots`：VLM/Stage 1–3 Kernel Command 状态和 request hash。
- `runtime.generation_attempts`：每次模型 Attempt 的 provider idempotency key、response id、
  raw response Blob、failure code/detail 和状态。
- `runtime.command_receipts`：最终 `succeeded/denied/failed`、ArtifactSet 或完整失败原因。
- `runtime.artifact_sets`、`runtime.artifacts`、`runtime.artifact_set_members`：成功业务产物。
- `storage.blob_objects`：不可变 request bytes、raw response bytes 和媒体 Blob。

最小排障查询：

```sql
SELECT ordinal, stage, state, version, completed_at
FROM runtime.pipeline_commands
WHERE run_id = '<run_id>'
ORDER BY ordinal;

SELECT c.command_name, c.state, r.outcome, r.failure_code, r.failure_detail
FROM runtime.command_slots c
LEFT JOIN runtime.command_receipts r USING (command_slot_id)
WHERE c.job_id = (SELECT job_id FROM runtime.jobs WHERE job_key = '<run_id>');

SELECT state, provider_request_id, failure_code, failure_detail
FROM runtime.generation_attempts
WHERE job_id = (SELECT job_id FROM runtime.jobs WHERE job_key = '<run_id>')
ORDER BY reserved_at;
```

## 分阶段文件 Debug

启用：

```bash
export AUTO_CUT_BOT_PIPELINE_MODEL_DEBUG_DIR=/absolute/private/debug/root
```

目录结构：

```text
<root>/<run_id>/<stage>/
  input.json
  output.json
  error.json                         # 仅稳定 error_type，避免泄密
  model/<provider>/<call>-<keyhash>/
    request.json                     # provider 实际请求，递归脱敏
    terminal.json                    # 完整 terminal/usage/status 的脱敏镜像
    raw-output.bin                   # 模型原始文本输出
```

这些文件是诊断镜像，不是权威产物；写文件失败不会改变 Command 结果。API key、Authorization、
cookie、password、secret、token 会被脱敏。真正用于恢复的是数据库 Blob、Attempt 和 Receipt。

## 状态与重试语义

- `failed/denied`：有终态 Receipt，不会自动当成功继续。
- `indeterminate`：外部结果未知或本地执行异常，优先 reconcile；不能盲目重复付费调用。
- `blocked`（目标状态；当前运行时仍以 `indeterminate` + admission barrier 承载）：单个 child 的持久化恢复屏障；不是成功，也不等于批次终态。它表示 indeterminate
  无法在 reconcile 预算/截止时间内收敛，或策略/Request 级预算禁止新 Attempt；需走显式
  recompute、裁决或取消（完整语义见 [08 §6.1](08-subtitle-aware-vlm-to-exact-span-design.md#61-批次暂停单集重试与断点续跑)）。
- 明确的 429/5xx：按冻结 retry policy 最多 3 次，退避 2 秒、8 秒。
- 400/401/403/404/409/422：通常为请求、鉴权或契约错误，不做无意义重试。
- 当前 VLM `VlmResponseRejected` 会按冻结 retry policy 最多自动重生成 3 次，给随机模型输出
  一次纠正机会；每次都有独立 Attempt、provider idempotency key 和失败原因。三次仍解析/引用
  不闭合后，`RETRY_BUDGET_EXHAUSTED` 终止。此时继续重放相同输入不会改善，必须修 prompt、
  Schema/策略或做选择性重算。

排障时常见的恢复记账 code：

- `RETRY_BUDGET_EXHAUSTED`：required child 的 `child_retry_budget`（不是 recompute Request 的 `request_attempt_budget`）耗尽，必须将当前 active 批次终态化为 `failed`；可选 child 按 `OPTIONAL_CHILD_OMITTED` 规则处理；
- `RECOMPUTE_BUDGET_EXHAUSTED`：lineage selected-recompute budget 耗尽；
- `DENIED_NON_RETRYABLE`：确定性的契约/策略拒绝，不得把它当作瞬态失败重试；
- `ABANDONED_BEFORE_INVOCATION`：Claim 已写入但调用前崩溃，Attempt ordinal 已记账；
- `CANCELLED_BY_OPERATOR`：操作员取消事件；批次投影为 `cancelled`，与 `denied` 分开审计；
- `OPTIONAL_CHILD_OMITTED`：明确声明的可选 child 未纳入 successor 发布目标，不得用于掩盖 required child 缺失。

### 批次失败与选择性恢复

批次的“停止”是 admission barrier，不是删除历史或强制全量重跑。完整的 child retry、
selected recompute、reconcile、`blocked`、预算累计、Receipt 收尾、断点续跑和未来并发
并发契约，以 [字幕感知 VLM 到 ExactSpan 设计 §6.1](08-subtitle-aware-vlm-to-exact-span-design.md#61-批次暂停单集重试与断点续跑)
为唯一规范来源；本节只保留排障摘要。当前代码事实是：VLM 提供 `selected_only` 执行过滤入口；
Media Preflight 的正式 Runtime 已接入跨 Run evidence binder 和单集 successor。所选集重算成功后，
Finalizer 会精确重读原 Run 已成功的兄弟集与 successor 的新结果；只有两者共同覆盖完整 source
census，且 producer/runtime authority、策略和依赖哈希完全兼容时，才在 successor 下生成标准
`timed_media_evidence_batch`。若仍有其他失败集或执行身份已经改变，所选集成功
会作为可审计的 inspection/recovery 结果保留，但不会冒充完整 aggregate，也不会进入下游。

## 当前最后已知真实断点（2026-09-01 更新）

最后明确记录的 `semantic_story` 真实 run 是
`pipeline_run_694567bc4b4e456a98aa939f71f24f84`：

- `source_prep`：成功；
- `context_prepare`：成功；
- `vlm`：3 次 Attempt 后失败；
- `stage1_narrative`、`stage2_portfolio`、`stage3_blueprint`：因前序失败未执行。

VLM 原始响应暴露的三类问题为：合法 enum 集合顺序不规范、事件引用未声明 fact `f049`、
candidate measurement 引用不在候选闭包。这里记录的是**最后一次已观察运行事实**，不是说
当前工作树已经再次验证仍会失败。当前代码已加入历史 batch policy mismatch 隔离和
contextual batch identity 修复。该父 run 只包含一集，因此其唯一失败目标对应用户集号 1。
当前代码已支持从该终态父 run 创建 `selected_only + episode_numbers:[1]` 新 run；成功后会
闭合一集完整 VLM Batch，并继续 semantic-story 的 Stage 1–3。尚无一次更新后 PC 实跑可以
把上述 checkpoint 改成成功。

由于该 run 没有进入 Stage 1，Stage 1–3 当前冻结的嵌套 `text.format.json_schema` wire
shape 也尚未完成真实 Ark 验证。这是下一个阶段的显式验证项，而不是已经观察到的失败；具体
差异见 [共同请求与结构化输出](./00-shared-request-envelope.md)。

另有一个旧 50 集 run `pipeline_run_499d1a6ea5614f3aae3d863c3744a772` 曾在 batch finalizer
因成员策略不一致卡住；当前代码会以
`VLM_BATCH_CHILD_REQUEST_POLICY_MISMATCH` 精确失败并阻断该 run，而不是毒死整个 worker。

## 下一次真实验证的停止条件

只跑一集 `semantic_story`。依次确认：

1. VLM `generation_attempts` 成功且 raw output 可从 Blob/debug 对照；
2. VLM batch Receipt/ArtifactSet 闭合；
3. Stage 1、2、3 各自产生成功 Receipt 和完整成员集；
4. 不把 Stage 3 成功误报为“已渲染/已发布”；
5. 单集通过后再扩大集数，失败时使用选择性重算而不是新建全量 VLM run。

## 2026-09-02 实现进度补充

已提交 `MediaPreflightRecomputeRequest`、显式 `stage` 分派和媒体单阶段
`pipeline_commands` 计划。媒体 successor 现在先以 `awaiting_binding` 状态原子占位，
再由持有持久化 `binding` lease 的幂等 evidence binder 绑定不可变源证据，最后通过版本
CAS 激活为 `pending` 并入队。
绑定失败会保留该占位，下一次使用同一 `Idempotency-Key` 可继续绑定；不会留下无控制面
记录的媒体 Blob，也不会让未绑定的命令被 worker 执行。精确请求重放会重新入队以修复
“claim 成功但 enqueue 失败”的窗口；更换同一 key 的集号或策略会被拒绝。

正式 Runtime 已接入跨 Run source/VLM evidence binder：它先证明 base Run 的 SourceManifest、
VLM aggregate 和完整 episode census 可被精确重读，再激活 successor。成功集 exact reuse 与
mixed aggregate finalizer 也已实现；HTTP `stage=media_preflight` 不再因为“功能未接线”固定返回
422。绑定缺失、引用不闭合或目标集越界仍会 fail-closed，且不会误走 VLM recompute 路径。

### 重试能力的边界（重要澄清）

当前代码中“失败重试”不是一个跨阶段的泛化开关，而是按命令类型分别实现：

- VLM `GenerateVlmEvidenceCommand` 已有持久化 `GenerationAttempt` 链。429/5xx、结果
  未知以及可重生成的结构解析拒绝会按冻结 `GenerationRetryPolicy` 创建新的 Attempt；
  预算耗尽后写入最终 `RETRY_BUDGET_EXHAUSTED` Receipt。`selected_only` 是新的
  successor Run，不会改写原始失败 Run。
- Media Preflight 的 successor reservation/binding lease 已可在绑定失败后用同一幂等键
  重试，防止留下未绑定的运行。`MediaPreflightRecomputeRequest.retry_budget` 已被冻结进所选
  child Request，并驱动 `TIMED_SPEECH_BUSY` 的有限自动重试；预算范围为 0–3。它目前是一次
  Command 执行内的有界重试，还不是跨进程持久化 Attempt/CAS 预算，因此进程崩溃后不能声称
  已具备与 VLM GenerationAttempt 相同的恢复证明。
- Media Stage 默认以最多 3 集有界并发执行（运行时可调整，且不改变证据身份）；某集终态失败
  不会取消或阻止其他独立集，成功 child 会保留，但 aggregate 与下游继续 fail-closed。失败集可
  通过媒体单集 successor 重跑；若其余集均已成功且执行身份兼容，mixed aggregate 会复用它们并
  闭合新批次。跨 CPU/CUDA 或 runtime policy 变化的复用还未接入持久化 recovery frontier，当前只
  保留所选集 inspection 结果并要求显式重算缺失身份，不能静默视为完整批次。
  不允许通过 `/resume` 重开终态命令，也不能把当前 `retry_budget` 当作跨重启累计的持久化计数器。

后续仍需补齐媒体子任务的持久化 Attempt、完整失败分类和预算 CAS，才能把当前进程内 BUSY 重试
升级为与 VLM 等价的崩溃安全恢复能力。当前接口可以声明“有限瞬态重试 + 单集 successor”，不能
声明“所有失败都会重试”或“重启不会重置剩余预算”。
