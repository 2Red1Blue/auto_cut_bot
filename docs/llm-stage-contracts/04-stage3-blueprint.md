# 04 Stage 3：编辑蓝图

## 调用前输入

Stage 3 必须重建 Stage 2 请求并证明 Stage 2 Receipt、ArtifactSet、policy hash 和
`next_action=continue` 全部一致。然后为冻结的 target Story 生成 `EditorialContextBatch`。

模型可见 context 按 Story 提供：选中的 proposal、Narrative 义务/事实、CandidateCatalog、
素材需求闭包和允许引用。模型不读取原始 VLM 文本、ASR/VAD、最终 PTS、Source 文件路径或
发布状态。

请求 Schema 根据 `target_story_ids` 动态冻结，因此模型必须返回所有目标 Story，顺序也必须
一致。禁止“生成一半也算成功”。

## 模型响应

根字段：`schema_version=stage3-editorial-blueprint-draft-v1`、
`input_binding_sha256`、`stories[]`。

每个 Story：

| 字段 | 含义 |
|---|---|
| `story_id` | 必须属于且按顺序匹配冻结目标 |
| `proposal_ref` | Stage 2 选中提案 |
| `beats[]` | 编辑节拍草案；数组位置就是 Beat ordinal |
| `ordering_constraints[]` | `precedes/adjacent/max_gap` 的确定性顺序要求 |
| `story_duration_seconds` | 整体 min/target/max |
| `editing_intent` | pacing 与 continuity priority |
| `teaser_intent` | teaser 策略和时长范围 |

每个 Beat：

| 字段 | 含义 |
|---|---|
| `narrative_role` | Beat 在故事结构中的职责 |
| `narrative_function` | hook/setup/escalation/.../aftermath |
| `summary` | Beat 要表达的内容 |
| `required_obligation_refs` | 必须履行的义务 |
| `required_fact_refs` | 必须保真的事实 |
| `evidence_requirements[]` | 每个素材需求的 event/candidate 备选集合 |
| `candidate_preferences` | 仅在已声明备选中的偏好 |
| `span_policy` | `tight/scene/context` 的偏好、允许集合、fallback 顺序 |
| `duration_seconds` | Beat min/target/max |

`span_policy` 是语义意图，不是物理端点。Stage 4 才能读取音频/视觉证据并选择 A/V span。

## 模型响应之后

Kernel 重新验证 Story/Beat/Material 引用、排序无环、需求守恒与语义可行性。N 个 Story
成功时提交 `3N+1` 个成员：每个 Story 一组
`editorial_blueprint + evidence_closure_set + context_manifest`，最后一个
`semantic_feasibility_admission`。任何 Story 不闭合时整批不提交。

## 主要拒绝码

- `STAGE3_DRAFT_OR_COMPILATION_REJECTED`
- `STAGE3_FEASIBILITY_REJECTED`
- `STAGE3_ADMISSION_REJECTED`
