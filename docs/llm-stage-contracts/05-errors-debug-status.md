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
- 明确的 429/5xx：按冻结 retry policy 最多 3 次，退避 2 秒、8 秒。
- 400/401/403/404/409/422：通常为请求、鉴权或契约错误，不做无意义重试。
- 当前 VLM `VlmResponseRejected` 会按冻结 retry policy 最多自动重生成 3 次，给随机模型输出
  一次纠正机会；每次都有独立 Attempt、provider idempotency key 和失败原因。三次仍解析/引用
  不闭合后，`RETRY_BUDGET_EXHAUSTED` 终止。此时继续重放相同输入不会改善，必须修 prompt、
  Schema/策略或做选择性重算。

## 当前最后已知真实断点（2026-08-31）

最后明确记录的 `semantic_story` 真实 run 是
`pipeline_run_694567bc4b4e456a98aa939f71f24f84`：

- `source_prep`：成功；
- `context_prepare`：成功；
- `vlm`：3 次 Attempt 后失败；
- `stage1_narrative`、`stage2_portfolio`、`stage3_blueprint`：因前序失败未执行。

VLM 原始响应暴露的三类问题为：合法 enum 集合顺序不规范、事件引用未声明 fact `f049`、
candidate measurement 引用不在候选闭包。这里记录的是**最后一次已观察运行事实**，不是说
当前工作树已经再次验证仍会失败。当前代码已加入历史 batch policy mismatch 隔离和
contextual batch identity 修复，但尚无一次更新后 PC 实跑可以把上述 checkpoint 改成成功。

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
