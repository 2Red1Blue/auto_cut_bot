# 00 共同请求与结构化输出

## Provider 实际收到的公共骨架

VLM 和 Stage 1–3 最终都转成 Ark Responses 请求。共同骨架如下：

```json
{
  "model": "<frozen model_id>",
  "input": [{"role": "user", "content": [{"type": "input_text", "text": "<prompt>"}]}],
  "text": {"format": "<见下方版本化差异>"},
  "max_output_tokens": 32768,
  "temperature": 0,
  "stream": true,
  "store": true
}
```

VLM 的 `content` 还包含 `input_video`，由 Ark Files API 上传后的 `file_id` 指向实际视频。

`text.format` 当前不是统一形状：

```json
{
  "type": "json_schema",
  "name": "vlm_semantic_pack_v4",
  "schema": {},
  "strict": true
}
```

上面是 VLM 当前已注册 adapter 使用的 Ark v4 直接形状。Stage 1–3 的
`DraftDispatchRequest` 当前冻结的是 SDK 类型注解对应的嵌套形状：

```json
{
  "type": "json_schema",
  "json_schema": {
    "name": "<stage schema name>",
    "schema": {},
    "strict": true
  }
}
```

二者属于不同的 provider wire contract，禁止在重放时静默互换。当前最后一次真实流程尚未
进入 Stage 1，因此嵌套形状仍需在首次真实 Stage 1 调用中验证；若 Ark 端拒绝，应新增并
版本化 text-draft adapter 后选择性重跑 Stage 1，不能改写既有请求 bytes。

| 参数 | 含义 | 是否影响语义重跑 |
|---|---|---:|
| `model` | 精确模型部署 ID | 是 |
| `input` | 模型真正可见的文字/视频 | 是 |
| `text.format` | 版本化的 provider 原生结构化输出约束；VLM 与文本 draft 形状不同 | 是 |
| `text.format.strict` | 必须严格按 Schema 返回 | 是，固定为 `true` |
| `max_output_tokens` | 最大输出预算；超限不能在同一请求中偷偷放宽 | 是 |
| `temperature` | 采样温度 | 是 |
| `stream` | SSE 流式接收，便于尽早持久化 response id | 否，但当前必须为 `true` |
| `store` | 允许用 response id 对账未知结果 | 否，但当前必须为 `true` |
| `thinking.type` | VLM adapter v5 的显式推理开关 | 是 |

## 不直接塞给模型的请求 Envelope

Kernel 同时冻结 `job`、`command_request`、`input_binding_sha256`、
`provider_request_sha256`、`response_schema_sha256`、`retry_policy`、前序 Receipt/ArtifactSet
引用等信息。这些字段用于：

- 证明重试是在重放同一个请求；
- 证明当前阶段读取的是指定前序产物；
- 防止代码更新后用“当前默认值”重解释历史请求；
- 让失败、重启和跨机器恢复仍能定位同一条因果链。

它们不应重复进入自然语言 prompt。Stage 1–3 当前仍有部分 owner/member 引用出现在
结构化 context 中，这是精确引用闭合所需；后续可以压缩显示 ID，但不能丢掉本地可复算映射。

## Schema 的角色

Schema 同时承担三层约束：

1. Provider 原生 `json_schema` 尽量阻止模型返回非法形状。
2. 本地严格 JSON decoder 拒绝重复 key、非 UTF-8、浮点禁区、深度/字节/数量超限。
3. Kernel evaluator 复查跨字段关系、引用闭合、前序 owner 和业务可行性。

所以“让模型直接输出 Pydantic”并不能替代本地校验。模型输出的是符合 JSON Schema 的
JSON 数据；Pydantic/dataclass 只是本地解码的一种实现，Admission 仍必须独立计算。

当前 V23 VLM 的规范化 Schema 约 10.8 KB，不是早期审计中的约 34 KB。Stage 1–3 的
Schema 根据冻结 Policy/目标 Story 动态生成，其 hash 写入 durable request。

## 重试身份

每次 Attempt 的 provider idempotency key 绑定：

```text
Command 名 + Job + command idempotency key + request_hash + attempt_ordinal
```

同一个 Attempt 结果未知时先用已持久化 response id 对账，不能盲目新发请求。只有明确标记为
retryable 的 429、500、502、503、504 或等价 provider 错误，才消耗下一次 Attempt。
