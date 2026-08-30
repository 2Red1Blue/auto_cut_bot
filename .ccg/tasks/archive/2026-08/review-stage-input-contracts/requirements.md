# 审查目标

审查当前真实 HTTP Pipeline 各阶段的输入来源、数据格式、格式校验、引用闭合与业务不变量，判断每个字段是否：

- essential：运行或确定性重放不可缺少；
- conditional：只在明确能力或路径启用时需要；
- removable：没有消费方、可由 Kernel 派生或只增加上下文与耦合。

审查必须区分：

1. HTTP 启动输入；
2. 外部 API Snapshot / Context Pack；
3. VLM 请求和结构化响应；
4. Stage 1–3 语义链；
5. Media Preflight / ASR / VAD；
6. Stage 4 精确剪辑；
7. Render / Publication QC；
8. 跨阶段 Artifact、Command、Receipt 的持久化控制字段。

本任务只审查，不修改生产代码。
