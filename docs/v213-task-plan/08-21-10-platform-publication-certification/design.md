# Platform Publication Certification Design

## 1. Boundary and Safety Claim

认证只针对一个明确的：

```text
(platform target, tenant/account, adapter/API version,
 visibility domain, ordered visibility surfaces, credential policy)
```

结论不可泛化到同平台其他 account、API revision、surface、batch size 或凭证
策略。平台控制域之外的抓取、转存和终端缓存不在原子性声明内，但任何平台控制
且可能发现内容的 surface 都必须列入 domain，不能为了通过测试而省略。

必须区分两个提交边界：

1. **Internal atomic commit**：PostgreSQL 在一个事务中提交 immutable ArtifactSet、
   intent/result evidence、Ledger revision、Receipt、exact-head CAS 与 outbox。
2. **External atomic visibility**：由平台原生 batch activation 或不可绕过的受控
   pointer 在认证 domain 中提供。数据库提交不能创造、撤销或证明该原子性。

系统采用可恢复 protocol/saga，不宣称跨 PostgreSQL 与第三方平台的分布式 ACID。
任何外部 effect 后的不确定结果都保持不确定，直到 authority query 证明终态。

### 1.1 Repository boundary

只有 `auto_cut_bot` Git repository 可以承载本任务未来的产品变更：

```text
auto_cut_bot repository
├─ packages/autocut-kernel/src/autocut_kernel/publication/**  # shared domain/coordinator
├─ packages/autocut-kernel/src/autocut_kernel/store/**        # serial persistence integration
├─ packages/autocut-kernel/migrations/*publication*.sql       # serial additive migration
├─ auto_cut_bot/publication_adapters/<selected_adapter_slug>/**
├─ auto_cut_bot/pipeline/runtime/**                            # serial composition integration
└─ tests/publication/**                                       # contract/simulator/integration evidence
```

`<selected_adapter_slug>` 不是可直接创建的默认路径。平台、tenant、API 与 credential
语义完成 feasibility review 后，TaskSnapshot 必须把它替换为唯一、真实、repo-relative
目录；在此之前 adapter wave 不存在且该 namespace 不可写。

`ac_auto_cut/原理/v2.1-implementation-design/**` 仅为只读设计来源。`ac_auto_cut`
repository 不接受本任务的 migration、Store、Coordinator、adapter、credential、test
harness 或 Runtime composition 写入，也不能作为第二份 `autocut_kernel` source。

Authority/source/schema 缺口由独立 Authority Change task 在 `auto_cut_bot` repository
的受保护 namespace 内串行完成。普通 Task10 implementer 不能同时拥有 Authority 与
业务实现路径；生成产物与 source paths 必须由该 Authority task 自己的 exact
allowlist/TaskSnapshot 冻结，而不是从本设计推断 broad glob。

## 2. Trust and Capability Separation

```text
Local Real E2E Receipt ───────────────┐
Dual Runtime Conformance Receipt ─────┤
Signed PublishDecision(allow) ─────────┼─> Publication Admission Verifier
PlatformPublicationCapability ─────────┤              │
PublicationEnablement(active/current) ─┘              ▼
                                              Publication Coordinator
                                                       │ short-lived scoped identity
                                                       ▼
                                                Platform Adapter/Port
```

五个输入彼此独立：Local E2E 证明当前 build 能从真实输入得到 committed
QC/Release facts；Runtime conformance 证明两种 Runtime 不能改变共享事实、batch
identity 或决策；PublishDecision 证明 exact objects 在当前 target policy 下业务上
允许发布；Capability 证明 target/account/API 能实现所声明事务语义；Enablement
证明这些 exact identities 当前获准使用且未过期/撤销。

`allow` Artifact 由隔离 Release Authority 签发。签名/attestation payload 使用
closed canonical encoding，覆盖 exact refs 与上下文 hash；verifier 从受保护 key
registry/current revocation head 解析 key。Coordinator、adapter、Runtime、operator
输入和普通数据库角色均不能写 allow chain。任何自由 JSON、复制 ID、旧签名、
错 object digest 或仅 projection 中存在的 allow 都拒绝。

## 3. Platform Capability Classes

### 3.1 Eligible class A: native atomic activation

平台支持 private prepare、稳定 transaction ID、同一 transaction 的单一逻辑
activate、全 surface authority query、足够长的 idempotency retention，以及
abort/withdraw。允许一个逻辑 activate 有多个 transport attempt 的唯一前提是：
所有 attempt 使用同一 certified idempotency identity，平台保证它们映射到同一
transaction effect。

### 3.2 Eligible class B: non-bypassable indirection

平台对象始终 private，任何 public reader 都只能通过一个受控 manifest/pointer；
pointer 切换原子，旧/新 manifest immutable，direct object locator、list/search/
feed/API/CDN 均不能绕过 pointer。只要存在一个可公开访问的 staged object 或独立
索引 surface，该能力降为 ineligible。

### 3.3 Ineligible classes

以下能力不能运行 unattended `all_or_nothing`：N 个逐对象 visibility mutations；
无同 epoch proof 的“预约同时发布”；webhook/eventual list 作为唯一 query；surface
不可枚举或 query 不能绑定 exact transaction/object digest；direct URL、搜索、feed
或 cache/CDN 独立暴露；timeout 后只能创建新 transaction；幂等记录短于 reconcile
window；没有 hidden abort、visible withdrawal 或 credential isolation。

ineligible 的正确输出是 capability deny。测试可停留在 local simulator/hidden
shadow，不能调用真实 activation。若未来支持 `independent_outputs`，必须另建任务、
Policy 和 Ledger，不能由本协议 fallback。

## 4. Immutable Data Model

所有对象采用 closed schema、canonical bytes、content hash 与 exact `ArtifactRef`。

### 4.1 `PublicationBatch`

Batch 是 immutable plan，不承载可变 lifecycle：

```text
batch_id
publication_lineage_id
canonical_batch_key + key_algorithm/version
external_transaction_id
target / tenant / visibility_domain / ordered_surfaces
ordered_object_refs + ordered_object_digest
publish_decision / capability / enablement exact refs
local_e2e + runtime_conformance exact refs
policy/QC/rights exact refs
authority/schema/registry/kernel/runtime/adapter build hashes
created_at + creator principal attestation
```

canonical key 至少绑定 lineage、target/tenant、domain、ordered object digest、Policy
与 decision revision。相同 key 只能对应相同 canonical request；否则 conflict。

### 4.2 Lifecycle evidence

- `UploadIntent` / `UploadObservation`：每个 object 的 durable request identity、
  hash、expected private mode、adapter/API、fence 与 raw evidence ref。
- `PreparedObjectSet`：全部对象 exact platform IDs/hash/metadata 已验证，并证明
  所有 surface 仍为 zero-visible；缺一个对象就不能存在 complete Set。
- `ActivationIntent` / `ActivationObservation`：绑定 batch、prepared token、
  current credential lease、capability heads 与 fencing token。
- `SurfaceQueryEvidence`：每个 surface 的 authority query、transaction epoch、
  exact membership、visibility 与 raw response BlobRef。
- `BatchCommitResult`：validator 对完整 query closure 的规范结论，不采信 adapter
  的 success boolean。
- `CompensationIntent/Result`、`WithdrawalBatch` 与 `PublicationIncident`：独立、
  append-only、不可抹除。

### 4.3 `PublicationLedger`

Ledger 是 immutable revision chain，current head 由 exact ref + fencing CAS 推进：

```text
planned -> staging -> staged_verified -> activation_intent_committed
        -> reconciling -> committed | aborted | denied
                       -> atomicity_violation | quarantined_indeterminate
```

`atomicity_violation` 和 `quarantined_indeterminate` 不是可覆写临时错误。补偿、
withdrawal 或后来观察到全有/全无只追加新 evidence；历史 revision 保留。

## 5. Protocol

### 5.1 Admission and reservation

1. 从 authority Store 回读五类 prerequisite current heads 与 exact refs；验证签名、
   producer principal、contract/build identity、expiry/revocation 和依赖闭包。
2. 规范化 ordered objects 与 surfaces，派生 canonical key/transaction ID。
3. 在一个内部事务中竞争 publication lineage head，写 immutable Batch、planned
   Ledger、Command Receipt、lease/fence 和 outbox。CAS loser 回读现有 Batch；request
   不同则 conflict。
4. freeze 对应 Release heads，防止 Runtime switch、child Run 或第二 Coordinator
   规划竞争 batch。

Admission 任一步 unknown 都不调用平台。Runtime 只能提交 typed publication intent；
它无权传入 decision、platform token、object IDs 或内部 transaction state。

### 5.2 Private staging and verify

1. 每个上传先提交 durable UploadIntent，再用 staging-only credential 调用 adapter。
2. duplicate/timeout 使用同一 object request identity 查询或重放 certified idempotent
   request；禁止从本地 checkpoint 推断接受，也禁止改名再次上传。
3. 逐对象验证 platform ID、bytes/content digest、metadata、privacy/audience、
   target/tenant 和 transaction association。
4. 查询完整 surface inventory，要求 exact zero-visible；不可查询、提前索引、直链
   成功或缓存泄漏均 certification failure。
5. 只有 N 个对象全部验证后，才原子提交 `PreparedObjectSet` 与
   `staged_verified` Ledger revision。

partial upload、对象错配或 staging TTL 不足时写 deny/abort intent 并清理 hidden
objects。不得从成功子集重写 Batch，也不得 activate。

### 5.3 Activate and authoritative verification

1. activate 前重新验证 publish decision、capability、enablement 与 credential lease
   current heads；发现 stale/revoked/expired 且尚无 effect 时 abort。
2. 内部原子提交 `ActivationIntent`、request hash、prepared token 和 fence，之后才
   调用一次逻辑 `Platform.activate`。
3. 无论 transport 返回 success、timeout、disconnect 或 worker crash，后续只对原
   transaction reconcile。success response 也必须 query，不能直接 committed。
4. validator 收集完整同 epoch surface closure：
   - exact N visible、digest/membership/epoch 一致 → `committed`；
   - exact 0 且权威证明 transaction 未生效/已 abort → `aborted` 或保持 prepared；
   - 0 < visible < N、surface epoch/membership 不一致或 staged leak →
     `atomicity_violation`；
   - query 缺失、矛盾、超时或非权威 → `reconciling`。

只有 committed Result、完整 raw evidence closure 和内部 result transaction 成功后，
下游才可看到 batch committed。projection lag 不改变 authority。

### 5.4 Reconcile and compensate

- Reconciler 由 durable work item 驱动，每次先回读 Batch/Ledger/current fence；旧 owner
  只能附加 observation，不能推进 head。
- retry 使用 certified backoff、同 transaction/idempotency identity 与最大窗口。
  budget 用尽后转 quarantined，但 work item/alert/owner 不终止。
- pre-activation failure：abort transaction，删除/过期 hidden objects，并 query 证明
  zero-visible；unknown 保持 reconciling。
- post-activation mixed/unknown：先 revoke target capability / issue shadow-only
  enablement revision，再 withdraw/hide 全部 batch objects。补偿必须 query 全 surfaces。
- `RunOutcome` 在 committed、proven aborted/denied 或明确未闭合接管前不得 finalized。
  人工只能选择继续 query、withdraw、隔离 account，不能手工标 success。

## 6. Platform Port Without Assuming an API

共享 kernel 只定义语义 port；adapter 将具体 API 映射为这些语义：

```text
stage_private(batch, object, request_identity, fence) -> observation
query_staged(batch, complete_surface_inventory) -> authority_evidence
activate(batch, prepared_token, request_identity, fence) -> observation
query_visibility(batch, complete_surface_inventory) -> authority_evidence
abort_hidden(batch, request_identity, fence) -> observation
withdraw_visible(batch, request_identity, fence) -> observation
```

Port response 不含可被当作业务 authority 的 `allow/committed` boolean。adapter 保留
raw response bytes/hash、API/version、request identity 与 provider trace，但不得
持有 Release Authority signing key 或直接写 authority tables。

目标 API 无法无损映射任一语义时，capability certification deny。多 API calls
组成的逐对象 activate 不可映射为一个 atomic `activate`。

## 7. Credential Isolation

凭证由 secret broker/KMS 在 worker 执行时短期签发，不存入 Batch/Artifact/DB/log：

| Principal | Minimum capability | Forbidden |
|---|---|---|
| staging worker | create/query/delete private staged objects | public activate, signing allow |
| activation worker | activate exact prepared transaction | upload arbitrary object, issue decision |
| query/reconcile worker | authoritative read for exact tenant/domain | mutate visibility |
| compensation worker | abort/withdraw exact transaction objects | create unrelated content |
| Release Authority | sign/revoke publish decisions | platform credential, adapter execution |
| Pipeline/Agent Runtime | submit typed Command/query receipts | every platform/secret capability |

credential lease 绑定 target、tenant、transaction/batch、operation、expiry 与 workload
attestation。test 与 production 采用不同 account/key/policy。redaction test 对 structured
log、exception、raw evidence wrapper、trace、fixture 和 crash dump 做 secret scan。

## 8. Audit, Withdrawal and Rollback

- authority audit 保存 canonical request/response hashes、raw evidence BlobRefs、actor/
  workload attestation、credential key ID（不保存 secret）、adapter/API/build、fence、
  exact refs、DB Receipt 与 external transaction epoch。
- Capability、Enablement、Decision、Incident、Batch、Ledger、Compensation 与 Withdrawal
  都 append-only；projection 可重建且不能授权。
- kill switch 创建新 enablement revision；未写 activation intent 的 batch 停止并
  abort hidden。已有 intent 的 batch 只 reconcile/compensate，不能假定未生效。
- binary rollback 前检查 current schema/registry/capability/ledger；不兼容则保持
  服务关闭并由新版本 reconcile。
- committed 后撤回创建 `WithdrawalBatch`，不修改原 Result。只有 authority query
  证明全部声明 surfaces unavailable/zero-visible 才标 complete；不承诺平台域外副本。

## 9. Certification Strategy

认证分四层，前层不通过不能进入后层：

1. **Contract/validator**：closed schemas、canonical key、signature/revocation、state
   transitions、surface closure 与 wrong-ref negatives。
2. **PostgreSQL/concurrency**：same/different request races、CAS、lease expiry、double
   Coordinator、Runtime switch、intent/result crash points、outbox duplicates。
3. **Deterministic simulator**：对每个 upload、activate、query、abort/withdraw、
   response loss 与 DB failure 做 fault injection；不能签发真实 Capability。
4. **Real non-production target**：隔离 tenant/audience 与非敏感 corpus，验证 staged
   zero、activate 0→N、直链/list/search/feed/API/cache/CDN、duplicates/timeouts、
   adapter crash、credential revoke、withdraw 和 retention window。

真实认证生成 signed `PlatformCertificationReceipt`，绑定 dossier、完整 test vectors、
raw evidence refs、runner/toolchain/build hashes、target/API identity、batch bounds、
issued/expires/revocation 与 independent SupervisorDecision。截图、adapter 自报或缺
evidence closure 的报告均 fail。

一次 mixed visibility 即当前 capability revision 失败并触发 incident；不能通过重跑
覆盖。扩大 batch size、增加 surface、升级 API/adapter、改变 account/credential
policy 或超过 expiry 都要求新 revision 重新认证。

## 10. Compatibility and Rollout

- shared schema/state machine/coordinator 属于唯一 `autocut_kernel` source owner；
  Pipeline 与 Agent 只经过 Dispatcher，不能各自实现 publication flow。
- target adapter 位于 infrastructure/composition 边界；具体路径与 SDK 只在选择平台
  后写入 task allowlist。
- 当前只允许规划与 local simulator。满足 prerequisites 后先 hidden shadow，再做
  隔离 audience real activation certification，最后生成 shadow-only capability。
- 首次 enabled 是后续受保护 rollout step：fresh certification、fresh local E2E/
  runtime receipts、limited target/account/batch bounds、kill switch drill 与明确审批。

### 10.1 Context is evidence routing, not an oracle

`implement.jsonl` / `check.jsonl` 只告诉 Loader 哪些 spec/task/design 来源可能相关。
它们不包含 candidate、write allowlist、check result，也不能产生 `allow`。Task start
前必须生成机器可复算的 `TaskSnapshot.context_manifest`：

1. 对每个直接引用文件记录规范路径、byte length、UTF-8 解码、EOF closure 和
   full-file SHA-256；文件漂移使旧 Snapshot 失效。
2. 对每个实际注入片段记录稳定 heading/section anchor、精确 byte range、slice
   SHA-256；全文完整性校验不等于把全文注入 Agent。
3. 递归闭合片段引用的定义、前置不变量、错误矩阵、例外以及直接引用的
   Command/Rule/Artifact；实现者摘要不能替代 closure。
4. 同一 Snapshot 绑定 authority/context、base/candidate、repository predecessor、
   allowed-write-paths、gate bundle、toolchain lock 与 Supervisor contract hashes。
5. 缺稳定 anchor、Loader 截断、slice/ref closure 不完整或 closure 超上下文预算时
   `deny`；按 owner/AC 拆成 child task，各 child 维护独立 manifests/Snapshot，不能
   自动摘要或删掉错误规则后继续。

## 11. Open Design Inputs Before Start

- 候选平台是否为 class A 或严格 class B；否则以 capability deny 结束。
- authority query consistency、commit epoch semantics 与全 surface inventory。
- exact idempotency retention、staging TTL、reconcile window、withdrawal SLO。
- target SDK/transport、credential broker 与 KMS/key registry 接口。
- 首批 batch size、对象类型/metadata、隔离 audience 和认证 corpus。
- 前置 LocalRealE2EReceipt / RuntimeConformanceReceipt 的 schema 与 exact refs。
