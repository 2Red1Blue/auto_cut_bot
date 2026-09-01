# 当前 Pipeline 的 LLM 阶段契约

本文档集只描述 `feat/v213-contract-codegen` 当前代码。历史任务计划、旧 25-stage、旧
ArtifactBus、旧 HITL 和旧 `ac_auto_cut` 复用方式均不是实现依据。

## 当前真实 HTTP 流程

`AUTO_CUT_BOT_PIPELINE_PLAN=semantic_story` 创建以下顺序：

```text
source_prep
  -> context_prepare
  -> vlm
  -> stage1_narrative
  -> stage2_portfolio
  -> stage3_blueprint
```

| 阶段 | 是否调用 LLM | 当前作用 | 当前 HTTP 状态 |
|---|---:|---|---|
| `source_prep` | 否 | 枚举、探测、压缩视频并冻结窗口/PTS/Blob 身份 | 已接线 |
| `context_prepare` | 否 | 外部剧情 API Snapshot、显式集映射、生成 `WindowContextPack` | 已接线，可降级 video-only |
| `vlm` | 是，视频 VLM | 每集视频窗口生成局部语义事实、事件和候选 | 已接线 |
| `stage1_narrative` | 是，文本 LLM | 跨窗口生成 Beat、义务、故事线和实体合并提案 | 已接线 |
| `stage2_portfolio` | 是，文本 LLM | 生成故事提案与素材需求；Kernel 决定可行组合 | 已接线 |
| `stage3_blueprint` | 是，文本 LLM | 为冻结 Story 生成编辑蓝图草案 | 已接线 |
| `media_preflight` | 否 | SenseVoiceSmall/FSMN、帧/采样/字幕安全证据 | 不在 `semantic_story` 计划内 |
| Stage 4 精确剪辑 | 否 | 选择严格 A/V span、生成 Recipe | 尚未接入当前 HTTP 计划 |
| Render / Publication QC | 否 | 渲染、本地 QC、发布准入 | 尚未接入当前 HTTP 计划 |

因此，当前 `semantic_story` 的 `succeeded` 只表示 Stage 3 蓝图闭合，不表示已产生视频，
更不表示允许发布。

## 共同规则

1. 模型只产生 **draft**。Artifact、全局 ID、哈希、Receipt、Admission 和发布许可均由
   Kernel/Store 生成，模型不能自证成功。
2. 所有 LLM 调用都使用 Ark Responses 的 `stream=true`、`store=true` 和 provider 原生
   `json_schema`。完整 Schema 位于请求的 `text.format`，不是自然语言 prompt 的一部分；
   provider 是否把 Schema 计入输入 token 由其计费实现决定。
3. 请求中存在两类信息：
   - **模型可见**：`input[*].content[*].text`、视频 `file_id`、响应 Schema，以及模型参数。
   - **审计但模型不必理解**：Command、Job、BlobRef、Receipt、hash、retry policy、
     provider idempotency key。这些保存在 durable request envelope/数据库。
4. 任何未知字段、错误 owner、未知引用、超预算、非严格 JSON、Schema 不匹配或策略 hash
   不闭合都不能自动补默认值。
5. 失败查询、debug 目录和当前运行断点见 [错误、重试、Debug 与当前状态](./05-errors-debug-status.md)。

## 文档入口

- [00 共同请求与结构化输出](./00-shared-request-envelope.md)
- [01 VLM：视频窗口语义](./01-vlm.md)
- [02 Stage 1：知识链/叙事图](./02-stage1-narrative.md)
- [03 Stage 2：故事设计/组合](./03-stage2-portfolio.md)
- [04 Stage 3：编辑蓝图](./04-stage3-blueprint.md)
- [05 错误、重试、Debug 与当前状态](./05-errors-debug-status.md)
- [06 LLM 与程序责任边界重设计](./06-llm-program-responsibility-redesign.md)
- [07 V23 全字段 Parity Matrix 与外部参考](./07-v23-field-parity-and-external-references.md)

其中 `00`–`05` 描述当前代码，`06` 是经过现状审查和外部方案调研后的目标设计，`07` 是
V23 到目标契约的逐字段防丢失账本与参考资料索引；在对应迁移项完成并通过 fixture/真实单集
验证前，`06`–`07` 不能被解释为已经上线的执行事实。

## 权威代码

- 请求组装：`auto_cut_bot/pipeline/vlm/request_factory.py` 与
  `packages/autocut-kernel/src/autocut_kernel/pipeline/*_request.py`
- 响应 Schema/decoder：`packages/autocut-kernel/src/autocut_kernel/vlm/` 与
  `packages/autocut-kernel/src/autocut_kernel/semantic_chain/*_draft.py`
- Provider：`auto_cut_bot/pipeline/vlm/doubao_ark_provider.py`、
  `auto_cut_bot/pipeline/vlm/ark_responses_transport.py`
- 持久化：`packages/autocut-kernel/src/autocut_kernel/store/postgres.py`
- 分阶段 debug：`auto_cut_bot/pipeline/debug/model_io.py`
