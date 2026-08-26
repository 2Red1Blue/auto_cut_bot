# Stage 1 generation Command 与八成员 reader：最小集成点

2026-08-26，只读代码研究；仅新增本记录。未运行 PostgreSQL、provider、模型或服务。
不使用 inactive `semantic_chain/{authority,stage1,rules}.py` 或 fixture prototype 作为权威。
以下区分**已有 API**与**建议新增接口**；不是 Stage 1 完成或上线许可。

## 1. 结论与最小顺序

1. 先完成正在实现的独立 proof/KC evaluator 与八成员闭合 codec；现有六成员 compiler 不能充当 Admission。
2. 新增一个 Kernel `BuildNarrativeGraphCommand`：exact Store inputs → 持久化 generation request/Attempt → 单次 draft dispatch/reconcile → durable raw bytes 重解码 → compiler + 独立 evaluator → 八成员原子提交。
3. 新增 Stage 1 **exact-set reader**，同时服务成功 replay 与 Stage 2/3；共享已有 set/Blob/Attempt 核验，不复制一套 SQL 完整性实现。
4. 最后新增薄 Runtime stage adapter，冻结新的 execution profile/stage order，复用同一 Kernel Command。仍不把 Stage 1 成功当全 Pipeline 成功。

无需为 Stage 1 新建 provider-attempt 状态机，也不应把 VLM video request 伪装成 text draft request。

## 2. 可直接复用的 API

完整路径相对产品仓库；下文 `store/`、`semantic_chain/`、`vlm/` 及
`pipeline/generate_vlm_evidence_command.py` 简写均以
`packages/autocut-kernel/src/autocut_kernel/` 为前缀。Runtime 路径以
`auto_cut_bot/pipeline/` 为前缀。

### Exact Source/Window/VLM inputs

- `store/models.py:1099`：`CommittedSemanticInputsRequest(job: Job, source_manifest: CommittedArtifactMemberReference, vlm_semantic_pack_set: CommittedArtifactMemberReference)`。
- `store/postgres.py:3735`：`read_committed_semantic_inputs(request: CommittedSemanticInputsRequest) -> CommittedSemanticInputs`。
  该 reader 校验 Source owner、`semantic_analysis` grant、唯一整批 VLM owner、child Receipt/Set/ordinal、请求/响应 Blob、raw-response 重解码与 Source/Window identity；不是 caller 拼装 pack 列表，也不查询 ASR/VAD。
- `store/models.py:1197`：返回 Source manifest/grant、exact aggregate reference、aggregate policy，以及规范排序的 `CommittedVlmSemanticInput`；每个 child 保留 request identity、pack、response reference 和 raw response Blob。
- `semantic_chain/stage1_draft.py:338`：`stage1_draft_prompt_inputs(inputs, *, policy: Stage1DraftPolicy) -> dict[str, object]`，已有输入闭合、对象目录和 prompt byte budget。
- 同文件 `:438`：`decode_stage1_draft(raw: bytes, *, inputs, policy) -> Stage1Draft`；`:587`：`stage1_draft_response_schema(policy) -> dict[str, object]`。
  使用原始 bytes 重新解码，不把公开 `Stage1Draft` 构造器当能力凭证。
- `semantic_chain/coverage_compiler.py:319`：`compile_stage1_coverage(inputs, raw_draft, *, draft_policy, coverage_policy, scope, revision)` 返回六个 pending business members；后接新的 proof/evaluator，不能调用旧 prototype 补两个“通过”成员。

### 通用 durable generation 生命周期

`store/postgres.py` 已提供以下实际接口；`pipeline/generate_vlm_evidence_command.py:89` 的 `GenerationStore` Protocol 列出大部分签名：

| API | 位置 | Stage 1 用途 |
| --- | --- | --- |
| `claim_command(claim: CommandClaim) -> CommandOutcome` | `:1114` | `CommandClaim(job, key, command_name, request_hash, execution_kind="generation")`；kind 已显式化 |
| `put_immutable_blob(job, *, content, content_hash, media_type) -> BlobRef` | `:2111` | 保存请求与原始 draft bytes，并建立 Job claim |
| `read_immutable_blob(job, reference: BlobRef) -> bytes` | `:2193` | owner/hash/length/media 校验后读取 durable bytes |
| `reserve_generation_attempt(slot_id, request_hash, *, provider_id, provider_idempotency_key, request_payload, retry_policy_hash, max_attempts) -> GenerationAttempt` | `:2418` | 校验 slot kind/request hash、Job BlobClaim；首次 reservation 可重放 |
| `reserve_next_generation_attempt(previous_attempt_id, *, expected_version, provider_idempotency_key)` | `:2543` | 仅已确定 retryable failure + 冻结预算内创建下一 ordinal |
| `dispatch_generation_attempt(attempt_id, *, expected_version, provider_request_id=None)` | `:2649` | CAS 获取 dispatch lease，竞争失败不外呼 |
| `acquire_generation_reconcile_lease(attempt_id, *, expected_version)` | `:2703` | 重启/未知结果走 reconcile，而非 redispatch |
| `record_generation_provider_request_id(attempt_id, *, expected_version, provider_request_id, dispatch_lease_token)` | `:2770` | stream 收到 provider request ID 就持久化 |
| `record_generation_response(...)` / `reconcile_generation_response(...)` | `:2749` / `:2884` | 把 exact raw-response Blob 绑定到同 Attempt，含 version/lease/可选 provider request ID |
| `mark_generation_indeterminate(...)` | `:2832` | 未知外部结果不解释为安全重试 |
| `fail_generation_attempt(..., failure_code, failure_detail_json, failure_disposition, ...)` | `:2905` | 保存明确失败分类；raw response 不因 parser 拒绝而丢失 |
| `commit_generation_success(attempt_id, *, expected_version, success: CommandSuccess) -> GenerationAttempt` | `:2995` | 同事务写 business set/Receipt、Attempt committed 和整个 Attempt-chain Receipt binding |
| `commit_generation_rejection(attempt_id, *, expected_version, rejection: CommandRejection) -> CommandOutcome` | `:3053` | 最终 failed Attempt 绑定 denied/failed Receipt，无 business set |
| `read_generation_attempt(attempt_id)` | `:3108` | exact Attempt ID |
| `read_generation_attempt_for_slot(job, command_slot_id)` / `read_generation_attempt_chain(job, command_slot_id)` | `:3118` / `:3162` | exact Job/slot 下最新 Attempt 或完整有序链，不是跨 slot/provider latest |

`commit_command_success` 在 `:1721` 要求 deterministic kind；generation 成功/replay 必须走 generation API。无需追加新 Command-name 白名单才能使用 generation。

**请求 envelope 的现有硬约束**：`_generation_retry_backoff_seconds`（`:5897`）从 durable request Blob 顶层读取 `retry_policy` 和 `retry_policy_sha256`，并校验 `{backoff_seconds,max_attempts,strategy_version}`。新的 Stage 1 request payload 必须保留这两个键，不能只传独立 hash；`max_attempts` 当前限定 1–3。

## 3. 新 Command 真正需要补什么

### 请求与 provider seam

建议新增 `pipeline/build_narrative_graph_command.py`，请求仅含 exact input request、Job/idempotency、output scope/revision 和冻结 Stage 1 policies，不接收 caller-built `CommittedSemanticInputs`/draft 作为生产权威。

Stage 1 command request hash 至少覆盖：Job/profile、两个完整 predecessor refs、Store 返回的 input-binding hash、完整 provider request payload hash、provider/model/prompt/schema/parser identities、draft/coverage/dependency/compiler/evaluator policy identities、retry policy、output scope/revision。ID/hash 应用现有 canonical helper；不要把尚未产生的 raw-response hash 塞进首次 claim identity 形成时间循环。

`GenerateVlmEvidenceCommand.execute`（`pipeline/generate_vlm_evidence_command.py:403`）的状态分支可作为实现模式，但其 request/parse/三个 Artifact records 是 VLM 专属，不能继承后只换 parser。现有 `vlm/provider_port.py:36` 的 `ProviderDispatchRequest` 强制真实 `WindowProxyBlobRef + proxy_content`；Stage 1 只有语义 prompt，因此缺一个小的 text-draft dispatch DTO/port 与真实 provider adapter。可以复用现有结果分类、request-ID callback、reconcile query 和底层单次 HTTP transport；不得填假 proxy、重新上传视频、引入隐藏 SDK retry。

实际执行/重启状态分支：

- 无 Attempt：先存 request Blob，再 reserve；reserved：只有 CAS lease winner dispatch。
- dispatched/indeterminate：reconcile 同 provider request/idempotency identity；未知 timeout 不创建 successor。
- responded/reconciled：从 Store 读 raw Blob，再 decode/compile/evaluate，不重新调用模型。
- failed：仅已分类 retryable 且剩余预算允许 successor；repairable/nonretryable 不静默改 prompt 后重试。
- committed/succeeded：读**既有八成员 set**与 Attempt closure 返回原 refs，不以重新编译出的 pending DTO 代替 committed output。

### 写入与审核记录

Store 的 generation success 已能原子写任意非空 member tuple；不需要新建“Stage 1 attempt”表。但它不懂八成员类型/顺序、seven-business subject、KC 结果或 raw-draft payload 关联。新 Command 必须调用共享 Stage 1 闭合 verifier，并在 reader 复用相同**结构**校验；独立 KC evaluator 仍不能以重跑同 compiler 后 hash 相等作为唯一检查。

新增八成员 shape 固定为 Cards、Digests、Graph、EvidenceDiagnostics、ConflictDiagnostics、Ledger、DependencyClosureProof、唯一 CoverageAdmission（最终 ordinal 常量由新 owner 统一定义，不能散落魔数）。拒绝时不提交前六成员的部分成功。

当前真实审计载体是 `runtime.generation_attempts`、`storage.blob_objects/blob_claims` 和 `runtime.generation_receipt_attempts`。当前可执行 generation 路径没有通用 `GenerationInvocation` Artifact API。不要把 Attempt UUID 包装成不存在的 ArtifactRef，也不要给固定八业务成员追加第九个假审计成员。若后续契约确需独立审计 Artifact，必须另行定义真实 durable owner；当前研究不凭名字宣称它已经存在。

## 4. raw draft 与 audited Attempt 的 exact binding

1. 在 provider 调用前，将完整 canonical request bytes 存入该 Job 的 immutable Blob；Attempt 保存它的 exact BlobRef、slot request hash、provider/key、retry identity。
2. 完成响应后先写原始 bytes Blob，再调用 response/reconcile transition；只有成功绑定到该 Attempt 的 bytes 才能送入 Stage 1 decoder。不能直接消费 adapter 返回的内存 dict。
3. 类似 VLM `_assert_attempt_identity`（`generate_vlm_evidence_command.py:770`）逐项核对 slot、request hash、provider、ordinal 派生 provider key、request Blob hash、retry hash/budget；同时验证 exact Job owner。
4. `raw_draft_sha256 = SHA256(attempt.raw_response bytes)`；`canonical_draft_sha256 = decoded Stage1Draft.canonical_hash`。二者可以不同，Diagnostics 已分别存储；input-binding hash 绑定 predecessor closure，不能冒称 raw-byte hash。
5. 成功 reader 必须证明最终 committed Attempt 的 `receipt_id/artifact_set_id/command_slot_id` 指向当前八成员 set，request Blob 闭合 frozen request/inputs/policies，raw Blob hash 等于两个 Diagnostics 声明值，重解码后 canonical hash 一致；再核所有 cross-member refs/subject/proof/Admission 结构。
6. malformed raw、语义拒绝或 evaluator 不通过：保留 raw Blob、Attempt failure 和完整 chain 的 final Receipt；不重写 raw、不制造 empty accepted business set。`GenerateVlmEvidenceCommand._parse_and_commit`（`:590`）及 `_commit_terminal_failure`（`:687`）已有相近审计分支可参考。

## 5. 八成员 reader：确实缺的入口，不重复 generic member reader

已有 `read_committed_artifact_member(reference)`（`store/postgres.py:3569`）要求 caller 已知完整 Receipt/Set/ordinal/scope/type/logical/revision/hash，仅返回一个 member；它不证明固定八成员、producer kind、Attempt/raw audit 或 Stage 1 Admission。

已有私有 `_read_exact_committed_set(cursor, job, reference)`（`:5331`）会检查 Job/profile、succeeded slot/Receipt、Set member count、连续 ordinals、每个 payload hash、set hash、exact requested member。`_member_matches_reference`（`:5224`）可复用。该 helper 没有 Stage 1 producer/Attempt 语义，也未返回 slot request hash/kind。

**不能遗漏 replay 启动问题**：`CommandOutcome`（`store/models.py:1492`）已有 slot/Receipt/ArtifactSet IDs，但没有 Admission content hash。无法用这三个 ID 猜一个 full member reference 调 generic reader。

建议新增一个 exact-set 入口（名称待实现统一），例如：

```python
read_committed_narrative_graph(
    job: Job,
    *,
    command_slot_id: UUID,
    receipt_id: UUID,
    artifact_set_id: UUID,
    expected_request_hash: str,
) -> PersistedNarrativeGraphSet
```

它只解析指定 Job/slot/Receipt/Set，enumerate 八成员后从 DB 构造 full refs，再严格检查闭合；不按 head、最大 revision、hash猜UUID或其他成功 set 补成员。可将 `_read_exact_committed_set` 的共同 set 核验抽成内部基础函数，保留现有 full-anchor wrapper，避免重复约 130 行 SQL/校验。

新返回值应包含 typed 八成员内容、exact ordered committed refs、producer slot/Receipt/Set 和已核验的 Attempt/request/raw provenance。它不是 `SemanticMemberIdentity` 的“升级构造器”：同 set 的 identity→object resolution 必须真的解析 owner payload，特别是 Event 只能 canonicalize 到 Card owner。

除共享 set 验证外，新增 reader 必须检查：精确 producer Command 名及 `execution_kind=generation`、request hash、同 Job 的唯一 committed final Attempt、所有失败前序 Attempt 的同一 terminal Receipt binding、claimed request/raw Blobs、固定八成员 scope/revision/type/logical IDs、七业务 subject 不含 Admission 自身、输入/策略/raw hash 闭合及独立评估结果完整性。普通 Pipeline Job 在此仍可 running，不套用 calibration authority Job 的 terminalization 例外。

## 6. Runtime 最小缺口

- `auto_cut_bot/pipeline/runtime/composition.py:448` 当前 execute/reconcile 只注册 `source_prep/vlm/media_preflight`；没有真实 Stage 1 port。
- `runtime/models.py:189` 的 `_FAIL_CLOSED_BOOTSTRAP_STAGES` 和 `PipelineExecutionProfile`（`:193`）只冻结现行 VLM/media 配置，缺 Stage 1 generation/draft/compiler/coverage/dependency/evaluator policies。必须用新 profile revision 明确冻结，历史 run 不能读取当前环境默认值继续执行。
- 新 stage adapter 可参考 `runtime/vlm_stage.py:151` 的 exact context→Job、`:167` 的 persisted profile 重建、`tests/pipeline/test_pipeline_vlm_stage.py` 的 restart/reconcile 测试模式，但语义输入只走 Kernel exact reader。
- `runtime/stages.py` 已有通用 ordered runner/reconciler 和 lease heartbeat，无需增加第二 scheduler。
- `runtime/postgres.py:53` 的 `_PIPELINE_SUCCESS_TERMINAL_STAGE = None` 是有意 fail-closed 占位；Stage 1 接入不等于 Stage 2/3/4/Render/QC 完成，不能顺手设为 Stage 1。
- Stage order 变更需与任务设计一起冻结；不能因为当前 bootstrap 将 media_preflight 放在 VLM 后，就让 Stage 1 读取 timed evidence。Pipeline/Agent-Native 都调用同一新 Kernel Command。

## 7. 可复用测试与新增必要负例

本轮只读，没有执行以下 DB 测试；其中 fixture 会创建/重置 schema，不能在任意已配置数据库上直接运行。

- `tests/store/test_command_execution_kind_lifecycle.py:86–187`：fake cursor 覆盖任意 generation Command claim/reserve/commit/replay、kind drift、generic deterministic API 绕过；不用为 Stage 1 再证明名字白名单。
- `tests/store/test_command_execution_kind_postgres.py:100,197,262`：任意 generation Command 的拒绝/成功 Attempt-chain 及 populated migration；可复用隔离测试方法。
- `tests/pipeline/test_generate_vlm_evidence_command_postgres.py:409,562,588,618`：成功一次/replay 零 provider、未知响应 reconcile、request-ID callback 后崩溃及 fallback。
- 同文件 `:650,674,729,748,771`：parse rejection/terminal failure 保留审计、无业务 set、generic rejection 不能替代 generation rejection。
- 同文件 `:790,832,872,914,940,962`：503/429 重试链、预算耗尽、durable backoff、不可重试/repairable、active lease、并发 successor CAS。
- `tests/store/test_semantic_committed_readers_postgres.py:906,1057,1266,1400,1440,1518`：exact 输入、授权先于 VLM 查询、raw/persisted 自洽伪造拒绝、continuity 差异保留、Blob/member owner tamper、head 前进后仍读旧 exact revision。
- `tests/pipeline/test_pipeline_vlm_stage.py:987,1007,1076,1120,1144`：非终态 predecessor 不 dispatch、grant fail-closed、批 Receipt 到控制平面之间崩溃后 reconcile、不重复 generation。

Stage 1 必须另补：真实新 compiler/evaluator 成功产生八成员；任意一个成员遗漏/换 scope/revision/hash/Receipt/Set；Graph Event alias；Audit Attempt/raw/request Blob 换绑；raw/canonical draft hash 混淆；replay 不重新采样；responded 后重启仅重新解析持久化 bytes；失败仍可读 raw 与完整 chain；KC 拒绝不落前六成员；同 key 改输入/策略/request bytes 必须冲突；success 后精确 reader 不能返回重新编译但未提交的 refs。测试的合成输入只作 oracle，不宣称真实模型/数据库验收。
