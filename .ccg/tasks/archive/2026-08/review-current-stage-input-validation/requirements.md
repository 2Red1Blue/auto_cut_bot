# 审查范围

只读审查当前 `feat/v213-contract-codegen` 工作区中真实存在的流水线输入与格式校验，覆盖 HTTP run、Source Prep、Context Prepare、VLM、Stage 1–4、Media Preflight、Render/QC 及阶段间持久化引用。

重点判断：

1. 每个输入字段是否为该阶段完成职责所必需；
2. 是否把审计、物理剪辑、ASR/VAD、未来剧情或外部 API 原始数据错误地送入模型；
3. schema、枚举、引用闭合、owner/scope/hash、时间单位和版本校验是否过松或过严；
4. 是否支持同集、同阶段、同 invocation 的确定性重跑，而无需全量重跑 VLM；
5. 已提交基线与未提交候选实现必须分开陈述；
6. 不调用真实 Ark/VLM，不修改业务代码。
