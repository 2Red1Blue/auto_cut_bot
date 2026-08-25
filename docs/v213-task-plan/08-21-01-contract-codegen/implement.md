# Implementation Plan

1. 冻结五份权威文档的文件 hash/章节锚点，并建立 source-meta schema 与 contract-path 转录清单；不从 legacy 读取。
2. 在 `autocut_kernel.contracts` 新建 compiler foundation、JCS canonicalization、source/generated manifest 与 build command；增加 reproducibility 和 generated-diff tests。
3. 实现 common primitives 及其严格负例，先保证 ref、tick/time_base、封闭 union、Envelope 可以被生成/加载。`SourceSpanRef` 使用 authority commit `ff6aca77` 的 v2.1.3 勘误字段集，必须包含 `source_sha256` 并绑定其 SourceClock owner。
   `Diagnostic.evidence_refs` 与 `Degradation.omitted_refs` 在未获得明确闭合 item schema 前不得生成；示例不能替代 Schema 定义。
4. 按 owner pack 转录并编译 system、Stage 01–05/publication schema 与五个 registries；每次增加一组即增加对应 traces/fixtures，不允许留悬空条目。
5. 增加 schema examples、规则/状态转移/命令负例、registry closure、legacy-absent wheel 及 Agent/Pipeline 公共加载 conformance。
6. 每一个 delivery pack：运行 firewall、compiler checks、pack tests；由独立 reviewer 对照冻结章节审查；通过 scope gate 后单独 commit。全部 pack 完成后才允许关闭 Task 01。

## Stop conditions

- 规范的字段、enum、owner、错误动作或例外没有稳定锚点：停止并提交 Authority Change，不在 source 中猜测。
- 生成模型和 JSON Schema 需要双写来获得不同结果：停止，修 compiler/IR。
- 任何 runtime 或 kernel 需要 import legacy contracts：停止；仅在未来独立 ledger/bridge task 获准后考虑适配。
