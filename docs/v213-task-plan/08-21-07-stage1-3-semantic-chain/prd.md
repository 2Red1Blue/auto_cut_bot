# Stage 1–3 Real Semantic Chain

## Goal

把已提交的整剧 Source/Window 与 Doubao VLM observations 编译为真实的 NarrativeGraph/CoverageLedger、Story Portfolio 和 EditorialBlueprint，替换当前 fixture-only semantic chain。Pipeline Runtime 与 Agent-Native Runtime 只能调用同一组共享 Command；测试阶段保持 publication closed。ASR/VAD/timed media evidence 不属于 Stage 1–3 的语义输入，只在 Stage 4 证明对白完整性和物理切点可行性。

## Required Inputs

- 已提交并 hash-bound 的 SourceManifest/WindowManifestSet/VlmSemanticPackSet。
- SourceManifest 包含与 source ID/hash 绑定的操作授权：Stage 1 要求
  `semantic_analysis`，Stage 2/4 要求 `render_source`；缺失 purpose 默认 deny。
- 每个 VLM semantic pack 以局部 entity/fact/event/continuity 和可选
  highlight/hook hypothesis 分层，携带 frame evidence、整数 proxy coarse
  interval 与置信/风险字段；Source 映射、全局 ID、owner 与 provenance 由
  Kernel 派生。Stage 1–3 不读取 Transcript 文本、VAD 或物理 endpoint。
  Stage 3 只能声明待 Stage 4 满足的 dialogue/visual/subtitle evidence requirement。
- Stage/Job/Generation policies 的明确版本与 hash。

## Requirements

- Stage 1 Command 从 root evidence 与受审计 draft 编译 EpisodeDigestSet、EventCardSet、NarrativeGraph、CoverageLedger、Diagnostics/Proof 和 Admission；每个输入 observation/window/obligation 恰有 ledger row，unresolved 不得改写为 water content。
- EventCard 只表达事实与 source evidence；高光、反转、情绪、对白适用性属于 Stage 2 CandidateCatalog；任何物理端点只属于 Stage 4。
- Stage 2 允许生成模型扩展 Candidate/Proposal draft，但本地 deterministic compiler 只验证 VLM semantic evidence/capability、taint、Source 授权与粗粒度候选支持，选择 lexicographically first feasible Portfolio 并冻结 target_story_ids。对白/字幕/精确 A/V 支持保持 deferred 到 Stage 4。禁止静默丢弃失败 Proposal 后提交 partial success。
- Stage 2 的每个权威 Candidate 必须从已提交的 VLM observation/capability 产生非空、规范排序的 `editing_modes ⊆ {dialogue, action}`，并绑定其 observation hash。`dialogue` 与 `action` 可以同时存在；ASR 文本、word 数量、VAD、旧 strength 字段和本地启发式均不得推断或改写该集合。
- Stage 3 对冻结 Story fan-out，生成每 Story 的 EditorialBlueprint、EvidenceClosureSet、ContextManifest 与 Admission。每个必选 Beat 必须存在且可由 committed evidence 支持；失败不得省略 Beat。
- 所有 Stage 业务成员和唯一 Admission 通过现有 Command/Receipt/ArtifactSet/CAS 原子提交。重放返回同一 committed refs，不重复生成。
- v2 observation Artifact 只保留审计历史，不允许转换为 v3 权威输入；禁止双写和兼容字段映射。
- Pipeline 与 Agent-Native 给定相同 committed inputs/drafts/policies 时产生相同 business Artifact hashes、Admissions 与 target_story_ids。
- 生产 profile 在 publication certification 与双 Runtime conformance 完成前 fail closed；test/shadow 可继续到本地 Render/QC。

## Acceptance Criteria

- [ ] 真实 Doubao observation 至少生成一个 admitted Story 和完整 Blueprint，不使用 fixture registry/resolver，也不读取 ASR 文本。
- [ ] 未覆盖 observation、tainted VLM evidence、未知 ID、裸路径/float seconds/物理 cut 字段均失败关闭；Transcript/VAD/Scene/Subtitle 缺失由 Stage 4 feasibility admission 处理，不得倒灌为剧情语义。
- [ ] 多 Proposal 的 canonical Portfolio 选择、target freeze、Story fan-out 和 all-or-nothing Blueprint 有确定性回归测试。
- [ ] Candidate `editing_modes` 只能来自 owner-bound VLM evidence；缺失、未知值、非规范顺序、hash 不匹配和任何 ASR/VAD 推断尝试均失败关闭，并能由 Stage 4 一对一消费。
- [ ] PostgreSQL restart/replay 不重复 provider invocation，不生成第二 ArtifactSet。
- [ ] Pipeline/Agent adapter conformance fixture 对相同输入输出相同 hashes。
- [ ] Ruff、BasedPyright、target/PostgreSQL/import-firewall tests 通过并完成独立审查。
- [ ] 运行时切到新 Command 的同一迁移波删除旧 fixture semantic command、v2 adapter 和未接入运行时的 Stage 1–3 prototype；生产包不存在两套 semantic authority。

## Non-goals

- 本任务不选择最终 A/V endpoint、不调用 ffmpeg render、不产生 publish_decision=allow、不实现高光前端。
