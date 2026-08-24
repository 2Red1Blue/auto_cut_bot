# Plan

## Layer 1 — 契约与持久化

1. 定义封闭的 `GenerationRetryPolicy`、`ProviderFailureDisposition` 和 Attempt ordinal/因果字段。
2. 添加 PostgreSQL 迁移，把 CommandSlot→GenerationAttempt 从一对一升级为有序一对多，并加入前驱、ordinal、dispatch lease/token、Receipt-chain 与并发唯一约束。
3. 扩展 Store 端口和 PostgreSQL Store：读取有序 Attempt 链、原子预留下一 Attempt、成功/耗尽时生成精确 Receipt 关系。

## Layer 2 — Provider 与 Command

1. 豆包 adapter 对明确错误输出分类，不执行 SDK 自动重试，并严格区分 HTTP trace ID 与 Responses API response ID。
2. GenerateVlmEvidenceCommand 在 retryable terminal failure 后保留 Command running，并按策略预留下一个 Attempt。
3. indeterminate 继续只 reconcile；repairable 与 nonretryable 明确终止。

## Layer 3 — Runtime、测试与文档

1. Retry policy 写入 PipelineExecutionProfile 并参与 canonical hash。
2. 补三次成功/耗尽、不可重试、未知结果、并发/CAS和 PostgreSQL迁移测试。
3. 更新 Trellis Task04 设计，记录 transport retry 与 semantic recovery 的边界。
4. 运行 PostgreSQL、单元、类型、lint、架构防火墙测试，独立审查后提交。
