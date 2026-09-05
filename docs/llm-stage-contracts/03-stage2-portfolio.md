# 03 Stage 2：故事设计与组合

版本说明：下文 v1 仍用于旧请求。新代码还支持显式 `generation.prompt_version=stage2-proposal-compact-v2`，
尚未全局替换 HTTP 默认。新版本调用前必须同时选取对应 prompt/schema/decoder，不能只改提示词文本。

## compact-v2 的差异

模型仍获得分集、主体、事实、事件、故事线、义务、候选和策略选择；完整 owner/hash 留私有 envelope。
短引用按请求绑定为 p/f/e/t/o/c/s，辅助图节点使用仅输入的 n 命名空间；不能跨请求复用短 ID。

| 模型输出 | 程序处理 |
|---|---|
| `title/narrative_claim/audience_hook` | 保留模型叙事表达 |
| `thread_refs/obligation_refs/key_subject_refs` | 从私有映射恢复准确引用；主体允许 observed person 或已建立 character，绝不混称 |
| `genre_tags/editing_profile_ref/teaser_strategy/target_duration_seconds` | 检查给定策略，恢复精确版本化配置 |
| `material_requirements` | 绑定所选义务，合并强制安全检查；源允许范围只能收紧 |

v2 根只有 schema discriminator 与非空 proposals；模型不再回填 input hash、proposal ID、required fact closure。
每个材料项返回 `obligation_ref/minimum_usable_seconds/additional_checks/source_constraints`。
`source_constraints.source_selection` 为 all_granted 或 subset，后者必须有合法非空 allowed_source_refs。
未知字段/引用仍拒绝，不做拼写猜测和业务默认补全。v1→v2 纯迁移会记录类型与事实闭包差异，
不等于已经提供了可提交的 Stage 2 派生 Command。

详细字段边界见 [09 §4](./09-model-boundary-refactor.md#4-stage-2-compact-v2-的可实施合同)，
单阶段入口见 [10](./10-local-recovery-and-stage2-runbook.md)。

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
