# 设计：旁路 Model I/O Debug Artifact

## 决定

`PostgresRuntimeStore` 继续是不可变请求/响应 Blob、GenerationAttempt、ArtifactSet 和 Receipt 的唯一权威。新增 `ModelIoDebugSink` 仅在 Runtime provider adapter 边界镜像实际 I/O；它没有 Store 写权限、不会进入 Command 构造函数，也不会改变 idempotency 或错误分类。

## 目录和文件

显式环境变量 `AUTO_CUT_BOT_PIPELINE_MODEL_DEBUG_DIR` 启用。每个调用创建：

```text
<root>/<provider>/<provider-idempotency-key>/
  request.json
  terminal.json
  raw-output.json              # 仅有文本输出时
```

每个 JSON 都带 schema_version、provider、model、dispatch/reconcile 标记、已脱敏 request/response、时间、响应 ID 和 provider idempotency key；后两者可精确定位 PostgreSQL 中的 Command/Attempt/Receipt。写入临时同目录文件后 `os.replace` 原子落盘；同一键的重复写只能覆盖相同阶段文件，不会影响 provider 调用。

## 调用覆盖

- `ArkResponsesTransport` 负责 Ark VLM 与 Ark Draft 的请求、流式终态和 retrieve reconcile 统一镜像；VLM/Draft adapter 给它带受限 `DebugContext`（provider idempotency key、调用类别、model）。
- FunASR/FSMN HTTP adapter 在本地 HTTP 请求与响应边界写相同格式的镜像，禁止写 shared token。
- Runtime composition 只在显式目录可用时注入 sink；未设置时用 No-op sink。

## 失败场景

流式 `incomplete`/`failed` 仍写 `terminal.json`，包括供应商返回的 status、error、usage 和 output 项；这补齐当前数据库对“没有 JSON 文本的失败响应”只保存 response id 的缺口。网络异常没有 provider response 时保存本地错误分类；不得尝试新请求。

## 安全与保留

调试根目录不得位于仓库内；composition 拒绝仓库路径、相对路径及不存在/不可写路径。调试文件可能包含剧情、对白和模型输出，运行文档标为本机敏感运行数据，不纳入 Git。API 密钥、Authorization、Cookie、token、文件字节均不写入。
