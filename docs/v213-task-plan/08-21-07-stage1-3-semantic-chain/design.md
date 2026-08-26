# Design

Current Stage 2 decisions and implementation ownership are frozen in
[Stage 2 production wave](stage2-production-wave.md). Its v3 candidate projection,
acyclic member IDs, conservative coarse duration and exact joint assignment
correct the older enrichment/scoring examples; physical safety remains Stage 4.

The audited request/Command and exact reader are detailed in
[Stage 2 durable Command wave](stage2-command-wave.md). Text-generation retry,
lease, reconcile and causal Receipt handling have one shared owner used by both
Stage 1 and Stage 2; business compilation and Admission stay stage-specific.

## 实现策略：选择性重写，不继续补丁

当前 Stage 1–3 prototype 没有真实 Runtime consumer，Stage 2/3 的 production
成功路径又被显式关闭。继续逐个打开 evaluator 会同时保留 synthetic pass、fixture
authority 和新的 committed authority，后续每接一个 Runtime 或 Stage 4 都要重复补
join。因此冻结以下边界：

- 保留：PostgreSQL Command/Receipt/ArtifactSet/Blob/CAS/replay、exact committed
  readers、Window/整数时间映射、Doubao Ark 流式 attempt/reconcile、canonical
  value 与 Stage 4 exact-span 原语。
- 整体替换：VLM coarse observation v2、Source 隐式授权、Stage 1–3 prototype
  的 authority/evaluator/public façade。
- 迁移后删除：旧 fixture semantic command、`vlm_semantic_adapter`、旧 v2 typed
  reader 和生产包内的 fixture resolver/beat/PTS 路径。

这不是全仓重写。纯值对象和局部确定性算法可以迁移，但不得通过兼容 adapter
把 caller-built DTO 伪装成 committed authority。

## VLM Semantic Pack v3

Provider 只输出闭合的局部语义包：window summary/continuity、local entities、
visible facts、由 fact refs 闭合的 events、以及可为空的 highlight/hook hypotheses
与 semantic measurements。所有 support 使用整数 proxy PTS、保守 uncertainty 和
allow-listed frame IDs。

Kernel 独占 Source interval 映射、global IDs、core owner、request/manifest identity、
raw response hash 和 Artifact refs。出现 Source/Artifact/物理 endpoint 字段即拒绝。

Stage 1 只消费 entity/fact/event/continuity；Stage 2 只消费 committed candidate
hypothesis/measurements；Stage 3 只消费 admitted Stage 1/2 refs；Stage 4 独占
ASR/VAD/frame/sample/subtitle evidence 和物理 endpoint。

Prompt、Schema、Parser、Decoder、Artifact type、Store reader、batch finalizer 和
tests 同一波切到 v3。旧 v2 Artifact 只读审计，不 oneOf、不双写、不 backfill。

## Source 操作授权

Source preparation 必须提交：`authorization_id`、
`authorization_policy_sha256`、`series_id`、canonical `authorized_purposes`
和现有 source ID/hash 集合。首版只注册：

- `semantic_analysis`：Stage 1 可读取该 source/window/VLM；
- `render_source`：Stage 2 可选择，Stage 4 必须再次从 Store 独立复验。

Candidate 只能携带对 committed grant 的 witness，不能成为永久 capability token。
外部 publication authorization 不属于本任务。

Source/Window 的 canonical payload decoder 属于 Kernel authority。Store、Pipeline
Runtime 与 Agent Runtime 只能调用这一份 decoder；Kernel 禁止反向 import
`auto_cut_bot` 来复用应用层 source-prep helper。发现应用层已有重复 decoder 时，
同一迁移波把实现搬到 Kernel 并让所有调用方切换，禁止复制或 import-firewall 白名单。

## 新模块边界

```text
semantic_chain/
  authority.py
  rules.py
  stage1.py
  stage2.py
  stage3.py

pipeline/
  build_narrative_graph_command.py
  compile_story_portfolio_command.py
  build_editorial_blueprint_command.py
```

Compiler 是 `typed committed authority + audited draft + frozen policy ->
business members + admission decision` 的纯函数。Command 负责 claim、generation
audit、exact read、atomic commit 和 replay。

Rule 默认 `indeterminate`，只有实际完成对应读取和检查的 evaluator 才能置 pass；
禁止 `computed_rule_results` 一类“未失败即 pass”的 helper。Stage 3 多 Story member
按 `(artifact_type, logical_id)` 唯一，只有一个 batch Admission，内含 frozen target
order 的 per-story rows。

## 防补丁门禁

每个 finding 先归类为契约缺口、domain-model 缺口、实现缺陷或 test/fixture 缺口。
同一根因影响两个以上模块时，不允许改 consumer 局部绕过，必须修改 owner
contract/model、迁移全部 consumer，并增加跨层回归测试。临时 shim 最多只能
用于尚未切换的非权威公开 API；本次 v2→v3 语义迁移因从未产生可执行生产数据，
禁止 re-export、旧 request 转换、双写、mint authority 或调用旧 builder。

```text
Committed Source/Window/VLM semantic packs
  → BuildNarrativeGraph (draft → deterministic compiler → coverage admission)
  → CompileStoryPortfolio (committed candidate projection + proposal draft → local feasibility compiler)
  → BuildEditorialBlueprint per frozen Story (context closure → semantic feasibility admission)
```

生成模型只扩大候选空间，不能提交权威业务 Artifact。每个 Command 先保存 GenerationInvocation/原始 response Blob，再由共享 parser/compiler 校验封闭 Schema、owner refs、hashes、coverage 和 policy。业务 ArtifactSet 只有在独立 evaluator 全部 pass 后一次提交；draft 被拒绝仍保留审计记录，但不可被下游消费。

## Stage 1 开工前的 owner correction

Stage 1 不得直接挂在当前 consumer 后面。实现业务模型前必须在同一根修波闭合以下 owner；这些是替换，不是兼容层：

1. 把 VLM 整批成功 Artifact 固定为唯一的 `vlm_semantic_pack_set`。它必须列出有序的 exact child Receipt/ArtifactSet/member identity，并绑定共同的 Source provenance、prompt/schema/model/parse policy；Stage 1 不接受调用方自行拼装的一组 child refs。
2. `CommittedStage1Inputs` 必须返回 Store 解码并验证过的 `SourceOperationGrant`，以及按 `(episode_index, stream_index, core_start_pts, core_end_pts, window_manifest_sha256)` 排序的 Semantic Pack；Command 不重新解析 Source JSON。
3. Store 只证明 pack/window/blob/owner 完整性。跨窗口 continuity 不一致属于 Stage 1 diagnostics，不得由 Store 以字符串相等检查当成输入损坏。
4. Command 持久化以封闭 `execution_kind=deterministic|generation` 驱动约束，禁止在 Python/SQL 中继续追加具体 Command 名白名单。`BuildNarrativeGraph` 的 draft request、raw response、Attempt 和 Receipt 复用同一通用 generation 状态机。
5. 删除 `computed_rule_results` 和 public pending/admission 自证路径。每条 Rule 初始 `indeterminate`，只有完成对应读取与复算后才产生 pass/fail。
6. 提交前成员身份使用 `{artifact_type, logical_id, revision, scope, content_hash}`；数据库生成的 `artifact_id` 只在提交后进入 committed reference。前七个业务成员形成 `business_subject_hash`，Admission 不参与自身 subject hash。
7. Coverage universe 首版固定为 `fact|event|source_window|obligation`。每个 committed VLM Fact/Event、每个 required Source Window 和每个 compiler 生成的 obligation 恰有一行。
8. `TaintSeed` 由 CoverageLedger 内联拥有，Admission 只引用 Ledger-owned seed；Conflict competing claims 内联拥有并用 local IDs 引用，禁止 Admission/Diagnostics 通过 content-hash DomainRef 自指形成哈希循环。
9. 当前 partial contract-contribution 资产仍标记 `stage_01_ready=false`，只能作为历史审计输入。新的 production owner 完整交付 8 个封闭 wire models、tests 和 committed reader 后，旧 partial pack 必须删除或明确迁出 executable registry，禁止两套 Stage 1 authority 并存。
10. Runtime 以新的 profile revision 冻结 Stage 1 generation/compiler/coverage/dependency policy；阶段顺序为 `source_prep → vlm → build_narrative_graph`，Stage 1 完成后仍 fail closed，直到 Stage 2/3/4/Render/QC 全部接入。

Stage 1 需要独立、受审计的跨窗口 draft，但不建立第二套媒体语义权威。VLM Semantic Pack 仍是唯一媒体语义来源；Stage 1 draft 只提出 EpisodeDigest、跨窗口 identity resolution、Graph fragment、Beat/obligation/story-thread 候选，compiler 对无法证明的 merge 保留不同 Node 并生成 `possible_duplicate` conflict。EventCard 尽量从 committed VLM Event 确定性投影，不让模型重述事实。

首个真实 vertical slice 只选择一个 test/shadow Story，但仍使用正式的集合、ledger、Admission 与 target freeze 语义。不得把当前 `FixtureCandidateRegistry`、`FixtureBeatResolver` 或测试 PTS catalog 包装成生产 adapter；它们只保留为 oracle fixture。

Stage 3 输出的是叙事职责和 evidence requirements，不包含 start/end PTS。Stage 4 通过 CandidateCatalog + timed evidence 解析可行 A/V endpoint 并 canonical select。

Candidate compiler 将 committed candidate hypothesis 投影为规范的非空
`editing_modes ⊆ {dialogue, action}` 并绑定 semantic support hash；二者同时出现时保持
非互斥集合，不在 Stage 2 决定物理边界。该字段不接受 Transcript/VAD 或调用方覆盖。

## 正式输入读取与 join

生产路径新增 typed committed readers，不能把 caller dict 或已有 fixture
resolver 当作数据权威：

- VLM reader 返回完整 `VlmRequestIdentity`、Window binding、SemanticPack
  ArtifactRef 和提交它的 Receipt/ArtifactSet；
- Stage 1–3 不加载 timed speech payload。它只保留 VLM pack item 到 source/window 的 owner-bound 引用；Transcript/VAD reader 属于 Stage 4 integration owner。
- Stage 4 将 VLM candidate support 与 candidate timed evidence 只按
  pack 内 candidate support identity 一对一 join，禁止依赖可重命名
  的 candidate ID；缺失、重复或多配一均阻断 Stage 4 feasibility admission。

`RootMediaEvidenceBundle`、`CandidateTimedEvidenceSet` 的 strict decoder 由 Stage 4
任务实现；committed empty evidence 与字段缺失严格区分。Stage 1–3 Command 只接受
Source/Window/VLM reader 返回的 owner-bound typed refs。

## 首个真实策略

Stage 1 首版采用 `strict_global`：任何 fact/event/window/obligation 未覆盖、
conflicted、tainted 或证据 indeterminate 时整批 quarantine；后续有真实失败数据
后再引入 dependency-scoped 隔离。Stage 2 按 Proposal 数组下标组合的
lexicographic 顺序选择第一个 fully feasible tuple。Stage 3 首版
`all_or_nothing`，任一 Story 缺必选事实/Beat/evidence closure 时不产生 admitted
partial batch。

运行 profile 必须冻结三个 Stage 的 provider/model/prompt/schema、generation
parameters、JobPolicy、coverage/dependency/candidate/story/context policy hashes。
任一变化产生新 Command identity，禁止旧 run 读取新的环境默认值继续运行。

## ArtifactSet 成员

- Stage 1：EpisodeDigestSet、EventCardSet、NarrativeGraph、CoverageLedger、
  EvidenceDiagnostics、ConflictDiagnostics、DependencyClosureProof、唯一
  CoverageAdmission。
- Stage 2：CandidateCatalog、ProposalSet（每个 draft 有 disposition）、
  StoryPortfolio、SourceUsageLedger、唯一 PortfolioAdmission。
- Stage 3：每 Story 的 EditorialBlueprint、EvidenceClosureSet、ContextManifest、
  条件式 GenerationPartitionPlan、唯一 SemanticFeasibilityAdmission；整个目标集
  以 `all_or_nothing` batch Receipt 提交。

所有模型输出均为 Draft。确定性 Kernel 负责 owner 校验、ID 规范化、coverage
守恒、taint/conflict、VLM semantic/Source authorization support、组合选择、target freeze、context closure、
稳定 Beat ID、canonical merge 和 Admission。
