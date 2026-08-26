# Contract Codegen

## 当前适用范围（2026-08-26）

本任务保留完整通用 Registry/codegen 的未完成工作，不是当前本地 Pipeline 的启动前置。
用户已撤回并删除总契约 `v2-production-system-contracts.md`；下文“五份”及其相关来源描述
是历史计划，不能据此恢复该文档或要求本地运行先补齐 19 个通用发布命令。
当前本地配置编译、校准和安装加载由 `08-25-lock-real-test-authority-profiles` 负责，
使用独立 domain hash，不能把其成功宣称为本任务完整 Registry 已 ready。
保留的 Stage 原理、显式来源、封闭字段、整数时间及无 legacy 依赖约束继续适用。

## Goal

将五份 v2.1.3 权威 Markdown 中已经冻结的契约，逐项转录为可审计的机器源，并确定性编译成唯一的 JSON Schema、Pydantic 模型、Rule / Command / Trace / Strategy Registry 与 hash manifest。Markdown 保持规范权威；机器源是其可执行镜像，二者差异必须被 CI 拒绝。

## Non-goals

- 不从 `autocut_core`、旧 Pydantic、旧 ArtifactBus、默认值或 parser 推断任何字段。
- 不实现 Store、Dispatcher、Admission 或业务 Stage handler；它们属于后续任务。
- 不以一个宽松的通用 `dict` 或自由 `parameters` 替代尚未完成的对象 Schema。

## Required delivery sequence

本 child 的范围大于单次安全提交，必须按下列可独立验收的 work package 交付；每包都完成计划、实现、测试、独立审查和 commit，全部完成后才能关闭本 child：

1. compiler foundation：canonical JSON/YAML loader、source manifest、生成目录、hash manifest、禁止手改 generated 输出；
2. shared primitives：Envelope、ArtifactRef、DomainRef、SourceSpanRef、ImmutableBlobRef、整数 tick/time_base、封闭 union helpers；
3. system contracts：Artifact/Policy/Command/Receipt/Registry/transaction/recovery/publication 的机器源与 registries；
4. Stage 01–04 contract packs：逐 Stage 的 payload、Rule、Command、Strategy、Trace 与 fixture mapping；
5. complete conformance：所有规范示例、负例、跨引用、重复 ID、Schema↔generated diff、两 Runtime loading 与 legacy-absent wheel 验证。

## Requirements

- 前置：`00-trellis-authority-sync` 与 `02-import-firewall-and-package-skeleton` 已完成。
- 源和生成物位于新的 `packages/autocut-kernel/src/autocut_kernel/contracts/`；Runtime 只 import generated public API。
- Source registry 对每个 Artifact、Command、Rule、Strategy、Trace 和测试 ID 给出唯一登记、owner、版本和闭合引用。
- 所有 Schema 默认 `additionalProperties: false`；判别联合明确列出 variant required/forbidden 字段；`*_ref|*_refs` 不接受裸 string；Domain 不出现 float seconds。
- Generated 输出可在没有 legacy distribution、没有 repository checkout 的环境中加载；同一 source/toolchain 必须产生相同 manifest/hash。

## Acceptance Criteria

- [ ] 所有规范示例由同一生成 Schema 校验，且每个示例映射到具体的 source/schema hash。
- [ ] source/generated 双向 diff、悬空引用、重复 ID、owner 冲突、空 pass/fail trace、未注册 Schema/测试均失败。
- [ ] 未知字段、自由 enum、裸 ref、float time、缺失 time_base、非法 state transition 和自由 Command parameters fixture 均失败。
- [ ] 每个 required Rule、Command profile、Artifact Schema、状态转换有至少一个 Contract Trace，且 Rule 的 indeterminate 语义显式。
- [ ] import firewall 在 compiler 与 generated package 上通过；若后续 Reuse Ledger gate 已启用，也必须在 codegen tests 前通过。
- [ ] 独立审查对每一 work package 逐条映射上述 AC 并绑定 candidate tree；未覆盖的 Stage 不能被宣称完成。
