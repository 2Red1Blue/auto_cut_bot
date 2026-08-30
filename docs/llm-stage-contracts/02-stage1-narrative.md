# 02 Stage 1：知识链与叙事图

## 调用前输入

Stage 1 不看视频、不看 ASR/VAD，也不重新请求外部剧情 API。它读取已提交的整批
`VlmSemanticPack`，为模型生成 `stage1-cross-window-draft-v1` 投影：

| 字段 | 含义 |
|---|---|
| `schema_version` | Stage 1 draft 协议版本 |
| `input_binding_sha256` | 本次 source、VLM batch、policy、每个窗口身份的整体绑定 |
| `allowed_refs[]` | 模型允许引用的 `window_manifest_sha256 + object_type + object_id` 白名单 |
| `windows[]` | 每窗口的摘要、continuity、实体、事实和事件 |

`windows[].entities` 只传 ID、类型、标签和视觉描述；`facts` 传事实类型、主客体和摘要；
`events` 传事件类型、参与者、fact/因果引用、开放问题和时间模式。原始视频、长 Blob ID、
provider response id、Receipt、物理时间和 candidate hypotheses 不进入 Stage 1 prompt。

请求还冻结模型、prompt 模板/版本、输出 Schema、temperature、token/字节/数量预算和
retry policy。这些控制信息多数属于 durable envelope，不应要求模型照抄。

## 模型响应

| 字段 | 含义 | 用途 |
|---|---|---|
| `schema_version` | 固定 `stage1-cross-window-draft-v1` | decoder 路由 |
| `input_binding_sha256` | 必须逐字返回本次绑定 | 防止错批次响应被接纳 |
| `beats[]` | `beat_id`、摘要、叙事阶段、event refs、义务 ID | 编译叙事节拍 |
| `obligations[]` | 义务描述、必需 fact refs、成功条件 | Coverage Admission 的目标 |
| `story_threads[]` | 标题、前提、关联义务 | Stage 2 故事提案输入 |
| `merge_proposals[]` | 跨窗口实体合并建议与证据 | 仅是提案，Kernel 独立验证 |

`phase` 只能是 `setup/escalation/turn/reveal/payoff/consequence/coda`。模型引用只能来自
`allowed_refs`；不能用摘要文本、角色名或自造 ID 代替引用。

## 模型响应之后

Kernel 重新解码 raw bytes、核对输入绑定与引用，然后计算并原子提交 8 个成员：

```text
event_card_set
episode_digest_set
narrative_graph
evidence_diagnostics
conflict_diagnostics
coverage_ledger
dependency_closure_proof
coverage_admission
```

模型不能直接返回上述 Admission，也不能把未覆盖内容自动标成水内容。Coverage 或依赖闭合
不成立时，本阶段 `denied`，Stage 2 不会启动。

## 主要拒绝码

- `STAGE1_DRAFT_OR_COMPILATION_REJECTED`：JSON、预算、ID、引用、绑定或编译失败。
- `STAGE1_COVERAGE_REJECTED`：draft 可解析，但覆盖、冲突、授权或依赖 Admission 不允许继续。
