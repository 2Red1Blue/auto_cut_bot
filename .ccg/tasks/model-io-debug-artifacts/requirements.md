# 模型 I/O 调试文件

## 目标

在真实 Pipeline 每次阶段执行后，保存一个可直接阅读、可按 run/stage/command 定位的文件化调试副本。

## 必须保存

- 每个已执行或已进入 reconcile 的阶段均有独立目录：`<root>/<run_id>/<stage>/`；
- 每个阶段固定写入 `input.json`、`output.json`，发生未捕获异常时另写 `error.json`；它们记录阶段上下文、命令、冻结配置指纹与结果/Receipt 引用；
- 实际发送给模型的去密钥请求体与供应商终态响应放在对应阶段的 `model/` 子目录；视频只保留 BlobRef/file_id/哈希，绝不复制视频字节；
- provider idempotency key 与 provider response ID；它们可在 PostgreSQL 中定位对应的 command/attempt/Receipt，不复制或替代这些权威记录。

## 边界

- 覆盖 source prep、VLM、media preflight、Stage 1/2/3 等全部已执行阶段；没有模型调用的阶段仍保留阶段级输入输出，`model/` 不创建空文件。
- 文件是调试镜像，绝不作为 Artifact、Receipt、重试、准入或恢复的权威输入。
- 默认关闭；启用必须给出显式安全目录。任何写入失败只能记录本地诊断，不能改变模型调用或 Command 结果。
- 递归脱敏 API key、Authorization、token、cookie 等敏感字段；测试必须证明脱敏、原子写入和镜像写失败不影响调用结果。
