# 模型 I/O 调试文件

## 目标

在真实 Pipeline 每次模型调用后，保存一个可直接阅读、可按 run/command/attempt 定位的文件化调试副本。

## 必须保存

- 实际发送给模型的去密钥请求体；视频只保留 BlobRef/file_id/哈希，绝不复制视频字节；
- 供应商终态响应的完整可序列化快照、原始文本输出（如果存在）、HTTP/供应商响应 ID、状态、usage 与失败详情；
- provider idempotency key 与 provider response ID；它们可在 PostgreSQL 中定位对应的 command/attempt/Receipt，不复制或替代这些权威记录。

## 边界

- 覆盖现有 Ark 视频 VLM、Ark 文本 Draft（Stage 1/2/3）和 FunASR/FSMN HTTP 调用；没有模型调用的 Stage 不生成空文件。
- 文件是调试镜像，绝不作为 Artifact、Receipt、重试、准入或恢复的权威输入。
- 默认关闭；启用必须给出显式安全目录。任何写入失败只能记录本地诊断，不能改变模型调用或 Command 结果。
- 递归脱敏 API key、Authorization、token、cookie 等敏感字段；测试必须证明脱敏、原子写入和镜像写失败不影响调用结果。
