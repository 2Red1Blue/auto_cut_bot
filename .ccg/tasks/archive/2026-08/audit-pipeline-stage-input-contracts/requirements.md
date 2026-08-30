# Review Scope

审查当前 HTTP Pipeline 从外部请求到 SourcePrep、ContextPrepare、VLM、Stage 1–3、Media Preflight 的输入字段、模型可见上下文和格式校验。

重点判断：

1. 字段是否被下游实际消费；
2. 校验是否保护真实的安全、重跑或证据不变量；
3. 是否存在必填空字段、长引用和重复 provenance 导致的无效上下文；
4. 当前真实 PC 运行的 debug 是否足以证明实际输入输出；
5. 找出会阻断故事生成或降低无人流水线质量的契约矛盾。

本任务只审查，不修改业务契约或运行时行为。
