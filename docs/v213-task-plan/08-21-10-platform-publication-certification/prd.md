# Platform Publication Certification

## Goal

在一个明确选择的非生产平台租户与一个封闭的 atomic visibility domain
内，证明一批已经通过本地真实 E2E、四层 QC 和双 Runtime conformance 的
内容只能以 `0 → N` 整批方式对外可见；若任何前置凭证、平台能力、外部
结果或恢复结论不完整，则无人值守发布保持关闭。

本任务是未来的平台认证与发布事务任务，不是当前启用许可。任务仍处于
`planning`；当前没有选定平台、认证租户或可复算的生产级本地 E2E / 双
Runtime Receipt，因此不得实现真实发布副作用，也不得生成
`PublicationEnablement(mode=enabled)`。

## Confirmed Constraints

- PostgreSQL 事务只能原子提交内部 intent、Artifact、Receipt、Ledger 与
  CAS head；它不能与外部平台组成一个 ACID 事务，也不能回滚已经发生的
  外部可见性。
- `all_or_nothing` 的对象是一个经认证 visibility domain 内的全部对象和
  全部平台控制 surface。快速逐条上传、定时同时发布、最终全部成功或事后
  删除失败对象都不构成原子可见。
- 平台内容若可绕过受控 manifest/pointer 通过直链、列表、搜索、feed、API、
  CDN 或其他公开 surface 到达，则受控 pointer 不能证明整批原子。
- 任务目录状态、日志、HTTP 2xx、webhook、上传完成、QC 文件、人工截图或
  adapter 自报 success 均不是发布许可或 committed 证明。
- 外部可见后不存在真正的历史回滚。withdraw/compensate 只能减少后续可见性，
  必须保留曾经的可见性与违规审计事实。
- `ac_auto_cut/` 只提供原理与实现设计文档，不是本任务的产品代码仓、adapter
  owner、Store owner 或可写 scope。任何产品代码只能进入 `auto_cut_bot` Git
  repository 内的 `autocut_kernel` 与受控 composition/adapter 边界。
- `implement.jsonl` 与 `check.jsonl` 只列出规划/规范来源；文件被列入不表示
  admission、pass、写权限或完整上下文。实现前必须另行生成 hash/EOF/slice/ref-
  closure 闭合的 `TaskSnapshot`。

## Entry Prerequisites

以下条件是实现和真实平台测试的 hard prerequisites；任何一项缺失都必须
保持 `planning/blocked` 和 `unattended publication = deny`：

1. **本地真实 E2E Receipt**：使用受登记真实媒体、真实 PostgreSQL、真实
   FFmpeg/ffprobe 和正式 Store/Command 边界，从 Root Input 到 Stage 5、
   四层 QC、Portfolio Release 完整跑通；Receipt 必须绑定 authority、Schema、
   Registry、Policy、kernel/runtime build、输入与输出 ArtifactSet hashes。
   fixture-only、mock Store、仅 persistence slice 或仅本地文件存在不满足。
2. **双 Runtime Conformance Receipt**：Pipeline 与 Agent-Native 使用相同
   authority/kernel/Policy 和等价 committed input，比较 Gateway decision、
   Receipt/Admission/Release refs、business Artifact hashes、恢复记账、
   Recipe/QC 与 local-release milestone；缺任一侧、只有日志、Runtime 版本
   不同或 trace 未闭合均不满足。该 Receipt 本身不产生 publication fact 或权限；
   两个 Runtime 对 publication intent 的等价性与唯一 Batch identity 由本任务在
   无外部副作用的共享 Coordinator/simulator 中另行证明。
3. **独立 `publish_decision=allow`**：由隔离的 Release Authority 从 committed
   QC、授权、敏感内容、目标 Policy 与完整 object digest 独立计算并签发，
   不能由 Runtime、Coordinator、adapter、平台响应或调用者布尔参数生成。
4. **平台能力 dossier**：选定非生产 tenant/account、adapter/API version、
   visibility domain、全部 surface、对象/批次上限、staging/activation/query/
   withdrawal 语义、幂等保留期与故障注入环境均已冻结。
5. **凭证与运维基础**：短期 workload identity、staging/activation/query/
   compensate 最小权限分离、secret manager/KMS、审计保留、kill switch、告警
   与人工事件响应 owner 已就绪；任何秘密不得进入 Artifact、日志或任务文件。
6. **仓库与上下文冻结**：产品 repository 固定为 `auto_cut_bot`；每个实现 wave
   具有 repo-relative exact allowlist，平台 adapter 的唯一目录只在选定平台后写入
   TaskSnapshot。Loader 对全部直接来源闭合 full-file hash/EOF，Planner 对实际注入
   章节闭合 section/range/slice hash 与引用闭包，并绑定 gate/toolchain/Supervisor
   contract hashes。任何截断、自动摘要替代或闭包超预算均先拆 child，不得 start。

前置 task 的 `completed` 字段不等于上述 Receipt。进入实现时必须回读并验证
exact refs、签名、hash、过期时间与 revocation head。

## Requirements

### R1. 原子可见性与平台能力门禁

- 一个 `PublicationBatch` 只能覆盖一个 target、tenant、visibility domain、
  policy revision 和确定的 ordered object set。
- 合格平台必须提供以下二者之一：
  - 原生 private/staged prepare + 单一逻辑 atomic activate + authority query；
  - 所有外部访问都强制经过同一不可绕过的受控 manifest/pointer，而 staged
    objects 永远私有，且 pointer 可单次原子切换。
- 平台必须能枚举并权威查询 domain 内全部 surface，稳定关联同一个
  transaction/commit epoch，并支持使用同一 idempotency identity 恢复未知结果。
- 平台幂等记录、private staging 和查询证据的保留期必须覆盖最大 reconcile
  window；必须具备可验证的 abort/delete-hidden 与 withdraw/hide-visible 能力。
- 只支持逐对象 publish、surface 不可枚举、查询非权威、直链可绕过、缓存/
  索引独立暴露、重复 activate 语义不明或无法撤回的平台，不允许 unattended
  `all_or_nothing`。不得静默降级为“尽力而为”或 `independent_outputs`。

### R2. 独立且不可伪造的发布许可

- `publish_decision=allow` 必须是 closed-schema、内容寻址、签名/attestation
  保护的 immutable Artifact；签名覆盖 decision、ordered object digest、target/
  domain、Policy/QC/rights refs、authority/schema/registry/build hashes、issued/
  expires/revocation epoch。
- Release Authority 的签发 principal/key 与 publication Coordinator、平台凭证、
  Runtime 和 operator 分离；Publisher 只有读取/验证权，不能写、替换或自行
  注册 allow Artifact。
- `PlatformPublicationCapability`、`PublicationEnablement` 与
  `publish_decision=allow` 是三个独立条件。一个不能推导、代替或续期另一个。
- prepare 前与 activate 前均须重新验证三个 current heads、签名、exact refs、
  versions、expiry 和 revocation。任何 unknown/mismatch/stale 均 deny；若已经
  写入 activate intent，只能进入 reconcile，不能猜测 abort 或重发新事务。

### R3. Immutable batch 与 staged/private protocol

- `PublicationBatch` 冻结 lineage、canonical idempotency key、transaction ID、
  ordered objects/digest、target/domain/surfaces、decision/capability/enablement
  exact refs 及全部 build/contract hashes，创建后不可修改。
- 生命周期不写回 Batch，而由 append-only intent/result evidence、
  `PreparedObjectSet` 与 immutable `PublicationLedger` revisions 表示；current
  head 通过 expected exact ref + fencing token CAS 推进。
- 协议顺序固定为：durable plan/reservation → private staged upload intents →
  upload → 完整性与 zero-visible 验证 → immutable PreparedObjectSet → durable
  activation intent → 单一逻辑 activate → authority query 全部 surfaces →
  committed 或 reconcile/compensate。
- 未完成全部对象上传、任一对象 hash/metadata/隐私验证失败、任一 staged object
  提前可见或 surface coverage 不完整时，activate 调用次数必须为 0。
- 外部 request/response 只作为原始证据保存；规范状态必须由共享 validator
  根据冻结 contract 与 authority query 生成，adapter 不直接决定业务状态。

### R4. 幂等、并发与恢复

- canonical batch key 按 publication lineage + target/tenant + visibility domain +
  ordered object digest + policy/decision revision 派生。同 key/same request 返回
  原事务；同 key/different digest 或 refs 为 conflict，不能创建第二事务。
- 所有父/子 Run、Runtime switch、重复消息和多个 Coordinator 竞争同一个
  lineage head/lease/fence。任何旧 fence 的外部调用结果不得推进 current state。
- external call 前必须先 durable intent；call 后 response 丢失、DB commit 失败、
  timeout、worker crash 或网络 unknown 均使用原 transaction/request ID 进行
  authority query，不能用新 key 盲重试。
- reconcile 预算耗尽可进入 `quarantined_indeterminate`，但 durable work item、
  owner 和告警必须保留，RunOutcome 不得 final；人工操作不能把 unknown 改为
  committed。

### R5. Fail-closed 与补偿

- partial upload 只能清理/abort hidden objects，Batch 保持 denied/reconciling；
  不得把已上传对象当成缩小后的新 Batch。
- activate timeout/unknown 必须先查询同一 transaction 的全部 surfaces；只有
  certified idempotency contract 允许使用同一 identity 重放同一逻辑 activate，
  永远不能创建新 activation identity。
- mixed/partial visibility 立即产生 `atomicity_violation`，撤销 capability current
  revision、全局关闭该 target 的真实发布、持久化 incident 并启动 withdraw/hide
  compensation。即使补偿后变成全无或后续变成全有，历史违规不得改写为成功。
- compensation 是 append-only 新操作：pre-activate 用 abort/delete-hidden；
  post-activate 用 withdraw/hide-visible，并权威查询所有声明 surface。失败或
  unknown 保持 open incident，不得丢弃。
- 平台不满足 R1 hard prerequisites 时，shadow 可以验证 port contract 和 hidden
  upload，但 unattended publication、真实 activation 与 allow enablement 必须 deny。

### R6. Credential isolation 与审计

- Pipeline/Agent Runtime、LLM tool/prompt 和业务 caller 永不持有平台 credential
  或 publication principal；仅隔离的 adapter worker 可按阶段领取短期、目标绑定、
  最小权限 credential。
- test/staging/production tenant、service identity、KMS key 和审计 stream 物理或
  策略隔离；生产 secret 不可用于认证 fixture。
- 每个状态转换保存 actor/workload identity、request hash、adapter/API/build、
  transaction/idempotency identity、fence、exact input/output refs、authority
  response BlobRef、query timestamp/epoch 与 redaction proof。
- 审计记录、签名验证、capability/enablement revision、incident、补偿和撤回证据
  采用不可变保留；projection/dashboard 只读且不能授权。

### R7. 回滚、撤回与启用边界

- 部署 kill switch 通过新的 `PublicationEnablement(shadow_only|revoked)` revision
  立即阻止尚未 activate 的事务；已有 activation intent 的事务继续 reconcile。
- binary 回滚只有在旧版本支持 current contract/schema/registry/capability 时
  允许；数据库与审计历史 forward-only，不删除已提交证据。
- 已 committed batch 的撤回是独立 `WithdrawalBatch`/ledger chain，验证声明
  surfaces 的 zero-visible/unavailable；它不删除原 PublicationBatch，也不承诺
  消除平台控制域外已复制内容。
- 本任务认证通过最多产生 target/account-scoped、短期、可撤销的 capability
  与 shadow-only enablement。首次 `mode=enabled` 必须在全部 Receipt 新鲜且用户/
  operator 对最终认证摘要做独立批准后由受保护流程签发，不能由测试自动升级。

## Acceptance Criteria

- [ ] AC1：缺本地真实 E2E Receipt、缺双 Runtime Conformance Receipt、缺签名
  publish allow、缺 capability 或缺 active enablement 的任意组合，在 external
  call 前 fail closed；任务状态或日志不能替代凭证。
- [ ] AC2：PublicationBatch 与 canonical key 不可变；same-key replay 返回同一
  事务，different-digest conflict，父/子 Run、双 Runtime 和双 Coordinator 最多
  产生一个逻辑 activation。
- [ ] AC3：N 个对象全部 private staged 且 exact hash/metadata/zero-visible 验证
  通过前 activation count 为 0；partial upload、提前可见与未知 surface 均 deny。
- [ ] AC4：合格认证目标在每个声明 surface 上只能观测同一 commit epoch 的
  `0` 或 exact `N`；direct URL、list、search、feed、API、cache/CDN 不能绕过。
- [ ] AC5：外部调用前后每个 crash point、timeout、response loss、DB failure 与
  duplicate/reordered request 都使用原 identity reconcile，不产生第二批次、
  第二对象或未经证明的 committed/aborted。
- [ ] AC6：任何 mixed visibility 产生不可抹除 `atomicity_violation`、capability
  revocation、全局 target kill switch、incident 与持续 compensation；后续全有/
  全无也不把历史结论改成 pass。
- [ ] AC7：平台只支持逐对象 publish、非权威 query、不可枚举 surface、可绕过
  staged object 或幂等保留期不足时，认证结论为 ineligible，unattended publication
  保持关闭且不降级为 independent outputs。
- [ ] AC8：伪造/重放/过期/撤销/错 target 或错 digest 的 `publish_decision=allow`、
  capability、enablement、signature 或 key revision 均在 prepare/activate 前拒绝；
  Coordinator/adapter/Runtime 无法签发或写入 allow。
- [ ] AC9：凭证 scope、tenant 与阶段隔离测试通过；日志、Blob、Receipt、异常和
  test fixtures 均无 secret/token，Agent prompt/tool 无法取得 publication principal。
- [ ] AC10：pre-activate abort、hidden cleanup、post-commit withdrawal、kill switch、
  binary rollback compatibility 与 compensation failure/unknown 均有可复算审计和
  real non-production target test。
- [ ] AC11：认证包包含平台 capability dossier、surface inventory、真实 fault
  matrix、raw evidence refs、测试工具/build hashes、issued/expires/revocation、
  独立 SupervisorDecision；截图或 free-text 报告不能成为 pass。
- [ ] AC12：当前规划评审完成后仍保持 external publication disabled；只有后续
  新鲜 prerequisites、选定平台、真实认证和独立启用批准全部完成，才可能签发
  target-scoped `PublicationEnablement(mode=enabled)`。
- [ ] AC13：所有产品 diff 只存在于 `auto_cut_bot` repository 的当前 wave exact
  allowlist；`ac_auto_cut/**` 零写入，Store/coordinator/schema/adapter/authority/
  integration owner 不重叠，repair 只返回唯一 owner。
- [ ] AC14：JSONL 来源不能单独满足 admission/check；TaskSnapshot 对每个直接文件
  记录 byte length、UTF-8/EOF、full SHA-256，对每个实际 slice 记录稳定 section、
  byte range、slice hash 与引用闭包，并绑定 authority/context/gate/toolchain/
  Supervisor hashes。缺失、漂移、截断或超预算均 deny/split child。

## Out of Scope

- 默认选择某个外部平台、API、SDK、认证方式或 production account。
- 为不支持 batch atomic visibility 的平台伪造通用事务层。
- 把 `independent_outputs`、人工逐条发布或“最终一致”作为本任务的降级成功。
- 用本地 atomic file pointer、数据库事务、HTTP 成功码、webhook 或 dashboard
  代替外部 surface 原子性证明。
- 删除第三方已复制内容、撤销终端用户已看到的事实，或承诺平台控制域外的
  全球互联网原子性。
- 在本规划任务中修改产品代码、调用真实平台、创建 credential 或启用发布。
- 修改 `ac_auto_cut` 产品代码、在其中新增平台 adapter/Store/Coordinator，或把
  设计文档引用解释为跨仓代码写入许可。

## Deferred Variables (Non-blocking for Design, Blocking for Start)

- 首个候选平台、非生产 tenant/account、adapter/API versions 与测试配额。
- 经平台能力调查后冻结的 visibility domain 和完整 surface inventory。
- 平台 batch/object limits、private staging TTL、authority query consistency、
  idempotency retention、最大 reconcile window 与 withdrawal SLO。
- secret broker/KMS/workload identity 方案及 staging/activation/query/compensation
  精确 scopes。
- 本地真实 E2E Receipt 与双 Runtime Conformance Receipt 的 exact refs/hashes。
- capability/enablement 有效期、首次 enabled 的审批主体与 limited rollout 上限。
