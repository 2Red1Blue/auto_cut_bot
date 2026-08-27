# 实施与验证

1. 实现无状态、可注入的 `ModelIoDebugSink` 与严格脱敏/原子写入规则。
2. 在共享 Ark transport 接入 request、stream terminal 与 retrieve terminal 镜像；覆盖 VLM 和 Draft。
3. 在 FunASR/FSMN HTTP 边界接入同一 sink。
4. 在 `PipelineStageRunner` 与 `PipelineStageReconciler` 创建 `run_id/stage` 作用域，固定输出阶段的 `input.json`、`output.json` 和按需 `error.json`。
5. 通过 composition 的显式环境变量创建同一个 sink；更新真实运行文档。
6. 编写单元测试：阶段目录、成功、incomplete、脱敏、原子写入、默认禁用；运行 pytest、ruff、basedpyright。
7. PC 拉取提交后，用真实 Pipeline 验证生成的 VLM 阶段目录和模型原始输入输出。
