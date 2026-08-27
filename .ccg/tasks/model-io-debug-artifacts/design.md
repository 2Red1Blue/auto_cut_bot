# 设计：旁路 Stage Debug Artifact

## 决定

`PostgresRuntimeStore` 继续是不可变请求/响应 Blob、GenerationAttempt、ArtifactSet 和 Receipt 的唯一权威。新增 `FileModelIoDebugSink` 同时实现阶段级旁路镜像：`PipelineStageRunner` 记录阶段输入/输出/异常，并通过 task-local `ContextVar` 将相同 run/stage 归因传递给 Runtime provider adapter。它没有 Store 写权限、不会进入 Command 构造函数，也不会改变 idempotency 或错误分类。

## 目录和文件

显式环境变量 `AUTO_CUT_BOT_PIPELINE_MODEL_DEBUG_DIR` 启用。每个调用创建：

```text
<root>/<run_id>/<stage>/
  input.json                   # 阶段的请求、Command、冻结 profile 指纹
  output.json                  # 阶段返回的 outcome、Receipt 引用
  error.json                   # 仅阶段端口抛出未捕获异常时
  model/
    <provider>/<call-kind>-<idempotency-key-hash>/
      request.json
      terminal.json
      raw-output.bin            # 仅有原始文本/JSON 输出时
```

每个 JSON 都带 schema_version、run/stage、operation、时间与已脱敏内容；模型记录另带 provider、model、响应 ID 和 provider idempotency key。后两者可精确定位 PostgreSQL 中的 Command/Attempt/Receipt。写入临时同目录文件后 `os.replace` 原子落盘；同一阶段重试覆盖该阶段的最新 `input/output/error`，模型调用按 provider idempotency key 独立保留，不会影响 provider 调用。

## 调用覆盖

- `ArkResponsesTransport` 负责 Ark VLM 与 Ark Draft 的请求、流式终态和 retrieve reconcile 统一镜像；VLM/Draft adapter 给它带受限 `DebugContext`（provider idempotency key、调用类别、model）。
- FunASR/FSMN HTTP adapter 在本地 HTTP 请求与响应边界写相同格式的镜像，禁止写 shared token。
- Runtime composition 只在显式目录可用时注入 sink；未设置时用 No-op sink。

## 失败场景

流式 `incomplete`/`failed` 仍写 `terminal.json`，包括供应商返回的 status、error、usage 和 output 项；这补齐当前数据库对“没有 JSON 文本的失败响应”只保存 response id 的缺口。网络异常没有 provider response 时保存本地错误分类；不得尝试新请求。

## 安全与保留

调试根目录不得位于仓库内；composition 拒绝仓库路径、相对路径及不存在/不可写路径。调试文件可能包含剧情、对白和模型输出，运行文档标为本机敏感运行数据，不纳入 Git。API 密钥、Authorization、Cookie、token、文件字节均不写入。
