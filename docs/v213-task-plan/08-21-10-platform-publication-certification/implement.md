# Platform Publication Certification Implementation Plan

## 0. Do-not-start Gate

本文件是未来执行计划，不代表启动授权。当前任务保持 `planning`，外部发布保持
disabled。开始任何产品代码、平台调用或 credential provisioning 前必须：

- 用户评审并在后续消息明确批准本 PRD/design/implement；
- 选择非生产候选 target/tenant，并完成 API/surface/capability evidence research；
- 验证新鲜的 production-grade LocalRealE2EReceipt 与双 Runtime
  RuntimeConformanceReceipt exact refs，而不是采信 task status、日志或旧 review；
- 冻结平台 dossier、visibility domain/surface inventory、reconcile window、credential
  scopes、batch bounds 与 withdrawal SLO；
- 产品代码 repository 固定为 `auto_cut_bot`，绑定 exact predecessor commit；
  `ac_auto_cut` 仅可读其 `原理/v2.1-implementation-design/**`，产品 diff 必须为零；
- 创建新的 TaskSnapshot，按下表为每个 wave 声明 repo-relative exact allowed write
  paths、独立 worktree/candidate 与 required checks；平台未选定前 adapter wave 不存在；
- 验证 authority/consumer lock、Schema/Registry、import firewall、Reuse Ledger、
  task-control-plane lock 与 Supervisor contract hashes。
- 将 JSONL 仅作为来源清单交给机器 Loader：对每个直接文件记录 canonical path、
  byte length、UTF-8/EOF closure 与 full-file SHA-256；Context Planner 对实际注入内容
  记录稳定 section/heading、byte range、slice SHA-256，并闭合定义、前置不变量、错误
  规则、例外及直接 Command/Rule/Artifact refs。
- TaskSnapshot 同时绑定 authority/context、base/candidate、repository predecessor、
  gate bundle、toolchain lock、Supervisor contract 与 per-wave allowlist hashes。任何
  截断、来源漂移、无稳定 anchor、slice/ref closure 不完整或闭包超预算都 deny；按
  owner/AC 拆 child task，不能自动摘要、删减错误规则或把 JSONL 当 pass oracle。

任一前置不存在、stale、expired、revoked 或无法复算时停止。若 publication source/
Registry/profile 仍只是 structural wire shape 或缺少本设计要求的 closed binding，另建
Authority Change task；本任务不能修改自己的 authority/oracle 来通过。

## 0.1 Multi-agent waves and exact file ownership

表中 namespace 均相对 `auto_cut_bot` repository root。一个文件在整个 active wave
期间只能有一个 writer；每个 writer 使用独立 worktree 并提交自己的 exact candidate。
共享 Authority/Schema、Store、Coordinator、Adapter 与 Integration waves 默认严格串行。

| Wave / owner | Repository | Exact repo-relative write path or namespace | Ordering and exclusion |
|---|---|---|---|
| P0 capability researcher | none (read-only) | none | 只读平台文档/非生产 capability；未知平台变量不写产品文件 |
| A independent Authority/Schema Change | `auto_cut_bot` | `packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/publication/**`; `packages/autocut-kernel/src/autocut_kernel/contracts/generated/publication/**`; `governance/{authority-sources.yaml,authority-lock.yaml,schema-index.yaml,protected-paths.yaml}` | 独立 protected task；Task10 implementer 零写入；其自身 Snapshot 必须逐文件列出 source/generated outputs，不能从 glob 自行扩权 |
| B Store/migration owner | `auto_cut_bot` | `packages/autocut-kernel/src/autocut_kernel/store/{models.py,postgres.py}`; `packages/autocut-kernel/migrations/*publication*.sql`; `tests/store/test_publication*.py` | A accepted 后单独执行；不得改 Coordinator、Runtime、adapter、Authority/Schema |
| C shared Coordinator/validator owner | `auto_cut_bot` | `packages/autocut-kernel/src/autocut_kernel/publication/**`; `tests/publication/unit/**`; `tests/publication/simulator/**` | B accepted 后执行；Store 文件只读；唯一 owner 负责 batch key/state/reconcile/validator |
| D selected platform adapter/credential owner | `auto_cut_bot` | `auto_cut_bot/publication_adapters/<selected_adapter_slug>/**`; `tests/publication/adapters/<selected_adapter_slug>/**` | C accepted 且平台选定后才把 placeholder 替换为一个真实 slug 并冻结；每次只允许一个 adapter owner，不得改 kernel/Store/Runtime |
| E serial integration owner | `auto_cut_bot` | `auto_cut_bot/pipeline/runtime/{composition.py,models.py,ports.py,service.py,worker.py,__init__.py}`; `tests/publication/integration/**` | C、D accepted 后唯一串行 owner；其他 Agent 不得同时修改共享 composition/exports |
| F certification harness owner | `auto_cut_bot` | `tests/publication/certification/**` | E accepted 后；只写 harness/fixtures，不修 production 或 oracle；真实调用需显式 non-production allowlist |
| G independent checker/adversarial reviewer | read-only candidate | none | 只读 exact candidate；不得改产品、tests、expected results、Authority 或 task AC |

`<selected_adapter_slug>` 当前是禁止写入的未决符号，不是默认目录名。平台/tenant/API
选择完成后必须在最新 TaskSnapshot 中替换为一个真实、规范化、单值路径；若无法选择，
Wave D–F 不启动，任务保持 planning/disabled。

Repair 根据 finding 的 `canonical_location` 只返回 A/B/C/D/E/F 中唯一原 owner：
checker 不修代码，integration owner 不代修 Store/Coordinator/adapter，任何需要第二
owner 同时改同一文件的 finding 先拆 child 或串行 handoff。修复 commit 使旧 candidate/
CheckReport/SupervisorDecision 失效，重新生成受影响的 hash-bound evidence；同一稳定
fingerprint 最多两轮，第三轮 deny/replan。

## 1. Platform Feasibility and Certification Profile

1. 对候选平台做只读 capability discovery，不上传内容：冻结 target/tenant/API/adapter
   identity、原生 transaction/idempotency/query/abort/withdraw 语义、private staging
   保留期、对象与 batch 上限、rate limit、cache/index 行为。
2. 列出全部 visibility surfaces：direct URL、profile/list、search、feed、public API、
   embed、notification、cache/CDN 及平台特有 surface；记录每个 authority query。
3. 判定 class A、严格 class B 或 ineligible。没有单一 atomic activate/不可绕过
   pointer、surface 不完整、query 非权威、idempotency 小于 reconcile window、无法
   withdraw 时，产生 capability-deny 并停止 public activation 实现。
4. 冻结 certification profile、非敏感 corpus、batch bounds、fault matrix、expected
   0/N membership 与独立 evidence collector。被测 adapter 不拥有 test oracle。

**Review gate P1**：平台能力评审先于 SDK/adapter 编码；ineligible 目标不允许用
“快速逐条发布”变通。

## 2. Authority and Contract Slice

1. 由 Wave A 的独立 Authority/Schema Change 在唯一 authority source owner 中闭合 PublicationBatch、PreparedObjectSet、
   PublicationLedger、intent/observation/result、Capability、Enablement、Decision
   verifier、Incident、Compensation、Withdrawal 与 CertificationReceipt schemas/
   Registry/profile bindings。若属于 protected authority 变更，必须由前置 Authority
   Change task 完成；普通 Task10 Wave B–F 只消费 accepted generated results。
2. 定义 canonical batch key、ordered object/surface digest、external transaction ID、
   signature/attestation payload、state transition 与 exact-ref dependency closure。
3. 生成 good/base/bad fixtures：wrong target/digest/ref、free fields、forged/stale/
   expired/revoked signature/capability/enablement、unknown surface/state、illegal
   transition、batch mutation 与 capability self-issuance。
4. 定义 LocalRealE2EReceipt/RuntimeConformanceReceipt admission verifier；receipt
   schema 或 producer 缺口回到 owning predecessor task，禁止本地 stub/pass fixture。

**Rollback point P2**：先独立交付 contract/codegen/negative fixtures；存在开放语义
或缺 owner binding 时停止，不进入 Store/Coordinator。

## 3. Store, Roles and Internal Atomicity

1. 用 forward-only migration/transaction profiles 落地 immutable publication facts、
   exact paired refs、logical heads、command slots、leases/fences、outbox 与 reconcile
   work items；projection 只读且无授权能力。
2. 实现一个内部事务的 Batch reservation：Batch + planned Ledger + Receipt + lineage
   freeze + outbox；same request replay、different request conflict。
3. 实现每次 intent/result/Ledger head 的 expected exact ref + fencing CAS；外部调用
   位于 durable intent commit 之后，结果只在独立内部事务验证后提交。
4. 创建最小 DB roles：kernel transaction functions、projection worker、reconciler；
   Runtime、adapter 和 projection role 不能直接写 authority tables，Publisher 不能写
   Decision/Capability/Enablement chains。
5. PostgreSQL 测试覆盖 concurrent same/different keys、双 Coordinator、lease takeover、
   stale fence、intent commit 前后 crash、external response 后 DB rollback、outbox
   duplicate 与 Runtime switch。

**Review gate P3**：证明 internal atomicity 与 external uncertainty 被分离；不得以
平台 response 直接更新 projection 并自称 committed。

## 4. Shared Coordinator and Validator

1. 实现 admission verifier，prepare 与 activate 前均回读五类 prerequisite current
   heads、签名/key registry、expiry/revocation、exact hashes 与 dependency closure。
2. 实现 private staging：per-object durable intent、stable request ID、exact bytes/
   metadata/private validation、complete surface zero-visible query 和原子
   PreparedObjectSet commit。
3. 实现 activation：durable intent 后调用一次逻辑 activate；所有响应（含 success）
   进入 authority query validator，只有完整同 epoch exact N 才 committed。
4. 实现 reconcile：原 transaction/idempotency identity、persistent work item、bounded
   backoff、quarantined-but-owned 状态；人工不能写 committed。
5. 实现 compensate/withdraw/kill switch：pre-activate abort hidden；post-effect
   withdraw all；mixed visibility 自动 incident + capability revoke + target disable。
6. Pipeline 与 Agent 只提交同一 typed Command intent；拒绝 Runtime 输入 private
   publication state、decision boolean、platform token 或 raw Store writer。

**Review gate P4**：deterministic simulator 对每个 state/effect boundary fault injection
通过，且 simulator evidence 明确不能签发真实 PlatformCapability。

## 5. Credential Broker and Target Adapter

1. 在冻结后的 `auto_cut_bot/publication_adapters/<selected_adapter_slug>/**`
   infrastructure 边界实现唯一目标 adapter；不得让 SDK/credential import
   进入 `autocut_kernel` 或 Runtime/Agent packages。
2. 接入短期 workload identity/secret broker，分别限制 staging、activation、query/
   reconcile、compensation scopes；绑定 target/tenant/batch/operation/expiry。
3. 所有 transport attempt 使用 frozen request body/hash、transaction/request identity、
   API version 和 fence。adapter 返回 observation/raw evidence ref，不返回权威 allow。
4. 实现 adapter error taxonomy：definite rejected、definite not-effected、effect observed、
   indeterminate；catch-all exception 不得映射为 not found/aborted/retry-new-key。
5. secret leak tests 扫描 logs、exceptions、traces、raw response wrapper、fixtures、
   receipts 与 crash dumps。

**Rollback point P5**：adapter 只连接 non-production tenant。无法无损映射 port 或
凭证不能分权时撤销 test identity 并回到 capability deny，绝不连接 production。

## 6. Deterministic Certification Harness

建立可复算 vector matrix，至少覆盖：

- N=1、N=max certified batch、ordered object digest/metadata mismatch；
- 第 1..N 次 upload 前/后 crash、partial upload、upload accepted but response lost、
  duplicate/reordered message、staging TTL expiry、staged direct-link leak；
- activation intent 前/后 crash、activate success/lost/timeout、query 0/N/mixed/
  contradictory/unknown、DB result commit failure；
- 双 Coordinator、父/子 Run、Pipeline/Agent 同时推进、Runtime switch、lease expiry/
  stale fence、same-key replay/different-digest conflict；
- publish decision/capability/enablement forged、expired、revoked、wrong target/hash，
  以及 revocation exactly before prepare/before activate/after activation intent；
- abort/delete-hidden、withdraw/hide-visible、compensation response loss、kill switch、
  old binary unable to reconcile current ledger；
- platform without atomic activation, incomplete surfaces, bypassable pointer, eventual-only
  query, short idempotency retention 和 missing withdrawal 的 hard-deny fixtures。

Harness 记录 runner/toolchain/build hashes、clock source、requests、raw evidence refs 与
expected invariant。test oracle 独立于 adapter，不能调用被测 state mutation 推导
expected success。

## 7. Real Non-production Certification

1. 先运行 hidden-only shadow：上传完整 Batch、验证 bytes/metadata/privacy、查询所有
   surfaces 为 0，abort/delete 并再次证明 0；activation 权限尚未授予 worker。
2. 独立安全评审后，临时授予隔离 audience activation scope，运行真实 0→N test；
   高频采样与 authority query 都必须显示同 epoch exact 0 或 N。
3. 对 direct URL/list/search/feed/API/cache/CDN 和平台特有 surfaces 重放 fault
   matrix；验证重复 logical activate、response loss、adapter crash、credential revoke、
   reconciliation 与 withdrawal。
4. 每轮结束撤销短期 credential，保存 immutable raw evidence，withdraw test objects；
   清理 unknown 保持 incident，不把环境复位当作 pass。
5. mixed visibility、未列 surface、API drift 或无法撤回时立即 revoke 当前 capability、
   发 shadow-only enablement、关闭 target，并以 certification failed 结束；不得只重跑
   success case。

## 8. Independent Check and Enablement Gate

1. 确定性 checks 产生绑定 candidate tree/toolchain/contract 的 CheckReport；独立只读
   Supervisor 按 AC1–AC14 检查 exact evidence，前后复算 candidate tree。
2. authority/high-risk 变更追加定向对抗复核：伪造 allow、错 target reuse、partial
   visibility recovery、credential exfiltration、double activation 与 omitted surface。
3. 认证成功只生成 target/account/API/batch-bound、短期、可撤销的
   PlatformCertificationReceipt/Capability 和 `shadow_only` Enablement，不自动 enabled。
4. 首次 `mode=enabled` 要求新的 fresh TaskSnapshot：最新 Local Real E2E 与双 Runtime
   receipts、最新 capability/decision/key revocation heads、limited rollout bounds、kill
   switch/withdraw drill、远端 release gate 和明确用户/operator 批准。
5. enabled 后先 one-batch canary；任何 indeterminate/mixed/credential incident 自动
   target disable。扩大 account/surface/API/batch size 必须新 capability revision。

## 9. Validation Commands to Freeze at Task Start

具体命令不得现在伪造；选择仓库/平台后写入 TaskSnapshot，并至少包含：

- authority/codegen/schema/registry/trace 与 import-firewall/Reuse-Ledger checks；
- `auto_cut_bot` repository 的 format/lint/type/unit tests；`ac_auto_cut` 仅验证设计
  来源 full-file hashes，且 candidate diff 必须为空；
- disposable PostgreSQL migration/integration/race/crash suite；
- Local Real E2E runner 与双 Runtime conformance runner，输出 exact signed receipts；
- deterministic publication simulator/fault matrix；
- real non-production certification runner（显式 opt-in、target allowlist、ephemeral
  credential；普通 test 命令不得触发真实 effect）；
- secret scan、candidate/history/scope checks、independent Supervisor 与 adversarial review。

所有命令、tool versions、environment manifest、target identity 与 evidence output paths
必须 hash-bound。skip、xfail、missing credential、rate-limit unknown 或 unavailable
surface 不是 pass。

## 10. Stop Conditions

- 前置真实 E2E 或双 Runtime Receipt 不存在/不新鲜/不可复算。
- JSONL 来源未生成 full-file hash/EOF + section/range/slice/ref-closure 闭合的
  TaskSnapshot，或上下文闭包超预算而未拆 child。
- 需要修改 protected authority/oracle 才能表达行为。
- 平台不能提供 class A 或严格 class B，或完整 surface inventory/query 不成立。
- 只能通过逐对象 activation、catch-all retry、新 transaction 或人工 success override。
- publish allow 可由 Coordinator/Runtime/adapter/operator 参数伪造或替换。
- credential 无法按 tenant/batch/operation 分离，或 secret 会进入 evidence/log。
- 发现 mixed visibility、遗漏 surface、idempotency retention 不足或 withdrawal 不可验证。
- 实现范围跨越 `auto_cut_bot` 未声明 path，或 predecessor/authority/kernel build
  identity 不匹配；外部 consumer lock 只能是只读 prerequisite，不能扩张代码 scope。
- `ac_auto_cut` 出现任何产品代码 diff，或平台未选定时创建/猜测 adapter 路径。
- 两个 active owner 需要写同一 Store/Coordinator/Schema/Adapter/Integration 文件，
  或 repair 无法唯一路由到原 owner。

出现 Stop Condition 时保留 fail-closed 状态与证据，回到 owning task/Authority Change/
平台选择；不能降低 AC、切换到 independent outputs 或提前启用。
