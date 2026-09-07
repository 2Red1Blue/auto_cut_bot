# R0：开源能力实验底座

## 目标与完成定义

建立一个能在 PC WSL 重放已保存结果、也能在显式预算下调用在线模型的统一实验入口。
完成后可以回答“某项方法是否提高语义/剪辑质量”，但不会改变生产 Pipeline。

完成必须同时满足：固定源码与输入身份、私有媒体路径映射、实际断网 replay、受预算约束 live、
原始响应不变保存、可复算投影、带分母的指标、同一实验可恢复、假 provider 集成测试通过。

## 现状与修改范围

已有 `tools/reuse/runner_skeleton.py` 只是样板：未实现 producer 调用，环境变量不能构成断网，
输出会停在 `NotImplementedError`。已有两个 fixture 中，Lucifer 的时间标签未核对视频，
book 42000021919 仍为 `frozen=false`。这些都不能作为完成证据。

拟新增：

```text
tools/reuse/
  run.py                    # 唯一 CLI
  models.py                 # closed DTO + JSON codec
  project_registry.py       # projects.json loader
  sandbox.py                # replay 子进程隔离
  storage.py                # append-only attempt 目录和原子 finalize
  metrics.py                # 指标协议与分母检查
  projects.json             # repo/commit/license/adapter 登记
  adapters/fake.py          # 协议验收 adapter
tests/reuse/                 # 纯单元与集成测试
```

旧骨架保留一版迁移说明后删除，避免两个入口并存。外部依赖仍在 `reference-projects/` 的独立环境。

## 数据合同

`ExperimentSpec/v1` 必需字段：

| 字段 | 必要性 | 类型/约束 | 用途 |
|---|---|---|---|
| `experiment_id` | 必需 | 稳定短 ID | 多次 attempt 的逻辑身份 |
| `producer_id`, `producer_commit`, `adapter_version` | 必需 | registry 中精确匹配 | 固定执行代码 |
| `fixture_ref`, `fixture_sha256` | 必需 | 已登记 manifest | 固定输入 |
| `variant` | 必需 | closed string | baseline/candidate 策略 |
| `mode` | 必需 | `replay\|live` | 禁止自动切换模式 |
| `model_request` | 条件必需 | replay 为空、live 为 closed object | 非密钥 provider/model/prompt/schema 身份；禁止 credential/header |
| `budget` | live 必需 | calls/input/output tokens/concurrency | live 前检查，调用后累计 |
| `metric_policy` | 必需 | 版本和标签 revision | 防止评分口径漂移 |

`ExperimentAttempt/v1` 状态限定为 `reserved/running/succeeded/failed/not_evaluated`。
错误码至少包含 `REPLAY_MISS`、`SOURCE_MISMATCH`、`FIXTURE_UNVERIFIED`、`BUDGET_EXHAUSTED`、
`PROVIDER_RESULT_UNKNOWN`、`PROJECTION_REJECTED`、`METRIC_INPUT_INCOMPLETE`。

目录身份使用 `experiment_id/attempt_id`。`metadata.json` 先写临时文件、fsync、原子 rename；
成功标记只能在 raw、projection、metrics 的 hash 全部写入之后生成。恢复时读取同一 spec hash；
未知在线结果保存 provider response ID 并轮询，不换 attempt 重新调用。

producer adapter 使用窄接口：`validate(spec, fixture)`、`build_calls(state)`、
`accept_response(call_id, raw)`、`finalize()`、`project(raw_outputs)`。外部子进程需要模型时仅通过
JSONL IPC 发出 `ProviderCallRequest/v1`；宿主验证 provider/model/schema/预算后调用现有 adapter，
再返回无密钥响应。多轮项目每一调用都经过同一预算账本，不能在子进程内自行访问网络。

## 执行流程

```text
解析 spec → 核对 project/fixture/media hashes → reserve attempt
  → replay: 在 --network=none 环境运行，或读取精确 request hash 的录制响应
  → live: 宿主 provider adapter 按预算调用，外部子进程不接触密钥
  → 保存 raw → 确定性 projection → 指标计算 → 原子 finalize → report
```

`--media-root` 只映射私有相对路径，不进入 fixture 内容 hash。live 请求/响应进入 `calls/<id>/`，
密钥只留在宿主进程。日志禁止输出 Authorization、DSN 和 signed URL。

## 实现任务

1. 定义 DTO、strict JSON codec 和 projects registry；固定上述参考仓库 commit/license 路径。
2. 实现 attempt storage、崩溃恢复和 hash 校验。
3. 实现 Linux/Podman `--network=none` replay；非 Linux 环境只允许静态检查，不标记隔离通过。
4. 定义 provider port 和 FakeProvider；实现调用/token 预算及未知结果恢复。
5. 实现 manifest v1 兼容读取和 `FixtureManifest/v2`；创建新的已核对标签 revision。
6. 实现 metric protocol 和 Markdown report，缺分母返回 null + 原因。
7. 删除或改写旧骨架入口，更新 `tools/reuse/README.md`。

## 验证和退出门槛

- 单元测试：重复键/未知字段、hash 漂移、路径逃逸、预算边界、重复 attempt、崩溃恢复。
- 集成测试：无网络容器内访问网络失败；子进程环境不含宿主密钥；FakeProvider replay/live 各成功一次。
- 对抗测试：替换 fixture 文件、dirty upstream、修改 prompt 后复用旧 raw、并发相同 attempt 均被发现。
- PC WSL smoke 保存完整 metadata/raw/projection/metrics/report，二次执行复用同一 spec 且不覆盖第一次证据。

退出产物：R0 commit、运行命令、测试报告、一个无私有数据的 fake 实验摘要。真实开源方法没有质量数据时，
R0 仍可完成；不能把协议 smoke 写成 VideoAgent/MMLVE 已有效。
