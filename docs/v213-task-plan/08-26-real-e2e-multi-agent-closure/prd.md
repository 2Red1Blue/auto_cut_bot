# 真实本地 E2E 集成父任务与多 Agent 闭环

## 目标和边界

本任务是 planning-only integration parent。它只编排已接受的 child work，最终在
本地完成一集真实 E2E，随后按冻结 partition 扩展到 45 集。它不直接授权产品代码、
remote push、生产启用、外部平台调用、publication source 或 credential path。

唯一允许链路为：HTTP authenticated trigger → immutable SourcePrep → Ark streaming
semantic evidence 与 timed physical evidence → Stage 1–3 Blueprint → Stage 4 exact
A/V Recipe → FFmpeg Render → local publication-grade QC → Receipt-bound local output。
HTTP、Agent、worker 或 caller DTO 都不能创建 Authority/Profile/Calibration/Recipe/QC
事实，也不能以文件、路径、目录扫描、日志或 transport status 证明业务成功。

## 固定依赖顺序

不得并行或倒置以下业务依赖：

```text
08-25 calibration/profile/bootstrap accepted
  -> SourcePrep accepted
  -> Ark semantic evidence || timed physical evidence accepted
  -> Stage 1–3 Blueprint accepted
  -> Stage 4 exact edit accepted
  -> Render/QC/local output accepted
  -> Task09 authority closure + dual-runtime conformance accepted
  -> one-episode final E2E accepted
  -> ordered 45-episode rollout accepted
```

Stage 4 必须同时消费 Stage 1–3 admitted Blueprint 和 committed physical evidence；不得
直接从 VLM coarse interval、ASR/VAD 或 caller payload 生成 Recipe。Task09 位于
Render/QC 后，因为其 conformance case 需要完整 local release facts；它不是 Stage4
的并行前置。每个箭头都要求 predecessor 的 immutable commit、authority/context hash、
required receipt 和 child SupervisorDecision 被重新验证。

## Requirements

### R1. Authority and no-bypass execution

08-25 成功的 governed calibration/profile/bootstrap 是所有 provider work 的硬前置。
HTTP composition 只注入 verified snapshot，缺 anchor/profile/record/snapshot 时在
SourcePrep 前拒绝。所有业务动作走 generated public CommandGateway；Runtime 没有 Store
write、Admission、renderer、provider 或 platform capability。

### R2. Exact provider partition and source evidence

在任何 Ark 或 FunASR dispatch 前，Kernel command 必须 commit 一份 immutable
`ExecutionPartition`：ordered episode-member refs、profile/policy/build hashes、partition
ordinal、provider budget、requested work set 和 predecessor receipt refs。baseline partition
精确包含一集，且 telemetry/attempt ledger 证明其他 44 集 Ark/FunASR provider count 为
0。后续 partition 固定为 3、9、32；它们 pairwise disjoint，连同 baseline 的 union
恰为冻结 episode-set 的 45 个成员。缺、重叠、漏集、变序或非零未授权 provider count
均 deny。

### R3. Timed physical evidence and resource limits

首版 FunASR 是一个 loopback service process、一个复用的 `AutoModel` 实例，同时拥有
SenseVoiceSmall word timestamps 和 FSMN-VAD；推理并发为 **1**，队列容量为 **3**。
全局最大实例为 1，且 RSS、swap、disk staging、queue、request/response bytes 都由锁定
profile budget 约束。容量 3 是等待请求上限，不是三次推理或三进程。提高任何并行/实例
预算需要新 Calibration/Profile/authority acceptance，不能由环境或 HTTP 改写。

### R4. Retry, indeterminate and recovery

只在 dispatch 前可证明没有 external/provider effect、没有 durable intent 且没有
request bytes 离开本进程时，才可标记 retryable。dispatch 后 timeout、ack loss、network
disconnect、Store ambiguity 或 provider unknown 绑定原 Attempt/request identity，进入
`indeterminate`/reconcile；不得新 key、再发一份工作或报告 success。successor action 必须
持久化为 Admission-authorized Recovery record，并由同一 RecoveryLedger CAS/debit
获得权限。Store/CAS/transaction exceptions 传播给 Store recovery，不改写成 business
denial。

### R5. Legacy and secret boundary

每个 child 首次 read legacy 必须在 Reuse Ledger 登记 permitted read root、purpose、exact
content hash 和 bridge status；`autocut_kernel` import firewall 永远拒绝 legacy import。
publication source/credential directories不在任何 child allowlist；所有 publication ports
为 deny-on-call，publication outbox count 必须为零。网络 allowlist 只允许锁定 Ark endpoint
和 `127.0.0.1` FunASR；任何其他 egress 是 blocking failure。secrets 仅存在 local secret
broker/ignored environment，绝不进 Git、Artifact、Receipt、logs、fixture 或 task 文件。

### R6. Local output and no partial success

Render 只消费 Stage4 admitted Recipe。成功 output 必须从一个 succeeded Receipt 可回读到
完整 Render/QC/LocalRelease ArtifactSet 和 exact asset hash。unknown/denied/failed QC、
orphan blob/staging file、partial set 或 directory pointer 都不可见。precommit unreferenced
blob 只能作为 non-admissible cleanup material，不能用于 validator/release。

### R7. Multi-agent control and review cost

每个 wave 在启动前创建 child task、hash-bound TaskSnapshot slices、repo-relative exact
allowlist 和独立 worktree。任何活动旧 task 仍拥有 shared file 时，child 不得抢占。Store、
migrations、composition、models、exports 和 stage-plan 均只由串行 integration owner 在其
指定 W7 命名空间修改；其他 wave 一律只读。每个 leaf 运行 deterministic checks 后立即
local commit；每个 repair 是单独 commit 并回到唯一原 owner。W7 仅产出 AC1–AC8 baseline；
最终 W8 candidate 必须由一名 read-only Supervisor 统一映射 AC1–AC9 并输出
SupervisorDecision。仅 authority/bootstrap、indeterminate recovery 和最终 E2E 追加定向
adversarial review。相同 finding 最多两轮 repair，第三轮 deny/replan。

## Acceptance criteria

- [ ] **AC1 / R1**：authority/profile/bootstrap substitution、missing anchor 或 caller-built
  DTO 在 SourcePrep 前 deny；HTTP/Agent 只能到同一 CommandGateway。
- [ ] **AC2 / R2**：baseline immutable ExecutionPartition 精确一集，其他 44 集 Ark/FunASR
  attempt/provider count 为零；3/9/32 partitions disjoint 且总 union=45。
- [ ] **AC3 / R3**：FunASR telemetry 证明全局一实例、推理并发一、队列最多三，并拒绝
  profile budget 外 RSS/swap/disk/bytes/queue；不存在静默扩容。
- [ ] **AC4 / R4**：pre-dispatch safe retry、post-dispatch original-attempt indeterminate
  reconcile、Store exception propagation 和 Recovery authorization 都有 deterministic tests。
- [ ] **AC5 / R5**：Reuse Ledger/import firewall、credential/source exclusion、deny-on-call
  publication port、zero publication outbox 和 Ark+loopback-only egress checks 都通过。
- [ ] **AC6 / R6**：一集真实本地链路完成 committed evidence → Blueprint → Recipe →
  Render/QC → seekable output；所有 denial/unknown/orphan/partial negative cases不可见。
- [ ] **AC7 / R7**：每个 child 的 allowlist/worktree/TaskSnapshot/leaf commit 有记录；
  W7 的 baseline Supervisor 只检查 AC1–AC8，最终 W8 candidate 的统一 SupervisorDecision
  映射 AC1–AC9，指定 adversarial reviews 通过。
- [ ] **AC8 / fixed order**：Task09 detailed authority closure/conformance 在 Render/QC 后
  通过；其 exact receipt 与 one-episode final E2E receipt 共同成为 45 集开闸条件。
- [ ] **AC9 / rollout**：45 集 coordinator 只接受冻结 partitions，per-member terminal
  Receipt 可重放；未 resolved member 时 aggregate success deny。

## Non-goals

外部 publication、target/credential provisioning、production rollout、remote push、旧
Stage/ArtifactBus/file-state reuse、调用方选择 provider/profile/endpoint 或绕过 calibration。
