# 03 Stage 2：故事设计与组合

## 调用前输入

Stage 2 只在 Stage 1 成功并可重建同一份 request/outcome/content 后运行。模型 context 为
`stage2-proposal-context-v1`：

| 字段 | 含义 |
|---|---|
| `input_binding_sha256` | Narrative、CandidateCatalog 与所有设计 Policy 的绑定 |
| `stage1_members[]` | Stage 1 已提交成员引用 |
| `source_grant` | 哪些源允许用于 `render_source`，不是文件路径 |
| `candidate_catalog` | Kernel 从 VLM 候选投影出的可选素材语义目录 |
| `episode_digest_set` | 分集摘要 |
| `event_card_set` | 已验证事件卡 |
| `narrative_graph` | 角色、事实、义务与故事线 |
| `policies` | 候选、Job 和故事设计封闭策略 |

模型看不到 ASR/VAD、最终 cut point、媒体文件路径和可发布许可。CandidateCatalog 是语义候选，
不能证明物理可剪。

## 模型响应

根字段固定为 `schema_version`、`input_binding_sha256`、`proposals[]`。每个 proposal：

`narrative_refs` 是 Kernel 从多类引用计算的 Python 属性，不是要求模型返回的 wire 字段；
模型字段是下表的 `thread_refs`、`required_obligation_refs` 等。不能据此把合法响应误判为字段缺失。

| 字段 | 含义 |
|---|---|
| `proposal_id` | 本批次唯一提案 ID |
| `title` | 故事标题 |
| `narrative_claim` | 该故事要表达的主张 |
| `thread_refs` | 采用的 Stage 1 故事线 |
| `required_obligation_refs` | 不可遗漏的叙事义务 |
| `required_fact_refs` | 不可篡改的事实 |
| `key_character_refs` | 核心角色引用 |
| `genre_tags` | 必须来自 Policy 允许集合 |
| `editing_profile` | 版本化编辑配置引用 |
| `target_duration_seconds` | 故事目标时长整数区间 |
| `teaser_strategy` | Policy 允许的预告策略 |
| `audience_hook` | 面向观众的钩子描述 |
| `material_requirements[]` | 每个义务需要什么素材、至少多少可用秒数 |

每个 material requirement 还必须声明 `dialogue_integrity`、`subtitle_clearance`、
`visual_validity` 等物理要求，以及允许/禁止 Source 引用和 `render_source` 授权目的。

## 模型响应之后

Kernel 验证所有 Narrative/Source/Candidate 引用和 Policy，执行确定性的 material support
评估与 `first_feasible_lexicographic_v1` 组合搜索。模型不能指定“最终选中哪组”来绕过搜索。

成功时原子提交 5 个成员：

```text
candidate_catalog
proposal_set
portfolio
source_usage_ledger
portfolio_admission
```

`completion_policy` 当前为 `all_or_nothing`，不会静默丢弃失败 Story 后提交部分 Portfolio。

## 主要拒绝码

- `STAGE2_DRAFT_OR_COMPILATION_REJECTED`
- `STAGE2_PORTFOLIO_INFEASIBLE`
- `STAGE2_MATERIAL_INDETERMINATE`
- `STAGE2_ADMISSION_REJECTED`
