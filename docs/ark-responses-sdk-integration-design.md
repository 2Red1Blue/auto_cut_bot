# Ark Responses SDK 调用设计 — 当前 Pipeline 的 Files、缓存与恢复边界

**状态：实现前设计与复审完成；尚未替代当前运行时。**
**适用实现：** `auto_cut_bot/pipeline/vlm/`、`packages/autocut-kernel/src/autocut_kernel/vlm/`。
**不适用实现：** `packages/autocut-core/` 的历史 Pipeline、Agent 的通用 OpenAI-compatible Provider。

## 1. 目标与结论

当前语义 Pipeline 使用火山官方 `volcenginesdkarkruntime`（发行包
`volcengine-python-sdk[ark]`）调用 Ark Files API 与 Responses API。它不是
LiteLLM 路径。当前项目的 `.venv` 与 `uv.lock` 实际解析为 `5.0.47`；系统 Python 的
`5.0.45` 安装缺少可导入的 Ark runtime，不能作为 Pipeline 运行依据。生产契约以锁定
环境的请求形状、流事件和 Files 状态为依据。

本设计只解决三件事：

1. 上传过的视频只能在**相同 Ark 请求作用域**内复用；
2. Files/Responses 出现超时、429 或流中断时，恢复不得重复产生未知副作用；
3. 并发窗口分析不能因为把整段代理视频复制为 Python `bytes` 而放大内存峰值。

它不改变 VLM 的职责：VLM 只输出视频语义观察，不能输出物理剪辑端点；Ark
调用也不能绕过 Kernel 的 Command、Attempt、Receipt 和 Artifact admission。

## 2. 当前审计事实

### 2.1 当前唯一的新架构调用链

```text
GenerateVlmEvidenceCommand
  -> ProviderDispatchRequest(proxy_content: bytes)
  -> DoubaoArkVlmProvider
  -> Files.create(... purpose=user_data, video.fps)
  -> files.retrieve / files.wait_for_processing
  -> Responses.create(stream=True, store=True)
  -> ArkResponsesTransport
```

当前适配器已正确做到：本地视频经 Files API 取得 `file_id`；Responses 输入使用
`input_video` + `input_text`；V5 显式传 `thinking.type`；`failed`、`incomplete`
和未知流终态不伪装为成功；普通 4xx 不盲目重试；`response.created` 的 response id
先持久化，再允许后续结果成为完成结果。

### 2.2 旧提交的适用范围

`0559694d`（`auto_cut_bot`）和 `be51968`（`ac_auto_cut`）位于历史分支/旧实现，
不是当前 `feat/v213-contract-codegen` 的祖先。它们包含的 SDK 5.0.45 API 经验是真实
证据，但不能直接 cherry-pick：旧实现有旧 ArtifactBus、旧缓存和 Chat 风格消息
转换。新运行时只应移植下文定义的 API 行为与测试，不复用其架构对象。

### 2.3 当前缺口

| 编号 | 事实 | 风险 |
| --- | --- | --- |
| A1 | `tenant_id`、`project_id` 只进入本地缓存指纹，未送入 Ark 请求。 | 它们不是 Ark SDK 参数；当前名称会误导实现者以为已经选择了远端项目。 |
| A2 | 新适配器仍调用 SDK `files.wait_for_processing`。 | headers 为空时这是 SDK 的正常用法；只有未来端点契约要求自定义 header 时，waiter 才不能继续使用。 |
| A3 | scope 指纹取 base URL 的 origin，不含规范化 path。 | 同域不同 API 网关/路径可能错误复用 provider `file_id`。 |
| A4 | `ProviderDispatchRequest` 强制携带完整 `bytes`。 | 窗口并行时，Blob、Kernel 和 SDK multipart 可能保留多份大视频副本。 |
| A5 | 项目声明允许未来任意 5.x SDK。 | lock 被意外更新时，已验证的 wire 契约可能漂移。 |

## 3. 决策：ArkRequestScope/v1

### 3.1 先定义语义，不猜 header 名称

`ArkRequestScope/v1` 是**本项目内部的缓存/调用边界**，不是 Ark SDK 新增的参数。
官方 `Ark(api_key, base_url, ...)` 本身不需要 `tenant_id` 或 `project_id`。对当前的
单账号、单 endpoint Pipeline，scope 固定为 endpoint 加 `default` 账户别名，headers
为空；无需新增环境变量，也不应向 Ark 发送额外 header。

现有 `tenant_id` 与 `project_id` 应从 Provider 的必填配置中移除，或仅在未来多个 Ark
账户共用同一 PostgreSQL 缓存时，替换为明确的非密钥 `credential_scope_id`。不得根据
旧变量名擅自拼接 `X-*` header。只有端点文档明确要求时，才注册一个受白名单约束的
header 字段和相应 wire 测试。

### 3.2 ArkRequestScope/v1 的最小字段

```json
{
  "kind": "ArkRequestScope/v1",
  "base_url": "https://host.example/api/v3",
  "credential_scope_id": "default",
  "headers": {},
  "scope_fingerprint": "sha256:<canonical base_url + credential scope + canonical header names/values>"
}
```

约束：

- `base_url` 必须 HTTPS，去除 fragment/query、规范化 host、默认端口和尾斜杠；
- `credential_scope_id` 默认是内部常量 `default`；只有多个 Ark 账户共用同一缓存库时
  才需要显式配置。它不含密钥。API key 轮换但 scope 未变时可以复用；
- headers 默认必须为空。若端点契约明确要求额外 header，只能来自固定 allow-list，
  不接受任意环境变量 JSON；
- 原始 header 仅传给 SDK，不写入 PostgreSQL、Receipt、缓存记录或 debug 文件；
- 只持久化 `scope_fingerprint`。它使用完整 credential scope/header 值计算 hash，以防
  不同 Ark 账户/租户共用 Files 对象；日志中最多显示 header 名称和 hash；
- `api_key` 继续只由 SDK 认证使用，绝不纳入可打印身份。

所有 Ark 调用必须使用同一个 scope：`files.create`、`files.retrieve`、
`responses.create` 和 `responses.retrieve`。当前 endpoint 的 scope headers 为空，
并且不再要求租户/项目环境变量。

## 4. Files API 设计

### 4.1 缓存身份

Provider media cache 的唯一逻辑身份是：

```text
provider_id
+ ArkRequestScope/v1.scope_fingerprint
+ source Blob content_hash + byte_length + media_type
+ FilesRequestPolicy/v1.canonical_hash
```

`FilesRequestPolicy/v1` 的 canonical mapping 为：

```json
{
  "purpose": "user_data",
  "video_preprocess": {"fps": 1.0},
  "multipart_media_contract": "mime-bearing-v1"
}
```

它不应把 Responses schema、prompt、thinking 或 model id 放入文件缓存；这些不改变
已上传视频的 Files 语义。反之，endpoint、header scope、purpose、MIME 与预处理
参数任何一个改变，都必须命中不同缓存记录。旧 origin-only 记录不迁移复用，安全地
自然过期或一次性失效。

### 4.2 Files 轮询

当前默认 scope 没有额外 header，因此保留 SDK `files.wait_for_processing` 是正确且
更简单的选择。**只有**未来注册了非空 scope headers 时，才启用
`ArkFilePoller/v1` 取代 waiter：

```text
retrieve(file_id, scope headers)
  active | processed             -> record_available, 可提交 Responses
  failed | error | expired | deleted -> record_failed, 不重传
  其他 processing 状态           -> 在 deadline 内 sleep 后 retrieve 同一 file_id
  deadline / 网络结果未知        -> release_processing, 返回 repairable/retryable
```

该条件下的本地轮询必须：

- 使用单调时钟计算 deadline，并受现有 generation lease 上限约束；
- 每次都传同一 `ArkRequestScope`；
- 只轮询已持久化的 `file_id`，绝不因轮询异常重新 `files.create`；
- 使用可观测、非敏感的 `provider_status`、HTTP status、Ark trace id；
- 将未知上传结果保持为现有 `indeterminate` quarantine，直到隔离期结束前禁止盲传。

### 4.3 上传体与内存

短期实现可保持 `bytes` 端口，先完成作用域与轮询修复。长期新增
`VerifiedProxyMediaReader`，由 Store 只读打开已经绑定 BlobRef 的内容，并将文件对象
直接交给 SDK multipart 上传。Provider 不接收任意文件路径，也不自行相信路径：

```text
Store immutable BlobRef -> verified one-shot reader -> Ark Files multipart
```

完整性来自 Store 对不可变 BlobRef 的哈希/长度绑定；Provider 只检查 reader 的声明
长度和媒体类型，不重复把整个视频读入内存计算 hash。切换前要量化 10 个并发窗口的
RSS，并证明中断上传仍进入现有 unknown-outcome quarantine。

## 5. Responses API 与恢复设计

Responses 请求继续使用当前的单用户消息：

```json
{
  "input": [{"role": "user", "content": [
    {"type": "input_video", "file_id": "file-..."},
    {"type": "input_text", "text": "<frozen rendered prompt>"}
  ]}],
  "text": {"format": {"type": "json_schema", "name": "...", "strict": true, "schema": {}}},
  "stream": true,
  "store": true,
  "thinking": {"type": "disabled"}
}
```

这里的 `thinking`、response schema、prompt、model 和视频 FPS 都必须来自冻结的
semantic authority/request payload，不能由环境变量临时覆盖。

恢复状态机：

```text
Responses create
  -> response.created: 先 CAS 持久化 response.id
  -> stream completed: 使用 terminal response.output 的严格正文
  -> stream lost / local timeout: 用已持久化 response.id reconcile
  -> failed/incomplete: 返回明确失败；只有 429/5xx 或 provider 明示暂态错误可重试
  -> 无 response.id 的未知 create: indeterminate，不能另发一次 create
```

`usage`、response id、终态 status 和 trace id 是诊断/计量事实：写入现有 secret-redacted
Model I/O debug 以及 Attempt 诊断；它们不是 VLM Artifact 通过 admission 的依据。

## 6. 结果复用与身份

当前 VLM 复用投影已绑定渲染后的 prompt、schema、模型、provider id、请求参数、解析
契约、窗口采样和 provider scope。完成 `ArkRequestScope/v1` 后，它应直接使用新的
`scope_fingerprint`；不得另造一个与 Command/Receipt 平行的“Responses 结果缓存”。

因此：

- 改 prompt/schema/model/thinking/FPS -> 新 Command 身份或新 policy，不能复用旧结果；
- 改 endpoint/header scope -> 新 provider scope，不能复用旧结果或旧 Files 对象；
- 仅改 debug 路径、日志格式、非语义连接池设置 -> 不影响历史 VLM 结果复用；
- SDK 版本变化不是自动可复用条件，必须先通过 wire compatibility suite。

## 7. 版本与发布规则

项目可以维持 `>=5.0.45,<6.0.0` 的声明，但生产/CI 必须使用已提交的 `uv.lock` 并以
`uv sync --frozen` 安装。2026-08-28 已将 lock 升级到 `5.0.47`，并通过 Ark
adapter/stream/debug 定向 suite；后续新版本仍必须走同一升级验证流程。升级 SDK 的最小变更集为：

1. 更新 lock；
2. 运行 Ark wire contract tests；
3. 对 Files upload、scope header、manual polling、Responses streaming 做一次真实端点
   小视频验证；
4. 若 wire 变化，注册新的 adapter strategy version，不能改写历史版本语义。

若不能保证 frozen lock，则将生产依赖临时精确锁为 `==5.0.47`。

## 8. 实施拆分与验收

### Wave 1 — 简化作用域与 Files 缓存身份（P0）

修改范围：Ark provider config、transport、file cache identity、composition、单元测试。

验收：

- 移除被误用的 tenant/project 必填运行配置；单账号默认使用 `credential_scope_id=default`；
- 完整规范化 endpoint 参与 Files cache scope；
- 同视频、不同完整 endpoint 或 header scope 不复用 `file_id`；
- 同 scope/同 FilesRequestPolicy 仅上传一次；
- header 明文不出现在 cache、Receipt、debug request/terminal；
- 默认 scope 使用 SDK waiter；只有注册了非空 headers 时，处理轮询才改为只 retrieve
  同一 `file_id`，不创建第二个 Files 对象。

### Wave 2 — SDK 锁与真实 wire 验证（P0）

验收：冻结安装解析 5.0.47；真实小视频完成 `upload -> active -> Responses completed`；
429、普通 4xx、流中断分别命中既定 Attempt 状态，不产生未知重复调用。

### Wave 3 — 流式 Blob 端口（P1）

修改 Kernel provider port、Store reader 与 Ark upload adapter；不得碰 VLM parser 或
semantic artifact schema。验收包括 10 并发窗口的峰值 RSS 对比、hash/长度绑定和中断
上传 quarantine 回归。

### Wave 4 — 可选连接复用（P2）

只在 SDK client 的线程安全和关闭语义被实际验证后，按 worker 生命周期复用 transport
client。它是吞吐优化，不能改变 Attempt/Files/Responses 的幂等语义。

## 9. 实施后反向审查

以下是本设计的对抗性复审结论。

| 审查问题 | 结论 | 设计处置 |
| --- | --- | --- |
| “当前 SDK 是否需要 tenant/project header。” | 不需要，已纠正先前过度设计。 | 单账号 scope 为 endpoint + `default`，headers 为空；只有端点契约明确要求时才增加。 |
| “手动轮询会因为超时重传同一视频。” | 条件闭合。 | 只有启用 headers 时才使用 poller；它只接受已持久化 `file_id`，无 create 分支。 |
| “缓存 hash 会泄露 API key/header。” | 已闭合。 | 只持久化哈希；debug 的递归 secret redaction 保持启用。 |
| “只用 origin 仍跨网关复用。” | 已闭合。 | scope 以完整规范化 base URL 计算。 |
| “将 endpoint/header 加入 Files cache 会错误使 VLM 结果缓存失效或重跑。” | 不成立。 | Files cache 与 Command 结果复用分层；VLM reuse identity 只接受新的 scope fingerprint。 |
| “流式端口削弱 Blob 完整性。” | 未实施，设计已限制。 | 只能由 Store 提供绑定 BlobRef 的只读 reader；不得传用户路径或未验证流。 |
| “一味精确锁 SDK 会阻止安全升级。” | 不成立。 | 采用 frozen lock + 显式升级验证，而非永久禁止升级。 |

**复审总论：** Wave 1/2 可以进入实现；Wave 3 是容量优化，不应阻塞真实单窗运行。
当前没有外部证据表明 Ark endpoint 需要 tenant/project header，因此它们不进入请求；
未来若端点契约变化，才新增明确 header 及其局部 poller 测试。

## 10. 关联代码与非目标

实现所有权：

- `auto_cut_bot/pipeline/vlm/doubao_ark_provider.py`
- `auto_cut_bot/pipeline/vlm/ark_responses_transport.py`
- `auto_cut_bot/pipeline/vlm/ark_file_cache.py`
- `auto_cut_bot/pipeline/runtime/composition.py`
- `packages/autocut-kernel/src/autocut_kernel/vlm/provider_port.py`（仅 Wave 3）

非目标：迁移/修复 `packages/autocut-core/` 的旧 Ark adapter；让 Agent Runtime 借用
Pipeline 私有 Ark adapter；把 ASR/VAD/字幕塞入 VLM 请求；把 Ark 输出直接当作物理剪辑
端点。
