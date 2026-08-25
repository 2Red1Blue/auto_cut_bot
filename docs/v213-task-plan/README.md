# v2.1.3 当前设计任务快照

更新日期：2026-08-26。

这里是当前新架构实现任务的 Git 版本快照，供台式机上的 AI 或开发者直接读取。它不是
旧 `ac_auto_cut` 的任务，也不是可以绕过 authority/Registry/Admission 的实现许可。

## 使用范围

- 代码仓库：`auto_cut_bot`；目标分支：`feat/v213-contract-codegen`。
- 共享 Kernel、Pipeline Runtime、Agent-Native Runtime 必须遵守当前仓库代码和这些任务
  文档的边界。
- 每个任务目录只保留 `task.json`、`prd.md`、`design.md`、`implement.md`、
  `implement.jsonl`、`check.jsonl` 等主计划文件；历史 `research/` 不复制，避免把旧
  结论误当作当前实现依据。
- 任务状态仍以工作区全局 `.trellis/tasks/` 为调度真源；本目录是可提交、可回滚、可在
  台式机读取的设计快照。修改任务后必须同步更新两处，并在任务入口重新确认 hash。

## 当前主线

```text
authority implementation
  → contract codegen / Artifact Store
  → command admission & recovery
  → media preflight + calibration
  → Stage 4 exact A/V vertical slice
  → Stage 1–3 semantic chain
  → Render / Publication QC
  → dual-runtime conformance
  → platform publication certification
  → migration cutover
```

`08-25-lock-real-test-authority-profiles` 是当前真实运行的前置任务；它必须先冻结
Shadow/local-run Profile 的权威源语法，才能进入 Profile decoder、authority lock 和
真实单集运行。

`08-26-real-e2e-multi-agent-closure` 是真实一集到全剧验收的集成计划；它不能替代各个
阶段的实现，也不能把 HTTP 503 或 fixture 测试称为真实 Pipeline 成功。

## AI 接手规则

1. 先读取本目录对应任务的 `prd.md`、`design.md`、`implement.md`。
2. 再读取当前代码和 `.trellis/spec/`；若两者冲突，停止并报告，不自行选择旧实现。
3. 一个 Agent 只拥有一个明确文件集合；共享导出、Runtime composition、migration 和
   authority lock 必须指定唯一 integration owner。
4. 先完成最小实现、测试和独立审查，再进入下一个任务；失败要保留 Receipt/诊断，不得
   静默跳过或用默认值伪造成功。
5. 不得导入 `ac_auto_cut` legacy 代码，不得让 Runtime 直接写 Store/Admission，也不得
   从普通环境变量伪造 authority snapshot。

## 当前不代表已完成

这些文件是计划和验收依据，不表示 Stage 1–4、Render/QC 或真实整剧 HTTP Pipeline 已经
跑通。真实状态以代码、测试结果、数据库 Receipt 和对应任务提交共同确认。
